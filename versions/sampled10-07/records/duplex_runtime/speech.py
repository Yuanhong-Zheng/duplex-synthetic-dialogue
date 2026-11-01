"""User-side speech assets. No assistant audio, playback clock, or rollout is invented."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import requests

from duplex_runtime.audio_gain import AmbientGainPolicy, ambient_gain_key, scale_pcm16
from duplex_runtime.prepare import verify_prepared_session
from duplex_runtime.user_simulator import json_hash, write_json


class SpeechError(ValueError):
    pass


@dataclass(frozen=True)
class SpeechSettings:
    endpoint: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    model: str = "MiniMax/speech-2.8-hd"
    voice_id: str = "male-qn-qingse"
    speed: float = 1.0
    volume: float = 1.0
    pitch: int = 0
    sample_rate: int = 16000
    subtitle_type: str | None = None
    audio_delivery: str = "hex"
    emotion: str | None = None
    voice_modify: dict | None = None

    def __post_init__(self):
        if self.endpoint != SpeechSettings.__dataclass_fields__["endpoint"].default:
            raise SpeechError("this adapter supports the verified DashScope endpoint only")
        if not self.voice_id or not 0.5 <= self.speed <= 2 or not 0 < self.volume <= 10:
            raise SpeechError("invalid voice settings")
        if type(self.pitch) is not int or not -12 <= self.pitch <= 12 or self.sample_rate != 16000:
            raise SpeechError("this renderer requires a 16kHz output and valid integer pitch")
        if self.model != "MiniMax/speech-2.8-hd":
            raise SpeechError("use the user-selected speech-2.8-hd model")
        if self.subtitle_type not in {None, "word"}:
            raise SpeechError("only verified word subtitles are supported")
        if self.audio_delivery not in {"hex", "url"}:
            raise SpeechError("audio delivery must be hex or URL")
        if self.emotion not in {None,"happy","sad","angry","fearful","disgusted","surprised","calm"}:
            raise SpeechError("unsupported speech-2.8 emotion")
        if self.voice_modify is not None and (not isinstance(self.voice_modify,dict) or
                set(self.voice_modify)-{"pitch","intensity","timbre"} or
                any(type(v) is not int or not -100 <= v <= 100 for v in self.voice_modify.values())):
            raise SpeechError("invalid voice characteristic adjustment")

    def request(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) >= 10000:
            raise SpeechError("TTS text must contain 1–9999 characters")
        if re.search(r"<#|\(laughs\)|\(sighs\)", text):
            raise SpeechError("runtime utterances must not contain synthesis control tags")
        request = {"model": self.model, "input": {"action": "tts", "text": text,
                "voice_setting": {"voice_id": self.voice_id, "speed": self.speed, "vol": self.volume, "pitch": self.pitch},
                "audio_setting": {"sample_rate": self.sample_rate, "format": "wav", "channel": 1}}}
        if self.subtitle_type:
            request["input"].update(subtitle_enable=True, subtitle_type=self.subtitle_type)
        if self.emotion is not None:
            request["input"]["voice_setting"]["emotion"] = self.emotion
        if self.voice_modify:
            request["input"]["voice_modify"] = dict(self.voice_modify)
        if self.audio_delivery == "url":
            request["input"]["output_format"] = "url"
        return request


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode_wav(raw: bytes, sample_rate=16000) -> np.ndarray:
    import wave
    try:
        with wave.open(io.BytesIO(raw), "rb") as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (sample_rate, 1, 2, "NONE"):
                raise SpeechError("TTS returned a different audio format; no implicit conversion")
            count = audio.getnframes()
            pcm = audio.readframes(count)
    except (EOFError, wave.Error) as exc:
        raise SpeechError("TTS response is not a valid PCM WAV") from exc
    samples = np.frombuffer(pcm, dtype="<i2").copy()
    if len(samples) != count or not count or not np.any(samples):
        raise SpeechError("TTS audio is truncated, empty, or entirely silent")
    return samples


def encode_wav(samples: np.ndarray, sample_rate=16000) -> bytes:
    import wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buffer.getvalue()


def verify_ambient_asset(directory: Path, asset: dict, policy: dict | None, key: str):
    """Verify derived PCM against the untouched provider source; accept legacy manifests."""
    if policy is None:
        if asset.get("gain") is not None:
            raise SpeechError("ambient gain is missing its manifest policy")
        return
    expected = AmbientGainPolicy(**policy).record(key)
    if asset.get("gain") != expected:
        raise SpeechError("ambient gain differs from its frozen seed or clip identity")
    source, target = directory / asset["source_path"], directory / asset["path"]
    if digest(source) != asset["source_sha256"] or digest(target) != asset["sha256"]:
        raise SpeechError("ambient source or rendered audio changed")
    original, rendered = decode_wav(source.read_bytes()), decode_wav(target.read_bytes())
    if len(original) != asset["frames"] or not np.array_equal(rendered, scale_pcm16(original, expected["factor"])):
        raise SpeechError("ambient PCM differs from its recorded gain or frame count")


def sanitize(value, secret=""):
    if isinstance(value, dict):
        return {key: sanitize(item, secret) for key, item in value.items() if key.lower() not in {"authorization", "api_key", "headers"}}
    if isinstance(value, list):
        return [sanitize(item, secret) for item in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
        if value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            if parsed.query:
                value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "REDACTED_QUERY", ""))
    return value


class DashScopeTTSClient:
    """Identical requests get five retries; credentials stay in memory."""
    def __init__(self, api_key: str, settings: SpeechSettings | None = None):
        if not api_key:
            raise SpeechError("DASHSCOPE_API_KEY is missing")
        self.settings = settings or SpeechSettings()
        self._key = api_key

    def synthesize(self, text: str, call_dir: Path) -> bytes:
        request = self.settings.request(text)
        write_json(call_dir / "request.json", request)
        started = time.monotonic()
        metadata = {"provider": "dashscope", "model": self.settings.model, "endpoint": self.settings.endpoint,
                    "created_utc": datetime.now(timezone.utc).isoformat()}
        from duplex_runtime.request_retry import request_with_retries, RequestRetriesExhausted
        responses = 0
        def post():
            nonlocal responses
            response = requests.post(self.settings.endpoint, json=request,
                headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
                timeout=(10, 60), allow_redirects=False)
            responses += 1
            metadata['http_status'] = response.status_code
            try: body = response.json()
            except ValueError as exc:
                raise requests.RequestException('TTS returned a non-JSON response') from exc
            write_json(call_dir/f'http-response-{responses:03d}.json',sanitize(body,self._key))
            write_json(call_dir/'response.json',sanitize(body,self._key))
            return response
        try:
            response = request_with_retries(post,call_dir,'synthesis',valid_response=lambda r:
                (r.json().get('output')or{}).get('base_resp',{}).get('status_code') == 0)
            metadata["http_status"] = response.status_code
            try:
                body = response.json()
            except ValueError as exc:
                raise SpeechError("TTS returned a non-JSON response") from exc
            write_json(call_dir / "response.json", sanitize(body, self._key))
            output = body.get("output") or {}
            metadata.update(request_id=body.get("request_id"), base_resp=output.get("base_resp"), usage=body.get("usage"))
            if response.status_code != 200 or output.get("base_resp", {}).get("status_code") != 0:
                raise SpeechError("TTS request failed; see redacted response and metadata")
            audio = (output.get("data") or {}).get("audio")
            metadata["audio_delivery"] = self.settings.audio_delivery
            if self.settings.audio_delivery == "url":
                parsed = urlsplit(audio) if isinstance(audio, str) else None
                if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise SpeechError("TTS did not return a valid HTTPS audio URL")
                download_started = time.monotonic()
                # The provider token is NEVER forwarded to the signed audio host.
                download = request_with_retries(lambda: requests.get(audio, timeout=(10,60),allow_redirects=False),
                                               call_dir,'audio-download')
                metadata.update(audio_download_http_status=download.status_code,
                                audio_download_seconds=time.monotonic()-download_started,
                                credentials_forwarded_to_audio_host=False)
                if download.status_code != 200:
                    raise SpeechError("audio URL download failed; no format/model substitution")
                raw = download.content
                (call_dir / "downloaded_audio.wav").write_bytes(raw)
                metadata["downloaded_audio_sha256"] = hashlib.sha256(raw).hexdigest()
                metadata["downloaded_audio_bytes"] = len(raw)
            else:
                if not isinstance(audio, str) or not re.fullmatch(r"[0-9A-Fa-f]+", audio) or len(audio) % 2:
                    raise SpeechError("TTS did not return hex audio")
                raw = bytes.fromhex(audio)
            decode_wav(raw, self.settings.sample_rate)
            if self.settings.subtitle_type:
                url = (output.get("data") or {}).get("subtitle_file")
                parsed = urlsplit(url) if isinstance(url, str) else None
                if not parsed or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise SpeechError("word subtitles missing or invalid; no estimated alignment fallback")
                # Signed CDN URL; never forward the provider bearer token to this host.
                subtitle = request_with_retries(lambda: requests.get(url,timeout=(10,30),allow_redirects=False),
                                               call_dir,'subtitle-download')
                if subtitle.status_code != 200:
                    raise SpeechError("subtitle download failed; signed URL was redacted")
                try:
                    parsed_subtitle = subtitle.json()
                except ValueError as exc:
                    raise SpeechError("subtitle response is not JSON") from exc
                write_json(call_dir / "subtitle.json", parsed_subtitle)
                metadata["subtitle_sha256"] = digest(call_dir / "subtitle.json")
                metadata["alignment_source"] = "minimax_provider_word_timestamps"
            return raw
        except RequestRetriesExhausted as exc:
            raise SpeechError("TTS request exhausted five identical-parameter retries") from exc
        except requests.RequestException as exc:
            raise SpeechError("TTS transport failed; retry the same accepted event explicitly") from exc
        finally:
            metadata["elapsed_seconds"] = time.monotonic() - started
            write_json(call_dir / "metadata.json", sanitize(metadata, self._key))


def trim_edges(samples: np.ndarray, sample_rate: int, threshold_dbfs=-50.0) -> tuple[np.ndarray, dict]:
    """Declared acoustic boundary, not word alignment. Only remove quiet outer edges."""
    threshold = math.ceil(32768 * 10 ** (threshold_dbfs / 20))
    active = np.flatnonzero(np.abs(samples.astype(np.int32)) >= threshold)
    if not len(active):
        raise SpeechError("no reliable speech boundary above the declared trim threshold")
    start, end = int(active[0]), int(active[-1]) + 1
    if max(start, len(samples) - end) > 2 * sample_rate or end - start < sample_rate * 0.05:
        raise SpeechError("ambiguous edge trim; inspect the audio before choosing another method")
    return samples[start:end].copy(), {"method": "outer_sample_amplitude_threshold_v1", "threshold_dbfs": threshold_dbfs,
        "threshold_pcm16": threshold, "raw_frames": len(samples), "kept_start_frame": start,
        "kept_end_frame": end, "removed_leading_frames": start, "removed_trailing_frames": len(samples) - end,
        "first_kept_sample": int(samples[start]), "last_kept_sample": int(samples[end - 1])}


class SpeechRenderer:
    """Single-writer, hash-verified asset cache and immutable accepted-event rendering."""
    def __init__(self, prepared_dir: Path, output_dir: Path, client, *, voice_acceptance_reason: str,
                 ambient_gain_policy: AmbientGainPolicy | None = None, side_clients: dict | None = None, voice_plan: dict | None = None):
        verify_prepared_session(prepared_dir)
        if not voice_acceptance_reason.strip():
            raise SpeechError("record the user's voice selection/override reason")
        self.prepared = prepared_dir.resolve()
        self.directory = output_dir.resolve()
        if self.directory.exists() or self.directory.is_relative_to(self.prepared):
            raise SpeechError("use a new speech directory outside the frozen environment")
        self.directory.mkdir(parents=True)
        for name in ["calls", "cache", "events", "failures"]:
            (self.directory / name).mkdir()
        self.client = client
        self.settings = client.settings
        self.side_clients = {"user": client, **(side_clients or {})}
        if self.side_clients["user"] is not client:
            raise SpeechError("user asides must use the same pinned user voice")
        for role, side_client in self.side_clients.items():
            if role not in {"user", "third_party", "machine"} or side_client.settings.sample_rate != self.settings.sample_rate:
                raise SpeechError("invalid side-speech speaker client")
        voice_ids = [c.settings.voice_id for c in self.side_clients.values()]
        if len(set(voice_ids)) != len(voice_ids):
            raise SpeechError("side-speech speaker roles must have distinct voices")
        self.ambient_gain_policy = ambient_gain_policy or AmbientGainPolicy()
        from duplex_runtime.audio_validation import METHOD, MAX_REGENERATIONS
        self.turns = {turn["turn_id"]: turn for turn in json.loads((self.prepared / "frozen/episode_script.json").read_text())["episode_turns"]}
        self.manifest = {"schema": "duplex-runtime.speech.v1", "case_id": json.loads((self.prepared / "session.json").read_text())["case_id"],
            "prepared_session_sha256": digest(self.prepared / "session.json"), "settings": asdict(self.settings),
            "ambient_gain_policy": asdict(self.ambient_gain_policy),
            "tts_alignment_policy": {"method":METHOD,"max_regenerations":MAX_REGENERATIONS,
                                     "enabled":self.settings.subtitle_type == "word"},
            "frozen_voice_profile": json.loads((self.prepared / "frozen/user_voice.json").read_text())["voice_profile"],
            "runtime_voice_selection": {"voice_id": self.settings.voice_id, "reason": voice_acceptance_reason, "frozen_profile_edited": False},
            "pause_policy": {"method": "split_at_unique_anchor_trim_outer_edges_insert_pcm_silence_v1", "threshold_dbfs": -50.0,
                "sample_rate": self.settings.sample_rate, "word_alignment_claimed": False},
            "actor_connected": False, "playback_executed": False}
        if voice_plan is not None:
            from duplex_runtime.voice_profile import validate_plan
            selected=validate_plan(voice_plan,self.manifest['frozen_voice_profile'])
            if selected!=self.settings:raise SpeechError('TTS client differs from the compiled user voice profile')
            self.manifest['compiled_voice_profile']=voice_plan
            self.manifest['runtime_voice_selection'].update(source='frozen_stage1_voice_profile',reason=voice_plan['mapping']['reason'])
        if side_clients is not None:
            self.manifest["side_speech_settings"] = {role: asdict(c.settings) for role, c in self.side_clients.items()}
        write_json(self.directory / "manifest.json", self.manifest)
        self.records, self._processed = [], {}
        self.model_calls = 0
        self.cache_hits = 0

    def _asset(self, text: str, *, t02_continuation: bool | None = None, client=None) -> dict:
        client = client or self.client
        settings = client.settings
        request = settings.request(text)
        identity={"endpoint": settings.endpoint, "request": request, "schema": "tts-pcm16-v1"}
        if t02_continuation is not None:
            identity['t02_alignment_validation']={'continuation':t02_continuation,'method':self.manifest['tts_alignment_policy']['method']}
        key = json_hash(identity)
        target = self.directory / "cache" / key
        if target.exists():
            record = json.loads((target / "asset.json").read_text())
            if record["request"] != request or digest(target / "raw.wav") != record["sha256"]:
                raise SpeechError("cached TTS asset changed; refusing silent regeneration")
            decode_wav((target / "raw.wav").read_bytes(), settings.sample_rate)
            self.cache_hits += 1
            return {**record, "cache_hit": True}
        self.model_calls += 1
        call_dir = self.directory / "calls" / f"{self.model_calls:04d}"
        call_dir.mkdir()
        write_json(call_dir / "request.json", request)
        try:
            if t02_continuation is not None:
                from duplex_runtime.audio_validation import synthesize_checked
                raw = synthesize_checked(client,text,call_dir,continuation=t02_continuation)
            else: raw = client.synthesize(text, call_dir)
            samples = decode_wav(raw, settings.sample_rate)
            record = {"key": key, "request": request, "sha256": hashlib.sha256(raw).hexdigest(),
                "path": f"cache/{key}/raw.wav", "call": str(call_dir.relative_to(self.directory)),
                "frames": len(samples), "sample_rate": settings.sample_rate, "duration_seconds": len(samples) / settings.sample_rate}
            with tempfile.TemporaryDirectory(prefix=".asset-", dir=self.directory / "cache") as stage:
                stage = Path(stage)
                (stage / "raw.wav").write_bytes(raw)
                write_json(stage / "asset.json", record)
                stage.rename(target)
            return {**record, "cache_hit": False}
        except Exception as exc:
            write_json(call_dir / "failure.json", {"type": type(exc).__name__, "progress": "no_rendered_event_committed"})
            raise

    def _main_audio(self, event: dict, folder: Path) -> dict:
        text = event["utterance"]
        cue = (event.get("dispatch") or {}).get("pause_cue")
        if not cue:
            asset = self._asset(text)
            raw = (self.directory / asset["path"]).read_bytes()
            result = {"text": text, "segments": [asset], "pause": None}
        else:
            anchor, milliseconds = cue["after_text"], cue["duration_ms"]
            if text.count(anchor) != 1 or type(milliseconds) is not int or milliseconds <= 0:
                raise SpeechError("pause requires the original unique anchor and a positive integer duration")
            split = text.index(anchor) + len(anchor)
            texts = [text[:split], text[split:]]
            if not all(any(character.isalnum() for character in piece) for piece in texts):
                raise SpeechError("pause must have speakable text on both sides")
            assets = [self._asset(piece,t02_continuation=bool(index)) if self.settings.subtitle_type=='word' else self._asset(piece)
                      for index,piece in enumerate(texts)]
            trimmed = [trim_edges(decode_wav((self.directory / item["path"]).read_bytes()), self.settings.sample_rate) for item in assets]
            frames, remainder = divmod(milliseconds * self.settings.sample_rate, 1000)
            if remainder:
                raise SpeechError("pause duration is not representable at this sample rate")
            joined = np.concatenate([trimmed[0][0], np.zeros(frames, dtype=np.int16), trimmed[1][0]])
            start = len(trimmed[0][0])
            assert joined[start - 1] != 0 and joined[start + frames] != 0
            raw = encode_wav(joined, self.settings.sample_rate)
            result = {"text": text, "segments": [{**asset, "trim": info} for asset, (_, info) in zip(assets, trimmed)],
                "pause": {"after_text": anchor, "requested_ms": milliseconds, "start_frame": start, "frames": frames,
                    "end_frame": start + frames, "measured_inserted_silence_ms": frames * 1000 / self.settings.sample_rate,
                    "start_seconds": start / self.settings.sample_rate, "duration_seconds": milliseconds / 1000,
                    "boundary_definition": "outer PCM amplitude >= -50 dBFS; sample-exact inserted zero interval; not independent word alignment"}}
        (folder / "main.wav").write_bytes(raw)
        samples = decode_wav(raw)
        return {**result, "path": str((folder / "main.wav").relative_to(self.directory)), "sha256": hashlib.sha256(raw).hexdigest(),
                "frames": len(samples), "sample_rate": self.settings.sample_rate, "duration_seconds": len(samples) / self.settings.sample_rate}

    def render_event(self, event: dict) -> dict:
        verify_prepared_session(self.prepared)
        if digest(self.prepared / "session.json") != self.manifest["prepared_session_sha256"]:
            raise SpeechError("prepared session changed")
        if event.get("schema") != "duplex-runtime.user-event.v1" or type(event.get("sequence")) is not int or event["sequence"] < 1 or not isinstance(event.get("observation_id"), str) or not event["observation_id"]:
            raise SpeechError("speech requires an identified accepted simulator event")
        if event.get("semantic_guard") != {"decision": "pass", "findings": []}:
            raise SpeechError("only accepted simulator events may be spoken")
        action = event.get("action")
        if action not in {"deliver", "correct", "clarify", "unknown", "wait", "end", "backchannel"}:
            raise SpeechError("unsupported simulator action")
        if not isinstance(event.get("utterance"), str) or (action == "wait") != (not event["utterance"]):
            raise SpeechError("wait must remain silent; other actions require speech")
        dispatch = event.get("dispatch") or {}
        if action == "deliver":
            turn = self.turns.get(event.get("focus_turn_id"))
            if turn is None or any(dispatch.get(field) != turn.get(field) for field in ["timing_refs", "pause_cue"]):
                raise SpeechError("event timing or pause differs from frozen turn")
            if dispatch.get("side_speech", []) != turn.get("side_speech", []):
                raise SpeechError("side speech differs from the frozen enrichment")
        elif dispatch.get("pause_cue") or dispatch.get("side_speech"):
            raise SpeechError("local replies cannot attach a new frozen pause or aside")
        fingerprint = json_hash(event)
        identity = json_hash({"observation_id": event.get("observation_id"), "sequence": event.get("sequence")})[:24]
        if identity in self._processed:
            old, record = self._processed[identity]
            if old != fingerprint:
                raise SpeechError("an accepted event identity was reused with changed contents")
            for item in [record.get("main"), *record["side_speech"]]:
                if item and digest(self.directory / item["path"]) != item["sha256"]:
                    raise SpeechError("rendered audio changed")
            for index, aside in enumerate(record["side_speech"]):
                verify_ambient_asset(self.directory, aside, self.manifest["ambient_gain_policy"],
                                     ambient_gain_key(self.manifest["case_id"], event["focus_turn_id"], index))
            if record.get("main"):
                from duplex_runtime.side_speech import verify_during_user
                verify_during_user(record["main"], record["side_speech"], self.directory)
            return {**copy.deepcopy(record), "cached_event": True}
        if any(a.get("speaker") == "user" and a.get("placement") == "during_user"
               for a in dispatch.get("side_speech", [])):
            raise SpeechError("user during_user side speech is disabled; preserve uninterrupted main speech")
        folder = self.directory / "events" / identity
        folder.mkdir(exist_ok=True)
        source = folder / "user_event.json"
        if source.exists() and json_hash(json.loads(source.read_text())) != fingerprint:
            raise SpeechError("retry must use the exact accepted event")
        write_json(source, event)
        try:
            main = None if action == "wait" else self._main_audio(event, folder)
            asides = []
            for index, aside in enumerate(dispatch.get("side_speech", [])):
                side_client = self.side_clients.get(aside["speaker"])
                if side_client is None:
                    raise SpeechError("side-speech role has no configured voice: " + aside["speaker"])
                if aside.get("placement") not in {"during_user", "during_assistant"}:
                    raise SpeechError("side-speech placement is unsupported")
                asset = self._asset(aside["text"], client=side_client)
                path = folder / f"side-{index:02d}.wav"
                gain = self.ambient_gain_policy.record(ambient_gain_key(self.manifest["case_id"], event["focus_turn_id"], index))
                original = decode_wav((self.directory / asset["path"]).read_bytes())
                path.write_bytes(encode_wav(scale_pcm16(original, gain["factor"])))
                asides.append({**asset, **aside, "side_speech_index": index, "source_path": asset["path"], "source_sha256": asset["sha256"],
                    "path": str(path.relative_to(self.directory)), "sha256": digest(path), "gain": gain,
                    "execution": "asset_ready_not_scheduled"})
            if main is not None:
                from duplex_runtime.side_speech import attach_during_user
                main, asides = attach_during_user(main, asides, self.directory, folder)
            record = {"schema": "duplex-runtime.rendered-user-event.v1", "event_id": identity, "user_event_sha256": fingerprint,
                "source_event": str(source.relative_to(self.directory)), "focus_turn_id": event.get("focus_turn_id"), "action": action,
                "main": main, "side_speech": asides, "status": "silent_wait" if action == "wait" else "audio_ready",
                "timing_refs": dispatch.get("timing_refs", []), "playback_executed": False, "actor_connected": False}
            write_json(folder / "render.json", record)
            self.records.append(record)
            self._processed[identity] = (fingerprint, copy.deepcopy(record))
            write_json(self.directory / "rendered-events.json", self.records)
            return copy.deepcopy(record)
        except Exception as exc:
            failure = self.directory / "failures" / f"{identity}-{len(list((self.directory / 'failures').iterdir())):04d}.json"
            write_json(failure, {"event_id": identity, "user_event_sha256": fingerprint, "error_type": type(exc).__name__,
                                 "source_event": str(source.relative_to(self.directory)), "status": "failed_no_advance_to_next_audio"})
            raise


class SpeakingUserSimulator:
    """Compose the existing text policy with TTS; block new steps until audio succeeds."""
    def __init__(self, simulator, renderer: SpeechRenderer):
        self.simulator, self.renderer = simulator, renderer
        self.pending_event = None

    def step(self, observation=None, **kwargs):
        if self.pending_event is not None:
            raise SpeechError("TTS pending; call retry_audio before requesting another user step")
        self.pending_event = self.simulator.step(observation, **kwargs)
        return self.retry_audio()

    def retry_audio(self):
        if self.pending_event is None:
            raise SpeechError("there is no pending accepted event")
        speech = self.renderer.render_event(self.pending_event)
        result = {"user_event": copy.deepcopy(self.pending_event), "speech": speech}
        self.pending_event = None
        return result
