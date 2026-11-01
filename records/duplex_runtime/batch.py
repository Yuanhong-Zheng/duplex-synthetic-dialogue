"""Pinned episode sampling, concurrent workers and exact journal replay after process loss."""
from __future__ import annotations

import argparse
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
from duplex_runtime.call_journal import JournalModel, JournalStream, JournalTTS
from duplex_runtime.prepare import verify_prepared_session
from duplex_runtime.scoring import file_hashes
from duplex_runtime.speech import DashScopeTTSClient, SpeechSettings, digest
from duplex_runtime.synthetic_rollout import SyntheticRollout
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_simulator import configured_clients
from duplex_runtime.verify_synthetic import read, require, verify

ROOT = Path(__file__).resolve().parents[1]
CORE = ["duplex_runtime/"+name+".py" for name in ["batch", "call_journal", "synthetic_rollout", "assistant", "user_simulator", "speech", "alignment", "playback", "prepare", "environment", "verify_simulator"]]
CORE += ["minimax_client.py", "run_with_api_profile.py"]
CORE += ["duplex_runtime/prompts/"+name+".md" for name in ["user_policy", "user_guard", "assistant_policy", "assistant_listener", "trigger_observer"]]


def core_hashes():return {name: digest(ROOT/name) for name in CORE}


def create_batch(directory, sessions, count, workers, seed, model, fault_after=0):
    directory = directory.resolve()
    require(not directory.exists(), "use a new batch directory")
    require(count > 0 and 1 <= workers <= 4, "invalid batch size or worker count")
    sources = []
    for session in sessions:
        session = session.resolve(); verify_prepared_session(session)
        sources.append({"directory": str(session), "session_sha256": digest(session/"session.json"), "case_id": read(session/"session.json")["case_id"]})
    require(bool(sources), "no prepared sessions")
    directory.mkdir(parents=True); (directory/"jobs").mkdir()
    randomizer = random.Random(seed)
    jobs = []
    for i in range(count):
        source = randomizer.choice(sources)
        job = directory/"jobs"/f"E{i+1:03d}"; job.mkdir()
        shutil.copytree(Path(source["directory"]), job/"environment")
        item = {"episode_id": job.name, **source, "validation_crash_after_main_turns": fault_after if i == 0 else 0}
        jobs.append(item); write_json(job/"state.json", {"status": "pending", "attempts": []})
    hashes = core_hashes()
    for name in hashes:
        p = directory/"code_snapshot"/name; p.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT/name, p)
    manifest = {"schema": "duplex-runtime.batch.v1", "model": model, "workers": workers, "seed": seed,
                "sampling": "seeded_uniform_with_replacement_from_explicit_prepared_sessions", "jobs": jobs,
                "code_sha256": hashes, "git_baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "independent_episode_journals": True, "assistant_wav_url_recovery_authorized": True,
                "audio_mode": "offline_synthetic_PCM;16kHz_WAV;approved_case_voices",
                "validation_process_crash_is_service_failure": False, "pricing_configured": False}
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
    # One recorded HTTP attempt at a time; retries occur explicitly in new batch attempts.
    user.max_retries = guard.max_retries = 0
    user.fallback_enabled = guard.fallback_enabled = False
    key = os.environ.get("DASHSCOPE_API_KEY")
    require(bool(key), "DASHSCOPE_API_KEY missing")
    journal = job/"journal"
    user = JournalModel(user, journal, "user-policy")
    guard = JournalModel(guard, journal, "semantic-decisions")
    actor = JournalStream(StreamingChatClient(user.base_url, user.api_key, model=manifest["model"]), journal)
    user_tts = JournalTTS(DashScopeTTSClient(key, SpeechSettings(subtitle_type="word")), journal, "user-tts")
    actor_tts = JournalTTS(DashScopeTTSClient(key, SpeechSettings(voice_id="female-chengshu", subtitle_type="word")), journal, "assistant-tts")
    url_tts = JournalTTS(DashScopeTTSClient(key, SpeechSettings(voice_id="female-chengshu", subtitle_type="word", audio_delivery="url")), journal, "assistant-url-tts")
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
    rollout = SyntheticRollout(job/"environment", attempt/"rollout", user, guard, actor, user_tts, actor_tts, url_tts, progress_callback=progress)
    rollout.run()
    write_json(attempt/"rollout/integrity.json", verify(attempt/"rollout"))


def cost_ledger(job):
    rows = []
    for identity in sorted((job/"journal").glob("*/*/identity.json")):
        request = read(identity)
        for trial in sorted((identity.parent/"tries").glob("*")):
            state = read(trial/"state.json")
            meta = read(trial/"metadata.json") if (trial/"metadata.json").exists() else {}
            usage = meta.get("usage")
            if usage is None and (trial/"stream.json").exists():
                usage = next((c["chunk"]["usage"] for c in read(trial/"stream.json") if c["chunk"].get("usage")), None)
            body = request.get("request", {})
            rows.append({"record": str(trial.relative_to(job)), "kind": identity.parent.parent.name, "status": state["status"],
                         "model": request.get("model") or body.get("model"), "provider_usage": usage,
                         "tts_characters": len(body.get("input", {}).get("text", "")) if body else None,
                         "http_status": meta.get("http_status"), "http_attempts": meta.get("http_attempts", 1),
                         "wall_seconds": meta.get("journal_elapsed_seconds", meta.get("elapsed_seconds", state.get("elapsed_seconds"))),
                         "amount": None, "currency": None})
    ledger = {"schema": "duplex-runtime.call-cost.v1", "network_call_records": len(rows),
              "successful_calls": sum(r["status"] == "completed" for r in rows), "failed_or_incomplete_calls": sum(r["status"] != "completed" for r in rows),
              "provider_token_usage_available": sum(bool(r["provider_usage"]) for r in rows),
              "tts_characters_requested": sum(r["tts_characters"] or 0 for r in rows),
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
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--profile", default="commonstack-chat")
    parser.add_argument("--episode", action="append")
    parser.add_argument("--validate-crash-after", type=int, default=0)
    args = parser.parse_args(); directory = args.batch_dir.resolve()
    if args.command == "create":create_batch(directory, args.session_dir, args.episodes, args.workers, args.seed, args.model, args.validate_crash_after)
    if args.command == "episode":run_episode(directory, args.episode[0], args.credentials_file, args.profile)
    else:
        result = run_batch(directory, args.credentials_file, args.profile, args.episode)
        print(json.dumps({"status": result["status"], "episodes": [(s["episode_id"],s["status"]) for s in result["episodes"]]}, ensure_ascii=False))


if __name__ == "__main__":main()
