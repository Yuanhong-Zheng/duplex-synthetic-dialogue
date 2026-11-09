"""Render a frozen V2 script, then compile its exact PCM consumption timeline.

There is no conversational model, listener, trigger observer or estimated reading
speed in this module.  All complete clips are synthesized independently; only
aligned, explicitly consumed assistant prefixes enter either role's history.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from dataclasses import asdict
import errno
import fcntl
import json
from pathlib import Path
import time
import urllib.error

import numpy as np

from duplex_runtime.alignment import rendered_spans
from duplex_runtime.audio_gain import scale_pcm16
from duplex_runtime.audio_validation import MAX_REGENERATIONS, METHOD, WORD_METHOD
from duplex_runtime.playback import TextSpan
from duplex_runtime.speech import SpeechError, SpeechRenderer, decode_wav, digest, encode_wav
from duplex_runtime.synthetic_rollout import SyntheticTimeline
from duplex_runtime.user_simulator import json_hash, write_json
from duplex_v2.anchor_punctuation import (LEGACY_SENTENCE_PUNCTUATION,
                                         is_unspoken_prose_attachment)


SAMPLE_RATE = 16000
DEFAULT_GAP_MS = 240
SCHEMA = 'duplex_v2.fixed_script.v1'
SCRIPT_SCHEMAS = frozenset((SCHEMA, 'duplex_v2.fixed_script.v2', 'duplex_v2.fixed_script.v3'))
ANCHOR_POLICY = 'exact_spoken_boundary_with_recorded_unspoken_punctuation_attachment_v1'
ANCHOR_POLICY_V25 = 'exact_spoken_boundary_with_recorded_context_checked_prose_punctuation_attachment_v2'
BACKCHANNEL_ALIGNMENT_STRICT = 'fail_case'
BACKCHANNEL_ALIGNMENT_WARNING = 'warn_and_omit_exhausted_alignment_v1'
SENTENCE_PUNCTUATION = LEGACY_SENTENCE_PUNCTUATION


class AudioPlanError(SpeechError):
    """The frozen plan cannot be realized without changing its meaning."""


class AnchorAlignmentError(AudioPlanError):
    """An exact script character boundary has no exact acoustic boundary."""


class AnchorScheduleError(AudioPlanError):
    """Frozen event anchors cannot fit the actual consumed clip intervals."""


def _validate_user_side_policy(policy):
    if policy is not None:
        from duplex_v2.user_side_speech import POLICY
        if policy != POLICY:
            raise AudioPlanError('unsupported frozen user side-speech policy')


def _validate_backchannel_policy(policy):
    if policy not in (BACKCHANNEL_ALIGNMENT_STRICT, BACKCHANNEL_ALIGNMENT_WARNING):
        raise AudioPlanError('unsupported frozen backchannel alignment failure policy')


def _backchannel_omission(job, root):
    """Recognize an exhausted local word-alignment repair, retaining its proof."""
    if job.get('kind') != 'backchannel':
        return None
    directory = root/'tts'/job['audio_id']
    failure_path = directory/'failure.json'
    if not failure_path.is_file() or (directory/'clip.json').exists():
        return None
    failure = json.loads(failure_path.read_text())
    if (failure.get('error_type') != 'TTSAlignmentError' or
            failure.get('audio_id') != job['audio_id'] or
            failure.get('identity_sha256') != json_hash(job) or
            not (directory/'job.json').is_file() or
            json.loads((directory/'job.json').read_text()) != job):
        return None
    for repair in sorted((directory/'calls').glob('*/word-alignment-repair')):
        evidence = [repair.parent/'rejected-word-alignment/alignment-validation.json']
        evidence += [repair/f'attempt-{i:02d}'/'alignment-validation.json'
                     for i in range(1, MAX_REGENERATIONS+1)]
        if not all(p.is_file() for p in evidence):
            continue
        if any(json.loads(p.read_text()).get('status') != 'failed' or
               json.loads(p.read_text()).get('method') != WORD_METHOD for p in evidence):
            continue
        return {'event_id': job['audio_id'], 'target_assistant_id': job['focus_turn_id'],
                'text': job['text'], 'code': 'backchannel_alignment_exhausted_omitted',
                'reason': '逐词对齐在原合成及两次同参数重合成后仍失败；本条附和未混入音频，也未进入已听历史。',
                'error_type': failure['error_type'], 'error': failure['error'],
                'failure_path': str(failure_path.relative_to(root)), 'failure_sha256': digest(failure_path),
                'identity_sha256': failure['identity_sha256'], 'max_regenerations': MAX_REGENERATIONS,
                'validation_files': [{'path': str(p.relative_to(root)), 'sha256': digest(p)} for p in evidence]}
    return None


def _validate_omitted_backchannels(script, assets, root, policy):
    _validate_backchannel_policy(policy)
    omitted = assets.get('omitted_backchannels', [])
    if (not isinstance(omitted, list) or
            (omitted and policy != BACKCHANNEL_ALIGNMENT_WARNING) or assets.get('failures')):
        raise AudioPlanError('unresolved TTS failures or unauthorized omitted backchannels')
    definitions = {e['event_id']: e for e in script.get('backchannels', [])}
    checked = {}
    for record in omitted:
        identifier = record.get('event_id') if isinstance(record, dict) else None
        if identifier not in definitions or identifier in checked or identifier in assets.get('clips', {}):
            raise AudioPlanError('omitted backchannel identity differs from the frozen script')
        path = root/'tts'/identifier/'job.json'
        job = json.loads(path.read_text())
        event = definitions[identifier]
        if (job.get('kind') != 'backchannel' or job.get('text') != event['text'] or
                job.get('focus_turn_id') != event['target_assistant_id'] or
                _backchannel_omission(job, root) != record):
            raise AudioPlanError('omitted backchannel evidence changed: '+identifier)
        checked[identifier] = record
    return checked


def _validate_anchor_policy(anchor_policy):
    if anchor_policy not in (ANCHOR_POLICY, ANCHOR_POLICY_V25):
        raise AudioPlanError('unsupported frozen anchor boundary policy')


def _resolve_anchor(text, spans, prefix, *, label='anchor', anchor_policy=ANCHOR_POLICY):
    """Resolve a source prefix; only unspoken attached punctuation is equivalent."""
    _validate_anchor_policy(anchor_policy)
    if not isinstance(prefix, str) or not prefix or not text.startswith(prefix):
        raise AnchorAlignmentError(f'{label}: anchor must be a nonempty exact full prefix')
    spans = [TextSpan(**s) if isinstance(s, dict) else s for s in spans]
    for span in spans:
        if span.end_char == len(prefix):
            return span.end_frame, prefix, None
    covering = next((s for s in spans if s.start_char < len(prefix) < s.end_char), None)
    suffix = text[len(prefix):covering.end_char] if covering else ''
    unspoken = (is_unspoken_prose_attachment(text, len(prefix), covering.end_char)
                if covering and anchor_policy == ANCHOR_POLICY_V25
                else bool(suffix) and all(c.isspace() or c in SENTENCE_PUNCTUATION for c in suffix))
    if unspoken:
        actual = text[:covering.end_char]
        warning = {'code': 'unspoken_punctuation_attached_to_aligned_word', 'label': label,
            'planned_prefix': prefix, 'actual_heard_prefix': actual,
            'planned_end_char': len(prefix), 'actual_end_char': covering.end_char,
            'actual_end_frame': covering.end_frame, 'attached_unspoken_text': suffix,
            'planned_text_boundary_exact': False, 'spoken_boundary_exact': True,
            'script_modified': False, 'word_boundary_moved': False}
        return covering.end_frame, actual, warning
    before = max([s.end_char for s in spans if s.end_char < len(prefix)] or [0])
    after = min([s.end_char for s in spans if s.end_char > len(prefix)] or [len(text)])
    raise AnchorAlignmentError(
        f'{label}: character {len(prefix)} falls inside an alignment span; '
        f'available surrounding boundaries are {before} and {after}; no boundary was moved')


def anchor_frame(text, spans, prefix, *, label='anchor', warnings=None, anchor_policy=ANCHOR_POLICY):
    """Return the exact spoken boundary, recording nonspoken attachment if supplied."""
    frame, _, warning = _resolve_anchor(text, spans, prefix, label=label, anchor_policy=anchor_policy)
    if warning is not None and warnings is not None:
        warnings.append(warning)
    return frame


def _boundary_summary(warnings, anchor_policy=ANCHOR_POLICY):
    return {'anchor_boundary_policy': anchor_policy,
        'planned_text_boundaries_exact': not bool(warnings),
        'spoken_anchor_boundaries_exact': True, 'anchor_boundary_warnings': warnings}


class _ClipRenderer(SpeechRenderer):
    """Isolated single-writer cache using the original SpeechRenderer T02 code.

    V2 admission is the frozen script audit, rather than a live simulator event.
    Consequently only the common asset and pause renderer methods are reused;
    no legacy environment, Guard pass or live user event is invented.
    """
    def __init__(self, directory, client):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in ('calls', 'cache', 'render'):
            (self.directory/name).mkdir(exist_ok=True)
        self.client = client
        self.settings = client.settings
        self.manifest = {'tts_alignment_policy': {'method': METHOD,
                         'max_regenerations': MAX_REGENERATIONS}}
        self.model_calls = len(list((self.directory/'calls').iterdir()))
        self.cache_hits = 0


def _verify_clip(root, clip):
    path = root/clip['path']
    if digest(path) != clip['sha256'] or len(decode_wav(path.read_bytes())) != clip['frames']:
        raise AudioPlanError('saved frozen clip changed: '+clip['audio_id'])
    for item in clip['files']:
        if digest(root/item['path']) != item['sha256']:
            raise AudioPlanError('saved TTS/alignment source changed: '+item['path'])


TRANSPORT_FAILURE = 'resident Qwen request failed; retained files must be inspected before retrying'
TRANSPORT_MAX_ATTEMPTS = 6


def _transport_cause(error):
    """Classify only the SDK's explicit uncommitted transport exception."""
    if type(error) is not SpeechError or str(error) != TRANSPORT_FAILURE:
        return None
    cause = error.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        return ({'type':type(cause).__name__,'status':cause.code}
                if cause.code in {429,500,502,503,504} else None)
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    if isinstance(cause, TimeoutError):
        return {'type':type(cause).__name__,'errno':getattr(cause,'errno',None)}
    if isinstance(cause, OSError) and cause.errno in {
            errno.ETIMEDOUT,errno.ECONNREFUSED,errno.ECONNRESET,errno.EPIPE,
            errno.EHOSTUNREACH,errno.ENETUNREACH}:
        return {'type':type(cause).__name__,'errno':cause.errno}
    if isinstance(cause,json.JSONDecodeError):
        return {'type':type(cause).__name__}
    return None


