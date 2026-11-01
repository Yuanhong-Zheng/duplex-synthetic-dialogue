"""Score frozen Rubrics against consumed PCM and independent post-hoc judgments."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
import wave

import numpy as np

from duplex_runtime.speech import digest
from duplex_runtime.user_simulator import json_hash, write_json
from duplex_runtime.verify_synthetic import verify, read, require
from duplex_runtime.verify_simulator import configured_clients

STATUSES = ("pass", "fail", "not_triggered", "insufficient_evidence")
PROMPT = Path(__file__).parent / "prompts/trajectory_judge.md"


def file_hashes(directory):
    return {str(p.relative_to(directory)): digest(p) for p in sorted(directory.rglob("*")) if p.is_file()}


def criteria_for(rubric):
    return [{"criterion_id": f"{rubric['turn_id']}-{letter}{index:02d}", "kind": kind, "text": text}
            for kind, letter in [("semantic", "S"), ("behavior", "B")]
            for index, text in enumerate(rubric[kind], 1)]


def response_evidence(response):
    """The Judge receives consumed words only; neither received text nor PCM tails."""
    return {"response_id": response["response_id"], "heard_text": response["heard_text"],
            "start_frame": response["start_frame"], "end_frame": response["start_frame"] + response["played_frames"],
            "outcome": response["outcome"], "interrupted": response["outcome"] == "queue_discarded"}


def quote_time(response, quote):
    require(bool(quote) and quote in response["heard_text"], "Judge quote is absent from consumed response")
    # Repeated quotations use the earliest matching occurrence and state that policy.
    begin = response["heard_text"].index(quote)
    end = begin + len(quote)
    spans = [s for s in response["alignment"] if s["start_char"] < end and s["end_char"] > begin]
    require(bool(spans), "quoted evidence lacks provider word alignment")
    return {"response_id": response["response_id"], "quote": quote,
            "start_frame": response["start_frame"] + min(s["start_frame"] for s in spans),
            "end_frame": response["start_frame"] + min(response["played_frames"], max(s["end_frame"] for s in spans)),
            "alignment_source": "provider_word_timestamps;earliest_exact_quote_occurrence"}


def validate_judgment(value, packet, response_lookup):
    require(isinstance(value, dict) and set(value) == {"turn_id", "criteria"}, "malformed scoring response")
    require(value["turn_id"] == packet["turn_id"] and isinstance(value["criteria"], list), "Judge turn differs")
    require([v.get("criterion_id") for v in value["criteria"] if isinstance(v, dict)] ==
            [c["criterion_id"] for c in packet["judge_criteria"]], "Judge changed criterion identity/order")
    allowed = {r["response_id"] for r in packet["responses"]}
    rows = []
    for item in value["criteria"]:
        require(set(item) == {"criterion_id", "status", "reason", "evidence"}, "malformed scoring criterion")
        require(item["status"] in STATUSES and isinstance(item["reason"], str) and bool(item["reason"].strip()), "invalid scoring status/reason")
        require(isinstance(item["evidence"], list), "evidence must be an array")
        require(item["status"] != "not_triggered" or not packet["triggered"], "delivered turn cannot be not_triggered")
        require(item["status"] != "pass" or bool(item["evidence"]), "pass requires consumed response evidence")
        evidence = []
        for quote in item["evidence"]:
            require(isinstance(quote, dict) and set(quote) == {"response_id", "quote"}, "malformed evidence quote")
            require(quote["response_id"] in allowed and isinstance(quote["quote"], str), "quote refers to another turn")
            evidence.append(quote_time(response_lookup[quote["response_id"]], quote["quote"]))
        rows.append({**item, "evidence": evidence})
    return rows


def count_signal(stereo, start, end, channel):
    return int(np.count_nonzero(stereo[max(0, start):max(0, end), channel]))


def measure_turn(turn, main, matching_responses, timeline, stereo):
    """Measure observable execution, without classifying task meaning by keywords."""
    if main is None:
        return {"triggered": False, "execution_status": "not_triggered"}
    start, end = main["start_frame"], main["start_frame"] + main["frames"]
    events = timeline["events"]
    measured = {"triggered": True, "user_start_frame": start, "user_end_frame": end}
    if "T02" in turn["timing_refs"]:
        pause = main["pause"]
        left, right = start + pause["start_frame"], start + pause["end_frame"]
        decisions = [e for e in events if e["kind"] == "assistant_listener_decision" and left <= e["frame"] < right]
        premature = [r["response_id"] for r in timeline["responses"] if left <= r["start_frame"] < end]
        signal = count_signal(stereo, left, end, 1)
        measured.update(type="T02", pause_start_frame=left, pause_end_frame=right,
                        pause_frames=right-left, requested_ms=pause["requested_ms"],
                        user_pause_nonzero_samples=count_signal(stereo, left, right, 0),
                        assistant_samples_before_user_end=signal, premature_response_ids=premature,
                        listener_decisions=decisions)
        measured["execution_status"] = "pass" if not signal and not premature and decisions and all(e["decision"] in {"listen", "ignore"} for e in decisions) else "fail"
    elif "T03" in turn["timing_refs"]:
        previous = [r for r in timeline["responses"] if r["start_frame"] <= start < r["start_frame"] + r["played_frames"]]
        require(len(previous) <= 1, "multiple assistant streams at interruption")
        old = previous[0] if previous else None
        stops = [e for e in events if e["kind"] == "assistant_queue_discard" and old and e["response_id"] == old["response_id"] and start <= e["frame"] <= end]
        stop = stops[0] if len(stops) == 1 else None
        stop_frame = stop["frame"] if stop else None
        silent = count_signal(stereo, stop_frame, end, 1) if stop else None
        resumption = bool(matching_responses) and all(r["start_frame"] >= end for r in matching_responses)
        measured.update(type="T03", interrupted_response_id=old["response_id"] if old else None,
                        stop_frame=stop_frame, onset_to_stop_ms=(stop_frame-start)/16 if stop else None,
                        assistant_samples_after_stop_before_user_end=silent,
                        discarded_audio_frames=stop["discarded_audio_frames"] if stop else 0,
                        new_response_ids=[r["response_id"] for r in matching_responses],
                        resume_after_user_finished=resumption, provider_request_cancelled=False,
                        native_realtime_latency_measured=False)
        measured["execution_status"] = "pass" if old and stop and not silent and old["outcome"] == "queue_discarded" else "fail"
        measured["recovery_execution_status"] = "pass" if measured["execution_status"] == "pass" and resumption else "fail"
    else:
        measured.update(type="T01", execution_status="not_scored")
    return measured


def aggregate(criteria):
    result = {}
    for kind in ("semantic", "behavior"):
        rows = [c for c in criteria if c["kind"] == kind]
        counts = {s: sum(c["status"] == s for c in rows) for s in STATUSES}
        scored = counts["pass"] + counts["fail"]
        result[kind] = {**counts, "total": len(rows), "scored": scored,
                        "pass_rate": counts["pass"]/scored if scored else None,
                        "observed_coverage": scored/len(rows) if rows else None}
    return result


def combine_scores(criteria, measured, judge_rows):
    judgments = {r["criterion_id"]: r for r in judge_rows}
    results = []
    for criterion in criteria:
        identifier = criterion["criterion_id"]
        if criterion["kind"] == "semantic":
            result = {**criterion, **judgments[identifier], "method": "independent_model_judge"}
        elif measured["type"] == "T02" or identifier.endswith("B01"):
            status = measured["execution_status"]
            result = {**criterion, "status": status, "method": "deterministic_consumed_pcm",
                      "reason": "依据用户发声、停顿、助手停止边界及已消费 PCM 核对原行为标准。",
                      "evidence": [], "measurement": measured}
        else:
            judgment = judgments[identifier]
            execution = measured["recovery_execution_status"]
            status = "fail" if execution == "fail" else judgment["status"]
            result = {**criterion, **judgment, "status": status, "method": "pcm_execution_and_independent_semantic_judge",
                      "measurement": measured}
        result["reward"] = 1 if result["status"] == "pass" else 0 if result["status"] == "fail" else None
        results.append(result)
    return results


def prepare_packets(directory, output):
    timeline = read(directory/"timeline.json")
    source = directory/"user/environment/frozen"
    script = read(source/"episode_script.json")["episode_turns"]
    rubrics = read(source/"actor_rubric.json")["turn_rubrics"]
    user_events = read(directory/"user-results.json")
    with wave.open(str(directory/"dialogue-stereo.wav"), "rb") as reader:
        stereo = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").reshape(-1, 2)
    mains = []
    for item in user_events:
        event = item["event"]
        if event["action"] != "deliver":
            continue
        p = next(p for p in timeline["placements"] if p["audio_id"] == f"user-{event['sequence']:03d}")
        mains.append({**p, "pause": (item["rendered"]["main"] or {}).get("pause"), "event": event})
    packets, measurements, response_lookup = [], {}, {r["response_id"]: r for r in timeline["responses"]}
    for turn, rubric in zip(script, rubrics):
        require(turn["turn_id"] == rubric["turn_id"], "script and Rubric order differ")
        main = next((p for p in mains if p["focus_turn_id"] == turn["turn_id"]), None)
        if main is None:
            packets.append({"turn_id": turn["turn_id"], "triggered": False, "all_criteria": criteria_for(rubric), "judge_criteria": [], "responses": []})
            continue
        # Responses are attributed by their start, including the consumed portion
        # after the next user's interruption onset until the old queue was stopped.
        next_start = min([p["start_frame"] for p in mains if p["start_frame"] > main["start_frame"]] + [timeline["frame"]+1])
        matching = [r for r in timeline["responses"] if main["start_frame"] <= r["start_frame"] < next_start]
        measured = measure_turn(turn, main, matching, timeline, stereo)
        criteria = criteria_for(rubric)
        behavior = [c for c in criteria if c["kind"] == "behavior"]
        # Bind the existing generated contract; do not infer semantic meaning
        # from keywords. Unsupported layouts require an explicit implementation.
        require(not behavior or (measured["type"] == "T02" and len(behavior) == 1) or
                (measured["type"] == "T03" and len(behavior) == 2), "unsupported Behavior binding; discuss before changing Rubric")
        judge_criteria = [c for c in criteria if c["kind"] == "semantic" or
                          (measured["type"] == "T03" and c["criterion_id"].endswith("B02"))]
        history = [{"turn_id": p["focus_turn_id"], "text": p["text"]} for p in mains if p["start_frame"] <= main["start_frame"]]
        packet = {"turn_id": turn["turn_id"], "triggered": True, "sample_rate": 16000,
                  "clock": "offline_synthetic_pcm", "disclosed_user_history": history,
                  "responses": [response_evidence(r) for r in matching], "judge_criteria": judge_criteria,
                  "behavior_measurement": measured}
        clip_end = max([main["start_frame"] + main["frames"], *[r["start_frame"]+r["played_frames"] for r in matching]])
        clip_path = output/"audio"/f"{turn['turn_id']}.wav"
        clip_path.parent.mkdir(exist_ok=True)
        with wave.open(str(clip_path), "wb") as writer:
            writer.setnchannels(2); writer.setsampwidth(2); writer.setframerate(16000)
            writer.writeframes(stereo[main["start_frame"]:clip_end].tobytes())
        write_json(output/"packets"/f"{turn['turn_id']}.json", packet)
        packets.append({**packet, "all_criteria": criteria, "audio_path": str(clip_path.relative_to(output)),
                        "clip_start_frame": main["start_frame"], "clip_end_frame": clip_end})
        measurements[turn["turn_id"]] = measured
    sides = []
    for p in timeline["placements"]:
        if p.get("kind") != "side_speech":
            continue
        begin, end = p["start_frame"], p["start_frame"]+p["frames"]
        events = [e for e in timeline["events"] if begin <= e["frame"] <= end+9600 and e["kind"] == "assistant_listener_decision"]
        pcm = stereo[begin:end].astype(np.int32)
        sides.append({"turn_id": p["focus_turn_id"], "text": p["text"], "start_frame": begin, "end_frame": end,
                      "both_channels_active_frames": int(np.count_nonzero((np.abs(pcm[:,0]) >= 104) & (np.abs(pcm[:,1]) >= 104))),
                      "listener_decisions": events, "scored": False,
                      "reason": "Original U05 Behavior list is empty; retained as execution evidence only."})
    return packets, measurements, response_lookup, sides


def score_run(directory, output, client, *, workers=1):
    directory, output = directory.resolve(), output.resolve()
    require(not output.exists() and not output.is_relative_to(directory), "use a new output directory outside the source rollout")
    original = file_hashes(directory)
    integrity = verify(directory)  # Read-only, unlike verify_synthetic's CLI.
    output.mkdir(parents=True)
    (output/"packets").mkdir(); (output/"calls").mkdir()
    prompt = PROMPT.read_text()
    (output/"prompt.md").write_text(prompt)
    for name in ["actor_rubric.json", "episode_script.json"]:
        shutil.copyfile(directory/"user/environment/frozen"/name, output/name)
    manifest = {"schema": "duplex-runtime.actual-scoring.v1", "case_id": read(directory/"manifest.json")["case_id"],
                "source_run": str(directory), "source_files_sha256": original,
                "source_integrity": integrity, "judge_model": client.model,
                "prompt_sha256": digest(output/"prompt.md"), "created_utc": datetime.now(timezone.utc).isoformat(),
                "scoring_policy": "equal_weight_binary_per_original_criterion;NA_and_insufficient_excluded_with_coverage_reported",
                "response_attribution": "assistant_start_between_primary_user_onsets;consumed_prefix_including_stop_overlap",
                "independent_posthoc_judge": True, "native_realtime_latency_measured": False,
                "behavior_binding": "T02_single_wait;T03_first_stop_second_semantic_recovery",
                "side_speech_scored": False, "workers": workers}
    write_json(output/"manifest.json", manifest)
    packets, measurements, response_lookup, sides = prepare_packets(directory, output)
    write_json(output/"measurements.json", {"turns": measurements, "side_speech_diagnostics": sides})
    def judge(packet):
        if not packet["triggered"]:
            return {"turn_id": packet["turn_id"], "criteria": [{**c, "status": "not_triggered", "reward": None,
                    "reason": "User turn was not delivered.", "evidence": [], "method": "event_presence"} for c in packet["all_criteria"]]}
        context = read(output/"packets"/f"{packet['turn_id']}.json")
        folder = output/"calls"/packet["turn_id"]
        folder.mkdir(); write_json(folder/"input.json", context)
        started = time.monotonic()
        metadata = {"requested_model": client.model, "new_model_request": True, "input_sha256": json_hash(context)}
        try:
            raw, meta = client.complete(prompt, context, max_tokens=6000, response_format={"type": "json_object"})
            (folder/"raw_response.txt").write_text(raw)
            metadata["model_text_sha256"] = digest(folder/"raw_response.txt")
            metadata["response_sha256_scope"] = "provider_HTTP_envelope_not_extracted_text"
            metadata.update({k:v for k,v in meta.items() if k in {"model", "provider", "fallback", "timestamp", "response_sha256", "usage"}})
            metadata["new_model_request"] = meta.get("new_model_request", True)
            if meta.get("reused_from"):
                metadata["reused_from"] = meta["reused_from"]
            require(not meta.get("fallback", False), "Judge fallback is not authorized")
            rows = validate_judgment(json.loads(raw), context, response_lookup)
            combined = combine_scores(packet["all_criteria"], packet["behavior_measurement"], rows)
            result = {"turn_id": packet["turn_id"], "criteria": combined,
                      "responses": packet["responses"], "audio_path": packet["audio_path"],
                      "clip_start_frame": packet["clip_start_frame"], "clip_end_frame": packet["clip_end_frame"],
                      "measurement": packet["behavior_measurement"]}
            write_json(output/f"score-{packet['turn_id']}.json", result)
            print(json.dumps({"turn": packet["turn_id"], "phase": "scored", "summary": aggregate(combined)}, ensure_ascii=False), flush=True)
            return result
        except Exception as exc:
            write_json(folder/"failure.json", {"error_type": type(exc).__name__, "reason": str(exc)})
            raise
        finally:
            metadata["elapsed_seconds"] = time.monotonic()-started
            write_json(folder/"metadata.json", metadata)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(judge, packets))
        require(file_hashes(directory) == original, "source rollout changed while scoring")
        summary = {"schema": manifest["schema"], "status": "completed", "case_id": manifest["case_id"],
                   "summary": aggregate([c for r in results for c in r["criteria"]]), "turns": results,
                   "side_speech_diagnostics": sides, "source_inputs_unchanged": True,
                   "judge_calls": sum(p["triggered"] for p in packets), "native_realtime_latency_measured": False}
        write_json(output/"scores.json", summary)
        write_json(output/"artifact-hashes.json", {"schema": "duplex-runtime.score-files.v1", "files": file_hashes(output)})
        return summary
    except Exception as exc:
        write_json(output/"failure.json", {"status": "failed", "error_type": type(exc).__name__, "reason": str(exc),
                   "source_inputs_unchanged": file_hashes(directory) == original})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--judge-model", default="openai/gpt-5.6-sol")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    require(1 <= args.workers <= 4, "Judge workers must be between 1 and 4")
    _, client = configured_clients(args.credentials_file, args.profile, args.judge_model, args.judge_model)
    client.fallback_enabled = False
    summary = score_run(args.run_dir, args.output_dir, client, workers=args.workers)
    print(json.dumps({"status": summary["status"], "summary": summary["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
