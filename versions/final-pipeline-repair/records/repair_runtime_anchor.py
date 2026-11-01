"""Model-only missing-window repair, original Stage-3 Judge, then full Audit."""
from __future__ import annotations

import copy
from pathlib import Path
import re
import shutil

from minimax_client import parse_json_text
from record_model_call import RecordedCompletion
from duplex_runtime.prepare import prepare_session
from duplex_runtime.speech import digest
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_synthetic import read, require
from run_pipeline import JSON_RESPONSE_FORMAT, _redact_secret, stage_context
from stage_validation import environment_audit_release_blockers, frozen_script_validation_stage, validate_stage
from step_03_generate_episode_script import _base_context, _judge_input, _judge_errors
from step_05_repair_environment_audit import ARTIFACTS, AUDIT_PROMPT, audit_case, audit_input, process_case
from revise_episode_anchor import revise_anchor

ROOT = Path(__file__).resolve().parent
MAX_REPAIRS = 2


def missing_anchor_failure(job):
    state = read(job/"state.json")
    if state["status"] != "failed":return None
    result_path = job/state["latest_rollout"]/"result.json"
    if not result_path.exists():return None
    failure = read(result_path).get("failure")or{}
    if failure.get("type") != "RolloutBlocked":return None
    match = re.fullmatch(r"(U\d+) interrupt anchor not reached before audible playback ended; original script retained",failure.get("reason",""))
    return match.group(1)if match else None


