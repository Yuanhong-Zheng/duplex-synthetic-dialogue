"""Pinned episode sampling, concurrent workers and exact journal replay after process loss."""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

from duplex_runtime.assistant import StreamingChatClient
from duplex_runtime.audio_gain import AmbientGainPolicy, episode_gain_seed
from duplex_runtime.backchannel_policy import spoken_target
from duplex_runtime.call_journal import JournalModel, JournalStream, JournalTTS
from duplex_runtime.prepare import verify_prepared_session
from duplex_runtime.scoring import file_hashes
from duplex_runtime.speech import DashScopeTTSClient, SpeechSettings, digest
from duplex_runtime.side_speech import side_voice_ids
from duplex_runtime.synthetic_rollout import SyntheticRollout
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_simulator import configured_clients
from duplex_runtime.verify_synthetic import read, require, verify

ROOT = Path(__file__).resolve().parents[1]
CORE = ["duplex_runtime/"+name+".py" for name in ["batch", "call_journal", "synthetic_rollout", "assistant", "user_simulator", "speech", "alignment", "playback", "prepare", "environment", "verify_simulator", "backchannel_policy", "backchannel_metrics"]]
CORE += ["minimax_client.py", "run_with_api_profile.py"]
CORE += ["duplex_runtime/fork_episode.py", "duplex_runtime/audio_gain.py", "duplex_runtime/audio_validation.py", "revise_episode_anchor.py"]
CORE += ["duplex_runtime/side_speech.py"]
CORE += ["duplex_runtime/prompts/"+name+".md" for name in ["user_policy", "user_guard", "assistant_policy", "assistant_listener", "trigger_observer", "backchannel_observer"]]
CORE += ["duplex_runtime/prompts/user_repair.md"]
CORE += ["duplex_runtime/voice_profile.py", "duplex_runtime/prompts/voice_profile.md"]
CORE += ["duplex_runtime/request_retry.py"]


def core_hashes():return {name: digest(ROOT/name) for name in CORE}


def create_batch(directory, sessions, count, workers, seed, model, fault_after=0, *, backchannel_target=None,
                 user_voice_id=None, assistant_voice_id="female-chengshu",
                 third_party_voice_id=None, machine_voice_id=None, voice_profile_client=None, voice_catalog=None,
                 inherited_voice_plans=None):
    directory = directory.resolve()
    require(not directory.exists(), "use a new batch directory")
    require(count > 0 and 1 <= workers <= 4, "invalid batch size or worker count")
    if user_voice_id is not None:SpeechSettings(voice_id=user_voice_id)
    else:require(bool(inherited_voice_plans) or (voice_profile_client is not None and bool(voice_catalog)), "automatic user voice requires a profile mapper and verified catalog")
    SpeechSettings(voice_id=assistant_voice_id)
    if backchannel_target is not None:
        backchannel_target = spoken_target(1, target=backchannel_target)
    sources = []
    for session in sessions:
        session = session.resolve(); verify_prepared_session(session)
        sources.append({"directory": str(session), "session_sha256": digest(session/"session.json"), "case_id": read(session/"session.json")["case_id"]})
    require(bool(sources), "no prepared sessions")
    directory.mkdir(parents=True); (directory/"jobs").mkdir()
    voice_plans={}
    if user_voice_id is None:
        from duplex_runtime.voice_profile import compile_profile
        for source in sources:
            profile=read(Path(source['directory'])/'frozen/user_voice.json')['voice_profile']
            inherited=(inherited_voice_plans or {}).get(source['session_sha256'])
            if inherited:
                from duplex_runtime.voice_profile import validate_plan
                validate_plan(inherited,profile)
                voice_plans[source['session_sha256']]=copy.deepcopy(inherited)
                continue
            require(voice_profile_client is not None and bool(voice_catalog),'missing inherited plan for this frozen session')
            voice_plans[source['session_sha256']]=compile_profile(profile,voice_catalog,voice_profile_client,
                directory/'voice-profiles'/source['session_sha256'],
                previous_voice_ids=[p['mapping']['voice_id'] for p in voice_plans.values()])
    randomizer = random.Random(seed)
    jobs = []
    for i in range(count):
        source = randomizer.choice(sources)
        job = directory/"jobs"/f"E{i+1:03d}"; job.mkdir()
        shutil.copytree(Path(source["directory"]), job/"environment")
        item = {"episode_id": job.name, **source, "validation_crash_after_main_turns": fault_after if i == 0 else 0}
        item["ambient_gain_seed"] = episode_gain_seed(seed, job.name, source["case_id"])
        if user_voice_id is None:item['user_voice_plan']=voice_plans[source['session_sha256']]
        selected=user_voice_id or item['user_voice_plan']['settings']['voice_id']
        item['side_voices']=side_voice_ids(selected,assistant_voice_id,third_party_voice_id,machine_voice_id)
        jobs.append(item); write_json(job/"state.json", {"status": "pending", "attempts": []})
    hashes = core_hashes()
    for name in hashes:
        p = directory/"code_snapshot"/name; p.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT/name, p)
    manifest = {"schema": "duplex-runtime.batch.v1", "model": model, "workers": workers, "seed": seed,
                "sampling": "seeded_uniform_with_replacement_from_explicit_prepared_sessions", "jobs": jobs,
                "code_sha256": hashes, "git_baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "independent_episode_journals": True, "assistant_wav_url_recovery_authorized": True,
                "audio_mode": "offline_synthetic_PCM;16kHz_WAV;approved_case_voices",
                "validation_process_crash_is_service_failure": False, "pricing_configured": False,
                "backchannel_spoken_target": backchannel_target}
    manifest["voices"] = {"user": user_voice_id, "assistant": assistant_voice_id}
    if user_voice_id is not None:manifest["side_voices"] = jobs[0]['side_voices']
    manifest['user_voice_selection']='stage1_profile' if user_voice_id is None else 'explicit_override'
    manifest["side_speech_execution"] = "provider_word_boundary_asides_v1"
    manifest["model_repair_policy"] = {"user_candidate_repairs":2,"semantic_decision_repairs":2}
    manifest['request_retry_policy']={'max_retries':5,'owner':'request_client','same_parameters':True}
    write_json(directory/"manifest.json", manifest)
    return manifest


