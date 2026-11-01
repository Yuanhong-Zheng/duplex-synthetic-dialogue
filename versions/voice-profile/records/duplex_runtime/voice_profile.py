"""Compile the frozen Stage-1 user voice into pinned, auditable TTS settings."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re

import requests

from duplex_runtime.call_journal import JournalModel
from duplex_runtime.speech import SpeechError, SpeechSettings, digest, sanitize
from duplex_runtime.user_simulator import json_hash, write_json

PROMPT = Path(__file__).parent/'prompts/voice_profile.md'


def fetch_catalog(secret, directory):
    if not secret:raise SpeechError('DASHSCOPE_API_KEY is required for voice catalog lookup')
    directory = Path(directory); directory.mkdir(parents=True,exist_ok=True)
    request = {'model':'MiniMax/speech-2.8-turbo','input':{'action':'get_voice','voice_type':'system'}}
    if (directory/'catalog.json').exists():
        saved=json.loads((directory/'catalog.json').read_text())
        if saved['request']!=request:raise SpeechError('voice catalog request changed')
        return saved['voices']
    write_json(directory/'request.json',request)
    response=requests.post(SpeechSettings().endpoint,json=request,
                          headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'},
                          timeout=(10,60),allow_redirects=False)
    value=response.json();write_json(directory/'response.json',sanitize(value,secret))
    if response.status_code!=200 or value.get('output',{}).get('base_resp',{}).get('status_code')!=0:
        raise SpeechError('voice catalog API failed; see saved redacted response')
    voices=[{k:v[k] for k in ('voice_id','voice_name','description')} for v in value['output']['system_voice']]
    if not voices or len({v['voice_id'] for v in voices})!=len(voices):
        raise SpeechError('invalid system voice catalog')
    write_json(directory/'catalog.json',{'request':request,'voices':voices})
    return voices


def gender(text):
    male=bool(re.search(r'男性|男声|男童|男孩|男音|(?<!fe)male',text,re.I))
    female=bool(re.search(r'女性|女声|女童|女孩|女音|female',text,re.I))
    return 'male' if male and not female else 'female' if female and not male else None


def age(text):
    if re.search(r'老年|老人|六十|七十|八十|九十|花甲|[6-9]\d\s*岁',text):return 'elder'
    if re.search(r'中年|[四五]十|[45]\d\s*岁',text):return 'middle'
    if re.search(r'儿童|[男女]童|小孩',text):return 'child'
    if re.search(r'青年|二十|三十|[23]\d\s*岁',text):return 'young'
    if re.search(r'少年|青少年|十几',text):return 'teen'
    return None


def candidate_errors(value, profile, catalog):
    expected={'voice_id','speed','pitch','emotion','voice_modify','reason','unmatched'}
    if not isinstance(value,dict) or set(value)!=expected:return ['voice plan fields differ from the output contract']
    voices={v['voice_id']:v for v in catalog}
    if value['voice_id'] not in voices:return ['voice_id is absent from the verified catalog']
    if type(value['speed']) not in (float,int) or not .5<=value['speed']<=2:return ['invalid speech speed']
    if not isinstance(value['voice_modify'],dict):return ['voice_modify must be an object']
    if not isinstance(value['reason'],str) or not value['reason'].strip():return ['missing mapping reason']
    if not isinstance(value['unmatched'],list) or any(not isinstance(x,str) for x in value['unmatched']):return ['unmatched must be strings']
    try:
        SpeechSettings(voice_id=value['voice_id'],speed=value['speed'],pitch=value['pitch'],
                       emotion=value['emotion'],voice_modify=value['voice_modify'])
    except (ValueError,TypeError) as exc:return [str(exc)]
    selected=voices[value['voice_id']]
    desc=selected['voice_id']+' '+selected['voice_name']+' '+' '.join(selected['description'])
    wanted=gender(profile)
    errors=[]
    if wanted and gender(desc)!=wanted:errors.append('selected voice does not match the frozen perceived gender')
    desired_age=age(profile)
    compatible=[v for v in catalog if gender(v['voice_id']+' '+' '.join(v['description']))==wanted
                and age(' '.join(v['description']))==desired_age]
    if desired_age and compatible and age(desc)!=desired_age:
        errors.append('selected voice does not match the available frozen age group')
    return errors


def compile_profile(profile, catalog, client, directory, *, previous_voice_ids=()):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    context={'voice_profile':profile,'available_voices':catalog,'previous_voice_ids':list(previous_voice_ids)}
    identity=json_hash({'context':context,'prompt':PROMPT.read_text()})
    final=directory/'voice-plan.json'
    if final.exists():
        result=json.loads(final.read_text())
        if result['identity_sha256']!=identity:raise SpeechError('frozen voice mapping input changed')
        validate_plan(result,profile)
        return result
    recorded=JournalModel(client,directory/'journal','voice-profile')
    feedback=None
    for attempt in range(3):
        folder=directory/f'attempt-{attempt+1:02d}';folder.mkdir(exist_ok=True)
        actual={**context,**({'repair_feedback':feedback} if feedback else {})}
        write_json(folder/'input.json',actual);(folder/'prompt.md').write_text(PROMPT.read_text())
        raw,meta=recorded.complete(PROMPT.read_text(),actual,max_tokens=1800,response_format={'type':'json_object'})
        (folder/'raw_response.txt').write_text(raw);write_json(folder/'metadata.json',sanitize(meta))
        try:value=json.loads(raw);errors=candidate_errors(value,profile,catalog)
        except ValueError: value=None;errors=['invalid JSON']
        write_json(folder/'validation.json',{'errors':errors,'status':'failed' if errors else 'passed'})
        if errors:
            feedback={'errors':errors,'rejected_output':raw}
            continue
        settings=SpeechSettings(voice_id=value['voice_id'],speed=float(value['speed']),pitch=value['pitch'],
                                emotion=value['emotion'],voice_modify=value['voice_modify'] or None,subtitle_type='word')
        result={'schema':'duplex-runtime.user-voice-plan.v1','identity_sha256':identity,
                'voice_profile':profile,'voice_profile_sha256':json_hash(profile),'catalog_sha256':json_hash(catalog),
                'mapping':value,'settings':asdict(settings),'attempts':attempt+1,
                'source':'frozen_stage1_voice_profile','fixed_for_whole_episode':True}
        write_json(final,result);return result
    raise SpeechError('voice profile mapping failed after two model corrections; all attempts retained')


def validate_plan(plan, profile):
    if plan.get('schema')!='duplex-runtime.user-voice-plan.v1' or plan.get('voice_profile')!=profile or plan.get('voice_profile_sha256')!=json_hash(profile):
        raise SpeechError('voice plan differs from the frozen Stage-1 voice profile')
    settings=SpeechSettings(**plan['settings'])
    for key in ('voice_id','speed','pitch','emotion'):
        if getattr(settings,key)!=plan['mapping'][key]:raise SpeechError('voice plan settings differ from mapping')
    if (settings.voice_modify or {})!=plan['mapping']['voice_modify']:
        raise SpeechError('voice characteristic settings differ from mapping')
    return settings
