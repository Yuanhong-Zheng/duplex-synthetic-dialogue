"""Modern Stage 0–5 → admitted dialogue → scoring/export, with bounded recovery."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

from duplex_offline_workflow import finalize_case
from duplex_runtime.batch import create_batch, run_batch
from duplex_runtime.finish_batch import finish
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_batch import verify_batch
from duplex_runtime.verify_simulator import configured_clients
from duplex_runtime.verify_synthetic import read, require
from run_with_api_profile import profile_environment

ROOT = Path(__file__).resolve().parent


def transient_transport_failure(failure):
    """Only known transport failures; never retry semantic rejections or permissions."""
    if not isinstance(failure,dict): return False
    if failure.get('type') in {'TimeoutError','ReadTimeout','ConnectTimeout','ConnectionError','ChunkedEncodingError'}: return True
    return (failure.get('type'),failure.get('reason')) in {
        ('SpeechError','TTS transport failed; retry the same accepted event explicitly'),
        ('AssistantError','assistant stream transport failed'),
    }


def complete_batch(batch, credentials, profile, *, max_attempts=3):
    batch = Path(batch).resolve(); manifest=read(batch/'manifest.json')
    require(bool(os.environ.get('DASHSCOPE_API_KEY')),'DASHSCOPE_API_KEY must be supplied in memory')
    for job in manifest['jobs']:
        folder=batch/'jobs'/job['episode_id']; state=read(folder/'state.json')
        require(state['status']!='running','a worker is already running; do not start a competing recovery')
        while state['status']!='completed':
            if state['status']=='failed':
                from repair_runtime_anchor import missing_anchor_failure,repair_missing_anchor
                from duplex_runtime.fork_runtime import create_runtime_branch
                turn=missing_anchor_failure(folder)
                if turn:
                    remaining=2-manifest.get('runtime_anchor_repair_counts',{}).get(turn,0)
                    require(remaining>0,'runtime anchor exhausted two model repairs and re-reviews: '+turn)
                    client,_=configured_clients(credentials,profile,manifest['model'],manifest['model'])
                    client.judge_model=manifest['model']
                    prior_repair=ROOT/'runs'/(batch.name+'-'+turn+'-anchor-repair')
                    repaired=repair_missing_anchor(batch,job['episode_id'],ROOT/'runs'/(batch.name+'-'+turn+'-anchor-repair-owner-v2'),client,
                                                   max_repairs=remaining,reuse_from=prior_repair if prior_repair.exists()else None)
                    child=ROOT/'runtime_runs'/(batch.name+'-'+turn+'-repaired-v2')
                    if not child.exists():
                        create_runtime_branch(batch,job['episode_id'],Path(repaired['prepared']),child,turn_id=turn,anchor_repair_attempts=len(repaired['attempts']))
                    result=complete_batch(child,credentials,profile,max_attempts=max_attempts)
                    return {**result,'stage5':result.get('stage5',repaired['stage5']),'prepared':result.get('prepared',repaired['prepared']),
                            'anchor_repair_runs':[repaired['stage5'],*result.get('anchor_repair_runs',[])],
                            'parent_batches':[str(batch),*result.get('parent_batches',[])]}
                prior=folder/state['latest_rollout'];failure=read(prior/'result.json').get('failure')
                approved_t02=(bool(manifest.get('tts_alignment_migrations')) and
                    failure=={'type':'SpeechError','reason':'edge trim destroyed a timestamp boundary; inspect this sample'} and
                    not read(prior/'user-speech/manifest.json').get('tts_alignment_policy',{}).get('enabled'))
                require(transient_transport_failure(failure) or approved_t02,'runtime failure remained after its owner repair budget: '+str(failure))
                write_json(folder/f'automatic-transport-recovery-{len(state["attempts"])+1:03d}.json',
                    {'failure':failure,'same_logical_episode':True,'same_requests_prompts_voices_and_timing':True,
                     'max_total_attempts':max_attempts,'changed_data_or_semantic_rules':False})
            require(len(state['attempts'])<max_attempts,'runtime transport recovery limit reached')
            run_batch(batch,credentials,profile,[job['episode_id']]); state=read(folder/'state.json')
    from duplex_runtime.migrate_processing import upgrade_processing
    upgrade_processing(batch)
    processed=finish(batch,credentials,profile,workers=1)
    require(processed['status']=='completed','scoring/export did not complete; preserve processing records')
    proof=verify_batch(batch);write_json(batch/'verification.json',proof)
    return {'status':'passed','batch':str(batch),'verification':str(batch/'verification.json')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--credentials-file',type=Path,required=True);p.add_argument('--profile',default='commonstack-chat')
    p.add_argument('--run-id');p.add_argument('--scenario-ids');p.add_argument('--stage35-run',type=Path)
    p.add_argument('--stage0-run',type=Path,help='reuse an already passed fresh Stage-0 run, then generate its missing common-QA directions')
    p.add_argument('--resume-batch',type=Path);p.add_argument('--cases')
    p.add_argument('--seed',type=int,default=20261031);p.add_argument('--workers',type=int,default=2)
    p.add_argument('--model',default='gpt-5.6-sol');p.add_argument('--judge-model',default='gpt-5.6-sol')
    p.add_argument('--runtime-model',default='openai/gpt-5.6-sol')
    p.add_argument('--taxonomy',type=Path,default=ROOT/'taxonomy/domain-scenario-with-task-examples-gpt56-sol-v5.json')
    p.add_argument('--user-voices',default='male-qn-qingse');p.add_argument('--assistant-voices',default='female-chengshu')
    p.add_argument('--max-runtime-attempts',type=int,default=3)
    p.add_argument('--audit-repair-resume',type=Path,action='append',default=[],help='revalidate saved model candidates in a new repair version')
    p.add_argument('--prepare-only',action='store_true',help='stop at admitted frozen environments; no audio or dataset is claimed')
    args=p.parse_args()
    require(1<=args.workers<=4 and 1<=args.max_runtime_attempts<=3,'invalid worker or bounded recovery count')
    os.environ.update(profile_environment(args.credentials_file,args.profile))
    if args.resume_batch:
        result=complete_batch(args.resume_batch,args.credentials_file,args.profile,max_attempts=args.max_runtime_attempts)
        print(json.dumps(result,ensure_ascii=False),flush=True);return
    require(args.run_id and (args.scenario_ids or args.stage35_run),'supply a run ID and fresh scenarios or an admitted Stage-3.5 source')
    require(not(args.scenario_ids and args.stage35_run),'choose either fresh generation or a Stage-3.5 continuation')
    work=ROOT/'work'/args.run_id;work.mkdir(parents=True,exist_ok=True)
    def command(stage,*options):
        target=ROOT/'runs'/(args.run_id+'-stage'+stage)
        require(not target.exists(),'use a fresh run ID; existing stage outputs are immutable')
        cmd=[sys.executable,'-B',str(ROOT/'run_with_api_profile.py'),'--credentials-file',str(args.credentials_file),'--profile',args.profile,
             '--',*options,'--run-id',target.name,'--model',args.model,'--judge-model',args.judge_model,'--workers',str(args.workers)]
        write_json(work/('stage'+stage+'-command.json'),{'command':cmd})
        with (work/('stage'+stage+'.log')).open('w')as log: result=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        require(result.returncode==0,'Stage '+stage+' failed after its repair budget; retained logs require discussion')
        return target
    stages={}
    if args.scenario_ids:
        taxonomy_three=work/'taxonomy-three-styles.json';taxonomy=work/'taxonomy.json';scenes=args.scenario_ids.split(',')
        if args.stage0_run:
            stages['0']=args.stage0_run.resolve();prior=read(stages['0']/'manifest.json')
            require(prior['scenario_ids']==scenes and not prior['failures'],'Stage 0 continuation changed scenes or reused a failed run')
            taxonomy_three=Path(prior['output_taxonomy'])
        else:
            stages['0']=command('0','stage0','--taxonomy',str(args.taxonomy),'--scenario-ids',args.scenario_ids,'--output',str(taxonomy_three),'--no-html')
        for scene in scenes:require(read(stages['0']/scene/'validation.json')['status']=='passed','Stage 0 gate did not pass')
        stages['0qa']=command('0qa','stage0','--taxonomy',str(taxonomy_three),'--scenario-ids',args.scenario_ids,
                              '--style','常识问答','--force','--output',str(taxonomy),'--no-html')
        for scene in scenes:require(read(stages['0qa']/scene/'validation.json')['status']=='passed','Stage 0 common-QA gate did not pass')
        stages['1']=command('1','stage1','--count',str(len(scenes)),'--seed',str(args.seed),'--scenario-ids',args.scenario_ids,
                            '--taxonomy',str(taxonomy),'--goal-memory',str(work/'goal-memory.json'),'--no-html')
        stages['2']=command('2','stage2','--source-run',str(stages['1']),'--no-html')
        stages['3']=command('3','stage3','--source-run',str(stages['2']))
        stages['35']=command('35','stage3.5','--source-run',str(stages['3']),'--side-speech-mode','all','--backchannel-mode','all')
        case_ids=[c['case_id']for c in read(stages['1']/'manifest.json')['cases']]
    else:
        stages['35']=args.stage35_run.resolve()
        case_ids=args.cases.split(',') if args.cases else read(stages['35']/'manifest.json')['case_ids']
    def voices(value):
        result=value.split(',')
        require(len(result)in(1,len(case_ids)),'provide one voice or one per case')
        return result*len(case_ids) if len(result)==1 else result
    users,assistants=voices(args.user_voices),voices(args.assistant_voices)
    def episode(item):
        index,case=item;prefix=args.run_id+'-'+case
        client,_=configured_clients(args.credentials_file,args.profile,args.model,args.judge_model);client.judge_model=args.judge_model
        offline=finalize_case(stages['35'],case,prefix,client,repair_resumes=args.audit_repair_resume)
        write_json(work/(case+'-offline.json'),offline)
        if args.prepare_only: return {**offline,'status':'prepared','pipeline_stages':{k:str(v)for k,v in stages.items()}}
        batch=ROOT/'runtime_runs'/(prefix+'-batch')
        create_batch(batch,[Path(offline['prepared'])],1,1,args.seed+index,args.runtime_model,
                     user_voice_id=users[index],assistant_voice_id=assistants[index])
        completed=complete_batch(batch,args.credentials_file,args.profile,max_attempts=args.max_runtime_attempts)
        entry={**offline,**completed,'pipeline_stages':{k:str(v)for k,v in stages.items()}}
        write_json(work/(case+'-complete.json'),entry);return entry
    with ThreadPoolExecutor(max_workers=args.workers)as pool: results=list(pool.map(episode,enumerate(case_ids)))
    status='prepared' if args.prepare_only else 'completed'
    write_json(work/'result.json',{'status':status,'cases':results,'manual_artifact_repairs':False})
    print(json.dumps({'status':status,'episodes':len(results),'result':str(work/'result.json')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