def run_episode(batch_dir, episode_id, credentials, profile):
    manifest = read(batch_dir/"manifest.json")
    require(core_hashes() == manifest["code_sha256"], "generator code changed; replay cannot silently change the episode")
    job = batch_dir/"jobs"/episode_id
    config = next(c for c in manifest["jobs"] if c["episode_id"] == episode_id)
    require(digest(job/"environment/session.json") == config["session_sha256"], "sampled prepared environment changed")
    state = read(job/"state.json")
    attempt = job/"attempts"/f"{len(state['attempts']):03d}"  # Parent has already appended this attempt.
    attempt.mkdir(parents=True)
    user, guard = configured_clients(credentials, profile, manifest["model"], manifest["model"])
    # Request failures get five identical-parameter retries inside the client.
    user.max_retries = guard.max_retries = 5
    user.fallback_enabled = guard.fallback_enabled = False
    key = os.environ.get("DASHSCOPE_API_KEY")
    require(bool(key), "DASHSCOPE_API_KEY missing")
    journal = job/"journal"
    user = JournalModel(user, journal, "user-policy")
    guard = JournalModel(guard, journal, "semantic-decisions", prompt_revisions=manifest.get("guard_prompt_revisions", []))
    actor = JournalStream(StreamingChatClient(user.base_url, user.api_key, model=manifest["model"]), journal)
    voices = manifest.get("voices", {"user":"male-qn-qingse", "assistant":"female-chengshu"})
    if config.get('user_voice_plan'):
        from duplex_runtime.voice_profile import validate_plan
        user_settings=validate_plan(config['user_voice_plan'],read(job/'environment/frozen/user_voice.json')['voice_profile'])
    else:user_settings=SpeechSettings(voice_id=voices['user'],subtitle_type='word')
    user_tts = JournalTTS(DashScopeTTSClient(key, user_settings), journal, "user-tts")
    actor_tts = JournalTTS(DashScopeTTSClient(key, SpeechSettings(voice_id=voices["assistant"], subtitle_type="word")), journal, "assistant-tts")
    url_tts = JournalTTS(DashScopeTTSClient(key, SpeechSettings(voice_id=voices["assistant"], subtitle_type="word", audio_delivery="url")), journal, "assistant-url-tts")
    side_tts = {role: JournalTTS(DashScopeTTSClient(key, SpeechSettings(voice_id=voice, subtitle_type="word")),
                               journal, role+"-tts") for role, voice in config.get('side_voices',manifest.get("side_voices", {})).items()}
    previous = read(job/"checkpoint.json") if (job/"checkpoint.json").exists() else None
    replay_verified = previous is None
    def progress(current):
        nonlocal replay_verified
        if previous and current["accepted_user_events"] == previous["accepted_user_events"]:
            require(current == previous, "reconstructed safe checkpoint differs from prior consumed prefix")
            replay_verified = True
            write_json(attempt/"replay-verification.json", {"status": "passed", "checkpoint": previous, "method": "exact_call_sequence_and_reconstructed_pcm_state"})
        if previous and current["accepted_user_events"] < previous["accepted_user_events"]:
            return
        require(replay_verified, "cannot advance beyond unverified recovery prefix")
        write_json(job/"checkpoint.json", current); write_json(attempt/"checkpoint.json", current)
        print(json.dumps({"episode": episode_id, "main_turns": current["delivered_main_turns"], "frame": current["frame"]}), flush=True)
        fault = config["validation_crash_after_main_turns"]
        if fault and current["delivered_main_turns"] == fault and not (job/"validation-crash.json").exists():
            write_json(job/"validation-crash.json", {"controlled_recovery_test": True, "exit_code": 86, "checkpoint": current})
            sys.stdout.flush(); os._exit(86)
    rollout = SyntheticRollout(job/"environment", attempt/"rollout", user, guard, actor, user_tts, actor_tts, url_tts,
                               progress_callback=progress, backchannel_target=manifest.get("backchannel_spoken_target"),
                               ambient_gain_policy=AmbientGainPolicy(seed=config["ambient_gain_seed"]),
                               side_tts=side_tts or None,user_voice_plan=config.get('user_voice_plan'),
                               model_repair_policy=manifest.get("model_repair_policy"),
                               prefix_anchor_replays=config.get('prefix_anchor_replays',()))
    rollout.run()
    write_json(attempt/"rollout/integrity.json", verify(attempt/"rollout"))


