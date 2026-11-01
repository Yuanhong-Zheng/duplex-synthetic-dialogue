"""Re-synthesize a recorded user's speech and rebuild an audio-only playback view.

Frozen words and original assistant PCM are reused. No policy, listener, trigger,
Guard, task Judge or training export is rerun or attributed to the new audio.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil

import numpy as np

from duplex_runtime.alignment import rendered_spans
from duplex_runtime.audio_gain import scale_pcm16
from duplex_runtime.call_journal import JournalTTS
from duplex_runtime.remix_ambient import encode_stereo
from duplex_runtime.speech import DashScopeTTSClient, SpeechError, SpeechRenderer, decode_wav, digest, encode_wav
from duplex_runtime.user_simulator import json_hash, write_json
from duplex_runtime.voice_profile import validate_plan


def boundary(spans, char):
    """Use the provider word END covering the prefix; never estimate by text length."""
    return next(s.end_frame for s in spans if s.end_char>=char) if char else 0


def map_local(offset, clip):
    if clip.get('unchanged_audio'):return offset
    old,new=clip['old_render'],clip['new_render']
    if offset>=old['frames']:return new['frames']+offset-old['frames']
    if old.get('pause') and old['pause']['start_frame']<=offset<=old['pause']['end_frame']:
        return new['pause']['start_frame']+offset-old['pause']['start_frame']
    heard=[s for s in clip['old_spans'] if s.end_frame<=offset]
    if not heard:return min(offset,new['frames'])
    last=heard[-1]
    return min(new['frames'],boundary(clip['new_spans'],last.end_char)+offset-last.end_frame)


def arrange(clips):
    """Keep original assistant-relative interruptions and remap incoming word anchors."""
    arranged=[];reservations={}
    for clip in sorted(clips,key=lambda c:(c['old_start'],c['order'])):
        old=clip['old_start'];anchor=None
        if clip.get('owner'):
            parent=next(c for c in arranged if c['id']==clip['owner'])
            start=parent['start_frame']+boundary(parent['new_spans'],clip['boundary_char'])
            anchor={'audio_id':parent['id'],'provider_end_char':clip['boundary_char']}
        else:
            opposite='assistant' if clip['speaker']=='user' else 'user'
            active=[c for c in arranged if c['speaker']==opposite and c['old_start']<=old<c['old_start']+c['old_frames']]
            if active:
                parent=max(active,key=lambda c:c['old_start']);offset=old-parent['old_start']
                local=offset if parent['speaker']=='assistant' else map_local(offset,parent)
                start=parent['start_frame']+local
                anchor={'audio_id':parent['id'],'source_offset_frame':offset,'mapped_offset_frame':local}
            else:
                ended=[c for c in arranged if c['old_start']+c['old_frames']<=old]
                if ended:
                    parent=max(ended,key=lambda c:c['old_start']+c['old_frames'])
                    gap=old-parent['old_start']-parent['old_frames']
                    start=parent['start_frame']+parent['frames']+gap
                    anchor={'audio_id':parent['id'],'after_end_frames':gap}
                else:start=old
        physical=clip.get('source_speaker',clip['speaker'])
        earliest=reservations.get(physical,0)
        clip['start_frame']=max(start,earliest)
        clip['playback_anchor']=anchor
        clip['same_speaker_wait_frames']=max(0,earliest-start)
        reservations[physical]=clip['start_frame']+clip['frames']
        if clip['speaker']=='user' and any(c['speaker']=='assistant' and c['old_start']<=old<c['old_start']+c['old_frames'] for c in arranged):
            parent=next(c for c in reversed(arranged) if c['speaker']=='assistant' and c['old_start']<=old<c['old_start']+c['old_frames'])
            if clip['start_frame']>=parent['start_frame']+parent['frames']:
                raise SpeechError('new user voice cannot retain the recorded assistant overlap; discuss before changing the schedule')
        arranged.append(clip)
    return arranged


def rerender(source, prepared, view_summary, plan_file, output, secret):
    source,prepared,view_summary,plan_file,output=map(lambda p:Path(p).resolve(),(source,prepared,view_summary,plan_file,output))
    output.mkdir(parents=True,exist_ok=True)
    read=lambda p:json.loads(p.read_text())
    plan=read(plan_file);profile=read(prepared/'frozen/user_voice.json')['voice_profile']
    settings=validate_plan(plan,profile)
    identity={'source':str(source),'prepared_sha256':digest(prepared/'session.json'),
              'view_sha256':digest(view_summary),'plan_sha256':digest(plan_file)}
    manifest=output/'manifest.json'
    if manifest.exists() and read(manifest)['identity']!=identity:raise SpeechError('audio rerender source or profile changed')
    write_json(manifest,{'schema':'duplex-runtime.audio-rerender.v1','identity':identity,'is_new_model_trajectory':False})
    if (output/'result.json').exists() and read(output/'result.json')['status']=='completed_audio_rerender':
        result=read(output/'result.json')
        if digest(output/result['audio_path'])!=result['audio_sha256']:raise SpeechError('completed rerender audio changed')
        return result
    attempt=output/'attempts'/f'{len(list((output/"attempts").glob("*")))+1:03d}'
    attempt.mkdir(parents=True)
    tts=JournalTTS(DashScopeTTSClient(secret,settings),output/'journal','user-tts')
    renderer=SpeechRenderer(prepared,attempt/'user-speech',tts,voice_acceptance_reason='Stage-1 voice_profile 自动映射',voice_plan=plan)
    summary=read(view_summary);view=copy.deepcopy(summary['results_explorer']['synthetic_rollout'])
    allowed={s['audio_id'] for s in view['segments']}
    users=read(source/'user-results.json');timeline=read(source/'timeline.json')
    placements={p['audio_id']:p for p in timeline['placements'] if p['audio_id'] in allowed}
    gain_overrides={}
    if view.get('audio_derivative'):
        recipe=read((view_summary.parent/view['audio_derivative']['recipe_path']).resolve())
        gain_overrides={c['audio_id']:c['gain'] for c in recipe['clips']}
    order={}
    for e in timeline['events']:
        if e['kind'] in ('user_audio_started','assistant_buffer_ready'):
            order[e.get('audio_id',e.get('response_id'))]=e['sequence']
    clips=[];changed=0
    def add(identifier,new,old,speaker,**extra):
        p=placements[identifier]
        clips.append({'id':identifier,'speaker':speaker,'kind':p.get('kind','assistant'),
                      'text':p['text'],'old_start':p['start_frame'],'old_frames':p['frames'],
                      'order':order.get(identifier,0),'frames':new['frames'],'path':new['path'],
                      'old_render':old,'new_render':new,**extra})
    for record in users:
        event=record['event'];old=record['rendered'];identifier=f"user-{event['sequence']:03d}"
        if identifier in allowed and old.get('main'):
            if event.get('semantic_guard')!={'decision':'pass','findings':[]}:
                raise SpeechError('cannot rerender a rejected user utterance')
            folder=renderer.directory/'events'/identifier;folder.mkdir()
            new=renderer._main_audio(event,folder)
            old_spans,_=rendered_spans(source/'user-speech',old['main']);new_spans,_=rendered_spans(renderer.directory,new)
            new['path']=str((renderer.directory/new['path']).relative_to(output))
            add(identifier,new,old['main'],'user',old_spans=old_spans,new_spans=new_spans)
            write_json(folder/'render.json',{'source_event_sha256':json_hash(event),'source_event':event,'main':new})
            changed+=1;print(f"{output.name}: 用户音频 {changed} 段已就绪",flush=True)
        for aside in old.get('side_speech',[]):
            match=[p for p in placements.values() if p['path']=='user-speech/'+aside['path']]
            if not match:continue
            p=match[0];side_id=p['audio_id']
            if aside['speaker']=='user' and aside['placement']=='during_user':
                raise SpeechError('disabled self-interruption remains in the requested playback view')
            if aside['speaker']=='user':
                asset=renderer._asset(aside['text']);pcm=decode_wav((renderer.directory/asset['path']).read_bytes())
                gain=gain_overrides.get(side_id,aside.get('gain'))
                if not gain:raise SpeechError('the recorded user-aside volume is missing')
                target=attempt/(side_id+'.wav');target.write_bytes(encode_wav(scale_pcm16(pcm,gain['factor'])))
                new={**asset,'path':str(target.relative_to(output)),'sha256':digest(target),'gain':gain}
                old_spans,_=rendered_spans(source/'user-speech',{'segments':[aside],'text':aside['text'],'frames':aside['frames']})
                new_spans,_=rendered_spans(renderer.directory,{'segments':[asset],'text':aside['text'],'frames':asset['frames']})
            else:
                target=attempt/(side_id+'.wav');shutil.copyfile(source/p['path'],target)
                new={**aside,'path':str(target.relative_to(output))}
            extra={'source_speaker':aside['speaker']}
            if aside['speaker']=='user':extra.update(old_spans=old_spans,new_spans=new_spans)
            else:extra['unchanged_audio']=True
            if aside['placement']=='during_user':extra.update(owner=identifier,boundary_char=aside['schedule']['boundary_end_char'])
            add(side_id,new,aside,'user',**extra)
    for identifier,p in placements.items():
        if p['speaker']!='assistant':continue
        target=attempt/'assistant'/Path(p['path']).name;target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source/p['path'],target)
        new={'frames':p['frames'],'path':str(target.relative_to(output)),'sha256':digest(target)}
        add(identifier,new,new,'assistant')
    if {c['id'] for c in clips}!=allowed:raise SpeechError('recorded playback segment coverage differs')
    clips=arrange(clips)
    original=read(source/'result.json')
    tail=max(0,original['audio']['frames']-max(p['start_frame']+p['frames'] for p in timeline['placements']))
    length=max(c['start_frame']+c['frames'] for c in clips)+tail
    mixed=np.zeros((length,2),dtype=np.int32)
    for c in clips:
        pcm=decode_wav((output/c['path']).read_bytes())
        if len(pcm)!=c['frames']:raise SpeechError('rendered clip frame count differs')
        mixed[c['start_frame']:c['start_frame']+c['frames'],0 if c['speaker']=='user' else 1]+=pcm
    if np.max(np.abs(mixed),initial=0)>32767:raise SpeechError('rerender would clip PCM; do not silently normalize')
    audio=output/'dialogue-stereo.wav';audio.write_bytes(encode_stereo(mixed))
    rendered=[]
    for segment in view['segments']:
        c=next(c for c in clips if c['id']==segment['audio_id'])
        segment.update(start_seconds=c['start_frame']/16000,duration_seconds=c['frames']/16000)
        rendered.append({k:v for k,v in c.items() if k not in ('old_spans','new_spans')})
    write_json(output/'playback-timeline.json',{'schema':'duplex-runtime.rerendered-playback.v1',
        'is_new_model_trajectory':False,'sample_rate':16000,'frames':length,'clips':rendered})
    view.update(duration_seconds=length/16000,is_new_model_trajectory=False,audio_path='dialogue-stereo.wav',
                audio_edit_note='用户声音按冻结的 Stage 1 voice_profile 重新合成；主句、附和及用户旁语使用同一配置。助手原录音与双方台词复用，播放时间轴已重排；原模型判断、评分和训练导出未重跑。')
    view.pop('audio_derivative',None)
    write_json(output/'playback-view.json',view);write_json(output/'voice-plan.json',plan)
    result={'status':'completed_audio_rerender','is_new_model_trajectory':False,'user_utterances_regenerated':False,
            'assistant_responses_regenerated':False,'assistant_pcm_reused':True,'policy_or_task_judge_calls':0,
            'user_audio_segments':changed,'tts_logical_calls':renderer.model_calls,'audio_path':audio.name,
            'audio_sha256':digest(audio),'duration_seconds':length/16000,'voice_settings':plan['settings']}
    write_json(output/'result.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-rollout','prepared-session','view-summary','voice-plan','output-dir'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(rerender(args.source_rollout,args.prepared_session,args.view_summary,args.voice_plan,
                             args.output_dir,os.environ.get('DASHSCOPE_API_KEY','')),ensure_ascii=False))
