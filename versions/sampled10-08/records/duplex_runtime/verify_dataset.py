"""Check actual source observations, output boundaries, PCM assets and reward credit owners."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import wave

from duplex_runtime.export_dataset import text_boundary
from duplex_runtime.scoring import file_hashes
from duplex_runtime.speech import digest
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_synthetic import read, require


def validate_training_region(sample, expected_text, aligned_frames=None):
    region=sample["training_region"]
    require(region["target_text"]==expected_text,"training text differs from actual consumed/source model output")
    require(region["character_ranges"]==([[0,len(expected_text)]] if expected_text else []),"training characters extend outside the actual target")
    if aligned_frames is not None:
        require(region["audio_frame_ranges"]==([[0,aligned_frames]] if aligned_frames else []),"unplayed or incomplete-word PCM entered the loss mask")


def verify_dataset(directory):
    directory=directory.resolve();manifest=read(directory/"manifest.json")
    artifacts=read(directory/"artifact-hashes.json")["files"]
    require(all(digest(directory/name)==value for name,value in artifacts.items()),"export artifact changed")
    run,score_dir=Path(manifest["source_run"]),Path(manifest["score_dir"])
    require(file_hashes(run)==manifest["source_files_sha256"],"source rollout changed after export")
    require(file_hashes(score_dir)==manifest["score_files_sha256"],"scoring evidence changed after export")
    samples=[json.loads(l)for l in (directory/"samples.jsonl").read_text().splitlines()]
    rewards=[json.loads(l)for l in (directory/"rewards.jsonl").read_text().splitlines()]
    require(len(samples)==manifest["samples"] and len({s["sample_id"]for s in samples})==len(samples),"sample count/identity differs")
    timeline=read(run/"timeline.json");responses={r["start_frame"]:r for r in timeline["responses"]}
    placements={p["path"]:p for p in timeline["placements"]}
    user_results={str(Path("user/calls")/f"{row['event']['generation_call']:04d}-generation"):row for row in read(run/"user-results.json")}
    roles={}
    for sample in samples:
        role=sample["policy_role"];roles[role]=roles.get(role,0)+1
        folder=run/sample["provenance"]["call_directory"]
        context=read(folder/"input.json")
        require(sample["observation"]["context"]==context and sample["observation"]["system_prompt"]==(folder/"prompt.md").read_text(),"policy observation differs from actual call")
        require(digest(folder/"input.json")==sample["provenance"]["input_sha256"],"source input hash differs")
        if role=="assistant":
            response=responses[context["current_frame"]]
            require(sample["action"]["text"]==response["heard_text"],"assistant action includes unplayed text")
            aligned=text_boundary(response,response["heard_text"])
            validate_training_region(sample,response["heard_text"],aligned)
            paired=sample["action"]["paired_consumed_audio"]
            require(paired is None or paired["frames"]==response["played_frames"],"assistant paired audio is not consumed PCM")
            require(sample["generated_boundary"]["consumed_frames"]==response["played_frames"],"consumption boundary differs")
        else:
            raw=(folder/"raw_response.txt").read_text()
            validate_training_region(sample,raw)
            if role=="user":
                require(sample["action"]["candidate"]==json.loads(raw) and not sample["reward_annotation_ids"],"user event was credited with assistant reward")
            else:
                require({k:v for k,v in sample["action"].items()if k!="kind"}==read(folder/"decision.json"),"listener target differs from actual decision")
        projections=sample["observation"]["aligned_pcm_prefixes"]
        if role in {"assistant","assistant_listener"}:
            expected={}
            by_id={p["audio_id"]:p for p in timeline["placements"]}
            for heard in context["observed_audio"]:
                require(heard["through_frame"]<=context["current_frame"],"future transcript timestamp")
                expected[heard["audio_id"]]=max(expected.get(heard["audio_id"],0),heard["through_frame"]-by_id[heard["audio_id"]]["start_frame"])
            require({p["audio_id"]:p["frames"]for p in projections}=={k:v for k,v in expected.items()if v},"observation PCM projection differs from heard word boundaries")
        elif role=="user":
            row=user_results[sample["provenance"]["call_directory"]]
            heard=context["observation"]["heard_text"]
            expected=[]
            if heard:
                response=max((r for r in timeline["responses"]if r["start_frame"]<=row["start_frame"]),key=lambda r:r["start_frame"])
                expected=[(response["response_id"],text_boundary(response,heard))]
            require([(p["audio_id"],p["frames"])for p in projections]==expected,"user observation audio differs from its actual heard assistant prefix")
            main=row["rendered"]["main"];paired=sample["action"]["paired_consumed_audio"]
            require((paired is None)==(main is None),"user action audio presence differs")
            if main:
                require(paired["source"]=="user-speech/"+main["path"] and paired["frames"]==main["frames"],"user action audio differs from actually rendered speech")
            require(sample["training_region"]["audio_frame_ranges"]==([[0,main["frames"]]]if main else []),"user audio loss mask differs from consumed utterance")
        all_audio=projections+[sample["action"]["paired_consumed_audio"]] if "paired_consumed_audio" in sample["action"] else projections
        for audio in all_audio:
            if audio is None:continue
            source=run/audio["source"]
            require(audio["source_start_frame"]==0 and audio["source_end_frame"]==audio["frames"],"projection source boundary differs")
            if audio["source"] in placements:require(audio["frames"]<=placements[audio["source"]]["frames"],"unplayed source PCM exported")
            with wave.open(str(source),"rb")as r:original=r.readframes(audio["frames"])
            with wave.open(str(directory/audio["path"]),"rb")as r:
                require((r.getframerate(),r.getnchannels(),r.getsampwidth(),r.getnframes())==(16000,1,2,audio["frames"]),"PCM asset format/length differs")
                require(r.readframes(r.getnframes())==original,"projected PCM differs from original prefix")
    for path,asset in manifest["assets"].items():require(digest(directory/path)==asset["sha256"],"audio asset hash differs")
    originals={c["criterion_id"]:c for t in read(score_dir/"scores.json")["turns"]for c in t["criteria"]}
    require(len(rewards)==len(originals) and len({r["annotation_id"]for r in rewards})==len(rewards),"reward criteria were duplicated or omitted")
    require({r["criterion"]["criterion_id"]for r in rewards}==set(originals),"original criterion coverage differs")
    lookup={s["sample_id"]:s for s in samples}
    scored_turns=read(score_dir/"scores.json")["turns"]
    for reward in rewards:
        original=originals[reward["criterion"]["criterion_id"]]
        require(reward["criterion"]==original and reward["value"]==original["reward"],"original criterion reward changed")
        require(reward["reward_mask"]==(original["reward"]is not None),"unknown criterion entered reward mask")
        members=[lookup[n]for n in reward["sample_ids"]]
        if original["method"]=="pcm_and_semantic_combined_behavior":
            turn=next(t for t in scored_turns if any(c["criterion_id"]==original["criterion_id"]for c in t["criteria"]))
            response_frames={r["start_frame"]for r in turn["responses"]}
            stop=original["measurement"].get("stop_frame")
            expected=[s["sample_id"]for s in samples if
                      (s["policy_role"]=="assistant"and s["observation"]["context"]["current_frame"]in response_frames)or
                      (s["policy_role"]=="assistant_listener"and s["observation"]["context"]["current_frame"]==stop)]
            require(set(reward["sample_ids"])==set(expected)and len(reward["sample_ids"])==len(expected),"combined behavior reward is not bound once to its actual stop and response actions")
            require(reward["credit_scope"]=="assistant_response_and_listener_action_group"and reward["duplicated_per_group_member"]is False,"combined behavior credit was duplicated")
            for member in members:require(reward["annotation_id"]in member["reward_annotation_ids"],"combined reward reference missing")
            continue
        expected_role="assistant_listener"if original["method"]=="deterministic_consumed_pcm"else"assistant"
        require(all(s["policy_role"]==expected_role for s in members),"reward attached to the wrong policy action")
        if expected_role=="assistant":
            turn=next(t for t in scored_turns if any(c["criterion_id"]==original["criterion_id"]for c in t["criteria"]))
            frames={r["start_frame"]for r in turn["responses"]}
            require({s["sample_id"]for s in members}=={s["sample_id"]for s in samples if s["policy_role"]=="assistant" and s["observation"]["context"]["current_frame"]in frames},"response-group reward refers to another turn")
        else:
            m=original["measurement"]
            if m["type"]=="T03":
                frame=m.get("stop_frame")
                start=m["user_start_frame"]
            else:
                frame=next((e["frame"]for e in m["listener_decisions"]),None)
                start=m.get("pause_start_frame")
            if frame is None and m["type"] not in {"backchannel", "side_speech"}:
                frame=next((e["frame"]for e in timeline["events"]if e["kind"]=="assistant_listener_decision" and start<=e["frame"]<m["user_end_frame"]),None)
            frames = {e["frame"] for e in m["listener_decisions"]} if m["type"] in {"backchannel", "side_speech"} else {frame}
            expected=[s["sample_id"]for s in samples if s["policy_role"]=="assistant_listener" and s["observation"]["context"]["current_frame"] in frames]
            require(reward["sample_ids"]==expected,"wait/stop reward refers to a different listener action")
        require(reward["duplicated_per_group_member"]is False,"group reward was duplicated")
        for member in members:require(reward["annotation_id"]in member["reward_annotation_ids"],"reward reference missing")
    require(manifest["on_policy_for_a_native_trained_model"]is False and manifest["rubric_or_rewards_in_policy_observation"]is False,"export overclaimed policy/source boundary")
    return {"schema":"duplex-runtime.dataset-integrity.v1","status":"passed","samples":len(samples),"roles":roles,"reward_annotations":len(rewards),"audio_assets":len(manifest["assets"]),
            "checks":"actual call inputs, consumed output and word loss boundaries, PCM prefix reconstruction, nonduplicated rewards and policy credit ownership"}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--dataset-dir",type=Path,required=True)
    args=parser.parse_args();proof=verify_dataset(args.dataset_dir);write_json(args.dataset_dir/"verification.json",proof);print(json.dumps(proof,ensure_ascii=False))


if __name__=="__main__":main()
