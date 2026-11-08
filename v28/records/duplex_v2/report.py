"""Source-backed V2 reports using the project's canonical HTML builders.

The report consumes the orchestration manifest; it never generates dialogue or
changes an execution outcome. Failed samples keep their own result page.
"""
from __future__ import annotations

from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

from build_workflow_report import Catalog
from workflow_explorer import organize_workflow, validate_catalog

ROOT = Path(__file__).resolve().parents[1]
MODE = "static_script_v2"
PROFILE_LABELS = {
    'formality': {'casual': '口语', 'neutral': '日常中性', 'formal': '正式口语'},
    'directness': {'indirect': '委婉', 'balanced': '直接度适中', 'direct': '直接'},
    'emotional_expressiveness': {'reserved': '克制', 'balanced': '情感适中', 'expressive': '情感外显'},
    'verbosity': {'brief': '简洁', 'balanced': '详略适中', 'detailed': '细致'},
    'initiative': {'low': '主动低', 'medium': '主动中', 'high': '主动高'},
    'caution': {'low': '审慎低', 'medium': '审慎中', 'high': '审慎高'},
    'patience': {'low': '耐心低', 'medium': '耐心中', 'high': '耐心高'},
}


def _read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else default


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def defer_record_text(value):
    """Keep raw files intact and let the existing viewer fetch their text."""
    if isinstance(value, dict):
        if value.get('path') and value.get('sha256'):
            value.pop('text', None)
        for item in value.values():
            defer_record_text(item)
    elif isinstance(value, list):
        for item in value:
            defer_record_text(item)


def _path(value, base):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _existing(paths):
    return list(dict.fromkeys(Path(p) for p in paths if p and Path(p).is_file()))


def _example(catalog, node, case_id, label, inputs=(), outputs=(), prompt=None, metadata=None):
    inputs, outputs = _existing(inputs), _existing(outputs)
    if not inputs and not outputs and not (prompt and Path(prompt).is_file()):
        return
    node['examples'].append(catalog.example(
        label, inputs, outputs,
        prompt if prompt and Path(prompt).is_file() else None,
        metadata if metadata and Path(metadata).is_file() else None,
        dataset_case=case_id))


def _recorded_calls(catalog, node, case_id, directory, *, label="实际调用"):
    """Read persisted calls with either plain or attempt-prefixed filenames."""
    if not directory.is_dir():
        return
    format_identities = sorted(path for path in directory.rglob('identity.json')
                               if re.fullmatch(r'format-attempt-\d+', path.parent.name)
                               and 'calls' not in path.relative_to(directory).parts)
    format_parents = {path.parent.parent.parent for path in format_identities}
    for prompt in sorted(directory.rglob('*prompt.md')):
        if 'calls' in prompt.relative_to(directory).parts:
            # The stage's own records already reference the immutable call cache.
            continue
        prefix = prompt.name[:-len('prompt.md')]
        folder = prompt.parent
        inputs = [folder / (prefix + 'input.json')]
        outputs = sorted(p for p in folder.iterdir()
                         if p.is_file() and p.suffix in {'.json', '.txt'}
                         and (not prefix or p.name.startswith(prefix))
                         and 'input' not in p.name and 'metadata' not in p.name
                         and p.name != 'identity.json')
        if folder in format_parents:
            # The wrapper's prompt/input precede format repair, whereas its raw
            # response is the final retry. Show each exact request below instead.
            _example(catalog, node, case_id,
                     label + ' · ' + str(folder.relative_to(directory)) + ' 最终裁判结果',
                     outputs=outputs)
            continue
        _example(catalog, node, case_id,
                 label + ' · ' + str(prompt.relative_to(directory)), inputs, outputs,
                 prompt, folder / (prefix + 'metadata.json'))
    for identity_path in format_identities:
        actual = _read(identity_path)
        if not isinstance(actual, dict) or not isinstance(actual.get('prompt'), str) or 'context' not in actual:
            raise ValueError('Judge format request is missing its recorded prompt/context: ' + str(identity_path))
        folder = identity_path.parent
        metadata = folder / 'metadata.json'
        example = catalog.example(
            label + ' · ' + str(folder.relative_to(directory)) + ' 实际裁判请求',
            inputs=[identity_path], outputs=_existing([folder / 'raw_response.txt', folder / 'validation.json']),
            metadata=metadata if metadata.is_file() else None, dataset_case=case_id)
        # Extract fields only for display; both links retain the original identity
        # bytes and hash, including the precise format feedback sent on retries.
        example['inputs'][0].update(label='实际输入（identity.json 的 context）', json_key='context',
                                    text=json.dumps(actual['context'], ensure_ascii=False, indent=2))
        example['prompt'] = {**catalog.artifact(identity_path, '实际 Prompt（identity.json 的 prompt）'),
                             'text': actual['prompt'], 'origin': 'recorded', 'json_key': 'prompt'}
        node['examples'].append(example)


