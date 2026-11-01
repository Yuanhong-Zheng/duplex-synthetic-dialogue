"""Independent text assistant and listener for explicitly synthetic GPT+TTS episodes."""
from __future__ import annotations

import json
from pathlib import Path
import time

import requests

from duplex_runtime.environment import decode_object
from duplex_runtime.speech import sanitize
from duplex_runtime.user_simulator import json_hash, write_json


class AssistantError(ValueError):
    pass


def parse_chat_stream(lines, on_chunk):
    # Some compatible gateways emit empty choices beside the actual text choice.
    # Preserve every chunk, but never concatenate independent completions.
    text, selected, done = "", None, False
    finishes = {}
    for raw in lines:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not raw.startswith("data:"):
            continue
        value = raw[5:].strip()
        if value == "[DONE]":
            done = True
            break
        chunk = json.loads(value)
        if chunk.get("error"):
            raise AssistantError("chat streaming returned an error")
        on_chunk(chunk)
        for choice in chunk.get("choices", []):
            index = choice.get("index", 0)
            if type(index) is not int or index < 0:
                raise AssistantError("invalid assistant choice index")
            content = choice.get("delta", {}).get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise AssistantError("unexpected non-text assistant delta")
                if content:
                    if selected is not None and selected != index:
                        raise AssistantError("multiple assistant choices contain text")
                    if index in finishes:
                        raise AssistantError("assistant content follows its finish event")
                    selected = index
                    text += content
            if choice.get("finish_reason") is not None:
                reason = choice["finish_reason"]
                if index in finishes and finishes[index] != reason:
                    raise AssistantError("conflicting assistant finish events")
                finishes[index] = reason
    if not done or finishes.get(selected) != "stop" or not text.strip():
        raise AssistantError("incomplete or truncated assistant stream; no generated substitute")
    return text


class StreamingChatClient:
    """One real SSE request. Separate wall timings; no provider/model fallback."""
    def __init__(self, endpoint: str, api_key: str, model="openai/gpt-5.6-sol"):
        self.endpoint, self._key, self.model = endpoint, api_key, model

    def generate(self, prompt: str, context: dict, folder: Path) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"model": self.model, "messages": [{"role": "system", "content": prompt},
                   {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                   "stream": True, "max_tokens": 4096}
        write_json(folder / "request.json", payload)
        (folder / "prompt.md").write_text(prompt)
        write_json(folder / "input.json", context)
        started, chunks, first = time.monotonic(), [], None
        metadata = {"provider": "commonstack_openai_compatible", "requested_model": self.model,
                    "endpoint": self.endpoint, "input_sha256": json_hash(context),
                    "new_model_request": True, "native_audio_model": False}
        def record(chunk):
            nonlocal first
            elapsed = time.monotonic() - started
            chunks.append({"elapsed_seconds": elapsed, "chunk": chunk})
            if first is None and any(c.get("delta", {}).get("content") for c in chunk.get("choices", [])):
                first = elapsed
        try:
            with requests.post(self.endpoint, json=payload, headers={"Authorization": "Bearer " + self._key},
                               stream=True, timeout=(10, 120), allow_redirects=False) as response:
                metadata["http_status"] = response.status_code
                if response.status_code != 200:
                    write_json(folder / "error.json", sanitize(response.json(), self._key))
                    raise AssistantError("assistant HTTP request failed; see redacted archive")
                result = parse_chat_stream(response.iter_lines(chunk_size=1), record)
            (folder / "text.txt").write_text(result)
            metadata["status"] = "completed"
            return result
        except Exception as exc:
            metadata.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            metadata.update(elapsed_seconds=time.monotonic()-started, first_content_seconds=first)
            write_json(folder / "stream.json", sanitize(chunks, self._key))
            write_json(folder / "metadata.json", sanitize(metadata, self._key))


PROMPTS = Path(__file__).parent / "prompts"


class SyntheticAssistant:
    """Only caller-supplied heard audio is visible; no frozen environment is loaded."""
    def __init__(self, directory: Path, text_client, decision_client):
        if directory.exists():
            raise AssistantError("use a new assistant directory")
        directory.mkdir(parents=True)
        self.directory, self.text_client, self.decision_client = directory, text_client, decision_client
        self.calls = []
        self.prompts = {name: (PROMPTS / name).read_text() for name in ("assistant_policy.md", "assistant_listener.md", "trigger_observer.md")}
        for name, prompt in self.prompts.items():
            (directory / name).write_text(prompt)

    def _folder(self, role, context):
        folder = self.directory / f"{len(self.calls)+1:04d}-{role}"
        folder.mkdir()
        self.calls.append({"role": role, "path": folder.name, "input_sha256": json_hash(context)})
        write_json(self.directory / "calls.json", self.calls)
        return folder

    def response(self, context):
        folder = self._folder("response", context)
        # generate owns the request directory but permits its pre-created empty folder.
        return self.text_client.generate(self.prompts["assistant_policy.md"], context, folder)

    def decide(self, role, context):
        if role not in {"listener", "trigger"}:
            raise AssistantError("unknown semantic decision role")
        name = "assistant_listener.md" if role == "listener" else "trigger_observer.md"
        folder = self._folder(role, context)
        prompt = self.prompts[name]
        (folder / "prompt.md").write_text(prompt)
        write_json(folder / "input.json", context)
        start = time.monotonic()
        try:
            raw, metadata = self.decision_client.complete(prompt, context, max_tokens=700, response_format={"type": "json_object"})
            (folder / "raw_response.txt").write_text(raw)
            write_json(folder / "metadata.json", {**{k:v for k,v in metadata.items() if k in {"model","provider","fallback","timestamp","response_sha256"}},
                       "elapsed_seconds": time.monotonic()-start, "input_sha256": json_hash(context),
                       "new_model_request": metadata.get("new_model_request", True),
                       "reused_from": metadata.get("reused_from")})
            result = decode_object(raw.encode(), role)
            expected = {"decision", "evidence", "reason"}
            allowed = {"ready", "wait"} if role == "trigger" else {"stop_and_listen", "listen", "continue_speaking", "respond", "ignore"}
            if set(result) != expected or result["decision"] not in allowed or any(not isinstance(result[k], str) for k in expected):
                raise AssistantError("malformed semantic decision")
            heard = context.get("heard_assistant", "") if role == "trigger" else context.get("incoming_heard_text", "")
            if result["evidence"] and result["evidence"] not in heard:
                raise AssistantError("semantic decision quotes unobserved content")
            if role == "trigger" and result["decision"] == "ready" and not result["evidence"]:
                raise AssistantError("trigger readiness requires heard evidence")
            if role == "listener" and context["incoming_state"] == "speaking" and result["decision"] == "respond":
                raise AssistantError("cannot respond while incoming speech is still continuous")
            write_json(folder / "decision.json", result)
            return result
        except Exception as exc:
            write_json(folder / "failure.json", {"type": type(exc).__name__, "elapsed_seconds": time.monotonic()-start})
            raise
