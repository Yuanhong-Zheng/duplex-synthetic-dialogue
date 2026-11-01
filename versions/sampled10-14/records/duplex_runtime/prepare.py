"""Prepare and verify self-contained runtime inputs; no model or audio calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import uuid

from duplex_runtime.environment import (
    PreparationError, audit_admission, decode_object, environment_statistics, load_environment, sha256,
)
from validate_offline_package import STAGES, validate_case


SESSION_SCHEMA = "duplex-runtime.prepared-session.v1"
PREPARATION_EXECUTION = {"models_called": False, "actor_connected": False, "audio_rendered": False, "rollout_started": False}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_prepared_session(directory: Path) -> dict:
    directory = directory.resolve()
    session = decode_object((directory / "session.json").read_bytes(), "session")
    if session.get("schema") != SESSION_SCHEMA or session.get("status") != "prepared":
        raise PreparationError("unsupported prepared session schema or state")
    if session.get("execution") != PREPARATION_EXECUTION:
        raise PreparationError("a prepared session cannot claim model, actor, audio or rollout execution")
    if set(session.get("inputs", {})) != set(STAGES):
        raise PreparationError("session must reference all six frozen artifacts")
    artifacts = {}
    for stage in STAGES:
        item = session["inputs"][stage]
        if item.get("path") != f"frozen/{stage}.json":
            raise PreparationError(f"invalid snapshot path: {stage}")
        raw = (directory / item["path"]).read_bytes()
        if item.get("sha256") != sha256(raw) or item.get("bytes") != len(raw):
            raise PreparationError(f"snapshot hash or size mismatch: {stage}")
        artifacts[stage] = decode_object(raw, stage)
    errors = [item for item in validate_case(directory / "frozen")
              if not item.startswith("audit release blocker:") and item != "audit release_decision is not pass"]
    if errors:
        raise PreparationError("invalid prepared environment: " + "; ".join(errors))
    admission = session["admission"]
    accepted_ids = tuple(item["finding_id"] for item in admission["accepted_minor_findings"])
    expected_admission = audit_admission(artifacts["environment_audit"], accepted_ids, admission["acceptance_reason"])
    if admission != expected_admission:
        raise PreparationError("session admission differs from the frozen audit")
    if session["statistics"] != environment_statistics(artifacts):
        raise PreparationError("session statistics differ from the frozen inputs")
    provenance_paths = [item["path"] for item in session["provenance_files"]]
    if "provenance/source-manifest.json" not in provenance_paths or len(provenance_paths) != len(set(provenance_paths)):
        raise PreparationError("session must contain a unique source manifest snapshot")
    for item in session["provenance_files"]:
        if item["path"] not in {"provenance/source-manifest.json", "provenance/audit-input.json"}:
            raise PreparationError("invalid provenance snapshot path")
        raw = (directory / item["path"]).read_bytes()
        if sha256(raw) != item["sha256"]:
            raise PreparationError("provenance snapshot hash mismatch")
    source_manifest = decode_object((directory / "provenance/source-manifest.json").read_bytes(), "source manifest")
    if session["case_id"] not in source_manifest.get("case_ids", []):
        raise PreparationError("prepared case is not declared in the source manifest")
    if session["source_run_id"] != source_manifest.get("run_id"):
        raise PreparationError("prepared source run ID does not match provenance")
    if "provenance/audit-input.json" in provenance_paths:
        audit_input = decode_object((directory / "provenance/audit-input.json").read_bytes(), "audit input")
        if audit_input.get("case_id") != session["case_id"]:
            raise PreparationError("prepared audit input case ID does not match")
        if any(audit_input.get(stage) != artifacts[stage] for stage in STAGES if stage != "environment_audit"):
            raise PreparationError("prepared artifacts differ from the audited input")
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines() if line.strip()]
    if len(events) != 1 or events[0] != {
        "schema": "duplex-runtime.event.v1", "sequence": 1,
        "session_id": session["session_id"], "case_id": session["case_id"],
        "recorded_at_utc": session["created_utc"], "type": "session.prepared",
        "payload": {"turn_count": session["statistics"]["turn_count"], "admission_status": admission["status"]},
    }:
        raise PreparationError("prepared session must contain exactly its preparation event")
    return {
        "status": "passed", "case_id": session["case_id"], "session_id": session["session_id"],
        "frozen_artifacts_verified": len(STAGES), "statistics": session["statistics"],
        "admission_status": admission["status"], "original_release_decision": admission["original_release_decision"],
        "event_count": len(events), "model_requests": 0, "actor_responses": 0, "audio_files": 0,
    }


def prepare_session(
    audit_run: Path, case_id: str, output_dir: Path, *, accepted_ids: tuple[str, ...] = (), acceptance_reason: str = "",
) -> dict:
    environment = load_environment(audit_run, case_id, accepted_ids=accepted_ids, acceptance_reason=acceptance_reason)
    output = output_dir.expanduser().resolve()
    if output.is_relative_to(environment.run_dir):
        raise PreparationError("runtime output must be outside the frozen source run")
    if output.exists():
        raise PreparationError(f"output already exists; choose a new session directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = {stage: environment.artifact(stage) for stage in STAGES}
    created = datetime.now(timezone.utc).isoformat()
    session_id = "runtime-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=output.parent) as temporary:
        target = Path(temporary)
        (target / "frozen").mkdir()
        (target / "provenance").mkdir()
        inputs = {}
        for stage in STAGES:
            source = environment.run_dir / case_id / f"{stage}.json"
            raw = environment.source_bytes[source]
            relative = f"frozen/{stage}.json"
            (target / relative).write_bytes(raw)
            inputs[stage] = {"path": relative, "sha256": sha256(raw), "bytes": len(raw), "source_path": str(source)}
        provenance = []
        for source, relative in [
            (environment.run_dir / "manifest.json", "provenance/source-manifest.json"),
            (environment.run_dir / case_id / "input.json", "provenance/audit-input.json"),
        ]:
            if source in environment.source_bytes:
                raw = environment.source_bytes[source]
                (target / relative).write_bytes(raw)
                provenance.append({"path": relative, "sha256": sha256(raw), "source_path": str(source)})
        source_manifest = decode_object(environment.source_bytes[environment.run_dir / "manifest.json"], "source manifest")
        session = {
            "schema": SESSION_SCHEMA, "session_id": session_id, "case_id": case_id,
            "status": "prepared", "created_utc": created,
            "source_run_id": source_manifest.get("run_id"), "source_run_path": str(environment.run_dir),
            "inputs": inputs, "provenance_files": provenance,
            "admission": environment.admission, "statistics": environment_statistics(artifacts),
            "execution": dict(PREPARATION_EXECUTION),
        }
        write_json(target / "session.json", session)
        event = {
            "schema": "duplex-runtime.event.v1", "sequence": 1,
            "session_id": session_id, "case_id": case_id, "recorded_at_utc": created,
            "type": "session.prepared",
            "payload": {"turn_count": session["statistics"]["turn_count"], "admission_status": session["admission"]["status"]},
        }
        (target / "events.jsonl").write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
        verification = verify_prepared_session(target)
        environment.assert_sources_unchanged()
        verification["original_sources_unchanged_at_preparation"] = True
        write_json(target / "verification.json", verification)
        if output.exists():
            raise PreparationError(f"output was created concurrently: {output}")
        target.rename(output)
    return {"output_dir": str(output), **verification}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare a complete Stage-5 case without executing it")
    prepare.add_argument("--audit-run", type=Path, required=True)
    prepare.add_argument("--case", required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--accept-minor-finding", action="append", default=[])
    prepare.add_argument("--acceptance-reason", default="")
    verify = commands.add_parser("verify", help="verify a prepared session, including after moving it")
    verify.add_argument("--session-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare_session(args.audit_run, args.case, args.output_dir,
                                     accepted_ids=tuple(args.accept_minor_finding), acceptance_reason=args.acceptance_reason)
        else:
            result = verify_prepared_session(args.session_dir)
    except (PreparationError, OSError, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, f"runtime preparation failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
