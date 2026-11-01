"""Durable per-episode call sequence; exact replay across attempts, never across episodes."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import time

from duplex_runtime.speech import SpeechError, digest, sanitize
from duplex_runtime.user_simulator import json_hash, write_json


class JournalError(ValueError):
    pass


class CallJournal:
    def __init__(self, root, namespace):
        self.root = Path(root)/namespace
        self.root.mkdir(parents=True, exist_ok=True)
        self.cursor = 0

    def entry(self, request):
        self.cursor += 1
        folder = self.root/f"{self.cursor:05d}"
        folder.mkdir(exist_ok=True)
        path = folder/"identity.json"
        if path.exists():
            if json.loads(path.read_text()) != request:
                raise JournalError("resume request differs from durable sequence; original records retained")
        else:
            write_json(path, request)
        commit = folder/"commit.json"
        if commit.exists():
            data = json.loads(commit.read_text())
            for name, value in data["files"].items():
                if digest(folder/name) != value:
                    raise JournalError("committed call artifact changed; refusing regeneration")
            return folder, folder/data["result_directory"], True
        attempts = folder/"tries"; attempts.mkdir(exist_ok=True)
        target = attempts/f"{len(list(attempts.iterdir()))+1:03d}"; target.mkdir()
        write_json(target/"state.json", {"status": "started", "request_sha256": json_hash(request), "new_network_request": True})
        return folder, target, False

    def commit(self, folder, target):
        files = {str(p.relative_to(folder)): digest(p) for p in target.iterdir() if p.is_file()}
        files["identity.json"] = digest(folder/"identity.json")
        write_json(folder/"commit.json", {"schema": "duplex-runtime.call-commit.v1", "result_directory": str(target.relative_to(folder)), "files": files})


class JournalModel:
    def __init__(self, client, journal_root, namespace):
        self.client = client; self.journal = CallJournal(journal_root, namespace)

    def __getattr__(self, name):return getattr(self.client, name)

    def complete(self, prompt, context, max_tokens=4000, response_format=None):
        request = {"model": self.model, "provider": self.provider, "endpoint": self.base_url,
                   "temperature": self.temperature, "max_tokens": max_tokens, "response_format": response_format,
                   "prompt": prompt, "context": context}
        folder, target, reused = self.journal.entry(request)
        if reused:
            metadata = json.loads((target/"metadata.json").read_text())
            return (target/"text.txt").read_text(), {**metadata, "new_model_request": False, "reused_from": str(target)}
        started = time.monotonic()
        try:
            text, metadata = self.client.complete(prompt, context, max_tokens=max_tokens, response_format=response_format)
            (target/"text.txt").write_text(text)
            metadata = {**metadata, "new_model_request": True, "journal_elapsed_seconds": time.monotonic()-started}
            write_json(target/"metadata.json", sanitize(metadata))
            write_json(target/"state.json", {"status": "completed", "request_sha256": json_hash(request), "new_network_request": True})
            self.journal.commit(folder, target)
            return text, metadata
        except Exception as exc:
            write_json(target/"state.json", {"status": "failed", "error_type": type(exc).__name__, "request_sha256": json_hash(request), "new_network_request": True, "elapsed_seconds": time.monotonic()-started})
            raise


class JournalStream:
    def __init__(self, client, journal_root):
        self.client = client; self.journal = CallJournal(journal_root, "assistant-stream")

    def __getattr__(self, name):return getattr(self.client, name)

    def generate(self, prompt, context, output):
        request = {"model": self.model, "endpoint": self.endpoint, "prompt": prompt, "context": context, "stream": True, "max_tokens": 4096}
        folder, target, reused = self.journal.entry(request)
        if reused:
            for p in target.iterdir():
                if p.name != "state.json":shutil.copyfile(p, output/p.name)
            metadata = json.loads((output/"metadata.json").read_text())
            write_json(output/"metadata.json", {**metadata, "new_model_request": False, "reused_from": str(target)})
            return (output/"text.txt").read_text()
        try:
            text = self.client.generate(prompt, context, output)
            for p in output.iterdir():
                if p.is_file():shutil.copyfile(p, target/p.name)
            write_json(target/"state.json", {"status": "completed", "request_sha256": json_hash(request), "new_network_request": True})
            self.journal.commit(folder, target)
            return text
        except Exception as exc:
            for p in output.iterdir():
                if p.is_file():shutil.copyfile(p, target/p.name)
            write_json(target/"state.json", {"status": "failed", "error_type": type(exc).__name__, "request_sha256": json_hash(request), "new_network_request": True})
            raise


class JournalTTS:
    def __init__(self, client, journal_root, namespace):
        self.client = client; self.journal = CallJournal(journal_root, namespace)

    def __getattr__(self, name):return getattr(self.client, name)

    def synthesize(self, text, output):
        request = {"endpoint": self.settings.endpoint, "request": self.settings.request(text)}
        folder, target, reused = self.journal.entry(request)
        if reused:
            for p in target.iterdir():
                if p.name not in {"state.json", "audio.wav"}:shutil.copyfile(p, output/p.name)
            meta = json.loads((output/"metadata.json").read_text())
            write_json(output/"metadata.json", {**meta, "new_model_request": False, "reused_from": str(target)})
            if not (target/"audio.wav").exists():
                raise SpeechError("replayed original HTTP500 InternalError; use the authorized identical WAV URL route")
            return (target/"audio.wav").read_bytes()
        try:
            raw = self.client.synthesize(text, output)
            (target/"audio.wav").write_bytes(raw)
            for p in output.iterdir():
                if p.is_file():shutil.copyfile(p, target/p.name)
            write_json(target/"state.json", {"status": "completed", "request_sha256": json_hash(request), "new_network_request": True})
            self.journal.commit(folder, target)
            return raw
        except Exception as exc:
            for p in output.iterdir():
                if p.is_file():shutil.copyfile(p, target/p.name)
            write_json(target/"state.json", {"status": "failed", "error_type": type(exc).__name__, "request_sha256": json_hash(request), "new_network_request": True})
            # Preserve the error that selected an authorized URL recovery. Its
            # successful URL audio must remain identical during prefix replay.
            if (output/"metadata.json").exists() and (output/"response.json").exists():
                meta = json.loads((output/"metadata.json").read_text())
                response = json.loads((output/"response.json").read_text())
                if meta.get("http_status") == 500 and response.get("code") == "InternalError":
                    self.journal.commit(folder, target)
            raise
