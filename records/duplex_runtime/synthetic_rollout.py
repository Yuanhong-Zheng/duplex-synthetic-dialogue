"""GPT+TTS interaction on an OFFLINE PCM clock; never a native realtime benchmark.

Only consumed, word-aligned text enters either role. API generation is real but runs
while the synthetic sample clock is frozen. Cancellation discards queued PCM/text;
it does not claim that a provider request still in flight was cancelled.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import wave

import numpy as np

from duplex_runtime.alignment import rendered_spans, word_spans
from duplex_runtime.assistant import StreamingChatClient, SyntheticAssistant
from duplex_runtime.playback import PlaybackLedger, PlaybackReceipt
from duplex_runtime.prepare import verify_prepared_session
from duplex_runtime.speech import DashScopeTTSClient, SpeechError, SpeechRenderer, SpeechSettings, decode_wav, digest, encode_wav
from duplex_runtime.user_simulator import PlaybackObservation, UserSimulator, write_json
from duplex_runtime.verify_simulator import configured_clients


class RolloutBlocked(ValueError):
    pass


class SyntheticTimeline:
    """A deterministic PCM consumption clock, with no physical-device claims."""
    rate = 16000
    clock_id = "synthetic_pcm_16000"

    def __init__(self, directory: Path):
        self.directory = directory
        self.frame = 0
        self.events, self.observed, self.placements, self.responses = [], [], [], []
        self.active = None
        self.last_response = None

    def log(self, kind, **values):
        self.events.append({"sequence": len(self.events)+1, "frame": self.frame, "clock": self.clock_id, "kind": kind, **values})
        self.save()

    def save(self):
        write_json(self.directory / "timeline.json", {"schema": "duplex-runtime.synthetic-pcm-timeline.v1",
                   "sample_rate": self.rate, "frame": self.frame, "events": self.events,
                   "placements": self.placements, "observed_audio": self.observed, "responses": self.responses})

    def hear(self, speaker, identifier, text, frame):
        if text:
            self.observed.append({"speaker": speaker, "audio_id": identifier, "text": text, "through_frame": frame})

    def context(self):
        # Merge only consecutive fragments of the same clip. No future/audio-intent fields.
        history = []
        for item in sorted(self.observed, key=lambda x: x["through_frame"]):
            if history and all(history[-1][k] == item[k] for k in ("speaker", "audio_id")):
                history[-1]["text"] += item["text"]
                history[-1]["through_frame"] = item["through_frame"]
            else:
                history.append(dict(item))
        return {"observed_audio": history, "current_frame": self.frame, "clock": self.clock_id,
                "assistant_speaking": self.is_speaking}

    @property
    def is_speaking(self):
        return self.active is not None and self.frame < self.active["start_frame"] + self.active["audible_end_frame"]

    def start_response(self, identifier, text, pcm, spans, raw_path):
        if self.active:
            raise RolloutBlocked("a second assistant response cannot start over an unresolved queue")
        ledger = PlaybackLedger(identifier, self.rate)
        ledger.append_audio(pcm.tobytes()); ledger.append_transcript(text)
        for span in spans:
            ledger.add_alignment(span)
        if ledger.spans[-1].end_char != len(text):
            raise RolloutBlocked("assistant alignment does not cover full text")
        ledger.mark_response_done()
        active_samples = np.flatnonzero(np.abs(pcm.astype(np.int32)) >= 104)
        if not len(active_samples):
            raise RolloutBlocked("assistant output has no usable speech")
        self.active = {"id": identifier, "start_frame": self.frame, "ledger": ledger,
                       "raw_path": str(raw_path.relative_to(self.directory)), "pcm": pcm,
                       "audible_end_frame": int(active_samples[-1])+1, "published_chars": 0}
        self.last_response = self.active
        self.log("assistant_buffer_ready", response_id=identifier, generated_chars=len(text),
                 received_frames=len(pcm), api_generation_already_completed=True,
                 synthetic_playback_start_frame=self.frame)

    def advance(self, target):
        if type(target) is not int or target < self.frame:
            raise RolloutBlocked("synthetic clock cannot go backward")
        active = self.active
        if active:
            ledger = active["ledger"]
            through = min(ledger.received_frames, max(0, target-active["start_frame"]))
            receipt = PlaybackReceipt(f"{active['id']}-{through}", through, self.clock_id,
                                      (active["start_frame"]+through)*1_000_000_000//self.rate, "synthetic_pcm_timeline")
            ledger.confirm_playback(receipt)
            for span in ledger.spans:
                if active["published_chars"] < span.end_char <= ledger.played_chars:
                    self.hear("assistant", active["id"], ledger.transcript[active["published_chars"]:span.end_char], active["start_frame"]+span.end_frame)
                    active["published_chars"] = span.end_char
        self.frame = target
        if active and active["ledger"].state == "finished":
            self._finish("drained")

    def stop(self, reason):
        if not self.active:
            return
        active = self.active; ledger = active["ledger"]
        ledger.request_cancel()
        # This ack is local queued-buffer discard, not provider/server cancellation.
        ledger.acknowledge_cancel()
        ledger.confirm_stopped(PlaybackReceipt(f"{active['id']}-stop", ledger.played_frames, self.clock_id,
                                               self.frame*1_000_000_000//self.rate, "synthetic_pcm_timeline"))
        self.log("assistant_queue_discard", response_id=active["id"], reason=reason,
                 discarded_audio_frames=ledger.received_frames-ledger.played_frames,
                 discarded_text_chars=len(ledger.transcript)-ledger.played_chars,
                 provider_request_cancelled=False, native_stop_latency_measured=False)
        self._finish("queue_discarded")

    def _finish(self, outcome):
        active = self.active; ledger = active["ledger"]
        played_path = self.directory / "played" / f"{active['id']}.wav"
        played_path.parent.mkdir(exist_ok=True)
        # A received response may be discarded even before its first sample.
        played_path.write_bytes(encode_wav(np.frombuffer(ledger.played_pcm, dtype="<i2")))
        self.placements.append({"speaker": "assistant", "audio_id": active["id"], "start_frame": active["start_frame"],
                                "frames": ledger.played_frames, "path": str(played_path.relative_to(self.directory)),
                                "text": ledger.heard_text, "source_path": active["raw_path"], "outcome": outcome})
        self.responses.append({**ledger.snapshot(), "start_frame": active["start_frame"], "outcome": outcome,
                               "alignment": [asdict(s) for s in ledger.spans],
                               "receipts": [asdict(r) for r in ledger.receipts]})
        self.active = None
        self.log("assistant_playback_ended", response_id=active["id"], outcome=outcome,
                 audio_end_frame=active["start_frame"]+ledger.played_frames)

    def observation(self, identifier):
        if not self.last_response:
            raise RolloutBlocked("assistant observation requested before any response")
        ledger = self.last_response["ledger"]
        return PlaybackObservation(identifier, "synthetic_pcm_timeline", ledger.transcript, ledger.played_chars,
                                   "speaking" if self.is_speaking else "finished")

    def write_audio(self):
        length = max([self.frame, *[p["start_frame"]+p["frames"] for p in self.placements]])
        channels = np.zeros((length, 2), dtype=np.int32)
        for placement in self.placements:
            if not placement["frames"]:
                continue
            pcm = decode_wav((self.directory / placement["path"]).read_bytes())
            if len(pcm) != placement["frames"]:
                raise RolloutBlocked("placement PCM length changed")
            index = 0 if placement["speaker"] == "user" else 1
            start, end = placement["start_frame"], placement["start_frame"]+len(pcm)
            if np.any(channels[start:end, index]):
                raise RolloutBlocked("same-speaker audio overlaps unexpectedly")
            channels[start:end, index] += pcm
        if np.max(np.abs(channels), initial=0) > 32767:
            raise RolloutBlocked("unexpected PCM clipping")
        path = self.directory / "dialogue-stereo.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2); output.setsampwidth(2); output.setframerate(self.rate)
            output.writeframes(channels.astype("<i2").tobytes())
        return {"path": path.name, "sha256": digest(path), "frames": length, "sample_rate": self.rate,
                "channels": ["user_including_asides", "assistant"], "duration_seconds": length/self.rate,
                "background_audio_added": False, "alignment": "provider_word_timestamps_not_independent_ASR"}


class SyntheticRollout:
    """One frozen environment, one independent assistant, actual user+guard+TTS calls."""
    def __init__(self, prepared: Path, directory: Path, user_client, guard_client, actor_text_client, user_tts, actor_tts, actor_url_tts=None, *, progress_callback=None):
        verify_prepared_session(prepared)
        if directory.exists():
            raise RolloutBlocked("use a new runtime output directory")
        directory.mkdir(parents=True)
        self.directory, self.prepared = directory, prepared
        self.progress_callback = progress_callback
        self.timeline = SyntheticTimeline(directory)
        self.user = UserSimulator(prepared, directory / "user", user_client, guard_client)
        self.speech = SpeechRenderer(prepared, directory / "user-speech", user_tts,
                                     voice_acceptance_reason="用户已试听认可本 case male-qn-qingse，保留冻结声音描述；本次增加 provider word subtitles")
        self.actor_tts = actor_tts
        self.actor_url_tts = actor_url_tts
        self.actor = SyntheticAssistant(directory / "assistant", actor_text_client, guard_client)
        self.turns = json.loads((prepared / "frozen/episode_script.json").read_text())["episode_turns"]
        self.response_count, self.observation_count = 0, 0
        self.pending_asides = []
        self.user_results, self.listener_results, self.triggers = [], [], []
        self.started = time.monotonic()
        self.manifest = {"schema": "duplex-runtime.synthetic-rollout.v1", "case_id": json.loads((prepared/"session.json").read_text())["case_id"],
                         "prepared_session_sha256": digest(prepared/"session.json"), "mode": "offline_synthetic_gpt_tts_pcm_clock",
                         "assistant_model": actor_text_client.model, "user_model": user_client.model, "judge_model": guard_client.model,
                         "user_speech": asdict(user_tts.settings), "assistant_speech": asdict(actor_tts.settings),
                         "api_wait_advances_synthetic_clock": False, "synthetic_model_reaction_delay_frames": 0,
                         "native_realtime_latency_measured": False, "native_server_cancellation": False,
                         "additional_asr_used": False, "actor_sees_frozen_script_or_rubric": False,
                         "assistant_wav_url_recovery_authorized": actor_url_tts is not None,
                         "delivery_recovery_policy": "same_fulltext_WAV_URL_only_after_HTTP500_InternalError" if actor_url_tts else "none",
                         "sampling_policy": {"incoming_check_chars": 6, "trigger_check_chars": 10,
                                             "internal_silence_check_ms": 400, "initial_end_silence_ms": 600,
                                             "additional_end_checks_ms": [1500, 3000], "aside_after_assistant_chars": 8}}
        write_json(directory / "manifest.json", self.manifest)
        source_root=Path(__file__).parent.parent
        sources=list((source_root/"duplex_runtime").glob("*.py"))+list((source_root/"duplex_runtime/prompts").glob("*.md"))
        sources += [source_root/"minimax_client.py",source_root/"run_with_api_profile.py"]
        source_hashes={}
        for source in sources:
            relative=source.relative_to(source_root)
            target=directory/"code_snapshot"/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target);source_hashes[str(relative)]=digest(source)
        write_json(directory/"code_snapshot/source-manifest.json",source_hashes)

    def new_response(self):
        if self.timeline.active:
            self.timeline.stop("new response selected after observed incoming silence")
        self.response_count += 1
        identifier = f"A{self.response_count:03d}"
        text = self.actor.response(self.timeline.context())
        folder = self.directory / "assistant-audio" / identifier
        folder.mkdir(parents=True)
        try:
            raw = self.actor_tts.synthesize(text, folder)
        except SpeechError:
            metadata_path, response_path = folder/"metadata.json", folder/"response.json"
            metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
            response = json.loads(response_path.read_text()) if response_path.exists() else {}
            if self.actor_url_tts is None or metadata.get("http_status") != 500 or response.get("code") != "InternalError":
                raise
            original_folder = folder
            requested = json.loads((folder/"request.json").read_text())
            resolved = self.actor_url_tts.settings.request(text)
            comparison = json.loads(json.dumps(resolved));comparison["input"].pop("output_format")
            if comparison != requested:
                raise RolloutBlocked("URL recovery would change synthesis conditions")
            folder = folder/"url-delivery";folder.mkdir()
            self.timeline.log("tts_audio_delivery_recovery", response_id=identifier, original_http_status=500,
                              changed_field="input.output_format=url", synthesis_conditions_unchanged=True)
            raw = self.actor_url_tts.synthesize(text, folder)
            write_json(original_folder/"delivery-recovery.json", {"original_request":"request.json","original_response":"response.json",
                       "successful_delivery_directory":"url-delivery","changed_field":"input.output_format=url",
                       "original_text_sha256":hashlib.sha256(text.encode()).hexdigest(),"synthesis_conditions_unchanged":True})
        (folder / "raw.wav").write_bytes(raw)
        pcm = decode_wav(raw)
        spans = word_spans(text, json.loads((folder/"subtitle.json").read_text()), len(pcm))
        self.timeline.start_response(identifier, text, pcm, spans, folder / "raw.wav")

    def listener(self, text, state, silence_ms=0):
        context = {**self.timeline.context(), "incoming_heard_text": text, "incoming_state": state, "silence_ms": silence_ms}
        result = self.actor.decide("listener", context)
        self.listener_results.append({"frame": self.timeline.frame, **context, **result})
        write_json(self.directory/"listener-results.json", self.listener_results)
        self.timeline.log("assistant_listener_decision", decision=result["decision"], evidence=result["evidence"],
                          incoming_state=state, silence_ms=silence_ms)
        if result["decision"] == "stop_and_listen":
            self.timeline.stop("semantic listener requested stop on heard input")
        elif result["decision"] == "respond":
            self.new_response()
        return result["decision"]

    def play_input(self, main, spans, identifier, *, kind, focus_turn_id):
        start = self.timeline.frame
        pcm = decode_wav((self.speech.directory/main["path"]).read_bytes())
        if digest(self.speech.directory/main["path"]) != main["sha256"]:
            raise RolloutBlocked("incoming audio changed")
        self.timeline.placements.append({"speaker": "user", "audio_id": identifier, "kind": kind, "focus_turn_id": focus_turn_id,
                                         "start_frame": start, "frames": len(pcm), "text": main["text"],
                                         "path": str((self.speech.directory/main["path"]).relative_to(self.directory))})
        self.timeline.log("user_audio_started", audio_id=identifier, kind_detail=kind, focus_turn_id=focus_turn_id,
                          overlap_with_assistant=self.timeline.is_speaking)
        steps = [(s.end_frame, "word", s) for s in spans]
        if main.get("pause"):
            pause = main["pause"]
            # User-approved exact PCM gap. Future duration/continuation never enters the listener.
            steps.append((pause["start_frame"] + 400*self.timeline.rate//1000, "pause", pause))
        previous, checked = 0, 0
        for local_frame, category, item in sorted(steps, key=lambda x:x[0]):
            self.timeline.advance(start+local_frame)
            if category == "word":
                self.timeline.hear("user", identifier, main["text"][previous:item.end_char], start+local_frame)
                previous = item.end_char
                if self.timeline.is_speaking and previous-checked >= 6:
                    self.listener(main["text"][:previous], "speaking")
                    checked = previous
            else:
                decision = self.listener(main["text"][:previous], "silence", 400)
                self.timeline.log("intra_user_pause_observed", focus_turn_id=focus_turn_id, pause_frames=item["frames"],
                                  listener_decision=decision, premature_response=decision == "respond")
        self.timeline.advance(start+len(pcm))
        if previous != len(main["text"]):
            raise RolloutBlocked("incoming text was not causally released in full")
        self.timeline.log("user_audio_ended", audio_id=identifier, focus_turn_id=focus_turn_id)
        for silence_ms in (600, 1500, 3000):
            self.timeline.advance(start+len(pcm)+silence_ms*self.timeline.rate//1000)
            decision = self.listener(main["text"], "silence", silence_ms)
            if decision not in {"listen", "stop_and_listen"}:
                break
        else:
            raise RolloutBlocked("assistant listener still waiting after 3s synthetic silence; no forced response")

    def deliver(self, observation=None):
        event = self.user.step(observation)
        rendered = self.speech.render_event(event)
        self.user_results.append({"event": event, "rendered": rendered, "start_frame": self.timeline.frame})
        write_json(self.directory/"user-results.json", self.user_results)
        if event["action"] == "wait":
            return event
        if self.pending_asides:
            raise RolloutBlocked("new user main input reached before an existing aside was scheduled")
        main = rendered["main"]
        spans, provenance = rendered_spans(self.speech.directory, main)
        write_json(self.directory/f"user-alignment-{event['sequence']:03d}.json", {"spans": [asdict(s)for s in spans], "sources": provenance})
        self.play_input(main, spans, f"user-{event['sequence']:03d}", kind=event["action"], focus_turn_id=event["focus_turn_id"])
        self.pending_asides = [(aside, event["focus_turn_id"]) for aside in rendered["side_speech"]]
        return event

    def schedule_aside(self):
        aside, focus = self.pending_asides.pop(0)
        if not self.timeline.is_speaking:
            raise RolloutBlocked("during_assistant aside has no ongoing audible assistant output")
        subtitle_path = self.speech.directory / aside["call"] / "subtitle.json"
        spans = word_spans(aside["text"], json.loads(subtitle_path.read_text()), aside["frames"])
        # Identifier is visible to the assistant: do not leak "aside", target or Uxx.
        identifier = "input-"+hashlib.sha256(focus.encode()).hexdigest()[:12]
        self.play_input(aside, spans, identifier, kind="side_speech", focus_turn_id=focus)

    def next_observation(self):
        self.observation_count += 1
        identifier = f"synthetic-observation-{self.observation_count:03d}"
        pending = next((t for t in self.turns if t["turn_id"] == self.user.next_turn_id), None)
        need_trigger = bool(pending and "T03" in pending["timing_refs"])
        checked = 0
        while self.timeline.active:
            active = self.timeline.active
            ledger = active["ledger"]
            candidates = [s for s in ledger.spans if s.end_frame > ledger.played_frames]
            candidate = next((s for s in candidates if s.end_char-checked >= (8 if self.pending_asides else 10)), None)
            if candidate is None:
                self.timeline.advance(active["start_frame"]+ledger.received_frames)
                break
            self.timeline.advance(active["start_frame"]+candidate.end_frame)
            checked = candidate.end_char
            if self.pending_asides and self.timeline.is_speaking:
                self.schedule_aside()
                checked = 0
                continue
            if need_trigger and self.timeline.is_speaking:
                observation = self.timeline.observation(identifier)
                result = self.actor.decide("trigger", {"interrupt_anchor": pending["interrupt_anchor"],
                                         "heard_assistant": observation.visible()["heard_text"], "playback_state": observation.playback_state})
                self.triggers.append({"frame": self.timeline.frame, "turn_id": pending["turn_id"], **result,
                                      "heard_assistant": observation.visible()["heard_text"]})
                write_json(self.directory/"trigger-results.json", self.triggers)
                self.timeline.log("user_trigger_checked", turn_id=pending["turn_id"], **result)
                if result["decision"] == "ready":
                    return observation
        if self.pending_asides:
            raise RolloutBlocked("assistant ended before frozen during_assistant aside could be scheduled")
        observation = self.timeline.observation(identifier)
        if need_trigger:
            # Record a genuine wait from user+guard. Do not turn the pending T03 into T01.
            self.deliver(observation)
            raise RolloutBlocked(f"{pending['turn_id']} interrupt anchor not reached before audible playback ended; original script retained")
        return observation

    def run(self):
        status, failure = "running", None
        try:
            event = self.deliver()
            self.checkpoint()
            for _ in range(60):
                if event["action"] == "end":
                    if self.timeline.active:
                        active = self.timeline.active
                        self.timeline.advance(active["start_frame"]+active["ledger"].received_frames)
                    status = "completed"
                    break
                observation = self.next_observation()
                event = self.deliver(observation)
                self.checkpoint()
            else:
                raise RolloutBlocked("event limit reached; no frozen turn skipped")
        except (Exception, KeyboardInterrupt) as exc:
            status = "blocked"
            failure = {"type": type(exc).__name__, "reason": str(exc)}
            if self.timeline.active:
                self.timeline.stop("run halted; buffer discarded without claiming task completion")
            raise
        finally:
            audio = self.timeline.write_audio()
            self.timeline.save()
            summary = {"schema": "duplex-runtime.synthetic-rollout-result.v1", "status": status, "failure": failure,
                       "mode": self.manifest["mode"], "case_id": self.manifest["case_id"], "audio": audio,
                       "delivered_turns": [r["event"]["focus_turn_id"] for r in self.user_results if r["event"]["action"] == "deliver"],
                       "user_state": self.user.state_snapshot(), "user_events": len(self.user_results),
                       "user_model_and_guard_calls": self.user.model_calls, "assistant_calls": self.actor.calls,
                       "assistant_responses": self.response_count, "user_tts_calls": self.speech.model_calls,
                       "assistant_tts_calls": len(list((self.directory/"assistant-audio").rglob("metadata.json"))),
                       "wall_elapsed_seconds": time.monotonic()-self.started, "pending_asides": len(self.pending_asides),
                       "native_realtime_latency_measured": False, "task_scoring_performed": False}
            write_json(self.directory/"result.json", summary)

    def checkpoint(self):
        if self.progress_callback:
            state = {"user": self.user.state_snapshot(), "frame": self.timeline.frame,
                     "events": self.timeline.events, "observed_audio": self.timeline.observed,
                     "placements": self.timeline.placements, "responses": self.timeline.responses,
                     "pending_asides": self.pending_asides}
            from duplex_runtime.user_simulator import json_hash
            self.progress_callback({"accepted_user_events": len(self.user_results),
                                    "delivered_main_turns": sum(r["event"]["action"] == "deliver" for r in self.user_results),
                                    "frame": self.timeline.frame, "state_sha256": json_hash(state)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--profile", default="commonstack-chat")
    parser.add_argument("--model", default="openai/gpt-5.6-sol")
    parser.add_argument("--reuse-calls-from", type=Path, action="append", default=[], help="reuse only exact matching successful real calls; retain original attempts")
    parser.add_argument("--assistant-wav-url-recovery", action="store_true", help="explicitly allow same-WAV URL delivery after HTTP500 InternalError; no text/voice/model changes")
    args = parser.parse_args()
    client, guard = configured_clients(args.credentials_file, args.profile, args.model, args.model)
    text = StreamingChatClient(client.base_url, client.api_key, model=args.model)
    key = os.environ.get("DASHSCOPE_API_KEY") or getpass.getpass("DashScope API key (not stored): ")
    user_tts = DashScopeTTSClient(key, SpeechSettings(subtitle_type="word"))
    actor_tts = DashScopeTTSClient(key, SpeechSettings(voice_id="female-chengshu", subtitle_type="word"))
    actor_url_tts = DashScopeTTSClient(key, SpeechSettings(voice_id="female-chengshu", subtitle_type="word", audio_delivery="url")) if args.assistant_wav_url_recovery else None
    if args.reuse_calls_from:
        from duplex_runtime.recorded_calls import RecordedModelCalls, RecordedStreamingCalls, RecordedTTSCalls
        sources=[p.resolve()for p in args.reuse_calls_from]
        client,guard=RecordedModelCalls(client,sources),RecordedModelCalls(guard,sources)
        text=RecordedStreamingCalls(text,sources)
        user_tts,actor_tts=RecordedTTSCalls(user_tts,sources),RecordedTTSCalls(actor_tts,sources)
        if actor_url_tts:actor_url_tts=RecordedTTSCalls(actor_url_tts,sources)
    rollout = SyntheticRollout(args.session_dir.resolve(), args.output_dir.resolve(), client, guard, text, user_tts, actor_tts, actor_url_tts)
    try:
        rollout.run()
    except Exception as exc:
        parser.exit(1, f"Synthetic rollout stopped: {type(exc).__name__}: {exc}\n")
    print(json.dumps({"status": "completed", "output_dir": str(args.output_dir), "synthetic": True}), flush=True)


if __name__ == "__main__":
    main()