def _nodes(catalog, *, script_max_repairs=3, script_repair_policy=None, text_policy=None, script_execution_policy=None,
           language='zh', workflow_version=None, script_feedback_policy=None, length_compaction_policy=None,
           compaction_output_policy=None):
    specs = [
        ('v2_sample', 'offline', '0.1', '领域、场景与风格采样', '从 taxonomy 随机选择本批场景；身份、随机种子与补采来源保存在原始清单。', 'sample_taxonomy.py', 'program', ['taxonomy、随机种子'], ['本批冻结场景与采样来源']),
        ('v2_library', 'offline', '0.2', '任务方向库生成与审查', '显示本次 Stage 0 任务方向库的真实生成、Judge 和修复记录。', 'step_00_generate_task_examples.py', 'llm', ['场景、任务风格'], ['example_tasks 与 Judge 结论']),
        ('stage1', 'offline', '1', '任务、用户与声音设定', '将选定场景实例化为任务目标、用户种子和 voice_profile。', 'step_01_generate_scene_task_user_voice.py', 'llm', ['采样场景、任务方向'], ['scene_task.json、user_voice.json']),
        ('judge1', 'offline', '1J', '任务与声音检查', '保留 Stage 1 实际语义检查；若有修复，仍需重审。', 'step_01_generate_scene_task_user_voice.py', 'llm', ['当前候选与冻结场景'], ['Judge findings / decision']),
        ('repair1', 'offline', '1R', '任务与声音定向修复', '只有原始检查要求修复时才调用；无记录时明确标为未触发。', 'step_01_generate_scene_task_user_voice.py', 'llm', ['候选与 findings'], ['修复候选与重审']),
        ('stage2', 'offline', '2', '情境、资料与约束', '生成任务材料、环境、约束与安全边界，后续剧本沿用这些冻结事实。', 'step_02_generate_situation_knowledge.py', 'llm', ['任务、用户与声音设定'], ['situation_knowledge.json']),
        ('judge2', 'offline', '2J', '情境语义检查', '独立检查情境和材料的一致性、可执行性及任务支撑。', 'step_02_generate_situation_knowledge.py', 'llm', ['情境候选及上游事实'], ['Judge findings / decision']),
        ('repair2', 'offline', '2R', '情境定向修复', '依据原 Judge findings 修复后重新检查，旧候选保留。', 'step_02_generate_situation_knowledge.py', 'llm', ['候选与 findings'], ['修复情境与重审']),
        ('v2_script', 'offline', '3', '整段双人剧本与双工事件生成', '一次生成整段用户和助手正文，并指定打断起声、助手停播、用户附和、句内停顿与旁语的文本锚点。', 'duplex_v2/script.py', 'llm', ['冻结 Stage 1 / 2 环境'], ['全量 U/A 台词、事件与精确前缀锚点']),
        ('v2_script_judge', 'offline', '3J', '整段剧本与已听边界检查', 'Sol 离线检查事实、角色、因果、已听前缀、自然简洁和事件一致性。此处不模拟实时观察器。', 'duplex_v2/script.py', 'llm', ['整段候选、冻结事实、已听边界'], ['Judge findings / decision']),
        ('v2_script_refine', 'offline', '3R', '整段剧本定向修复与重审', '按 findings 由原生成模型修复，再经同一独立 Judge；最多三次内容修复；旧记录保留原预算。', 'duplex_v2/script.py', 'llm', ['候选与具体 findings'], ['修复候选与独立重审']),
        ('v2_length', 'offline', '3L', '独立长度精简与重审', '内容过审后，超过 250 字的助手回答有独立三次精简机会；耗尽后保留内容已过审全文并标记超长。', 'duplex_v2/script.py', 'llm', ['内容过审全文与当前已听信息'], ['最终冻结 script.json、length_report.json']),
        ('v2_voices', 'interaction', '4.1', '固定声音映射', '用户沿用 Stage 1 voice_profile 的固定参考；助手沿用固定声音，第三方按自己的 profile 固定声音。', 'duplex_v2/audio.py', 'program', ['voice_profile、说话人身份'], ['声音与参考音频映射']),
        ('v2_tts', 'interaction', '4.2', '冻结台词并行 TTS', '案例之间和各段台词并行请求现有 Qwen3-TTS 常驻服务；文本冻结后不再生成新台词。', 'duplex_v2/audio.py', 'tts', ['冻结台词、声音与音频参数'], ['各段原始 WAV、音频来源记录']),
        ('v2_alignment', 'interaction', '4.3', '逐词对齐与锚点映射', '对真实音频做强制对齐，将锚点映射到采样边界。仅无声标点附着差异可沿用同一发音片段末尾并记警告，保留计划与实际已听前缀；实词内部边界仍拒绝。', 'duplex_v2/audio.py', 'program', ['真实 WAV、原台词、文本锚点'], ['assets.json 中的对齐记录、原计划与实际前缀、标点边界警告']),
        ('v2_schedule', 'interaction', '4.4', '静态时间轴与已听前缀', '按冻结锚点安排起声和停播；分别记录用户起声时与助手停播时的已听前缀。附和不停止助手，也不推进主轮。', 'duplex_v2/audio.py', 'program', ['精确对齐、冻结事件'], ['timeline.json、heard-context.json']),
        ('v2_mix', 'interaction', '4.5', '裁剪、重叠与双声道混音', '保留助手完整源音频，混音只使用实际已播前缀；旁语按 30%–70% 增益混入，不进入实时决策。', 'duplex_v2/audio.py', 'program', ['静态时间轴、各段 PCM'], ['最终双声道音频、事件位置与音量']),
        ('v2_integrity', 'interaction', '4.6', '因果与音频完整性核验', '核对计划、真实播放和已听前缀一致性；只有实际完成并通过核验的案例标记为完成。', 'duplex_v2/audio.py', 'program', ['冻结剧本、音频、时间轴'], ['result.json、integrity.json']),
        ('v2_records', 'engineering', 'E1', '调用来源、旧候选与失败保留', '保存真实 Prompt、输入、响应、元信息和失败，不把工程等待计入合成音频时间。', 'duplex_v2/report.py', 'program', ['原始调用与状态'], ['可下载来源记录']),
        ('v2_timings', 'engineering', 'E2', '阶段耗时与批次状态', '按原运行清单展示阶段耗时和模型配置；报告构建本身不调用文本或语音模型。', 'duplex_v2/report.py', 'program', ['运行 manifest 与时间记录'], ['批次状态、阶段耗时']),
        ('v2_export', 'engineering', 'E3', '已播放前缀训练数据导出', '仅导出实际消耗的文本前缀与音频范围；静态目标数据与实时策略评测分开标注。', 'duplex_v2/export.py', 'program', ['实际时间轴与已听上下文'], ['training.jsonl、dataset/manifest.json']),
    ]
    result = {}
    for identifier, phase, number, title, purpose, source, kind, inputs, outputs in specs:
        if identifier == 'v2_script_refine':
            purpose = ('按 findings 由原生成模型修复，再经同一独立 Judge；最多 '
                       + str(script_max_repairs) + ' 次内容修复；旧记录保留原预算。')
            if script_repair_policy:
                title = '剧本局部修改、应用与重审'
                purpose = ('原编剧仅输出必要字段变更；程序核对基底哈希并原子应用后，仍经完整校验、'
                           '已听边界重建与原独立 Judge。五次内容修复与无法解析时的三次重试分别计数。')
        if identifier == 'v2_length' and (script_repair_policy or {}).get('overlength_after_compaction') == 'fail_case':
            purpose = '内容过审后，超过250字的回复有独立三次精简机会；三次均未同时通过内容与长度要求，整条案例失败，不进入TTS或成功训练数据。'
        if text_policy:
            if identifier == 'v2_library':
                purpose += ' Luna 生成和修复，Sol 审核；Stage 0 总共最多五次审核。'
            elif identifier in {'stage1', 'repair1'}:
                purpose += ' 本版由 Luna 生成或修复，仍由 Sol 独立审核。'
            elif identifier == 'repair2':
                title = '情境局部修改、应用与重审'
                purpose = ('Sol 只输出原 Judge 指向的局部修改，程序原子应用后做完整结构检查和 Sol 重审；'
                           '基础环境描述与派生声景汇总分开，抽样记录由程序持有，旧候选与拒绝保留。')
            elif identifier == 'v2_length':
                purpose += ' 第一次由 Luna 精简，第二、三次由 Sol 精简，每次仍由 Sol 审核，共用原三次预算。'
        if script_execution_policy and language != 'mixed':
            if identifier == 'v2_length':
                purpose = ('前两次须同时满足语义和250字；第三次满足250字及结构锚点即可接受，'
                           '保留语义未通过标记与原Judge，不伪称语义过审；仍超长或结构无效则失败。'
                           '精简模型依次为Luna、Sol、Sol，独立三次机会。')
            elif identifier in {'v2_script', 'v2_script_refine', 'v2_script_judge'}:
                purpose += (' V2.6由程序派生T01及起声后1–3字的T03停声位置，'
                            '模型同步其余受影响的语义锚点；整组校验后重建已听上下文并完整重审。')
            elif identifier == 'v2_integrity':
                purpose += ' 附和自身对齐重合成耗尽只记警告并省略，实际次数、混音和已听历史均排除该条。'
        if language == 'zh' and script_execution_policy:
            from duplex_v2.anchor_dependencies import ZH_MIXED_POLICY, LEGACY_ZH_MIXED_POLICY
            if script_execution_policy in (ZH_MIXED_POLICY, LEGACY_ZH_MIXED_POLICY):
                purpose = purpose.replace('V2.6由', 'V2.8中文版由').replace(
                    '1–3字', '1–3个完整单位（汉字、英文单词或编号）')
                if identifier == 'v2_alignment':
                    purpose += (' 原停点缺少精确边界时，仅在起声后1–3个完整单位内至多改选一次真实边界，'
                                '经原Judge完整重审后复用录音；不拆英文单词或编号，不移动用户起声，'
                                '没有合法边界或重审未通过则保留失败。')
                if script_execution_policy == ZH_MIXED_POLICY and identifier in {'v2_script','v2_script_judge','v2_script_refine'}:
                    purpose += ' 中文数词和金额也逐字计数，例如六百九十九元可在六百后停声；阿拉伯数字写法699作为整体。'
        if identifier == 'v2_length' and compaction_output_policy:
            from duplex_v2.compaction_contract import POLICY as COMPACT_POLICY
            if compaction_output_policy == COMPACT_POLICY:
                purpose += (' 精简示例和校验共用四字段契约，声景由程序原样保留；仅允许忽略与冻结基稿精确一致的'
                            '重复environment_events并留证，未知字段或声景改动仍拒绝，不增加调用或预算。')
        if language == 'en':
            word_range = '1–3' if (script_execution_policy or {}).get('audio_stop_selection') else '1–2'
            version_label = 'V2.8' if workflow_version == 'v2.8' else 'V2.7'
            purpose = purpose.replace('250字', '120个英文词').replace('250 字', '120个英文词').replace('1–3字', word_range+'个完整英文词').replace('V2.6由', version_label+'英文版由')
            if identifier == 'v2_alignment' and (script_execution_policy or {}).get('audio_stop_selection'):
                purpose += ' 原停点缺少边界时，在起声后1–3词内至多改选一次真实精确边界，经原Judge完整重审后复用录音；无合法候选或重审未通过保留失败。'
            if identifier in {'v2_library','stage1','stage2','v2_script','v2_script_judge','v2_script_refine'}:
                purpose += ' 本次模型提示词使用独立英文模板，台词与环境材料原生生成英文。'
            if script_feedback_policy == 'cumulative_findings_applied_diffs_v1' and identifier in {'v2_script_refine','v2_script_judge'}:
                purpose += ' 本版提供历史真实Judge意见与程序计算的修改差异，保护冻结约束；首审完整核对逐轮事实披露，通用原则与个案私有事实分开判断。'
            if length_compaction_policy == 'incremental_previous_candidate_target_100_105_v1' and identifier == 'v2_length':
                purpose += ' 首次以100–105词为生成目标；后两次接收上次精简稿、实际词数和审查意见做增量修改，120词仍为硬上限。'
        if language == 'mixed':
            if identifier == 'v2_length':
                purpose = ('各案例沿用自己的冻结规则：中文上限250字，英文上限120个英文词；'
                           '内容过审后独立三次精简，模型依次为Luna、Sol、Sol。第三次仅在满足'
                           '本案例长度及结构锚点要求时可按原策略保留语义警告；仍超长则失败。'
                           '英文增量精简目标及历史反馈仅在该案例原策略启用时展示，不套用于中文。')
            elif identifier in {'v2_script','v2_script_judge','v2_script_refine'}:
                purpose += (' 每条案例保留各自语言、反馈及执行策略：中文起声后1–3字停声，'
                            '本批英文起声后1–3个完整英文词停声；实际已听前缀与审查均读取该案例原记录。')
            elif identifier == 'v2_alignment':
                purpose += (' 中英文分别使用自己的对齐记录；英文仅在原策略明确允许时执行一次'
                            '1–3词精确停声改选及完整重审，不能移动另一案例的起声或正文。')
            elif identifier == 'v2_voices':
                purpose += ' 中文和英文分别沿用各自固定声音参考，不跨语言替换音色。'
            if identifier in {'v2_library','stage1','stage2','v2_script','v2_script_judge','v2_script_refine'}:
                purpose += ' 中文案例使用中文模板，英文案例使用英文模板，输入、Prompt与输出按案例分别保留。'
        source_path = ROOT / source
        # Code may not yet exist while an unfinished batch is being inspected.
        source = source if source_path.is_file() else 'duplex_v2/report.py'
        node = catalog.node(identifier, phase, title, purpose, inputs, outputs, source=source,
                            kind=kind, actual='查看所选案例的原始记录',
                            step_number=number, step_title=title)
        result[identifier] = node
    chain = ['v2_sample', 'v2_library', 'stage1', 'judge1', 'stage2', 'judge2',
             'v2_script', 'v2_script_judge', 'v2_length', 'v2_voices', 'v2_tts',
             'v2_alignment', 'v2_schedule', 'v2_mix', 'v2_integrity']
    for left, right in zip(chain, chain[1:]):
        result[left]['downstream'].append(right)
        result[right]['upstream'].append(left)
    for judge, repair in [('judge1', 'repair1'), ('judge2', 'repair2'),
                          ('v2_script_judge', 'v2_script_refine')]:
        result[judge]['downstream'].append(repair)
        result[repair]['upstream'].append(judge)
        result[repair]['downstream'].append(judge)
    return result


