"""Bounded runtime findings → responsible model → original Judges and full Audit."""
from __future__ import annotations

import copy
from pathlib import Path
import re
import shutil

from duplex_runtime.backchannel_policy import planning_policy
from duplex_runtime.prepare import prepare_session,verify_prepared_session
from duplex_runtime.user_simulator import json_hash,write_json
from duplex_runtime.verify_synthetic import read,require
from judge_output_repair import complete_checked_judge,JudgeFormatExhausted
from minimax_client import parse_json_text
from duplex_runtime.speech import digest
from record_model_call import RecordedCalls
from repair_runtime_anchor import stage3_mainline
from run_pipeline import stage_context
from stage_validation import environment_audit_release_blockers
from step_03_generate_episode_script import _base_context,_judge_input,_judge_errors
from step_03_5_generate_duplex_enrichment import _context_for_enrichment,_judge_input as enrichment_input,_judge_errors as enrichment_errors
from step_05_repair_environment_audit import ARTIFACTS,audit_case,audit_input,json_pointer_exists,repair_one_stage,refresh_changed_turn_rubrics,RubricRefreshExhausted

ROOT=Path(__file__).resolve().parent


class RuntimeRepairExhausted(ValueError):
    pass


def canonical_path(path):
    """Translate exact dotted/bracket paths; never guess a field or meaning."""
    if not isinstance(path,str):return path
    wrappers=('episode_script','enriched_episode_script','original_episode_script','actor_rubric')
    for name in wrappers:
        if path.startswith(name+'.'):path=path[len(name)+1:];break
        if path.startswith('/'+name+'/'):path=path[len(name)+1:];break
    if path.startswith('/'):return path
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*',path):return path
    return '/'+re.sub(r'\[(\d+)\]',r'/\1',path).replace('.','/')


def pointer(value,path):
    for key in path.strip('/').split('/')if path else []:
        key=key.replace('~1','/').replace('~0','~')
        value=value[int(key)]if isinstance(value,list)else value[key]
    return value


def changed_paths(before,after,path=''):
    if isinstance(before,dict)and isinstance(after,dict):
        result=[]
        for key in sorted(set(before)|set(after)):
            item=path+'/'+key.replace('~','~0').replace('/','~1')
            result.extend([item]if key not in before or key not in after else changed_paths(before[key],after[key],item))
        return result
    if isinstance(before,list)and isinstance(after,list)and len(before)==len(after):
        return [p for i,(a,b)in enumerate(zip(before,after))for p in changed_paths(a,b,path+'/'+str(i))]
    return []if before==after else[path]


def owner(stage,path):
    if stage=='actor_rubric':return 'stage4'
    if stage!='episode_script':raise RuntimeRepairExhausted('runtime repair cannot alter frozen '+stage)
    return 'stage35'if re.search(r'/episode_turns/\d+/(side_speech|user_backchannels)(/|$)',path)else'stage3'


def budget_key(stage,path,artifacts):
    parts=path.strip('/').split('/')
    if stage=='actor_rubric'and len(parts)>=2 and parts[0]=='turn_rubrics':
        row=artifacts[stage]['turn_rubrics'][int(parts[1])]
        return 'actor_rubric/'+row['turn_id']
    if stage=='episode_script'and len(parts)>=3 and parts[0]=='episode_turns':
        turn=artifacts[stage]['episode_turns'][int(parts[1])]
        return 'episode_script/'+turn['turn_id']+'/'+parts[2]
    raise RuntimeRepairExhausted('review did not locate a specific repairable field: '+stage+path)


def targeted_errors(before,after,findings,owner_name):
    paths=[f['path']for f in findings];allowed=list(paths)
    if 'episode_turns'in before:
        if [t['turn_id']for t in before['episode_turns']]!=[t.get('turn_id')for t in after.get('episode_turns',[])]:
            return ['runtime repair must preserve main turn identities and order']
        for path in paths:
            parts=path.strip('/').split('/')
            if len(parts)<3:continue
            root='/episode_turns/'+parts[1]+'/'
            if owner_name=='stage3':
                allowed.extend(root+field for field in('semantic_goal','user_utterance','assistant_semantic_goal','pause_cue','timing_refs','act_ref','interrupt_anchor'))
            else:allowed.append(root+parts[2])
    else:
        if [r['turn_id']for r in before['turn_rubrics']]!=[r.get('turn_id')for r in after.get('turn_rubrics',[])]:
            return ['runtime repair must preserve rubric turn identities and order']
        allowed.extend('/turn_rubrics/'+p.strip('/').split('/')[1] for p in paths)
    errors=[]
    for path in changed_paths(before,after):
        if not any(path==p or path.startswith(p+'/')for p in allowed):errors.append('unrelated field changed: '+path)
        if 'episode_turns'in before and owner('episode_script',path)!=owner_name:
            errors.append('field belongs to another stage: '+path)
    return errors


