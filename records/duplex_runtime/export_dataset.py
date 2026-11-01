"""Model-independent observations/actions, causal PCM assets and separately scoped rewards."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import wave

import numpy as np

from duplex_runtime.scoring import file_hashes
from duplex_runtime.speech import digest, encode_wav
from duplex_runtime.user_simulator import json_hash, write_json
from duplex_runtime.verify_scoring import verify_scores
from duplex_runtime.verify_synthetic import read, require, verify


class AssetWriter:
    def __init__(self, directory):
        self.directory = directory; (directory/"audio").mkdir(exist_ok=True)
        self.assets = {}

    def prefix(self, source, end_frame, *, source_relative):
        with wave.open(str(source), "rb") as reader:
            require(reader.getnchannels() == 1 and reader.getframerate() == 16000, "export requires source mono PCM16")
            require(0 <= end_frame <= reader.getnframes(), "audio projection extends beyond consumed source")
            pcm = reader.readframes(end_frame)
        if not end_frame:return None
        raw = encode_wav(np.frombuffer(pcm, dtype="<i2")); sha = hashlib.sha256(raw).hexdigest()
        name = "audio/"+sha+".wav"
        if not (self.directory/name).exists():(self.directory/name).write_bytes(raw)
        self.assets[name] = {"sha256": sha, "frames": end_frame, "sample_rate": 16000, "channels": 1}
        return {"path": name, "frames": end_frame, "source": source_relative, "source_start_frame": 0, "source_end_frame": end_frame}


def text_boundary(response, heard_text):
    require(response["heard_text"].startswith(heard_text), "exported heard prefix differs from consumed assistant text")
    if not heard_text:return 0
    spans = [s for s in response["alignment"] if s["end_char"] == len(heard_text) and s["end_frame"] <= response["played_frames"]]
    require(bool(spans), "text prefix has no completed provider word")
    return spans[-1]["end_frame"]


def export_run(run, score_dir, output, *, episode_id=None):
    run, score_dir, output = run.resolve(), score_dir.resolve(), output.resolve()
    require(not output.exists() and not output.is_relative_to(run), "use a new export directory outside the rollout")
    integrity = verify(run); score_integrity = verify_scores(score_dir, run)
    timeline = read(run/"timeline.json"); scores = read(score_dir/"scores.json")
    require(integrity["rollout_status"] == "completed", "incomplete trajectories need an explicit partial-export policy")
    output.mkdir(parents=True); assets = AssetWriter(output)
    episode_id = episode_id or run.name
    placements = {p["audio_id"]: p for p in timeline["placements"]}
    responses = {r["response_id"]: r for r in timeline["responses"]}
    calls = read(run/"assistant/calls.json")
    source_manifest = read(run/"manifest.json")
    samples, annotations, groups = [], [], []
    response_samples, listener_samples = {}, {}
    def actor_audio(context):
        result = []
        consumed = {}
        for heard in context["observed_audio"]:
            identifier = heard["audio_id"]
            require(heard["through_frame"] <= context["current_frame"], "future heard fragment entered training input")
            consumed[identifier] = max(consumed.get(identifier,0), heard["through_frame"]-placements[identifier]["start_frame"])
        for identifier, end in consumed.items():
            p = placements[identifier]
            require(end <= p["frames"], "unplayed PCM entered observation")
            item = assets.prefix(run/p["path"], end, source_relative=p["path"])
            if item:result.append({"audio_id": identifier, **item})
        return result
    def provenance(folder):
        metadata = read(folder/"metadata.json")
        return {"call_directory": str(folder.relative_to(run)), "input_sha256": digest(folder/"input.json"),
                "model": metadata.get("model",metadata.get("requested_model")),
                "new_network_request_in_final_attempt": metadata.get("new_model_request", True)}
    for item in calls:
        if item["role"] == "trigger":continue  # Environment observer is not the assistant policy.
        folder = run/"assistant"/item["path"]
        context = read(folder/"input.json")
        sample_id = episode_id+":"+item["path"]
        sample = {"schema":"duplex-runtime.training-sample.v1", "sample_id":sample_id,"episode_id":episode_id,
                  "policy_role":"assistant" if item["role"]=="response" else "assistant_listener",
                  "observation":{"system_prompt":(folder/"prompt.md").read_text(),"context":context,"aligned_pcm_prefixes":actor_audio(context)},
                  "provenance":provenance(folder),"reward_annotation_ids":[],"native_audio_policy":False}
        if item["role"] == "response":
            response = next(r for r in responses.values() if r["start_frame"] == context["current_frame"])
            p = placements[response["response_id"]]
            aligned = text_boundary(response,response["heard_text"])
            audio = assets.prefix(run/p["path"], p["frames"],source_relative=p["path"])
            sample.update(action={"kind":"text_response","text":response["heard_text"],"paired_consumed_audio":audio},
                          training_region={"target_text":response["heard_text"],"character_ranges":[[0,len(response["heard_text"])]] if response["heard_text"] else [],
                                           "audio_frame_ranges":[[0,aligned]] if aligned else [],"mask_unit":"Unicode_characters_and_PCM_frames;not_tokenizer_tokens",
                                           "unplayed_text_excluded":True,"unplayed_pcm_excluded":True,"partial_word_pcm_after_last_completed_word_excluded_from_loss":True},
                          generated_boundary={"received_frames":response["received_frames"],"consumed_frames":response["played_frames"],"aligned_loss_frames":aligned,"outcome":response["outcome"]},
                          trainable=bool(aligned and response["heard_text"]))
            response_samples[response["response_id"]] = sample_id
        else:
            raw=(folder/"raw_response.txt").read_text(); decision=read(folder/"decision.json")
            sample.update(action={"kind":"listener_decision",**decision},training_region={"target_text":raw,"character_ranges":[[0,len(raw)]],"audio_frame_ranges":[],"mask_unit":"Unicode_characters;not_tokenizer_tokens"},trainable=True)
            listener_samples[context["current_frame"]] = sample_id
        samples.append(sample)
    for item in read(run/"user-results.json"):
        event=item["event"]; folder=run/"user/calls"/f"{event['generation_call']:04d}-generation"
        view=read(folder/"input.json"); audio=[];heard=view["observation"]["heard_text"]
        if heard:
            candidates=[r for r in responses.values() if r["start_frame"]<=item["start_frame"]]
            response=max(candidates,key=lambda r:r["start_frame"])
            p=placements[response["response_id"]]
            asset=assets.prefix(run/p["path"],text_boundary(response,heard),source_relative=p["path"])
            if asset:audio.append({"audio_id":response["response_id"],**asset})
        main=item["rendered"]["main"]
        paired=assets.prefix(run/"user-speech"/main["path"],main["frames"],source_relative="user-speech/"+main["path"]) if main else None
        raw=(folder/"raw_response.txt").read_text()
        samples.append({"schema":"duplex-runtime.training-sample.v1","sample_id":episode_id+f":user-{event['sequence']:03d}","episode_id":episode_id,
                        "policy_role":"user","observation":{"system_prompt":(folder/"prompt.md").read_text(),"context":view,"aligned_pcm_prefixes":audio},
                        "action":{"kind":"user_policy", "candidate":json.loads(raw),"paired_consumed_audio":paired},
                        "training_region":{"target_text":raw,"character_ranges":[[0,len(raw)]],"audio_frame_ranges":[[0,main["frames"]]] if main else [],"mask_unit":"Unicode_characters_and_PCM_frames;not_tokenizer_tokens"},
                        "provenance":provenance(folder),"trainable":True,"reward_annotation_ids":[],
                        "validation_label":{"user_guard":event["semantic_guard"],"is_assistant_reward":False},"native_audio_policy":False})
    sample_lookup={s["sample_id"]:s for s in samples}
    for turn in scores["turns"]:
        response_ids=[response_samples[r["response_id"]] for r in turn.get("responses",[])]
        for criterion in turn["criteria"]:
            members=response_ids
            scope="assistant_response_group"
            if criterion["method"]=="deterministic_consumed_pcm":
                m=criterion["measurement"]
                frame=m.get("stop_frame") if m["type"]=="T03" else next((e["frame"]for e in m["listener_decisions"]),None)
                if frame is None:
                    start=m["user_start_frame"] if m["type"]=="T03" else m["pause_start_frame"]
                    frame=next((e["frame"]for e in timeline["events"]if e["kind"]=="assistant_listener_decision" and start<=e["frame"]<m["user_end_frame"]),None)
                members=[listener_samples[frame]] if frame in listener_samples else []
                scope="assistant_listener_action" if members else "no_observed_listener_action;reward_retained_without_policy_credit"
            annotation_id=episode_id+":"+criterion["criterion_id"]
            annotation={"annotation_id":annotation_id,"episode_id":episode_id,"criterion":criterion,"sample_ids":members,"credit_scope":scope,
                        "value":criterion["reward"],"reward_mask":criterion["reward"] is not None,"duplicated_per_group_member":False}
            annotations.append(annotation)
            for identifier in members:sample_lookup[identifier]["reward_annotation_ids"].append(annotation_id)
        groups.append({"turn_id":turn["turn_id"],"response_sample_ids":response_ids,"score_file_sha256":digest(score_dir/f"score-{turn['turn_id']}.json")})
    sides=[]
    for p in placements.values():
        if p.get("kind")=="side_speech":
            audio=assets.prefix(run/p["path"],p["frames"],source_relative=p["path"])
            sides.append({"audio_id":p["audio_id"],"start_frame":p["start_frame"],"text":p["text"],"audio":audio,
                          "source":"frozen_environment_speech_event","assistant_reward":None,"policy_training_target":False})
    (output/"samples.jsonl").write_text(''.join(json.dumps(s,ensure_ascii=False)+'\n' for s in samples))
    (output/"rewards.jsonl").write_text(''.join(json.dumps(s,ensure_ascii=False)+'\n' for s in annotations))
    write_json(output/"side-speech.json",sides)
    manifest={"schema":"duplex-runtime.generic-dataset.v1","episode_id":episode_id,"case_id":source_manifest["case_id"],
              "source_run":str(run),"score_dir":str(score_dir),"source_files_sha256":file_hashes(run),"score_files_sha256":file_hashes(score_dir),
              "source_integrity":integrity,"score_integrity":score_integrity,"samples":len(samples),"reward_annotations":len(annotations),"assets":assets.assets,"score_groups":groups,
              "scoring_summary":scores["summary"],"native_audio_policy":False,"on_policy_for_a_native_trained_model":False,
              "mask_unit":"Unicode_characters_and_PCM_frames;tokenizer_adapter_not_selected","observation_audio_projection":"provider_word_completed_prefixes_of_consumed_PCM",
              "rubric_or_rewards_in_policy_observation":False,"side_speech_added_as_reward":False}
    write_json(output/"manifest.json",manifest)
    write_json(output/"artifact-hashes.json",{"files":file_hashes(output)})
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir",type=Path,required=True);parser.add_argument("--score-dir",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True);parser.add_argument("--episode-id")
    args=parser.parse_args();result=export_run(args.run_dir,args.score_dir,args.output_dir,episode_id=args.episode_id)
    print(json.dumps({k:result[k] for k in ["episode_id","samples","reward_annotations","on_policy_for_a_native_trained_model"]},ensure_ascii=False))


if __name__=="__main__":main()
