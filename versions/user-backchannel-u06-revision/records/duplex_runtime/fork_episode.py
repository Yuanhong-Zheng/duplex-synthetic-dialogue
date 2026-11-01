"""Fork a failed episode at a saved checkpoint after one approved anchor revision."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from duplex_runtime.batch import ROOT, create_batch
from duplex_runtime.prepare import verify_prepared_session
from duplex_runtime.scoring import file_hashes
from duplex_runtime.speech import digest
from duplex_runtime.user_simulator import write_json
from duplex_runtime.verify_synthetic import read, require
from revise_episode_anchor import revise_anchor


def contains(value, text):
    if isinstance(value, dict):
        return any(contains(v, text) for v in value.values())
    if isinstance(value, list):
        return any(contains(v, text) for v in value)
    return value == text


def anchor_change(parent_environment, prepared, turn_id):
    old = read(parent_environment/'frozen/episode_script.json')
    new = read(prepared/'frozen/episode_script.json')
    targets = [t for t in new['episode_turns'] if t['turn_id'] == turn_id]
    require(len(targets) == 1, 'revised turn is missing or duplicated')
    revised, revision = revise_anchor(old, turn_id, targets[0]['interrupt_anchor'])
    require(revised == new, 'environment branch changed more than the approved anchor')
    for name in ('scene_task', 'user_voice', 'situation_knowledge', 'actor_rubric'):
        require(read(parent_environment/'frozen'/f'{name}.json') == read(prepared/'frozen'/f'{name}.json'),
                f'branch changed frozen {name}')
    return revision, old


def create_branch(parent_batch, episode_id, prepared, output, turn_id):
    parent_batch, prepared, output = [Path(p).resolve() for p in (parent_batch, prepared, output)]
    require(not output.exists(), 'use a new branch directory')
    verify_prepared_session(prepared)
    parent = parent_batch/'jobs'/episode_id
    state, manifest = read(parent/'state.json'), read(parent_batch/'manifest.json')
    require(state['status'] == 'failed', 'only a preserved failed episode can be branched')
    require(not manifest.get('approved_anchor_branch'), 'a second anchor revision needs a separately reviewed branch')
    verify_prepared_session(parent/'environment')
    revision, old_script = anchor_change(parent/'environment', prepared, turn_id)
    latest = parent/state['latest_rollout']
    result, checkpoint = read(latest/'result.json'), read(parent/'checkpoint.json')
    require(result['status'] == 'blocked' and result['failure']['type'] == 'RolloutBlocked'
            and result['failure']['reason'].startswith(turn_id+' interrupt anchor not reached'),
            'parent failure is not the approved missing interrupt window')
    index = next(i for i, t in enumerate(old_script['episode_turns']) if t['turn_id'] == turn_id)
    require(checkpoint['delivered_main_turns'] == index and
            result['delivered_turns'] == [t['turn_id'] for t in old_script['episode_turns'][:index]],
            'parent checkpoint is not immediately before the revised turn')
    # Policy, observations, rendering and timing are frozen. Only branch setup
    # and accounting code may differ from the parent's generator snapshot.
    setup_files = {'duplex_runtime/batch.py', 'duplex_runtime/fork_episode.py', 'revise_episode_anchor.py'}
    for name, expected in manifest['code_sha256'].items():
        if name not in setup_files:
            require(digest(ROOT/name) == expected, 'branch changed a runtime policy or adapter: '+name)
    inherited = []
    for namespace in ('user-policy', 'semantic-decisions', 'assistant-stream', 'user-tts', 'assistant-tts', 'assistant-url-tts'):
        for entry in sorted((parent/'journal'/namespace).glob('[0-9]*')):
            identity = read(entry/'identity.json')
            # A changed request begins a new suffix. Nothing at or after that
            # position is imported, even if a later request happens to match.
            if contains(identity.get('context', {}), revision['before']):
                break
            require((entry/'commit.json').exists(), 'prefix contains an uncommitted call')
            require(not (entry/'policy-revisions').exists(), 'policy-revised prefix needs a separate reviewed migration')
            commit = read(entry/'commit.json')
            require(all(digest(entry/name) == value for name, value in commit['files'].items()),
                    'parent committed call is damaged')
            inherited.append({'path': str(entry.relative_to(parent/'journal')), 'source': str(entry),
                              'files': file_hashes(entry), 'commit_sha256': digest(entry/'commit.json')})
    require(bool(inherited), 'no committed prefix was found')
    create_batch(output, [prepared], 1, 1, manifest['seed'], manifest['model'],
                 backchannel_target=manifest.get('backchannel_spoken_target'))
    child = output/'jobs/E001'
    provenance = child/'branch-provenance'; provenance.mkdir()
    shutil.copytree(parent/'environment', provenance/'environment')
    shutil.copy2(parent_batch/'manifest.json', provenance/'parent-batch-manifest.json')
    shutil.copy2(parent/'checkpoint.json', provenance/'checkpoint.json')
    for name in ('result.json', 'timeline.json', 'trigger-results.json', 'backchannel-coverage.json'):
        if (latest/name).exists():shutil.copy2(latest/name, provenance/name)
    for item in inherited:
        destination = child/'journal'/item['path']
        shutil.copytree(Path(item['source']), destination)
        write_json(destination/'inheritance.json', {'source': item['source'], 'files': item['files'],
                   'same_logical_episode': True, 'new_network_request': False})
    shutil.copy2(parent/'checkpoint.json', child/'checkpoint.json')
    proof = {'schema': 'duplex-runtime.approved-anchor-branch.v1', 'parent_batch': str(parent_batch),
             'parent_episode_id': episode_id, 'parent_rollout': str(latest), 'revision': revision,
             'checkpoint': checkpoint, 'inherited': inherited,
             'parent_provenance_files': file_hashes(provenance), 'same_logical_episode': True,
             'runtime_observer_guard_body_voice_and_timing_unchanged': True}
    write_json(child/'branch-reuse.json', proof)
    updated = read(output/'manifest.json')
    updated['sampling'] = 'user_approved_single_anchor_revision_branch_from_saved_checkpoint'
    updated['approved_anchor_branch'] = {'episode_id': 'E001', 'proof': 'jobs/E001/branch-reuse.json',
                                         'revision': revision, 'parent_episode_id': episode_id}
    write_json(output/'manifest.json', updated)
    verify_branch(child, require_replay=False)
    return {'status': 'prepared', 'branch': str(output), 'inherited_call_records': len(inherited),
            'checkpoint': checkpoint, 'revision': revision}


def verify_branch(job, *, require_replay=True):
    proof = read(job/'branch-reuse.json')
    require(proof['same_logical_episode'] is True and
            proof['runtime_observer_guard_body_voice_and_timing_unchanged'] is True, 'invalid branch scope')
    provenance = job/'branch-provenance'
    verify_prepared_session(provenance/'environment')
    require(all(digest(provenance/name) == value for name, value in proof['parent_provenance_files'].items()),
            'parent branch provenance changed')
    revision, _ = anchor_change(provenance/'environment', job/'environment', proof['revision']['turn_id'])
    require(revision == proof['revision'], 'branch anchor differs from its approved revision')
    require(read(provenance/'checkpoint.json') == proof['checkpoint'], 'parent checkpoint changed')
    parent_manifest = read(provenance/'parent-batch-manifest.json')
    child_manifest = read(job.parents[1]/'manifest.json')
    require(parent_manifest['model'] == child_manifest['model'], 'branch changed the model')
    for name, value in parent_manifest['code_sha256'].items():
        if name not in {'duplex_runtime/batch.py', 'duplex_runtime/fork_episode.py', 'revise_episode_anchor.py'}:
            require(child_manifest['code_sha256'].get(name) == value, 'branch changed runtime policy or rendering: '+name)
    markers = {str(p.parent.relative_to(job/'journal')) for p in (job/'journal').glob('*/*/inheritance.json')}
    require(markers == {item['path'] for item in proof['inherited']}, 'undeclared inherited call marker')
    for item in proof['inherited']:
        entry = job/'journal'/item['path']
        require(all(digest(entry/name) == value for name, value in item['files'].items()), 'inherited call changed')
        require(digest(entry/'commit.json') == item['commit_sha256'], 'inherited call pointer changed')
        marker = read(entry/'inheritance.json')
        require(marker == {'source': item['source'], 'files': item['files'],
                           'same_logical_episode': True, 'new_network_request': False}, 'inheritance marker changed')
        require(not contains(read(entry/'identity.json').get('context', {}), revision['before']),
                'an obsolete anchor request was imported')
    if require_replay:
        require(any(read(p)['checkpoint'] == proof['checkpoint'] and read(p)['status'] == 'passed'
                    for p in job.glob('attempts/*/replay-verification.json')), 'revised branch lacks exact prefix replay proof')
    return {'status': 'passed', 'inherited_call_records': len(proof['inherited']),
            'same_logical_episode': True, 'revised_turn': revision['turn_id']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-batch', type=Path, required=True)
    parser.add_argument('--episode', default='E001')
    parser.add_argument('--session-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--turn-id', required=True)
    args = parser.parse_args()
    print(create_branch(args.parent_batch, args.episode, args.session_dir, args.output_dir, args.turn_id))


if __name__ == '__main__':main()