def normalize(items,origin,stage='episode_script'):
    values=[]
    for i,item in enumerate(items):
        if not isinstance(item,dict):continue
        value={'finding_id':item.get('finding_id',origin+'-'+str(i+1)),
            'category':item.get('category','act_timing'),'severity':item.get('severity','must_fix'),
            'stage':item.get('stage',stage),'path':canonical_path(item['path']),'review_path':item['path'],'evidence':item.get('evidence',''),
            'reason':item.get('reason',item.get('problem','')),
            'suggested_fix':item.get('suggested_fix',item.get('repair','')),'origin':origin}
        values.append(value)
    return values


def review_errors(value,script,*,enrichment=False):
    errors=(enrichment_errors if enrichment else _judge_errors)(value)
    if errors:return errors
    for finding in value['must_fix'if enrichment else'findings']:
        path=canonical_path(finding.get('path'))
        if not json_pointer_exists(script,path):errors.append('finding path outside the reviewed script: '+str(path))
    return errors


def repair_output_for(batch):
    """Resume a mechanically misread original verdict without redrawing it."""
    base=ROOT/'runs'/(Path(batch).name+'-owner-recovery')
    recovered=base.with_name(base.name+'-path-format-recovered')
    if recovered.exists():return recovered
    result=base/'result.json'
    if not result.exists()or read(result).get('reason')!='evaluator output remained malformed after two model corrections; original candidate and all reviews retained':return base
    for folder in sorted(base.glob('round-*/stage*-judge/format-attempt-01')):
        errors=read(folder/'validation.json').get('errors',[])
        if not errors or not all(e.startswith(('finding path outside','Stage-3.5 finding must locate'))for e in errors):continue
        context=read(folder/'identity.json')['context'];enrichment='enriched_episode_script'in context
        script=context['enriched_episode_script'if enrichment else'episode_script']
        review=parse_json_text((folder/'raw_response.txt').read_text(),{'decision','must_fix','warnings','duplex_summary'}if enrichment else{'decision','findings'})
        if review_errors(review,script,enrichment=enrichment):continue
        shutil.copytree(base,recovered)
        (recovered/'result.json').rename(recovered/'retained-prior-format-failure.json')
        write_json(recovered/'path-format-recovery.json',{'source':str(base),'original_result_sha256':digest(result),
            'saved_verdict':str(folder/'raw_response.txt'),'saved_verdict_sha256':digest(folder/'raw_response.txt'),
            'method':'exact_dotted_bracket_to_json_pointer','semantic_decision_unchanged':True,
            'new_model_requests_for_recovery':0,'repair_counts_reconstructed_from_same_cached_calls':True})
        return recovered
    return base


def audit_paths(value,artifacts):
    return ['finding must locate an existing upstream field: '+str(f.get('stage'))+str(f.get('path'))
            for f in value.get('findings',[])if f.get('stage')not in artifacts or
            not json_pointer_exists(artifacts[f['stage']],f.get('path'))]