def _render_with_transport_retry(renderer, event, directory, identity):
    """Use new SDK call directories while retaining successful T02 caches."""
    state = directory/'transport-retry.json'
    with state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        parent_file=directory/'transport-parent.json'
        parent=json.loads(parent_file.read_text()) if parent_file.exists() else None
        initial=[]
        service_parent = directory/'service-transport-parent.json'
        if service_parent.exists():
            if parent is not None:
                raise AudioPlanError('cannot mix service recovery with another transport parent')
            from duplex_v2.service_recovery import validate_transport_parent
            parent, initial = validate_transport_parent(directory, identity)
        elif parent is not None:
            old=Path(parent['original_failure_file'])
            if (parent.get('identity_sha256')!=identity or
                    parent.get('audio_id')!=directory.name or
                    type(parent.get('prior_attempts')) is not int or
                    not 1<=parent['prior_attempts']<=TRANSPORT_MAX_ATTEMPTS or
                    digest(old)!=parent.get('original_failure_sha256')):
                raise AudioPlanError('original transport failure or budget changed')
            prior=json.loads(old.read_text())
            if (prior.get('error_type')!='SpeechError' or prior.get('error')!=TRANSPORT_FAILURE or
                    prior.get('identity_sha256')!=identity or prior.get('audio_id')!=directory.name):
                raise AudioPlanError('transport retry requires its exact original RPC failure')
            initial=[{'number':i,'status':'network_error','origin':'retained_parent_failure'}
                     for i in range(1,parent['prior_attempts']+1)]
        trace=json.loads(state.read_text()) if state.exists() else {
            'schema':'duplex_v2.clip_transport_retry.v1','identity_sha256':identity,
            'max_attempts':TRANSPORT_MAX_ATTEMPTS,'parent':parent,'attempts':initial,'status':'new'}
        attempts=trace.get('attempts',[])
        if (trace.get('identity_sha256')!=identity or trace.get('max_attempts')!=TRANSPORT_MAX_ATTEMPTS or
                trace.get('parent')!=parent or not isinstance(attempts,list) or
                len(attempts)>TRANSPORT_MAX_ATTEMPTS or attempts[:len(initial)]!=initial or
                any(row.get('number')!=i for i,row in enumerate(attempts,1))):
            raise AudioPlanError('transport retry identity/budget changed; no reset permitted')
        if trace.get('status') in {'running','completed','nonretryable_error'}:
            raise AudioPlanError('interrupted or finished transport attempt retained; inspect its existing artifacts')
        while len(attempts)<TRANSPORT_MAX_ATTEMPTS:
            number=len(attempts)+1
            if number>1:time.sleep(2**(number-2))
            row={'number':number,'status':'running','origin':'render_attempt'}
            attempts.append(row);trace.update(status='running',attempts=attempts)
            write_json(state,trace);started=time.monotonic()
            try:
                from duplex_v2.qwen_runtime import prime_client_health
                prime_client_health(renderer.client)
                result=renderer._main_audio(event,directory/'render')
            except Exception as error:
                from duplex_v2.qwen_runtime import invalidate_client_health
                invalidate_client_health(renderer.client,error)
                cause=_transport_cause(error)
                row.update(status='network_error' if cause else 'nonretryable_error',
                           error_type=type(error).__name__,cause=cause,
                           elapsed_seconds=time.monotonic()-started)
                trace['status']=row['status'];write_json(state,trace)
                if cause is None:raise
                continue
            row.update(status='completed',elapsed_seconds=time.monotonic()-started)
            trace['status']='completed';write_json(state,trace)
            return result
        trace['status']='exhausted';write_json(state,trace)
        raise AudioPlanError('clip transport exhausted six attempts; original calls and budget retained')


def _render_clip(job, root, client_factory):
    identifier = job['audio_id']
    directory = root/'tts'/identifier
    directory.mkdir(parents=True, exist_ok=True)
    identity = json_hash(job)
    final = directory/'clip.json'
    if final.exists():
        clip = json.loads(final.read_text())
        if clip['identity_sha256'] != identity:
            raise AudioPlanError('frozen clip identity changed: '+identifier)
        _verify_clip(root, clip)
        return clip
    if (directory/'failure.json').exists():
        raise AudioPlanError('previous clip failure retained; use an explicitly repaired new branch: '+identifier)
    write_json(directory/'job.json', job)
    started = time.monotonic()
    client, close = client_factory(job['settings'], directory/'worker')
    try:
        renderer = _ClipRenderer(directory, client)
        event = {'utterance': job['text'], 'dispatch': {'pause_cue': job.get('pause_cue')}}
        main = _render_with_transport_retry(renderer,event,directory,identity)
        spans, provenance = rendered_spans(directory, main)
        # The full-text request beside main.wav also describes a composite T02
        # asset; its exact segment requests and trims remain separately retained.
        write_json(directory/'render/request.json', client.settings.request(job['text']))
        files = [{'path': str(p.relative_to(root)), 'sha256': digest(p)}
                 for p in sorted(directory.rglob('*')) if p.is_file()]
        clip = {**{k: v for k, v in job.items() if k != 'settings'},
                'identity_sha256': identity, 'settings': job['settings'],
                'path': str((directory/main['path']).relative_to(root)),
                'frames': main['frames'], 'sha256': main['sha256'],
                'sample_rate': SAMPLE_RATE, 'alignment': [asdict(s) for s in spans],
                'alignment_provenance': provenance, 'rendered': main,
                'render_root': str(directory.relative_to(root)), 'files': files,
                'elapsed_seconds': time.monotonic()-started}
        write_json(final, clip)
        return clip
    except Exception as exc:
        write_json(directory/'failure.json', {'audio_id': identifier, 'error_type': type(exc).__name__,
                   'error': str(exc), 'identity_sha256': identity, 'old_files_retained': True})
        raise
    finally:
        close()


def _default_client_factory(options,synthesis_reuse_roots=None):
    from local_tts.backend import QwenSession, decode_settings
    from duplex_v2.qwen_transport import wrap_local_client
    def create(settings, directory):
        session = QwenSession(directory, options)
        if synthesis_reuse_roots:
            from local_tts.retained_synthesis import RetainedSynthesisClient
            client = RetainedSynthesisClient(session,decode_settings(settings),directory.parent.name,
                                             synthesis_reuse_roots)
        else:
            client = session.client(decode_settings(settings))
        return wrap_local_client(client,options,directory), session.close
    return create