def _offline_records(catalog, nodes, case, run, manifest, case_id):
    stages = case.get('stages', manifest.get('stages', {}))
    source = _path(case.get('source_directory', case['directory']), run)
    source_id = case.get('source_case_id', source.name)
    original_manifest = _path(case['directory'], run).parent / 'manifest.json'
    stage1_root = _path(stages['1'], run) if stages.get('1') else None
    scene = _read(stage1_root / source_id / 'scene_task.json', {}) if stage1_root else {}
    if not scene:
        scene = _read(_path(case['directory'], run) / 'source.json', {}).get('scene_task', {})
    scenario_id = scene.get('scene', {}).get('scenario_id')
    _example(catalog, nodes['v2_sample'], case_id, '本批真实采样与随机种子',
             outputs=[original_manifest, run / 'manifest.json'])
    for stage in ('0', '0qa'):
        if stages.get(stage):
            folder = _path(stages[stage], run)
            _example(catalog, nodes['v2_library'], case_id, 'Stage ' + stage + ' 清单',
                     outputs=[folder / 'manifest.json'])
            current = folder / scenario_id if scenario_id and (folder / scenario_id).is_dir() else folder
            _recorded_calls(catalog, nodes['v2_library'], case_id, current,
                            label='Stage ' + stage)
    for stage, primary in [('1', ['scene_task.json', 'user_voice.json']),
                           ('2', ['situation_knowledge.json'])]:
        if not stages.get(stage):
            continue
        stage_root = _path(stages[stage], run)
        folder = stage_root / source_id
        if stage == '2' and source.is_dir():
            folder = source
        inputs = [folder / 'input.json']
        batch_cache = stage_root / 'stage_cache' / 'scene_task_batch' / source_id.split('-')[0]
        if stage == '1' and not inputs[0].is_file():
            inputs = sorted(batch_cache.glob('*/attempt-01-input.json'))
        _example(catalog, nodes['stage' + stage], case_id, 'Stage ' + stage + ' 实际生成',
                 inputs=inputs, outputs=[folder / name for name in primary]
                 + [folder / 'validation.json', folder / 'raw_response.txt'],
                 prompt=folder / 'prompt.md', metadata=folder / 'metadata.json')
        caches = [folder / 'stage_cache']
        if stage == '1':
            caches += [stage_root / 'stage_cache' / 'scene_task_batch' / source_id.split('-')[0]]
        for cache in caches:
            if not cache.is_dir():
                continue
            if stage == '2' and manifest.get('text_policy'):
                current = cache / 'situation_knowledge'
                for name, owner in [('generate', 'stage2'), ('semantic_repair', 'repair2'), ('judge', 'judge2')]:
                    _recorded_calls(catalog, nodes[owner], case_id, current / name,
                                    label='Stage 2 / ' + name)
                for attempt in sorted((current / 'semantic_repair').glob('attempt-*')):
                    _example(catalog, nodes['repair2'], case_id, 'Stage 2 局部修改与原子应用',
                             outputs=[attempt / name for name in
                                      ('patch.json', 'revision.json', 'candidate.json', 'validation.json', 'decision.json')])
                continue
            for prompt in sorted(cache.rglob('*prompt.md')):
                rel = str(prompt.relative_to(cache)).lower()
                owner = 'repair' + stage if 'repair' in rel else 'judge' + stage
                prefix = prompt.name[:-len('prompt.md')]
                outputs = [p for p in prompt.parent.iterdir() if p.is_file()
                           and p.suffix in {'.json', '.txt'} and p.name.startswith(prefix)
                           and 'input' not in p.name and 'metadata' not in p.name]
                _example(catalog, nodes[owner], case_id, 'Stage ' + stage + ' / ' + rel,
                         [prompt.with_name(prefix + 'input.json')], outputs, prompt,
                         prompt.with_name(prefix + 'metadata.json'))
    return source


def _script_records(catalog, nodes, script, case_id):
    for attempt in sorted((script / 'attempts').glob('attempt-*')):
        initial = (attempt / 'generate').is_dir()
        _recorded_calls(catalog, nodes['v2_script' if initial else 'v2_script_refine'],
                        case_id, attempt / ('generate' if initial else 'repair'), label=attempt.name)
        _recorded_calls(catalog, nodes['v2_script_refine'], case_id, attempt / 'patch', label=attempt.name+' 局部修改')
        _recorded_calls(catalog, nodes['v2_script' if initial else 'v2_script_refine'],
                        case_id, attempt / 'json-retries', label=attempt.name+' 独立 JSON 重试')
        _example(catalog, nodes['v2_script_refine'], case_id, attempt.name+' 程序应用的变更',
                 outputs=[attempt / 'patch.json', attempt / 'patch-applied.json',
                          attempt / 'program-anchors.json', attempt / 'dependency-closure.json'])
        _example(catalog, nodes['v2_script' if initial else 'v2_script_refine'], case_id,
                 attempt.name + ' 候选与程序校验', outputs=[attempt / 'candidate.json',
                 attempt / 'validation.json', attempt / 'heard_context.json',
                 attempt / 'event_heard_context.json', attempt / 'decision.json', attempt / 'parse-report.json'])
        _recorded_calls(catalog, nodes['v2_script_judge'], case_id, attempt / 'judge', label=attempt.name)
    for folder in sorted((script / 'length').glob('*')):
        _recorded_calls(catalog, nodes['v2_length'], case_id, folder, label=folder.name + ' 长度精简 / 重审')
        for receipt in sorted(folder.glob('attempt-*/compaction-output-normalization.json')):
            _example(catalog, nodes['v2_length'], case_id, folder.name + ' 精简只读重复字段核对记录',
                     inputs=[receipt.parent / 'compact' / 'raw_response.txt'], outputs=[receipt])
    _example(catalog, nodes['v2_length'], case_id, '最终冻结台词与长度标记',
             outputs=[script / 'script.json', script / 'length_rejected_script.json',
                      script / 'length_report.json', script / 'summary.json'])
    _example(catalog, nodes['v2_script_judge'], case_id, '最终已听边界与生成结论',
             outputs=[script / 'heard_context.json', script / 'event_heard_context.json',
                      script / 'summary.json'])


def _segment_speaker_label(audio_id, item, clip, frozen):
    """Display the physical actor, independently of the stereo channel."""
    event = next((event for event in frozen.get('side_speech', [])
                  if event.get('event_id') == audio_id), None)
    source = (event.get('speaker') if event else None) or item.get(
        'source_speaker', clip.get('source_speaker', item.get('speaker', 'user')))
    # Audio execution appends a profile hash to distinguish physical actors.
    source = str(source).split('-', 1)[0]
    return {'third_party': '旁人', 'machine': '机器', 'assistant': '助手',
            'user': '用户'}.get(source, '助手' if item.get('speaker') == 'assistant' else '用户')