def cost_ledger(job):
    rows = []
    for identity in sorted([*(job/"journal").glob("*/*/identity.json"), *(job/"journal").glob("*/*/policy-revisions/*/identity.json")]):
        request = read(identity)
        for trial in sorted((identity.parent/"tries").glob("*")):
            state = read(trial/"state.json")
            meta = read(trial/"metadata.json") if (trial/"metadata.json").exists() else {}
            usage = meta.get("usage")
            if usage is None and (trial/"stream.json").exists():
                usage = next((c["chunk"]["usage"] for c in read(trial/"stream.json") if c["chunk"].get("usage")), None)
            body = request.get("request", {})
            inheritance = identity.parent/'inheritance.json'
            inherited = inheritance.exists()
            rows.append({"record": str(trial.relative_to(job)), "kind": identity.parent.relative_to(job/"journal").parts[0], "status": state["status"],
                         "model": request.get("model") or body.get("model"), "provider_usage": usage,
                         "tts_characters": len(body.get("input", {}).get("text", "")) if body else None,
                         "http_status": meta.get("http_status"), "http_attempts": meta.get("http_attempts", 1),
                         "wall_seconds": meta.get("journal_elapsed_seconds", meta.get("elapsed_seconds", state.get("elapsed_seconds"))),
                         "amount": None, "currency": None, "inherited_from_parent": inherited,
                         "new_network_request": not inherited,
                         "inherited_source": read(inheritance)["source"] if inherited else None})
    new_rows = [r for r in rows if not r["inherited_from_parent"]]
    ledger = {"schema": "duplex-runtime.call-cost.v1", "network_call_records": len(new_rows),
              "inherited_call_records": len(rows)-len(new_rows),
              "successful_calls": sum(r["status"] == "completed" for r in new_rows), "failed_or_incomplete_calls": sum(r["status"] != "completed" for r in new_rows),
              "provider_token_usage_available": sum(bool(r["provider_usage"]) for r in new_rows),
              "tts_characters_requested": sum(r["tts_characters"] or 0 for r in new_rows),
              "amount": None, "currency": None,
              "unknown_amount_reason": "Provider did not return a monetary amount and no verified tariff was configured; failed-call billing also unknown.",
              "replayed_requests_count_as_new_network_calls": False, "rows": rows}
    write_json(job/"cost.json", ledger)
    return ledger