def _jobs_and_voices(script, voice_profile, root, seed, reference_plan_overrides=None, language=None,
                     assistant_reference_root=None, user_side_speech_policy=None):
    _validate_user_side_policy(user_side_speech_policy)
    from local_tts.backend import assistant_reference_settings, profile_plan, validate_profile_plan
    overrides = reference_plan_overrides or {}
    rounds = script['rounds']
    if not isinstance(voice_profile, str) or not voice_profile.strip():
        raise AudioPlanError('the frozen Stage 1 user voice_profile is required')
    english = language == 'en'
    reference_text = 'We can take this one step at a time. Let me know what matters most to you.'
    voice_options = {'language': 'English'} if english else {}
    user = profile_plan(voice_profile, reference_text if english else rounds[0]['user_utterance'],
                        root/'voices/user-reference', seed, **voice_options)
    if 'user' in overrides:
        user = overrides['user']
        validate_profile_plan(user, voice_profile)
    assistant_options = {'language': 'English', 'reference_text': reference_text} if english else {}
    if assistant_reference_root is not None:
        assistant_options['reference_root'] = Path(assistant_reference_root).resolve()
    assistant = asdict(assistant_reference_settings('Aiden' if english else 'Serena', seed,
                                                  **assistant_options))
    plans = {'user': user, 'assistant': {'settings': assistant,
             'source': 'shared_fixed_Aiden_reference' if english else 'shared_fixed_Serena_reference',
             'fixed_for_whole_episode': True}}
    jobs = []
    for turn in rounds:
        jobs.extend([
            {'audio_id': turn['user_id'], 'focus_turn_id': turn['user_id'], 'kind': 'deliver',
             'speaker': 'user', 'text': turn['user_utterance'],
             'pause_cue': turn.get('pause_cue'), 'settings': user['settings']},
            {'audio_id': turn['assistant_id'], 'focus_turn_id': turn['user_id'], 'kind': 'assistant',
             'speaker': 'assistant', 'text': turn['assistant_utterance'],
             'pause_cue': None, 'settings': assistant}])
    for event in script.get('backchannels', []):
        jobs.append({'audio_id': event['event_id'], 'focus_turn_id': event['target_assistant_id'],
                     'kind': 'backchannel', 'speaker': 'user', 'text': event['text'],
                     'pause_cue': None, 'settings': user['settings']})
    side_plans = {}
    for event in script.get('side_speech', []):
        role = event['speaker']
        if role == 'user':
            if (user_side_speech_policy is None or script.get('schema_version') != 'duplex_v2.fixed_script.v3'
                    or not isinstance(event.get('recipient'), str) or not event['recipient'].strip()
                    or 'voice_profile' in event):
                raise AudioPlanError('user side speech requires its policy, recipient and inherited user voice')
            jobs.append({'audio_id': event['event_id'], 'focus_turn_id': event['target_assistant_id'],
                         'kind': 'side_speech', 'speaker': 'user', 'source_speaker': 'user',
                         'recipient': event['recipient'], 'speech_direction': 'user_to_third_party',
                         'expected_assistant_behavior': 'continue', 'stop_required': False,
                         'text': event['text'], 'pause_cue': None, 'settings': user['settings']})
            continue
        profile = event['voice_profile']
        if role not in {'third_party', 'machine'} or not isinstance(profile, str) or not profile.strip():
            raise AudioPlanError('side speech requires a separate third-party/machine voice profile')
        voice_identity = [event['source_id'], profile] if event.get('source_id') else profile
        key = role+'-'+json_hash(voice_identity)[:16]
        if key not in side_plans:
            side_plans[key] = profile_plan(profile, reference_text if english else event['text'],
                                           root/'voices'/key, seed, **voice_options)
            if key in overrides:
                side_plans[key] = overrides[key]
                validate_profile_plan(side_plans[key], profile)
        jobs.append({'audio_id': event['event_id'], 'focus_turn_id': event['target_assistant_id'],
                     'kind': 'side_speech', 'speaker': 'user', 'source_speaker': key,
                     'text': event['text'], 'pause_cue': None, 'settings': side_plans[key]['settings']})
    voice_ids = [user['settings']['voice_id'], assistant['voice_id'],
                 *[p['settings']['voice_id'] for p in side_plans.values()]]
    if len(voice_ids) != len(set(voice_ids)):
        raise AudioPlanError('physical speaker roles must have distinct pinned voices')
    plans['side_speech'] = side_plans
    if set(overrides)-{'user', *side_plans}:
        raise AudioPlanError('reference preparation contains an unknown physical speaker')
    if len({j['audio_id'] for j in jobs}) != len(jobs):
        raise AudioPlanError('audio IDs must be globally unique')
    return jobs, plans


def _validate_ambient_binding(script, plan):
    events = script.get('environment_events', [])
    if plan is None:
        if events:
            raise AudioPlanError('frozen environment events require their resolved ambient plan')
        return
    if not isinstance(plan, dict) or plan.get('schema') != 'duplex_v2.ambient_audio_plan.v1':
        raise AudioPlanError('a versioned resolved ambient audio plan is required')
    planned = plan.get('environment_events', [])
    if (not isinstance(planned, list) or len(planned) != len(events) or
            len({e.get('source_id') for e in events}) != len(events) or
            len({e.get('source_id') for e in planned}) != len(planned)):
        raise AudioPlanError('resolved environment events differ from the frozen script')
    by_id = {e['source_id']: e for e in planned}
    for event in events:
        resolved = by_id.get(event['source_id'], {})
        if any(resolved.get(key) != event.get(key) for key in ('target_audio_id', 'placement_fraction')):
            raise AudioPlanError('resolved environment event target or placement fraction changed')


def _append_ambient(timeline, plan, root):
    """Overlay acoustic sources after foreground consumption has been verified."""
    from duplex_v2.ambient import render_ambient
    total = max([timeline.frame, *[p['start_frame']+p['frames'] for p in timeline.placements]])
    additions, provenance = render_ambient(copy.deepcopy(plan), root,
        copy.deepcopy(timeline.placements), total, sample_rate=SAMPLE_RATE)
    if not isinstance(additions, list) or not isinstance(provenance, dict):
        raise AudioPlanError('ambient renderer returned no placement/provenance records')
    identifiers = {p['audio_id'] for p in timeline.placements}
    speech_roles = {p.get('source_speaker', p['speaker']) for p in timeline.placements}
    for placement in additions:
        kind = placement.get('kind')
        start, frames = placement.get('start_frame'), placement.get('frames')
        path = (root/placement['path']).resolve()
        if (kind not in {'ambient_layer', 'environment_event'} or placement.get('speaker') != 'user' or
                placement.get('background_only') is not True or placement['audio_id'] in identifiers or
                placement.get('source_speaker') in speech_roles or not placement.get('source_speaker') or
                type(start) is not int or type(frames) is not int or start < 0 or frames <= 0 or
                start+frames > total or not path.is_relative_to(root) or
                digest(path) != placement.get('sha256') or len(decode_wav(path.read_bytes())) != frames):
            raise AudioPlanError('ambient placement changed foreground timing, identity, bounds or PCM proof')
        if kind == 'ambient_layer' and (start != 0 or frames != total):
            raise AudioPlanError('a continuous ambient layer must cover the complete existing dialogue')
        identifiers.add(placement['audio_id'])
    counts = {kind: sum(p['kind'] == kind for p in additions) for kind in ('ambient_layer', 'environment_event')}
    if (counts['ambient_layer'] != len(plan.get('ambient_layers', [])) or
            counts['environment_event'] != len(plan.get('environment_events', []))):
        raise AudioPlanError('ambient renderer omitted a frozen layer or discrete event')
    timeline.placements.extend(additions)
    write_json(root/'ambient-provenance.json', provenance)
    return {'plan_sha256': json_hash(plan), 'frozen_plan_path': 'frozen-ambient-plan.json',
        'provenance_path': 'ambient-provenance.json',
        'provenance_sha256': digest(root/'ambient-provenance.json'),
        'ambient_layers': counts['ambient_layer'], 'environment_events': counts['environment_event'],
        'sources_added_to_heard_text': False, 'assistant_timing_changed': False}


