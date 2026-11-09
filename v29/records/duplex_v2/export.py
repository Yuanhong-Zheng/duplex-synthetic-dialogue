"""Export consumed V2 prefixes as scripted targets, never live policy rewards."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import wave

import numpy as np

from duplex_runtime.playback import TextSpan
from duplex_runtime.speech import decode_wav, digest
from duplex_runtime.user_simulator import write_json
from duplex_runtime.user_simulator import json_hash


class ConsumedExportError(ValueError):
    pass


def _read(path):
    return json.loads(path.read_text())


def _require(condition, message):
    if not condition:
        raise ConsumedExportError(message)


def _within(root, name, reused_tts=None):
    path = (root/name).resolve()
    if not path.is_relative_to(root):
        # An anchor-only branch reuses precisely the frozen parent TTS assets.
        # Every other external path remains forbidden, including arbitrary
        # files elsewhere inside the same parent audio directory.
        _require(reused_tts is not None and (root/name).is_relative_to(root/'tts')
                 and path in reused_tts and digest(path) == reused_tts[path],
                 'audio artifact path escapes its source directory')
    return path


def _verify_exact_stop_reuse(root, parent, manifest, assets):
    """Authorize only the selected stops and their bound original-Judge pass."""
    from duplex_v2.audio import ANCHOR_POLICY
    from duplex_v2.audio_stop import (apply_selected_stops, select_exact_stops,
                                      verify_parent_script)
    receipt = manifest.get('exact_stop_selection', {})
    original = _read(parent/'frozen-script.json')
    selected = _read(root/'frozen-script.json')
    script_root = Path(receipt.get('parent_script_directory', ''))
    source = _read(script_root/'source.json')
    verify_parent_script(original, source, receipt)
    parent_result = _read(parent/'result.json')
    _require(parent_result.get('status') == 'failed' and
             parent_result.get('phase') == 'static_audio_schedule' and
             parent_result.get('error_type') == 'AnchorAlignmentError',
             'exact stop export has no retained original boundary failure')
    _require(receipt.get('parent_artifacts_sha256') == manifest['parent_artifact_sha256'],
             'exact stop receipt differs from its authorized parent artifacts')
    _require(apply_selected_stops(original, receipt, source) == selected,
             'exact stop export changed fields outside its selected stop proof')
    base_receipt = {key:value for key,value in receipt.items() if key != 'review'}
    reviewed_source = {**source, 'audio_stop_selection':base_receipt}
    review = receipt.get('review', {})
    decision_path = Path(review.get('decision_file', ''))
    approval = {'decision':'pass', 'findings':[]}
    _require(decision_path.is_file() and digest(decision_path) == review.get('decision_sha256') and
             _read(decision_path) == approval and
             review.get('candidate_script_sha256') == json_hash(selected) and
             review.get('source_sha256') == json_hash(reviewed_source),
             'exact stop export lost its bound original Judge approval')
    # The receipt's hashes must also describe the actual recorded Judge input,
    # rather than an unrelated passing decision with the same short JSON body.
    judge_root = decision_path.parent/'review/judge'
    judge_input = _read(judge_root/'input.json')
    _require(judge_input.get('script') == selected and
             judge_input.get('frozen_source') == reviewed_source and
             _read(judge_root/'result.json') == approval,
             'exact stop export differs from the original Judge input/result')
    actual_script, actual_receipt = select_exact_stops(original, source, assets,
        manifest.get('anchor_boundary_policy', ANCHOR_POLICY))
    selection = {key:value for key,value in receipt.items() if key not in {
        'review', 'parent_artifacts_sha256', 'parent_script_directory', 'parent_script_artifacts_sha256'}}
    _require(actual_script == selected and actual_receipt == selection,
             'exact stop export differs from the retained alignment boundaries')


def _authorized_reused_tts(root):
    path = root/'manifest.json'
    if not path.exists():
        return None
    manifest = _read(path)
    from duplex_v2.speech_onset import RECOMPILE_MODE
    anchor_mode = 'anchor_only_recompile_existing_verified_TTS'
    exact_stop_mode = 'exact_program_stop_recompile_existing_verified_TTS'
    if manifest.get('mode') not in (anchor_mode, exact_stop_mode, RECOMPILE_MODE):
        return None
    parent = Path(manifest['parent_audio_directory']).resolve()
    tts_root = Path(manifest['reused_tts_root']).resolve()
    _require(manifest.get('new_tts_requests') == 0 and manifest.get('tts_assets_read_only') is True,
             'reused TTS branch changed its zero-synthesis policy')
    _require((root/'tts').is_symlink() and (root/'tts').resolve() == tts_root
             and (parent/'tts').resolve() == tts_root,
             'reused TTS link differs from its manifest-authorized parent')
    hashes = manifest['parent_artifact_sha256']
    _require(set(hashes) == {'manifest.json','assets.json','frozen-script.json','result.json'},
             'reused TTS parent proof is incomplete')
    _require(all(digest(parent/name) == value for name,value in hashes.items()),
             'reused TTS parent artifacts changed')
    assets = _read(parent/'assets.json')
    _require(not assets.get('failures'), 'reused TTS parent has unresolved failures')
    if manifest['mode'] == RECOMPILE_MODE:
        from duplex_v2.speech_onset import POLICY, verify_normalized_assets
        _require(manifest.get('speech_onset_policy') == POLICY,
                 'normalized audio branch changed its frozen policy')
        verify_normalized_assets(_read(root/'frozen-script.json'),assets,
                                 _read(root/'assets.json'),parent,root)
    else:
        _require(assets == _read(root/'assets.json'), 'reused TTS asset inventory changed')
    if manifest['mode'] == exact_stop_mode:
        _verify_exact_stop_reuse(root, parent, manifest, assets)
    allowed = {}
    for clip in assets['clips'].values():
        clip_path = (parent/clip['path']).resolve()
        normalized_copy=(clip.get('normalization') and manifest['mode'] in (anchor_mode, exact_stop_mode))
        _require((clip_path.is_relative_to(tts_root) or normalized_copy) and digest(clip_path) == clip['sha256'],
                 'reused TTS clip changed or escapes its authorized root')
        if normalized_copy:
            _require(clip['path'] == 'normalized-users/'+clip['audio_id']+'.wav' and
                     clip_path.is_relative_to(parent/'normalized-users'),
                     'normalized user suffix escapes its authorized parent directory')
            copied=(root/clip['path']).resolve()
            _require(copied.is_relative_to(root) and digest(copied)==clip['sha256'],
                     'normalized user suffix missing or changed in reused TTS branch')
        for item in clip['files']:
            artifact = (parent/item['path']).resolve()
            _require(artifact.is_relative_to(tts_root) and digest(artifact) == item['sha256'],
                     'reused TTS synthesis/alignment record changed')
        if clip_path.is_relative_to(tts_root):
            allowed[clip_path] = clip['sha256']
    return allowed


def _history_at(observed, frame):
    history = []
    for item in sorted((i for i in observed if i['through_frame'] <= frame),
                       key=lambda i: i['through_frame']):
        if history and all(history[-1][k] == item[k] for k in ('speaker', 'audio_id')):
            history[-1]['text'] += item['text']
            history[-1]['through_frame'] = item['through_frame']
        else:
            history.append(dict(item))
    return history


def _ambient_export_evidence(source, result, timeline, reused_tts):
    """Bind the actual noisy dialogue and each acoustic source to the export."""
    ambient = result['ambient_audio']
    plan_path = _within(source, ambient['frozen_plan_path'])
    provenance_path = _within(source, ambient['provenance_path'])
    _require(json_hash(_read(plan_path)) == ambient['plan_sha256'], 'frozen ambient plan changed before export')
    _require(digest(provenance_path) == ambient['provenance_sha256'], 'ambient source provenance changed before export')
    mixed_path = _within(source, result['audio']['path'])
    _require(digest(mixed_path) == result['audio']['sha256'], 'actual mixed dialogue changed before export')
    with wave.open(str(mixed_path), 'rb') as handle:
        _require((handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getcomptype()) ==
                 (2, 2, 16000, 'NONE'), 'mixed dialogue must be uncompressed stereo 16k PCM16')
        frames = handle.getnframes()
        actual = np.frombuffer(handle.readframes(frames), dtype='<i2').reshape(-1, 2)
    _require(frames == result['audio']['frames'] and len(actual) == frames, 'mixed dialogue clock differs from its manifest')
    rebuilt = np.zeros((frames, 2), dtype=np.int32)
    background = []
    for placement in timeline['placements']:
        pcm = decode_wav(_within(source, placement['path'], reused_tts).read_bytes())
        start, length = placement['start_frame'], placement['frames']
        _require(type(start) is int and type(length) is int and 0 <= start and
                 start+length <= frames and len(pcm) == length, 'mixed source PCM bounds changed')
        rebuilt[start:start+length, 0 if placement['speaker'] == 'user' else 1] += pcm
        if placement.get('background_only'):
            if placement.get('kind') in {'ambient_layer', 'environment_event'}:
                _require(digest(_within(source, placement['path'])) == placement.get('sha256'),
                         'rendered environmental source changed before export')
            background.append(dict(placement))
    _require(not np.any((rebuilt < -32768) | (rebuilt > 32767)) and
             np.array_equal(actual, rebuilt.astype('<i2')), 'mixed dialogue does not reconstruct from its actual PCM sources')
    attribution = {'schema': 'duplex_v2.background_attribution.v1',
        'ambient_plan_sha256': ambient['plan_sha256'], 'placements': background,
        'environment_provenance': _read(provenance_path),
        'background_in_heard_text': False, 'continuous_layers_are_discrete_events': False}
    voice_path = source/'voice-plan.json'
    if voice_path.exists():
        attribution['third_party_voice_plans'] = _read(voice_path).get('side_speech', [])
    return mixed_path, attribution, {name: digest(source/name) for name in
        (ambient['frozen_plan_path'], ambient['provenance_path'])}


def _export_full_duplex_input(source, target, result, integrity, timeline, reused_tts, ambient_verified):
    """Expose the actual synthetic user-channel PCM separately from supervision-only labels."""
    from duplex_v2.user_side_speech import POLICY
    policy = result.get('user_side_speech_policy')
    _require(policy == POLICY and _read(source/'manifest.json').get('user_side_speech_policy') == policy,
             'full-duplex input requires its frozen user side-speech policy')
    mixed = _within(source, result['audio']['path'])
    _require(digest(mixed) == result['audio']['sha256'], 'full-duplex mixed PCM changed')
    with wave.open(str(mixed), 'rb') as handle:
        _require((handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (2, 2, 16000),
                 'full-duplex input requires the actual stereo PCM clock')
        actual = np.frombuffer(handle.readframes(handle.getnframes()), dtype='<i2').reshape(-1, 2)
    _require(len(actual) == result['audio']['frames'], 'full-duplex input clock differs from its result')
    if not ambient_verified:
        rebuilt = np.zeros(actual.shape, dtype=np.int32)
        for placement in timeline['placements']:
            pcm = decode_wav(_within(source, placement['path'], reused_tts).read_bytes())
            start, count = placement['start_frame'], placement['frames']
            _require(len(pcm) == count and 0 <= start < start+count <= len(actual),
                     'full-duplex source lies outside the audio clock')
            rebuilt[start:start+count, 0 if placement['speaker'] == 'user' else 1] += pcm
        _require(not np.any((rebuilt < -32768) | (rebuilt > 32767)) and
                 np.array_equal(actual, rebuilt.astype('<i2')), 'full-duplex PCM differs from its sources')
    proof_path = _within(source, result['user_side_speech_onset_path'])
    _require(digest(proof_path) == result['user_side_speech_onset_sha256'] ==
             integrity.get('user_side_speech_onset_sha256'), 'user side-speech onset proof changed')
    proof_rows = _read(proof_path)['events']
    proofs = {e['event_id']: e for e in proof_rows}
    _require(len(proofs) == len(proof_rows), 'duplicate user side-speech onset proofs')
    events = []
    hosts = {p['audio_id']: p for p in timeline['placements'] if p.get('speaker') == 'assistant'}
    for placement in timeline['placements']:
        if placement.get('speech_direction') != 'user_to_third_party':
            continue
        identifier = placement['audio_id'];proof = proofs.get(identifier, {})
        host = hosts.get(placement.get('target_assistant_id'), {})
        _require(placement.get('source_speaker') == 'user' and placement.get('background_only') is True and
                 placement.get('expected_assistant_behavior') == 'continue' and
                 placement.get('stop_required') is False and isinstance(placement.get('recipient'), str) and
                 proof.get('user_pcm_start_frame') == placement['start_frame'] and
                 proof.get('user_speech_onset_frame') == placement['speech_onset_frame'] and
                 proof.get('assistant_audio_changed') is False and
                 host.get('start_frame', -1) <= placement['start_frame'] and
                 placement['start_frame']+placement['frames'] <= host.get('start_frame', 0)+host.get('frames', 0),
                 'user side speech differs from its continuation supervision')
        events.append({'event_id': identifier, 'speaker': 'user', 'recipient': placement['recipient'],
            'event_type': 'user_to_third_party', 'text': placement['text'],
            'start_frame': placement['start_frame'], 'speech_onset_frame': placement['speech_onset_frame'],
            'end_frame': placement['start_frame']+placement['frames'],
            'target_assistant_id': placement['target_assistant_id'],
            'expected_assistant_behavior': 'continue', 'stop_required': False,
            'advances_main_turn': False, 'new_assistant_response': False})
    _require(set(proofs) == {e['event_id'] for e in events}, 'user side-speech supervision inventory changed')
    input_path = target/'audio/user-input.wav';input_path.parent.mkdir(parents=True, exist_ok=True)
    from duplex_runtime.speech import encode_wav
    raw = encode_wav(actual[:, 0].copy())
    if input_path.exists():
        _require(input_path.read_bytes() == raw, 'previous full-duplex input changed')
    else:
        input_path.write_bytes(raw)
    supervision = {'schema': 'duplex_v2.user_side_speech_supervision.v1',
        'policy': policy, 'sample_rate': 16000, 'frames': len(actual),
        'annotation_usage': 'supervision_only_not_model_input', 'events': events,
        'live_policy_evaluation': False}
    supervision_path = target/'user-side-speech-supervision.json'
    if supervision_path.exists():
        _require(_read(supervision_path) == supervision, 'previous side-speech supervision changed')
    else:
        write_json(supervision_path, supervision)
    # The target track uses the same clock, including the existing A continuation.
    output_path = target/'audio/assistant-output.wav'
    output_raw = encode_wav(actual[:, 1].copy())
    if output_path.exists():
        _require(output_path.read_bytes() == output_raw, 'previous full-duplex target changed')
    else:
        output_path.write_bytes(output_raw)
    return {'input_audio': {'path': 'audio/user-input.wav', 'sha256': digest(input_path),
                'sample_rate': 16000, 'channels': 1, 'frames': len(actual),
                'includes_user_side_speech': bool(events), 'user_side_speech_count': len(events)},
            'target_audio': {'path': 'audio/assistant-output.wav', 'sha256': digest(output_path),
                'sample_rate': 16000, 'channels': 1, 'frames': len(actual)},
            'event_supervision': {'path': supervision_path.name, 'sha256': digest(supervision_path),
                'usage': 'supervision_only_not_model_input'},
            'aligned_clock': True, 'data_kind': 'scripted_target_behavior'}


def export_consumed_dataset(audio_dir: Path, output_dir: Path) -> dict:
    """Verify receipt-bound histories and copy only actually played A audio.

    Full scripts, source facts, full synthesis and unplayed suffixes are not
    included in this dataset. Their existing audio archive remains separate.
    """
    source, target = Path(audio_dir).resolve(), Path(output_dir).resolve()
    _require(target != source and not target.is_relative_to(source),
             'training export must be separate from the full audio archive')
    result = _read(source/'result.json')
    integrity = _read(source/'integrity.json')
    timeline = _read(source/'timeline.json')
    contexts = _read(source/'heard-context.json')
    reused_tts = _authorized_reused_tts(source)
    _require(result.get('status') == 'completed' and integrity.get('status') == 'passed',
             'only completed, verified PCM timelines may be exported')
    _require(timeline.get('sample_rate') == 16000, 'unsupported PCM clock')
    placements = timeline['placements']
    placement_by_id = {p['audio_id']: p for p in placements}
    _require(len(placement_by_id) == len(placements), 'duplicate placement IDs')
    if result.get('speech_onset_policy'):
        from duplex_v2.audio import _verified_existing_assets
        _verified_existing_assets(source)
        proof_path = _within(source,result['interruption_onset_path'])
        _require(digest(proof_path) == result['interruption_onset_sha256'] and
                 integrity.get('interruption_onset_sha256') == result['interruption_onset_sha256'],
                 'interruption acoustic evidence changed before export')
        by_user = {c['user_id']:c for c in contexts}
        for item in _read(proof_path)['interruptions']:
            placement = placement_by_id[item['user_id']]
            _require(placement['start_frame'] == item['user_pcm_start_frame'] and
                     placement['speech_onset_frame'] == item['user_speech_onset_frame'] ==
                     by_user[item['user_id']]['onset_frame'] and
                     by_user[item['user_id']]['assistant_stop_frame'] == item['assistant_stop_frame'] and
                     item['semantic_anchor_changed'] is False and item['assistant_stop_changed'] is False,
                     'actual speech onset/stop differs from its unchanged-anchor evidence')
    omitted = result.get('omitted_backchannels', [])
    if omitted:
        from duplex_v2.audio import _validate_omitted_backchannels
        _require(integrity.get('omitted_backchannels') == omitted, 'omitted backchannel warning differs from integrity')
        assets = _read(source/'assets.json')
        _require(assets.get('omitted_backchannels') == omitted, 'omitted backchannel warning differs from assets')
        _validate_omitted_backchannels(_read(source/'frozen-script.json'), assets, source,
                                     result.get('backchannel_alignment_failure_policy'))
        _require(not any(item['event_id'] in placement_by_id for item in omitted), 'an omitted backchannel has a PCM placement')
    responses = {r['response_id']: r for r in timeline['responses']}
    assistant_placements = {p['audio_id']: p for p in placements if p['speaker'] == 'assistant'}
    _require(set(responses) == set(assistant_placements), 'assistant placement/ledger IDs differ')
    _require(len(responses) == len(timeline['responses']), 'duplicate assistant ledger IDs')
    _require(len(contexts) == len(responses), 'each assistant reply needs one role-visible history')

    # Verify the cropped waveform against the explicit consumption receipt and
    # against its complete synthesis source, without exporting that source.
    consumed_paths = {}
    for aid, response in responses.items():
        placement = assistant_placements[aid]
        pcm_path = _within(source, placement['path'], reused_tts)
        pcm = decode_wav(pcm_path.read_bytes())
        full = decode_wav(_within(source, placement['source_path'], reused_tts).read_bytes())
        count = response['played_frames']
        _require(len(pcm) == count == placement['frames'], aid+': consumed PCM frame count differs')
        _require(response['received_frames'] == len(full), aid+': full source frame count differs')
        _require(hashlib.sha256(pcm.astype('<i2').tobytes()).hexdigest() == response['played_pcm_sha256'],
                 aid+': consumed PCM hash differs from playback receipt')
        _require(hashlib.sha256(full.astype('<i2').tobytes()).hexdigest() == response['received_pcm_sha256'],
                 aid+': complete synthesis source hash differs')
        _require(pcm.tobytes() == full[:count].tobytes(), aid+': played WAV is not the consumed source prefix')
        _require(placement['start_frame'] == response['start_frame'], aid+': response start clock differs')
        heard = response['heard_text']
        _require(heard == placement['text'] and len(heard) == response['played_chars'],
                 aid+': consumed text differs from playback ledger')
        spans = [TextSpan(**s) for s in response['alignment']]
        released = [s for s in spans if s.end_frame <= count]
        _require(released and released[-1].end_char == len(heard),
                 aid+': heard text is not the complete released alignment prefix')
        _require(response['receipts'] and response['receipts'][-1]['through_frame'] == count,
                 aid+': no final consumption receipt')
        consumed_paths[aid] = pcm_path

    # Validate each observed fragment, rather than trusting arbitrary history
    # text or accidentally importing acoustic-only background metadata.
    observed = timeline['observed_audio']
    published = {aid: '' for aid in responses}
    for item in observed:
        identifier = item['audio_id']
        _require(identifier in placement_by_id, 'observed transcript has no PCM placement')
        placement = placement_by_id[identifier]
        _require(not placement.get('background_only') and placement.get('kind') != 'side_speech',
                 'background speech cannot enter assistant training history')
        _require(item['speaker'] == placement['speaker'] and item['speaker'] in {'user', 'assistant'},
                 'observed speaker differs from PCM placement')
        if item['speaker'] == 'assistant':
            published[identifier] += item['text']
            response = responses[identifier]
            _require(response['heard_text'].startswith(published[identifier]),
                     identifier+': observed assistant history contains an unplayed tail')
            prefix_length = len(published[identifier])
            boundaries = [s for s in response['alignment'] if s['end_char'] == prefix_length]
            _require(len(boundaries) == 1 and item['through_frame'] ==
                     response['start_frame']+boundaries[0]['end_frame'],
                     identifier+': observed prefix was released at a different acoustic boundary')
        else:
            _require(placement.get('kind') in {'deliver', 'backchannel'} and
                     item['text'] == placement['text'] and
                     item['through_frame'] == placement['start_frame']+placement['frames'],
                     identifier+': observed user event is not a completed foreground clip')
    _require(all(published[aid] == r['heard_text'] for aid, r in responses.items()),
             'observed assistant fragments do not reconstruct the consumed text')

    by_user = {c['user_id']: c for c in contexts}
    _require(len(by_user) == len(contexts), 'duplicate role-visible user histories')
    records = []
    copied = []
    for aid, response in sorted(responses.items(), key=lambda pair: pair[1]['start_frame']):
        uid = assistant_placements[aid].get('focus_turn_id')
        # Legacy SyntheticTimeline placements omit focus_turn_id; the main
        # user with the latest onset before this reply identifies its context.
        if uid is None:
            mains = [p for p in placements if p['speaker']=='user' and p.get('kind')=='deliver'
                     and p['start_frame']+p['frames'] <= response['start_frame']]
            _require(bool(mains), aid+': no completed main user before reply')
            uid = max(mains, key=lambda p: p['start_frame'])['focus_turn_id']
        _require(uid in by_user, aid+': missing corresponding role-visible history')
        context = by_user[uid]['assistant_history_before_reply']
        expected = _history_at(observed, response['start_frame'])
        _require(context['current_frame'] == response['start_frame'] and
                 context['observed_audio'] == expected,
                 aid+': assistant input history differs from the consumed timeline')
        messages = [{'role': i['speaker'], 'audio_id': i['audio_id'], 'content': i['text']}
                    for i in expected]
        relative = 'audio/'+aid+'.wav'
        records.append({'schema': 'duplex_v2.consumed_training_example.v1',
            'data_kind': 'scripted_target_behavior', 'example_id': aid, 'user_id': uid,
            'messages': messages,
            'response': {'role': 'assistant', 'content': response['heard_text'],
                         'audio_path': relative, 'audio_sha256': digest(consumed_paths[aid]),
                         'sample_rate': 16000, 'frames': response['played_frames']},
            'context_through_frame': response['start_frame']})
        copied.append((consumed_paths[aid], relative))

    raw = ''.join(json.dumps(record, ensure_ascii=False, sort_keys=True)+'\n' for record in records)
    ambient_export = (_ambient_export_evidence(source, result, timeline, reused_tts)
                      if 'ambient_audio' in result else None)
    target.mkdir(parents=True, exist_ok=True)
    final = target/'training.jsonl'
    if final.exists():
        _require(final.read_text() == raw, 'existing consumed dataset changed; use a new export directory')
    for pcm_path, relative in copied:
        destination = target/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _require(digest(destination) == digest(pcm_path), 'previous exported PCM changed')
        else:
            shutil.copyfile(pcm_path, destination)
    if not final.exists():
        temporary = target/'training.jsonl.tmp'
        temporary.write_text(raw)
        temporary.replace(final)
    summary = {'schema': 'duplex_v2.consumed_dataset.v1', 'status': 'passed',
        'data_kind': 'scripted_target_behavior', 'examples': len(records),
        'training_path': 'training.jsonl', 'training_sha256': digest(final),
        'assistant_audio_seconds': sum(r['played_frames'] for r in responses.values())/16000,
        'contains_full_script': False, 'contains_unplayed_assistant_text': False,
        'background_in_history': False, 'live_policy_evaluation': False,
        'verified_artifact_sha256': {name: digest(source/name) for name in
                                  ('timeline.json', 'heard-context.json', 'integrity.json', 'result.json')}}
    audio_manifest = _read(source/'manifest.json') if (source/'manifest.json').exists() else {}
    if 'language' in audio_manifest:
        summary['language'] = audio_manifest['language']
    if omitted:
        summary['omitted_backchannels'] = omitted
    if ambient_export is not None:
        mixed_path, attribution, evidence = ambient_export
        mixed_output = target/'audio/dialogue-stereo.wav'
        if mixed_output.exists():
            _require(digest(mixed_output) == digest(mixed_path), 'previous exported mixed dialogue changed')
        else:
            shutil.copyfile(mixed_path, mixed_output)
        attribution_path = target/'background-attribution.json'
        if attribution_path.exists():
            _require(_read(attribution_path) == attribution, 'previous exported background attribution changed')
        else:
            write_json(attribution_path, attribution)
        summary.update(mixed_audio={'path': 'audio/dialogue-stereo.wav',
            'sha256': digest(mixed_output), 'sample_rate': 16000, 'frames': result['audio']['frames'],
            'channels': result['audio']['channels']},
            background_attribution={'path': attribution_path.name, 'sha256': digest(attribution_path)},
            verified_background_artifact_sha256=evidence)
    if result.get('user_side_speech_policy') is not None:
        summary['full_duplex_training'] = _export_full_duplex_input(
            source, target, result, integrity, timeline, reused_tts, ambient_export is not None)
    write_json(target/'manifest.json', summary)
    return summary
