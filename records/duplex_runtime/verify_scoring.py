"""Verify score provenance, original criteria, model quotes and consumed audio clips."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from duplex_runtime.scoring import (aggregate, combine_scores, criteria_for, file_hashes,
                                    prepare_packets, validate_judgment)
from duplex_runtime.speech import digest
from duplex_runtime.user_simulator import json_hash, write_json
from duplex_runtime.verify_synthetic import verify, read, require


def verify_scores(directory, source=None):
    directory = directory.resolve()
    manifest, result = read(directory/"manifest.json"), read(directory/"scores.json")
    artifacts = read(directory/"artifact-hashes.json")["files"]
    require(all(digest(directory/name) == value for name, value in artifacts.items()), "saved scoring artifact changed")
    source = source.resolve() if source else Path(manifest["source_run"])
    require(file_hashes(source) == manifest["source_files_sha256"], "original rollout artifacts changed")
    require(verify(source) == manifest["source_integrity"], "source integrity differs")
    require(digest(directory/"prompt.md") == manifest["prompt_sha256"], "scoring prompt changed")
    for name in ["actor_rubric.json", "episode_script.json"]:
        require((directory/name).read_bytes() == (source/"user/environment/frozen"/name).read_bytes(), "frozen scoring artifact changed")
    require(result["status"] == "completed" and result["native_realtime_latency_measured"] is False, "scoring status/clock differs")
    criteria = [c for rubric in read(directory/"actor_rubric.json")["turn_rubrics"] for c in criteria_for(rubric)]
    actual = [c for turn in result["turns"] for c in turn["criteria"]]
    require([{k:c[k] for k in ["criterion_id", "kind", "text"]} for c in actual] == criteria, "original Rubric criteria were changed")
    require(result["summary"] == aggregate(actual), "score arithmetic differs")
    with tempfile.TemporaryDirectory() as scratch:
        temp = Path(scratch); (temp/"packets").mkdir()
        packets, measurements, responses, sides = prepare_packets(source, temp)
        require(read(directory/"measurements.json") == {"turns": measurements, "side_speech_diagnostics": sides}, "measurements differ from source PCM")
        require(result["side_speech_diagnostics"] == sides and all(s["scored"] is False for s in sides), "side speech was given an invented reward")
        quoted = 0
        for packet, scored in zip(packets, result["turns"]):
            require(packet["turn_id"] == scored["turn_id"], "turn scores reordered")
            if not packet["triggered"]:
                require(all(c["status"] == "not_triggered" and c["reward"] is None for c in scored["criteria"]), "untriggered criterion scored")
                continue
            context = read(directory/"packets"/f"{packet['turn_id']}.json")
            require(context == read(temp/"packets"/f"{packet['turn_id']}.json"), "Judge packet includes changed/unconsumed evidence")
            folder = directory/"calls"/packet["turn_id"]
            require(read(folder/"input.json") == context, "Judge call input differs")
            metadata = read(folder/"metadata.json")
            require(metadata["input_sha256"] == json_hash(context) and metadata["requested_model"] == manifest["judge_model"] and not metadata.get("fallback", False), "Judge model/provenance differs")
            raw = (folder/"raw_response.txt").read_text()
            # The shared client hashes the full HTTP envelope in response_sha256;
            # raw_response.txt contains the extracted model text, a different byte sequence.
            if metadata.get("model_text_sha256"):
                require(metadata["model_text_sha256"] == digest(folder/"raw_response.txt"), "Judge text changed")
            rows = validate_judgment(json.loads(raw), context, responses)
            combined = combine_scores(packet["all_criteria"], measurements[packet["turn_id"]], rows)
            require(combined == scored["criteria"] and read(directory/f"score-{packet['turn_id']}.json") == scored, "score differs from original model response/program measurement")
            require(digest(directory/scored["audio_path"]) == digest(temp/packet["audio_path"]), "evidence clip differs from original consumed stereo PCM")
            require(scored["clip_start_frame"] == packet["clip_start_frame"] and scored["clip_end_frame"] == packet["clip_end_frame"], "evidence clip origin differs")
            require(scored["responses"] == packet["responses"], "scored assistant text differs from consumed response")
            quoted += sum(len(c["evidence"]) for c in combined)
        require(len(packets) == len(result["turns"]), "missing scored turns")
    require(file_hashes(source) == manifest["source_files_sha256"], "verification changed source files")
    return {"schema": "duplex-runtime.scoring-integrity.v1", "status": "passed", "turns": len(result["turns"]),
            "original_criteria": len(criteria), "aligned_quotes": quoted,
            "source_files_unchanged": len(manifest["source_files_sha256"]),
            "summary": result["summary"], "checks": "original criterion identity, Judge provenance, consumed quotes, PCM reconstruction, arithmetic and side-speech exclusion"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-dir", type=Path, required=True)
    parser.add_argument("--source-run", type=Path)
    args = parser.parse_args()
    proof = verify_scores(args.score_dir, args.source_run)
    write_json(args.score_dir/"verification.json", proof)
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    main()
