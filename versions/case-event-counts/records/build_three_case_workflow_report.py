"""Project the three completed case families into an expandable case selector."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import posixpath
import subprocess
import sys
import zipfile

from workflow_explorer import validate_catalog

ROOT = Path(__file__).resolve().parent
SELECTION = [
    ('legacy', 'with-backchannel-ambient-gain', '新员工账户与权限核对'),
    ('travel-replanning', 'travel-replanning', '旅行中断后的行程重排'),
    ('todo-estimation', 'todo-estimation', '待办事项耗时预估'),
]
LEGACY_RUNTIME = ('runtime_runs/20261031-u06-anchor-revision-batch/', 'runtime_runs/20261031-ambient-gain-remix/')


def read(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'path' and isinstance(item, str):yield item
            else:yield from references(item)
    elif isinstance(value, list):
        for item in value:yield from references(item)


def project(data, base):
    original = copy.deepcopy(data)
    ids = {item[0] for item in SELECTION}
    data = copy.deepcopy(data)
    data.update(cases=[], dialogue_versions=[], default_case='legacy', compact_view=True,
                successful_cases_only=True, case_navigation='expandable_list', traces=[], failure_reviews=[])
    data.pop('batch_summary', None)
    summaries = []
    for family, version_id, label in SELECTION:
        version = copy.deepcopy(next(v for v in original['dialogue_versions'] if v['id'] == version_id))
        rollout = read(base / Path(version['path']).parent / 'report-summary.json')['results_explorer']['synthetic_rollout']
        if rollout['status'] != 'completed':raise ValueError('Selected case has not completed: ' + family)
        turns = rollout['delivered_turns']
        seconds = round(rollout['duration_seconds'])
        feedback = sum(s['kind'] == 'backchannel' for s in rollout['segments'])
        interruptions = rollout['interruptions']
        side_speeches = sum(s['kind'] == 'side_speech' for s in rollout['segments'])
        short = (f'{turns} 轮 · {seconds // 60} 分 {seconds % 60:02d} 秒 · 附和 {feedback} 次'
                 f' · 打断 {interruptions} 次 · 旁音 {side_speeches} 次')
        data['cases'].append({'id':family, 'label':label, 'status':'completed', 'summary':short})
        version.update(label=label, description=short + '。完整语音与流程记录对应同一案例。')
        data['dialogue_versions'].append(version)
        audio = posixpath.normpath(str(Path(version['path']).parent / rollout['audio_path']))
        if audio.startswith('../'):raise ValueError('Selected audio escapes its report')
        summaries.append({'case':family, 'version':version_id, 'audio_path':audio,
                          'audio_sha256':sha(base/audio), 'turns':turns, 'backchannels':feedback,
                          'interruptions':interruptions, 'side_speeches':side_speeches})
    data['dialogue_audio_url'] = summaries[0]['audio_path']
    data['taxonomy_catalog_by_case'] = {k:v for k,v in data['taxonomy_catalog_by_case'].items() if k in ids}
    for node in data['nodes']:
        old = node['examples']
        legacy = [(i,e) for i,e in enumerate(old) if e.get('dataset_case') == 'legacy']
        current = {i for i,e in legacy if any(prefix in p for p in references(e) for prefix in LEGACY_RUNTIME)}
        preferred = node.get('default_examples_by_case', {}).get('legacy', node.get('default_example', 0))
        # A single current frozen artifact, plus all calls from the accepted
        # original trajectory. Earlier audio variants are not separate cases.
        keep_legacy = current if current and node['phase'] != 'offline' else {preferred}
        kept = [(i,e) for i,e in enumerate(old) if e.get('dataset_case') in ids and
                (e['dataset_case'] != 'legacy' or i in keep_legacy)]
        remap = {old_index:new_index for new_index,(old_index,_) in enumerate(kept)}
        node['examples'] = [e for _,e in kept]
        defaults = {}
        for family in ids:
            candidates = [i for i,e in enumerate(node['examples']) if e['dataset_case'] == family]
            old_default = node.get('default_examples_by_case', {}).get(family)
            if candidates:defaults[family] = remap.get(old_default, candidates[0])
        node['default_examples_by_case'] = defaults
        node['default_example'] = defaults.get('legacy', 0)
        for field in ('source_by_case', 'rules_by_case'):
            if field in node:node[field] = {k:v for k,v in node[field].items() if k in ids}
        node['actual_by_case'] = {
            family:(f'本案例保存 {sum(e["dataset_case"] == family for e in node["examples"])} 条实际记录。'
                    if any(e['dataset_case'] == family for e in node['examples']) else '本案例未触发该分支。')
            for family in ids
        }
    data['created_utc'] = datetime.now(timezone.utc).isoformat()
    used = set(references({k:v for k,v in data.items() if k != 'source_files'}))
    data['source_files'] = {k:v for k,v in original['source_files'].items() if v['path'] in used}
    validate_catalog(data)
    return data, summaries


def build(base, output, audit_run):
    base, output, audit_run = [Path(p).resolve() for p in (base,output,audit_run)]
    site = output/'site'
    if site.exists():raise ValueError('Use a new report directory')
    site.mkdir(parents=True)
    old_roots = {name:sha(base/name) for name in ('index.html','workflow-data.json','workflow-records.zip')}
    data, summaries = project(read(base/'workflow-data.json'),base)
    ui_sources = []
    for name in ('workflow_explorer.py','report_assets/workflow_explorer.js','report_assets/workflow_explorer.css',Path(__file__).name):
        source = ROOT/name
        relative = 'versions/three-case-list/records/'+name
        target = site/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(source.read_bytes())
        artifact = {'path':relative,'source_path':name,'sha256':sha(target),'bytes':target.stat().st_size,'label':'本次案例列表实现'}
        data['source_files']['three-case-list:'+name] = artifact
        ui_sources.append(artifact)
    data['presentation_sources'] = ui_sources
    (site/'workflow-data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    summary = {'title':'全双工流程 · 三个完整案例', 'report_intro':'展开案例列表，切换完整语音及各阶段的输入、Prompt 与输出。',
               'omit_canonical_case':True, 'workflow_explorer':data}
    output.mkdir(parents=True,exist_ok=True)
    (output/'report-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    subprocess.run([sys.executable,'-B',str(ROOT/'build_episode_script_bundle.py'),'--run-dir',str(audit_run),
                    '--report-summary',str(output/'report-summary.json'),'--output',str(site/'index.html')],cwd=ROOT,check=True)
    with zipfile.ZipFile(base/'workflow-records.zip') as old, zipfile.ZipFile(site/'workflow-records.zip','w',zipfile.ZIP_DEFLATED) as archive:
        archive.write(site/'workflow-data.json','workflow-data.json')
        for item in data['source_files'].values():
            local = site/item['path']
            raw = local.read_bytes() if local.exists() else old.read(item['path'])
            if hashlib.sha256(raw).hexdigest() != item['sha256']:raise ValueError('Source bytes changed: '+item['path'])
            archive.writestr(item['path'],raw)
    proof = {'status':'passed','successful_cases':3,'audio_choices':3,'failed_cases_in_view':0,
             'expandable_case_list':True,'source_files':len(data['source_files']),'selected_audio':summaries,
             'base_site':str(base),'baseline_root_sha256':old_roots,'model_or_tts_calls':0,'archive_and_audio_files_deleted':False,
             'canonical_builder_used':True,'appendix_omitted':True}
    (output/'build-verification.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(proof,ensure_ascii=False))
    return proof


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-site',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--audit-run',type=Path,required=True);a=p.parse_args()
    build(a.base_site,a.output_dir,a.audit_run)