def repair_missing_anchor(batch, episode_id, output, client, *, max_repairs=MAX_REPAIRS, reuse_from=None, prepared_dir=None):
    batch,output = Path(batch).resolve(),Path(output).resolve()
    job = batch/"jobs"/episode_id
    turn_id = missing_anchor_failure(job)
    require(turn_id,"runtime failure is not an eligible missing interrupt anchor")
    environment = job/"environment"
    case_id = read(environment/"session.json")["case_id"]
    case = output/case_id
    if (output/"result.json").exists():
        saved=read(output/"result.json")
        require(saved["parent_batch"]==str(batch)and saved["turn_id"]==turn_id,"anchor repair resume identity changed")
        require(saved["status"]=="passed","anchor repair budget already exhausted")
        return saved
    require(type(max_repairs)is int and 1<=max_repairs<=MAX_REPAIRS,"invalid remaining anchor-repair budget")
    case.mkdir(parents=True,exist_ok=True)
    if reuse_from:
        saved=Path(reuse_from)/case_id/"stage_cache/runtime_anchor_repair/attempt-01"
        target=case/"stage_cache/runtime_anchor_repair/attempt-01"
        if not target.exists() and saved.exists():
            shutil.copytree(saved,target)
            write_json(case/"saved-call-reuse.json",{"source":str(saved),"request_identities_must_match_exactly":True})
    artifacts = {name:read(environment/"frozen"/f"{name}.json")for name in ARTIFACTS}
    original = copy.deepcopy(artifacts["episode_script"])
    context = _base_context(artifacts,case_id)
    latest = job/read(job/"state.json")["latest_rollout"]
    failure = read(latest/"result.json")["failure"]
    triggers = [r for r in read(latest/"trigger-results.json")if r["turn_id"]==turn_id]
    prompt = (ROOT/"prompts/03_runtime_anchor_repair.md").read_text()
    judge_prompt = (ROOT/"prompts/03_episode_script_judge.md").read_text()
    judge = client.for_model(getattr(client,"judge_model",client.model),temperature=0.0)
    feedback = None
    history = []
    for number in range(1,max_repairs+1):
        attempt = case/"stage_cache/runtime_anchor_repair"/f"attempt-{number:02d}"
        attempt.mkdir(parents=True,exist_ok=True)
        request={**copy.deepcopy(context),"frozen_episode_script":original,"turn_id":turn_id,"runtime_failure":failure,
                 "original_observer_records":triggers,"review_feedback":feedback}
        write_json(attempt/"input.json",request);(attempt/"prompt.md").write_text(prompt)
        raw,meta=RecordedCompletion(client,attempt/"model-call").complete(prompt,request,max_tokens=1800,response_format=JSON_RESPONSE_FORMAT)
        (attempt/"raw_response.txt").write_text(_redact_secret(raw,client));write_json(attempt/"metadata.json",meta)
        try:
            value=parse_json_text(raw,{"interrupt_anchor","reason"})
            require(set(value)=={"interrupt_anchor","reason"}and all(isinstance(v,str)and v.strip()for v in value.values()),"anchor repair must return exactly two nonempty strings")
            revised,revision=revise_anchor(original,turn_id,value["interrupt_anchor"])
            write_json(attempt/"episode_script.json",revised);write_json(attempt/"revision.json",revision)
            errors=validate_stage(frozen_script_validation_stage(revised),revised,context)
            require(not errors,"revised script schema: "+str(errors))
        except ValueError as exc:
            feedback={"structural_errors":[str(exc)]}
            write_json(attempt/"validation.json",feedback);history.append({"attempt":number,"status":"invalid"});continue
        # Stage-3 Judge reviews its own mainline, not Stage-3.5 additions.
        mainline=copy.deepcopy(revised)
        for turn in mainline["episode_turns"]:turn.pop("user_backchannels",None)
        judge_input=_judge_input(context,mainline)
        write_json(attempt/"judge-input.json",judge_input);(attempt/"judge-prompt.md").write_text(judge_prompt)
        judge_raw,judge_meta=RecordedCompletion(judge,attempt/"judge-call").complete(judge_prompt,judge_input,max_tokens=3600,response_format=JSON_RESPONSE_FORMAT)
        (attempt/"judge-raw_response.txt").write_text(_redact_secret(judge_raw,judge));write_json(attempt/"judge-metadata.json",judge_meta)
        try:
            review=parse_json_text(judge_raw,{"decision","findings"});errors=_judge_errors(review)
        except ValueError as exc:
            feedback={"stage3_judge_format_errors":[str(exc)]};history.append({"attempt":number,"status":"judge_invalid"});continue
        write_json(attempt/"judge-review.json",review);write_json(attempt/"judge-validation.json",{"errors":errors})
        if errors or review["decision"]!="pass":
            feedback={"original_stage3_judge":review,"errors":errors};history.append({"attempt":number,"status":"judge_rejected"});continue
        changed={**copy.deepcopy(artifacts),"episode_script":revised}
        audit_context=stage_context(case_id,"environment_audit",artifacts["scene_task"]["scene"],changed)
        write_json(attempt/"audit-input.json",audit_input(audit_context,changed));(attempt/"audit-prompt.md").write_text(AUDIT_PROMPT)
        try:
            audit,audit_raw,audit_meta=audit_case(audit_context,changed,RecordedCompletion(judge,attempt/"audit-call"),record_dir=attempt/"audit-judge-format")
        except ValueError as exc:
            feedback={"full_audit_format_errors":[str(exc)]};history.append({"attempt":number,"status":"audit_invalid"});continue
        write_json(attempt/"environment_audit.json",audit);(attempt/"audit-raw_response.txt").write_text(_redact_secret(audit_raw,judge));write_json(attempt/"audit-metadata.json",audit_meta)
        blockers=environment_audit_release_blockers(audit)
        final_audit_input=audit_input(audit_context,changed)
        owner_repairs=[]
        if blockers and any(f.get("stage")=="actor_rubric" for f in audit.get("findings",[])):
            # A new rubric finding belongs to Stage 4, not to the anchor model.
            bundle=attempt/"audit-bundle";snapshot=bundle/case_id;snapshot.mkdir(parents=True,exist_ok=True)
            for name,value in {**changed,"environment_audit":audit}.items():write_json(snapshot/f"{name}.json",value)
            write_json(snapshot/"input.json",audit_input(audit_context,changed));(snapshot/"prompt.md").write_text(AUDIT_PROMPT)
            (snapshot/"environment_audit.raw_response.txt").write_text(_redact_secret(audit_raw,judge))
            write_json(snapshot/"validation.json",{"errors":[]})
            write_json(bundle/"manifest.json",{"run_id":bundle.name,"stage":"environment_audit_review","case_ids":[case_id],
                       "source_run":read(environment/"session.json")["source_run_path"]})
            source=bundle
            for owner_attempt in range(1,3):
                findings=read(source/case_id/"environment_audit.json").get("findings",[])
                if not environment_audit_release_blockers(read(source/case_id/"environment_audit.json")):break
                # Do not resolve a rubric finding by changing an unrelated field.
                if any(f.get("stage")!="actor_rubric" for f in findings):break
                target=attempt/f"audit-owner-repair-{owner_attempt:02d}"
                if not (target/"manifest.json").exists():
                    result=process_case(source,source,source,target,case_id,client,frozen_source=bundle)
                    write_json(target/"manifest.json",{"run_id":target.name,"stage":"environment_audit_final_repair","case_ids":[case_id],
                        "source_run":str(source),"rubric_run":str(source),"audit_run":str(source),"results":[result]})
                final={name:read(target/case_id/f"{name}.json")for name in ARTIFACTS}
                require(all(final[name]==changed[name]for name in ARTIFACTS if name!="actor_rubric"),"rubric repair changed an unrelated frozen artifact")
                owner_repairs.append(str(target));source=target
            changed={name:read(source/case_id/f"{name}.json")for name in ARTIFACTS}
            audit=read(source/case_id/"environment_audit.json")
            audit_raw=(source/case_id/"environment_audit.raw_response.txt").read_text()
            audit_meta=read(source/case_id/"metadata.json")if (source/case_id/"metadata.json").exists() else audit_meta
            blockers=environment_audit_release_blockers(audit)
            final_audit_input=read(source/case_id/"input.json")
        if blockers:
            if owner_repairs:
                # An exhausted owner repair cannot be laundered by rejudging a new anchor.
                history.append({"attempt":number,"status":"audit_owner_repair_exhausted","owner_repairs":owner_repairs})
                break
            feedback={"full_environment_audit":audit,"release_blockers":blockers};history.append({"attempt":number,"status":"audit_rejected"});continue
        for name in ARTIFACTS:
            if name=="episode_script"or changed[name]!=artifacts[name]:write_json(case/f"{name}.json",changed[name])
            else:shutil.copy2(environment/"frozen"/f"{name}.json",case/f"{name}.json")
        write_json(case/"environment_audit.json",audit);write_json(case/"input.json",final_audit_input)
        (case/"prompt.md").write_text(AUDIT_PROMPT);(case/"environment_audit.raw_response.txt").write_text(_redact_secret(audit_raw,judge))
        write_json(case/"metadata.json",audit_meta);write_json(case/"validation.json",{"status":"passed","errors":[]})
        history.append({"attempt":number,"status":"passed","audit_owner_repairs":owner_repairs});write_json(case/"repair_history.json",history)
        write_json(output/"manifest.json",{"run_id":output.name,"stage":"environment_audit_review","case_ids":[case_id],
            "source_run":read(environment/"session.json")["source_run_path"],"runtime_parent_batch":str(batch),"model":client.model,
            "runtime_anchor_repair":True,"runtime_rubric_repairs":owner_repairs,"manual_artifact_edits":False})
        prepared=Path(prepared_dir)if prepared_dir else ROOT/"runtime_runs"/(output.name+"-prepared")
        prepare_session(output,case_id,prepared)
        result={"status":"passed","parent_batch":str(batch),"case_id":case_id,"turn_id":turn_id,"stage5":str(output),
                "prepared":str(prepared),"revision":revision,"attempts":history,"manual_artifact_edits":False}
        write_json(output/"result.json",result)
        return result
    result={"status":"failed","parent_batch":str(batch),"turn_id":turn_id,"attempts":history,"reason":"model repair/review budget exhausted"}
    write_json(case/"repair_history.json",history);write_json(output/"result.json",result)
    raise ValueError("runtime anchor model repair exhausted two corrections and re-reviews; original trajectory retained")