def _generation_controls(catalog, case, run, manifest, directory, source, frozen):
    """Read actual profiles and frozen plans, including upstream failed cases."""
    original = _read(_path(case['source_manifest'], run), {}) if case.get('source_manifest') else manifest
    copied = _read(directory / 'source.json', {})
    stages = case.get('stages', manifest.get('stages', {}))
    source_id = case.get('source_case_id', source.name)
    voice_path = (_path(stages['1'], run) / source_id / 'user_voice.json'
                  if stages.get('1') else source / 'user_voice.json')
    voice = _read(voice_path, copied.get('user_voice') or {})
    profile = voice.get('interaction_profile')
    candidate = next((item for item in manifest.get('candidates', [])
                      if item.get('sequence') == case.get('batch_candidate_sequence')), {})
    plan = case.get('dialogue_length_plan') or copied.get('dialogue_length_plan') or candidate.get('dialogue_length_plan')
    axes = case.get('sampled_interaction_axes') or candidate.get('sampled_interaction_axes')
    # Batched children contain one frozen plan even if Stage 1 never publishes.
    if not plan and len(original.get('dialogue_length_plans', [])) == 1:
        plan = original['dialogue_length_plans'][0]
    if not axes and len(original.get('sampled_interaction_axes', [])) == 1:
        axes = original['sampled_interaction_axes'][0]
    controls = {}
    if plan:
        actual = len(frozen['rounds']) if frozen.get('rounds') else None
        tier = {'short': '短篇', 'medium': '中篇', 'long': '长篇'}.get(plan['tier'], plan['tier'])
        controls.update(dialogue_length_plan=copy.deepcopy(plan), actual_script_turns=actual,
                        dialogue_length_summary=f"{tier} · 目标 {plan['target_turns']} 轮 · "
                        +(f'实际剧本 {actual} 轮' if actual is not None else '剧本未定稿'))
    if profile:
        summary = ' · '.join(values.get(profile.get(key), str(profile.get(key)))
                             for key, values in PROFILE_LABELS.items())
        reference = catalog.artifact(voice_path, 'Stage 1 原始 user_voice.json') if voice_path.is_file() else (
            catalog.artifact(directory / 'source.json', '冻结 source.json 中的 user_voice')
            if (directory / 'source.json').is_file() else None)
        controls.update(interaction_profile=copy.deepcopy(profile), interaction_profile_summary=summary,
                        user_profile_source=reference)
    elif axes:
        summary = ' · '.join(values.get(axes.get(key), str(axes.get(key)))
                             for key, values in PROFILE_LABELS.items())
        controls.update(sampled_interaction_axes=copy.deepcopy(axes),
                        interaction_profile_summary='采样轴：' + summary + '；原始 user_voice 与画像备注未生成')
    return controls


def _ambient_record_paths(audio, result):
    """Use only persisted acoustic records; never infer an implemented mix."""
    actual = result.get('ambient_audio') or {}
    return _existing([
        _path(actual.get('provenance_path', 'ambient-provenance.json'), audio),
        _path(actual.get('frozen_plan_path', 'frozen-ambient-plan.json'), audio),
    ])


def _soundscape_controls(catalog, directory, source, audio, result):
    """Keep V2.3 soundscape intentions distinct from actual mixed counts."""
    copied = _read(directory / 'source.json', {})
    situation_path = source / 'situation_knowledge.json'
    situation = _read(situation_path, copied.get('situation_knowledge') or {})
    plan = situation.get('soundscape_plan')
    control = copied.get('ambient_control')
    if not isinstance(plan, dict) and not isinstance(control, dict) and not result.get('ambient_audio'):
        return {}
    plan, control = plan or {}, control or {}
    counts = result.get('counts', {})
    recorded = ({key: counts[key] for key in ('ambient_layers', 'environment_events',
                                             'discrete_background_events')}
                if all(key in counts for key in ('ambient_layers', 'environment_events',
                                                'discrete_background_events')) else None)
    mode = plan.get('mode', control.get('mode'))
    mode_label = {'quiet': '安静样本', 'layered': '分层声景'}.get(mode, '声景计划')
    if recorded is not None:
        summary = (f"{mode_label} · 持续声层 {recorded['ambient_layers']} 层"
                   f" · 离散旁音 {recorded['discrete_background_events']} 次"
                   f"（环境音 {recorded['environment_events']} / 旁话 {counts.get('side_speech', 0)}）")
    else:
        target = control.get('discrete_event_target')
        summary = (f"{mode_label} · 计划持续声层 {len(plan.get('ambient_layers', []))} 层"
                   + (f' · 离散旁音目标 {target} 次' if target is not None else '')
                   + ' · 尚无完整混音')
    plan_source = situation_path if situation_path.is_file() else directory / 'source.json'
    references = ([catalog.artifact(plan_source, 'Stage 2 声景计划与来源')]
                  if plan_source.is_file() else [])
    references += [catalog.artifact(path) for path in _ambient_record_paths(audio, result)]
    timeline = _read(audio / 'timeline.json', {})
    sample_rate = timeline.get('sample_rate', result.get('audio', {}).get('sample_rate', 16000))
    schedule = [{'kind': item['kind'], 'source_id': item.get('source_id'), 'asset_id': item.get('asset_id'),
                 'start_seconds': item['start_frame'] / sample_rate,
                 'duration_seconds': item['frames'] / sample_rate}
                for item in timeline.get('placements', [])
                if item.get('kind') in {'ambient_layer', 'environment_event'}]
    return {'soundscape_plan': copy.deepcopy(plan), 'ambient_control': copy.deepcopy(control),
            'soundscape_summary': summary, 'ambient_recorded_counts': recorded,
            'soundscape': {'mode': mode, 'summary': summary, 'plan': copy.deepcopy(plan),
                          'control': copy.deepcopy(control), 'recorded_counts': recorded,
                          'schedule': schedule, 'files': references}}


def _preserved_case_history(catalog, node, case_id, run, manifest, manifest_path):
    """Attach actual old outcomes as evidence, never as additional case rows."""
    history = []
    for index, original in enumerate(manifest.get('case_history', []), 1):
        if original.get('original_case_id') != case_id:
            continue
        directory = _path(original['directory'], manifest_path.parent)
        evidence = original.get('batch_outcome', {}).get('evidence')
        failures = ([ _path(evidence, manifest_path.parent)] if evidence else
                    sorted((directory / 'model-calls').glob('*/request-failure.json')))
        source_manifest = (_path(original['source_manifest'], manifest_path.parent)
                           if original.get('source_manifest') else None)
        source_run = _read(source_manifest, {}) if source_manifest else {}
        profile = original.get('source_profile') or original.get('profile') or source_run.get('profile')
        site_label = {'gpugeek': 'GPUgeek', 'qiniu-chat': '七牛（qiniu-chat）'}.get(profile, profile or '未记录站点')
        timeout = original.get('text_request_timeout_seconds', source_run.get('text_request_timeout_seconds'))
        script = _path(original['script_directory'], manifest_path.parent) if original.get('script_directory') else directory / 'script'
        candidates = sorted((script / 'attempts').glob('*/candidate.json'))
        candidate_files = []
        if candidates:
            attempt = candidates[-1].parent
            candidate_files = [attempt / 'candidate.json', attempt / 'validation.json',
                               attempt / 'judge' / 'input.json', attempt / 'judge' / 'prompt.md',
                               attempt / 'judge' / 'result.json']
        completed = original.get('status') == 'completed'
        completed_files = []
        if completed:
            old_audio = (_path(original['audio_directory'], manifest_path.parent)
                         if original.get('audio_directory') else directory / 'audio')
            completed_files = [script / 'script.json', old_audio / 'result.json']
        files = _existing([*failures, directory / 'result.json',
                           *candidate_files, *completed_files, source_manifest,
                           manifest_path.parent / 'parent-manifest.json'])
        reason = ('已完成的旧版本结果保留；本次从同一冻结环境重新生成，原台词与音频未覆盖。'
                  if completed else '恢复前的失败状态与原始请求记录保留。')
        for failure in ([] if completed else failures):
            value = _read(failure, {})
            attempts = value.get('request_failures', [])
            if value.get('request_retries_exhausted') and attempts:
                reason = (f'{site_label} {len(attempts)} 次请求均超时，该次重试预算已耗尽。'
                          if all(a.get('error_type') == 'TimeoutError' for a in attempts)
                          else f'{site_label} 请求重试预算已耗尽，具体错误保留在原始记录中。')
                if candidates and _read(attempt / 'validation.json', {}).get('errors') == []:
                    judge = attempt / 'judge'
                    identity = _read(failure.with_name('identity.json'), {})
                    if ((judge / 'input.json').is_file() and (judge / 'prompt.md').is_file()
                            and not (judge / 'result.json').is_file()
                            and identity.get('context') == _read(judge / 'input.json')
                            and identity.get('prompt') == (judge / 'prompt.md').read_text()):
                        reason = '首稿已通过程序校验；内容 Judge 的 API 请求重试耗尽，尚无 Judge 判定。'
                        reason += '请求错误：' + ' → '.join(a.get('error_type', '未记录') for a in attempts) + '。'
                break
        _example(catalog, node, case_id, ('旧版本结果 · ' if completed else '历史保留 · ') + site_label, outputs=files)
        history.append({'history_id': original.get('case_id', 'history-' + str(index)),
                        'original_case_id': case_id, 'status': original.get('status'),
                        **({'history_label': original.get('history_label', '已完成旧版本')} if completed else {}),
                        'failure_phase': original.get('failure_phase'), 'reason': reason,
                        'source_profile': profile, 'site_label': site_label,
                        **({'text_request_timeout_seconds': timeout} if timeout is not None else {}),
                        'files': [catalog.artifact(path) for path in files]})
    return history


