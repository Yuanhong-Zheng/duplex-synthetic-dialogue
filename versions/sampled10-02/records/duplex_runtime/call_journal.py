"""Durable per-episode call sequence; exact replay across attempts, never across episodes."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import time

from duplex_runtime.speech import SpeechError, digest, sanitize
from duplex_runtime.user_simulator import json_hash, write_json


class JournalError(ValueError):
    pass


class CallJournal:
    def __init__(self, root, namespace, *, prompt_revisions=()):
        self.root = Path(root)/namespace
        self.root.mkdir(parents=True, exist_ok=True)
        self.cursor = 0
        self.prompt_revisions = tuple(prompt_revisions)

    def entry(self, request):
        self.cursor += 1
        folder = self.root/f"{self.cursor:05d}"
        folder.mkdir(exist_ok=True)
        path = folder/"identity.json"
        if path.exists() and json.loads(path.read_text()) != request:
            previous = json.loads(path.read_text())
            before = hashlib.sha256(previous.get("prompt", "").encode()).hexdigest()
            after = hashlib.sha256(request.get("prompt", "").encode()).hexdigest()
            revision = next((r for r in self.prompt_revisions if r["old_prompt_sha256"] == before and r["new_prompt_sha256"] == after), None)
            if revision is None or {k:v for k,v in previous.items() if k != "prompt"} != {k:v for k,v in request.items() if k != "prompt"}:
                raise JournalError("resume request differs from durable sequence; original records retained")
            # An explicitly approved new Guard prompt gets its own actual call;
            # the original identity, refusal and every committed file stay intact.
            folder = folder/"policy-revisions"/after
            folder.mkdir(parents=True, exist_ok=True)
            path = folder/"identity.json"
            write_json(folder/"revision-authorization.json", revision)
        if path.exists():
            if json.loads(path.read_text()) != request:
                raise JournalError("revised policy request differs from its immutable record")
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
    def __init__(self, client, journal_root, namespace, *, prompt_revisions=()):
        self.client = client; self.journal = CallJournal(journal_root, namespace, prompt_revisions=prompt_revisions)

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

    def synthesize_validated(self,text,output,*,validator,validation_key,max_regenerations=2):
        from duplex_runtime.audio_validation import TTSAlignmentError
        if max_regenerations != 2:raise JournalError('TTS alignment repair is limited to the two authorized regenerations')
        start_cursor=self.journal.cursor
        entry=self.journal.root/f'{start_cursor+1:05d}'
        policy_file=entry/'tts-quality-policy.json'
        number=0
        while True:
            policy=json.loads(policy_file.read_text()) if policy_file.exists() else None
            if policy and (policy['validation_key']!=validation_key or policy['max_regenerations']!=max_regenerations):
                raise JournalError('TTS validation context changed during recovery')
            trials=len(list((entry/'tries').glob('*'))) if entry.exists() else 0
            if policy and not(entry/'commit.json').exists() and trials-policy['baseline_trial_count']>=max_regenerations:
                raise TTSAlignmentError('T02 alignment repair exhausted two identical-parameter regenerations')
            candidate=Path(output)/'validation-attempts'/f'{number:03d}';candidate.mkdir(parents=True)
            self.journal.cursor=start_cursor
            raw=self.synthesize(text,candidate)
            (candidate/'candidate.wav').write_bytes(raw)
            try:
                validator(raw,candidate)
            except TTSAlignmentError as exc:
                if policy is None:
                    policy={'validation_key':validation_key,'max_regenerations':max_regenerations,
                            'baseline_trial_count':len(list((entry/'tries').glob('*'))),
                            'identity_sha256':digest(entry/'identity.json')}
                    write_json(policy_file,policy)
                revisions=entry/'tts-validation';revisions.mkdir(exist_ok=True)
                revision=revisions/f'revision-{len(list(revisions.glob("revision-*")))+1:03d}';revision.mkdir()
                commit=json.loads((entry/'commit.json').read_text())
                write_json(revision/'rejection.json',{'reason':str(exc),'validation_key':validation_key,
                    'identity_sha256':digest(entry/'identity.json'),'rejected_result_directory':commit['result_directory']})
                (entry/'commit.json').replace(revision/'commit-before.json')
                write_json(candidate/'alignment-validation.json',{'status':'failed','reason':str(exc),'validation_key':validation_key})
                number+=1
                continue
            for p in candidate.iterdir():
                if p.is_file():shutil.copyfile(p,Path(output)/p.name)
            total=len(list((entry/'tries').glob('*')))
            write_json(Path(output)/'tts-quality.json',{'validation_key':validation_key,'max_regenerations':max_regenerations,
                'regenerations':total-policy['baseline_trial_count'] if policy else 0,'status':'passed',
                'same_tts_request':True,'journal_entry':str(entry)})
            return raw

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