def process_job(batch_dir, episode_id, credentials, profile):
    job = batch_dir/"jobs"/episode_id
    with (job/"worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(job/"state.json")
        if state["status"] == "completed":return state
        existing = job/"attempts"
        protected = file_hashes(existing) if existing.exists() else {}
        number = len(state["attempts"])+1
        state["attempts"].append({"attempt": number, "directory": f"attempts/{number:03d}"})
        state["status"] = "running"; write_json(job/"state.json", state)
        logfile = job/f"attempt-{number:03d}.log"
        command = [sys.executable, "-B", "-m", "duplex_runtime.batch", "episode", "--batch-dir", str(batch_dir),
                   "--episode", episode_id, "--credentials-file", str(credentials), "--profile", profile]
        started = time.monotonic()
        with logfile.open("w") as output:
            result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
        status = "completed" if result.returncode == 0 else "validation_interrupted" if result.returncode == 86 else "failed"
        state["attempts"][-1].update(exit_code=result.returncode, status=status, wall_seconds=time.monotonic()-started)
        state["status"] = status
        state["latest_rollout"] = f"attempts/{number:03d}/rollout"
        if protected:
            require(all(digest(existing/name) == value for name, value in protected.items()), "prior attempt was modified during recovery")
            write_json(job/f"prior-attempt-proof-{number:03d}.json", {"status": "passed", "files": protected})
        cost_ledger(job); write_json(job/"state.json", state)
        print(json.dumps({"episode": episode_id, "status": status, "attempt": number}), flush=True)
        return state


def run_batch(directory, credentials, profile, episodes=None):
    manifest = read(directory/"manifest.json")
    require(core_hashes() == manifest["code_sha256"], "pinned generator changed")
    selected = [j["episode_id"] for j in manifest["jobs"] if episodes is None or j["episode_id"] in episodes]
    with ThreadPoolExecutor(max_workers=manifest["workers"]) as pool:
        list(pool.map(lambda episode: process_job(directory, episode, credentials, profile), selected))
    states = [{"episode_id": j["episode_id"], **read(directory/"jobs"/j["episode_id"]/"state.json")} for j in manifest["jobs"]]
    result = {"schema": manifest["schema"], "status": "completed" if all(s["status"] == "completed" for s in states) else "incomplete",
              "episodes": states, "independent_episode_journals": True, "native_realtime_latency_measured": False}
    write_json(directory/"result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["create", "resume", "episode"])
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, action="append", default=[])
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261029)
    parser.add_argument("--model", default="openai/gpt-5.6-sol")
    parser.add_argument("--user-voice-id", help="explicit override; otherwise compile the frozen Stage-1 voice_profile")
    parser.add_argument("--assistant-voice-id", default="female-chengshu", help="pin the assistant voice, including identical-voice URL recovery")
    parser.add_argument("--third-party-voice-id", help="distinct nearby-person voice, pinned at creation")
    parser.add_argument("--machine-voice-id", help="distinct spoken-device voice, pinned at creation")
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--profile", default="commonstack-chat")
    parser.add_argument("--episode", action="append")
    parser.add_argument("--validate-crash-after", type=int, default=0)
    parser.add_argument("--backchannel-target-min", type=int, help="create only: soft actual-speech target")
    parser.add_argument("--backchannel-target-max", type=int, help="create only: soft actual-speech target")
    args = parser.parse_args(); directory = args.batch_dir.resolve()
    target = None
    if args.backchannel_target_min is not None or args.backchannel_target_max is not None:
        if args.command != "create":
            parser.error("backchannel targets are pinned at batch creation")
        try:
            target = spoken_target(1, target=[args.backchannel_target_min, args.backchannel_target_max])
        except ValueError as exc:
            parser.error(str(exc))
    mapper,catalog=None,None
    if args.command=='create' and args.user_voice_id is None:
        from duplex_runtime.voice_profile import fetch_catalog
        mapper,_=configured_clients(args.credentials_file,args.profile,args.model,args.model)
        catalog=fetch_catalog(os.environ.get('DASHSCOPE_API_KEY',''),directory.parent/(directory.name+'-voice-catalog'))
    if args.command == "create":create_batch(directory, args.session_dir, args.episodes, args.workers, args.seed, args.model, args.validate_crash_after,
                                             backchannel_target=target, user_voice_id=args.user_voice_id, assistant_voice_id=args.assistant_voice_id,
                                             third_party_voice_id=args.third_party_voice_id, machine_voice_id=args.machine_voice_id,
                                             voice_profile_client=mapper,voice_catalog=catalog)
    if args.command == "episode":run_episode(directory, args.episode[0], args.credentials_file, args.profile)
    else:
        result = run_batch(directory, args.credentials_file, args.profile, args.episode)
        print(json.dumps({"status": result["status"], "episodes": [(s["episode_id"],s["status"]) for s in result["episodes"]]}, ensure_ascii=False))


if __name__ == "__main__":main()