def _report_build_provenance(site, manifest_path):
    """Keep current presentation hashes separate from generation-time records."""
    paths = ['duplex_v2/report.py', 'results_explorer.py', 'build_episode_script_bundle.py',
             'workflow_explorer.py', 'report_assets/workflow_explorer.js',
             'report_assets/workflow_explorer.css', 'report_assets/results_explorer.js',
             'duplex_v2/script_reader.py', 'report_assets/final_script_reader.css',
             'report_assets/final_script_reader.js']
    value = {'schema': 'duplex_v2.report_build_provenance.v1',
             'created_utc': datetime.now(timezone.utc).isoformat(),
             'source_manifest': str(manifest_path),
             'source_manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
             'scope': '本次报告构建源码；生成时的源码快照与冻结源证据仍以原运行记录为准。',
             'files': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}}
    path = site / 'report-build-provenance.json'
    _write(path, value)
    return {'path': path.name, 'source_path': path.name, 'label': '本次报告构建源码哈希',
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'format': 'json',
            'text': path.read_text()}


def _rollout(catalog, audio, script, case, status, site, case_id):
    result = _read(audio / 'result.json', {})
    timeline = _read(audio / 'timeline.json', {})
    assets = _read(audio / 'assets.json', {})
    frozen = _read(script / 'script.json', {})
    sample_rate = timeline.get('sample_rate', result.get('audio', {}).get('sample_rate', 24000))
    references = {}
    for name in ('result.json', 'integrity.json', 'timeline.json', 'assets.json', 'heard-context.json',
                 'event-heard-context.json', 'interruption-onset.json'):
        if (audio / name).is_file():
            references[name] = catalog.artifact(audio / name)
    for path in _ambient_record_paths(audio, result):
        references[path.name] = catalog.artifact(path)
    if (script / 'summary.json').is_file():
        references['script/summary.json'] = catalog.artifact(script / 'summary.json')
    rows = []
    responses = {row.get('response_id', row.get('audio_id')): row
                 for row in timeline.get('responses', [])}
    for item in timeline.get('placements', []):
        audio_id = item['audio_id']
        speaker = item.get('speaker', 'user')
        kind = item.get('kind', 'deliver')
        if kind in {'ambient_layer', 'environment_event'}:
            # Acoustic sources have a separate schedule, not invented A/U text.
            continue
        response = responses.get(audio_id, {})
        clip = assets.get('clips', {}).get(audio_id, {})
        full_text = clip.get('text', item.get('text', ''))
        text = item.get('text', '')
        if speaker == 'assistant' and response.get('played_chars') is not None:
            text = full_text[:response['played_chars']]
        unplayed = full_text[len(text):] if speaker == 'assistant' and full_text.startswith(text) else ''
        role = ('助手' if speaker == 'assistant' else '用户')
        speaker_label = _segment_speaker_label(audio_id, item, clip, frozen)
        label = {'backchannel': '用户附和 · 助手继续',
                 'side_speech': speaker_label + '背景旁语'}.get(kind, role)
        if speaker == 'assistant' and unplayed:
            label += '（被插话截断）'
        rows.append({'speaker': speaker, 'speaker_label': speaker_label, 'audio_id': audio_id,
                     'title': audio_id + ' · ' + label,
                     'short_label': audio_id, 'kind': kind, 'kind_label': label, 'text': text,
                     'unplayed_text': unplayed, 'start_seconds': item.get('speech_onset_frame',item['start_frame']) / sample_rate,
                     'duration_seconds': (item['start_frame']+item['frames']-item.get('speech_onset_frame',item['start_frame'])) / sample_rate,
                     'outcome_note': '按冻结锚点执行；原全文、已播前缀与对齐分别保留。',
                     'files': [{'label': name, 'path': '../../' + ref['path']}
                               for name, ref in references.items()
                               if name in {'assets.json', 'heard-context.json', 'event-heard-context.json', 'timeline.json'}]})
    rows.sort(key=lambda row: (row['start_seconds'], row['audio_id']))
    failures = '; '.join(str(item.get('audio_id', '片段'))+'：'+str(item.get('error', item.get('error_type', '失败')))
                         for item in result.get('failures', []) if isinstance(item, dict))
    reason = case.get('error') or result.get('failure_reason') or result.get('failure') or result.get('error') or failures or '本案例尚未完成生成或音频核验。'
    if not isinstance(reason, str):
        reason = json.dumps(reason, ensure_ascii=False)
    view = {'execution_mode': MODE, 'status': status, 'public_view': True, 'tts_backend': 'qwen3',
            'segments': rows, 'failure_reason': reason, 'status_label': '失败保留' if status == 'failed' else '尚未完成',
            'status_files': [{'label': name, 'path': '../../' + ref['path']} for name, ref in references.items()],
            'total_turns': len(frozen.get('rounds', [])),
            'delivered_turns': len(result.get('delivered_turns', [])) if isinstance(result.get('delivered_turns'), list)
                               else result.get('delivered_turns', 0),
            'duration_seconds': result.get('audio', {}).get('duration_seconds', 0),
            'interruptions': result.get('counts', {}).get('interruptions',
                             sum(event.get('kind') == 'user_audio_started' and event.get('overlap_with_assistant', False)
                                 for event in timeline.get('events', []))),
            'wall_seconds': result.get('wall_elapsed_seconds', result.get('wall_seconds', 0)),
            'event_summary': json.dumps(timeline.get('events', []), ensure_ascii=False, indent=2),
            'audit_note': '本页呈现离线审查后的静态目标数据；不将它标为实时自主交互轨迹。',
            'result_path': '../../' + references.get('result.json', {}).get('path', 'workflow-data.json'),
            'integrity_path': '../../' + references.get('integrity.json', references.get('result.json', {})).get('path', 'workflow-data.json'),
            'timeline_path': '../../' + references.get('timeline.json', {}).get('path', 'workflow-data.json'),
            'archive_path': '../../workflow-records.zip'}
    warnings = result.get('anchor_boundary_warnings', [])
    if result.get('speech_onset_policy'):
        view['audit_note'] += ' 插话已去除前导静音，保留30毫秒起声保护；只在同一已听前缀范围内校准时机，助手停声锚点保持原样。'
    if warnings:
        source = references.get('integrity.json') or references.get('result.json')
        view['alignment_warnings'] = {
            'count': len(warnings),
            'note': f'有 {len(warnings)} 项标点对齐警告：仅涉及无声标点归属，沿用同一发音片段末尾；原计划与实际已听前缀均保留。',
            'source_path': '../../' + source['path'] if source else None}
    omitted = result.get('omitted_backchannels', [])
    if result.get('acoustic_interruption_warnings'):
        existing = view.get('alignment_warnings',{})
        view['alignment_warnings'] = {'count':existing.get('count',0)+result['acoustic_interruption_warnings'],
            'note':existing.get('note','')+' 插话人声重叠警告：原锚点范围内没有足够的实际人声重叠，未移动语义锚点或助手停声点。',
            'source_path':'../../'+references['interruption-onset.json']['path']}
    if omitted:
        ids = '、'.join(item['event_id']+'“'+item['text']+'”' for item in omitted)
        note = f'附和对齐警告：{ids}在两次重合成后仍对齐失败，已从最终音频和已听历史省略；原录音和失败保留。'
        existing = view.get('alignment_warnings', {})
        view['alignment_warnings'] = {'count': existing.get('count',0)+len(omitted),
            'note': (existing.get('note', '')+' '+note).strip(), 'kind': 'audio_alignment',
            'source_path': view['integrity_path']}
    if rows:
        source = _path(result['audio']['path'], audio)
        target = site / 'audio' / case_id
        target.mkdir(parents=True, exist_ok=True)
        original = target / source.name
        mp3 = target / 'dialogue.mp3'
        reuse = case.get('audio_reuse')
        if reuse is not None:
            # Explicit evidence is mandatory; never reuse stale or merely same-sized audio.
            if not isinstance(reuse, dict):
                raise ValueError(case_id + ': invalid audio reuse evidence')
            expected_source, expected_mp3 = reuse.get('source_wav_sha256'), reuse.get('mp3_sha256')
            for path, expected in ((source, expected_source), (original, expected_source), (mp3, expected_mp3)):
                if not isinstance(expected, str) or len(expected) != 64 or not path.is_file():
                    raise ValueError(case_id + ': audio reuse evidence does not match ' + str(path))
                with path.open('rb') as stream:
                    actual = hashlib.file_digest(stream, 'sha256').hexdigest()
                if actual != expected:
                    raise ValueError(case_id + ': audio reuse evidence does not match ' + str(path))
        else:
            if not original.is_file() or original.stat().st_size != source.stat().st_size:
                shutil.copy2(source, original)
            subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-y', '-i', str(source),
                            '-codec:a', 'libmp3lame', '-q:a', '3', str(mp3)], check=True)
        view['audio_path'] = '../../' + str(mp3.relative_to(site))
        view['web_audio'] = {'original_path': '../../' + str(original.relative_to(site))}
        spoken = sum(row['kind'] == 'backchannel' for row in rows)
        view['backchannel_summary'] = {
            'spoken': spoken, 'passed': spoken if status == 'completed' else 0,
            'note': '附和位置在离线剧本中固定；混音保持助手原音轨连续。',
            'first_segment_index': next((i for i, row in enumerate(rows) if row['kind'] == 'backchannel'), None),
            'proof_path': view['integrity_path']}
    return view, result, references


