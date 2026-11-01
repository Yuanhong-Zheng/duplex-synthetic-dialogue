"""A current-turn user policy, explicit playback observations and guarded progress."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time
from typing import Protocol
import uuid

from duplex_runtime.environment import PreparationError, decode_object, sha256
from duplex_runtime.prepare import verify_prepared_session


PROMPTS = Path(__file__).parent / "prompts"
ACTIONS = {"deliver", "correct", "clarify", "unknown", "wait", "end", "backchannel"}
LOCAL_ACTIONS = {"correct", "clarify", "unknown"}
CURRENT_FIELDS = ("turn_id", "semantic_goal", "act_ref", "timing_refs", "user_utterance", "pause_cue", "interrupt_anchor")


class SimulatorError(ValueError):
    pass


class PolicyRejected(SimulatorError):
    """The candidate was archived without updating dialogue or progress."""


class GuardFormatExhausted(SimulatorError):
    pass


class ModelClient(Protocol):
    def complete(self, system_prompt: str, input_context: object, max_tokens: int = 4000,
                 response_format: dict | None = None) -> tuple[str, dict]: ...


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def json_hash(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


@dataclass(frozen=True)
class PlaybackObservation:
    observation_id: str
    source: str
    text: str
    played_chars: int
    playback_state: str

    def __post_init__(self):
        if not isinstance(self.observation_id, str) or not self.observation_id.strip():
            raise SimulatorError("observation_id must be nonempty")
        if self.source not in {"fixture", "actor_playback", "synthetic_pcm_timeline"}:
            raise SimulatorError("observation source must identify fixture, actor playback or synthetic PCM timeline")
        if not isinstance(self.text, str):
            raise SimulatorError("observation text must be a string")
        if type(self.played_chars) is not int or not 0 <= self.played_chars <= len(self.text):
            raise SimulatorError("played_chars must lie within the provided text")
        if self.playback_state not in {"idle", "speaking", "finished"}:
            raise SimulatorError("invalid playback_state")
        if self.playback_state == "idle" and (self.text or self.played_chars):
            raise SimulatorError("an idle observation cannot contain assistant speech")

    @classmethod
    def from_dict(cls, value: dict) -> PlaybackObservation:
        expected = {"observation_id", "source", "text", "played_chars", "playback_state"}
        if not isinstance(value, dict) or set(value) != expected:
            raise SimulatorError("observation must contain exactly its five defined fields")
        return cls(**value)

    def raw(self) -> dict:
        return {"observation_id": self.observation_id, "source": self.source, "text": self.text,
                "played_chars": self.played_chars, "playback_state": self.playback_state}

    def visible(self) -> dict:
        return {"observation_id": self.observation_id, "source": self.source,
                "heard_text": self.text[:self.played_chars], "playback_state": self.playback_state}


class UserSimulator:
    """Single-writer policy session; TTS and actual playback remain separate adapters."""

    def __init__(self, prepared_dir: Path, output_dir: Path, client: ModelClient, guard: ModelClient,
                 *, max_local_replies: int = 5, _seed: dict | None = None, max_candidate_repairs: int = 0):
        verify_prepared_session(prepared_dir)
        prepared_dir = prepared_dir.resolve()
        output_dir = output_dir.resolve()
        if output_dir.exists() or output_dir.is_relative_to(prepared_dir):
            raise SimulatorError("use a new output directory outside the prepared session")
        if type(max_local_replies) is not int or max_local_replies < 1:
            raise SimulatorError("max_local_replies must be a positive integer")
        output_dir.mkdir(parents=True)
        shutil.copytree(prepared_dir, output_dir / "environment")
        (output_dir / "calls").mkdir()
        (output_dir / "prompts").mkdir()
        for name in ("user_policy.md", "user_guard.md"):
            shutil.copyfile(PROMPTS / name, output_dir / "prompts" / name)
        self.directory = output_dir
        self.client, self.guard = client, guard
        self.max_local_replies = max_local_replies
        if type(max_candidate_repairs) is not int or not 0 <= max_candidate_repairs <= 2:
            raise SimulatorError("candidate repairs must be bounded at two")
        self.max_candidate_repairs = max_candidate_repairs
        self.repair_prompt = (PROMPTS/"user_repair.md").read_text()
        if max_candidate_repairs:
            shutil.copyfile(PROMPTS/"user_repair.md",output_dir/"prompts/user_repair.md")
        self._artifacts = {stage: decode_object((output_dir / "environment" / item["path"]).read_bytes(), stage)
                           for stage, item in decode_object((prepared_dir / "session.json").read_bytes(), "session")["inputs"].items()}
        self._turns = self._artifacts["episode_script"]["episode_turns"]
        self._cursor = 0
        self._history = []
        self._last_main_id = None
        self._local_replies = 0
        self._ended = False
        self._events = []
        self._processed = {}
        self._calls = 0
        self._replayed_generations = 0
        self._recorded_prefix_calls = 0
        self._recorded_call_ids = set()
        if _seed:
            self._cursor = _seed["next_turn_index"]
            self._history = copy.deepcopy(_seed["history"])
            self._last_main_id = _seed["last_main_turn_id"]
            self._local_replies = _seed["local_replies"]
            self._ended = _seed["ended"]
            write_json(output_dir / "seed_state.json", _seed)
        prepared = decode_object((prepared_dir / "session.json").read_bytes(), "session")
        self.manifest = {
            "schema": "duplex-runtime.user-simulator.v1", "simulator_id": "user-sim-" + uuid.uuid4().hex,
            "case_id": prepared["case_id"], "prepared_session_id": prepared["session_id"],
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "environment_session_sha256": sha256((output_dir / "environment/session.json").read_bytes()),
            "policy": "frozen_mainline_with_local_repair", "max_local_replies": max_local_replies,
            "planned_backchannel_budget": sum(len(t.get("user_backchannels", [])) for t in self._turns),
            "prompt_sha256": {name: sha256((output_dir / "prompts" / name).read_bytes()) for name in ("user_policy.md", "user_guard.md")},
            "actor_connected": False, "audio_rendered": False,
            "seed_parent": _seed.get("parent_directory") if _seed else None,
        }
        if max_candidate_repairs:
            self.manifest["max_candidate_repairs"] = max_candidate_repairs
            self.manifest["prompt_sha256"]["user_repair.md"] = sha256((output_dir/"prompts/user_repair.md").read_bytes())
        write_json(output_dir / "manifest.json", self.manifest)
        self._persist_state()

    @property
    def next_turn_id(self) -> str | None:
        return self._turns[self._cursor]["turn_id"] if self._cursor < len(self._turns) else None

    @property
    def events(self) -> list[dict]:
        return copy.deepcopy(self._events)

    @property
    def model_calls(self) -> int:
        return self._calls - self._replayed_generations - self._recorded_prefix_calls

    def state_snapshot(self) -> dict:
        return {"next_turn_index": self._cursor, "next_turn_id": self.next_turn_id,
                "last_main_turn_id": self._last_main_id, "local_replies": self._local_replies,
                "ended": self._ended, "history": copy.deepcopy(self._history)}

    def fork_for_verification(self, output_dir: Path) -> UserSimulator:
        seed = {**self.state_snapshot(), "parent_directory": str(self.directory),
                "parent_events_sha256": sha256((self.directory / "events.jsonl").read_bytes())}
        return UserSimulator(self.directory / "environment", output_dir, self.client, self.guard,
                             max_local_replies=self.max_local_replies, _seed=seed, max_candidate_repairs=self.max_candidate_repairs)

    def _persist_state(self):
        write_json(self.directory / "state.json", {**self.state_snapshot(), "accepted_events": len(self._events), "model_calls": self.model_calls,
                    "call_records": self._calls, "replayed_generations": self._replayed_generations})
        temporary = self.directory / "events.jsonl.tmp"
        temporary.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self._events), encoding="utf-8")
        temporary.replace(self.directory / "events.jsonl")

    def visible_context(self, observation: PlaybackObservation | None) -> dict:
        pending = self._turns[self._cursor] if self._cursor < len(self._turns) else None
        return {
            "user_goal": copy.deepcopy(self._artifacts["scene_task"]["task_instance"]["goal"]),
            "user_seed": copy.deepcopy(self._artifacts["user_voice"]["user_seed"]),
            "constraints": [item["text"] for item in self._artifacts["situation_knowledge"]["constraints"]],
            "safety_limits": [item["text"] for item in self._artifacts["situation_knowledge"]["safety_limits"]],
            "pending_turn": {key: copy.deepcopy(pending[key]) for key in CURRENT_FIELDS if key in pending} if pending else None,
            "last_delivered_turn_id": self._last_main_id,
            "history": copy.deepcopy(self._history),
            "observation": observation.visible() if observation else {"source": "opening", "heard_text": "", "playback_state": "idle"},
        }

    def _call(self, role: str, client: ModelClient, context: dict, *, prompt_suffix="") -> tuple[dict, int]:
        self._calls += 1
        number = self._calls
        folder = self.directory / "calls" / f"{number:04d}-{role}"
        folder.mkdir()
        prompt = (self.directory / "prompts" / ("user_policy.md" if role == "generation" else "user_guard.md")).read_text()
        prompt += prompt_suffix
        (folder / "prompt.md").write_text(prompt)
        write_json(folder / "input.json", context)
        started = time.monotonic()
        try:
            raw, metadata = client.complete(prompt, context, max_tokens=1400, response_format={"type": "json_object"})
            (folder / "raw_response.txt").write_text(raw)
            safe_metadata = {key: metadata[key] for key in ("model", "provider", "fallback", "response_sha256", "timestamp") if key in metadata}
            new_request = metadata.get("new_model_request", True)
            if not new_request:
                self._recorded_prefix_calls += 1
                self._recorded_call_ids.add(number)
            write_json(folder / "metadata.json", {**safe_metadata, "role": role, "input_sha256": json_hash(context), "text_sha256": sha256(raw.encode()),
                       "new_model_request": new_request, "reused_from": metadata.get("reused_from") if not new_request else None,
                       "elapsed_seconds": time.monotonic()-started, "elapsed_includes_network_request": new_request})
            return decode_object(raw.encode(), f"{role} response"), number
        except Exception as exc:
            write_json(folder / "failure.json", {"error_type": type(exc).__name__, "dialogue_progress_changed": False})
            self._persist_state()
            raise

    def _reuse_generation(self, source: Path, context: dict) -> tuple[dict, int]:
        """Recheck exactly the recorded candidate; never generate a replacement."""
        source = source.resolve()
        raw = (source / "raw_response.txt").read_bytes()
        original_input = decode_object((source / "input.json").read_bytes(), "recorded generation input")
        metadata = decode_object((source / "metadata.json").read_bytes(), "recorded generation metadata")
        if original_input != context or metadata.get("input_sha256") != json_hash(context):
            raise SimulatorError("recorded candidate context differs from the current observation or state")
        if metadata.get("text_sha256") != sha256(raw):
            raise SimulatorError("recorded candidate response changed")
        if (source / "prompt.md").read_bytes() != (self.directory / "prompts/user_policy.md").read_bytes():
            raise SimulatorError("user policy differs from the recorded generation")
        candidate = decode_object(raw, "recorded candidate")
        self._calls += 1
        self._replayed_generations += 1
        number = self._calls
        folder = self.directory / "calls" / f"{number:04d}-generation"
        folder.mkdir()
        for name in ("prompt.md", "input.json", "raw_response.txt"):
            (folder / name).write_bytes((source / name).read_bytes())
        safe_metadata = {key: metadata[key] for key in ("model", "provider", "fallback", "response_sha256", "timestamp") if key in metadata}
        write_json(folder / "metadata.json", {**safe_metadata, "role": "generation", "input_sha256": json_hash(context),
                    "text_sha256": sha256(raw), "new_model_request": False, "reused_from": str(source)})
        return candidate, number

    def _validate_candidate(self, candidate: dict, view: dict):
        if set(candidate) != {"action", "utterance", "evidence"} or any(not isinstance(candidate[key], str) for key in candidate):
            raise PolicyRejected("candidate must contain exactly three string fields")
        action, text, evidence = (candidate[key] for key in ("action", "utterance", "evidence"))
        if view.get("backchannel_plan") is not None and action not in {"backchannel", "wait"}:
            raise PolicyRejected("a backchannel opportunity accepts only backchannel or silent wait")
        if view.get("backchannel_plan") is not None:
            for event in self._events:
                prior_plan = event.get("backchannel_plan") or (event.get("dispatch") or {}).get("backchannel_plan")
                if event["focus_turn_id"] == self._last_main_id and prior_plan == view["backchannel_plan"]:
                    raise PolicyRejected("backchannel opportunity was already decided")
        if action not in ACTIONS:
            raise PolicyRejected("unknown user action")
        if self._cursor == 0 and action != "deliver":
            raise PolicyRejected("the opening must deliver the first main turn")
        if (action == "wait" and text != "") or (action != "wait" and not text.strip()):
            raise PolicyRejected("wait must be silent; other actions need an utterance")
        pending, observation = view["pending_turn"], view["observation"]
        heard = observation["heard_text"]
        if evidence and evidence not in heard:
            raise PolicyRejected("candidate evidence is not in the heard prefix")
        if action == "deliver":
            if not pending:
                raise PolicyRejected("all main turns have already been delivered")
            if "T03" in pending["timing_refs"]:
                if observation["playback_state"] != "speaking" or not heard or not evidence:
                    raise PolicyRejected("T03 requires an ongoing observed playback and quoted evidence")
            elif observation["playback_state"] != "finished" and not (self._cursor == 0 and observation["playback_state"] == "idle"):
                raise PolicyRejected("T01 requires the opening or a finished playback")
            if pending.get("pause_cue") and text.count(pending["pause_cue"]["after_text"]) != 1:
                raise PolicyRejected("the frozen pause anchor must occur exactly once")
        elif action == "backchannel":
            plan = view.get("backchannel_plan")
            if not plan or observation["playback_state"] != "speaking" or not heard or not evidence:
                raise PolicyRejected("backchannel requires its frozen plan and an ongoing heard response")
            owner = next((t for t in self._turns if t["turn_id"] == self._last_main_id), {})
            if plan not in owner.get("user_backchannels", []):
                raise PolicyRejected("backchannel plan does not belong to the last frozen main turn")
            if text != plan["text"]:
                raise PolicyRejected("backchannel must preserve its Stage-3.5 utterance")
            budget = sum(len(t.get("user_backchannels", [])) for t in self._turns)
            if sum(e["action"] == "backchannel" for e in self._events) >= budget:
                raise PolicyRejected("backchannel budget exhausted")
        elif action in LOCAL_ACTIONS:
            if not self._last_main_id or not heard or not evidence:
                raise PolicyRejected("local replies need a prior main turn and heard evidence")
            if self._local_replies >= self.max_local_replies:
                raise PolicyRejected("local reply limit reached; no turn will be skipped")
        elif action == "end":
            if pending or observation["playback_state"] != "finished" or not heard:
                raise PolicyRejected("end requires all main turns and the final observed response")

    def step(self, observation: PlaybackObservation | None = None, *, recorded_generation: Path | None = None, backchannel_plan: dict | None = None) -> dict:
        verify_prepared_session(self.directory / "environment")
        if sha256((self.directory / "environment/session.json").read_bytes()) != self.manifest["environment_session_sha256"]:
            raise SimulatorError("prepared environment manifest changed")
        for name, expected in self.manifest["prompt_sha256"].items():
            if sha256((self.directory / "prompts" / name).read_bytes()) != expected:
                raise SimulatorError("a frozen simulator prompt changed")
        identity = observation.observation_id if observation else "__opening__"
        fingerprint = json_hash({"observation": observation.raw() if observation else None, "backchannel_plan": backchannel_plan}) if backchannel_plan is not None else json_hash(observation.raw() if observation else None)
        if identity in self._processed:
            old_hash, event = self._processed[identity]
            if old_hash != fingerprint:
                raise SimulatorError("an observation ID cannot be reused with changed content")
            return {**copy.deepcopy(event), "cached": True}
        if self._ended:
            raise SimulatorError("the simulator has ended")
        if observation is None and self._cursor != 0:
            raise SimulatorError("an assistant observation is required after the opening")
        view = self.visible_context(observation)
        if backchannel_plan is not None:
            view["pending_turn"] = None
            view["backchannel_plan"] = copy.deepcopy(backchannel_plan)
        feedback = None
        repair_history = []
        for attempt in range(self.max_candidate_repairs+1):
            candidate, result = None, None
            generation_call = self._calls+1
            try:
                request = {**copy.deepcopy(view), **({"repair_feedback":feedback} if feedback else {})}
                candidate, generation_call = (self._reuse_generation(recorded_generation, view) if recorded_generation and attempt==0
                    else self._call("generation",self.client,request,prompt_suffix="\n"+self.repair_prompt if feedback else ""))
                self._validate_candidate(candidate,view)
                guard_feedback = None
                for guard_attempt in range(self.max_candidate_repairs+1):
                    try:
                        guard_request = {"view":view,"candidate":candidate,**({"repair_feedback":guard_feedback}if guard_feedback else {})}
                        result, guard_call = self._call("guard",self.guard,guard_request,
                            prompt_suffix="\n"+self.repair_prompt if guard_feedback else "")
                        if set(result) != {"decision","findings"} or result["decision"] not in {"pass","fail"} or not isinstance(result["findings"],list):
                            raise PolicyRejected("invalid semantic guard response")
                        break
                    except (PolicyRejected,PreparationError)as exc:
                        if guard_attempt >= self.max_candidate_repairs:
                            raise GuardFormatExhausted("semantic Guard format repair limit reached") from exc
                        guard_feedback = {"errors":[str(exc)],"rejected_candidate":result}
                        write_json(self.directory/f"guard-format-rejection-{self._calls:04d}.json",guard_feedback)
                if result["decision"] != "pass" or result["findings"]:
                    raise PolicyRejected("semantic guard rejected candidate; see archived guard response")
                break
            except (PolicyRejected,PreparationError)as exc:
                feedback = {"errors":[str(exc)],"rejected_candidate":candidate,
                            "guard_findings":result.get("findings",[]) if isinstance(result,dict) else []}
                repair_history.append({"attempt":attempt+1,"generation_call":generation_call,"feedback":feedback})
                write_json(self.directory/f"rejection-{generation_call:04d}.json",{"error_type":type(exc).__name__,"reason":str(exc),
                    "candidate":candidate,"progress_changed":False,"repair_attempt":attempt})
                self._persist_state()
                if recorded_generation or attempt >= self.max_candidate_repairs:raise
        if repair_history:
            write_json(self.directory/f"candidate-repair-{generation_call:04d}.json",{"attempts":repair_history,"final_status":"passed",
                "accepted_generation_call":generation_call,"original_guard_rules_used":True,"max_repairs":self.max_candidate_repairs})
        action = candidate["action"]
        pending = self._turns[self._cursor] if self._cursor < len(self._turns) else None
        focus = pending["turn_id"] if action == "deliver" else self._last_main_id
        before = self.next_turn_id
        dispatch = None
        if action == "deliver":
            dispatch = {"execution": "planned_only", "timing_refs": copy.deepcopy(pending["timing_refs"]),
                        "pause_cue": copy.deepcopy(pending.get("pause_cue")),
                        "interrupt_anchor": pending.get("interrupt_anchor"),
                        "side_speech": copy.deepcopy(pending.get("side_speech", []))}
            self._last_main_id = focus
            self._cursor += 1
            self._local_replies = 0
        elif action in LOCAL_ACTIONS:
            self._local_replies += 1
            dispatch = {"execution": "planned_only", "timing_refs": ["T03" if view["observation"]["playback_state"] == "speaking" else "T01"], "pause_cue": None, "side_speech": []}
        elif action == "backchannel":
            dispatch = {"execution": "planned_only", "timing_refs": [], "pause_cue": None, "side_speech": [], "backchannel_plan": copy.deepcopy(backchannel_plan)}
        elif action == "end":
            self._ended = True
        if observation and view["observation"]["heard_text"]:
            self._history.append({"role": "assistant", "kind": "heard_prefix", **view["observation"]})
        if candidate["utterance"]:
            self._history.append({"role": "user", "turn_id": focus, "kind": action, "content": candidate["utterance"]})
        event = {
            "schema": "duplex-runtime.user-event.v1", "sequence": len(self._events) + 1,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "observation_id": identity,
            "observation_sha256": fingerprint, "observation": view["observation"],
            "action": action, "focus_turn_id": focus, "utterance": candidate["utterance"], "evidence": candidate["evidence"],
            "pending_before": before, "pending_after": self.next_turn_id, "dispatch": dispatch,
            "generation_call": generation_call, "guard_call": guard_call,
            "generation_origin": "recorded_real_output" if recorded_generation or generation_call in self._recorded_call_ids else "new_model_request",
            "semantic_guard": result, "actor_connected": False, "audio_rendered": False,
        }
        if backchannel_plan is not None:
            event["backchannel_plan"] = copy.deepcopy(backchannel_plan)
        self._events.append(event)
        self._processed[identity] = (fingerprint, copy.deepcopy(event))
        if observation:
            write_json(self.directory / f"observation-{event['sequence']:04d}.json", observation.raw())
        self._persist_state()
        return copy.deepcopy(event)
