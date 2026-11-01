#!/usr/bin/env python3
"""生成 Stage-1 随机样本集：UserSeed + VoiceProfile。

本脚本专门用于 Stage-1 的场景/任务/用户/声音采样，刻意在 Scene/Task/User/Voice
调用后停止，不生成 Situation、Episode Script、Rubric、Audit 或 Rollout 产物。

核心流程：
1. 从 taxonomy 采样 Domain/Scenario 组合
2. 为每个场景生成 UserSeed + VoiceProfile（批量模式，每批最多 10 个候选）
3. 通过语义 Judge 审查候选质量，不合格则进行定向 Repair
4. 将通过的样本写入 run 目录，并生成 HTML 可视化报告

设计要点：
- 批量生成 + 批量 Judge：一次 LLM 调用生成多个候选，一次 Judge 调用审查全部
- Goal Memory：跨 run 持久化已通过的 goal，避免重复
- 断点续传：通过 style checkpoint 支持中断后从已完成批次继续
- 场景身份冻结：采样后场景信息不可变，下游只能改变措辞
"""
from __future__ import annotations

from judge_output_repair import complete_checked_judge

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
from pathlib import Path
import random
import time

from minimax_client import MiniMaxClient, parse_json_text
from run_pipeline import (
    JSON_RESPONSE_FORMAT,
    _prepare_stage_value,
    _redact_secret,
    model_scene,
    stage_prompt,
    _write_json_atomic,
    _write_text_atomic,
    digest,
    environment_contract,
)
from sample_taxonomy import sample_scene
from stage_validation import validate_stage

# ── 项目根目录与 Prompt 路径 ──────────────────────────────────────────
ROOT = Path(__file__).parent
PROMPT_PATH = ROOT / "prompts/01_scene_task_user_voice.md"

# ── 任务风格体系 ──────────────────────────────────────────────────────
# Stage 1 的默认任务风格候选池。常识问答与其它风格平级，默认随机采样时也会出现。
TASK_STYLES = ("开放式互动", "结构化整理", "混合型", "常识问答")
# 命令行校验、均衡采样和全量展开共用同一个完整风格集合。
SUPPORTED_TASK_STYLES = TASK_STYLES
BALANCED_TASK_STYLES = TASK_STYLES

# ── Goal Memory 版本与路径 ────────────────────────────────────────────
GOAL_MEMORY_VERSION = 2
DEFAULT_GOAL_MEMORY_PATH = ROOT / "work/stage1-task-goal-memory.json"

# ── 旧版任务风格映射（兼容旧 taxonomy） ───────────────────────────────
LEGACY_TASK_STYLE_MAP = {
    "日常问答": "开放式互动",
    "信息整理": "结构化整理",
}

# ── 批量 Judge/Repair 常量 ────────────────────────────────────────────
# Judge 输出必须包含的 key 集合
STAGE1_BATCH_JUDGE_KEYS = {"decision", "findings"}
# Repair 输出必须包含的 key 集合
STAGE1_PATCH_KEYS = {"patches"}
# Judge 调用的最大 token 数
STAGE1_JUDGE_MAX_TOKENS = 2200
# 语义 Judge 最大尝试次数（pass/repair 循环上限）
MAX_STAGE1_JUDGE_ATTEMPTS = 2
# 每批最大候选数（LLM 单次调用的上限）
BATCH_MAX_ITEMS = 10
# 批量生成最大重试次数
BATCH_MAX_ATTEMPTS = 2
# Repair 允许修改的 JSON Patch 路径（仅 goal 和 voice_profile 可修改）
BATCH_ALLOWED_PATCH_PATHS = {"/task_instance/goal", "/voice_profile"}


def _empty_goal_memory() -> dict:
    """创建空的 Goal Memory 结构。

    Goal Memory 用于跨 run 持久化已通过的 goal，避免同一 Scenario/Style
    重复生成相似的 task goal。结构为：
    {
        "version": 2,
        "description": "...",
        "scenarios": {
            "<scenario_id>": {
                "<task_style>": [{"goal": "...", "source_run": "...", "case_id": "..."}]
            }
        }
    }
    """
    return {
        "version": GOAL_MEMORY_VERSION,
        "description": "Stage-1 goals that passed the semantic Judge; used only as model context to avoid obvious repetition.",
        "scenarios": {},
    }


def _load_goal_memory(path: Path) -> dict:
    """加载持久化的 Goal Memory，容忍文件缺失或格式错误。

    如果文件不存在或解析失败，返回空的 Goal Memory 结构。
    加载后会强制将 version 更新为当前版本，确保格式兼容。

    Args:
        path: Goal Memory JSON 文件路径

    Returns:
        有效的 Goal Memory dict
    """
    if not path.exists():
        return _empty_goal_memory()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_goal_memory()
    # 校验基本结构：必须是 dict 且 scenarios 字段必须是 dict
    if not isinstance(value, dict) or not isinstance(value.get("scenarios"), dict):
        return _empty_goal_memory()
    # 强制更新版本号，补充缺失的 description
    value["version"] = GOAL_MEMORY_VERSION
    value.setdefault(
        "description",
        "Stage-1 goals that passed the semantic Judge; used only as model context to avoid obvious repetition.",
    )
    return value


