"""Apply one approved Stage-3 anchor revision without changing other fields."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def revise_anchor(script, turn_id, replacement):
    if not isinstance(script, dict) or not isinstance(script.get('episode_turns'), list):
        raise ValueError('episode_script must contain episode_turns')
    if not isinstance(replacement, str) or not replacement.strip():
        raise ValueError('replacement anchor must be a nonempty string')
    matches = [(i, turn) for i, turn in enumerate(script['episode_turns'])
               if isinstance(turn, dict) and turn.get('turn_id') == turn_id]
    if len(matches) != 1:
        raise ValueError('the revised turn must exist exactly once')
    index, turn = matches[0]
    if 'T03' not in turn.get('timing_refs', []) or not isinstance(turn.get('interrupt_anchor'), str):
        raise ValueError('only an existing T03 anchor can be revised')
    if replacement == turn['interrupt_anchor']:
        raise ValueError('replacement anchor must differ from the original')
    updated = copy.deepcopy(script)
    updated['episode_turns'][index]['interrupt_anchor'] = replacement
    return updated, {'turn_id': turn_id, 'path': f'/episode_turns/{index}/interrupt_anchor',
                     'before': turn['interrupt_anchor'], 'after': replacement,
                     'all_other_fields_preserved': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-script', type=Path, required=True)
    parser.add_argument('--turn-id', required=True)
    parser.add_argument('--anchor-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('use a new output directory; original artifacts are immutable')
    updated, revision = revise_anchor(json.loads(args.source_script.read_text()), args.turn_id,
                                     args.anchor_file.read_text().strip())
    args.output_dir.mkdir(parents=True)
    for name, value in [('episode_script.json', updated), ('anchor-revision.json', revision)]:
        (args.output_dir/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


if __name__ == '__main__':
    main()