def _render_fixed_script_once(script, output_dir, voice_profile, *, seed=20261103,
                        qwen_options=None, workers=16, gap_ms=DEFAULT_GAP_MS,
                        client_factory=None, ambient_plan=None, reference_plan_overrides=None,
                        anchor_policy=ANCHOR_POLICY,
                        backchannel_alignment_failure_policy=BACKCHANNEL_ALIGNMENT_STRICT,
                        speech_onset_policy=None,synthesis_reuse_roots=None,language=None,
                        assistant_reference_root=None, user_side_speech_policy=None):
    """Synthesize every frozen clip concurrently and compile its static timeline.

    Existing successful clip files are hash-verified and reused. Failed clips
    and exhausted same-parameter alignment recovery are retained, never erased.
    An injectable factory is for deterministic unit tests, not another runtime.
    """
    _validate_anchor_policy(anchor_policy)
    _validate_backchannel_policy(backchannel_alignment_failure_policy)
    _validate_user_side_policy(user_side_speech_policy)
    if user_side_speech_policy is not None and speech_onset_policy is None:
        raise AudioPlanError('user side speech requires frozen speech onset normalization')
    if script.get('schema_version') == 'duplex_v2.fixed_script.v3' and user_side_speech_policy is None:
        raise AudioPlanError('V2.9 audio requires its frozen user side-speech policy')
    if script.get('schema_version') not in SCRIPT_SCHEMAS or not script.get('rounds'):
        raise AudioPlanError('a nonempty frozen V2 fixed script is required')
    _validate_ambient_binding(script, ambient_plan)
    if type(workers) is not int or not 1 <= workers <= 64:
        raise AudioPlanError('clip concurrency must be within the resident pool capacity 1..64')
    if language not in (None, 'zh', 'en'):
        raise AudioPlanError('language must be zh or en')
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    manifest_path = root/'manifest.json'
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        options = existing['qwen_options']
        if existing.get('language') != language:
            raise AudioPlanError('frozen audio language changed')
        if qwen_options is not None and qwen_options != options:
            raise AudioPlanError('frozen Qwen engine configuration changed')
        if client_factory is None and options.get('engine_identity', {}).get('vllm_omni') == '0.28.0':
            from duplex_v2.qwen_runtime import activate_host_upgrade
            activate_host_upgrade()
    else:
        if client_factory is None and qwen_options is None:
            from duplex_v2.qwen_runtime import activate_host_upgrade
            activate_host_upgrade()
        if client_factory is None:
            from duplex_v2.qwen_runtime import discover_runtime_options, discovery_binding
            options = discover_runtime_options(qwen_options,
                evidence_path=root.with_name(root.name+'-engine-discovery.json'),
                binding=discovery_binding(script,voice_profile,seed,language,qwen_options))
        else:
            options = qwen_options or {}
    voice_options = {'language': language} if language is not None else {}
    if assistant_reference_root is not None:
        voice_options['assistant_reference_root'] = assistant_reference_root
    if user_side_speech_policy is not None:
        voice_options['user_side_speech_policy'] = user_side_speech_policy
    jobs, voices = _jobs_and_voices(script, voice_profile, root, seed, reference_plan_overrides,
                                  **voice_options)
    identity_fields = {'script': script, 'voice_profile': voice_profile, 'seed': seed,
                       'qwen_options': options, 'gap_ms': gap_ms}
    if language is not None:
        identity_fields['language'] = language
    if ambient_plan is not None:
        identity_fields['ambient_plan'] = ambient_plan
    if reference_plan_overrides:
        identity_fields['reference_plan_overrides'] = reference_plan_overrides
    if assistant_reference_root is not None:
        identity_fields['assistant_reference_root'] = str(Path(assistant_reference_root).resolve())
    # Preserve legacy identity bytes. The opt-in policy changes scheduling,
    # while independently frozen TTS clip identities remain untouched.
    if anchor_policy != ANCHOR_POLICY:
        identity_fields['anchor_boundary_policy'] = anchor_policy
    if backchannel_alignment_failure_policy != BACKCHANNEL_ALIGNMENT_STRICT:
        identity_fields['backchannel_alignment_failure_policy'] = backchannel_alignment_failure_policy
    if speech_onset_policy is not None:
        from duplex_v2.speech_onset import POLICY
        if speech_onset_policy != POLICY:
            raise AudioPlanError('unsupported frozen speech onset policy')
        identity_fields['speech_onset_policy'] = speech_onset_policy
    if synthesis_reuse_roots:
        identity_fields['synthesis_reuse_roots'] = [str(Path(p).resolve()) for p in synthesis_reuse_roots]
    if user_side_speech_policy is not None:
        identity_fields['user_side_speech_policy'] = user_side_speech_policy
    identity = json_hash(identity_fields)
    if manifest_path.exists() and existing['identity_sha256'] != identity:
        raise AudioPlanError('frozen audio plan identity changed; use a new branch')
    manifest = {'schema': 'duplex_v2.audio_manifest.v1', 'identity_sha256': identity,
                'seed': seed, 'qwen_options': options, 'voices': voices,
                'mode': 'frozen_script_parallel_TTS_static_PCM_timeline',
                'tts_backend': 'qwen3', 'clip_concurrency': workers, 'pool_capacity': 64,
                'gap_ms': gap_ms, 'runtime_text_model_calls': 0,
                'api_wait_advances_synthetic_clock': False,
                'native_realtime_latency_measured': False, 'native_server_cancellation': False,
                'alignment_source': 'qwen3_forced_aligner_0.6b_posthoc',
                'background_audio_is_context': False,
                'full_assistant_synthesized_before_prefix_crop': True,
                'status': 'synthesizing'}
    discovery_path = root.with_name(root.name+'-engine-discovery.json')
    if discovery_path.is_file():
        discovery = json.loads(discovery_path.read_text())
        if discovery.get('status') != 'completed' or discovery.get('runtime_options') != options:
            raise AudioPlanError('runtime discovery evidence differs from the frozen options')
        manifest['runtime_discovery'] = {'policy':discovery['policy'],
            'receipt_path':str(discovery_path), 'receipt_sha256':digest(discovery_path)}
    if anchor_policy != ANCHOR_POLICY:
        manifest['anchor_boundary_policy'] = anchor_policy
    if language is not None:
        manifest['language'] = language
    if assistant_reference_root is not None:
        manifest['assistant_reference_root'] = str(Path(assistant_reference_root).resolve())
    if backchannel_alignment_failure_policy != BACKCHANNEL_ALIGNMENT_STRICT:
        manifest['backchannel_alignment_failure_policy'] = backchannel_alignment_failure_policy
    if speech_onset_policy is not None:
        manifest['speech_onset_policy'] = speech_onset_policy
    if user_side_speech_policy is not None:
        manifest['user_side_speech_policy'] = user_side_speech_policy
    if ambient_plan is not None:
        manifest['ambient_plan_sha256'] = json_hash(ambient_plan)
        write_json(root/'frozen-ambient-plan.json', ambient_plan)
    write_json(manifest_path, manifest)
    write_json(root/'frozen-script.json', script)
    write_json(root/'voice-plan.json', voices)
    factory = client_factory or _default_client_factory(options,synthesis_reuse_roots)
    clips, failures, omitted = {}, [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_render_clip, job, root, factory): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            identifier = job['audio_id']
            try:
                clips[identifier] = future.result()
            except Exception as exc:
                warning = (_backchannel_omission(job, root)
                           if backchannel_alignment_failure_policy == BACKCHANNEL_ALIGNMENT_WARNING else None)
                if warning is not None:
                    omitted.append(warning)
                else:
                    failures.append({'audio_id': identifier, 'error_type': type(exc).__name__, 'error': str(exc)})
    assets = {'schema': 'duplex_v2.audio_assets.v1', 'clips': dict(sorted(clips.items())), 'failures': failures}
    if backchannel_alignment_failure_policy == BACKCHANNEL_ALIGNMENT_WARNING:
        assets['omitted_backchannels'] = sorted(omitted, key=lambda e: e['event_id'])
    if user_side_speech_policy is not None:
        assets['user_side_speech_policy'] = user_side_speech_policy
    write_json(root/'assets.json', assets)
    if failures:
        result = {'status': 'failed', 'phase': 'parallel_tts', 'failures': failures,
                  'wall_seconds': time.monotonic()-started, 'delivered_turns': []}
        write_json(root/'result.json', result)
        manifest['status'] = 'failed'; write_json(manifest_path, manifest)
        raise AudioPlanError('parallel TTS failed; all complete and failed clips retained: '+str(failures))
    try:
        if speech_onset_policy is not None:
            from duplex_v2.speech_onset import normalize_assets
            assets = normalize_assets(script, assets, root, root)
            write_json(root/'assets.json',assets)
        result = simulate_fixed_script(script, assets, root, gap_ms=gap_ms,
                                       ambient_plan=ambient_plan, anchor_policy=anchor_policy,
                                       backchannel_alignment_failure_policy=backchannel_alignment_failure_policy)
    except Exception as exc:
        write_json(root/'result.json', {'status': 'failed', 'phase': 'static_audio_schedule',
                   'failure_reason': str(exc), 'error_type': type(exc).__name__,
                   'wall_seconds': time.monotonic()-started, 'delivered_turns': []})
        manifest['status'] = 'failed'; write_json(manifest_path, manifest)
        raise
    result['wall_seconds'] = time.monotonic()-started
    write_json(root/'result.json', result)
    manifest.update(status='completed', **_boundary_summary(result['anchor_boundary_warnings'], anchor_policy))
    write_json(manifest_path, manifest)
    return result