def _write_goal_memory(path: Path, memory: dict) -> None:
    """将 Goal Memory 原子写入磁盘。

    使用 _write_json_atomic 确保写入的原子性，避免中途崩溃导致文件损坏。

    Args:
        path: 目标文件路径
        memory: Goal Memory 数据
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, memory)


def _goal_memory_key(scene: dict) -> tuple[str, str]:
    """提取 Goal Memory 的索引键：(scenario_id, task_style)。

    同一 Scenario + 同一 Task Style 的 goal 历史共享一个存储槽位。

    Args:
        scene: 场景信息 dict

    Returns:
        (scenario_id, task_style) 二元组
    """
    return (str(scene.get("scenario_id", "")), str(scene.get("task_style", "")))


def _scenario_group_key(scene: dict) -> str:
    """提取场景分组键：scenario_id。

    同一 Scenario 的所有 task style 共享一个并发组，
    在 _generate_scenario_batch_group 中按 style 顺序串行生成，
    确保同一 Scenario 内的 goal 历史按序累积。

    Args:
        scene: 场景信息 dict

    Returns:
        scenario_id 字符串
    """
    return str(scene.get("scenario_id", ""))


def _memory_goals(memory: dict, scene: dict) -> list[dict]:
    """从 Goal Memory 中提取指定 Scenario + Task Style 的已通过 goal 列表。

    这些 goal 会作为上下文传给 LLM，帮助其避免生成重复的 task goal。

    Args:
        memory: Goal Memory 数据
        scene: 场景信息 dict（用于定位 scenario_id 和 task_style）

    Returns:
        已通过 goal 的列表，每项为 {"task_style": "...", "goal": "..."}
    """
    scenario_id = str(scene.get("scenario_id", ""))
    task_style = str(scene.get("task_style", ""))
    styles = memory.get("scenarios", {}).get(scenario_id, {})
    if not isinstance(styles, dict):
        return []
    output: list[dict] = []
    entries = styles.get(task_style, [])
    if not isinstance(entries, list):
        return output
    # 只保留有效条目（goal 非空）
    for item in entries:
        if isinstance(item, dict) and str(item.get("goal", "")).strip():
            output.append({"task_style": task_style, "goal": str(item["goal"])})
    return output


def _remember_goal(memory: dict, scene: dict, goal: str, source_run: str, case_id: str) -> bool:
    """将一个已通过的 goal 记录到 Goal Memory 中。

    如果该 goal 已存在（去重），则不重复添加。

    Args:
        memory: Goal Memory 数据（会被原地修改）
        scene: 场景信息 dict
        goal: 已通过的 task goal 文本
        source_run: 来源 run 的 ID
        case_id: 来源 case 的 ID

    Returns:
        True 表示新增了 goal，False 表示已存在（去重跳过）
    """
    goal = str(goal).strip()
    scenario_id, task_style = _goal_memory_key(scene)
    # 任何一个关键信息缺失则不记录
    if not scenario_id or not task_style or not goal:
        return False
    scenarios = memory.setdefault("scenarios", {})
    styles = scenarios.setdefault(scenario_id, {})
    entries = styles.setdefault(task_style, [])
    if not isinstance(entries, list):
        entries = []
        styles[task_style] = entries
    # 去重：如果 goal 已存在则跳过
    if any(isinstance(item, dict) and item.get("goal") == goal for item in entries):
        return False
    entries.append({"goal": goal, "source_run": str(source_run), "case_id": str(case_id)})
    return True


def _bootstrap_goal_memory(memory: dict, run_dirs: list[Path]) -> int:
    """从指定的历史 run 目录中显式导入已通过的 goal。

    仅导入 validation 状态为 passed 的 case 的 goal，不会隐式导入。
    这用于在新 run 开始前预热 Goal Memory，避免与历史 run 产生重复 goal。

    Args:
        memory: Goal Memory 数据（会被原地修改）
        run_dirs: 历史 run 目录列表

    Returns:
        成功导入的 goal 数量
    """
    imported = 0
    for run_dir in run_dirs:
        if not run_dir.exists():
            continue
        for case_dir in sorted(item for item in run_dir.iterdir() if item.is_dir()):
            scene_task_path = case_dir / "scene_task.json"
            validation_path = case_dir / "validation.json"
            if not scene_task_path.exists() or not validation_path.exists():
                continue
            try:
                value = json.loads(scene_task_path.read_text(encoding="utf-8"))
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            # 只导入通过验证的 case
            if validation.get("status") != "passed":
                continue
            task_instance = value.get("task_instance") if isinstance(value, dict) else None
            goal = task_instance.get("goal") if isinstance(task_instance, dict) else None
            scene = value.get("scene") if isinstance(value, dict) else None
            if not isinstance(scene, dict) or not isinstance(goal, str):
                continue
            # 补充 task_style 信息后记录到 memory
            scene_with_style = {**scene, "task_style": value.get("task_style")}
            if _remember_goal(memory, scene_with_style, goal, run_dir.name, case_dir.name):
                imported += 1
    return imported


def _validate_batch_judge_output(value: object, item_count: int) -> list[str]:
    """校验批量 Judge 的输出格式，不解读其语义内容。

    Judge 输出必须包含：
    - decision: "pass" 或 "repair"
    - findings: 问题列表（pass 时必须为空，repair 时必须非空）

    每个 finding 必须包含：item_index, target, severity, path, problem, evidence, repair
    可选字段：related_item_indices

    Args:
        value: Judge 的 JSON 输出
        item_count: 批次中的候选数量（用于校验 item_index 范围）

    Returns:
        校验错误列表，空列表表示格式正确
    """
    errors: list[str] = []
    # 顶层结构校验：必须恰好包含 decision 和 findings 两个 key
    if not isinstance(value, dict) or set(value) != STAGE1_BATCH_JUDGE_KEYS:
        return ["batch judge output must contain exactly decision and findings"]
    # decision 只能是 pass 或 repair
    if value.get("decision") not in {"pass", "repair"}:
        errors.append("batch judge decision must be pass or repair")
    findings = value.get("findings")
    if not isinstance(findings, list):
        return errors + ["batch judge findings must be an array"]
    # 每个 finding 的字段校验
    required = {"item_index", "target", "severity", "path", "problem", "evidence", "repair"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"batch judge findings[{index}] must be an object")
            continue
        # 必须包含所有 required 字段，不能有多余字段
        if not required.issubset(finding):
            errors.append(f"batch judge findings[{index}] is missing required fields")
        if set(finding) - (required | {"related_item_indices"}):
            errors.append(f"batch judge findings[{index}] has unknown fields")
        # item_index 必须是有效索引
        item_index = finding.get("item_index")
        if not isinstance(item_index, int) or isinstance(item_index, bool) or not 0 <= item_index < item_count:
            errors.append(f"batch judge findings[{index}].item_index is invalid")
        # target 只能是 goal 或 voice_profile
        if finding.get("target") not in {"goal", "voice_profile"}:
            errors.append(f"batch judge findings[{index}].target is invalid")
        # path 必须与 target 对应
        expected_path = "/task_instance/goal" if finding.get("target") == "goal" else "/voice_profile"
        if finding.get("path") != expected_path:
            errors.append(f"batch judge findings[{index}].path must be {expected_path}")
        # severity 必须是 must_fix（Stage-1 Judge 只标记必须修复的问题）
        if finding.get("severity") != "must_fix":
            errors.append(f"batch judge findings[{index}].severity must be must_fix")
        # problem/evidence/repair 必须是非空字符串
        for field in ("problem", "evidence", "repair"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                errors.append(f"batch judge findings[{index}].{field} must be a non-empty string")
        # related_item_indices 校验
        related = finding.get("related_item_indices", [])
        if not isinstance(related, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or not 0 <= item < item_count
            for item in related
        ):
            errors.append(f"batch judge findings[{index}].related_item_indices is invalid")
    # 交叉校验：pass 时 findings 必须为空，repair 时 findings 必须非空
    if value.get("decision") == "pass" and findings:
        errors.append("batch judge pass must have an empty findings array")
    if value.get("decision") == "repair" and not findings:
        errors.append("batch judge repair must contain findings")
    return errors


def _validate_batch_patch_output(value: object, item_count: int) -> list[str]:
    """校验批量 Repair 的输出格式（JSON Patch 风格）。

    Repair 输出必须包含：
    - patches: 修补操作列表

    每个 patch 必须包含：item_index, op, path, value
    - op 只允许 "replace"
    - path 只允许修改 /task_instance/goal 或 /voice_profile
    - value 必须是非空字符串

    Args:
        value: Repair 的 JSON 输出
        item_count: 批次中的候选数量

    Returns:
        校验错误列表，空列表表示格式正确
    """
    if not isinstance(value, dict) or set(value) != STAGE1_PATCH_KEYS:
        return ["batch repair output must contain exactly patches"]
    patches = value.get("patches")
    if not isinstance(patches, list):
        return ["batch repair patches must be an array"]
    errors: list[str] = []
    required = {"item_index", "op", "path", "value"}
    for index, patch in enumerate(patches):
        # 每个 patch 必须恰好包含四个字段
        if not isinstance(patch, dict) or set(patch) != required:
            errors.append(f"batch repair patches[{index}] must contain exactly item_index, op, path, value")
            continue
        # item_index 范围校验
        item_index = patch.get("item_index")
        if not isinstance(item_index, int) or isinstance(item_index, bool) or not 0 <= item_index < item_count:
            errors.append(f"batch repair patches[{index}].item_index is invalid")
        # op 只允许 replace
        if patch.get("op") != "replace":
            errors.append(f"batch repair patches[{index}].op must be replace")
        # path 只允许修改 goal 或 voice_profile
        if patch.get("path") not in BATCH_ALLOWED_PATCH_PATHS:
            errors.append(f"batch repair patches[{index}].path is not mutable")
        # value 必须是非空字符串
        if not isinstance(patch.get("value"), str) or not patch["value"].strip():
            errors.append(f"batch repair patches[{index}].value must be a non-empty string")
    return errors


def _judge_client(client: MiniMaxClient) -> MiniMaxClient:
    """为 Judge 调用创建专用的 LLM 客户端。

    如果 Judge 模型是 GPT 系列，使用独立的 OpenAI 兼容端点；
    否则复用当前客户端，切换到 Judge 模型并设置 temperature=0.0（确定性输出）。

    Args:
        client: 生成用的 LLM 客户端

    Returns:
        配置好的 Judge 客户端
    """
    if client.judge_model.lower().startswith("gpt-"):
        return MiniMaxClient.judge_from_env(client.judge_model)
    return client.for_model(client.judge_model, temperature=0.0)


def _fill_task_styles(scenes: list[tuple[str, dict]], rng: random.Random) -> None:
    """为缺少 task_style 的场景随机分配任务风格。

    处理逻辑：
    1. 旧版风格名（日常问答/信息整理）映射到新版名称
    2. 无效或缺失的风格从四种默认风格中随机分配

    Args:
        scenes: (case_id, scene) 列表，scene 会被原地修改
        rng: 随机数生成器（seed 驱动，确保可复现）
    """
    for _, scene in scenes:
        style = scene.get("task_style")
        # 旧版风格名映射
        if style in LEGACY_TASK_STYLE_MAP:
            scene["task_style"] = LEGACY_TASK_STYLE_MAP[style]
        # 无效风格随机分配
        elif style not in TASK_STYLES:
            scene["task_style"] = rng.choice(TASK_STYLES)


def _freeze_style_examples(scenes: list[tuple[str, dict]]) -> None:
    """冻结每个场景的示例任务，只保留与当前 task_style 匹配的示例。

    taxonomy 中的 example_tasks 按 task_style 分组存储，本函数只保留
    当前 case 的 task_style 对应的示例，其余丢弃。这确保模型只看到
    与目标风格一致的参考示例。

    Args:
        scenes: (case_id, scene) 列表，scene 会被原地修改

    Raises:
        ValueError: 如果场景缺少对应 task_style 的示例
    """
    for _, scene in scenes:
        examples = scene.get("example_tasks")
        if isinstance(examples, dict) and examples:
            # 按 task_style 筛选示例
            style = scene.get("task_style")
            selected = examples.get(style)
            if not isinstance(selected, list) or not selected:
                raise ValueError(f"{scene.get('scenario_id')} has no example_tasks for task_style {style}")
            scene["example_tasks"] = copy.deepcopy(selected)
        elif scene.get("example_task_seeds"):
            # 兼容旧版 taxonomy：使用 example_task_seeds 字段
            scene["example_tasks"] = copy.deepcopy(scene["example_task_seeds"])
        else:
            raise ValueError(f"{scene.get('scenario_id')} has no example_tasks")
        # 移除旧版字段，避免后续混淆
        scene.pop("example_task_seeds", None)


def _sample_scenes(
    taxonomy: dict,
    seed: int,
    count: int,
    base_manifest: Path | None = None,
    forced_task_style: str | None = None,
    scenario_ids: list[str] | None = None,
    distinct_domains: bool = False,
    balanced_task_styles: bool = False,
    all_task_styles: bool = False,
) -> list[tuple[str, dict]]:
    """从 taxonomy 中采样场景，支持多种采样模式。

    采样模式（互斥）：
    1. scenario_ids：指定要使用的 scenario ID 列表（精确控制）
    2. base_manifest：从已有 run 的 manifest 中重放场景（保持身份一致）
    3. distinct_domains：每个 domain 最多选一个 scenario（跨域覆盖）
    4. 默认：均匀随机采样

    Task Style 分配模式（互斥）：
    - all_task_styles：每个场景生成所有四种风格（展开为 N×4 个 case）
    - balanced_task_styles：四种风格均匀分配（count 必须是 4 的倍数）
    - forced_task_style：所有 case 使用同一风格
    - 默认：随机分配风格

    Args:
        taxonomy: 完整的 taxonomy 数据
        seed: 随机种子
        count: 采样数量
        base_manifest: 已有 run 的 manifest 路径（重放模式）
        forced_task_style: 强制使用的任务风格
        scenario_ids: 指定的 scenario ID 列表
        distinct_domains: 是否每个 domain 只选一个 scenario
        balanced_task_styles: 是否均匀分配任务风格
        all_task_styles: 是否为每个场景生成所有风格

    Returns:
        (case_id, scene) 列表，每个 scene 包含完整的场景信息和冻结的 task_style

    Raises:
        ValueError: 参数冲突、scenario 不存在、count 不合法等
    """
    # 收集 taxonomy 中所有 (domain_id, scenario_id) 对
    pairs = [
        (domain["domain_id"], scenario["scenario_id"])
        for domain in taxonomy.get("domains", [])
        for scenario in domain.get("scenarios", [])
    ]
    if count > len(pairs):
        raise ValueError(f"taxonomy contains only {len(pairs)} scenarios")
    # 参数互斥校验
    if all_task_styles and (forced_task_style is not None or balanced_task_styles):
        raise ValueError("all_task_styles cannot be combined with task_style or balanced_task_styles")
    if scenario_ids and base_manifest is not None:
        raise ValueError("scenario_ids cannot be combined with base_manifest")
    rng = random.Random(seed)
    scenes: list[tuple[str, dict]] = []
    used_pairs: set[tuple[str, str]] = set()

    # ── 模式 1：指定 scenario_ids ─────────────────────────────────────
    if scenario_ids:
        pair_by_id = {
            scenario["scenario_id"]: (domain["domain_id"], scenario["scenario_id"])
            for domain in taxonomy.get("domains", [])
            for scenario in domain.get("scenarios", [])
        }
        missing = [item for item in scenario_ids if item not in pair_by_id]
        if missing:
            raise ValueError(f"unknown scenario IDs: {', '.join(missing)}")
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("scenario_ids must be unique")
        if count != len(scenario_ids):
            raise ValueError("count must equal the number of scenario_ids")
        selected = [pair_by_id[item] for item in scenario_ids]
        for index, (domain_id, scenario_id) in enumerate(selected, 1):
            scene_seed = rng.randrange(1, 2**31 - 1)
            scene = sample_scene(taxonomy, scene_seed, domain_id, scenario_id, "random_voice_sample")
            scene["sampling_reason"] = "random_voice_sample"
            scene["case_id"] = f"R{index:02d}"
            scenes.append((f"R{index:02d}", scene))
        new_count = 0

    # ── 模式 2：从 base manifest 重放 ─────────────────────────────────
    elif base_manifest is not None:
        manifest = json.loads(base_manifest.read_text(encoding="utf-8"))
        base_cases = manifest.get("cases")
        if not isinstance(base_cases, list) or not base_cases:
            raise ValueError(f"base manifest has no cases: {base_manifest}")
        # count 截取前 N 个冻结 case，支持对已有批次做低成本多样性探测
        base_cases = base_cases[:count]
        for item in base_cases:
            if not isinstance(item, dict):
                raise ValueError("base manifest contains an invalid case")
            pair = (str(item.get("domain_id", "")), str(item.get("scenario_id", "")))
            if pair not in pairs:
                raise ValueError(f"base manifest scenario is not in taxonomy: {pair}")
            # 用当前 taxonomy 重新解析冻结的 pair，保留旧 case 身份/seed/style，
            # 但防止已移除或废弃的字段从旧 manifest 泄漏
            scene = sample_scene(
                taxonomy,
                int(item.get("rng_seed", 0)),
                pair[0],
                pair[1],
                "random_voice_sample_replayed",
            )
            scene["case_id"] = str(item.get("case_id", ""))
            if item.get("task_style") in SUPPORTED_TASK_STYLES:
                scene["task_style"] = item["task_style"]
            scene["sampling_mode"] = "random_voice_sample"
            scene["sampling_reason"] = "random_voice_sample_replayed"
            case_id = str(scene.get("case_id", ""))
            if not case_id:
                raise ValueError("base manifest case has no case_id")
            scenes.append((case_id, scene))
            used_pairs.add(pair)
        new_count = count - len(scenes)

    # ── 模式 3/4：随机采样 ────────────────────────────────────────────
    else:
        new_count = count

    # 从未使用的 pair 中选取新的场景
    candidates = [pair for pair in pairs if pair not in used_pairs]
    if distinct_domains:
        # 每个 domain 最多选一个 scenario
        if base_manifest is not None or scenario_ids:
            raise ValueError("distinct_domains cannot be combined with base_manifest or scenario_ids")
        domain_to_pairs: dict[str, list[tuple[str, str]]] = {}
        for domain_id, scenario_id in candidates:
            domain_to_pairs.setdefault(domain_id, []).append((domain_id, scenario_id))
        if new_count > len(domain_to_pairs):
            raise ValueError(
                f"taxonomy contains only {len(domain_to_pairs)} distinct domains; requested {new_count}"
            )
        selected_domains = rng.sample(list(domain_to_pairs), new_count)
        selected = [rng.choice(domain_to_pairs[domain_id]) for domain_id in selected_domains]
    else:
        selected = rng.sample(candidates, new_count)

    # 为新选中的场景生成 case_id 和采样信息
    start_index = len(scenes) + 1
    for offset, (domain_id, scenario_id) in enumerate(selected):
        index = start_index + offset
        scene_seed = rng.randrange(1, 2**31 - 1)
        scene = sample_scene(taxonomy, scene_seed, domain_id, scenario_id, "random_voice_sample")
        # sample_scene 会将显式指定的 ID 标记为 fixed；本采样器已随机选取，
        # 因此覆盖 provenance 标记为 random_voice_sample
        scene["sampling_reason"] = "random_voice_sample"
        scene["case_id"] = f"R{index:02d}"
        scenes.append((f"R{index:02d}", scene))
    # ── Task Style 分配 ───────────────────────────────────────────────
    # task_style 是一个受 seed 控制的采样变量。Stage-1 模型接收冻结值，
    # 必须生成匹配的 goal；示例仅作为该风格内的多样性软参考。
    if all_task_styles:
        # 为每个场景展开所有四种风格
        styled_scenes: list[tuple[str, dict]] = []
        for case_id, scene in scenes:
            for style_index, style in enumerate(BALANCED_TASK_STYLES, 1):
                styled_case_id = f"{case_id}-S{style_index:02d}"
                styled_scene = copy.deepcopy(scene)
                styled_scene["case_id"] = styled_case_id
                styled_scene["task_style"] = style
                styled_scenes.append((styled_case_id, styled_scene))
        scenes = styled_scenes
    elif balanced_task_styles:
        # 均匀分配四种风格（count 必须是 4 的倍数）
        if len(scenes) % len(BALANCED_TASK_STYLES) != 0:
            raise ValueError(
                f"balanced task styles require case count divisible by {len(BALANCED_TASK_STYLES)}"
            )
        style_values = list(BALANCED_TASK_STYLES) * (len(scenes) // len(BALANCED_TASK_STYLES))
        rng.shuffle(style_values)
        for (_, scene), style in zip(scenes, style_values):
            scene["task_style"] = style
    else:
        # 随机分配风格
        _fill_task_styles(scenes, rng)

    # 强制覆盖为指定风格
    if forced_task_style is not None:
        if forced_task_style not in SUPPORTED_TASK_STYLES:
            raise ValueError(f"unknown task style: {forced_task_style}")
        for _, scene in scenes:
            scene["task_style"] = forced_task_style
    # 冻结示例：只保留与 task_style 匹配的示例
    _freeze_style_examples(scenes)
    return scenes


def _stage_context(
    scene: dict,
    previous_task_goals: list[dict] | None = None,
) -> dict:
    """构建 Stage-1 的上下文信息（单候选模式）。

    包含场景信息、任务风格、历史 goal 列表和音频能力合约。

    Args:
        scene: 场景信息 dict
        previous_task_goals: 该 Scenario/Style 的已通过 goal 列表

    Returns:
        上下文 dict，传给 LLM 作为输入
    """
    return {
        "scene": model_scene(scene),
        "task_style": scene.get("task_style"),
        "previous_task_goals": copy.deepcopy(previous_task_goals or []),
        "batch_size": 1,
        **environment_contract(),
    }


def _batch_stage_context(scene: dict, previous: list[dict], batch_size: int) -> dict:
    """构建 Stage-1 的上下文信息（批量模式）。

    在 _stage_context 基础上覆盖 batch_size 为实际批量大小。

    Args:
        scene: 场景信息 dict
        previous: 已通过的 goal 列表
        batch_size: 批量大小

    Returns:
        上下文 dict
    """
    context = _stage_context(scene, previous)
    context["batch_size"] = batch_size
    return context


def _write_style_memory_checkpoint(
    run_dir: Path,
    scene: dict,
    history: list[dict],
    batch_number: int,
) -> None:
    """在每个批次完成后持久化已通过的 goal 历史，不混合不同 task_style。

    断点续传机制的核心：每完成一个批次就写入 checkpoint，
    中断后可从最后一个完成的批次继续。

    Args:
        run_dir: run 目录路径
        scene: 场景信息 dict（用于定位 checkpoint 文件）
        history: 当前已通过的 goal 列表
        batch_number: 当前批次号
    """
    path = _style_memory_checkpoint_path(run_dir, scene)
    scenario_id = str(scene.get("scenario_id", "unknown"))
    task_style = str(scene.get("task_style", "unknown"))
    checkpoint = {
        "scenario_id": scenario_id,
        "task_style": task_style,
        "batch_number": batch_number,
        "goals": copy.deepcopy(history),
    }
    _write_json_atomic(path, checkpoint)


def _style_memory_checkpoint_path(run_dir: Path, scene: dict) -> Path:
    """计算 style memory checkpoint 的文件路径。

    路径格式：run_dir/stage_cache/goal_memory/{scenario_id}-{task_style}.json

    Args:
        run_dir: run 目录路径
        scene: 场景信息 dict

    Returns:
        checkpoint 文件路径
    """
    scenario_id = str(scene.get("scenario_id", "unknown"))
    task_style = str(scene.get("task_style", "unknown"))
    return run_dir / "stage_cache" / "goal_memory" / f"{scenario_id}-{task_style}.json"


def _load_style_memory_checkpoint(run_dir: Path, scene: dict) -> dict | None:
    """加载 style memory checkpoint，用于断点续传。

    校验 checkpoint 的身份信息（scenario_id + task_style）必须与当前场景匹配，
    防止错误的 checkpoint 被加载。

    Args:
        run_dir: run 目录路径
        scene: 场景信息 dict

    Returns:
        checkpoint dict，如果不存在则返回 None

    Raises:
        ValueError: checkpoint 格式错误或身份不匹配
    """
    path = _style_memory_checkpoint_path(run_dir, scene)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid Stage-1 resume checkpoint: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid Stage-1 resume checkpoint object: {path}")
    # 身份校验：checkpoint 的 scenario_id 和 task_style 必须与当前场景一致
    if value.get("scenario_id") != scene.get("scenario_id") or value.get("task_style") != scene.get("task_style"):
        raise ValueError(f"Stage-1 resume checkpoint identity mismatch: {path}")
    batch_number = value.get("batch_number")
    goals = value.get("goals")
    if not isinstance(batch_number, int) or batch_number < 1 or not isinstance(goals, list):
        raise ValueError(f"invalid Stage-1 resume checkpoint fields: {path}")
    return value


def _load_completed_batch(
    run_dir: Path,
    base_case_id: str,
    scene: dict,
    batch_size: int,
    batch_number: int,
    variant_offset: int,
) -> tuple[list[dict], list[tuple[dict, str, str]]] | None:
    """加载已完成的批次数据，用于断点续传；如果批次不完整则返回 None。

    检查每个 variant 的四个关键文件（scene_task/user_voice/validation/metadata）
    是否存在且格式正确，且验证状态为全部通过。

    Args:
        run_dir: run 目录路径
        base_case_id: 基础 case ID（如 "R01"）
        scene: 场景信息 dict
        batch_size: 批次大小
        batch_number: 批次号
        variant_offset: variant 偏移量

    Returns:
        (results, accepted) 元组，如果批次不完整则返回 None
    """
    results: list[dict] = []
    accepted: list[tuple[dict, str, str]] = []
    for local_index in range(1, batch_size + 1):
        variant_index = variant_offset + local_index
        case_id = f"{base_case_id}-V{variant_index:02d}"
        case_dir = run_dir / case_id
        # 检查四个关键文件是否都存在
        scene_task_path = case_dir / "scene_task.json"
        user_voice_path = case_dir / "user_voice.json"
        validation_path = case_dir / "validation.json"
        metadata_path = case_dir / "metadata.json"
        if not all(path.exists() for path in (scene_task_path, user_voice_path, validation_path, metadata_path)):
            return None
        try:
            scene_task = json.loads(scene_task_path.read_text(encoding="utf-8"))
            user_voice = json.loads(user_voice_path.read_text(encoding="utf-8"))
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(scene_task, dict) or not isinstance(user_voice, dict) or not isinstance(validation, dict) or not isinstance(metadata, dict):
            return None
        # 校验场景身份一致性
        stored_scene = scene_task.get("scene")
        if (
            scene_task.get("task_style") != scene.get("task_style")
            or not isinstance(stored_scene, dict)
            or stored_scene.get("scenario_id") != scene.get("scenario_id")
        ):
            return None
        # 必须全部通过（结构 + 语义）
        if validation.get("status") != "passed" or validation.get("structural_status") != "passed" or validation.get("semantic_status") != "passed":
            return None
        goal = scene_task.get("task_instance", {}).get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return None
        # 构建续传结果
        result = {
            "case_id": case_id,
            "scene": scene,
            "validation": validation,
            "model": metadata.get("model_identity", {}).get("model"),
            "provider": metadata.get("model_identity", {}).get("provider"),
            "judge_model": metadata.get("judge_model_identity", {}).get("model"),
            "accepted_goal": goal,
            "previous_task_goal_count": 0,
            "resumed": True,
        }
        results.append(result)
        accepted.append((scene, goal, case_id))
    return results, accepted


def _decode_compact_batch(raw: str) -> tuple[object, list[str]]:
    """解码 LLM 返回的批量 JSON 响应，支持多种格式容错。

    LLM 可能返回以下格式：
    1. 标准 JSON 数组：[{...}, {...}]
    2. 单个 JSON 对象（忽略数组指令）：{goal, user_seed, voice_profile}
    3. 拼接的 JSON 对象（无分隔符）：{...}{...}
    4. 带 items 包装的对象：{"items": [...]}
    5. 带 markdown 围栏的 JSON

    解码策略：
    1. 先尝试标准 json.loads
    2. 失败则尝试逐个解析拼接对象
    3. 再尝试 parse_json_text（处理围栏/前缀）
    4. 最后兜底：逐个 raw_decode

    Args:
        raw: LLM 的原始响应文本

    Returns:
        (解析结果, 错误列表)。解析结果通常是 list[dict]，
        错误列表非空表示解码失败
    """
    text = raw.strip()
    # 第一次尝试：标准 JSON 解析
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is None:
        # 尝试逐个解析拼接的 JSON 对象
        decoder = json.JSONDecoder()
        values: list[object] = []
        cursor = 0
        while cursor < len(text):
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor >= len(text):
                break
            try:
                value, end = decoder.raw_decode(text, cursor)
            except json.JSONDecodeError:
                values = []
                break
            values.append(value)
            cursor = end
        if len(values) > 1:
            return values, []
        # 尝试 parse_json_text 处理围栏/前缀格式
        try:
            parsed = parse_json_text(text)
        except Exception:
            parsed = None
    # 标准数组格式
    if isinstance(parsed, list):
        return parsed, []
    # {"items": [...]} 包装格式
    if isinstance(parsed, dict) and set(parsed) == {"items"} and isinstance(parsed.get("items"), list):
        return parsed["items"], []
    # 单对象格式：某些 provider 在 JSON-object 模式下可能返回单个候选
    if isinstance(parsed, dict) and {"goal", "user_seed", "voice_profile"}.issubset(parsed):
        return [parsed], []

    # 兜底：逐个 raw_decode 解析
    values: list[object] = []
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError as exc:
            return [], [f"batch generation JSON parse failed: {exc}"]
        values.append(value)
        cursor = end
    if values and all(isinstance(value, dict) for value in values):
        return values, []
    return [], ["batch generation output must be a JSON array"]


def _parse_batch_items(raw: str, context: dict, scene: dict, expected_count: int) -> tuple[list[dict], list[str]]:
    """解析批量生成的候选项，并进行结构校验。

    流程：
    1. 调用 _decode_compact_batch 解码 LLM 原始响应
    2. 校验每个候选项必须包含 goal/user_seed/voice_profile 三个字段
    3. 将候选项组装为 scene_task 格式
    4. 通过 _prepare_stage_value 和 validate_stage 进行合约校验

    Args:
        raw: LLM 的原始响应文本
        context: Stage-1 上下文
        scene: 场景信息 dict
        expected_count: 期望的候选数量

    Returns:
        (通过校验的候选列表, 错误列表)
    """
    parsed, decode_errors = _decode_compact_batch(raw)
    if decode_errors:
        return [], decode_errors
    items = parsed
    if not isinstance(items, list):
        return [], ["batch generation output must be a JSON array"]
    errors: list[str] = []
    if len(items) != expected_count:
        if len(items) > expected_count:
            # 保留请求数量的候选，完整原始响应已存入 batch cache 供审计；
            # 超长响应是可恢复的，不视为致命错误
            items = items[:expected_count]
        else:
            errors.append(f"batch generation expected {expected_count} items, got {len(items)}")
    prepared: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"batch item {index} must be an object")
            continue
        # 每个候选必须恰好包含 goal、user_seed、voice_profile
        if set(item) != {"goal", "user_seed", "voice_profile"}:
            errors.append(f"batch item {index} must contain exactly goal, user_seed, voice_profile")
            continue
        # 组装为 scene_task 格式
        candidate = {
            "scene": copy.deepcopy(context.get("scene")),
            "task_style": context.get("task_style"),
            "task_instance": {"goal": item.get("goal")},
            "user_seed": copy.deepcopy(item.get("user_seed")),
            "voice_profile": item.get("voice_profile"),
        }
        # 通过 _prepare_stage_value 注入系统字段，再通过 validate_stage 校验合约
        candidate = _prepare_stage_value("scene_task", candidate, context)
        item_errors = validate_stage("scene_task", candidate, {"scene": scene})
        if item_errors:
            errors.extend([f"batch item {index}: {error}" for error in item_errors])
        else:
            prepared.append(candidate)
    return prepared, errors


def _batch_judge_input(context: dict, items: list[dict]) -> list[dict]:
    """构建批量 Judge 调用的输入列表。

    每个候选项展开为包含场景、风格、goal、user_seed、voice_profile
    和能力合约的完整输入。Judge 和 Repair 共享此输入格式。

    Args:
        context: Stage-1 上下文
        items: 通过结构校验的候选列表

    Returns:
        Judge 输入列表，每项包含 item_index 和完整候选信息
    """
    return [
        {
            "item_index": index,
            "scene": context.get("scene"),
            "task_style": context.get("task_style"),
            "goal": copy.deepcopy(item.get("task_instance", {}).get("goal")),
            "user_seed": copy.deepcopy(item.get("user_seed")),
            "voice_profile": item.get("voice_profile"),
            "capability_contract": context.get("capability_contract"),
        }
        for index, item in enumerate(items)
    ]


def _batch_repair_input(context: dict, items: list[dict], findings: list[dict]) -> list[dict]:
    """构建批量 Repair 调用的输入列表。

    在 Judge 输入基础上附加完整的 Judge findings，让 Repair 模型
    看到所有问题并生成对应的修补操作。

    Args:
        context: Stage-1 上下文
        items: 候选列表
        findings: Judge 的 findings 列表

    Returns:
        Repair 输入列表
    """
    return [
        {
            **entry,
            "judge_findings": copy.deepcopy(findings),
        }
        for entry in _batch_judge_input(context, items)
    ]


def _apply_batch_patches(items: list[dict], patches: list[dict], scene: dict, context: dict) -> tuple[list[dict], list[str]]:
    """将 Repair 生成的 JSON Patch 应用到候选列表。

    只允许两种修补路径：
    - /task_instance/goal：修改 task goal
    - /voice_profile：修改声音画像

    应用修补后重新进行合约校验，校验失败则回退到原始候选。

    Args:
        items: 原始候选列表
        patches: Repair 生成的修补操作列表
        scene: 场景信息 dict
        context: Stage-1 上下文

    Returns:
        (修补后的候选列表或原始列表, 错误列表)
    """
    candidate_items = copy.deepcopy(items)
    errors: list[str] = []
    for index, patch in enumerate(patches):
        item_index = patch.get("item_index")
        path = patch.get("path")
        if not isinstance(item_index, int) or not 0 <= item_index < len(candidate_items):
            errors.append(f"patch {index}: item_index is invalid")
            continue
        # 应用修补：根据 path 修改对应字段
        if path == "/task_instance/goal":
            candidate_items[item_index].setdefault("task_instance", {})["goal"] = patch.get("value")
        elif path == "/voice_profile":
            candidate_items[item_index]["voice_profile"] = patch.get("value")
        else:
            errors.append(f"patch {index}: path is not mutable")
    if errors:
        return items, errors
    # 修补后重新校验合约
    for index, item in enumerate(candidate_items):
        prepared = _prepare_stage_value("scene_task", item, context)
        item_errors = validate_stage("scene_task", prepared, {"scene": scene})
        if item_errors:
            errors.extend([f"patched item {index}: {error}" for error in item_errors])
    # 校验失败则回退到原始候选
    return candidate_items if not errors else items, errors


def _write_batch_item(
    case_dir: Path,
    item: dict,
    scene: dict,
    context: dict,
    prompt: str,
    batch_raw: str,
    client: MiniMaxClient,
    judge_client: MiniMaxClient,
    semantic_attempts: list[dict],
    batch_index: int,
    batch_size: int,
    semantic_status: str,
    semantic_findings: list[dict],
    batch_number: int = 1,
    variant_index: int | None = None,
) -> dict:
    """将一个通过校验的批量候选项写入磁盘。

    写入的文件：
    - scene_task.json：场景 + 任务信息（从 item 中分离 user_seed/voice_profile 后）
    - user_voice.json：用户种子 + 声音画像
    - prompt.md：使用的 prompt
    - raw_response.txt：LLM 原始响应（脱敏后）
    - validation.json：验证状态（结构 + 语义）
    - metadata.json：模型身份、SHA-256 指纹、批量信息

    Args:
        case_dir: case 输出目录
        item: 通过校验的候选数据
        scene: 场景信息 dict
        context: Stage-1 上下文
        prompt: 使用的 prompt 文本
        batch_raw: 批量生成的原始响应
        client: 生成用 LLM 客户端
        judge_client: Judge 用 LLM 客户端
        semantic_attempts: 语义 Judge 尝试记录
        batch_index: 候选项在批次中的索引
        batch_size: 批次大小
        semantic_status: 语义校验状态（"passed" 或 "failed"）
        semantic_findings: 语义 Judge 的 findings
        batch_number: 批次号
        variant_index: variant 索引

    Returns:
        结果摘要 dict，包含 case_id、scene、validation、模型信息等
    """
    case_dir.mkdir(parents=True, exist_ok=True)
    published = copy.deepcopy(item)
    # 从 scene_task 中分离 user_seed 和 voice_profile 到独立文件
    user_voice = {
        "user_seed": published.pop("user_seed"),
        "voice_profile": published.pop("voice_profile"),
    }
    _write_json_atomic(case_dir / "scene_task.json", published)
    _write_json_atomic(case_dir / "user_voice.json", user_voice)
    _write_text_atomic(case_dir / "prompt.md", prompt)
    _write_text_atomic(case_dir / "raw_response.txt", _redact_secret(batch_raw, client))
    # 构建验证状态
    validation = {
        "status": "passed" if semantic_status == "passed" else "failed",
        "structural_status": "passed",
        "semantic_status": semantic_status,
        "errors": [] if semantic_status == "passed" else ["batch semantic judge did not pass"],
        "semantic_findings": semantic_findings,
        "repair_attempts": [],
        "semantic_attempts": semantic_attempts,
    }
    _write_json_atomic(case_dir / "validation.json", validation)
    # 写入元数据：模型身份、SHA-256 指纹、批量信息
    _write_json_atomic(case_dir / "metadata.json", {
        "model_identity": {"provider": client.provider, "model": client.model},
        "judge_model_identity": {"provider": judge_client.provider, "model": judge_client.model},
        "prompt_sha256": digest(prompt),
        "input_sha256": digest(context),
        "response_sha256": digest(_redact_secret(batch_raw, client)),
        "batch_mode": True,
        "batch_index": batch_index,
        "batch_size": batch_size,
        "batch_number": batch_number,
        "variant_index": variant_index if variant_index is not None else batch_index,
        "semantic_attempts": semantic_attempts,
        "api_key_serialized": False,
    })
    return {
        "case_id": case_dir.name,
        "scene": scene,
        "validation": validation,
        "model": client.model,
        "provider": client.provider,
        "judge_model": judge_client.model,
        "accepted_goal": str(published.get("task_instance", {}).get("goal", "")) if semantic_status == "passed" else None,
        "previous_task_goal_count": len(context.get("previous_task_goals", [])),
    }


def _generate_batch_style(
    base_case_id: str,
    scene: dict,
    run_dir: Path,
    client: MiniMaxClient,
    previous: list[dict],
    batch_size: int,
    batch_number: int = 1,
    variant_offset: int = 0,
) -> tuple[list[dict], list[tuple[dict, str, str]]]:
    """为单个场景的特定 task style 生成一批 Stage-1 候选。

    完整流程：
    1. 批量生成：调用 LLM 一次生成 batch_size 个候选（goal + user_seed + voice_profile）
    2. 结构校验：解析响应并验证每个候选的字段完整性
    3. 语义 Judge：调用 Judge 模型审查候选质量
    4. 定向 Repair：如果 Judge 发现问题，调用 Repair 模型生成修补
    5. 循环：Judge → Repair 最多 MAX_STAGE1_JUDGE_ATTEMPTS 轮
    6. 写入：将通过的候选写入磁盘

    Args:
        base_case_id: 基础 case ID（如 "R01"）
        scene: 场景信息 dict
        run_dir: run 目录路径
        client: 生成用 LLM 客户端
        previous: 已通过的 goal 列表（作为去重上下文）
        batch_size: 批次大小（1~10）
        batch_number: 批次号（用于缓存路径和断点续传）
        variant_offset: variant 偏移量（用于 case_id 编号）

    Returns:
        (results, accepted) 元组：
        - results: 每个候选的结果摘要
        - accepted: (scene, goal, case_id) 三元组列表

    Raises:
        ValueError: 生成失败、Judge 无效、Repair 失败、或达到修复上限
    """
    if not 1 <= batch_size <= BATCH_MAX_ITEMS:
        raise ValueError(f"batch size must be between 1 and {BATCH_MAX_ITEMS}")
    prompt = stage_prompt("scene_task", scene)
    context = _batch_stage_context(scene, previous, batch_size)
    # 缓存目录：按 base_case_id 和 batch_number 组织
    cache_dir = run_dir / "stage_cache" / "scene_task_batch" / base_case_id / f"B{batch_number:02d}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = ""
    meta: dict = {}
    items: list[dict] = []
    generation_errors: list[str] = []

    # ── 阶段 1：批量生成 ──────────────────────────────────────────────
    for attempt in range(1, BATCH_MAX_ATTEMPTS + 1):
        raw, meta = client.complete(
            prompt,
            context,
            max_tokens=max(6000, 1400 * batch_size),
            # 不使用 JSON object mode，因为需要返回根级数组
            response_format=None,
        )
        items, generation_errors = _parse_batch_items(raw, context, scene, batch_size)
        # 缓存每次尝试的原始响应和校验结果
        _write_text_atomic(cache_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(raw, client))
        _write_json_atomic(cache_dir / f"attempt-{attempt:02d}-input.json", context)
        _write_json_atomic(cache_dir / f"attempt-{attempt:02d}-validation.json", {"errors": generation_errors})
        if not generation_errors:
            break
        # 将校验错误反馈给下一轮生成
        context["batch_validation_errors"] = generation_errors
    if generation_errors:
        raise ValueError("batch generation failed: " + "; ".join(generation_errors))

    # ── 阶段 2：语义 Judge + Repair 循环 ──────────────────────────────
    judge_client = _judge_client(client)
    judge_prompt = (ROOT / "prompts/01_scene_task_batch_judge.md").read_text(encoding="utf-8")
    repair_prompt = (ROOT / "prompts/01_scene_task_batch_repair.md").read_text(encoding="utf-8")
    semantic_attempts: list[dict] = []
    semantic_findings: list[dict] = []
    semantic_status = "failed"
    for attempt in range(1, MAX_STAGE1_JUDGE_ATTEMPTS + 1):
        # 调用 Judge 审查候选质量
        judge_input = _batch_judge_input(context, items)
        judge_raw, judge_meta = complete_checked_judge(judge_client, judge_prompt, judge_input, keys=STAGE1_BATCH_JUDGE_KEYS, validator=lambda value: _validate_batch_judge_output(value, len(items)), directory=cache_dir / f"judge-format-{attempt:02d}", max_tokens=STAGE1_JUDGE_MAX_TOKENS)
        try:
            judge_value = parse_json_text(judge_raw, STAGE1_BATCH_JUDGE_KEYS)
        except Exception as exc:
            judge_value = {"_parse_error": str(exc)}
        judge_errors = _validate_batch_judge_output(judge_value, len(items))
        # 缓存 Judge 的输入、prompt、响应和校验结果
        _write_json_atomic(cache_dir / f"judge-{attempt:02d}-input.json", judge_input)
        _write_text_atomic(cache_dir / f"judge-{attempt:02d}-prompt.md", judge_prompt)
        _write_text_atomic(cache_dir / f"judge-{attempt:02d}-raw_response.txt", _redact_secret(judge_raw, judge_client))
        _write_json_atomic(cache_dir / f"judge-{attempt:02d}-review.json", judge_value)
        _write_json_atomic(cache_dir / f"judge-{attempt:02d}-validation.json", {"errors": judge_errors})
        if judge_errors:
            semantic_attempts.append({"attempt": attempt, "decision": "invalid", "errors": judge_errors})
            if attempt == MAX_STAGE1_JUDGE_ATTEMPTS:
                raise ValueError("batch judge response invalid: " + "; ".join(judge_errors))
            continue
        findings = list(judge_value.get("findings", []))
        semantic_findings = findings
        semantic_attempts.append({
            "attempt": attempt,
            "decision": judge_value.get("decision"),
            "finding_count": len(findings),
            "judge_model": judge_meta.get("model", judge_client.model),
            "response_sha256": digest(judge_raw),
        })
        # Judge 通过则结束循环
        if judge_value.get("decision") == "pass":
            semantic_status = "passed"
            semantic_findings = []
            break
        # 达到最大尝试次数则不再 Repair
        if attempt == MAX_STAGE1_JUDGE_ATTEMPTS:
            break
        # 调用 Repair 生成修补操作
        repair_input = _batch_repair_input(context, items, findings)
        repair_raw, repair_meta = client.complete(
            repair_prompt,
            repair_input,
            max_tokens=STAGE1_JUDGE_MAX_TOKENS,
            response_format=JSON_RESPONSE_FORMAT,
        )
        try:
            repair_value = parse_json_text(repair_raw, STAGE1_PATCH_KEYS)
        except Exception as exc:
            repair_value = {"_parse_error": str(exc)}
        repair_errors = _validate_batch_patch_output(repair_value, len(items))
        if not repair_errors:
            items, patch_errors = _apply_batch_patches(items, repair_value["patches"], scene, context)
            repair_errors.extend(patch_errors)
        # 缓存 Repair 的输入、prompt、响应和校验结果
        _write_json_atomic(cache_dir / f"repair-{attempt:02d}-input.json", repair_input)
        _write_text_atomic(cache_dir / f"repair-{attempt:02d}-prompt.md", repair_prompt)
        _write_text_atomic(cache_dir / f"repair-{attempt:02d}-raw_response.txt", _redact_secret(repair_raw, client))
        _write_json_atomic(cache_dir / f"repair-{attempt:02d}-parsed_output.json", repair_value)
        _write_json_atomic(cache_dir / f"repair-{attempt:02d}-validation.json", {"errors": repair_errors})
        if repair_errors:
            raise ValueError("batch repair failed: " + "; ".join(repair_errors))

    # Judge 未通过且达到修复上限
    if semantic_status != "passed":
        raise ValueError("batch semantic judge repair limit reached")

    # ── 阶段 3：写入通过的候选 ────────────────────────────────────────
    results: list[dict] = []
    accepted: list[tuple[dict, str, str]] = []
    for index, item in enumerate(items, 1):
        variant_index = variant_offset + index
        case_id = f"{base_case_id}-V{variant_index:02d}"
        result = _write_batch_item(
            run_dir / case_id,
            item,
            scene,
            context,
            prompt,
            raw,
            client,
            judge_client,
            semantic_attempts,
            index,
            batch_size,
            semantic_status,
            semantic_findings,
            batch_number,
            variant_index,
        )
        results.append(result)
        accepted.append((scene, result["accepted_goal"], case_id))
    return results, accepted


def _generate_scenario_batch_group(
    grouped_scenes: list[tuple[str, dict]],
    run_dir: Path,
    client: MiniMaxClient,
    memory: dict,
    batch_size: int,
    resume: bool = False,
) -> tuple[list[dict], list[tuple[dict, str, str]]]:
    """为同一 Scenario 的多个 task style 串行生成候选。

    按 task_style 排序后逐个处理，每个 style 内按 BATCH_MAX_ITEMS 分批。
    同一 Scenario 内的 goal 历史按序累积，确保去重有效。

    断点续传机制：
    - 如果 resume=True，加载 style memory checkpoint
    - 已完成的批次直接从磁盘加载，不重新调用 LLM
    - checkpoint 损坏时回退到基线 goal 历史，从断点批次重新生成

    Args:
        grouped_scenes: 同一 Scenario 的 (case_id, scene) 列表
        run_dir: run 目录路径
        client: 生成用 LLM 客户端
        memory: Goal Memory 数据（会被原地修改）
        batch_size: 每个 style 需要生成的候选总数
        resume: 是否启用断点续传

    Returns:
        (results, accepted) 元组
    """
    if not grouped_scenes:
        return [], []
    # 按 task_style 排序，确保确定性顺序
    style_order = {style: index for index, style in enumerate(SUPPORTED_TASK_STYLES)}
    ordered = sorted(grouped_scenes, key=lambda pair: style_order.get(pair[1].get("task_style"), 99))
    results: list[dict] = []
    accepted: list[tuple[dict, str, str]] = []
    for base_case_id, scene in ordered:
        # 从 Goal Memory 加载该 Scenario/Style 的历史 goal
        baseline_previous = _memory_goals(memory, scene)
        previous = baseline_previous
        # 断点续传：加载已完成的批次号
        checkpoint = _load_style_memory_checkpoint(run_dir, scene) if resume else None
        completed_batch = int(checkpoint["batch_number"]) if checkpoint else 0
        # 按 BATCH_MAX_ITEMS 分批生成
        for batch_start in range(0, batch_size, BATCH_MAX_ITEMS):
            current_size = min(BATCH_MAX_ITEMS, batch_size - batch_start)
            batch_number = batch_start // BATCH_MAX_ITEMS + 1
            # 如果该批次已完成，尝试从磁盘加载
            if batch_number <= completed_batch:
                resumed = _load_completed_batch(
                    run_dir,
                    base_case_id,
                    scene,
                    current_size,
                    batch_number,
                    batch_start,
                )
                if resumed is not None:
                    resumed_results, resumed_accepted = resumed
                    results.extend(resumed_results)
                    accepted.extend(resumed_accepted)
                    # 将续传的 goal 加入历史
                    for accepted_scene, goal, case_id in resumed_accepted:
                        if not any(item.get("goal") == goal for item in previous if isinstance(item, dict)):
                            previous.append({"task_style": accepted_scene.get("task_style"), "goal": goal})
                    continue
                # checkpoint 存在但批次文件不完整：回退到基线，从当前批次重新生成
                completed_batch = batch_number - 1
                previous = copy.deepcopy(baseline_previous)
            # 调用 LLM 生成新批次
            style_results, style_accepted = _generate_batch_style(
                base_case_id,
                scene,
                run_dir,
                client,
                previous,
                current_size,
                batch_number=batch_number,
                variant_offset=batch_start,
            )
            results.extend(style_results)
            accepted.extend(style_accepted)
            # 将新通过的 goal 加入历史和 Goal Memory
            for accepted_scene, goal, case_id in style_accepted:
                if not goal:
                    continue
                if not any(item.get("goal") == goal for item in previous if isinstance(item, dict)):
                    previous.append({"task_style": accepted_scene.get("task_style"), "goal": goal})
                _remember_goal(memory, accepted_scene, goal, run_dir.name, case_id)
            # 写入 checkpoint 以支持断点续传
            _write_style_memory_checkpoint(run_dir, scene, previous, batch_number)
    return results, accepted


def _render_html(run_dir: Path, run_id: str, results: list[dict]) -> Path:
    """Render a grouped, Stage-1-only review page.

    The general episode bundle builder intentionally ignores directories that
    do not contain an episode script. Stage 1 therefore owns its own report:
    scenarios are grouped like the Stage-00 diversity page, while each goal
    keeps its UserSeed, VoiceProfile, validation state, and raw JSON available.
    """
    grouped: dict[str, dict] = {}
    for result in results:
        case_id = str(result.get("case_id", ""))
        scene = result.get("scene") if isinstance(result.get("scene"), dict) else {}
        scenario_id = str(scene.get("scenario_id", ""))
        group = grouped.setdefault(scenario_id, {"scene": scene, "styles": {}})
        style = str(scene.get("task_style", ""))
        group["styles"].setdefault(style, []).append(result)

    def candidate_html(result: dict) -> str:
        case_id = str(result.get("case_id", ""))
        scene = result.get("scene") if isinstance(result.get("scene"), dict) else {}
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        case_dir = run_dir / case_id
        scene_task = json.loads((case_dir / "scene_task.json").read_text(encoding="utf-8")) if (case_dir / "scene_task.json").exists() else {}
        user_voice = json.loads((case_dir / "user_voice.json").read_text(encoding="utf-8")) if (case_dir / "user_voice.json").exists() else {}
        user_seed = user_voice.get("user_seed", {}) if isinstance(user_voice, dict) else {}
        goal = scene_task.get("task_instance", {}).get("goal", "") if isinstance(scene_task, dict) else ""
        passed = validation.get("status") == "passed" and validation.get("semantic_status") == "passed"
        status = "通过" if passed else "未通过"
        findings = validation.get("semantic_findings", [])
        errors = validation.get("errors", [])
        diagnostic = findings or errors
        diagnostic_html = "" if not diagnostic else f"<details class='diagnostic'><summary>Judge / validation</summary><pre>{html.escape(json.dumps(diagnostic, ensure_ascii=False, indent=2))}</pre></details>"
        full_json = {"scene": scene, "scene_task": scene_task, "user_voice": user_voice, "validation": validation}
        return f"""<li class='sample'>
          <div class='sample-head'><span class='sample-id'>{html.escape(case_id)}</span><span class='status {'ok' if passed else 'bad'}'>{status}</span></div>
          <p class='goal'>{html.escape(str(goal))}</p>
          <div class='sample-meta'><span><b>UserSeed</b> {html.escape(str(user_seed.get('role_relation', '')))} · {html.escape(str(user_seed.get('task_familiarity', '')))}</span><span><b>VoiceProfile</b> {html.escape(str(user_voice.get('voice_profile', '')))}</span></div>
          {diagnostic_html}
          <details class='raw'><summary>完整输入与产物 JSON</summary><pre>{html.escape(json.dumps(full_json, ensure_ascii=False, indent=2))}</pre></details>
        </li>"""

    scenario_cards: list[str] = []
    style_order = {style: index for index, style in enumerate(SUPPORTED_TASK_STYLES)}
    for scenario_id, group in grouped.items():
        scene = group["scene"]
        style_sections = []
        for style, style_results in sorted(group["styles"].items(), key=lambda item: style_order.get(item[0], 99)):
            style_results.sort(key=lambda item: str(item.get("case_id", "")))
            style_sections.append(
                f"<section class='style'><h3>{html.escape(style)} <span>{len(style_results)} 条</span></h3><ol>{''.join(candidate_html(item) for item in style_results)}</ol></section>"
            )
        scenario_cards.append(f"""<article class='scenario'>
          <header><p>{html.escape(str(scene.get('domain_id', '')))} · {html.escape(scenario_id)}</p><h2>{html.escape(str(scene.get('domain', '')))} / {html.escape(str(scene.get('scenario', '')))}</h2></header>
          <div class='styles'>{''.join(style_sections)}</div>
        </article>""")

    models = {f"{result.get('provider', '')} / {result.get('model', '')}" for result in results}
    judges = {str(result.get('judge_model', '')) for result in results}
    passed_count = sum(1 for result in results if result.get("validation", {}).get("status") == "passed")
    output = f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Stage-1 Goal Diversity Review</title><style>
:root{{--ink:#18232b;--muted:#5a6a76;--line:#d0dbe0;--paper:#f4f7f8;--blue:#176787;--green:#17633d;--red:#8a2b2b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1760px;margin:0 auto;padding:32px 22px 68px}}h1{{margin:0 0 8px;font-size:30px;letter-spacing:0}}.lead{{margin:0 0 28px;color:var(--muted)}}
.scenario{{background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:20px}}.scenario>header{{padding:16px 18px;background:#ecf3f5;border-bottom:1px solid var(--line)}}.scenario header p{{margin:0 0 3px;color:var(--blue);font-size:12px;font-weight:700;letter-spacing:.04em}}h2{{margin:0;font-size:20px;line-height:1.35}}
.styles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));overflow-x:auto}}.style{{padding:16px;border-right:1px solid var(--line);min-width:520px}}.style:last-child{{border-right:0}}h3{{margin:0 0 10px;color:var(--blue);font-size:15px}}h3 span{{color:var(--muted);font-size:12px;font-weight:400}}ol{{padding-left:0;margin:0;list-style:none}}.sample{{padding:10px 0;border-top:1px solid #e8edef}}.sample:first-child{{border-top:0}}.sample-head{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.sample-id{{color:var(--blue);font:700 12px ui-monospace,SFMono-Regular,Menlo,monospace}}.status{{border-radius:999px;padding:1px 8px;font-size:11px}}.status.ok{{color:var(--green);background:#e6f5ec}}.status.bad{{color:var(--red);background:#fde9e9}}.goal{{margin:5px 0 7px;font-size:14px;line-height:1.6}}.sample-meta{{display:grid;gap:3px;color:var(--muted);font-size:12px}}.sample-meta b{{color:#34515e}}details{{margin-top:7px}}details summary{{cursor:pointer;color:var(--muted);font-size:12px}}pre{{white-space:pre-wrap;overflow:auto;background:#f6f8fa;border:1px solid var(--line);padding:9px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}.diagnostic pre{{color:var(--red)}}
@media(max-width:1120px){{.styles{{grid-template-columns:1fr}}.style{{border-right:0;border-bottom:1px solid var(--line);min-width:0}}.style:last-child{{border-bottom:0}}}}@media(max-width:640px){{main{{padding:24px 12px 48px}}h1{{font-size:25px}}}}
</style></head><body><main>
<h1>Stage-1 Goal Diversity Review</h1>
<p class='lead'>Run {html.escape(run_id)} · {len(grouped)} 个 Scenario · {len(results)} 条候选 goal · 已通过 {passed_count} 条。生成模型：{html.escape(', '.join(sorted(models)))}；Judge：{html.escape(', '.join(sorted(judges)))}。每条保留 UserSeed、VoiceProfile、校验信息及完整 JSON。</p>
{''.join(scenario_cards)}
</main></body></html>"""
    path = run_dir / "offline-episode-script-bundle.html"
    _write_text_atomic(path, output)
    return path