def _legacy(batch,turn,case_id,artifacts,counts):
    """Reuse generated candidates; a corrected owner never resets their budget."""
    base=ROOT/'runs'/(batch.name+'-'+turn+'-anchor-repair-owner-v2')
    corrected=base.with_name(base.name+'-projection-v3')
    source=corrected if(corrected/'result.json').exists()and read(corrected/'result.json')['status']=='passed'else base
    if not(source/'result.json').exists():return None,[]
    result=read(source/'result.json');number=next(i for i,t in enumerate(artifacts['episode_script']['episode_turns'])if t['turn_id']==turn)
    key=budget_key('episode_script',f'/episode_turns/{number}/interrupt_anchor',artifacts)
    used=result.get('model_corrections_used',len(result.get('attempts',[])))
    counts[key]=max(counts.get(key,0),read(batch/'manifest.json').get('runtime_anchor_repair_counts',{}).get(turn,0)+used)
    if result['status']=='passed':return result,[]
    folder=source/case_id/'stage_cache/runtime_anchor_repair';candidates=sorted(folder.glob('attempt-*/episode_script.json'))
    if candidates:artifacts['episode_script']=read(candidates[-1])
    findings=[]
    for attempt in sorted(folder.glob('attempt-*')):
        if not(attempt/'episode_script.json').exists():continue
        previous=read(attempt/'episode_script.json')
        reviews=[]
        if(attempt/'judge-review.json').exists():
            reviews.extend(normalize([f for f in read(attempt/'judge-review.json').get('findings',[])if
                '/side_speech'not in f['path']and'/user_backchannels'not in f['path']],'stage3_judge'))
        if(attempt/'environment_audit.json').exists():reviews.extend(normalize(read(attempt/'environment_audit.json').get('findings',[]),'audit'))
        for finding in reviews:
            current=artifacts[finding['stage']]
            old=previous if finding['stage']=='episode_script'else current
            if json_pointer_exists(current,finding['path'])and json_pointer_exists(old,finding['path'])and pointer(current,finding['path'])==pointer(old,finding['path']):
                if not any(f['stage']==finding['stage']and f['path']==finding['path']for f in findings):findings.append(finding)
    return None,findings