def _canonical(run, summary, output):
    subprocess.run([sys.executable, '-B', str(ROOT / 'build_episode_script_bundle.py'),
                    '--run-dir', str(run), '--report-summary', str(summary), '--output', str(output)],
                   cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def _build_case_report(run, site, manifest, *, version=None, manifest_path=None):
    cases = manifest.get('cases', [])
    if not cases:
        raise ValueError('V2 report requires the actual sampled case manifest')
    site.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(site, inline_text=not manifest.get('report_lazy_records', False))
    nodes = _nodes(catalog, script_max_repairs=manifest.get('script_max_repairs', 3),
                   script_repair_policy=manifest.get('script_repair_policy'),
                   text_policy=manifest.get('text_policy'), script_execution_policy=manifest.get('script_execution_policy'),
                   language=manifest.get('language','zh'), workflow_version=manifest.get('workflow_version'),
                   script_feedback_policy=manifest.get('script_feedback_policy'),
                   length_compaction_policy=manifest.get('length_compaction_policy'),
                   compaction_output_policy=manifest.get('compaction_output_policy'))
    manifest_path = manifest_path or run / 'manifest.json'
    manifest_ref = catalog.artifact(manifest_path)
    frozen_rerun = manifest.get('schema') in {'duplex-v2.frozen-source-rerun.v1',
                                             'duplex-v2.same-source-relay-recovery.v1'}
    report_build = _report_build_provenance(site, manifest_path) if frozen_rerun else None
    data = {'schema': 'duplex-workflow-explorer.v1', 'execution_mode': MODE,
            'created_utc': datetime.now(timezone.utc).isoformat(), 'model': manifest.get('model'),
            'compact_view': True, 'case_navigation': 'expandable_list', 'cases': [],
            'default_case': next((c['case_id'] for c in cases if c.get('status') == 'completed'), cases[0]['case_id']),
            'dialogue_versions': [], 'nodes': catalog.nodes,
            'traces': [], 'failure_reviews': [], 'visibility': [], 'source_files': catalog.files,
            'results_url': 'index.html', 'dialogue_audio_url': '',
            'phase_definitions': [('offline', '0–3', '采样、环境与完整剧本', '采样 → 环境 → 整段剧本 → Judge / refine'),
                                  ('interaction', '4', '冻结剧本的音频执行', '并行 TTS → 对齐 → 静态调度 → 混音与核验'),
                                  ('engineering', 'E', '工程记录（按需查看）', '调用来源、阶段耗时与失败保留')]}
    if version:
        data['workflow_version'] = {key: version[key] for key in ('id', 'label')}
    data['reasoning_effort'] = manifest.get('reasoning_effort')
    if manifest.get('text_policy'):
        data['text_policy'] = copy.deepcopy(manifest['text_policy'])
        data['soundscape_policy'] = copy.deepcopy(manifest.get('soundscape_policy'))
    data['script_max_repairs'] = manifest.get('script_max_repairs', 3)
    if manifest.get('initial_cohort'):
        data['initial_cohort'] = copy.deepcopy(manifest['initial_cohort'])
    if report_build:
        data['report_build_provenance'] = report_build
    for case in cases:
        case_id = case['case_id']
        if not case_id.replace('-', '').replace('_', '').isalnum():
            raise ValueError('Unsafe V2 case_id')
        directory = _path(case['directory'], run)
        script = _path(case.get('script_directory', directory / 'script'), run)
        audio = _path(case.get('audio_directory', directory / 'audio'), run)
        source = _offline_records(catalog, nodes, case, run, manifest, case_id)
        if frozen_rerun:
            provenance_path = manifest.get('rerun_provenance_path') or manifest.get('relay_recovery_provenance_path')
            provenance = _path(provenance_path, manifest_path.parent) if provenance_path else None
            _example(catalog, nodes['v2_records'], case_id, '冻结环境重跑、换站与本次报告来源',
                     inputs=[manifest_path], outputs=[provenance, directory / 'initial-request-equivalence.json'])
            nodes['v2_records']['examples'][-1]['outputs'].append(report_build)
        _script_records(catalog, nodes, script, case_id)
        if case.get('audio_stop_selection_file'):
            selection = _path(case['audio_stop_selection_file'], run)
            recovery = selection.parent
            if case.get('parent_script_directory') and _path(case['parent_script_directory'], run) != script:
                _script_records(catalog, nodes, _path(case['parent_script_directory'], run), case_id)
            _example(catalog, nodes['v2_alignment'], case_id, '程序停声精确边界改选',
                     inputs=[selection], outputs=[recovery/'result.json'])
            _example(catalog, nodes['v2_script_judge'], case_id, '停声改选后的原Judge完整重审',
                     inputs=[recovery/'review/judge/input.json'], outputs=[recovery/'decision.json'],
                     prompt=recovery/'review/judge/prompt.md', metadata=recovery/'review/judge/metadata.json')
        result = _read(audio / 'result.json', {})
        integrity = _read(audio / 'integrity.json', result.get('integrity', {}))
        integrity_passed = integrity.get('status') in {'passed', 'pass'} or integrity.get('passed') is True
        status = case.get('status', 'pending')
        if status == 'completed' and (result.get('status') != 'completed' or not integrity_passed):
            raise ValueError(case_id + ': completed case lacks completed audio and passing integrity')
        if status not in {'completed', 'failed', 'partial', 'pending'}:
            status = 'partial'
        frozen = _read(script / 'script.json', {})
        scene = _read(source / 'scene_task.json', {})
        if not scene:
            scene = _read(directory / 'source.json', {}).get('scene_task') or {}
        if not scene:
            stages = case.get('stages', manifest.get('stages', {}))
            if stages.get('1'):
                source_id = case.get('source_case_id', source.name)
                scene = _read(_path(stages['1'], run) / source_id / 'scene_task.json', {})
        label = frozen.get('title') or scene.get('task_instance', {}).get('goal') or case.get('display_label') or case_id
        label = label if len(label) <= 45 else label[:44] + '…'
        view, result, references = _rollout(catalog, audio, script, case, status, site, case_id)
        acceptance_ref = None
        if case.get('acceptance_review_file'):
            review_path = _path(case['acceptance_review_file'], manifest_path.parent)
            review = _read(review_path, {})
            if review.get('status') != status:
                raise ValueError(case_id + ': acceptance review is missing or differs from the case status')
            acceptance_ref = catalog.artifact(review_path, '长度准入复核结果')
            view['status_files'].append({'label': '长度准入复核结果', 'path': '../../'+acceptance_ref['path']})
            _example(catalog, nodes['v2_length'], case_id, '当前长度准入复核', outputs=[review_path])
        if ((version and version.get('workflow_version') in {'v2.5', 'v2.6', 'v2.7', 'v2.8'})
                or (version and version.get('id') == 'v2_5_routing_default_soundscape')
                or (version is None and manifest.get('workflow_version') in {'v2.5', 'v2.6', 'v2.7', 'v2.8'})):
            script_file = script / 'script.json'
            if script_file.is_file() and not (acceptance_ref and status == 'failed'):
                waivers = _read(script / 'summary.json', {}).get('semantic_waivers', [])
                script_ref = catalog.artifact(script_file, '按本版规则定稿的完整剧本' if waivers else '审查通过的完整定稿剧本')
                view['final_script_reader'] = {'script': frozen, 'source_path': '../../' + script_ref['path'],
                    'semantic_waivers': waivers, 'omitted_backchannels': result.get('omitted_backchannels', []),
                    'language': case.get('language',manifest.get('language','zh'))}
            else:
                view['final_script_reader'] = {'script': None}
        history = _preserved_case_history(catalog, nodes['v2_records'], case_id, run, manifest, manifest_path)
        if history:
            view['preserved_case_history'] = [{**item, 'files': [
                {**reference, 'path': '../../' + reference['path']} for reference in item['files']]}
                for item in history]
        controls = _generation_controls(catalog, case, run, manifest, directory, source, frozen)
        soundscape = _soundscape_controls(catalog, directory, source, audio, result)
        if soundscape:
            controls.update(soundscape)
            view['soundscape'] = {**soundscape['soundscape'], 'files': [
                {**item, 'path': '../../' + item['path']} for item in soundscape['soundscape']['files']]}
            purposes = {
                'stage2': '同一次情境生成规划声景：从冻结素材目录选择持续声层与环境事件，并定义第三方旁话来源；安静模式与离散目标由程序预采样。',
                'judge2': '检查声景与当前地点一致、素材来自冻结目录、安静模式严格为空，离散环境事件加旁话满足冻结目标。',
                'v2_script': '生成整段台词、双工锚点与离散环境事件位置；环境音使用冻结素材，第三方旁话有台词并独立配声。',
                'v2_script_judge': '检查台词、已听边界与事件可执行性；持续声层不计离散旁音，环境音与旁话不引入新的任务事实。',
                'v2_tts': '仅有台词的用户、助手、附和与第三方旁话并行 TTS；无台词环境音读取已冻结的本地素材。',
                'v2_schedule': '将台词锚点映射到真实采样；另行记录持续声层与离散环境音时段，背景不进入双方文字历史。',
                'v2_mix': '将持续声层、离散环境音和已合成旁话按冻结计划混入用户声道；保留素材、许可、原始哈希、裁剪、循环与增益来源。',
                'v2_integrity': '核对真实声景计数、混音时段、背景素材来源与已听前缀；只有实际完整音频通过核验才标记完成。',
            }
            for identifier, purpose in purposes.items():
                nodes[identifier].setdefault('purpose_by_case', {})[case_id] = purpose
        if controls.get('dialogue_length_summary'):
            view['dialogue_length_summary'] = controls['dialogue_length_summary']
        if controls.get('interaction_profile'):
            reference = controls.get('user_profile_source')
            view['user_interaction_profile'] = {
                'value': controls['interaction_profile'], 'summary': controls['interaction_profile_summary'],
                'source_path': '../../' + reference['path'] if reference else None}
            nodes['stage1'].setdefault('purpose_by_case', {})[case_id] = '同一次 Stage 1 调用生成任务、用户关系、声音与独立表达画像；程序采样的七个轴保持冻结，模型生成 2–3 条表达备注。'
            nodes['judge1'].setdefault('purpose_by_case', {})[case_id] = '检查任务、声音与表达备注；七个采样轴只读，当前情绪由具体情境承载。'
            nodes['repair1'].setdefault('purpose_by_case', {})[case_id] = '只修 Judge 指出的 goal、voice 或 behavioral_notes，再经原 Judge；不更改采样轴。'
        elif controls.get('interaction_profile_summary'):
            view['interaction_profile_pending_note'] = controls['interaction_profile_summary']
        for identifier, inputs, outputs in [
                ('v2_voices', [directory / 'source.json'], [audio / 'voice-plan.json', audio / 'manifest.json']),
                ('v2_tts', [audio / 'frozen-script.json'], [audio / 'assets.json', audio / 'manifest.json']),
                ('v2_alignment', [], [audio / 'assets.json', audio / 'integrity.json', audio / 'result.json']),
                ('v2_schedule', [audio / 'frozen-script.json'], [audio / 'timeline.json', audio / 'heard-context.json',
                                                               audio / 'event-heard-context.json']),
                ('v2_mix', [audio / 'timeline.json'], [*_ambient_record_paths(audio, result), audio / 'result.json']),
                ('v2_integrity', [audio / 'timeline.json'], [audio / 'integrity.json', audio / 'result.json']),
                ('v2_records', [directory / 'source.json'], [script / 'summary.json', audio / 'result.json']),
                ('v2_timings', [], [directory.parent / 'manifest.json', run / 'manifest.json']),
                ('v2_export', [audio / 'timeline.json', audio / 'heard-context.json'],
                 [directory / 'dataset' / 'training.jsonl', directory / 'dataset' / 'manifest.json'])]:
            _example(catalog, nodes[identifier], case_id, label + ' · 实际记录', inputs, outputs)
        first_tts_example = len(nodes['v2_tts']['examples'])
        for request in sorted((audio / 'tts').glob('*/calls/**/request.json')):
            relative = request.relative_to(audio / 'tts')
            _example(catalog, nodes['v2_tts'], case_id, '实际 TTS 请求 · ' + str(relative),
                     inputs=[request], outputs=[request.parent / 'subtitle.json',
                     request.parent / 'metadata.json', request.parent / 'raw-alignment.json'])
        if len(nodes['v2_tts']['examples']) > first_tts_example:
            nodes['v2_tts'].setdefault('default_examples_by_case', {})[case_id] = first_tts_example
        for clip in sorted((audio / 'tts').glob('*/clip.json')):
            _example(catalog, nodes['v2_alignment'], case_id, clip.parent.name + ' 原文与精确对齐',
                     inputs=[clip.parent / 'job.json'], outputs=[clip])
        for node in nodes.values():
            examples = [e for e in node['examples'] if e['dataset_case'] == case_id]
            node.setdefault('actual_by_case', {})[case_id] = ('已保存 ' + str(len(examples)) + ' 组真实记录'
                if examples else ('未触发；无此分支调用' if node['id'] in {'repair1', 'repair2', 'v2_script_refine'}
                                  else '未生成；以当前状态记录为准'))
        warning_count = len(result.get('anchor_boundary_warnings', []))
        if warning_count:
            nodes['v2_alignment']['actual_by_case'][case_id] += f' · 标点对齐警告 {warning_count} 项'
        counts = result.get('counts', {})
        turn_scope = (f"{view['total_turns']} 轮" if status == 'completed'
                      else f"已交付 {view['delivered_turns']}/{view['total_turns']} 轮"
                      if view['total_turns'] else '尚无冻结剧本')
        summary = (f"{'完整' if status == 'completed' else '失败保留' if status == 'failed' else '尚未完成'}"
                   f" · {turn_scope} · {view.get('duration_seconds', 0):.1f} 秒"
                   f" · 附和 {counts.get('backchannels', sum(s['kind'] == 'backchannel' for s in view['segments']))} 次"
                   f" · 打断 {view.get('interruptions', 0)} 次")
        summary += (' · ' + soundscape['soundscape_summary'] if soundscape else
                    f" · 旁音 {counts.get('side_speech', sum(s['kind'] == 'side_speech' for s in view['segments']))} 次")
        if warning_count:
            summary += f' · 标点对齐警告 {warning_count} 项'
        if result.get('omitted_backchannels'):
            summary += f" · 对齐失败跳过附和 {len(result['omitted_backchannels'])} 次"
        if _read(script / 'summary.json', {}).get('semantic_waivers'):
            summary += ' · 含第三次精简语义未通过标记'
        if controls.get('dialogue_length_summary'):
            summary = controls['dialogue_length_summary'] + ' · ' + summary
        if controls.get('interaction_profile_summary'):
            summary += ' · 用户：' + controls['interaction_profile_summary']
        if history:
            history_kind = '旧版本/失败记录' if any(h.get('status') == 'completed' for h in history) else '恢复前失败'
            summary += f' · {history_kind} {len(history)} 条保留'
        status_source = acceptance_ref or references.get('result.json')
        if not acceptance_ref and frozen_rerun and directory.is_relative_to(manifest_path.parent.resolve()):
            status_source = (catalog.artifact(directory / 'result.json', '本次案例状态')
                             if (directory / 'result.json').is_file() else manifest_ref)
        elif not status_source and case.get('source_manifest'):
            source_manifest = _path(case['source_manifest'], run)
            if source_manifest.is_file():
                status_source = catalog.artifact(source_manifest, '原始子任务状态清单')
        data['cases'].append({'id': case_id, 'label': label, 'summary': summary, 'status': status,
                              'validation_status': 'passed' if status == 'completed' and integrity_passed else 'not_passed',
                              'status_source': status_source or manifest_ref, **controls,
                              **({'case_history': history} if history else {})})
        destination = site / 'dialogues' / case_id
        page = {'title': label + ' · V2 静态剧本目标数据', 'report_intro': summary,
                'omit_canonical_case': True, 'results_explorer': {'view': 'synthetic_rollout',
                'case_id': case_id, 'omit_canonical_case': True, 'synthetic_rollout': view}}
        _write(destination / 'report-summary.json', page)
        _canonical(run, destination / 'report-summary.json', destination / 'index.html')
        data['dialogue_versions'].append({'id': case_id, 'dataset_case': case_id, 'label': label,
            'path': 'dialogues/' + case_id + '/index.html', 'summary': summary,
            'status': 'completed' if status == 'completed' else 'failed_preserved',
            'description': summary + '。整段台词与事件在音频生成前冻结；可查看实际 Prompt、候选和审查记录。'})
        if status == 'failed':
            data['failure_reviews'].append({'dataset_case': case_id, 'episode': case_id,
                'where': 'V2 生成或音频执行', 'recorded': view['failure_reason'],
                'latest': '保留原始失败，未记为通过。', 'node': 'v2_records'})
    if manifest.get('report_lazy_records'):
        defer_record_text(data)
    data = organize_workflow(data)
    validate_catalog(data)
    _write(site / 'workflow-data.json', data)
    reasoning_note = ('推理强度使用服务默认值，不显式指定 effort。' if manifest.get('text_policy') else
                      '推理强度 ' + str(manifest['reasoning_effort']) + '。'
                      if manifest.get('reasoning_effort') else '')
    model_note = ('Luna 负责 Stage 0/0qa、Stage 1 的生成和修复，以及第一次长度精简；'
                  'Sol 负责其余生成、修复和全部语义审核。' if manifest.get('text_policy') else
                  '所有文本生成、Judge 与 refine 使用 GPT-5.6-sol。')
    page = {'title': version['label'] if version else 'V2 · 完整剧本与并行语音生成',
            'report_intro': '从领域和场景随机采样开始，先生成并审查整段双人台词与双工事件，再并行 TTS、精确对齐和静态混音。' + model_note + reasoning_note,
            'omit_canonical_case': True, 'workflow_explorer': data}
    delivery_note = manifest.get('fresh_audio_delivery_note')
    if isinstance(delivery_note, str) and delivery_note.strip():
        page['report_intro'] += ' ' + delivery_note.strip()
    _write(site / 'report-summary.json', page)
    _canonical(run, site / 'report-summary.json', site / 'index.html')
    with zipfile.ZipFile(site / 'workflow-records.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(site / 'workflow-data.json', 'workflow-data.json')
        for path in sorted((site / 'records').rglob('*')):
            if path.is_file():
                archive.write(path, str(path.relative_to(site)))
    return {'site': str(site), 'index': str(site / 'index.html'), 'cases': len(cases),
            'completed': sum(c['status'] == 'completed' for c in data['cases']),
            'source_files': len(catalog.files), 'models_called_for_report': 0}


def _copy_frozen_site(source, destination):
    """Reuse a completed canonical report without rewriting its historical text."""
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        return
    if source in destination.parents or destination in source.parents:
        raise ValueError('Frozen report source and destination must not contain one another')
    def copy_file(src, dst):
        if Path(dst).exists():
            if os.path.samefile(src, dst):
                return dst
            Path(dst).unlink()
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return dst
    shutil.copytree(source, destination, copy_function=copy_file, dirs_exist_ok=True)


def _build_versioned_report(run, site, manifest):
    definitions = manifest['versions']
    if not isinstance(definitions, list) or not definitions:
        raise ValueError('Versioned V2 report requires nonempty versions')
    ids = [item.get('id') for item in definitions]
    if len(ids) != len(set(ids)) or any(not isinstance(identifier, str) or
            not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) for identifier in ids):
        raise ValueError('Workflow version IDs must be unique safe identifiers')
    default = manifest.get('default_version', ids[-1])
    if default not in ids:
        raise ValueError('Unknown default workflow version')
    site.mkdir(parents=True, exist_ok=True)
    versions, all_cases = [], []
    for definition in definitions:
        if not definition.get('label'):
            raise ValueError('Workflow versions require change-based labels')
        identifier = definition['id']
        destination = site / 'versions' / identifier
        if definition.get('site_directory'):
            source = _path(definition['site_directory'], run)
            if not (source / 'index.html').is_file():
                raise ValueError('Frozen workflow version is missing its canonical page')
            data = _read(source / 'workflow-data.json', {})
            validate_catalog(data)
            if definition.get('cases') and {
                    item['case_id']: item.get('status') for item in definition['cases']} != {
                    item['id']: item.get('status') for item in data['cases']}:
                raise ValueError('Frozen workflow version does not match its recorded cases/statuses')
            _copy_frozen_site(source, destination)
            reused = True
        else:
            version_manifest = copy.deepcopy(manifest)
            if definition.get('source_manifest'):
                version_manifest.update(_read(_path(definition['source_manifest'], run), {}))
            version_manifest.pop('versions', None)
            version_manifest.update({key: value for key, value in definition.items()
                                     if key not in {'id', 'label', 'site_directory', 'source_manifest'}})
            if not version_manifest.get('cases'):
                raise ValueError('Workflow version requires its actual independently sampled cases')
            version_manifest_path = (_path(definition['source_manifest'], run)
                                     if definition.get('source_manifest') else run / 'manifest.json')
            _build_case_report(run, destination, version_manifest, version=definition,
                               manifest_path=version_manifest_path)
            data = _read(destination / 'workflow-data.json', {})
            reused = False
        cases = copy.deepcopy(data['cases'])
        prefix = 'versions/' + identifier + '/'
        versions.append({'id': identifier, 'label': definition['label'], 'path': prefix + 'index.html',
                         'catalog_path': prefix + 'workflow-data.json',
                         'records_path': prefix + 'workflow-records.zip', 'cases': cases,
                         'completed': sum(c['status'] == 'completed' for c in cases),
                         'failed': sum(c['status'] == 'failed' for c in cases),
                         'frozen_site_reused': reused, 'source_runs': definition.get('source_runs', []),
                         'reasoning_effort': definition.get('reasoning_effort'),
                         'script_max_repairs': definition.get('script_max_repairs', data.get('script_max_repairs'))})
        all_cases.extend({'id': identifier + ':' + item['id'], 'case_id': item['id'],
                          'workflow_version': identifier, 'status': item['status']} for item in cases)
    catalog = Catalog(site)
    catalog.artifact(run / 'manifest.json', '版本与各自采样来源清单')
    data = {'schema': 'duplex-workflow-versions.v1', 'execution_mode': MODE,
            'created_utc': datetime.now(timezone.utc).isoformat(), 'default_version': default,
            'workflow_versions': versions, 'cases': all_cases, 'source_files': catalog.files}
    _write(site / 'workflow-data.json', data)
    summary = {'title': 'V2 · 完整剧本与并行语音生成',
               'report_intro': '选择代码版本，查看该版本独立采样的案例、完整阶段记录与双侧语音。',
               'omit_canonical_case': True, 'workflow_explorer': data}
    _write(site / 'report-summary.json', summary)
    _canonical(run, site / 'report-summary.json', site / 'index.html')
    return {'site': str(site), 'index': str(site / 'index.html'), 'versions': len(versions),
            'cases': len(all_cases), 'completed': sum(v['completed'] for v in versions),
            'models_called_for_report': 0, 'version_results': versions}


def build_report(run_dir: Path, output_dir: Path) -> dict:
    """Build canonical V2 reports, optionally switching independent versions.

    A single version uses ``manifest.cases``. Each case needs ``case_id``,
    ``directory``, ``source_directory``, ``script_directory``, ``audio_directory``
    and ``status``; absolute paths and per-case ``stages`` overrides are allowed.

    A multi-version manifest instead declares ``versions`` and optionally
    ``default_version``. Each version has ``id``, ``label`` and its own ``cases``
    plus model/budget/source metadata. ``site_directory`` reuses a frozen
    canonical site byte-for-byte; ``source_manifest`` can supply a new version's
    case/configuration fields. Case IDs are scoped to a version, never paired.
    """
    run, site = Path(run_dir).resolve(), Path(output_dir).resolve()
    manifest = _read(run / 'manifest.json', {})
    if manifest.get('versions'):
        return _build_versioned_report(run, site, manifest)
    return _build_case_report(run, site, manifest)