def main() -> None:
    """主入口：执行 Stage-1 随机样本生成。

    完整流程：
    1. 解析命令行参数
    2. 初始化 LLM 客户端
    3. 加载 taxonomy 并采样场景
    4. 加载/预热 Goal Memory
    5. 按 Scenario 分组，并发执行批量生成
    6. 汇总结果，写入 manifest
    7. 生成 HTML 可视化报告

    命令行参数说明：
    --run-id: run 标识符，默认为时间戳
    --seed: 随机种子，确保可复现
    --count: 采样场景数量
    --workers: 并发线程数
    --base-run: 从已有 run 重放场景
    --task-style: 强制使用指定任务风格
    --no-html: 跳过 HTML 生成
    --resume: 启用断点续传
    --samples-per-case: 每个场景生成的候选数
    --distinct-domains: 每个 domain 最多选一个 scenario
    --balanced-task-styles: 均匀分配任务风格
    --all-task-styles: 为每个场景生成所有风格
    --goal-memory: Goal Memory 文件路径
    --bootstrap-run: 从历史 run 导入 goal
    --taxonomy: taxonomy JSON 路径
    --scenario-ids: 指定 scenario ID 列表
    --model: 生成模型
    --judge-model: Judge 模型
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S") + "-random-user-voice")
    parser.add_argument("--seed", type=int, default=20260723)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--base-run", type=Path, help="reuse cases from a prior sample manifest, then append new random cases")
    parser.add_argument("--task-style", choices=SUPPORTED_TASK_STYLES, help="freeze one task style for every case in this run")
    parser.add_argument("--no-html", action="store_true", help="skip the random-user-voice HTML artifact")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume Stage-1 batches from the existing run's style checkpoints",
    )
    parser.add_argument(
        "--samples-per-case",
        type=int,
        default=1,
        help="total Stage-1 candidates per frozen Scenario/style; generated in batches of at most 10",
    )
    parser.add_argument(
        "--distinct-domains",
        action="store_true",
        help="sample at most one scenario from each domain; useful for cross-domain batches",
    )
    parser.add_argument(
        "--balanced-task-styles",
        action="store_true",
        help="assign the four supported task styles equally; case count must be divisible by four",
    )
    parser.add_argument(
        "--all-task-styles",
        action="store_true",
        help="for every sampled scene, generate all four task styles; combine with --samples-per-case",
    )
    parser.add_argument(
        "--goal-memory",
        type=Path,
        default=DEFAULT_GOAL_MEMORY_PATH,
        help="persistent JSON memory of passed goals, stored and exposed by scenario/task style",
    )
    parser.add_argument(
        "--bootstrap-run",
        action="append",
        type=Path,
        default=[],
        help="explicit prior Stage-1 run to import; may be repeated",
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=ROOT / "taxonomy/domain-scenario-with-task-examples-gpt56-sol-v5.json",
        help="taxonomy JSON used to sample scenes",
    )
    parser.add_argument("--scenario-ids", help="comma-separated Scenario IDs to process in order")
    parser.add_argument("--model", help="generation model; gpt-* uses the BBLABU-compatible endpoint")
    parser.add_argument("--judge-model", help="semantic Judge model")
    args = parser.parse_args()
    if args.count < 1 or args.workers < 1 or args.samples_per_case < 1:
        parser.error("--count, --workers and --samples-per-case must be positive")

    # ── 初始化 LLM 客户端 ────────────────────────────────────────────
    client = MiniMaxClient.from_env()
    if args.model:
        if args.model.lower().startswith("gpt-"):
            client = MiniMaxClient.judge_from_env(args.model, temperature=1.0)
        else:
            client = client.for_model(args.model)
    if args.judge_model:
        client.judge_model = args.judge_model

    # ── 加载 taxonomy 并采样场景 ──────────────────────────────────────
    taxonomy_path = args.taxonomy if args.taxonomy.is_absolute() else ROOT / args.taxonomy
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    run_dir = ROOT / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    base_manifest = (args.base_run / "manifest.json") if args.base_run else None
    scenes = _sample_scenes(
        taxonomy,
        args.seed,
        args.count,
        base_manifest,
        args.task_style,
        [item.strip() for item in args.scenario_ids.split(",") if item.strip()] if args.scenario_ids else None,
        args.distinct_domains,
        args.balanced_task_styles,
        args.all_task_styles,
    )

    # ── 加载/预热 Goal Memory ────────────────────────────────────────
    goal_memory_path = args.goal_memory if args.goal_memory.is_absolute() else ROOT / args.goal_memory
    goal_memory = _load_goal_memory(goal_memory_path)
    bootstrap_runs = [item if item.is_absolute() else ROOT / item for item in args.bootstrap_run]
    imported_goal_count = _bootstrap_goal_memory(goal_memory, bootstrap_runs) if bootstrap_runs else 0

    # ── 按 Scenario 分组，并发执行批量生成 ────────────────────────────
    results: list[dict] = []
    failures: list[str] = []
    # 按 scenario_id 分组：同一 Scenario 的多个 style 串行处理，
    # 不同 Scenario 之间并发处理
    groups: dict[str, list[tuple[str, dict]]] = {}
    for case_id, scene in scenes:
        groups.setdefault(_scenario_group_key(scene), []).append((case_id, scene))
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {
            executor.submit(
                _generate_scenario_batch_group,
                group,
                run_dir,
                client,
                copy.deepcopy(goal_memory),  # 每个线程使用独立的 memory 副本
                args.samples_per_case,
                args.resume,
            ): key
            for key, group in groups.items()
        }
        for job in as_completed(jobs):
            key = jobs[job]
            try:
                group_results, accepted = job.result()
                results.extend(group_results)
                # 将接受的 goal 合并回主 Goal Memory
                for scene, goal, case_id in accepted:
                    _remember_goal(goal_memory, scene, goal, args.run_id, case_id)
            except Exception as exc:
                failures.append(f"{key}: {_redact_secret(str(exc), client)}")

    # ── 汇总结果，写入 manifest ──────────────────────────────────────
    results.sort(key=lambda item: item["case_id"])
    _write_goal_memory(goal_memory_path, goal_memory)
    # 展开每个场景的 variant，构建 manifest 的 cases 列表
    manifest_cases = []
    for base_case_id, scene in scenes:
        for sample_index in range(1, args.samples_per_case + 1):
            expanded_scene = copy.deepcopy(scene)
            expanded_scene["case_id"] = f"{base_case_id}-V{sample_index:02d}"
            manifest_cases.append(expanded_scene)
    _write_json_atomic(run_dir / "manifest.json", {
        "run_id": args.run_id,
        "seed": args.seed,
        "count": args.count,
        "samples_per_case": args.samples_per_case,
        "generation_pipeline": "batch",
        "resume": args.resume,
        "distinct_domains": args.distinct_domains,
        "balanced_task_styles": args.balanced_task_styles,
        "all_task_styles": args.all_task_styles,
        "expanded_case_count": len(manifest_cases),
        "workers": args.workers,
        "cases": manifest_cases,
        "model": client.model,
        "provider": client.provider,
        "judge_model": client.judge_model,
        "goal_memory_path": str(goal_memory_path),
        "goal_memory_entries": sum(
            len(entries)
            for styles in goal_memory.get("scenarios", {}).values()
            if isinstance(styles, dict)
            for entries in styles.values()
            if isinstance(entries, list)
        ),
        "goal_memory_bootstrap_runs": [str(item) for item in bootstrap_runs],
        "goal_memory_bootstrap_imported": imported_goal_count,
        "goal_memory_group_count": len(groups),
        "same_group_generation": "scenario sequential; scenarios remain concurrent",
        "semantic_judge_enabled": True,
        "max_judge_attempts": MAX_STAGE1_JUDGE_ATTEMPTS,
        "rollout_generated": False,
        "api_key_serialized": False,
        "failures": failures,
    })

    # ── 错误处理 ─────────────────────────────────────────────────────
    if failures:
        raise RuntimeError("; ".join(failures))
    # 检查是否有结构校验失败的 case
    invalid = [
        f"{item['case_id']}: {json.dumps(item['validation'].get('errors', []), ensure_ascii=False)}"
        for item in results
        if item.get("validation", {}).get("structural_status") != "passed"
    ]
    if invalid:
        raise RuntimeError("stage structural validation failed; no HTML was published: " + "; ".join(invalid))

    # ── 生成 HTML 可视化报告 ──────────────────────────────────────────
    if not args.no_html:
        # Stage 1 只有 scene/task/user/voice，不能交给需要 episode_script
        # 的通用 bundle builder；使用本阶段的分组 diversity review。
        _render_html(run_dir, args.run_id, results)


if __name__ == "__main__":
    main()