def render_fixed_script(script, output_dir, voice_profile, *, seed=20261103,
                        qwen_options=None, workers=16, gap_ms=DEFAULT_GAP_MS,
                        client_factory=None, ambient_plan=None, anchor_policy=ANCHOR_POLICY,
                        backchannel_alignment_failure_policy=BACKCHANNEL_ALIGNMENT_STRICT,
                        speech_onset_policy=None,synthesis_reuse_roots=None,reference_plan_overrides=None,language=None,
                        assistant_reference_root=None, user_side_speech_policy=None):
    """Render once, or resume one approved reference-only recovery branch."""
    _validate_anchor_policy(anchor_policy)
    _validate_backchannel_policy(backchannel_alignment_failure_policy)
    root = Path(output_dir).resolve()
    retained_manifest = root/'manifest.json'
    if retained_manifest.exists() and json.loads(retained_manifest.read_text()).get(
            'anchor_boundary_policy', ANCHOR_POLICY) != anchor_policy:
        raise AudioPlanError('frozen audio plan identity changed; use a new branch')
    if retained_manifest.exists() and json.loads(retained_manifest.read_text()).get(
            'backchannel_alignment_failure_policy', BACKCHANNEL_ALIGNMENT_STRICT) != backchannel_alignment_failure_policy:
        raise AudioPlanError('frozen backchannel warning policy changed; use a new branch')
    if retained_manifest.exists() and json.loads(retained_manifest.read_text()).get('speech_onset_policy') != speech_onset_policy:
        raise AudioPlanError('frozen speech onset policy changed; use a new branch')
    _validate_user_side_policy(user_side_speech_policy)
    if retained_manifest.exists() and json.loads(retained_manifest.read_text()).get('user_side_speech_policy') != user_side_speech_policy:
        raise AudioPlanError('frozen user side-speech policy changed; use a new branch')
    recovery_file = root/'reference-recovery.json'
    options = dict(seed=seed, qwen_options=qwen_options, workers=workers, gap_ms=gap_ms,
                   client_factory=client_factory, ambient_plan=ambient_plan,
                   anchor_policy=anchor_policy,
                   backchannel_alignment_failure_policy=backchannel_alignment_failure_policy,
                   speech_onset_policy=speech_onset_policy,synthesis_reuse_roots=synthesis_reuse_roots,language=language)
    if assistant_reference_root is not None:
        options['assistant_reference_root'] = assistant_reference_root
    if user_side_speech_policy is not None:
        options['user_side_speech_policy'] = user_side_speech_policy
    if not recovery_file.exists():
        try:
            retained_failure = root/'result.json'
            if (client_factory is None and retained_failure.exists() and
                    json.loads(retained_failure.read_text()).get('phase') == 'parallel_tts'):
                raise AudioPlanError('resume the retained parallel TTS failure without overwriting it')
            return _render_fixed_script_once(script, root, voice_profile,
                                            reference_plan_overrides=reference_plan_overrides,**options)
        except AudioPlanError:
            if client_factory is not None or not (root/'result.json').exists():
                raise
            failed = json.loads((root/'result.json').read_text())
            if failed.get('phase') != 'parallel_tts':
                raise
            from local_tts.backend import QwenSession
            from local_tts.reference_audio import shorten_profile_reference
            manifest = json.loads((root/'manifest.json').read_text())
            plans = manifest['voices']
            profiles = {'user': plans['user'], **plans.get('side_speech', {})}
            session = QwenSession(root/'reference-preparation/asr', manifest['qwen_options'])
            overrides, receipts = {}, {}
            try:
                for role, plan in profiles.items():
                    prepared = shorten_profile_reference(plan, root/'reference-preparation'/role,
                        lambda text, audio: session.asr.call({'operation': 'align', 'text': text, 'audio': audio,
                            **({'language': 'English'} if language == 'en' else {})}))
                    if prepared:
                        overrides[role], receipts[role] = prepared
            finally:
                session.close()
            if not overrides:
                raise
            write_json(recovery_file, {'schema': 'duplex_v2.reference_recovery.v1',
                'parent_audio_identity_sha256': manifest['identity_sha256'],
                'audio_directory': str(root/'audio-short-reference'),
                'reference_plan_overrides': overrides, 'reference_preparations': receipts,
                'original_audio_and_reference_retained': True})
    recovery = json.loads(recovery_file.read_text())
    parent = json.loads((root/'manifest.json').read_text())
    if recovery['parent_audio_identity_sha256'] != parent['identity_sha256']:
        raise AudioPlanError('reference recovery source audio plan changed')
    try:
        result = _render_fixed_script_once(script, recovery['audio_directory'], voice_profile,
            reference_plan_overrides=recovery['reference_plan_overrides'], **options)
    except AnchorAlignmentError as error:
        error.audio_directory = recovery['audio_directory']
        raise
    result.update(audio_directory=recovery['audio_directory'],
                  reference_preparations=recovery['reference_preparations'])
    write_json(Path(recovery['audio_directory'])/'result.json', result)
    return result


