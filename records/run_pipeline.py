#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Model-generated offline environment pipeline.

The only case data owned by this program is the fixed Domain/Scenario sample.
Every downstream field is returned by the configured primary model (or its
network-only fallback) and must pass a stage gate.
"""
# 全双工语音对话系统离线训练环境数据生成主管线
#
# 五阶段流水线架构:
#   Stage 1: scene_task / user_voice  — 场景任务定义 + 用户声音画像
#   Stage 2: situation_knowledge      — 情境知识（约束/安全/参考数据/环境音）
#   Stage 3: episode_script           — 对话剧本（冻结层，下游只改表层措辞）
#   Stage 4: actor_rubric             — 演员评分标准
#   Stage 5: environment_audit        — 环境审计（终审关卡，决定发布/拒绝）
#
# 核心设计原则:
#   - 冻结控制层: Episode Script 是冻结的，下游阶段只能改变表层措辞
#   - 三层修复: 结构修复(repair_json) → 语义修复(repair_audit) → 硬失败
#   - 内容寻址缓存: cache_key = SHA256(case_id + stage + prompt + input + model)
#   - 原子写入: write-to-tmp + os.replace()，防止半写状态
#   - 字段单一职责: 每个字段只由一个阶段生成，避免跨阶段字段冲突
#
# Act 行为体系: A01(建立任务) A02(控制播报,必须T03) A03(追问/澄清)
#               A04(更新状态) A05(异议/拒绝) A06(话题管理)
# Timing 时机体系: T01(自然接话) T02(句内停顿,需pause_cue) T03(播报中途起声)
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

from minimax_client import MiniMaxClient, parse_json_text
from sample_taxonomy import sample_scene
from stage_validation import (
    ENVIRONMENT_AUDIT_ARTIFACTS,
    ENVIRONMENT_AUDIT_DIMENSIONS,
    OFFLINE_CAPABILITY_CONTRACT as CAPABILITY_CONTRACT,
    SCENE_IDENTITY_FIELDS,
    _script_behavior_owners,
    validate_stage,
)

ROOT = Path(__file__).parent
# 需要调用模型生成的阶段（不含 user_voice，因为它是 scene_task 的派生产物）
GENERATION_STAGES = ["scene_task", "situation_knowledge", "episode_script", "actor_rubric", "environment_audit"]
# 最终产出的所有 artifact 阶段（含 user_voice），用于失效传播和完整性检查
ARTIFACT_STAGES = ["scene_task", "user_voice", "situation_knowledge", "episode_script", "actor_rubric", "environment_audit"]
# 各阶段对应的 prompt 模板文件名
STAGE_PROMPTS = {
    "scene_task": "01_scene_task_user_voice.md", "situation_knowledge": "02_situation_knowledge.md",
    "episode_script": "03_offline_episode_script.md", "actor_rubric": "04_actor_rubric.md", "environment_audit": "05_environment_audit.md",
}
# 各阶段的最大生成 token 数，按内容复杂度递增
STAGE_MAX_TOKENS = {"scene_task": 4800, "situation_knowledge": 5200, "episode_script": 6500, "actor_rubric": 7500, "environment_audit": 8000}
# 结构修复（repair_json）的最大尝试次数
LOCAL_REPAIR_ATTEMPTS = 2
# 语义修复（repair_audit）的最大轮次，防止无限修复循环
MAX_SEMANTIC_REPAIR_PASSES = 2
# 强制模型返回 JSON 格式
JSON_RESPONSE_FORMAT = {"type": "json_object"}
# 传给模型的场景字段白名单：排除采样种子、分类哈希等非语义信息
MODEL_SCENE_FIELDS = (*SCENE_IDENTITY_FIELDS, "example_tasks", "task_brief")
# 各阶段期望的 JSON 顶层键集合，用于 parse_json_text 的键过滤
PREFERRED_JSON_KEYS = {
    "scene_task": {"scene", "task_instance", "user_seed", "voice_profile"},
    "situation_knowledge": {"episode_situation", "task_reference_data", "environment_audio", "estimated_snr_db", "constraints", "safety_limits"},
    "episode_script": {"episode_turns"},
    "actor_rubric": {"turn_rubrics"},
    "environment_audit": {"reviewed_refs", "findings", "dimension_scores", "total_score", "frequency_calibration", "release_decision"},
    "user_voice": {"user_seed", "voice_profile"},
}


def voice_sampling_instruction(scene: dict) -> str:
    """Return a deterministic per-case gender-presentation instruction.

    The condition is prompt-only: it is not a UserSeed field or an artifact
    field. Combining the frozen scene seed with case_id keeps variants
    independent while preserving resume/cache behavior.
    """
    # 声音性别采样指令生成器
    # 目的：为每个 case 确定性地生成一个性别呈现条件（女性/男性），
    #       该条件仅作为 prompt 的一部分，不写入 artifact 字段。
    # 设计理由：结合冻结的场景种子 + case_id 生成确定性随机数，
    #          保证同一 case 在 resume 时得到相同的性别条件，
    #          同时不同 case 之间保持独立性。
    seed = scene.get("rng_seed") if isinstance(scene, dict) else None
    if not isinstance(seed, int):
        # 若场景无显式 rng_seed，则从场景 JSON 的 SHA256 哈希派生一个
        identity = json.dumps(scene or {}, ensure_ascii=False, sort_keys=True)
        seed = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12], 16)
    case_id = scene.get("case_id") if isinstance(scene, dict) else None
    # 将 seed 和 case_id 混合，确保不同 case 即使共享 rng_seed 也会产生不同结果
    seed_material = f"{seed}:{case_id or ''}"
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:12], 16)
    # 使用确定性随机数生成器选择性别呈现
    presentation = random.Random(seed).choice(("女性", "男性"))
    return (
        "\n\n[外部声音采样条件]\n"
        f"本次调用的声音性别呈现已由外部采样器随机确定为：{presentation}。"
        "请把它自然地融入唯一的 voice_profile 自然语言描述中；其它声音特征根据场景状态和自然口语表达自由描述。"
    )


def stage_prompt(stage: str, scene: dict) -> str:
    # 加载阶段对应的 prompt 模板；仅 scene_task 阶段追加声音采样条件
    prompt = (ROOT / "prompts" / STAGE_PROMPTS[stage]).read_text(encoding="utf-8")
    return prompt + voice_sampling_instruction(scene) if stage == "scene_task" else prompt

def digest(value: object) -> str:
    # 内容寻址哈希：对任意 JSON-serializable 值计算确定性 SHA256
    # 用于缓存键、源哈希校验、响应去重等场景
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment_contract() -> dict:
    """Return the small runtime contract shared by downstream stages."""
    # 返回运行时能力合约，供所有下游阶段共享
    # 包含可用交互模态列表和完整的能力合约定义
    # 设计理由：每个阶段的 context 都需要包含能力合约，提取为函数避免重复构造
    return {
        "available_modalities": list(CAPABILITY_CONTRACT["interaction_modalities"]),
        "capability_contract": copy.deepcopy(CAPABILITY_CONTRACT),
    }


def published_stage_contexts(data: dict) -> dict[str, dict]:
    """Build release-validator contexts through the runtime stage contract.

    A previous implementation duplicated the stage-to-input mapping here and
    in :func:`stage_context`.  Those copies could silently diverge after a
    field was added to one stage.  Reuse the same builder used during a run;
    deep-copy each result because validators are allowed to inspect/mutate
    their local context.
    """
    # 构建发布验证器使用的各阶段上下文
    # 设计理由：复用 stage_context() 而非在此处重复映射逻辑，
    #          避免两处映射在新增字段后静默分叉。
    #          深拷贝每个结果，因为验证器可能检查/修改其本地上下文。
    if not isinstance(data, dict):
        data = {}
    scene_task = data.get("scene_task") if isinstance(data.get("scene_task"), dict) else {}
    scene = scene_task.get("scene") if isinstance(scene_task.get("scene"), dict) else {}
    contexts: dict[str, dict] = {}
    for stage in ("scene_task", "user_voice", "situation_knowledge", "episode_script", "actor_rubric", "environment_audit"):
        context = stage_context("", stage, scene, data)
        # 标记为已发布 artifact 的上下文，影响 scene_task 的根键集合
        context["published_artifact"] = True
        contexts[stage] = copy.deepcopy(context)
    return contexts


def compact_scene(scene: dict) -> dict:
    """Keep sampling provenance out of the public Domain/Task artifact."""
    # 将场景压缩为仅包含身份字段的公开版本
    # 设计理由：采样种子、分类哈希等采样溯源信息属于运行时元数据，
    #          不应出现在公开的 Domain/Task artifact 中
    return {field: scene.get(field) for field in SCENE_IDENTITY_FIELDS}


def model_scene(scene: dict) -> dict:
    """Pass only semantic scene inputs to a generation model.

    Sampling seeds, provenance, taxonomy hashes and case IDs remain in the
    run manifest; they are not useful generation instructions. ``task_brief``
    is retained when present because fixed cases use it as a real task bound.
    """
    # 过滤场景数据，仅传递对模型生成有用的语义输入
    # 设计理由：采样种子、溯源信息、分类哈希、case_id 等属于运行时元数据，
    #          对模型生成无帮助，保留在 manifest 中即可。
    #          task_brief 例外保留，因为固定 case 用它作为真实任务绑定。
    if not isinstance(scene, dict):
        return {}
    result = {
        field: copy.deepcopy(scene[field])
        for field in MODEL_SCENE_FIELDS
        if field in scene
    }
    # 兼容旧版分类体系：旧版用 example_task_seeds，新版用 example_tasks
    # 不向模型暴露历史字段名，但保留其语义内容
    if "example_tasks" not in result and scene.get("example_task_seeds"):
        result["example_tasks"] = copy.deepcopy(scene["example_task_seeds"])
    return result


def compact_actor_rubric_script(episode_script: dict) -> dict:
    """Pass only user goals and observable event metadata to Actor Rubric.

    ``assistant_semantic_goal`` is a Stage-3 planning hint, not an evaluation
    target.  The later actor response is judged against the user's goal and
    actual runtime evidence instead.
    """
    # 将 episode_script 压缩为 actor_rubric 所需的最小子集
    # 设计理由：assistant_semantic_goal 是 Stage-3 的规划提示，不是评估目标。
    #          Actor Rubric 应基于用户目标和可观察的运行时证据来评判，
    #          而非基于助手的规划意图，避免"自证预言"式的评估偏差。
    if not isinstance(episode_script, dict) or not isinstance(episode_script.get("episode_turns"), list):
        return {"episode_turns": []}
    # 仅保留 actor_rubric 需要的字段：用户目标、行为引用、时机引用、中断锚点、
    # 停顿提示、用户话语、旁白语音
    fields = (
        "turn_id",
        "semantic_goal",
        "act_ref",
        "timing_refs",
        "interrupt_anchor",
        "pause_cue",
        "user_utterance",
        "side_speech",
    )
    turns = []
    for item in episode_script["episode_turns"]:
        if isinstance(item, dict):
            turns.append({key: copy.deepcopy(item[key]) for key in fields if key in item})
    return {"episode_turns": turns}


def compact_episode_timing_view(episode_script: dict) -> dict:
    """Project only the expected semantic context needed for T03 anchors."""
    # 将 episode_script 投影为 environment_audit 所需的时机语义视图
    # 设计理由：environment_audit 需要验证 T03（播报中途起声）锚点的语义合理性，
    #          但不需要完整的剧本内容。此视图仅提供语义目标和时机引用，
    #          同时携带前一轮的 assistant_semantic_goal 用于跨轮次语义连贯性检查。
    if not isinstance(episode_script, dict) or not isinstance(episode_script.get("episode_turns"), list):
        return {"episode_turns": []}
    turns = []
    previous_goal = None
    for item in episode_script["episode_turns"]:
        if not isinstance(item, dict):
            continue
        turns.append({
            "turn_id": item.get("turn_id"),
            "act_ref": item.get("act_ref"),
            "timing_refs": copy.deepcopy(item.get("timing_refs", [])),
            "interrupt_anchor": item.get("interrupt_anchor"),
            "assistant_semantic_goal": item.get("assistant_semantic_goal"),
            # 前一轮的助手语义目标，用于验证 T03 中断的语义合理性
            "previous_assistant_semantic_goal": previous_goal,
        })
        previous_goal = item.get("assistant_semantic_goal")
    return {"episode_turns": turns}


def stage_context(case_id: str, stage: str, scene: dict, prior: dict) -> dict:
    """Only pass the frozen state needed by the current stage."""
    # 构建当前阶段所需的输入上下文
    # 设计理由：每个阶段只接收其所需的上游冻结状态，遵循最小知识原则。
    #          这确保了阶段间的信息隔离，防止下游阶段意外依赖不应访问的字段。
    #          字段单一职责：每个字段只由一个阶段生成，此函数精确控制可见性。
    scene_task = prior.get("scene_task", {})
    task = scene_task.get("task_instance", scene_task) if isinstance(scene_task, dict) else {}
    frozen_scene = scene_task.get("scene") if isinstance(scene_task, dict) else None
    if not isinstance(frozen_scene, dict):
        # 若 scene_task 尚未生成，使用采样时的原始场景（压缩版）
        frozen_scene = compact_scene(scene)
    user = prior.get("user_voice", {})
    situation = prior.get("situation_knowledge", {})
    episode_script = prior.get("episode_script", {})
    contract = environment_contract()
    if stage == "scene_task":
        # Stage 1: 仅需场景语义输入 + 能力合约
        return {"scene": model_scene(scene), **contract}
    task_style = scene_task.get("task_style") if isinstance(scene_task, dict) else None
    if stage == "user_voice":
        # user_voice 是 scene_task 的派生产物，需要冻结场景 + 任务 + 任务风格
        return {"case_id": case_id, "scene": frozen_scene, "task_style": task_style, "task_instance": task, **contract}
    if stage == "situation_knowledge":
        # Stage 2: 需要用户画像（user_seed, voice_profile）来生成情境知识
        return {"case_id": case_id, "scene": frozen_scene, "task_style": task_style, "task_instance": task, "user_seed": user.get("user_seed"), "voice_profile": user.get("voice_profile"), **contract}
    if stage == "episode_script":
        # Stage 3: 需要完整的情境知识来编写对话剧本
        # 注意：A01-A06 和 T01/T02/T03 的完整目录已嵌入固定 prompt 中，
        #       不在输入上下文中重复，避免与 prompt 定义冲突
        return {"case_id": case_id, "scene": frozen_scene, "task_style": task_style, "task_instance": task, "user_seed": user.get("user_seed"), "voice_profile": user.get("voice_profile"), "episode_situation": situation.get("episode_situation"), "task_reference_data": situation.get("task_reference_data"), "environment_audio": situation.get("environment_audio"), "constraints": situation.get("constraints"), "safety_limits": situation.get("safety_limits"), **contract}
    if stage == "actor_rubric":
        # Stage 4: 使用压缩版剧本（排除 assistant_semantic_goal），
        #          因为 Rubric 只评判可观察行为，不评判规划意图
        return {"case_id": case_id, "scene": frozen_scene, "task_style": task_style, "task_instance": task, "episode_situation": situation.get("episode_situation"), "task_reference_data": situation.get("task_reference_data"), "environment_audio": situation.get("environment_audio"), "constraints": situation.get("constraints"), "safety_limits": situation.get("safety_limits"), "episode_script": compact_actor_rubric_script(episode_script), **contract}
    # Stage 5 (environment_audit): 需要所有上游 artifact + 时机视图
    # timing_view 提供跨轮次语义连贯性信息，用于验证 T03 锚点
    return {
        "case_id": case_id,
        "scene_task": scene_task,
        "task_style": task_style,
        "user_voice": user,
        "situation_knowledge": situation,
        "episode_script": episode_script,
        "timing_view": compact_episode_timing_view(episode_script),
        "actor_rubric": prior.get("actor_rubric"),
        **contract,
    }


def _write_text_atomic(path: Path, value: str) -> None:
    # 原子写入文本文件：先写临时文件，再 os.replace() 替换
    # 设计理由：os.replace() 在 POSIX 上是原子操作，防止读取到半写状态
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, path)


def _write_json_atomic(path: Path, value: object) -> None:
    # 原子写入 JSON 文件，确保 UTF-8 编码和可读缩进
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _remove_case_artifacts_from(case_dir: Path, stage: str) -> None:
    """Invalidate a stage and every downstream artifact in one place."""
    # 从指定阶段开始，级联删除该阶段及所有下游的 artifact 文件和缓存
    # 设计理由：上游阶段失效后，依赖它的下游产物也必须失效，
    #          否则会出现不一致状态（如旧 audit 引用新 script）
    if stage in ARTIFACT_STAGES:
        # 删除该阶段及其所有下游阶段的文件
        stages = ARTIFACT_STAGES[ARTIFACT_STAGES.index(stage):]
    else:
        stages = [stage]
    for name in stages:
        # 删除 artifact JSON、元数据 JSON、原始响应文本
        for suffix in (".json", ".meta.json", ".raw_response.txt"):
            path = case_dir / f"{name}{suffix}"
            if path.exists():
                path.unlink()
        # 删除阶段缓存目录
        cache_path = case_dir / "stage_cache" / name
        if cache_path.exists():
            shutil.rmtree(cache_path)


def _redact_secret(text: str, client: MiniMaxClient) -> str:
    # 从文本中移除 API 密钥，替换为 [REDACTED_API_KEY]
    # 设计理由：原始响应和错误信息可能包含 API 密钥，写入磁盘前必须脱敏
    for key in (getattr(client, "api_key", ""), getattr(client, "glm_api_key", "")):
        if key:
            text = text.replace(key, "[REDACTED_API_KEY]")
    return text


def _model_cache_identity(client: MiniMaxClient) -> dict[str, str | None]:
    """Return a secret-free identity for every model eligible for this run."""
    # 返回不含密钥的模型身份信息，用于缓存键计算
    # 设计理由：缓存键需要包含模型身份以确保不同模型的响应不会混用，
    #          但密钥本身不应出现在缓存元数据中
    identity = getattr(client, "cache_model_identity", None)
    if isinstance(identity, dict):
        return copy.deepcopy(identity)
    return {"primary_model": getattr(client, "model", None), "fallback_model": None}


# 提供者元数据字段白名单：仅保留与模型调用相关的字段
PROVIDER_METADATA_FIELDS = (
    "model",
    "provider",
    "fallback",
    "fallback_from",
    "fallback_reason",
    "response_sha256",
    "timestamp",
)


def _provider_metadata(value: dict) -> dict:
    # 从响应元数据中提取提供者相关字段
    return {key: value[key] for key in PROVIDER_METADATA_FIELDS if key in value}


def _artifact_source_metadata(initial: dict, repairs: list[dict]) -> dict:
    """Attribute an artifact to the response that supplied its selected value."""
    # 确定 artifact 的来源归属：初始生成还是某次修复
    # 设计理由：需要追踪最终被选中的值来自哪次模型调用（初始生成或某次修复），
    #          以支持溯源和审计。如果某次修复被选中（selected=True），
    #          则覆盖初始生成的归属信息。
    result = {"source": "initial_generation", **_provider_metadata(initial)}
    for repair in repairs:
        if repair.get("status") == "completed" and repair.get("selected"):
            # 被选中的修复覆盖初始生成的归属
            result = {
                "source": "repair",
                "repair_attempt": repair.get("attempt"),
                **_provider_metadata(repair),
            }
    return result


def _normalize_cached_metadata(meta: dict) -> dict:
    """Upgrade pre-provenance cache metadata without rerunning a valid stage."""
    # 升级旧版缓存元数据到新的溯源格式，无需重新运行有效阶段
    # 设计理由：早期版本的缓存元数据不含 artifact_source 和 initial_response 字段，
    #          此函数将旧格式升级为新格式，保持向后兼容。
    if not isinstance(meta, dict):
        return meta
    repairs = meta.get("repair_attempts")
    repairs = repairs if isinstance(repairs, list) else []
    initial = meta.get("initial_response")
    if not isinstance(initial, dict):
        # 旧版元数据：从顶层提取提供者字段作为初始响应记录
        initial = _provider_metadata(meta)
    artifact_source = meta.get("artifact_source")
    if not isinstance(artifact_source, dict):
        # 旧版元数据：根据初始响应和修复记录重建溯源信息
        artifact_source = _artifact_source_metadata(initial, repairs)
    return {
        **meta,
        "initial_response": initial,
        "artifact_source": artifact_source,
        **_provider_metadata(artifact_source),
    }


# environment_audit 可修复的上游阶段列表（按管线顺序排列）
# 用于确定语义修复的目标阶段和修复顺序
AUDIT_UPSTREAM_STAGES = (
    "scene_task",
    "user_voice",
    "situation_knowledge",
    "episode_script",
    "actor_rubric",
)


def _is_score(value: object) -> bool:
    # 判断值是否为数值分数（int 或 float），排除 bool
    # 设计理由：Python 中 bool 是 int 的子类，需要显式排除
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _prepare_audit_summary(value: dict) -> None:
    """Apply deterministic audit shape normalization and recompute the sum."""
    # 对 environment_audit 结果进行确定性形状归一化并重算总分
    # 设计理由：模型输出可能存在格式偏差（如字段名不一致、路径格式差异），
    #          此函数仅做无害的结构归一化，不推断语义分数或证据。
    reviewed_refs = value.get("reviewed_refs")
    if isinstance(reviewed_refs, dict) and "artifacts" in reviewed_refs:
        # reviewed_refs.artifacts 是冻结的管线合约字段，运行时能力/证据键不属于此账本
        reviewed_refs["artifacts"] = list(ENVIRONMENT_AUDIT_ARTIFACTS)
    findings = value.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            # 部分模型将 finding_id 简写为 id，这是明确的结构别名，不是语义修复
            if "finding_id" not in finding and isinstance(finding.get("id"), str):
                finding["finding_id"] = finding.pop("id")
            stage = finding.get("stage")
            path = finding.get("path")
            # 审计路径相对于声明的阶段。仅当归一化常见全路径拼写
            # "/episode_script/..." 且 finding 已声明该确切阶段时才截断
            if (
                isinstance(stage, str)
                and isinstance(path, str)
                and path.startswith(f"/{stage}/")
            ):
                finding["path"] = path[len(stage) + 1 :]
    dimensions = value.get("dimension_scores")
    # 维度标签是代码拥有的枚举。仅归一化标签中空格的遗漏；
    # 语义分数和证据完全由模型生成，此处不做推断
    if isinstance(dimensions, list):
        canonical = {name.replace(" ", ""): name for name, _maximum, _minimum in ENVIRONMENT_AUDIT_DIMENSIONS}
        for item in dimensions:
            if isinstance(item, dict) and isinstance(item.get("dimension"), str):
                item["dimension"] = canonical.get(item["dimension"].replace(" ", ""), item["dimension"])
    # 当所有维度分数都是有效数值时，重算总分（确定性求和）
    if (
        isinstance(dimensions, list)
        and all(
            isinstance(item, dict) and _is_score(item.get("score"))
            for item in dimensions
        )
    ):
        value["total_score"] = sum(item["score"] for item in dimensions)


def _audit_release_formula_passes(value: object) -> bool:
    """Return the deterministic release predicate for a complete audit."""
    # 确定性的审计发布判定公式
    # 设计理由：发布决策不能仅依赖模型的 release_decision 字段，
    #          必须通过确定性公式验证所有条件，防止模型误判。
    # 发布条件：无 findings + 12 个维度 + 总分 >= 85 + frequency_calibration = N-A
    #          + 每个维度分数 >= 最低分 + 每个维度 status = "pass"
    if not isinstance(value, dict):
        return False
    findings = value.get("findings")
    dimensions = value.get("dimension_scores")
    total = value.get("total_score")
    # 存在任何 finding 则不通过
    if not isinstance(findings, list) or findings:
        return False
    # 必须恰好 12 个维度
    if not isinstance(dimensions, list) or len(dimensions) != 12:
        return False
    # 总分必须 >= 85
    if not _is_score(total) or total < 85:
        return False
    # frequency_calibration 必须为 N-A（不适用）
    if value.get("frequency_calibration") != "N-A":
        return False
    # 每个维度必须满足：分数 >= 最低分 且 status = "pass"
    return all(
        isinstance(item, dict)
        and _is_score(item.get("score"))
        and _is_score(item.get("minimum"))
        and item["score"] >= item["minimum"]
        and item.get("status") == "pass"
        for item in dimensions
    )


def _json_pointer_exists(document: object, pointer: object) -> bool:
    """Check a non-root RFC 6901 pointer without interpreting its prose."""
    # 检查 RFC 6901 JSON Pointer 是否在文档中存在（不解释其语义）
    # 设计理由：审计 finding 中的 path 字段使用 JSON Pointer 语法，
    #          需要验证路径是否指向实际存在的字段，而非凭空引用。
    #          仅检查非根指针（不以 "/" 结尾的指针）。
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        return False
    current = document
    for raw_token in pointer[1:].split("/"):
        # RFC 6901 转义：~1 → /, ~0 → ~
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                return False
            index = int(token)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _audit_repair_targets(value: object, context: dict) -> list[str]:
    """Read explicit evaluator routes; never infer a route from finding text."""
    # 从审计 findings 中读取显式的修复路由目标阶段
    # 设计理由：只使用 finding 中显式声明的 stage/path 路由，
    #          绝不从 finding 文本中推断路由，防止误修复。
    #          如果任何 finding 的路由无效（阶段不存在或路径不存在），
    #          则返回空列表，表示无法自动修复。
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list) or not value["findings"]:
        return []
    targets: list[str] = []
    for finding in value["findings"]:
        if not isinstance(finding, dict):
            # finding 格式无效，无法确定修复路由
            return []
        stage = finding.get("stage")
        path = finding.get("path")
        if stage not in AUDIT_UPSTREAM_STAGES:
            # finding 指向不可修复的阶段
            return []
        if not _json_pointer_exists(context.get(stage), path):
            # finding 的路径在上下文中不存在
            return []
        if stage not in targets:
            targets.append(stage)
    return targets


def _shape_contract(value: object) -> object:
    """Describe JSON shape without copying model-authored scalar content."""
    # 描述 JSON 结构形状，不复制模型生成的标量内容
    # 设计理由：修复 prompt 需要知道期望的 JSON 结构，
    #          但不应包含实际的模型生成内容（避免修复时直接复制原文）。
    #          仅描述类型骨架：dict → 递归描述子结构，list → 仅描述首元素，
    #          标量 → 类型名称字符串。
    if isinstance(value, dict):
        return {key: _shape_contract(child) for key, child in value.items()}
    if isinstance(value, list):
        # 仅用第一个元素代表列表元素的结构，空列表保持为空
        return [_shape_contract(value[0])] if value else []
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    return "string"


def _semantic_target_context(
    case_id: str,
    target_stage: str,
    scene: dict,
    audit_context: dict,
) -> dict:
    # 构建语义修复目标阶段的上下文
    # 设计理由：语义修复需要重新验证修复后的 artifact，
    #          因此需要构建与原始生成时相同的上下文。
    #          对于 scene_task，标记为 published_artifact 以匹配验证逻辑。
    prior = {
        stage: audit_context.get(stage)
        for stage in AUDIT_UPSTREAM_STAGES
        if isinstance(audit_context.get(stage), dict)
    }
    context = stage_context(case_id, target_stage, scene, prior)
    if target_stage == "scene_task":
        context = {**context, "published_artifact": True}
    return context


def _repair_semantic_target(
    case_id: str,
    scene: dict,
    audit: dict,
    audit_context: dict,
    client: MiniMaxClient,
) -> tuple[str, object, str, dict, list[str], dict]:
    """Repair the earliest explicitly routed artifact with repair_audit.md."""
    # 语义修复：使用 repair_audit.md prompt 修复审计发现指向的最早阶段
    # 设计理由：当 environment_audit 发现问题时，需要修复最早的上游阶段，
    #          因为修复早期阶段会使下游阶段级联失效并重新生成。
    #          选择最早阶段（按 AUDIT_UPSTREAM_STAGES 顺序）确保修复的
    #          影响范围最小化，避免不必要的下游重生成。
    targets = _audit_repair_targets(audit, audit_context)
    if not targets:
        raise ValueError("environment audit has no complete explicit semantic repair route")
    # 选择最早的目标阶段（管线顺序最靠前的）
    target_stage = min(targets, key=AUDIT_UPSTREAM_STAGES.index)
    # 提取指向目标阶段的 findings
    target_findings = [
        copy.deepcopy(finding)
        for finding in audit["findings"]
        if isinstance(finding, dict) and finding.get("stage") == target_stage
    ]
    current_artifact = copy.deepcopy(audit_context[target_stage])
    target_index = AUDIT_UPSTREAM_STAGES.index(target_stage)
    # 冻结目标阶段之前的所有上游 artifact（修复不能改变它们）
    frozen_upstream = {
        stage: copy.deepcopy(audit_context.get(stage))
        for stage in AUDIT_UPSTREAM_STAGES[:target_index]
        if isinstance(audit_context.get(stage), dict)
    }
    # 包含能力合约（修复需要知道系统约束）
    for field in ("capability_contract",):
        if field in audit_context:
            frozen_upstream[field] = copy.deepcopy(audit_context[field])
    repair_context = {
        "target_stage": target_stage,
        "target_findings": target_findings,
        "current_artifact": current_artifact,
        "frozen_upstream": frozen_upstream,
        "expected_shape": _shape_contract(current_artifact),
    }
    # 调用 repair_audit.md prompt 进行语义修复
    prompt = (ROOT / "prompts" / "repair_audit.md").read_text(encoding="utf-8")
    raw, meta = client.complete(
        prompt,
        repair_context,
        max_tokens=STAGE_MAX_TOKENS.get(target_stage, 6500),
        response_format=JSON_RESPONSE_FORMAT,
    )
    try:
        repaired = parse_json_text(raw, PREFERRED_JSON_KEYS.get(target_stage))
    except Exception as exc:
        repaired = {"_parse_error": str(exc)}
    # 构建目标阶段的验证上下文
    target_context = _semantic_target_context(
        case_id,
        target_stage,
        scene,
        audit_context,
    )
    # 应用代码拥有的归一化（ID 分配、场景冻结等）
    repaired = _prepare_stage_value(target_stage, repaired, target_context)
    # 验证修复后的 artifact
    errors = validate_stage(target_stage, repaired, target_context)
    return target_stage, repaired, raw, meta, errors, target_context


def _semantic_repair_history(case_dir: Path) -> list[dict]:
    """Load the case-level repair ledger, migrating legacy artifact metadata."""
    # 加载 case 级别的语义修复账本，兼容迁移旧版分散在各阶段元数据中的修复记录
    # 设计理由：早期版本将语义修复记录分散存储在各阶段的 .meta.json 中，
    #          新版统一存储在 semantic_repair_history.json 中。
    #          此函数先尝试读取新格式，若不存在则从旧格式迁移。
    ledger_path = case_dir / "semantic_repair_history.json"
    try:
        history = json.loads(ledger_path.read_text(encoding="utf-8"))
        if isinstance(history, list):
            return [copy.deepcopy(item) for item in history if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    # 从旧版各阶段元数据中迁移修复记录
    history = []
    seen: set[tuple[object, object, object]] = set()
    for stage in AUDIT_UPSTREAM_STAGES:
        meta_path = case_dir / f"{stage}.meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        repairs = meta.get("semantic_repairs") if isinstance(meta, dict) else None
        if not isinstance(repairs, list):
            continue
        for item in repairs:
            if not isinstance(item, dict):
                continue
            # 使用 (response_sha256, timestamp, finding_ids) 三元组去重
            identity = (
                item.get("response_sha256"),
                item.get("timestamp"),
                tuple(item.get("finding_ids", [])) if isinstance(item.get("finding_ids"), list) else None,
            )
            if identity in seen:
                continue
            seen.add(identity)
            history.append({"target_stage": stage, "status": "published", **copy.deepcopy(item)})
    return history


def _append_semantic_repair_history(case_dir: Path, entry: dict) -> None:
    # 追加语义修复记录到 case 级别账本
    # 设计理由：使用 (response_sha256, timestamp, target_stage) 三元组
    #          识别重复记录，若已存在则更新而非追加，防止重复计数。
    history = _semantic_repair_history(case_dir)
    identity = (
        entry.get("response_sha256"),
        entry.get("timestamp"),
        entry.get("target_stage"),
    )
    matching_index = next(
        (
            index
            for index, item in enumerate(history)
            if (
                item.get("response_sha256"),
                item.get("timestamp"),
                item.get("target_stage"),
            ) == identity
        ),
        None,
    )
    if matching_index is None:
        history.append(copy.deepcopy(entry))
    else:
        # 更新已有记录（如从 "failed_structure" 更新为 "published"）
        history[matching_index] = {**history[matching_index], **copy.deepcopy(entry)}
    _write_json_atomic(case_dir / "semantic_repair_history.json", history)


def _semantic_repair_count(case_dir: Path) -> int:
    # 返回已完成的语义修复次数，用于限制修复轮次
    return len(_semantic_repair_history(case_dir))


def _publish_semantic_repair(
    case_dir: Path,
    target_stage: str,
    repaired: object,
    raw: str,
    meta: dict,
    findings: list[dict],
    client: MiniMaxClient,
    structural_repairs: list[dict] | None = None,
) -> None:
    """Publish a validated target artifact and retain its generation cache key."""
    # 发布经过验证的语义修复 artifact，更新元数据和修复账本
    # 设计理由：语义修复成功后，需要将修复后的 artifact 写入正式位置，
    #          更新溯源元数据，并记录到修复账本中。
    #          如果修复的是 scene_task，还需要级联更新 user_voice 的派生关系。
    meta_path = case_dir / f"{target_stage}.meta.json"
    try:
        existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        existing_meta = {}
    # 提取指向目标阶段的 findings
    target_findings = [
        copy.deepcopy(finding)
        for finding in findings
        if isinstance(finding, dict) and finding.get("stage") == target_stage
    ]
    finding_ids = [finding.get("finding_id") for finding in target_findings]
    artifact_source = {
        "source": "semantic_repair",
        "finding_ids": finding_ids,
        "findings": target_findings,
        **_provider_metadata(meta),
    }
    # 如果语义修复后还经过了结构修复且被选中，更新溯源为结构清理
    for repair in structural_repairs or []:
        if repair.get("status") == "completed" and repair.get("selected"):
            artifact_source = {
                "source": "semantic_repair_structural_cleanup",
                "finding_ids": finding_ids,
                "findings": target_findings,
                "repair_attempt": repair.get("attempt"),
                **_provider_metadata(repair),
            }
    # 追加到该阶段的语义修复历史
    history = existing_meta.get("semantic_repairs")
    history = copy.deepcopy(history) if isinstance(history, list) else []
    history.append(artifact_source)
    repaired_meta = {
        **existing_meta,
        "artifact_source": artifact_source,
        **_provider_metadata(artifact_source),
        "semantic_repairs": history,
    }
    # user_voice 是 scene_task 的派生产物，需要记录派生关系
    if target_stage == "user_voice":
        scene_task_path = case_dir / "scene_task.json"
        scene_task = json.loads(scene_task_path.read_text(encoding="utf-8"))
        repaired_meta.update({
            "derived_from": "scene_task",
            "source_hash": digest(scene_task),
        })
    # 原子写入修复后的 artifact、元数据和原始响应
    _write_json_atomic(case_dir / f"{target_stage}.json", repaired)
    _write_json_atomic(meta_path, repaired_meta)
    _write_text_atomic(
        case_dir / f"{target_stage}.raw_response.txt",
        _redact_secret(raw, client),
    )
    # 追加到 case 级别的语义修复账本
    _append_semantic_repair_history(case_dir, {
        "target_stage": target_stage,
        "status": "published",
        **copy.deepcopy(artifact_source),
    })
    # 如果修复的是 scene_task，需要级联更新 user_voice 的派生哈希
    if target_stage == "scene_task":
        user_meta_path = case_dir / "user_voice.meta.json"
        if user_meta_path.exists():
            try:
                user_meta = json.loads(user_meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                user_meta = {}
            user_meta.update({
                "derived_from": "scene_task",
                "source_hash": digest(repaired),
            })
            _write_json_atomic(user_meta_path, user_meta)


def _remove_downstream_after_semantic_repair(case_dir: Path, target_stage: str) -> None:
    """Keep the repaired artifact and invalidate only its consumers."""
    # 语义修复后，保留修复的 artifact，仅使其下游消费者失效
    # 设计理由：修复 scene_task/user_voice 会使 situation_knowledge 及之后全部失效；
    #          修复其他阶段则仅使紧邻的下游阶段及之后全部失效。
    #          修复后的 artifact 本身保留，因为它是新值。
    if target_stage in {"scene_task", "user_voice"}:
        # scene_task 和 user_voice 的修复都会使 situation_knowledge 失效
        first_downstream = "situation_knowledge"
    else:
        index = ARTIFACT_STAGES.index(target_stage) + 1
        if index >= len(ARTIFACT_STAGES):
            # 没有下游阶段需要失效
            return
        first_downstream = ARTIFACT_STAGES[index]
    _remove_case_artifacts_from(case_dir, first_downstream)


def _repair_quality(stage: str, value: object, errors: list[str]) -> tuple[int, int]:
    """Prefer a structurally usable repair before minimizing leaf errors."""
    # 修复质量评估：优先保证结构可用，其次最小化叶子错误数
    # 设计理由：一个结构完整但有少量字段错误的修复，比一个结构破碎但错误数
    #          更少的修复更有价值。结构惩罚（+100）远大于叶子错误数，
    #          确保结构完整的修复总是被优先选择。
    # 返回：(结构惩罚, 叶子错误数) 的元组，用于修复候选排序
    structural_penalty = 0
    # episode_script 必须有 episode_turns 列表
    if stage == "episode_script" and (
        not isinstance(value, dict)
        or not isinstance(value.get("episode_turns"), list)
    ):
        structural_penalty += 100
    # actor_rubric 必须有 turn_rubrics 列表
    if stage == "actor_rubric" and (
        not isinstance(value, dict)
        or not isinstance(value.get("turn_rubrics"), list)
    ):
        structural_penalty += 100
    # situation_knowledge 必须有完整的结构字段
    if stage == "situation_knowledge" and (
        not isinstance(value, dict)
        or not isinstance(value.get("constraints"), list)
        or not isinstance(value.get("safety_limits"), list)
        or not isinstance(value.get("task_reference_data"), list)
        or not isinstance(value.get("episode_situation"), str)
    ):
        structural_penalty += 100
    # environment_audit 必须有 dimension_scores 列表
    if stage == "environment_audit" and (
        not isinstance(value, dict) or not isinstance(value.get("dimension_scores"), list)
    ):
        structural_penalty += 100
    return structural_penalty, len(errors)


def _prepare_stage_value(stage: str, value: object, context: dict) -> object:
    """Apply only code-owned IDs, uniquely implied refs and score arithmetic."""
    # 应用代码拥有的归一化：ID 分配、隐含引用、分数算术
    # 设计理由：仅处理代码拥有且确定性可计算的字段（如位置 ID、冻结场景、总分），
    #          不修改模型生成的语义内容。这确保了无效语义对验证和修复可见，
    #          而非在代码中被静默重写。
    if stage == "situation_knowledge" and isinstance(value, dict):
        # 分配位置 ID：C01/C02/... 给 constraints，L01/L02/... 给 safety_limits
        _normalize_situation_ids(value)
    if stage == "scene_task" and isinstance(value, dict):
        # Domain/Scenario 在模型调用前已采样，是不可变的。
        # 保持模型负责 goal/user/voice 的文本生成，同时防止偶发的
        # 改写或修复包装改变已采样的场景身份。
        sampled_scene = context.get("scene")
        if isinstance(sampled_scene, dict) and isinstance(value.get("scene"), dict):
            # 用采样的冻结场景替换模型可能改写的场景
            value["scene"] = compact_scene(sampled_scene)
        if isinstance(sampled_scene, dict) and sampled_scene.get("task_style") in {"开放式互动", "结构化整理", "混合型", "常识问答"}:
            # task_style 由采样决定，不由模型生成
            value["task_style"] = sampled_scene["task_style"]
        # 修复响应可能回显内部路由标记 "stage"，它不属于 Stage-1 公开合约，可安全移除
        if set(value) - {"scene", "task_style", "task_instance", "user_seed", "voice_profile"} == {"stage"}:
            value.pop("stage", None)
    if stage == "episode_script" and isinstance(value, dict):
        # 分配位置 ID：U01/U02/... 给 episode_turns
        _assign_episode_turn_ids(value)
    if stage == "actor_rubric" and isinstance(value, dict):
        # 归一化 actor_rubric 结构：对齐 turn_id，移除多余字段
        _normalize_actor_rubric_structure(value, context)
    if stage == "environment_audit" and isinstance(value, dict):
        # 归一化审计摘要：修正 finding_id、路径格式、维度标签，重算总分
        _prepare_audit_summary(value)
    return value


def _normalize_situation_ids(value: dict) -> None:
    """Assign positional Cxx/Lxx IDs owned by the Stage-2 schema."""
    # 为 situation_knowledge 的 constraints 和 safety_limits 分配位置 ID
    # 设计理由：C01/C02/... 和 L01/L02/... 是 Stage-2 schema 拥有的确定性 ID，
    #          由代码根据数组位置分配，不由模型生成。
    #          这确保了 ID 的稳定性和一致性，便于下游引用和审计追踪。
    for field, prefix in (("constraints", "C"), ("safety_limits", "L")):
        items = value.get(field)
        if not isinstance(items, list):
            continue
        id_field = "constraint_id" if field == "constraints" else "limit_id"
        for index, item in enumerate(items, 1):
            if isinstance(item, dict):
                item[id_field] = f"{prefix}{index:02d}"


def _normalize_actor_rubric_structure(value: dict, context: dict) -> None:
    """Normalize only turn identities and extra wrapper fields."""
    # 归一化 actor_rubric 结构：对齐 turn_id，移除多余字段
    # 设计理由：actor_rubric 的 turn_id 必须与 episode_script 的 turn_id 一致，
    #          由代码根据数组位置强制对齐。同时移除 schema 之外的多余字段，
    #          保持 artifact 的结构清洁。
    turn_rubrics = value.get("turn_rubrics")
    script = context.get("episode_script")
    # 从 episode_script 中提取 turn_id 序列，用于对齐
    script_turn_ids = [
        item.get("turn_id")
        for item in script.get("episode_turns", [])
        if isinstance(item, dict) and isinstance(item.get("turn_id"), str)
    ] if isinstance(script, dict) and isinstance(script.get("episode_turns"), list) else []
    if not isinstance(turn_rubrics, list):
        return
    # actor_rubric 每项只允许这三个字段
    allowed_fields = {"turn_id", "semantic", "behavior"}
    for index, item in enumerate(turn_rubrics):
        if not isinstance(item, dict):
            continue
        # 按位置对齐 turn_id
        if index < len(script_turn_ids):
            item["turn_id"] = script_turn_ids[index]
        # 移除 schema 之外的字段
        for key in list(item):
            if key not in allowed_fields:
                item.pop(key, None)


def _assign_episode_turn_ids(value: dict) -> None:
    """Assign only the positional Uxx identities owned by the pipeline.

    Array order, Act/Timing choices, conditions and utterances remain
    model-authored. Invalid semantics therefore stay visible to validation and
    targeted repair instead of being rewritten in code.
    """
    # 为 episode_script 的 episode_turns 分配位置 ID：U01/U02/...
    # 设计理由：Uxx ID 是管线拥有的确定性标识，由代码根据数组位置分配。
    #          数组顺序、Act/Timing 选择、条件和话语完全由模型生成，
    #          不在代码中重写。这确保了无效语义对验证和定向修复可见，
    #          而非被代码静默修正。
    turns = value.get("episode_turns")
    if isinstance(turns, list):
        for index, item in enumerate(turns, 1):
            if isinstance(item, dict):
                item["turn_id"] = f"U{index:02d}"


# 受保护的文本字段集合：结构修复必须保留这些模型生成的文本内容
# 设计理由：结构修复（repair_json）只应修复 JSON 结构问题，
#          不应改变模型生成的语义文本。这些字段的文本内容在修复前后
#          必须完全一致，否则修复会被拒绝。
_PROTECTED_TEXT_FIELDS = {
    "domain",
    "scenario",
    "goal",
    "role_relation",
    "task_familiarity",
    "voice_profile",
    "text",
    "episode_situation",
    "environment_audio",
    "disclose_when",
    "why_unknown",
    "semantic_goal",
    "assistant_semantic_goal",
    "user_utterance",
    "semantic",
    "behavior",
    "evidence",
    "reason",
    "suggested_fix",
}


def _protected_text(value: object) -> dict[tuple[object, ...], str]:
    """Collect model-authored prose that a structural repair must preserve."""
    # 收集所有受保护的模型生成文本，以 JSON 路径为键
    # 设计理由：结构修复后需要比对受保护文本是否被改变，
    #          此函数递归遍历 JSON 树，收集所有 _PROTECTED_TEXT_FIELDS 中的文本值。
    result: dict[tuple[object, ...], str] = {}

    def visit(node: object, path: tuple[object, ...], protected: bool = False) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                # 当键名在受保护字段集合中时，标记子树为受保护
                visit(child, (*path, key), key in _PROTECTED_TEXT_FIELDS)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                # 列表中的元素继承父级的受保护状态
                visit(child, (*path, index), protected)
        elif protected and isinstance(node, str):
            # 受保护路径下的字符串值被收集
            result[path] = node

    visit(value, ())
    return result


def _text_preservation_error(before: object, after: object) -> str | None:
    # 检查结构修复是否改变了受保护的模型生成文本
    # 设计理由：结构修复只应修复 JSON 结构，不应改变语义文本。
    #          如果受保护文本被改变，返回错误描述；否则返回 None。
    #          最多报告前 8 个变化路径，避免错误信息过长。
    before_text = _protected_text(before)
    after_text = _protected_text(after)
    if before_text == after_text:
        return None
    changed_paths = sorted(
        {
            "/" + "/".join(str(part) for part in path)
            for path in set(before_text) | set(after_text)
            if before_text.get(path) != after_text.get(path)
        }
    )
    return "structural repair changed model-authored text at " + ", ".join(changed_paths[:8])


def _stage_root_keys(stage: str, context: dict) -> tuple[str, ...]:
    # 返回各阶段期望的 JSON 顶层键集合
    # 设计理由：结构修复 prompt 需要知道期望的顶层键，
    #          以便模型生成正确结构的 JSON。
    #          scene_task 在 published_artifact 模式下不包含 user_seed/voice_profile
    #          （它们已拆分为独立的 user_voice artifact）。
    if stage == "scene_task":
        task_style = {"task_style"} if isinstance(context.get("scene"), dict) and context["scene"].get("task_style") else set()
        return (
            ("scene", "task_instance", *task_style)
            if context.get("published_artifact")
            else ("scene", "task_instance", *task_style, "user_seed", "voice_profile")
        )
    return {
        "user_voice": ("user_seed", "voice_profile"),
        "situation_knowledge": (
            "episode_situation",
            "task_reference_data",
            "environment_audio",
            "estimated_snr_db",
            "constraints",
            "safety_limits",
        ),
        "episode_script": ("episode_turns",),
        "actor_rubric": ("turn_rubrics",),
        "environment_audit": (
            "reviewed_refs",
            "findings",
            "dimension_scores",
            "total_score",
            "frequency_calibration",
            "release_decision",
        ),
    }[stage]


def _stage_shape_contract(stage: str, context: dict) -> dict:
    """Return a compact field/type contract, without sample prose values."""
    # 返回各阶段的紧凑字段/类型合约，不含示例文本值
    # 设计理由：结构修复 prompt 需要知道期望的字段结构，
    #          但不应包含实际的模型生成内容（避免修复时直接复制原文）。
    #          仅描述字段名和类型约束，保留语义内容的模型自主性。
    item_contracts = {
        "scene_task": {
            "scene": ["domain_id", "domain", "scenario_id", "scenario"],
            "task_style": "开放式互动 or 结构化整理 or 混合型 or 常识问答",
            "task_instance": ["goal"],
        },
        "user_voice": {
            "user_seed": [
                "role_relation",
                "task_familiarity",
            ],
            "voice_profile": "non-empty scene-conditioned natural-language string",
        },
        "situation_knowledge": {
            "task_reference_data[]": "non-empty natural-language string",
            "constraints[]": ["constraint_id", "text", "kind"],
            "safety_limits[]": ["limit_id", "text", "severity"],
        },
        "episode_script": {
            "episode_turns[]": [
                "turn_id",
                "semantic_goal",
                "act_ref",
                "timing_refs",
                "user_utterance",
                "assistant_semantic_goal",
                "pause_cue (only with T02)",
            ],
            "pause_cue": ["after_text", "duration_ms"],
        },
        "actor_rubric": {
            "turn_rubrics[]": ["turn_id", "semantic[] (1-3 short criteria)", "behavior[]"],
            "behavior[]": "short observable natural-language criterion",
        },
        "environment_audit": {
            "reviewed_refs": ["artifacts", "C", "L", "U"],
            "findings[]": [
                "finding_id",
                "category",
                "severity",
                "stage",
                "path",
                "evidence",
                "reason",
                "suggested_fix",
            ],
            "dimension_scores[]": ["dimension", "score", "max", "minimum", "status", "evidence"],
        },
    }
    contract = {
        "root_keys": list(_stage_root_keys(stage, context)),
        "item_fields": copy.deepcopy(item_contracts[stage]),
    }
    # scene_task 在非 published 模式下还需包含 user_voice 的字段
    if stage == "scene_task" and not context.get("published_artifact"):
        contract["item_fields"].update(item_contracts["user_voice"])
    return contract


def _id_values(root: object, list_field: str, id_field: str) -> list[str]:
    # 从 JSON 对象的列表字段中提取所有 ID 值
    # 用于构建审计引用的 ID 集合（如 C01/C02, L01/L02, U01/U02）
    if not isinstance(root, dict) or not isinstance(root.get(list_field), list):
        return []
    return [
        str(item[id_field])
        for item in root[list_field]
        if isinstance(item, dict) and item.get(id_field)
    ]


def _structural_mappings(stage: str, value: object, context: dict) -> dict:
    """Expose only code-verifiable identities and reference partitions."""
    # 暴露仅代码可验证的身份标识和引用分区
    # 设计理由：结构修复 prompt 需要知道代码拥有的确定性映射关系，
    #          如冻结场景、turn_id 序列、约束/安全限制 ID、维度定义等。
    #          这些映射由代码确定，模型必须遵守，不能自行发明。
    if stage == "scene_task":
        # 冻结场景：模型不能改变已采样的场景身份
        scene = context.get("scene")
        return {"frozen_scene": compact_scene(scene)} if isinstance(scene, dict) else {}
    if stage == "situation_knowledge":
        # situation_knowledge 无代码拥有的确定性映射
        return {}
    if stage == "episode_script":
        # 代码拥有的 turn_id 序列：U01/U02/...
        turns = value.get("episode_turns", []) if isinstance(value, dict) else []
        return {"code_owned_turn_ids": [f"U{index:02d}" for index in range(1, len(turns) + 1)]}
    if stage == "actor_rubric":
        # 从 episode_script 中提取 turn_id 和拥有 behavior 的 turn_id
        script = context.get("episode_script")
        behavior_owners = _script_behavior_owners(script)
        return {
            "turn_ids": _id_values(script, "episode_turns", "turn_id"),
            "behavior_turn_ids": sorted(behavior_owners),
        }
    if stage == "environment_audit":
        # 审计引用：artifact 列表、约束 ID、安全限制 ID、turn ID、维度定义
        situation = context.get("situation_knowledge")
        script = context.get("episode_script")
        return {
            "reviewed_refs": {
                "artifacts": list(ENVIRONMENT_AUDIT_ARTIFACTS),
                "C": sorted(_id_values(situation, "constraints", "constraint_id")),
                "L": sorted(_id_values(situation, "safety_limits", "limit_id")),
                "U": sorted(_id_values(script, "episode_turns", "turn_id")),
            },
            "dimensions": [
                {"dimension": name, "max": maximum, "minimum": minimum}
                for name, maximum, minimum in ENVIRONMENT_AUDIT_DIMENSIONS
            ],
        }
    return {}


def _repair_stage(stage: str, value: object, errors: list[str], context: dict, client: MiniMaxClient) -> tuple[object, str, dict, list[str]]:
    # 结构修复：使用 repair_json.md prompt 修复 JSON 结构问题
    # 设计理由：本地阶段修复仅处理结构问题（缺失字段、类型错误等）。
    #          语义修复是独立的终审评估器路径，由 _repair_semantic_target 处理。
    #          结构修复必须保留模型生成的受保护文本，否则修复会被拒绝。
    prompt = (ROOT / "prompts" / "repair_json.md").read_text(encoding="utf-8")
    repair_context = {
        "stage": stage,
        "validation_errors": list(errors),
        "current_artifact": copy.deepcopy(value),
        "expected_shape": _stage_shape_contract(stage, context),
        "deterministic_mappings": _structural_mappings(stage, value, context),
    }
    # 修复阶段的 token 限制，按内容复杂度调整
    repair_tokens = {
        "situation_knowledge": 5200,
        "episode_script": 6500,
        "actor_rubric": 7500,
        "environment_audit": 8000,
    }.get(stage, 2600)
    raw, meta = client.complete(prompt, repair_context, max_tokens=repair_tokens,
                                 response_format=JSON_RESPONSE_FORMAT)
    repaired = parse_json_text(raw, PREFERRED_JSON_KEYS.get(stage))
    # 应用代码拥有的归一化
    repaired = _prepare_stage_value(stage, repaired, context)
    # 验证修复后的 artifact
    candidate_errors = validate_stage(stage, repaired, context)
    # 检查受保护文本是否被改变
    preservation_error = _text_preservation_error(value, repaired)
    if preservation_error:
        candidate_errors.append(preservation_error)
    return repaired, raw, meta, candidate_errors


def _run_local_repairs(
    stage: str,
    initial_value: object,
    initial_errors: list[str],
    context: dict,
    client: MiniMaxClient,
    cache_dir: Path,
) -> tuple[object, list[str], list[dict]]:
    """Repair the latest candidate while retaining the least-error result.

    Equal error counts can represent real progress when the error set changes.
    The latest successful response therefore feeds the next attempt regardless
    of whether it becomes the best candidate.  Exceptions leave that latest
    candidate intact, are recorded without secrets, and do not consume the
    opportunity to make the second (and final) local repair request.
    """
    # 执行本地结构修复循环：最多 LOCAL_REPAIR_ATTEMPTS 次尝试
    # 设计理由：
    #   - 保留错误最少的修复结果作为最佳候选
    #   - 相同错误数可能代表真实进展（错误集合变了），
    #     因此最新的成功响应用于下一次尝试，无论是否成为最佳候选
    #   - 异常不消耗修复机会：异常时保持当前候选不变，
    #     记录脱敏后的异常信息，继续下一次尝试
    current_value = initial_value
    current_errors = list(initial_errors)
    best_value = initial_value
    best_errors = list(initial_errors)
    best_quality = _repair_quality(stage, best_value, best_errors)
    repairs: list[dict] = []

    for attempt in range(1, LOCAL_REPAIR_ATTEMPTS + 1):
        if not current_errors:
            # 当前候选已无错误，无需继续修复
            break
        input_errors = list(current_errors)
        try:
            candidate, repair_raw, repair_meta, candidate_errors = _repair_stage(
                stage, current_value, current_errors, context, client
            )
            # 检查修复是否改变了初始值的受保护文本
            baseline_error = _text_preservation_error(initial_value, candidate)
            if baseline_error and baseline_error not in candidate_errors:
                candidate_errors.append(baseline_error)
            if baseline_error:
                # 受保护文本被改变，回退到修复前的错误状态
                current_errors = input_errors
            else:
                # 受保护文本未改变，更新当前候选
                current_value = candidate
                current_errors = list(candidate_errors)
            # 评估修复质量：结构惩罚 + 叶子错误数
            candidate_quality = _repair_quality(stage, candidate, candidate_errors)
            # 选择质量更好的候选（结构惩罚优先，叶子错误数次之）
            selected = candidate_quality < best_quality
            if selected:
                best_value = candidate
                best_errors = list(candidate_errors)
                best_quality = candidate_quality
            repairs.append({
                "attempt": attempt,
                "status": "completed",
                "input_errors": input_errors,
                "response_sha256": digest(repair_raw),
                **repair_meta,
                "remaining_errors": list(candidate_errors),
                "selected": selected,
            })
            # 保存修复的原始响应（脱敏后）
            _write_text_atomic(
                cache_dir / f"repair_{attempt}.raw_response.txt",
                _redact_secret(repair_raw, client),
            )
        except Exception as exc:
            # 异常不消耗修复机会：记录脱敏异常，保持当前候选不变
            safe_message = _redact_secret(str(exc), client)
            repairs.append({
                "attempt": attempt,
                "status": "exception",
                "input_errors": input_errors,
                "exception_type": type(exc).__name__,
                "exception": safe_message,
                "remaining_errors": list(current_errors),
                "selected": False,
            })
        # 每次尝试后持久化修复记录
        _write_json_atomic(cache_dir / "repair_attempts.json", repairs)

    return best_value, best_errors, repairs


def run_case(case_id: str, scene: dict, run_dir: Path, client: MiniMaxClient, resume: bool, repair_depth: int = 0) -> dict:
    # 运行单个 case 的完整五阶段管线
    # 参数:
    #   case_id: case 标识符
    #   scene: 采样后的场景数据
    #   run_dir: 运行输出目录
    #   client: 模型调用客户端
    #   resume: 是否启用断点续跑（复用缓存的有效 artifact）
    #   repair_depth: 语义修复递归深度，用于防止无限递归
    # 返回: 各阶段的最终 artifact 字典
    # 设计理由：这是管线的核心执行函数，实现了：
    #   - 内容寻址缓存：cache_key = SHA256(case_id + stage + prompt + input + model)
    #   - 断点续跑：resume 时复用缓存的有效 artifact
    #   - 三层修复：结构修复 → 语义修复 → 硬失败
    #   - 失效传播：上游失败时级联删除下游 artifact
    #   - user_voice 派生：从 scene_task 中拆分 user_seed/voice_profile
    case_dir = run_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    prior: dict = {}
    validation_report: dict[str, dict] = {}
    derived_user_voice_path = case_dir / "user_voice.json"
    if resume:
        # 断点续跑：检查 user_voice.json 是否与当前 scene_task.json 一致
        # user_voice.json 是 Stage-1 组合响应的派生兄弟文件。
        # 只有当其记录的源哈希与当前发布的 scene_task.json 匹配时才可复用；
        # 否则使 Stage-1 及下游 artifact 失效，防止旧画像进入新运行。
        scene_task_path = case_dir / "scene_task.json"
        user_voice_meta_path = case_dir / "user_voice.meta.json"
        source_matches = False
        try:
            scene_task_value = json.loads(scene_task_path.read_text(encoding="utf-8"))
            user_voice_meta = json.loads(user_voice_meta_path.read_text(encoding="utf-8"))
            # 验证派生关系：derived_from == "scene_task" 且 source_hash 匹配
            source_matches = (
                isinstance(scene_task_value, dict)
                and isinstance(user_voice_meta, dict)
                and user_voice_meta.get("derived_from") == "scene_task"
                and user_voice_meta.get("source_hash") == digest(scene_task_value)
            )
        except (OSError, json.JSONDecodeError, TypeError):
            source_matches = False
        if source_matches and derived_user_voice_path.exists():
            try:
                prior["user_voice"] = json.loads(derived_user_voice_path.read_text(encoding="utf-8"))
                validation_report["user_voice"] = {"status": "cached", "errors": [], "repair_attempts": []}
            except (OSError, json.JSONDecodeError):
                source_matches = False
        if not source_matches and (scene_task_path.exists() or derived_user_voice_path.exists()):
            # 已发布的 scene_task 没有有效的派生兄弟文件，无法为 Stage-2
            # 提供用户/声音输入。重新运行 Stage-1，而非允许部分或遗留状态
            # 泄漏到下游缓存中。
            _remove_case_artifacts_from(case_dir, "scene_task")
    # ===== 主阶段循环：依次执行五个生成阶段 =====
    for stage in GENERATION_STAGES:
        out_path = case_dir / f"{stage}.json"
        prompt = stage_prompt(stage, scene)
        context = stage_context(case_id, stage, scene, prior)
        model_identity = _model_cache_identity(client)
        # 内容寻址缓存键：SHA256(case_id + stage + prompt + input + model_identity)
        # 确保任何输入变化都会使缓存失效
        cache_key = digest({
            "case_id": case_id,
            "stage": stage,
            "prompt": prompt,
            "input": context,
            "model_identity": model_identity,
        })
        meta_path = case_dir / f"{stage}.meta.json"
        cached = False
        # ----- 缓存命中检查 -----
        if resume and out_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # 升级旧版缓存元数据到新格式
            normalized_meta = _normalize_cached_metadata(meta)
            candidate = json.loads(out_path.read_text(encoding="utf-8"))
            original_candidate = copy.deepcopy(candidate)
            # 应用代码拥有的归一化（可能与缓存时不同）
            candidate = _prepare_stage_value(stage, candidate, context)
            validation_context = context
            if stage == "scene_task" and isinstance(prior.get("user_voice"), dict):
                # scene_task 已拆分 user_voice，验证时使用 published 模式
                validation_context = {**context, "published_artifact": True}
            current_errors = validate_stage(stage, candidate, validation_context)
            same_cache = meta.get("cache_key") == cache_key
            # 仅公式有效的审计通过才可复用。非通过或不一致的通过必须
            # 重新进入显式 finding 路由。
            nonpassing_audit = (
                stage == "environment_audit"
                and (
                    not isinstance(candidate, dict)
                    or candidate.get("release_decision") != "pass"
                    or not _audit_release_formula_passes(candidate)
                )
            )
            # 缓存可用条件：相同缓存键 + 无验证错误 + 审计通过
            if same_cache and not current_errors and not nonpassing_audit:
                value = candidate
                cached = True
                # 如果归一化改变了候选值或元数据需要升级，重写文件
                if candidate != original_candidate or normalized_meta != meta:
                    _write_json_atomic(out_path, value)
                    _write_json_atomic(meta_path, {
                        **normalized_meta,
                        "cache_key": cache_key,
                        "prompt_hash": digest(prompt),
                        "input_hash": digest(context),
                        "normalized_shape": candidate != original_candidate,
                    })
        if cached:
            validation_report[stage] = {"status": "cached", "errors": [], "repair_attempts": []}
        else:
            # ----- 缓存未命中：需要生成或修复 -----
            # 失败的阶段保留其精确响应和解析候选在 stage_cache 中。
            # 断点续跑时复用该候选，仅花费下一次请求于修复。
            cache_dir = case_dir / "stage_cache" / stage
            recovered = False
            cache_input = cache_dir / "input.json"
            cache_prompt = cache_dir / "prompt.md"
            cache_parsed = cache_dir / "parsed_output.json"
            cache_raw = cache_dir / "raw_response.txt"
            # 尝试从 stage_cache 恢复之前的失败候选
            if resume and cache_input.exists() and cache_prompt.exists() and cache_parsed.exists():
                try:
                    metadata_file = cache_dir / "metadata.json"
                    cached_metadata = json.loads(metadata_file.read_text(encoding="utf-8")) if metadata_file.exists() else {}
                    # 验证缓存与当前输入一致
                    if (
                        digest(json.loads(cache_input.read_text(encoding="utf-8"))) == digest(context)
                        and cache_prompt.read_text(encoding="utf-8") == prompt
                        and cached_metadata.get("model_identity") == model_identity
                        and cached_metadata.get("cache_key") == cache_key
                    ):
                        value = json.loads(cache_parsed.read_text(encoding="utf-8"))
                        raw = cache_raw.read_text(encoding="utf-8") if cache_raw.exists() else json.dumps(value, ensure_ascii=False)
                        model_meta = cached_metadata
                        recovered = True
                except (OSError, json.JSONDecodeError, TypeError):
                    recovered = False
            if not recovered:
                # 缓存恢复失败，调用模型生成新响应
                raw, model_meta = client.complete(prompt, context, max_tokens=STAGE_MAX_TOKENS[stage],
                                                   response_format=JSON_RESPONSE_FORMAT)
                try:
                    value = parse_json_text(raw, PREFERRED_JSON_KEYS.get(stage))
                except Exception as exc:
                    value = {"_parse_error": str(exc)}
            # 应用代码拥有的归一化
            value = _prepare_stage_value(stage, value, context)
            # 验证生成的 artifact
            errors = validate_stage(stage, value, context)
            # ----- 审计修复路由分析 -----
            audit_targets: list[str] = []
            if stage == "environment_audit" and isinstance(value, dict):
                audit_targets = _audit_repair_targets(value, context)
                if value.get("findings") and not audit_targets:
                    # 存在 findings 但无有效修复路由，这是验证错误
                    errors.append(
                        "environment_audit findings require explicit, existing stage/path routes"
                    )
            if audit_targets:
                # 语义 findings 是终审评估器输出。不让结构修复 prompt
                # 重写或重新解释它们。
                repairs = []
                errors = [
                    "environment_audit rejected the package"
                    if value.get("release_decision") == "reject"
                    else "environment_audit findings require targeted upstream repair"
                ]
            else:
                # ----- 结构修复（第一层修复） -----
                value, errors, repairs = _run_local_repairs(
                    stage, value, errors, context, client, case_dir / "stage_cache" / stage
                )
                # 结构修复后重新检查审计修复路由
                if stage == "environment_audit" and isinstance(value, dict):
                    audit_targets = _audit_repair_targets(value, context)
                    if audit_targets:
                        errors = [
                            "environment_audit rejected the package"
                            if value.get("release_decision") == "reject"
                            else "environment_audit findings require targeted upstream repair"
                        ]
                    elif not errors and (
                        value.get("release_decision") != "pass"
                        or not _audit_release_formula_passes(value)
                    ):
                        # 无 findings 但不满足发布公式
                        errors = ["environment_audit does not satisfy the release formula"]
            if errors:
                # ----- 错误处理：语义修复或硬失败 -----
                if (
                    stage == "environment_audit"
                    and audit_targets
                    and value.get("release_decision") != "reject"
                    and _semantic_repair_count(case_dir) < MAX_SEMANTIC_REPAIR_PASSES
                ):
                    # ----- 语义修复（第二层修复） -----
                    # 审计发现指向可修复的上游阶段，且未超过修复轮次限制
                    (
                        target_stage,
                        repaired_target,
                        semantic_raw,
                        semantic_meta,
                        target_errors,
                        target_context,
                    ) = _repair_semantic_target(
                        case_id,
                        scene,
                        value,
                        context,
                        client,
                    )
                    # 对语义修复结果再进行结构修复
                    repaired_target, target_errors, target_repairs = _run_local_repairs(
                        target_stage,
                        repaired_target,
                        target_errors,
                        target_context,
                        client,
                        case_dir / "stage_cache" / target_stage / "semantic_repair",
                    )
                    if not target_errors:
                        # 语义修复成功：确定最终选中的原始响应
                        selected_raw = semantic_raw
                        for repair in target_repairs:
                            if repair.get("status") == "completed" and repair.get("selected"):
                                repair_path = (
                                    case_dir
                                    / "stage_cache"
                                    / target_stage
                                    / "semantic_repair"
                                    / f"repair_{repair.get('attempt')}.raw_response.txt"
                                )
                                if repair_path.exists():
                                    selected_raw = repair_path.read_text(encoding="utf-8")
                        # 发布修复后的 artifact
                        _publish_semantic_repair(
                            case_dir,
                            target_stage,
                            repaired_target,
                            selected_raw,
                            semantic_meta,
                            value["findings"],
                            client,
                            target_repairs,
                        )
                        # 使修复阶段的下游 artifact 失效
                        _remove_downstream_after_semantic_repair(
                            case_dir,
                            target_stage,
                        )
                        # 递归重跑管线：修复后的上游会级联影响下游
                        return run_case(
                            case_id,
                            scene,
                            run_dir,
                            client,
                            True,
                            repair_depth + 1,
                        )
                    # 语义修复失败（结构验证未通过）：记录失败历史
                    _append_semantic_repair_history(case_dir, {
                        "target_stage": target_stage,
                        "status": "failed_structure",
                        "finding_ids": [
                            finding.get("finding_id")
                            for finding in value.get("findings", [])
                            if isinstance(finding, dict) and finding.get("stage") == target_stage
                        ],
                        "findings": [
                            copy.deepcopy(finding)
                            for finding in value.get("findings", [])
                            if isinstance(finding, dict) and finding.get("stage") == target_stage
                        ],
                        "structural_errors": list(target_errors),
                        **_provider_metadata(semantic_meta),
                    })
                    errors = [
                        f"semantic repair for {target_stage} failed structure: {error}"
                        for error in target_errors
                    ]
                elif (
                    stage == "environment_audit"
                    and audit_targets
                    and value.get("release_decision") != "reject"
                    and _semantic_repair_count(case_dir) >= MAX_SEMANTIC_REPAIR_PASSES
                ):
                    # 语义修复轮次已达上限（第三层：硬失败）
                    errors = [
                        f"environment_audit semantic repair limit reached "
                        f"({MAX_SEMANTIC_REPAIR_PASSES})"
                    ]
                # ----- 硬失败处理：级联删除下游 artifact -----
                # 上游阶段失败会使所有下游 artifact 失效。
                # 在记录失败前删除它们，防止旧 audit 或 rubric 被误认为当前包。
                # 同时删除失败阶段的已发布兄弟文件，否则之前成功的 JSON 可能
                # 留在失败的验证报告旁边，看起来像可用的 artifact。
                for suffix in (".json", ".meta.json", ".raw_response.txt"):
                    stale = case_dir / f"{stage}{suffix}"
                    if stale.exists():
                        stale.unlink()
                if stage in ARTIFACT_STAGES:
                    start = ARTIFACT_STAGES.index(stage) + 1
                    for downstream in ARTIFACT_STAGES[start:]:
                        for suffix in (".json", ".meta.json", ".raw_response.txt"):
                            stale = case_dir / f"{downstream}{suffix}"
                            if stale.exists():
                                stale.unlink()
                validation_report[stage] = {"status": "failed", "errors": errors, "repair_attempts": repairs}
                # 保存失败状态到 stage_cache，供断点续跑复用
                cache_dir = case_dir / "stage_cache" / stage
                _write_json_atomic(cache_dir / "input.json", context)
                _write_text_atomic(cache_dir / "prompt.md", prompt)
                _write_text_atomic(cache_dir / "raw_response.txt", _redact_secret(raw, client))
                _write_json_atomic(cache_dir / "parsed_output.json", value)
                _write_json_atomic(cache_dir / "validation.json", validation_report[stage])
                _write_json_atomic(cache_dir / "metadata.json", {
                    "cache_key": cache_key,
                    "prompt_hash": digest(prompt),
                    "input_hash": digest(context),
                    "model_identity": model_identity,
                    **model_meta,
                    "repair_attempts": repairs,
                    "validation_status": "failed",
                })
                _write_json_atomic(case_dir / "validation.json", validation_report)
                raise RuntimeError(f"{case_id}/{stage} failed validation: {'; '.join(errors)}")
            # ----- 阶段通过：发布 artifact -----
            validation_report[stage] = {"status": "passed", "errors": [], "repair_attempts": repairs}
            artifact_source = _artifact_source_metadata(model_meta, repairs)
            cache_meta = {
                "cache_key": cache_key,
                "prompt_hash": digest(prompt),
                "input_hash": digest(context),
                "model_identity": model_identity,
                "initial_response": _provider_metadata(model_meta),
                "artifact_source": artifact_source,
                **_provider_metadata(artifact_source),
                "repair_attempts": repairs,
            }
            # 原子写入 artifact、元数据和原始响应
            _write_json_atomic(out_path, value)
            _write_json_atomic(meta_path, cache_meta)
            _write_text_atomic(case_dir / f"{stage}.raw_response.txt", _redact_secret(raw, client))
            # 同时保存到 stage_cache，供断点续跑复用
            cache_dir = case_dir / "stage_cache" / stage
            _write_json_atomic(cache_dir / "input.json", context)
            _write_text_atomic(cache_dir / "prompt.md", prompt)
            _write_text_atomic(cache_dir / "raw_response.txt", _redact_secret(raw, client))
            _write_json_atomic(cache_dir / "parsed_output.json", value)
            _write_json_atomic(cache_dir / "validation.json", validation_report[stage])
            _write_json_atomic(cache_dir / "metadata.json", cache_meta)
        # 将当前阶段的值加入 prior，供下游阶段使用
        prior[stage] = value
        # ----- user_voice 派生处理 -----
        # scene_task 的组合响应包含 user_seed 和 voice_profile，
        # 需要拆分为独立的 user_voice artifact
        if stage == "scene_task" and isinstance(value, dict) and isinstance(value.get("user_seed"), dict) and isinstance(value.get("voice_profile"), str):
            combined_user_voice = {
                "user_seed": value.pop("user_seed"),
                "voice_profile": value.pop("voice_profile"),
            }
            # 构建 user_voice 的验证上下文
            uv_context = stage_context(case_id, "user_voice", scene, {"scene_task": value})
            uv_errors = validate_stage("user_voice", combined_user_voice, uv_context)
            # 对 user_voice 也进行结构修复
            combined_user_voice, uv_errors, uv_repairs = _run_local_repairs(
                "user_voice",
                combined_user_voice,
                uv_errors,
                uv_context,
                client,
                case_dir / "stage_cache" / "user_voice",
            )
            if uv_errors:
                validation_report["user_voice"] = {"status": "failed", "errors": uv_errors, "repair_attempts": uv_repairs}
                # 派生修复失败时，绝不将临时的组合 Stage-1 响应
                # 留作已发布的 scene_task artifact
                for stale in (derived_user_voice_path, case_dir / "user_voice.meta.json"):
                    if stale.exists():
                        stale.unlink()
                # 重写 scene_task（已移除 user_seed/voice_profile）
                _write_json_atomic(out_path, value)
                _write_json_atomic(case_dir / "validation.json", validation_report)
                raise RuntimeError(f"{case_id}/user_voice derived artifact failed validation: {'; '.join(uv_errors)}")
            # 派生成功：写入 user_voice artifact 和元数据
            uv_artifact_source = _artifact_source_metadata(cache_meta, uv_repairs)
            uv_meta = {
                "derived_from": "scene_task",
                "source_hash": digest(value),
                "prompt_hash": digest(prompt),
                "input_hash": digest(context),
                "model_identity": model_identity,
                "artifact_source": uv_artifact_source,
                **_provider_metadata(uv_artifact_source),
                "repair_attempts": uv_repairs,
            }
            _write_json_atomic(case_dir / "user_voice.json", combined_user_voice)
            _write_json_atomic(case_dir / "user_voice.meta.json", uv_meta)
            prior["user_voice"] = combined_user_voice
            validation_report["user_voice"] = {"status": "derived", "errors": [], "repair_attempts": uv_repairs}
            # 移除派生兄弟字段后重写 scene_task
            _write_json_atomic(out_path, value)
    # ===== 最终完整性检查 =====
    # 确保所有 artifact 阶段都有对应的 JSON 文件
    missing_artifacts = [stage for stage in ARTIFACT_STAGES if not (case_dir / f"{stage}.json").exists()]
    if missing_artifacts:
        raise RuntimeError(f"{case_id} missing final artifacts: {', '.join(missing_artifacts)}")
    _write_json_atomic(case_dir / "validation.json", validation_report)
    return prior


def _extract_stage_dispatch(argv: list[str]) -> tuple[str | None, list[str]]:
    """Extract the optional top-level stage selector without consuming stage args.

    Stage 1 has its own (larger) CLI in ``step_01_generate_scene_task_user_voice.py``.
    Keeping the selector handling here deliberately tiny lets the Stage-1
    parser own all of its batch/resume options while preserving the legacy
    full-pipeline CLI unchanged.
    """
    stage: str | None = None
    forwarded: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--stage1":
            if stage is not None and stage != "stage1":
                raise ValueError("conflicting stage selectors")
            stage = "stage1"
            index += 1
            continue
        if token == "--stage":
            if index + 1 >= len(argv):
                raise ValueError("--stage requires a value")
            selected = argv[index + 1]
            if selected not in {"full", "stage1"}:
                raise ValueError("--stage must be full or stage1")
            if stage is not None and stage != selected:
                raise ValueError("conflicting stage selectors")
            stage = selected
            index += 2
            continue
        if token.startswith("--stage="):
            selected = token.split("=", 1)[1]
            if selected not in {"full", "stage1"}:
                raise ValueError("--stage must be full or stage1")
            if stage is not None and stage != selected:
                raise ValueError("conflicting stage selectors")
            stage = selected
            index += 1
            continue
        forwarded.append(token)
        index += 1
    return stage, forwarded


def main() -> None:
    # ``run_pipeline.py --stage stage1`` is a compatibility entry point for
    # the current batch Stage-1 generator.  The delegated parser receives the
    # original Stage-1 arguments intact (including --resume, --count,
    # --samples-per-case, --scenario-ids, and --all-task-styles).
    try:
        selected_stage, forwarded_args = _extract_stage_dispatch(sys.argv[1:])
    except ValueError as exc:
        raise SystemExit(f"run_pipeline.py: {exc}") from exc
    if selected_stage == "stage1":
        from step_01_generate_scene_task_user_voice import main as stage1_main

        original_argv = sys.argv
        try:
            sys.argv = [original_argv[0], *forwarded_args]
            stage1_main()
        finally:
            sys.argv = original_argv
        return

    # 管线入口函数：解析参数、加载分类体系、并行执行各 case
    # 设计理由：
    #   - --run-id: 运行标识，默认为时间戳，用于组织输出目录
    #   - --resume: 断点续跑模式，复用缓存的有效 artifact
    #   - --workers: 并行 worker 数，控制并发度
    #   - --cases: 指定处理的 case ID 子集，默认处理所有固定 case
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--cases", help="comma-separated case IDs to process; default is all fixed cases")
    # ``--stage full`` has already been removed by the dispatcher.  The
    # default path therefore remains byte-for-byte compatible with the legacy
    # fixed-case audit pipeline.
    args = parser.parse_args(forwarded_args if selected_stage == "full" else None)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    # 从环境变量初始化模型客户端
    client = MiniMaxClient.from_env()
    # 加载分类体系和 case 种子
    taxonomy = json.loads((ROOT / "taxonomy/domain-scenario-with-task-examples-gpt56-sol-v5.json").read_text(encoding="utf-8"))
    seeds = json.loads((ROOT / "cases/case-seeds.json").read_text(encoding="utf-8"))
    run_dir = ROOT / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # 过滤指定的 case ID
    selected_cases = {item.strip() for item in args.cases.split(",")} if args.cases else None
    unknown_cases = (selected_cases - set(seeds)) if selected_cases is not None else set()
    if unknown_cases:
        parser.error(f"unknown case IDs: {', '.join(sorted(unknown_cases))}")
    # 采样场景：为每个 case 生成确定性的场景数据
    scenes = []
    for case_id, spec in seeds.items():
        if selected_cases is not None and case_id not in selected_cases:
            continue
        scene = sample_scene(taxonomy, spec["seed"], spec["domain_id"], spec["scenario_id"], "fixed_audit_case")
        scene["case_id"] = case_id
        scene["task_brief"] = spec.get("task_brief", "")
        scenes.append((case_id, scene))
    # 加载或初始化运行清单
    manifest_path = run_dir / "manifest.json"
    previous_manifest = {}
    if args.resume and manifest_path.exists():
        try:
            previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_manifest = {}
    previous_status = previous_manifest.get("case_status", {})
    if not isinstance(previous_status, dict):
        previous_status = {}
    if selected_cases is not None:
        # 仅保留指定 case 的历史状态
        previous_status = {
            case_id: status
            for case_id, status in previous_status.items()
            if case_id in selected_cases
        }
    manifest = {
        "run_id": args.run_id,
        "model": client.model,
        "model_policy": {
            "primary": {"provider": client.provider, "model": client.model},
            "network_fallback": (
                {"provider": "glm-anthropic", "model": client.glm_model}
                if client.fallback_enabled and client.glm_api_key
                else None
            ),
            "actual_model_recorded_per_stage": True,
        },
        "workers": args.workers,
        "taxonomy_version": taxonomy["taxonomy_version"],
        "taxonomy_sha256": digest(taxonomy),
        # 清单描述本次调用的冻结范围。将之前全量 case 列表复用于
        # ``--cases P01`` 会使输出无法作为聚焦运行验证，
        # 且在种子清单变更后可能暴露过时的场景元数据。
        "cases": scenes_to_manifest(scenes),
        "rollout_generated": False,
        "api_key_serialized": False,
        "case_status": previous_status,
    }
    _write_json_atomic(run_dir / "manifest.json", manifest)
    # ===== 并行执行各 case =====
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {executor.submit(run_case, case_id, scene, run_dir, client, args.resume): case_id for case_id, scene in scenes}
        for job in as_completed(jobs):
            case_id = jobs[job]
            try:
                result = job.result()
                manifest["case_status"][case_id] = {"status": "passed", "release_decision": result["environment_audit"].get("release_decision")}
            except Exception as exc:
                safe_error = _redact_secret(str(exc), client)
                failures.append(f"{case_id}: {safe_error}")
                manifest["case_status"][case_id] = {"status": "failed", "error": safe_error}
    # 更新清单中的 case 状态
    _write_json_atomic(run_dir / "manifest.json", manifest)
    if failures:
        print("PIPELINE FAILED")
        print("\n".join(sorted(failures)))
        raise SystemExit(1)
    # 全部通过，打印运行目录
    print(run_dir)


def scenes_to_manifest(scenes: list[tuple[str, dict]]) -> list[dict]:
    # 将场景列表转换为清单格式（仅保留场景数据，不含 case_id）
    return [scene for _, scene in scenes]


if __name__ == "__main__":
    main()
