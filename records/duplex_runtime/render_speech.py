"""Render archived, accepted simulator events with real TTS; never generate actor replies."""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import re
import shutil

import numpy as np

from duplex_runtime.prepare import verify_prepared_session
from duplex_runtime.speech import DashScopeTTSClient, SpeechError, SpeechRenderer, decode_wav, digest
from duplex_runtime.user_simulator import json_hash, write_json


def verify_speech_run(directory: Path) -> dict:
    prepared = verify_prepared_session(directory / "environment")
    manifest = json.loads((directory / "speech/manifest.json").read_text())
    if manifest["prepared_session_sha256"] != digest(directory / "environment/session.json"):
        raise SpeechError("speech environment differs from the prepared snapshot")
    if manifest["actor_connected"] or manifest["playback_executed"]:
        raise SpeechError("speech rendering cannot claim real actor or playback execution")
    bundle = json.loads((directory / "bundle.json").read_text())
    for item in bundle["sources"]:
        source = directory / item["path"]
        if digest(source) != item["sha256"]:
            raise SpeechError("archived simulator source changed")
    sources = {item["id"]: [json.loads(line) for line in (directory / item["path"]).read_text().splitlines() if line.strip()]
               for item in bundle["sources"]}
    report = json.loads((directory / "render-manifest.json").read_text())
    if report.get("status") != "rendered" or len(report["scenarios"]) != len(bundle["scenarios"]):
        raise SpeechError("speech run is incomplete")
    audio_count = 0
    pauses = []
    silent_waits = 0
    main_ids = []
    for scenario, source_scenario in zip(report["scenarios"], bundle["scenarios"]):
        if scenario["id"] != source_scenario["id"] or len(scenario["events"]) != len(source_scenario["events"]):
            raise SpeechError("rendered scenario differs from its source")
        for record, original in zip(scenario["events"], source_scenario["events"]):
            event = original["event"]
            if sources[original["source_id"]][original["source_index"]] != event:
                raise SpeechError("event differs from the original accepted JSONL record")
            render = json.loads((directory / "speech" / record["render_path"]).read_text())
            archived = json.loads((directory / "speech" / render["source_event"]).read_text())
            if archived != event or json_hash(event) != render["user_event_sha256"]:
                raise SpeechError("rendered event text/provenance changed")
            if scenario["id"] == "mainline" and event["action"] == "deliver":
                main_ids.append(event["focus_turn_id"])
            if event["action"] == "wait":
                if render["main"] is not None or render["side_speech"]:
                    raise SpeechError("wait unexpectedly produced audio")
                silent_waits += 1
            for audio in [render["main"], *render["side_speech"]]:
                if audio is None:
                    continue
                audio_count += 1
                file = directory / "speech" / audio["path"]
                if digest(file) != audio["sha256"]:
                    raise SpeechError("rendered audio hash changed")
                pcm = decode_wav(file.read_bytes())
                if len(pcm) != audio["frames"]:
                    raise SpeechError("rendered frame count changed")
                if "segments" in audio:
                    segments = audio["segments"]
                    if "".join(item["request"]["input"]["text"] for item in segments) != event["utterance"]:
                        raise SpeechError("TTS segments altered the accepted user text")
                    raw_pcms = []
                    for segment in segments:
                        raw_file = directory / "speech" / segment["path"]
                        if digest(raw_file) != segment["sha256"]:
                            raise SpeechError("source synthesis changed")
                        raw_pcms.append(decode_wav(raw_file.read_bytes()))
                    if audio["pause"]:
                        pause = audio["pause"]
                        cue = event["dispatch"]["pause_cue"]
                        if pause["requested_ms"] != cue["duration_ms"] or pause["after_text"] != cue["after_text"]:
                            raise SpeechError("pause differs from the frozen cue")
                        left, right = [raw[seg["trim"]["kept_start_frame"]:seg["trim"]["kept_end_frame"]]
                                       for raw, seg in zip(raw_pcms, segments)]
                        start, end = pause["start_frame"], pause["end_frame"]
                        if start != len(left) or (end - start) * 1000 != cue["duration_ms"] * 16000:
                            raise SpeechError("pause sample count differs from its requested duration")
                        if not (np.array_equal(pcm[:start], left) and np.array_equal(pcm[end:], right)
                                and np.all(pcm[start:end] == 0) and pcm[start - 1] != 0 and pcm[end] != 0):
                            raise SpeechError("pause waveform is not the documented source splice")
                        pauses.append({"turn_id": event["focus_turn_id"], **pause})
                    elif not np.array_equal(pcm, raw_pcms[0]):
                        raise SpeechError("unpaused utterance was modified")
    for cache in (directory / "speech/cache").iterdir():
        asset = json.loads((cache / "asset.json").read_text())
        call = directory / "speech" / asset["call"]
        response = json.loads((call / "response.json").read_text())
        request = json.loads((call / "request.json").read_text())
        metadata = json.loads((call / "metadata.json").read_text())
        output = response.get("output") or {}
        if request != asset["request"] or metadata.get("provider") != "dashscope" or metadata.get("http_status") != 200:
            raise SpeechError("synthesis provenance differs from the recorded API call")
        if output.get("base_resp", {}).get("status_code") != 0 or bytes.fromhex(output["data"]["audio"]) != (cache / "raw.wav").read_bytes():
            raise SpeechError("source WAV differs from the original API response")
    frozen_turns = json.loads((directory / "environment/frozen/episode_script.json").read_text())["episode_turns"]
    if main_ids != [turn["turn_id"] for turn in frozen_turns]:
        raise SpeechError("complete mainline was not rendered exactly once in frozen order")
    return {"status": "passed", "case_id": prepared["case_id"], "main_turns": len(main_ids),
            "accepted_user_events": sum(len(item["events"]) for item in report["scenarios"]),
            "audio_assets": audio_count, "silent_waits": silent_waits, "pause_measurements": pauses,
            "sample_rate": 16000, "channels": 1, "sample_width_bytes": 2,
            "tts_requests": report["tts_requests"], "cache_hits": report["cache_hits"],
            "source_wavs_verified_against_api_responses": len(list((directory / "speech/cache").iterdir())),
            "source_event_streams_verified": len(bundle["sources"]),
            "actor_connected": False, "playback_executed": False, "independent_asr_performed": False,
            "quality_scope": "Format, source text, provenance, PCM splice and sample-exact inserted silence verified. Pronunciation and join naturalness remain available for audition."}