def simulate_fixed_script(script, assets, output_dir, *, gap_ms=DEFAULT_GAP_MS, ambient_plan=None,
                          anchor_policy=ANCHOR_POLICY,
                          backchannel_alignment_failure_policy=BACKCHANNEL_ALIGNMENT_STRICT):
    """Consume aligned PCM exactly at the frozen script anchors, without GPT."""
    _validate_anchor_policy(anchor_policy)
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _validate_user_side_policy(assets.get('user_side_speech_policy'))
    if script.get('schema_version') == 'duplex_v2.fixed_script.v3' and not assets.get('user_side_speech_policy'):
        raise AudioPlanError('new user side-speech script lacks frozen audio policy')
    omitted = _validate_omitted_backchannels(script, assets, root, backchannel_alignment_failure_policy)
    _validate_ambient_binding(script, ambient_plan)
    if ambient_plan is not None:
        frozen_plan = root/'frozen-ambient-plan.json'
        if frozen_plan.exists() and json.loads(frozen_plan.read_text()) != ambient_plan:
            raise AudioPlanError('frozen ambient plan changed; use a new audio branch')
        if not frozen_plan.exists():
            write_json(frozen_plan, ambient_plan)
    if type(gap_ms) is not int or gap_ms < 0:
        raise AudioPlanError('the declared between-turn gap must be a nonnegative integer')
    gap, remainder = divmod(gap_ms*SAMPLE_RATE, 1000)
    if remainder:
        raise AudioPlanError('between-turn gap is not sample-exact')
    clips = assets.get('clips', assets)
    acoustic_evidence = {e['user_id']:e for e in assets.get('interruption_speech_evidence',[])}
    onset_proofs = []
    aside_acoustic = {e['event_id']: e for e in assets.get('user_side_speech_evidence', [])}
    aside_proofs = []
    if assets.get('speech_onset_policy'):
        from duplex_v2.speech_onset import POLICY, calibrate_interruption
        if assets['speech_onset_policy'] != POLICY:
            raise AudioPlanError('unsupported frozen speech onset assets')
    rounds = script['rounds']
    by_assistant = {t['assistant_id']: t for t in rounds}
    if len(by_assistant) != len(rounds):
        raise AudioPlanError('assistant response IDs must be unique')
    for identifier, clip in clips.items():
        path = root/clip['path']
        if digest(path) != clip['sha256'] or len(decode_wav(path.read_bytes())) != clip['frames']:
            raise AudioPlanError('source PCM changed: '+identifier)
    starts, played, onsets = {}, {}, {}
    anchor_warnings = []
    def resolve(text, spans, prefix, *, label):
        frame, actual, warning = _resolve_anchor(text, spans, prefix, label=label,
                                                anchor_policy=anchor_policy)
        if warning is not None:
            anchor_warnings.append(warning)
        return frame, actual
    for index, turn in enumerate(rounds):
        aid, uid = turn['assistant_id'], turn['user_id']
        assistant, user = clips[aid], clips[uid]
        if assistant['text'] != turn['assistant_utterance'] or user['text'] != turn['user_utterance']:
            raise AudioPlanError('frozen script and TTS text differ')
        if not index:
            user_start, previous_end = 0, 0
            if turn.get('timing'):
                raise AudioPlanError('the first user has no prior assistant target')
        else:
            previous = rounds[index-1]['assistant_id']
            timing = turn.get('timing') or {}
            if timing.get('target_assistant_id') != previous:
                raise AudioPlanError(uid+': main user must target the preceding response')
            before = clips[previous]
            onset_prefix = timing.get('user_onset_after_text')
            if 'T03' in turn['timing_refs']:
                onset_frame, actual_onset = resolve(before['text'], before['alignment'], onset_prefix, label=uid+' onset')
                stop_prefix = timing.get('assistant_stop_after_text')
                stop_frame, actual_stop = resolve(before['text'], before['alignment'], stop_prefix, label=uid+' stop')
                if not onset_frame <= stop_frame < before['frames']:
                    raise AudioPlanError(uid+': true interruption requires onset <= stop < full PCM')
                if len(actual_onset) > len(actual_stop) or len(actual_stop) >= len(before['text']):
                    raise AudioPlanError(uid+': true interruption must leave an unplayed textual suffix')
                user_start = starts[previous]+onset_frame
                if uid in acoustic_evidence:
                    proof = calibrate_interruption(acoustic_evidence[uid],onset_frame,stop_frame,before['alignment'])
                    user_start += proof['shift_frames']
                    onset_proofs.append({'user_id':uid, 'assistant_id':previous,
                        'planned_anchor_frame':starts[previous]+onset_frame,
                        'user_pcm_start_frame':user_start,
                        'user_speech_onset_frame':user_start+user['speech_onset_frame'],
                        'assistant_stop_frame':starts[previous]+stop_frame,
                        'removed_leading_frames':acoustic_evidence[uid]['removed_leading_frames'],
                        'semantic_anchor_changed':False, 'assistant_stop_changed':False, **proof})
                played[previous] = stop_frame
            elif 'T01' in turn['timing_refs']:
                if onset_prefix != before['text'] or timing.get('assistant_stop_after_text') not in (None, before['text']):
                    raise AudioPlanError(uid+': T01 follows the entire preceding assistant')
                played[previous] = before['frames']
                user_start = starts[previous]+before['frames']+gap
                actual_onset = actual_stop = before['text']
            else:
                raise AudioPlanError(uid+': exactly one main timing T01/T03 is required')
            previous_end = starts[previous]+played[previous]
            onsets[uid] = {'target_assistant_id': previous, 'user_onset_after_text': onset_prefix,
                          'assistant_stop_after_text': before['text'] if 'T01' in turn['timing_refs']
                                                       else timing['assistant_stop_after_text'],
                          'actual_user_onset_after_text': actual_onset,
                          'actual_assistant_stop_after_text': actual_stop}
        starts[uid] = user_start
        starts[aid] = max(user_start+user['frames'], previous_end)+gap
        played[aid] = assistant['frames']

    placements = []
    events = []
    def add_event(frame, priority, kind, **payload):
        events.append((frame, priority, kind, payload))
    for turn in rounds:
        uid, aid = turn['user_id'], turn['assistant_id']
        clip = clips[uid]
        placement = {'speaker': 'user', 'audio_id': uid, 'focus_turn_id': uid,
            'start_frame': starts[uid], 'frames': clip['frames'], 'path': clip['path'],
            'text': clip['text'], 'kind': 'deliver', 'pause': clip.get('rendered', {}).get('pause')}
        speech_offset = clip.get('speech_onset_frame',0)
        if 'speech_onset_frame' in clip:
            placement['speech_onset_frame'] = starts[uid]+speech_offset
            placement['onset_normalization'] = clip['normalization']
        placements.append(placement)
        add_event(starts[uid]+speech_offset, 2, 'user_onset', uid=uid)
        add_event(starts[uid]+clip['frames'], 0, 'user_complete', uid=uid)
        add_event(starts[aid], 1, 'assistant_start', aid=aid, uid=uid)
        add_event(starts[aid]+played[aid], 0, 'assistant_end', aid=aid,
                  interrupted=played[aid] != clips[aid]['frames'])

    target_events = [('backchannel', e) for e in script.get('backchannels', [])]
    target_events += [('side_speech', e) for e in script.get('side_speech', [])]
    event_definitions = {event['event_id']: event for _, event in target_events}
    actual_event_prefixes = {}
    for kind, event in target_events:
        identifier, aid = event['event_id'], event['target_assistant_id']
        if identifier in omitted:
            continue
        if aid not in by_assistant:
            raise AudioPlanError(identifier+': event targets an unknown response')
        clip, host = clips[identifier], clips[aid]
        relative, actual_event_prefixes[identifier] = resolve(host['text'], host['alignment'], event['after_text'], label=identifier)
        if kind == 'side_speech' and event['speaker'] == 'user':
            if identifier not in aside_acoustic or not assets.get('speech_onset_policy'):
                raise AudioPlanError(identifier+': user side speech lacks verified speech-onset evidence')
            from duplex_v2.speech_onset import calibrate_interruption
            calibration = calibrate_interruption(aside_acoustic[identifier], relative, played[aid], host['alignment'])
            if calibration['warning']:
                raise AnchorScheduleError(identifier+': no true speech overlap within the unchanged assistant prefix')
            relative += calibration['shift_frames']
            aside_proofs.append({'event_id': identifier, 'target_assistant_id': aid,
                'planned_after_text': event['after_text'], 'actual_assistant_prefix': actual_event_prefixes[identifier],
                'user_pcm_start_frame': starts[aid]+relative,
                'user_speech_onset_frame': starts[aid]+relative+clip['speech_onset_frame'],
                'calibration': calibration, 'semantic_anchor_changed': False,
                'assistant_audio_changed': False, 'expected_assistant_behavior': 'continue'})
        start, end = starts[aid]+relative, starts[aid]+relative+clip['frames']
        if relative+clip['frames'] > played[aid]:
            raise AnchorScheduleError(identifier+': complete clip does not fit consumed assistant host; no crop or move permitted')
        if clip['text'] != event['text']:
            raise AudioPlanError(identifier+': frozen event and TTS text differ')
        placement = {'speaker': 'user', 'audio_id': identifier, 'focus_turn_id': by_assistant[aid]['user_id'],
            'start_frame': start, 'frames': clip['frames'], 'path': clip['path'], 'text': clip['text'],
            'kind': kind, 'target_assistant_id': aid, 'source_path': clip['path']}
        if kind == 'side_speech':
            gain = event['gain']
            if type(gain) not in (int, float) or not .3 <= gain <= .7:
                raise AudioPlanError(identifier+': background amplitude must be in 0.3..0.7')
            if event['speaker'] not in {'third_party', 'machine', 'user'}:
                raise AudioPlanError('unsupported side-speech physical speaker')
            if event['speaker'] == 'user' and not assets.get('user_side_speech_policy'):
                raise AudioPlanError('user turning aside is not enabled')
            path = root/'background'/f'{identifier}.wav'; path.parent.mkdir(exist_ok=True)
            path.write_bytes(encode_wav(scale_pcm16(decode_wav((root/clip['path']).read_bytes()), gain)))
            placement.update(path=str(path.relative_to(root)), source_speaker=clip.get('source_speaker', event['speaker']),
                background_only=True, gain={'method': 'pcm16_linear_amplitude_v1', 'factor': gain},
                background_schedule={'method': 'frozen_exact_prefix_contained_background_v2',
                    'host_audio_id': aid, 'host_start_frame': starts[aid],
                    'host_end_frame': starts[aid]+played[aid], 'start_frame': start, 'frames': clip['frames']})
        if kind == 'side_speech' and event['speaker'] == 'user':
            if clip.get('source_speaker') != 'user' or clip.get('settings') != clips[by_assistant[aid]['user_id']].get('settings'):
                raise AudioPlanError(identifier+': user side speech must reuse the physical user voice')
            placement.update(source_speaker='user', recipient=event['recipient'],
                speech_direction='user_to_third_party', expected_assistant_behavior='continue',
                stop_required=False)
            if clip.get('speech_onset_frame') is not None:
                placement['speech_onset_frame'] = start+clip['speech_onset_frame']
        placements.append(placement)
        add_event(start, 2, kind+'_onset', identifier=identifier, aid=aid)
        add_event(end, 0, kind+'_complete', identifier=identifier, aid=aid)

    # Interval validation is independent of amplitude: leading/trailing silence
    # never licenses two utterances by the same physical person at once.
    physical = defaultdict(list)
    for placement in placements:
        role = placement.get('source_speaker', placement['speaker'])
        for old in physical[role]:
            if max(old['start_frame'], placement['start_frame']) < min(
                    old['start_frame']+old['frames'], placement['start_frame']+placement['frames']):
                raise AnchorScheduleError(f"same physical speaker clips overlap: {old['audio_id']} / {placement['audio_id']}; no event moved")
        physical[role].append(placement)

    timeline = SyntheticTimeline(root)
    timeline.alignment_source = 'qwen3_forced_aligner_0.6b_posthoc;'+anchor_policy
    timeline.placements.extend(placements)
    for record in omitted.values():
        timeline.log('backchannel_omitted', **record, audio_played=False, added_to_heard_context=False)
    heard, user_results, event_heard = {}, [], []
    for frame, _, kind, payload in sorted(events, key=lambda e: (e[0], e[1], e[2])):
        timeline.advance(frame)
        if kind == 'assistant_start':
            aid, uid = payload['aid'], payload['uid']
            clip = clips[aid]
            heard.setdefault(uid, {})['assistant_history_before_reply'] = timeline.context()
            timeline.start_response(aid, clip['text'], decode_wav((root/clip['path']).read_bytes()),
                [TextSpan(**s) for s in clip['alignment']], root/clip['path'])
        elif kind == 'assistant_end':
            if payload['interrupted']:
                if not timeline.active or timeline.active['id'] != payload['aid']:
                    raise AudioPlanError('interrupted response is no longer the active PCM queue')
                timeline.stop('frozen_exact_stop_prefix')
        elif kind == 'user_onset':
            uid = payload['uid']
            record = {'user_id': uid, 'onset_frame': frame, 'user_history_at_onset': timeline.context()}
            if uid in acoustic_evidence:
                record.update(audio_start_frame=starts[uid], speech_onset_offset_frames=clips[uid]['speech_onset_frame'],
                    speech_onset_policy=assets['speech_onset_policy'])
            if uid in onsets:
                expected = onsets[uid]
                target = expected['target_assistant_id']
                ledger = timeline.last_response['ledger']
                if timeline.last_response['id'] != target or ledger.heard_text != expected['actual_user_onset_after_text']:
                    raise AudioPlanError(uid+': consumed user-onset prefix differs from frozen script')
                record.update(target_assistant_id=target,
                    assistant_prefix_at_user_onset=ledger.heard_text,
                    assistant_prefix_at_stop=expected['actual_assistant_stop_after_text'],
                    planned_assistant_prefix_at_user_onset=expected['user_onset_after_text'],
                    planned_assistant_prefix_at_stop=expected['assistant_stop_after_text'],
                    assistant_stop_frame=starts[target]+played[target],
                    planned_unplayed_suffix=clips[target]['text'][len(expected['actual_assistant_stop_after_text']):])
                if expected['user_onset_after_text'] != clips[target]['text']:
                    record['assistant_stop_delay_ms'] = (record['assistant_stop_frame']-frame)*1000/SAMPLE_RATE
            heard[uid] = record
            timeline.log('user_audio_started', audio_id=uid, focus_turn_id=uid,
                overlap_with_assistant=timeline.is_speaking,
                assistant_prefix_at_user_onset=record.get('assistant_prefix_at_user_onset', ''),
                planned_assistant_prefix_at_user_onset=record.get('planned_assistant_prefix_at_user_onset', ''))
        elif kind == 'user_complete':
            uid = payload['uid']; clip = clips[uid]
            timeline.hear('user', uid, clip['text'], frame)
            user_results.append({'event': {'schema': 'duplex_v2.frozen_user_event.v1',
                'sequence': next(i+1 for i, t in enumerate(rounds) if t['user_id']==uid),
                'focus_turn_id': uid, 'action': 'deliver', 'utterance': clip['text'],
                'source': 'audited_frozen_script', 'guard_called_at_runtime': False},
                'rendered': {'main': clip.get('rendered'), 'side_speech': [],
                             'source_root': clip.get('render_root')}, 'audio_id': uid})
            timeline.log('user_audio_completed', audio_id=uid)
        elif kind == 'backchannel_complete':
            identifier = payload['identifier']
            timeline.hear('user', identifier, clips[identifier]['text'], frame)
            timeline.log('backchannel_completed', audio_id=identifier,
                         response_id=payload['aid'], assistant_continues=True,
                         new_assistant_response=False, advances_main_turn=False)
        elif kind in {'backchannel_onset', 'side_speech_onset'}:
            identifier = payload['identifier']
            definition = event_definitions[identifier]
            if not timeline.last_response or timeline.last_response['id'] != payload['aid']:
                raise AudioPlanError(identifier+': event target is not the current response')
            prefix = timeline.last_response['ledger'].heard_text
            if prefix != actual_event_prefixes[identifier]:
                raise AudioPlanError(identifier+': actual event-onset prefix differs from the frozen script')
            event_heard.append({'event_id': identifier, 'kind': kind, 'onset_frame': frame,
                'target_assistant_id': payload['aid'], 'assistant_prefix_at_onset': prefix,
                'planned_assistant_prefix_at_onset': definition['after_text'],
                'history_at_onset': timeline.context(),
                'event_added_to_assistant_context': kind == 'backchannel_onset'})
            if definition.get('speaker') == 'user' and kind == 'side_speech_onset':
                event_heard[-1].update(speaker='user', recipient=definition['recipient'],
                    expected_assistant_behavior='continue', stop_required=False,
                    advances_main_turn=False, new_assistant_response=False)
            timeline.log(kind, **payload, assistant_prefix_at_onset=prefix,
                         planned_assistant_prefix_at_onset=definition['after_text'],
                         background_only=kind == 'side_speech_onset')
        else:
            timeline.log(kind, **payload, background_only=kind.startswith('side_speech'))
    response_by_id = {r['response_id']: r for r in timeline.responses}
    for uid, record in heard.items():
        if uid in onsets:
            response = response_by_id[record['target_assistant_id']]
            if response['heard_text'] != record['assistant_prefix_at_stop']:
                raise AudioPlanError(uid+': actual stopped prefix differs from the frozen script')
            record['assistant_stop_ledger'] = response
    ambient = _append_ambient(timeline, ambient_plan, root) if ambient_plan is not None else None
    audio = timeline.write_audio()
    timeline.save()
    write_json(root/'heard-context.json', list(heard.values()))
    write_json(root/'event-heard-context.json', event_heard)
    write_json(root/'user-results.json', user_results)
    counts = {'main_turns': len(rounds), 'backchannels': len(script.get('backchannels', []))-len(omitted),
              'side_speech': len(script.get('side_speech', [])),
              'interruptions': sum('T03' in t['timing_refs'] for t in rounds)}
    if assets.get('user_side_speech_policy'):
        counts['user_side_speech'] = sum(e['speaker'] == 'user' for e in script.get('side_speech', []))
    if ambient is not None:
        counts.update(ambient_layers=ambient['ambient_layers'],
            environment_events=ambient['environment_events'],
            discrete_background_events=ambient['environment_events']+counts['side_speech'])
    integrity = {'status': 'passed', 'exact_anchor_boundaries': not bool(anchor_warnings),
        'heard_context_uses_only_consumed_prefixes': True,
        'background_complete_and_contained': True, 'background_added_to_context': False,
        'backchannel_stops_assistant': False, 'same_speaker_overlap': False,
        'pcm_clipping': False, 'runtime_model_calls': 0, **_boundary_summary(anchor_warnings, anchor_policy)}
    if ambient is not None:
        integrity.update(real_environment_sources_verified=True, environment_audio_stops_assistant=False,
            continuous_layers_counted_as_discrete=False, ambient_plan_sha256=ambient['plan_sha256'])
    write_json(root/'integrity.json', integrity)
    result = {'schema': 'duplex_v2.compiled_audio_result.v1', 'status': 'completed',
              'delivered_turns': [t['user_id'] for t in rounds], 'audio': audio,
              'counts': counts, 'integrity': integrity, 'gap_ms': gap_ms,
              'heard_context_path': 'heard-context.json', 'failure_reason': None,
              **_boundary_summary(anchor_warnings, anchor_policy)}
    if backchannel_alignment_failure_policy == BACKCHANNEL_ALIGNMENT_WARNING:
        warning_summary = {'backchannel_alignment_failure_policy': backchannel_alignment_failure_policy,
                           'omitted_backchannels': list(omitted.values())}
        counts.update(planned_backchannels=len(script.get('backchannels', [])), omitted_backchannels=len(omitted))
        integrity.update(warning_summary)
        result.update(warning_summary)
        write_json(root/'integrity.json', integrity)
    if assets.get('user_side_speech_policy'):
        integrity['user_side_speech_stops_assistant'] = False
        integrity['user_side_speech_added_to_context'] = False
        result['user_side_speech_policy'] = assets['user_side_speech_policy']
        proof_path = root/'user-side-speech-onset.json'
        write_json(proof_path, {'policy': assets['user_side_speech_policy'], 'events': aside_proofs,
            'runtime_model_calls': 0})
        result.update(user_side_speech_onset_path=proof_path.name,
                      user_side_speech_onset_sha256=digest(proof_path))
        integrity['user_side_speech_onset_sha256'] = digest(proof_path)
        write_json(root/'integrity.json', integrity)
    if ambient is not None:
        result['ambient_audio'] = ambient
    if assets.get('speech_onset_policy'):
        proof_path = root/'interruption-onset.json'
        write_json(proof_path, {'policy':assets['speech_onset_policy'], 'interruptions':onset_proofs,
            'warnings':[p for p in onset_proofs if p['warning']], 'runtime_model_calls':0})
        acoustic = {'speech_onset_policy':assets['speech_onset_policy'],
            'interruption_onset_path':proof_path.name, 'interruption_onset_sha256':digest(proof_path),
            'acoustic_interruption_warnings':sum(bool(p['warning']) for p in onset_proofs),
            'speech_overlap_verified':all(not p['warning'] for p in onset_proofs)}
        integrity.update(acoustic)
        result.update(acoustic)
        write_json(root/'integrity.json',integrity)
    write_json(root/'result.json', result)
    return result