def repair_environment(batch,episode_id,output,client):
    batch,output=Path(batch).resolve(),Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    if(output/'result.json').exists():
        saved=read(output/'result.json')
        if saved['status']!='passed':raise RuntimeRepairExhausted(saved['reason'])
        return saved
    manifest=read(batch/'manifest.json');job=batch/'jobs'/episode_id;state=read(job/'state.json');environment=job/'environment'
    latest=job/state['latest_rollout'];failure=read(latest/'result.json')['failure'];case_id=read(environment/'session.json')['case_id']
    match=re.fullmatch(r'(U\d+) interrupt anchor not reached before audible playback ended; original script retained',failure.get('reason',''))
    if failure.get('type')!='RolloutBlocked'or not match:raise RuntimeRepairExhausted('runtime failure has no eligible environment-owned finding')
    turn=match.group(1);original={name:read(environment/'frozen'/f'{name}.json')for name in ARTIFACTS};artifacts=copy.deepcopy(original)
    counts=dict(manifest.get('runtime_owner_repair_counts',{}))
    for identifier,used in manifest.get('runtime_anchor_repair_counts',{}).items():
        counts['episode_script/'+identifier+'/interrupt_anchor']=max(counts.get('episode_script/'+identifier+'/interrupt_anchor',0),used)
    cached=RecordedCalls(client,output/'calls');judge=cached.for_model(getattr(client,'judge_model',client.model),temperature=0.0)
    history=[];context=stage_context(case_id,'environment_audit',artifacts['scene_task']['scene'],artifacts)
    legacy,pending=_legacy(batch,turn,case_id,artifacts,counts)
    if legacy:
        result={**legacy,'repair_counts':counts,'repair_rounds':manifest.get('runtime_owner_repair_rounds',0),
                'reused_reviewed_environment':True,'owner_repair_run':str(output)}
        write_json(output/'result.json',result);return result
    if not pending and artifacts==original:
        i=next(i for i,t in enumerate(artifacts['episode_script']['episode_turns'])if t['turn_id']==turn)
        records=[r for r in read(latest/'trigger-results.json')if r['turn_id']==turn]
        pending=normalize([{'path':f'/episode_turns/{i}/interrupt_anchor','evidence':artifacts['episode_script']['episode_turns'][i]['interrupt_anchor'],
            'problem':'实际助手播放已结束，该语义锚点始终未达到。实际已听记录：'+str(records[-2:]),
            'repair':'根据已听到的单一实质语义阶段最小修复插话锚点，保持本轮任务请求、事实和原判断规则。'}],'runtime_observation')
    last3=json_hash(stage3_mainline(original['episode_script']));last35=json_hash(original['episode_script'])
    refreshed=copy.deepcopy(original['episode_script']);base_rounds=manifest.get('runtime_owner_repair_rounds',0)
    audit=None;raw=None;meta=None
    try:
        # At most two corrections for each turn-owned field. The finite round
        # bound derives from those fields instead of imposing a new case cap.
        for number in range(1,20*len(artifacts['episode_script']['episode_turns'])-base_rounds+1):
            folder=output/f'round-{number:02d}';folder.mkdir(exist_ok=True)
            done=folder/'complete.json'
            if done.exists():
                saved=read(done);artifacts=saved['artifacts'];counts=saved['counts'];pending=saved['pending'];last3=saved['last3'];last35=saved['last35'];refreshed=saved['refreshed'];history=saved['history']
                if saved.get('passed'):break
                continue
            write_json(folder/'input.json',{'artifacts':artifacts,'findings':pending,'counts':counts,'runtime_failure':failure})
            for responsible in('stage3','stage35','stage4'):
                findings=[f for f in pending if owner(f['stage'],f['path'])==responsible]
                if not findings:continue
                stage='actor_rubric'if responsible=='stage4'else'episode_script'
                if any(not json_pointer_exists(artifacts[stage],f['path'])for f in findings):raise RuntimeRepairExhausted('owner finding path does not exist')
                keys={budget_key(stage,f['path'],artifacts)for f in findings};remaining=min(2-counts.get(k,0)for k in keys)
                if remaining<=0:raise RuntimeRepairExhausted('two model corrections exhausted for '+','.join(sorted(keys)))
                before=copy.deepcopy(artifacts[stage])
                def scope(value):
                    errors=targeted_errors(before,value,findings,responsible)
                    for path in changed_paths(before,value):
                        if counts.get(budget_key(stage,path,artifacts),0)>=2:
                            errors.append('coupled field already exhausted two model corrections: '+path)
                    return errors
                value,record=repair_one_stage(case_id,artifacts['scene_task']['scene'],{'findings':findings},context,artifacts,stage,cached,
                    record_dir=folder/responsible,max_attempts=remaining,strict_runtime=True,extra_validator=scope)
                used=record.get('attempt',0)
                if record['status']=='completed':
                    keys.update(budget_key(stage,p,artifacts)for p in changed_paths(before,value))
                    if any(counts.get(key,0)+used>2 for key in keys):
                        raise RuntimeRepairExhausted('a coupled field would exceed its two-correction budget')
                for key in keys:counts[key]=counts.get(key,0)+used
                history.append({'round':number,'owner':responsible,**{k:v for k,v in record.items()if k!='raw_response'}})
                if record['status']!='completed':raise RuntimeRepairExhausted('responsible model did not repair '+responsible+': '+str(record.get('errors',record['status'])))
                artifacts[stage]=value
            pending=[]
            # Original Stage-3 and Stage-3.5 Judges retain their own input scopes.
            mainline=stage3_mainline(artifacts['episode_script']);hash3=json_hash(mainline)
            if hash3!=last3:
                prompt=(ROOT/'prompts/03_episode_script_judge.md').read_text();request=_judge_input(_base_context(artifacts,case_id),mainline)
                validator=lambda review:review_errors(review,mainline)
                raw,meta=complete_checked_judge(judge,prompt,request,keys={'decision','findings'},validator=validator,directory=folder/'stage3-judge',max_tokens=3600)
                review=parse_json_text(raw,{'decision','findings'});write_json(folder/'stage3-review.json',review)
                if review['decision']!='pass':pending=normalize(review['findings'],'stage3_judge')
                else:last3=hash3
            hash35=json_hash(artifacts['episode_script'])
            if not pending and hash35!=last35:
                source={**artifacts,'backchannel_policy':planning_policy(len(mainline['episode_turns']),enabled=any(t.get('user_backchannels')for t in artifacts['episode_script']['episode_turns'])),
                    'side_speech_policy':{'mode':'all','enabled':any(t.get('side_speech')for t in artifacts['episode_script']['episode_turns']),'case_probability':1,'min_events':1,'max_events':2}}
                enrichment=_context_for_enrichment(source,case_id,mainline,{'status':'passed'})
                request=enrichment_input(enrichment,mainline,artifacts['episode_script']);prompt=(ROOT/'prompts/03_5_duplex_judge.md').read_text()
                raw,meta=complete_checked_judge(judge,prompt,request,keys={'decision','must_fix','warnings','duplex_summary'},validator=lambda review:review_errors(review,artifacts['episode_script'],enrichment=True),
                    directory=folder/'stage35-judge',max_tokens=2400)
                review=parse_json_text(raw,{'decision','must_fix','warnings','duplex_summary'});write_json(folder/'stage35-review.json',review)
                if review['decision']!='pass':pending=normalize(review['must_fix'],'stage35_judge')
                else:last35=hash35
            passed=False;audit=None
            if not pending:
                affected=[i for i,(a,b)in enumerate(zip(refreshed['episode_turns'],artifacts['episode_script']['episode_turns']))if a!=b]
                if affected:
                    keys={budget_key('actor_rubric',f'/turn_rubrics/{i}',artifacts)for i in affected};remaining=min(2-counts.get(k,0)for k in keys)
                    if remaining<=0:raise RuntimeRepairExhausted('affected Rubric exhausted two owner corrections')
                    record=refresh_changed_turn_rubrics(context,refreshed,artifacts,{'findings':read(folder/'input.json')['findings']},cached,
                        folder/'rubric-refresh',max_attempts=remaining,allow_unchanged=True)
                    for key in keys:counts[key]=counts.get(key,0)+record['attempt']
                    history.append({'round':number,'owner':'stage4_dependency',**record})
                    refreshed=copy.deepcopy(artifacts['episode_script'])
                if artifacts==original:raise RuntimeRepairExhausted('runtime repair did not change the failing environment')
                audit,raw,meta=audit_case(context,artifacts,judge,record_dir=folder/'audit-format',extra_validator=lambda value:audit_paths(value,artifacts))
                write_json(folder/'environment_audit.json',audit);(folder/'audit-raw_response.txt').write_text(raw);write_json(folder/'audit-metadata.json',meta)
                passed=not environment_audit_release_blockers(audit)
                if not passed:pending=normalize(audit.get('findings',[]),'audit')
                if not passed and not pending:raise RuntimeRepairExhausted('Audit blocked without a repairable finding')
            write_json(done,{'artifacts':artifacts,'counts':counts,'pending':pending,'last3':last3,'last35':last35,
                'refreshed':refreshed,'history':history,'passed':passed})
            if passed:break
        else:raise RuntimeRepairExhausted('runtime owner repair round limit reached')
        # The final audit is always an actual saved evaluator response.
        if audit is None:
            folder=output/f'round-{number:02d}';audit=read(folder/'environment_audit.json');raw=(folder/'audit-raw_response.txt').read_text();meta=read(folder/'audit-metadata.json')
        case=output/case_id;case.mkdir(exist_ok=True)
        for name,value in artifacts.items():write_json(case/(name+'.json'),value)
        write_json(case/'environment_audit.json',audit);write_json(case/'input.json',audit_input(context,artifacts))
        (case/'prompt.md').write_text((ROOT/'prompts/05_environment_audit.md').read_text());(case/'environment_audit.raw_response.txt').write_text(raw)
        write_json(case/'metadata.json',meta);write_json(case/'validation.json',{'status':'passed','errors':[]});write_json(case/'repair_history.json',history)
        write_json(output/'manifest.json',{'run_id':output.name,'stage':'environment_audit_review','case_ids':[case_id],'model':client.model,
            'source_run':read(environment/'session.json')['source_run_path'],'runtime_environment_repair':True,
            'runtime_parent_batch':str(batch),'manual_artifact_edits':False})
        prepared=ROOT/'runtime_runs'/(output.name+'-prepared')
        if not prepared.exists():prepare_session(output,case_id,prepared)
        verify_prepared_session(prepared)
        require(all(read(prepared/'frozen'/f'{name}.json')==value for name,value in artifacts.items()),'saved prepared environment differs from the reviewed repair')
        result={'status':'passed','stage4':str(output),'stage5':str(output),'prepared':str(prepared),
            'repair_counts':counts,'repair_rounds':base_rounds+number,'owner_repair_run':str(output),'manual_artifact_edits':False}
        write_json(output/'result.json',result);return result
    except (RuntimeRepairExhausted,JudgeFormatExhausted,RubricRefreshExhausted)as error:
        write_json(output/'result.json',{'status':'failed','reason':str(error),'repair_counts':counts,'history':history,
            'parent_batch':str(batch),'case_id':case_id,'original_records_retained':True})
        raise RuntimeRepairExhausted(str(error))from error