def render_bundle(prepared_dir: Path, bundle_file: Path, output_dir: Path, client, *, voice_acceptance_reason: str):
    verify_prepared_session(prepared_dir)
    bundle = json.loads(bundle_file.read_text())
    if output_dir.exists() or output_dir.resolve().is_relative_to(prepared_dir.resolve()):
        raise SpeechError("use a new output directory outside the original session")
    session = json.loads((prepared_dir / "session.json").read_text())
    if bundle["case_id"] != session["case_id"] or bundle["prepared_session_sha256"] != digest(prepared_dir / "session.json"):
        raise SpeechError("event bundle belongs to a different prepared session")
    source_lines = {}
    source_ids = [source["id"] for source in bundle["sources"]]
    if len(source_ids) != len(set(source_ids)) or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", item) for item in source_ids):
        raise SpeechError("source IDs must be unique safe filenames")
    for source in bundle["sources"]:
        path = Path(source["path"])
        if output_dir.resolve().is_relative_to(path.resolve().parent):
            raise SpeechError("speech output must be outside the original simulator runs")
        if digest(path) != source["sha256"]:
            raise SpeechError("source event stream changed before rendering")
        source_lines[source["id"]] = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for scenario in bundle["scenarios"]:
        for record in scenario["events"]:
            if source_lines[record["source_id"]][record["source_index"]] != record["event"]:
                raise SpeechError("event does not match its original JSONL source")
    output_dir.mkdir(parents=True)
    shutil.copytree(prepared_dir, output_dir / "environment")
    (output_dir / "provenance").mkdir()
    copied = copy.deepcopy(bundle)
    for source in copied["sources"]:
        original = Path(source["path"])
        relative = f"provenance/{source['id']}.jsonl"
        shutil.copyfile(original, output_dir / relative)
        source["original_path"] = str(original)
        source["path"] = relative
    write_json(output_dir / "bundle.json", copied)
    renderer = SpeechRenderer(output_dir / "environment", output_dir / "speech", client,
                              voice_acceptance_reason=voice_acceptance_reason)
    result = {"schema": "duplex-runtime.speech-case.v1", "case_id": bundle["case_id"], "status": "rendering", "scenarios": []}
    write_json(output_dir / "render-manifest.json", result)
    try:
        for source in copied["scenarios"]:
            scenario = {"id": source["id"], "title": source["title"], "events": []}
            result["scenarios"].append(scenario)
            for record in source["events"]:
                event = record["event"]
                rendered = renderer.render_event(event)
                scenario["events"].append({"label": record["label"], "render_path": f"events/{rendered['event_id']}/render.json"})
                result.update(tts_requests=renderer.model_calls, cache_hits=renderer.cache_hits)
                write_json(output_dir / "render-manifest.json", result)
                print(json.dumps({"scenario": source["id"], "turn": event["focus_turn_id"], "action": event["action"],
                    "status": rendered["status"], "tts_requests": renderer.model_calls, "cache_hits": renderer.cache_hits}, ensure_ascii=False), flush=True)
        result["status"] = "rendered"
        write_json(output_dir / "render-manifest.json", result)
        verification = verify_speech_run(output_dir)
        write_json(output_dir / "verification.json", verification)
        print(json.dumps(verification, ensure_ascii=False), flush=True)
        return verification
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, tts_requests=renderer.model_calls)
        write_json(output_dir / "render-manifest.json", result)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--session-dir", type=Path, required=True)
    render.add_argument("--event-bundle", type=Path, required=True)
    render.add_argument("--output-dir", type=Path, required=True)
    render.add_argument("--voice-acceptance-reason", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps(verify_speech_run(args.run_dir), ensure_ascii=False))
    else:
        secret = os.environ.get("DASHSCOPE_API_KEY") or getpass.getpass("DASHSCOPE_API_KEY (hidden, memory only): ")
        render_bundle(args.session_dir, args.event_bundle, args.output_dir, DashScopeTTSClient(secret),
                      voice_acceptance_reason=args.voice_acceptance_reason)


if __name__ == "__main__":
    main()