def _verified_existing_assets(root):
    assets = json.loads((root/'assets.json').read_text())
    if assets.get('failures'):
        raise AudioPlanError('anchor-only recompilation requires complete original TTS assets')
    if assets.get('omitted_backchannels'):
        manifest = json.loads((root/'manifest.json').read_text())
        _validate_omitted_backchannels(json.loads((root/'frozen-script.json').read_text()), assets, root,
            manifest.get('backchannel_alignment_failure_policy', BACKCHANNEL_ALIGNMENT_STRICT))
    originals = copy.deepcopy(assets)
    for identifier, clip in assets['clips'].items():
        recorded = json.loads((root/'tts'/identifier/'clip.json').read_text())
        originals['clips'][identifier] = recorded
        if recorded != clip and not assets.get('speech_onset_policy'):
            raise AudioPlanError('asset inventory differs from its frozen clip record: '+identifier)
        _verify_clip(root, recorded)
        _verify_clip(root, clip)
    if assets.get('speech_onset_policy'):
        from duplex_v2.speech_onset import verify_normalized_assets
        for key in ('speech_onset_policy','interruption_speech_evidence','user_side_speech_evidence'):
            originals.pop(key,None)
        verify_normalized_assets(json.loads((root/'frozen-script.json').read_text()), originals,assets,root,root)
    return assets


