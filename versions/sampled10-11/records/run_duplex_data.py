"""Modern Stage 0–5 → admitted dialogue → scoring/export, with bounded recovery."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

from duplex_offline_workflow import finalize_case,OfflineBudgetExhausted
from duplex_runtime.batch import create_batch, run_batch
from duplex_runtime.finish_batch import finish
from duplex_runtime.speech import digest
from duplex_runtime.speech import SpeechError
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_batch import verify_batch
from duplex_runtime.verify_simulator import configured_clients
from duplex_runtime.verify_synthetic import read, require
from run_with_api_profile import profile_environment
from judge_output_repair import JudgeFormatExhausted
from step_05_repair_environment_audit import RubricRefreshExhausted
from record_model_call import RecordedCalls

ROOT = Path(__file__).resolve().parent


def transient_transport_failure(failure):
    """Only known transport failures; never retry semantic rejections or permissions."""
    if not isinstance(failure,dict): return False
    if failure.get('type') in {'TimeoutError','ReadTimeout','ConnectTimeout','ConnectionError','ChunkedEncodingError'}: return True
    return (failure.get('type'),failure.get('reason')) in {
        ('SpeechError','TTS transport failed; retry the same accepted event explicitly'),
        ('AssistantError','assistant stream transport failed'),
    }


class PipelineSampleFailed(RuntimeError):
    """A retained sample exhausted an authorized model/request budget."""
    pass


def request_budget_exhausted(directory):
    directory=Path(directory)
    def unresolved(path):
        for parent in path.parents:
            if parent==directory.parent:break
            if (parent/'identity.json').exists()and(parent/'commit.json').exists():return False
        return True
    for path in Path(directory).rglob('*request-history.json'):
        if read(path).get('exhausted')and unresolved(path):return True
    for path in Path(directory).rglob('metadata.json'):
        if read(path).get('request_retries_exhausted')and unresolved(path):return True
    for path in Path(directory).rglob('request-failure.json'):
        if read(path).get('request_retries_exhausted')and unresolved(path):return True
    return False


def stage_gate(directory):
    """Consume deterministic stage status; unknown code errors stay actionable."""
    directory=Path(directory);require((directory/'manifest.json').exists(),'stage has an incomplete manifest; retain its calls and inspect')
    manifest=read(directory/'manifest.json')
    failed=[read(p)for p in directory.glob('*/validation.json')if read(p).get('status')!='passed']
    failures=manifest.get('failures',[])
    errors=' '.join(str(v)for v in failures+failed)
    if request_budget_exhausted(directory)or any(x in errors for x in (
        'request exhausted five','HTTP 403: configuration or authorization error','request_retries_exhausted',
        'HTTP Error 429','HTTP Error 500','HTTP Error 502','HTTP Error 503','HTTP Error 504')):
        raise PipelineSampleFailed('stage request exhausted its five-retry client budget; '+errors[:800])
    if 'evaluator output remained malformed after two model corrections'in errors:
        raise PipelineSampleFailed('stage Judge exhausted its two format corrections; '+errors[:800])
    identifiers=manifest.get('scenario_ids')or manifest.get('case_ids')or [c['case_id']for c in manifest.get('cases',[])]
    require(identifiers and all((directory/identifier/'validation.json').exists()for identifier in identifiers),
        'stage is missing case validation records; inspect its incomplete execution')
    if not failed and not failures:return
    require(not failures and not any(v.get('stage35_status')=='rejected_request_error'for v in failed),
        'unclassified stage execution error; inspect original records: '+errors[:800])
    raise PipelineSampleFailed('stage model corrections and original review did not admit the sample; '+errors[:800])


def expected_runtime_failure(folder,failure):
    if request_budget_exhausted(folder/'journal'):return True
    kind,reason=failure.get('type'),failure.get('reason','')
    if kind=='ConfigurationError'and reason=='OpenAI-compatible HTTP 403: configuration or authorization error':
        # Older journals saved only the terminal error. Its pinned control flow
        # raises this exact error only after all five retries. Preserve that
        # evidence without fabricating missing individual HTTP responses.
        batch=folder.parents[1];manifest=read(batch/'manifest.json');snapshot=batch/'code_snapshot'
        names=['minimax_client.py','duplex_runtime/batch.py']
        if manifest.get('request_retry_policy',{}).get('max_retries')==5 and all(digest(snapshot/name)==manifest['code_sha256'][name]for name in names):
            if 'if attempt >= self.max_retries and exc.code < 500'in(snapshot/names[0]).read_text()and'user.max_retries = guard.max_retries = 5'in(snapshot/names[1]).read_text():
                write_json(folder/'legacy-terminal-request-evidence.json',{'failure':failure,'max_retries':5,
                    'inferred_from_pinned_control_flow':True,'source_sha256':{name:manifest['code_sha256'][name]for name in names},
                    'per_retry_raw_responses_available':False,'new_network_requests':0})
                return True
    if kind=='TTSAlignmentError'and 'exhausted two'in reason:return True
    if kind=='RolloutBlocked'and reason in {
        'autonomous user made no mainline progress after two observed-silence opportunities',
        'event limit reached; no frozen turn skipped'}:return True
    latest=folder/read(folder/'state.json')['latest_rollout']
    if kind in {'PolicyRejected','GuardFormatExhausted','PreparationError'}:
        return len(list((latest/'user').glob('rejection-*.json')))>=3 or kind=='GuardFormatExhausted'
    if kind=='AssistantError':
        return any(len(list(p.glob('*/validation.json')))>=3 for p in(latest/'assistant').glob('*/attempts'))
    return False


def complete_batch(batch, credentials, profile, *, max_attempts=3):
    batch = Path(batch).resolve(); manifest=read(batch/'manifest.json')
    require(bool(os.environ.get('DASHSCOPE_API_KEY')),'DASHSCOPE_API_KEY must be supplied in memory')
    # A complete, already verified episode does not need a new processing pass.
    if (batch/'verification.json').exists()and read(batch/'verification.json').get('status')=='passed':
        return {'status':'passed','batch':str(batch),'verification':str(batch/'verification.json')}
    from duplex_runtime.revalidate_runtime import adopt_completed_rollout,upgrade_alignment_recovery
    from duplex_runtime.reviewed_branch import create_reviewed_branch
    from repair_runtime_environment import repair_environment,RuntimeRepairExhausted,repair_output_for
    for job in manifest['jobs']:
        folder=batch/'jobs'/job['episode_id']; state=read(folder/'state.json')
        require(state['status']!='running','a worker is already running; do not start a competing recovery')
        if adopt_completed_rollout(batch,job['episode_id']):state=read(folder/'state.json')
        while state['status']!='completed':
            implementation_replay=False
            if state['status']=='failed':
                prior=folder/state['latest_rollout'];saved=read(prior/'result.json')if(prior/'result.json').exists()else{}
                failure=saved.get('failure')or{}
                missing=(failure.get('type')=='RolloutBlocked'and ' interrupt anchor not reached before audible playback ended; original script retained'in failure.get('reason',''))
                silent=(not manifest.get('autonomous_user_policy',{}).get('enabled')and failure=={
                    'type':'RolloutBlocked','reason':'assistant listener still waiting after 3s synthetic silence; no forced response'})
                if missing or silent:
                    if missing:
                        client,_=configured_clients(credentials,profile,manifest['model'],manifest['model'])
                        client.max_retries=5;client.fallback_enabled=False;client.judge_model=manifest['model']
                        repair_output=repair_output_for(batch)
                        try:repaired=repair_environment(batch,job['episode_id'],repair_output,client)
                        except RuntimeRepairExhausted as exc:raise PipelineSampleFailed(str(exc))from exc
                        except Exception as exc:
                            if getattr(exc,'request_retries_exhausted',False)or request_budget_exhausted(repair_output):
                                raise PipelineSampleFailed('owner model request exhausted five retries; original calls retained')from exc
                            raise
                    else:
                        repaired={'prepared':str(folder/'environment'),'repair_counts':manifest.get('runtime_owner_repair_counts',{}),
                            'repair_rounds':manifest.get('runtime_owner_repair_rounds',0)}
                    child=ROOT/'runtime_runs'/(batch.name+('-owner-repaired'if missing else'-autonomous-user'))
                    proof=child/'jobs/E001/owner-branch-reuse.json'
                    if child.exists()and not proof.exists():
                        states=[read(p)for p in(child/'jobs').glob('*/state.json')]
                        require(not states or all(s['status']=='pending'and not s['attempts']for s in states),'partial reviewed branch has actual attempts; inspect before recovery')
                        child.rename(child.with_name(child.name+'-incomplete-'+str(len(list(child.parent.glob(child.name+'-incomplete-*')))+1)))
                    if not child.exists():
                        create_reviewed_branch(batch,job['episode_id'],Path(repaired['prepared']),child,
                            repair_counts=repaired['repair_counts'],repair_rounds=repaired['repair_rounds'])
                    result=complete_batch(child,credentials,profile,max_attempts=max_attempts)
                    return {**result,'stage4':result.get('stage4',repaired.get('stage4')),
                        'stage5':result.get('stage5',repaired.get('stage5')),'prepared':result.get('prepared',repaired['prepared']),
                        'owner_repair_runs':([str(repair_output)]if missing else[])+result.get('owner_repair_runs',[]),
                        'parent_batches':[str(batch),*result.get('parent_batches',[])]}
                if failure.get('type')=='SpeechError'and failure.get('reason')in{
                    'word offsets match neither exact provider nor original input coordinates',
                    'provider rewrote non-whitespace input; no fuzzy alignment',
                    'edge trim destroyed a timestamp boundary; inspect this sample'}:
                    records=[read(batch/p)for p in manifest.get('recovery_implementation_migrations',[])]
                    if not any(r['source_rollout']==state['latest_rollout']for r in records):
                        upgrade_alignment_recovery(batch,job['episode_id']);manifest=read(batch/'manifest.json')
                    implementation_replay=True
                else:
                    compatibility=None
                    for path in manifest.get('implementation_migrations',[]):
                        candidate=read(batch/path)['jobs'].get(job['episode_id'])
                        if candidate and candidate['source_rollout']==state['latest_rollout']:compatibility=candidate
                    legacy_transport=transient_transport_failure(failure)and not manifest.get('request_retry_policy')
                    if expected_runtime_failure(folder,failure):raise PipelineSampleFailed('sample failed after its bounded recovery: '+str(failure))
                    require(legacy_transport or compatibility,'unclassified runtime failure; inspect the retained original: '+str(failure))
                    implementation_replay=bool(compatibility)
            require(len(state['attempts'])<max_attempts or implementation_replay,'runtime transport recovery limit reached')
            run_batch(batch,credentials,profile,[job['episode_id']]); state=read(folder/'state.json')
            if adopt_completed_rollout(batch,job['episode_id']):state=read(folder/'state.json')
    from duplex_runtime.migrate_processing import upgrade_processing
    upgrade_processing(batch)
    processed=finish(batch,credentials,profile,workers=1)
    if processed['status']!='completed':
        for item in processed['episodes']:
            folder=batch/'jobs'/item['episode_id']
            if request_budget_exhausted(folder/'journal'):raise PipelineSampleFailed('post-hoc request exhausted five retries')
            state=read(folder/'processing-state.json')
            latest=folder/state['latest_directory']
            if any(len(list(p.glob('*/validation.json')))>=3 and all(read(v)['status']=='failed'for v in p.glob('*/validation.json'))
                for p in(latest/'scoring/calls').glob('*/attempts')):
                raise PipelineSampleFailed('post-hoc model corrections exhausted two repairs under the original Rubric')
        require(False,'scoring/export did not complete; inspect the original processing failure')
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
    p.add_argument('--user-voices',help='explicit override; default maps the frozen Stage-1 voice_profile');p.add_argument('--assistant-voices',default='female-chengshu')
    p.add_argument('--max-runtime-attempts',type=int,default=3)
    p.add_argument('--audit-repair-resume',type=Path,action='append',default=[],help='revalidate saved model candidates in a new repair version')
    p.add_argument('--prepare-only',action='store_true',help='stop at admitted frozen environments; no audio or dataset is claimed')
    args=p.parse_args()
    require(1<=args.workers<=4 and 1<=args.max_runtime_attempts<=3,'invalid worker or bounded recovery count')
    os.environ.update(profile_environment(args.credentials_file,args.profile))
    if args.resume_batch:
        result=complete_batch(args.resume_batch,args.credentials_file,args.profile,max_attempts=args.max_runtime_attempts)
        if args.run_id:
            work=ROOT/'work'/args.run_id;work.mkdir(parents=True,exist_ok=True)
            write_json(work/'runtime-complete.json',result)
        print(json.dumps(result,ensure_ascii=False),flush=True);return
    require(args.run_id and (args.scenario_ids or args.stage35_run),'supply a run ID and fresh scenarios or an admitted Stage-3.5 source')
    require(not(args.scenario_ids and args.stage35_run),'choose either fresh generation or a Stage-3.5 continuation')
    work=ROOT/'work'/args.run_id;work.mkdir(parents=True,exist_ok=True)
    def command(stage,*options):
        target=ROOT/'runs'/(args.run_id+'-stage'+stage)
        if target.exists():stage_gate(target);return target
        cmd=[sys.executable,'-B',str(ROOT/'run_with_api_profile.py'),'--credentials-file',str(args.credentials_file),'--profile',args.profile,
             '--',*options,'--run-id',target.name,'--model',args.model,'--judge-model',args.judge_model,'--workers',str(args.workers)]
        write_json(work/('stage'+stage+'-command.json'),{'command':cmd})
        with (work/('stage'+stage+'.log')).open('w')as log: result=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        stage_gate(target)
        require(result.returncode==0,'Stage '+stage+' encountered an unclassified code/command error; inspect its log')
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
        if value is None:return [None]*len(case_ids)
        result=value.split(',')
        require(len(result)in(1,len(case_ids)),'provide one voice or one per case')
        return result*len(case_ids) if len(result)==1 else result
    users,assistants=voices(args.user_voices),voices(args.assistant_voices)
    catalog=None
    if args.user_voices is None and not args.prepare_only:
        from duplex_runtime.voice_profile import fetch_catalog
        catalog=fetch_catalog(os.environ.get('DASHSCOPE_API_KEY',''),work/'voice-catalog')
    def episode(item):
        index,case=item;prefix=args.run_id+'-'+case
        client,_=configured_clients(args.credentials_file,args.profile,args.model,args.judge_model);client.judge_model=args.judge_model
        client=RecordedCalls(client,work/'offline-model-calls'/case)
        offline=finalize_case(stages['35'],case,prefix,client,repair_resumes=args.audit_repair_resume)
        write_json(work/(case+'-offline.json'),offline)
        if args.prepare_only: return {**offline,'status':'prepared','pipeline_stages':{k:str(v)for k,v in stages.items()}}
        batch=ROOT/'runtime_runs'/(prefix+'-batch')
        if not batch.exists():
            create_batch(batch,[Path(offline['prepared'])],1,1,args.seed+index,args.runtime_model,
                         user_voice_id=users[index],assistant_voice_id=assistants[index],voice_profile_client=client,voice_catalog=catalog)
        completed=complete_batch(batch,args.credentials_file,args.profile,max_attempts=args.max_runtime_attempts)
        entry={**offline,**completed,'pipeline_stages':{k:str(v)for k,v in stages.items()}}
        write_json(work/(case+'-complete.json'),entry);return entry
    with ThreadPoolExecutor(max_workers=args.workers)as pool: results=list(pool.map(episode,enumerate(case_ids)))
    status='prepared' if args.prepare_only else 'completed'
    write_json(work/'result.json',{'status':status,'cases':results,'manual_artifact_repairs':False})
    print(json.dumps({'status':status,'episodes':len(results),'result':str(work/'result.json')},ensure_ascii=False),flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        known=isinstance(exc,(PipelineSampleFailed,OfflineBudgetExhausted,JudgeFormatExhausted,RubricRefreshExhausted))or bool(getattr(exc,'request_retries_exhausted',False))
        known|=isinstance(exc,SpeechError)and str(exc)=='voice profile mapping failed after two model corrections; all attempts retained'
        if not known:raise
        # A durable typed outcome allows the sampling controller to replace
        # this sample. Unexpected exceptions never produce this marker.
        parser=argparse.ArgumentParser(add_help=False);parser.add_argument('--run-id');args,_=parser.parse_known_args()
        if args.run_id:
            folder=ROOT/'work'/args.run_id;folder.mkdir(parents=True,exist_ok=True)
            write_json(folder/'sample-failure.json',{'status':'failed_preserved','kind':type(exc).__name__,'reason':str(exc),'original_records_retained':True})
        raise SystemExit(str(exc))