def recompile_with_existing_assets(new_script, parent_audio_dir, new_audio_dir, *, anchor_policy=None,
                                   speech_onset_policy=None, stop_selection=None, source=None):
    """Recompile verified clips; optional stop changes need exact proof + Judge."""
    parent = Path(parent_audio_dir).resolve()
    root = Path(new_audio_dir).resolve()
    if root.exists() or root.is_relative_to(parent) or parent.is_relative_to(root):
        raise AudioPlanError('anchor recompilation requires a new separate audio branch')
    previous = json.loads((parent/'frozen-script.json').read_text())
    if stop_selection is not None:
        from duplex_v2.audio_stop import apply_selected_stops, verify_parent_script
        verify_parent_script(previous, source or {}, stop_selection)
        review = stop_selection.get('review', {})
        decision = Path(review.get('decision_file', ''))
        base_receipt = {key:value for key,value in stop_selection.items() if key != 'review'}
        reviewed_source = {**(source or {}), 'audio_stop_selection':base_receipt}
        if (not decision.is_file() or digest(decision) != review.get('decision_sha256') or
                json.loads(decision.read_text()) != {'decision':'pass','findings':[]} or
                review.get('candidate_script_sha256') != json_hash(new_script) or
                review.get('source_sha256') != json_hash(reviewed_source)):
            raise AudioPlanError('exact stop compilation requires its bound original Judge approval')
        if apply_selected_stops(previous, stop_selection, source or {}) != new_script:
            raise AudioPlanError('exact stop selection differs from its verified candidate')
        recorded = stop_selection.get('parent_artifacts_sha256', {})
        if (set(recorded) != {'manifest.json','assets.json','frozen-script.json','result.json'} or
                any(digest(parent/name) != value for name,value in recorded.items())):
            raise AudioPlanError('exact stop selection parent audio changed')
    elif previous != new_script:
        raise AudioPlanError('recompilation cannot change anchors, spoken text, voices, events or other frozen fields')
    assets = _verified_existing_assets(parent)
    old_manifest = json.loads((parent/'manifest.json').read_text())
    backchannel_policy = old_manifest.get('backchannel_alignment_failure_policy', BACKCHANNEL_ALIGNMENT_STRICT)
    if anchor_policy is None:
        anchor_policy = old_manifest.get('anchor_boundary_policy', ANCHOR_POLICY)
    _validate_anchor_policy(anchor_policy)
    if stop_selection is not None:
        from duplex_v2.audio_stop import select_exact_stops
        selected_now, actual_selection = select_exact_stops(previous, source, assets, anchor_policy)
        recorded_selection = {key:value for key,value in stop_selection.items()
                              if key not in {'parent_artifacts_sha256','review',
                                             'parent_script_directory','parent_script_artifacts_sha256'}}
        if selected_now != new_script or actual_selection != recorded_selection:
            raise AudioPlanError('selected stop differs from the actual retained alignment boundaries')
    ambient_plan = None
    if 'ambient_plan_sha256' in old_manifest:
        ambient_plan = json.loads((parent/'frozen-ambient-plan.json').read_text())
        if json_hash(ambient_plan) != old_manifest['ambient_plan_sha256']:
            raise AudioPlanError('retained ambient plan differs from its original manifest')
    expected = {}
    for row in new_script['rounds']:
        expected[row['user_id']] = row['user_utterance']
        expected[row['assistant_id']] = row['assistant_utterance']
    for collection in ('backchannels', 'side_speech'):
        expected.update({e['event_id']:e['text'] for e in new_script.get(collection, [])})
    for omitted in assets.get('omitted_backchannels', []):
        expected.pop(omitted['event_id'])
    if set(expected) != set(assets['clips']) or any(assets['clips'][k]['text'] != v for k,v in expected.items()):
        raise AudioPlanError('frozen script text/IDs differ from the retained TTS assets')
    parent_hashes = {name: digest(parent/name) for name in
                     ('manifest.json', 'assets.json', 'frozen-script.json', 'result.json')}
    recompile_identity = {'script':new_script, 'parent_artifact_sha256':parent_hashes}
    if stop_selection is not None:
        recompile_identity['exact_stop_selection'] = stop_selection
    if anchor_policy != ANCHOR_POLICY:
        recompile_identity['anchor_boundary_policy'] = anchor_policy
    if speech_onset_policy is not None:
        from duplex_v2.speech_onset import POLICY, RECOMPILE_MODE
        if speech_onset_policy != POLICY or assets.get('speech_onset_policy'):
            raise AudioPlanError('unsupported or already applied speech onset policy')
        recompile_identity['speech_onset_policy'] = speech_onset_policy
    manifest = copy.deepcopy(old_manifest)
    manifest.update(schema='duplex_v2.audio_manifest.v1',
        identity_sha256=json_hash(recompile_identity),
        status='recompiling', mode='anchor_only_recompile_existing_verified_TTS',
        parent_audio_directory=str(parent), parent_artifact_sha256=parent_hashes,
        reused_tts_root=str((parent/'tts').resolve()), new_tts_requests=0,
        tts_assets_read_only=True)
    manifest['anchor_boundary_policy'] = anchor_policy
    if stop_selection is not None:
        manifest.update(mode='exact_program_stop_recompile_existing_verified_TTS',
                        exact_stop_selection=stop_selection)
    if speech_onset_policy is not None:
        manifest.update(mode=RECOMPILE_MODE, speech_onset_policy=speech_onset_policy)
    root.mkdir(parents=True)
    # Keep lexical new-root paths for SyntheticTimeline.relative_to(), while
    # all synthesis and aligner records remain in their untouched old archive.
    (root/'tts').symlink_to((parent/'tts').resolve(), target_is_directory=True)
    if assets.get('speech_onset_policy') and speech_onset_policy is None:
        # A punctuation-only branch still needs the already-normalized user
        # suffixes; linking original TTS alone cannot reconstruct these paths.
        import shutil
        for clip in assets['clips'].values():
            if clip.get('normalization'):
                destination=root/clip['path'];destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(parent/clip['path'],destination)
    if speech_onset_policy is not None:
        from duplex_v2.speech_onset import normalize_assets,verify_normalized_assets
        normalized = normalize_assets(new_script, assets, parent, root)
        verify_normalized_assets(new_script,assets,normalized,parent,root)
        assets = normalized
    write_json(root/'manifest.json', manifest)
    write_json(root/'assets.json', assets)
    write_json(root/'frozen-script.json', new_script)
    if ambient_plan is not None:
        write_json(root/'frozen-ambient-plan.json', ambient_plan)
    if (parent/'voice-plan.json').exists():
        write_json(root/'voice-plan.json', json.loads((parent/'voice-plan.json').read_text()))
    started = time.monotonic()
    try:
        result = simulate_fixed_script(new_script, assets, root, gap_ms=manifest['gap_ms'],
                                       ambient_plan=ambient_plan, anchor_policy=anchor_policy,
                                       backchannel_alignment_failure_policy=backchannel_policy)
        if any(digest(parent/name) != value for name,value in parent_hashes.items()):
            raise AudioPlanError('parent audio artifacts changed during anchor-only recompilation')
        _verified_existing_assets(parent)
    except Exception as exc:
        write_json(root/'result.json', {'status':'failed', 'phase':'static_audio_schedule',
            'failure_reason':str(exc), 'error_type':type(exc).__name__,
            'new_tts_requests':0, 'delivered_turns':[], 'wall_seconds':time.monotonic()-started})
        manifest['status'] = 'failed'; write_json(root/'manifest.json', manifest)
        raise
    result.update(new_tts_requests=0, wall_seconds=time.monotonic()-started)
    write_json(root/'result.json', result)
    manifest.update(status='completed', **_boundary_summary(result['anchor_boundary_warnings'], anchor_policy))
    write_json(root/'manifest.json', manifest)
    return result
