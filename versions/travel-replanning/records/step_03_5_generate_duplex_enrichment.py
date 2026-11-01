#!/usr/bin/env python3
"""Stage 3.5 enrichment with bounded model repair and unchanged Judge rules.

Failed Stage-3 sources are never repaired here. An admitted source receives
one enrichment generation and at most two finding-directed model patches.
All candidates and reviews remain available; exhausted failures are never
admitted downstream. A saved rejection can continue in a separate new run.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import time

# MiniMaxClient：API 调用客户端，支持 MiniMax 和 GPT 兼容端点
# parse_json_text：从模型原始文本中提取 JSON 并校验必需字段
from minimax_client import MiniMaxClient, parse_json_text
# _base_context：构建 Stage 3 基础上下文（scene_task/user_voice/situation_knowledge）
# _source_cases：从 manifest.json 加载源用例列表
# _normalize_timing_shape：规范化 timing_refs 的结构（如 T01→[T01]）
from step_03_generate_episode_script import _base_context, _source_cases, _normalize_timing_shape
# JSON_RESPONSE_FORMAT：强制模型输出 JSON 的 response_format 参数
# STAGE_MAX_TOKENS：各阶段各产物的最大 token 数配置
# _prepare_stage_value：对候选输出做通用后处理（如字段清洗）
# _redact_secret：从日志文本中移除 API 密钥等敏感信息
# _write_json_atomic / _write_text_atomic：原子写入 JSON/文本文件，避免半写状态
# digest：计算对象的 SHA-256 摘要，用于数据指纹和去重
from run_pipeline import (
    JSON_RESPONSE_FORMAT,
    STAGE_MAX_TOKENS,
    _prepare_stage_value,
    _redact_secret,
    _write_json_atomic,
    _write_text_atomic,
    digest,
)
# validate_stage：结构验证函数，检查剧本是否符合 schema 约束
from stage_validation import validate_stage
from duplex_runtime.backchannel_policy import planning_policy
from duplex_enrichment_repair import apply_model_patches


# 项目根目录，用于定位 prompts 等相对路径资源
ROOT = Path(__file__).parent
# 增强输出必须包含的字段：仅 episode_turns（增强后的轮次列表）
# 模型只需输出修改过的轮次，合并逻辑会补全缺失轮次
ENRICHMENT_KEYS = {"episode_turns"}
# Judge 输出必须包含的字段：决策、必须修复项、警告、双工摘要
# must_fix 非空时 decision 必须为 repair；warnings 是软性提示不影响通过
JUDGE_KEYS = {"decision", "must_fix", "warnings", "duplex_summary"}
# 一次增强生成 + 最多两次模型定向修复；有效候选均交回原 Judge。
MAX_ATTEMPTS = 3
# Judge 响应的最大 token 数，双工摘要需要额外空间
# 比普通 Judge 多 ~400 token，因为 duplex_summary 包含 6 个计数字段
JUDGE_MAX_TOKENS = 2400


def _load_json(path: Path, default: object) -> object:
    """安全加载 JSON 文件，文件不存在或解析失败时返回默认值。

    设计理由：Stage 3.5 需要读取 Stage 3 的产物文件（validation.json、
    episode_script.json 等），但这些文件可能因 Stage 3 运行不完整而缺失。
    使用默认值而非抛异常，使流程能继续并标记为 skipped。

    Args:
        path: JSON 文件路径
        default: 文件不存在或解析失败时的默认返回值
    Returns:
        解析后的 Python 对象，或 default
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # JSON 格式损坏时也返回默认值，避免因单个文件问题中断整个批处理
        return default


def _judge_errors(value: object) -> list[str]:
    """Validate the structural shape of a Stage-3.5 Judge response.

    Unlike Stage-2/3 Judges which use a flat pass/repair + issues list,
    the Stage-3.5 Judge uses a two-tier must_fix + warnings structure and
    additionally requires a duplex_summary that counts each duplex event type.
    """
    # 校验 Stage 3.5 Judge 输出的结构合法性。
    #
    # 与 Stage 2/3 的 Judge 不同，Stage 3.5 Judge 使用 must_fix + warnings 双层结构，
    # 并额外要求 duplex_summary 统计各双工事件数量。
    #
    # 校验层次：
    # 1. 顶层必须是 dict，且恰好包含 4 个字段
    # 2. decision 只能是 pass 或 repair（fail-closed 由重试上限与失败状态表达）
    # 3. must_fix/warnings 必须是数组
    # 4. must_fix 中每项必须包含 path/problem/evidence 三元组（与 Stage 2/3 一致）
    # 5. duplex_summary 中 6 个计数必须为非负整数
    # 6. 逻辑一致性：pass 时 must_fix 必须为空，repair 时 must_fix 不能为空
    #
    # Args:
    #     value: Judge 的原始输出对象
    # Returns:
    #     错误列表，空列表表示校验通过
    if not isinstance(value, dict):
        return ["judge output must be an object"]
    errors: list[str] = []
    # 顶层字段必须恰好是 4 个，多或少都视为结构错误
    if set(value) != JUDGE_KEYS:
        errors.append("judge output must contain exactly decision, must_fix, warnings, duplex_summary")
    # decision 只允许 pass 或 repair，不允许 fail（修复失败由外层明确记录）
    if value.get("decision") not in {"pass", "repair"}:
        errors.append("judge decision must be pass or repair")
    for field in ("must_fix", "warnings"):
        if not isinstance(value.get(field), list):
            errors.append(f"judge {field} must be an array")
    # 校验 must_fix 中每项必须包含 path/problem/evidence 三元组
    # 这与 Stage 2/3 的 Judge 格式一致，便于后续统一处理
    must_fix = value.get("must_fix", [])
    if isinstance(must_fix, list):
        for index, item in enumerate(must_fix):
            if not isinstance(item, dict) or set(item) != {"path", "problem", "evidence"}:
                errors.append(f"must_fix[{index}] must contain path, problem, evidence")
    # 校验 duplex_summary 中各双工事件计数必须为非负整数
    # 这些计数用于质量监控和统计报告，必须确保数值合法
    summary = value.get("duplex_summary")
    if not isinstance(summary, dict):
        errors.append("duplex_summary must be an object")
    else:
        for key in ("t01_count", "t02_count", "t03_count", "a02_count", "side_speech_count", "natural_opportunities_used"):
            if not isinstance(summary.get(key), int) or summary[key] < 0:
                errors.append(f"duplex_summary.{key} must be a non-negative integer")
    # 逻辑一致性校验：pass 时 must_fix 必须为空，repair 时 must_fix 不能为空
    # 这防止模型输出自相矛盾的 Judge 结果
    if value.get("decision") == "pass" and must_fix:
        errors.append("judge pass must have an empty must_fix array")
    if value.get("decision") == "repair" and not must_fix:
        errors.append("judge repair must contain at least one must_fix")
    return errors


def _count_summary(script: dict) -> dict:
    """Count duplex event types in an enriched script for the Judge's duplex_summary.

    Walks episode_turns and tallies T01 (user starts speaking), T02 (user
    hesitation / pause), T03 (user continues), A02 (assistant interruption),
    and side_speech events.  The natural_opportunities_used counter is left
    at zero for the Judge to fill in.
    """
    # 统计剧本中各双工事件类型的数量。
    #
    # 遍历 episode_turns，统计以下事件：
    # - T01：用户开始说话（turn onset），通常每个用户轮次都有一个
    # - T02：用户停顿/犹豫，是双工增强的核心注入目标
    # - T03：用户继续说话（T02 之后恢复），与 T02 配对出现
    # - A02：助手打断用户，另一个双工增强目标
    # - side_speech：旁白语音事件（如环境音、第三方说话），按概率采样
    #
    # natural_opportunities_used 固定为 0，由 Judge 根据语义评估填充。
    # 这是因为"自然机会"是主观判断，需要 Judge 评估双工事件是否自然。
    #
    # Args:
    #     script: 包含 episode_turns 的剧本字典
    # Returns:
    #     各事件计数字典，natural_opportunities_used 固定为 0（由 Judge 填充）
    turns = script.get("episode_turns", []) if isinstance(script, dict) else []
    # 统计 side_speech 事件数：每个轮次的 side_speech 是一个列表，累加其长度
    side_speech_count = sum(
        len(turn.get("side_speech", []))
        for turn in turns
        if isinstance(turn, dict) and isinstance(turn.get("side_speech", []), list)
    )
    return {
        # T01/T02/T03 通过 timing_refs 列表判断，一个轮次可能同时有多个 timing 标记
        "t01_count": sum("T01" in (t.get("timing_refs") or []) for t in turns if isinstance(t, dict)),
        "t02_count": sum("T02" in (t.get("timing_refs") or []) for t in turns if isinstance(t, dict)),
        "t03_count": sum("T03" in (t.get("timing_refs") or []) for t in turns if isinstance(t, dict)),
        # A02 通过 act_ref 字段判断，与 timing_refs 不同，act_ref 是单值
        "a02_count": sum(t.get("act_ref") == "A02" for t in turns if isinstance(t, dict)),
        "side_speech_count": side_speech_count,
        # 由 Judge 填充：评估有多少双工事件是"自然"的而非生硬插入
        "natural_opportunities_used": 0,
    }


def _merge_candidate_with_original(candidate: dict, original: dict) -> dict:
    """Recover unchanged Stage-3 fields from the frozen script.

    Models occasionally emit only edited fields or serialize a side-speech
    object as an orphan list item. The released script must remain a complete
    ordered episode, so normalize those local omissions before validation.
    """
    # 将增强候选与原始 Stage 3 剧本合并，确保输出是完整的有序轮次列表。
    #
    # 设计理由：模型有时只输出修改过的轮次，或将 side_speech 序列化为孤立的列表项。
    # 合并策略：
    # 1. 按 turn_id 索引候选轮次
    # 2. 将无 turn_id 的孤立 side_speech 项归并到前一个有 ID 的轮次
    # 3. 以原始轮次顺序为骨架，用候选中匹配的轮次覆盖对应字段
    #
    # 为什么不直接使用候选输出？
    # - 模型可能省略未修改的轮次，导致轮次列表不完整
    # - 模型可能将 side_speech 作为独立列表项输出（而非嵌套在轮次内）
    # - 原始轮次顺序是经过 Stage 3 验证的，必须保留
    #
    # Args:
    #     candidate: 模型生成的增强候选（可能不完整）
    #     original: 原始 Stage 3 剧本（完整且已验证）
    # Returns:
    #     合并后的完整剧本，保留原始轮次顺序
    raw_turns = candidate.get("episode_turns") if isinstance(candidate, dict) else None
    original_turns = original.get("episode_turns", []) if isinstance(original, dict) else []
    # 如果候选或原始的轮次不是列表，无法合并，直接返回候选
    if not isinstance(raw_turns, list) or not isinstance(original_turns, list):
        return candidate
    # 按 turn_id 建立候选轮次索引
    by_id: dict[str, dict] = {}
    # 孤立 side_speech 项：没有 turn_id 的 side_speech 字典，需归并到前一个轮次
    orphan_events: list[tuple[str | None, dict]] = []
    # 追踪最近一个有 turn_id 的轮次，用于归并孤立的 side_speech
    last_named_id: str | None = None
    for item in raw_turns:
        if not isinstance(item, dict):
            continue
        turn_id = item.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            # 正常轮次：按 turn_id 索引
            by_id[turn_id] = copy.deepcopy(item)
            last_named_id = turn_id
        elif set(item).issubset({"side_speech"}) and item:
            # 模型有时将 side_speech 作为独立列表项输出，需归并到所属轮次
            # 例如：模型输出 [..., {"turn_id": "R01", ...}, {"side_speech": [...]}, ...]
            # 此时 {"side_speech": [...]} 应归并到 R01
            orphan_events.append((last_named_id, item))
    # 将孤立 side_speech 合并到目标轮次
    for target_id, orphan in orphan_events:
        target = by_id.get(target_id or "")
        if not isinstance(target, dict):
            continue
        if "side_speech" in orphan:
            target.setdefault("side_speech", [])
            if not isinstance(target["side_speech"], list):
                target["side_speech"] = [target["side_speech"]] if isinstance(target["side_speech"], dict) else []
            orphan_ss = orphan["side_speech"]
            if isinstance(orphan_ss, list):
                target["side_speech"].extend(copy.deepcopy(orphan_ss))
            elif isinstance(orphan_ss, dict):
                target["side_speech"].append(copy.deepcopy(orphan_ss))
    # 以原始轮次顺序为骨架，用候选中匹配的轮次覆盖
    # 这确保了：1) 轮次顺序与 Stage 3 一致  2) 未修改的轮次保留原始值  3) 修改的轮次使用候选值
    merged_turns: list[dict] = []
    for original_turn in original_turns:
        if not isinstance(original_turn, dict):
            continue
        turn_id = str(original_turn.get("turn_id"))
        # 先深拷贝原始轮次作为基础
        merged = copy.deepcopy(original_turn)
        if turn_id in by_id:
            # 候选中存在该轮次的修改，用候选值覆盖原始值
            # update 是浅合并，候选中的新字段会添加，已有字段会覆盖
            merged.update(copy.deepcopy(by_id[turn_id]))
        merged_turns.append(merged)
    return {"episode_turns": merged_turns}


def _normalize_side_speech_shape(candidate: dict, context: dict) -> list[str]:
    """Normalize local side_speech shape before structural validation.

    Doubao often emits a single side_speech object instead of a one-item array,
    or still attaches events when the case policy disabled side speech. Wrap or
    drop those local shape mistakes so an otherwise valid enrichment is kept.
    """
    changes: list[str] = []
    turns = candidate.get("episode_turns") if isinstance(candidate, dict) else None
    if not isinstance(turns, list):
        return changes
    policy = context.get("side_speech_policy") if isinstance(context, dict) else None
    disabled = isinstance(policy, dict) and policy.get("enabled") is False
    for turn in turns:
        if not isinstance(turn, dict) or "side_speech" not in turn:
            continue
        if disabled:
            turn.pop("side_speech", None)
            changes.append(f"{turn.get('turn_id', '?')}:dropped side_speech for disabled policy")
            continue
        side_speech = turn.get("side_speech")
        if isinstance(side_speech, dict):
            turn["side_speech"] = [side_speech]
            changes.append(f"{turn.get('turn_id', '?')}:wrapped side_speech object into array")
        elif side_speech is None:
            turn.pop("side_speech", None)
            changes.append(f"{turn.get('turn_id', '?')}:removed null side_speech")
    return changes


def _drop_incomplete_t02(candidate: dict, original: dict) -> list[str]:
    """Discard an optional model-added hesitation when its cue is unusable.

    Stage 3.5 can add side speech even when a speculative T02 is malformed.
    Keeping the existing T01/T03 onset is deterministic and avoids rejecting
    the complete enrichment for an event the model did not describe fully.
    """
    # 丢弃不完整的 T02（用户犹豫/停顿）事件，回退到原始 timing。
    #
    # 设计理由：模型可能添加了 T02 标记但 pause_cue 描述不完整（如 after_text
    # 在台词中不存在、duration_ms 为 0 等）。此时保留不完整的 T02 会导致
    # 运行时错误，而回退到原始 T01/T03 是安全的确定性操作，不会因一个
    # 局部缺陷而拒绝整个增强结果。
    #
    # 为什么不直接拒绝整个候选？
    # - T02 是增强添加的可选事件，原始剧本没有 T02 也能正常运行
    # - 其他增强（如 A02、side_speech）可能完全正确，不应因 T02 问题被连带拒绝
    # - 回退到原始 timing 是确定性操作，不会引入新的不确定性
    #
    # pause_cue 完整性条件：
    # - after_text 必须是非空字符串，且在 user_utterance 中恰好出现一次
    #   （出现 0 次说明模型幻觉，出现 >1 次说明定位不唯一）
    # - duration_ms 必须是正整数（0 或负数无意义）
    #
    # Args:
    #     candidate: 增强后的候选剧本
    #     original: 原始 Stage 3 剧本
    # Returns:
    #     修改记录列表，每项描述一个回退操作
    changes: list[str] = []
    turns = candidate.get("episode_turns", []) if isinstance(candidate, dict) else []
    # 建立原始轮次的 turn_id 索引，用于回退
    original_by_id = {
        turn.get("turn_id"): turn
        for turn in original.get("episode_turns", [])
        if isinstance(turn, dict) and isinstance(turn.get("turn_id"), str)
    } if isinstance(original, dict) else {}
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        timings = turn.get("timing_refs")
        # 只处理包含 T02 标记的轮次
        if not isinstance(timings, list) or "T02" not in timings:
            continue
        # 校验 pause_cue 的完整性：after_text 必须在台词中恰好出现一次，duration_ms > 0
        cue = turn.get("pause_cue")
        utterance = turn.get("user_utterance")
        valid_cue = (
            isinstance(cue, dict)
            and isinstance(cue.get("after_text"), str)
            and cue["after_text"]
            and isinstance(cue.get("duration_ms"), int)
            and cue["duration_ms"] > 0
            and isinstance(utterance, str)
            # after_text 在台词中必须恰好出现一次：0 次说明模型幻觉，>1 次说明定位不唯一
            and utterance.count(cue["after_text"]) == 1
        )
        if not valid_cue:
            # pause_cue 不完整，回退到原始 timing_refs 和 pause_cue
            # 这确保了：1) T02 被移除  2) 原始的 T01/T03 被恢复  3) 无效的 pause_cue 被清除
            original_turn = original_by_id.get(turn.get("turn_id"))
            if isinstance(original_turn, dict):
                turn["timing_refs"] = copy.deepcopy(original_turn.get("timing_refs", []))
                if "pause_cue" in original_turn:
                    turn["pause_cue"] = copy.deepcopy(original_turn["pause_cue"])
                else:
                    # 原始轮次没有 pause_cue，移除候选中无效的 pause_cue
                    turn.pop("pause_cue", None)
                changes.append(f"{turn.get('turn_id', '?')}: restored frozen timing after incomplete T02")
    return changes


def _context_for_enrichment(source: dict, case_id: str, original: dict, source_validation: dict) -> dict:
    """Build the context dict for the Stage-3.5 enrichment prompt.

    Extends the Stage-3 base context with the original script, its validation
    result, and the side-speech policy so the enrichment model knows the
    existing structure and the duplex injection strategy.
    """
    # 构建 Stage 3.5 增强的上下文输入。
    #
    # 在 Stage 3 基础上下文之上，附加三个额外字段：
    # 1. original_episode_script：原始剧本，让模型知道已有结构并在此基础上增强
    # 2. stage3_validation：Stage 3 验证结果，让模型了解原始剧本的质量状态
    # 3. side_speech_policy：旁白语音策略，控制 side_speech 的采样概率和事件数量
    #
    # 设计理由：
    # - 提供原始剧本使模型能精确修改而非从头生成，减少遗漏和幻觉
    # - 提供验证结果使模型知道哪些方面已经合格，避免不必要的修改
    # - side_speech_policy 按概率采样，避免所有剧本都出现旁白（影响多样性）
    #
    # Args:
    #     source: 包含 scene_task/user_voice/situation_knowledge 的源数据
    #     case_id: 用例 ID
    #     original: 原始 Stage 3 剧本
    #     source_validation: Stage 3 的验证结果
    # Returns:
    #     增强上下文字典
    context = _base_context(source, case_id)
    return {
        **context,
        # 原始剧本：模型需要在此基础上添加双工事件，而非重新生成
        "original_episode_script": copy.deepcopy(original),
        # Stage 3 验证结果：让模型了解原始剧本已通过哪些检查
        "stage3_validation": copy.deepcopy(source_validation),
        "backchannel_policy": copy.deepcopy(source.get("backchannel_policy", {"enabled": False, "min_events": 0, "max_events": 0, "max_events_per_turn": 2})),
        # 旁白语音策略：控制 side_speech 的采样概率和事件数量
        # - mode: sampled（按概率采样）/ all（全部启用）/ off（全部关闭）
        # - enabled: 当前用例是否启用 side_speech
        # - case_probability: 启用概率（sampled 模式下由哈希决定实际是否启用）
        # - min_events/max_events: 启用时 side_speech 事件的数量范围
        "side_speech_policy": copy.deepcopy(source.get("side_speech_policy", {
            "mode": "sampled",
            "enabled": True,
            "case_probability": 0.25,
            "min_events": 1,
            "max_events": 2,
        })),
    }


def _judge_input(context: dict, original: dict, candidate: dict) -> dict:
    """Assemble the input dict for the Stage-3.5 Judge prompt.

    The Judge needs the full context, the original Stage-3 script (as the
    baseline), and the enriched candidate (to evaluate).  All dicts are
    deep-copied so the Judge input is an immutable snapshot.
    """
    # 构建 Judge 审查的输入，包含上下文、原始剧本和增强候选。
    #
    # Judge 需要同时看到原始剧本和增强候选，才能评估：
    # 1. 增强是否保留了原始剧本的核心内容（不能丢失或篡改已有轮次）
    # 2. 新增的双工事件是否自然合理
    # 3. side_speech 是否与场景匹配
    #
    # 所有字典都深拷贝，确保 Judge 输入是不可变快照，不会被后续修改影响。
    #
    # Args:
    #     context: 增强上下文
    #     original: 原始 Stage 3 剧本
    #     candidate: 增强后的候选剧本
    # Returns:
    #     Judge 输入字典
    return {
        **copy.deepcopy(context),
        "original_episode_script": copy.deepcopy(original),
        "enriched_episode_script": copy.deepcopy(candidate),
    }


def _write_case_common(case_dir: Path, context: dict, original: dict, source_validation: dict) -> None:
    """Write the shared artifact files for a case directory.

    These three files are always written regardless of enrichment outcome,
    so downstream stages can always find the context and original script.
    """
    # 写入用例的公共文件：增强上下文、原始剧本和 Stage 3 验证结果。
    #
    # 这三个文件无论增强成功还是失败都会写入，确保下游阶段始终能找到
    # 上下文和原始剧本。这是 Stage 3.5 安全机制的一部分：即使增强失败，
    # 下游也能从 stage3_original.json 获取原始剧本。
    #
    # Args:
    #     case_dir: 用例输出目录
    #     context: 增强上下文
    #     original: 原始 Stage 3 剧本
    #     source_validation: Stage 3 验证结果
    _write_json_atomic(case_dir / "input.json", context)
    _write_json_atomic(case_dir / "stage3_original.json", original)
    _write_json_atomic(case_dir / "stage3_validation.json", source_validation)


def _result_from_original(
    case_id: str,
    case_dir: Path,
    context: dict,
    original: dict,
    source_validation: dict,
    status: str,
    reason: str,
    warnings: list[str] | None = None,
    must_fix: list[dict] | None = None,
    judge: dict | None = None,
) -> dict:
    """Fall back to the original Stage-3 script and write all artifact files.

    This is the core safety mechanism of Stage 3.5: no matter why enrichment
    fails (structural error, Judge rejection, request exception, etc.), the
    diagnostic script retains the original Stage-3 text; failed validation
    forbids downstream admission, so it is never a successful fallback.  The enrichment metadata records the failure reason for
    auditing, but the episode_script.json is always the safe original.
    """
    # 当增强失败时，生成回退到原始剧本的结果并写入所有产物文件。
    #
    # 这是 Stage 3.5 的核心安全机制：无论增强因何种原因失败（结构错误、Judge
    # 拒绝、请求异常等），都回退到原始 Stage 3 剧本，确保数据不丢失。
    #
    # 回退策略的关键设计：
    # - episode_script.json 写入原始剧本（而非候选），保证下游拿到的是安全数据
    # - duplex_enrichment.json 记录失败原因和 Judge 反馈，便于审计和改进
    # - validation.json 的 status 为 failed，但 stage35_status 记录具体失败类型
    # - original_script_hash 和 final_script_hash 相同，表明未做任何修改
    #
    # 可能的 status 值：
    # - skipped_stage3_failed：Stage 3 未通过，跳过增强
    # - rejected_structural：结构验证失败
    # - rejected_judge_invalid：Judge 输出结构不合法
    # - rejected_judge：Judge 判定需要修复
    # - rejected_request_error：API 请求异常
    #
    # Args:
    #     case_id: 用例 ID
    #     case_dir: 用例输出目录
    #     context: 增强上下文
    #     original: 原始 Stage 3 剧本
    #     source_validation: Stage 3 验证结果
    #     status: 增强状态（如 skipped_stage3_failed、rejected_structural 等）
    #     reason: 失败原因描述
    #     warnings: Judge 警告列表
    #     must_fix: Judge 必须修复项列表
    #     judge: Judge 完整输出
    # Returns:
    #     包含 case_id/status/stage35_status/script/warnings 的结果字典
    final_status = "failed"
    # 增强元数据：记录失败原因、Judge 反馈和脚本哈希
    enrichment = {
        "status": status,
        "reason": reason,
        "stage3_status": source_validation.get("status", "missing"),
        "warnings": warnings or [],
        "must_fix": must_fix or [],
        "judge": judge or {},
        "original_script_hash": digest(original),
        "final_script_hash": digest(original),  # 回退时 hash 不变，表明未修改
    }
    # 验证结果：status 为 failed，但保留 Stage 3 的结构/语义状态供参考
    validation = {
        "status": final_status,
        "structural_status": source_validation.get("structural_status", "missing"),
        "semantic_status": source_validation.get("semantic_status", "missing"),
        "stage35_status": status,
        "stage35_reason": reason,
        "warnings": warnings or [],
        "must_fix": must_fix or [],
        "source_validation": source_validation,
    }
    _write_case_common(case_dir, context, original, source_validation)
    # 回退时写入原始剧本（而非增强候选），这是安全机制的核心
    _write_json_atomic(case_dir / "episode_script.json", original)
    _write_json_atomic(case_dir / "duplex_enrichment.json", enrichment)
    _write_json_atomic(case_dir / "validation.json", validation)
    return {
        "case_id": case_id,
        "status": final_status,
        "stage35_status": status,
        "script": original,
        "warnings": warnings or [],
    }


def _candidate_errors(candidate, original, context):
    errors = validate_stage("episode_script_enriched", candidate, context)
    before, after = original.get("episode_turns", []), candidate.get("episode_turns", [])
    if [t.get("turn_id") for t in after] != [t.get("turn_id") for t in before]:
        errors.append("Stage-3 turn IDs, order and count must remain frozen")
    for old, new in zip(before, after):
        for key, value in old.items():
            if key != "user_backchannels" and new.get(key) != value:
                errors.append(f"Frozen {old['turn_id']}.{key} changed during enrichment")
    return errors


def _generate_one(case_id: str, source: dict, run_dir: Path, client: MiniMaxClient, *, resume_from: Path | None = None) -> dict:
    """Generate once, apply at most two model repairs, re-judge each valid change."""
    case_dir = run_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    source_run = Path(source["source_run"]); source_case = source_run / case_id
    source_validation = _load_json(source_case / "validation.json", {})
    original = _load_json(source_case / "episode_script.json", {"episode_turns": []})
    context = _context_for_enrichment(source, case_id, original, source_validation)
    _write_case_common(case_dir, context, original, source_validation)
    history = []
    def save_history():
        _write_json_atomic(case_dir / "repair_history.json", {"max_candidate_attempts":MAX_ATTEMPTS,
            "repair_enabled":True,"attempts":history,"manual_repair_applied":False,
            "resumed_from":str(resume_from) if resume_from else None})
    def fail(status, reason, judge=None):
        save_history()
        return _result_from_original(case_id, case_dir, context, original, source_validation, status, reason,
            warnings=(judge or {}).get("warnings", []), must_fix=(judge or {}).get("must_fix", []), judge=judge)
    if source_validation.get("status") != "passed" or not (source_case / "episode_script.json").is_file():
        return fail("skipped_stage3_failed", "Stage 3 must pass its own generation/repair gate before enrichment")
    prompt = (ROOT / "prompts/03_5_duplex_enrichment.md").read_text()
    judge_prompt = (ROOT / "prompts/03_5_duplex_judge.md").read_text()
    repair_prompt = (ROOT / "prompts/03_5_duplex_repair.md").read_text()
    judge_client = (MiniMaxClient.judge_from_env(client.judge_model) if client.judge_model.lower().startswith("gpt-")
                    else client.for_model(client.judge_model, temperature=0.0))
    candidate, structural_errors, patch_errors, judge_value = copy.deepcopy(original), [], [], {}
    structural_changes, generation_meta, judge_meta, raw = [], {}, {}, ""
    cached_judge = None
    try:
        if resume_from:
            resume_from = Path(resume_from).resolve()
            if resume_from == case_dir.resolve(): raise ValueError("resume must use a new run directory")
            if _load_json(resume_from / "input.json", {}) != context or _load_json(resume_from / "stage3_original.json", {}) != original:
                raise ValueError("saved rejection differs from the frozen input or policy")
            candidate = _load_json(resume_from / "enrichment_candidate.json", None)
            if not isinstance(candidate, dict): raise ValueError("saved candidate is missing; cannot fabricate a prior generation")
            raw = (resume_from / "enrichment_raw_response.txt").read_text()
            if (resume_from / "enrichment_prompt.md").read_text() != prompt:
                raise ValueError("saved generation Prompt differs")
            generation_meta = {"model":client.model,"reused_from":str(resume_from),"new_model_request":False}
            cached_judge = _load_json(resume_from / "judge.json", {})
            if cached_judge:
                if _judge_errors(cached_judge) or _load_json(resume_from / "judge_input.json", {}) != _judge_input(context,original,candidate):
                    raise ValueError("saved Judge input or response differs")
                if (resume_from / "judge_prompt.md").read_text() != judge_prompt:
                    raise ValueError("saved Judge Prompt differs")
        else:
            _write_text_atomic(case_dir / "enrichment_prompt.md", prompt)
            raw, generation_meta = client.complete(prompt,context,max_tokens=STAGE_MAX_TOKENS.get("episode_script",6500),response_format=JSON_RESPONSE_FORMAT)
            _write_text_atomic(case_dir / "enrichment_raw_response.txt", _redact_secret(raw,client))
            try:
                candidate = parse_json_text(raw, ENRICHMENT_KEYS)
                if not isinstance(candidate,dict): raise ValueError("enrichment output must be an object")
                candidate = _merge_candidate_with_original(candidate,original)
                candidate = _prepare_stage_value("episode_script",candidate,context)
                structural_changes = _drop_incomplete_t02(candidate,original)
                structural_changes.extend(_normalize_timing_shape(candidate))
                structural_changes.extend(_normalize_side_speech_shape(candidate,context))
            except (ValueError,TypeError,KeyError) as exc:
                candidate = copy.deepcopy(original); structural_errors = ["Invalid initial candidate: "+str(exc)]
        _write_text_atomic(case_dir / "enrichment_prompt.md",prompt)
        _write_text_atomic(case_dir / "enrichment_raw_response.txt",_redact_secret(raw,client))
        _write_json_atomic(case_dir / "enrichment_candidate.json",candidate)
        _write_json_atomic(case_dir / "generation_metadata.json",generation_meta)
        for attempt in range(1,MAX_ATTEMPTS+1):
            folder = case_dir / "stage_cache/duplex_enrichment" / f"attempt-{attempt:02d}"
            folder.mkdir(parents=True,exist_ok=True)
            entry = {"attempt":attempt,"kind":"saved_generation_and_review" if resume_from and attempt==1 else "generation" if attempt==1 else "model_repair"}
            if attempt > 1:
                repair_input = {**copy.deepcopy(context),"current_candidate":copy.deepcopy(candidate),
                    "judge_findings":copy.deepcopy(judge_value.get("must_fix",[])),"structural_errors":structural_errors,
                    "previous_patch_errors":patch_errors}
                _write_json_atomic(folder / "repair-input.json",repair_input)
                _write_text_atomic(folder / "repair-prompt.md",repair_prompt)
                repair_raw, repair_meta = client.complete(repair_prompt,repair_input,max_tokens=STAGE_MAX_TOKENS.get("episode_script",6500),response_format=JSON_RESPONSE_FORMAT)
                _write_text_atomic(folder / "repair-raw_response.txt",_redact_secret(repair_raw,client))
                _write_json_atomic(folder / "repair-metadata.json",repair_meta)
                try:
                    patches = parse_json_text(repair_raw,{"patches"})
                    _write_json_atomic(folder / "patches.json",patches)
                    repaired = apply_model_patches(candidate,original,patches,judge_value.get("must_fix",[]),structural_errors=structural_errors)
                    patch_errors = _candidate_errors(repaired,original,context)
                    if patch_errors: raise ValueError("; ".join(patch_errors))
                    candidate = repaired; structural_errors = []; patch_errors = []
                except (ValueError,KeyError,IndexError,TypeError) as exc:
                    patch_errors = [str(exc)]; entry.update(decision="rejected_patch",errors=patch_errors)
                    _write_json_atomic(folder / "patch-validation.json",{"status":"failed","errors":patch_errors})
                    history.append(entry);save_history();continue
                _write_json_atomic(folder / "patch-validation.json",{"status":"passed","errors":[]})
            structural_errors = structural_errors or _candidate_errors(candidate,original,context)
            _write_json_atomic(folder / "candidate.json",candidate)
            _write_json_atomic(folder / "structural-validation.json",{"errors":structural_errors})
            entry['candidate_sha256'] = digest(candidate)
            if structural_errors:
                entry.update(decision="structural_error",errors=structural_errors);history.append(entry);save_history();continue
            judge_input = _judge_input(context,original,candidate)
            _write_json_atomic(folder / "judge-input.json",judge_input)
            _write_text_atomic(folder / "judge-prompt.md",judge_prompt)
            if attempt == 1 and cached_judge:
                judge_value = cached_judge
                judge_raw = (resume_from / "judge_raw_response.txt").read_text()
                judge_meta = {"model":judge_client.model,"new_model_request":False,"reused_from":str(resume_from)}
            else:
                judge_raw, judge_meta = judge_client.complete(judge_prompt,judge_input,max_tokens=JUDGE_MAX_TOKENS,response_format=JSON_RESPONSE_FORMAT)
                _write_text_atomic(folder / "judge-raw_response.txt",_redact_secret(judge_raw,judge_client))
                _write_json_atomic(folder / "judge-metadata.json",judge_meta)
                judge_value = parse_json_text(judge_raw,JUDGE_KEYS)
            _write_text_atomic(folder / "judge-raw_response.txt",_redact_secret(judge_raw,judge_client))
            _write_json_atomic(folder / "judge-metadata.json",judge_meta)
            _write_json_atomic(folder / "judge.json",judge_value)
            _write_json_atomic(case_dir / "judge_input.json",judge_input)
            _write_text_atomic(case_dir / "judge_prompt.md",judge_prompt)
            _write_text_atomic(case_dir / "judge_raw_response.txt",_redact_secret(judge_raw,judge_client))
            _write_json_atomic(case_dir / "judge.json",judge_value)
            _write_json_atomic(case_dir / "judge_metadata.json",judge_meta)
            judge_errors = _judge_errors(judge_value)
            if judge_errors:
                entry.update(decision="invalid_judge",errors=judge_errors);history.append(entry)
                return fail("rejected_judge_invalid","; ".join(judge_errors))
            entry['decision'] = judge_value['decision'];history.append(entry);save_history()
            if judge_value['decision'] != 'pass': continue
            enrichment = {"status":"enriched","reason":"Stage 3.5 Judge passed","stage3_status":"passed",
                "warnings":judge_value['warnings'],"must_fix":[],"judge":judge_value,"structural_normalizations":structural_changes,
                "original_script_hash":digest(original),"final_script_hash":digest(candidate),"model_repair_attempts":attempt-1,
                "repair_history":"repair_history.json","manual_repair_applied":False}
            _write_json_atomic(case_dir / "episode_script.json",candidate)
            _write_json_atomic(case_dir / "duplex_enrichment.json",enrichment)
            _write_json_atomic(case_dir / "validation.json",{"status":"passed","structural_status":"passed","semantic_status":"passed",
                "stage35_status":"enriched","stage35_reason":enrichment['reason'],"warnings":judge_value['warnings'],"must_fix":[],
                "source_validation":source_validation,"judge":judge_value,"model_repair_attempts":attempt-1})
            _write_json_atomic(case_dir / "metadata.json",{"source_run":str(source_run),"model_identity":{"provider":client.provider,"model":client.model},
                "judge_model_identity":{"provider":judge_client.provider,"model":judge_client.model},"prompt_sha256":digest(prompt),
                "input_sha256":digest(context),"response_sha256":digest(_redact_secret(raw,client)),"final_artifact_sha256":digest(candidate),
                "repair_enabled":True,"max_attempts":MAX_ATTEMPTS,"api_key_serialized":False})
            return {"case_id":case_id,"status":"passed","stage35_status":"enriched","script":candidate,"warnings":judge_value['warnings']}
        return fail("rejected_repair_limit",f"Stage 3.5 exhausted {MAX_ATTEMPTS} candidate attempts",judge_value)
    except Exception as exc:
        _write_json_atomic(case_dir / "request-failure.json",{"type":type(exc).__name__,"reason":_redact_secret(str(exc),client)})
        return fail("rejected_request_error",_redact_secret(str(exc),client),judge_value)


def _stage35_sources(source_run: Path, selected: set[str] | None) -> list[tuple[str, dict]]:
    """Load a normal Stage-3 manifest, or a hand-assembled Stage-3 folder.

    Tries the standard manifest.json path first.  If that fails (e.g. a
    bundle run with only compact input.json files, or a final-repair bundle
    pointing to an older manifest), falls back to reconstructing each case's
    context from its embedded input.json.
    """
    # 加载 Stage 3 源数据，支持两种格式：
    # 1. 标准 manifest.json 格式（通过 _source_cases 加载）
    # 2. 手工组装的 Stage 3 文件夹（每个用例目录包含 input.json + 剧本产物）
    #
    # 设计理由：Bundle 运行可能只包含精简的 input.json 和剧本产物，
    # 不满足标准 manifest 的拆分产物契约；最终修复 bundle 也可能指向
    # 旧版源 manifest，此时回退到从嵌入的 input.json 重建上下文。
    #
    # 回退路径的重建逻辑：
    # - 从 input.json 提取 scene_task（兼容嵌入和拆分两种格式）
    # - 重建 user_voice（兼容旧版 UserSeed/VoiceProfile 格式）
    # - 重建 situation_knowledge（兼容嵌入和顶层两种格式）
    # - 添加 source_run 指向原始 Stage 3 运行目录
    #
    # Args:
    #     source_run: Stage 3 运行目录
    #     selected: 指定处理的用例 ID 集合，None 表示全部
    # Returns:
    #     (case_id, source_data) 元组列表
    manifest = source_run / "manifest.json"
    if manifest.exists():
        try:
            # 优先尝试标准 manifest 加载路径
            return _source_cases(source_run, selected)
        except ValueError as exc:
            # Bundle runs may intentionally contain only the compact input.json
            # plus episode artifacts. Final-repair bundles can also point to an
            # older source manifest whose split artifacts fail the current
            # contract, so use the embedded case context in that shape too.
            # 检查是否有嵌入的 input.json 文件可以作为回退
            embedded_inputs = list(source_run.glob("R*/input.json")) + list(source_run.glob("P*/input.json"))
            if not embedded_inputs and "missing frozen Stage-1/2 artifacts" not in str(exc):
                # 没有嵌入 input.json 且错误不是产物缺失，说明是真正的加载错误，向上抛出
                raise
    # 回退路径：从每个用例目录的 input.json 重建上下文
    # 只处理以 P 或 R 开头的目录（P=场景用例，R=角色用例）
    cases: list[tuple[str, dict]] = []
    for case_dir in sorted(p for p in source_run.iterdir() if p.is_dir() and p.name.startswith(("P", "R"))):
        case_id = case_dir.name
        if selected is not None and case_id not in selected:
            continue
        input_value = _load_json(case_dir / "input.json", {})
        if not isinstance(input_value, dict):
            raise ValueError(f"{case_id} is missing a usable Stage-3 input.json")
        # 从 input.json 重建 scene_task（兼容嵌入和拆分两种格式）
        # 嵌入格式：input.json 直接包含 scene_task 字段
        # 拆分格式：input.json 包含 scene/task_style/task_instance 等顶层字段
        embedded_scene_task = input_value.get("scene_task")
        if isinstance(embedded_scene_task, dict):
            scene_task = copy.deepcopy(embedded_scene_task)
            scene = scene_task.get("scene")
        else:
            # 拆分格式：从顶层字段重建 scene_task
            scene = input_value.get("scene")
            scene_task = {
                "scene": copy.deepcopy(scene),
                "task_style": input_value.get("task_style"),
                "task_instance": copy.deepcopy(input_value.get("task_instance", {})),
            }
        if not isinstance(scene, dict):
            raise ValueError(f"{case_id} is missing a usable Stage-3 scene")
        # 重建 user_voice（兼容旧版 UserSeed/VoiceProfile 格式）
        # 新版：input.json 包含 user_voice 字段
        # 旧版：input.json 包含 user_seed + voice_profile 字段
        embedded_voice = input_value.get("user_voice")
        user_voice = copy.deepcopy(embedded_voice) if isinstance(embedded_voice, dict) else {
            "user_seed": copy.deepcopy(input_value.get("user_seed", {})),
            "voice_profile": copy.deepcopy(input_value.get("voice_profile", "")),
        }
        # 重建 situation_knowledge
        # 优先使用嵌入的 situation_knowledge，否则从顶层字段重建
        situation = {
            "episode_situation": input_value.get("episode_situation", ""),
            "task_reference_data": input_value.get("task_reference_data", []),
            "environment_audio": input_value.get("environment_audio", ""),
            "constraints": input_value.get("constraints", []),
            "safety_limits": input_value.get("safety_limits", []),
        }
        if isinstance(input_value.get("situation_knowledge"), dict):
            situation = copy.deepcopy(input_value["situation_knowledge"])
        # 组装完整的 source 数据
        source = {
            "sampled_scene": copy.deepcopy(scene),
            "scene_task": scene_task,
            "user_voice": user_voice,
            "situation_knowledge": situation,
            # source_run 指向原始 Stage 3 运行目录，用于加载剧本产物
            "source_run": str(source_run.resolve()),
        }
        cases.append((case_id, source))
    if not cases:
        raise ValueError(f"source run has no Stage-3 cases: {source_run}")
    return cases


def main() -> None:
    """Stage 3.5 entry point: enrich all cases with duplex opportunities in parallel.

    Flow:
      1. Parse CLI arguments (source run, models, side-speech policy, etc.).
      2. Load Stage-3 source data.
      3. Apply side-speech policy to decide which cases get side_speech.
      4. Run _generate_one for each case in parallel.
      5. Aggregate results and write the manifest.
    """
    # Stage 3.5 主入口：并行增强所有用例的双工机会。
    #
    # 流程：
    # 1. 解析命令行参数（源运行、模型、旁白语音策略等）
    # 2. 加载 Stage 3 源数据
    # 3. 根据旁白语音策略决定每个用例是否启用 side_speech
    # 4. 并行调用 _generate_one 处理每个用例
    # 5. 汇总结果并写入 manifest
    parser = argparse.ArgumentParser()
    # --source-run: Stage 3 运行目录，包含已验证的剧本产物
    parser.add_argument("--source-run", type=Path, required=True)
    # --run-id: 本次运行的 ID，默认包含时间戳和 "duplex-enriched" 标识
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S") + "-duplex-enriched")
    # --workers: 并行工作线程数，默认 2（避免 API 限流）
    parser.add_argument("--workers", type=int, default=2)
    # --cases: 指定处理的用例 ID，逗号分隔；不指定则处理全部
    parser.add_argument("--cases")
    parser.add_argument("--repair-from-run", type=Path, help="continue a saved rejected candidate in a new run; inherit its frozen policies")
    # --model: 增强模型；gpt-* 前缀使用 GPT 兼容端点
    parser.add_argument("--model", default="gpt-5.6-sol", help="generation model; gpt-* uses the GPT-compatible endpoint")
    # --judge-model: 语义 Judge 模型，默认与增强模型相同
    parser.add_argument("--judge-model", default="gpt-5.6-sol", help="semantic Judge model")
    # 旁白语音策略参数
    # --side-speech-mode: sampled（按概率采样）/ all（全部启用）/ off（全部关闭）
    parser.add_argument("--side-speech-mode", choices=("sampled", "all", "off"), default="sampled")
    # --side-speech-probability: sampled 模式下启用 side_speech 的概率，默认 0.25
    parser.add_argument("--side-speech-probability", type=float, default=0.25)
    parser.add_argument("--backchannel-mode", choices=("off", "all"), default="off")
    parser.add_argument("--backchannel-min-events", type=int, help="planned minimum; default scales with main-turn count")
    parser.add_argument("--backchannel-max-events", type=int, help="planned maximum; default scales with main-turn count")
    parser.add_argument("--backchannel-max-per-turn", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if not 0.0 <= args.side_speech_probability <= 1.0:
        parser.error("--side-speech-probability must be between 0 and 1")
    # 解析 --cases 参数为集合，None 表示处理全部
    selected = {x.strip() for x in args.cases.split(",")} if args.cases else None
    source_run = args.source_run.resolve()
    # 加载 Stage 3 源数据（支持标准 manifest 和手工组装两种格式）
    sources = _stage35_sources(source_run, selected)
    # 初始化 API 客户端
    client = MiniMaxClient.from_env()
    if args.model:
        if args.model.lower().startswith("gpt-"):
            # GPT 系列模型使用专用端点，temperature=1.0 增加多样性
            client = MiniMaxClient.judge_from_env(args.model, temperature=1.0)
        else:
            client = client.for_model(args.model)
    if args.judge_model:
        client.judge_model = args.judge_model
    # 创建运行输出目录
    run_dir = ROOT / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # 加载已有 manifest（支持增量运行：已处理的用例不会重复处理）
    existing_manifest = _load_json(run_dir / "manifest.json", {})
    # 根据旁白语音策略为每个用例决定是否启用 side_speech
    prepared_sources = []
    for case_id, source in sources:
        if args.side_speech_mode == "all":
            # all 模式：所有用例都启用 side_speech
            enabled = True
        elif args.side_speech_mode == "off":
            # off 模式：所有用例都禁用 side_speech
            enabled = False
        else:
            # sampled 模式：用 case_id 的哈希值决定是否启用
            # 使用 run_id + case_id 的哈希确保：
            # 1. 同一用例在同一运行中结果一致（确定性）
            # 2. 不同运行中同一用例可能得到不同结果（多样性）
            digest_value = hashlib.sha256(f"{args.run_id}:{case_id}".encode("utf-8")).hexdigest()
            enabled = int(digest_value[:8], 16) / 0xFFFFFFFF < args.side_speech_probability
        source_copy = copy.deepcopy(source)
        # 构建旁白语音策略配置，传递给增强模型
        source_copy["side_speech_policy"] = {
            "mode": args.side_speech_mode,
            "enabled": enabled,
            # all 模式下概率设为 1.0，其他模式使用实际概率
            "case_probability": 1.0 if args.side_speech_mode == "all" else args.side_speech_probability,
            # 启用时至少 1 个事件，最多 2 个；禁用时为 0
            "min_events": 1 if enabled else 0,
            "max_events": 2,
        }
        source_case = Path(source["source_run"]) / case_id
        original_path = source_case / "episode_script.json"
        if not original_path.exists():
            original_path = source_case / "episode_script.candidate.json"
        original = _load_json(original_path, {})
        turn_count = len(original.get("episode_turns", [])) if isinstance(original, dict) else 0
        try:
            source_copy["backchannel_policy"] = planning_policy(
                turn_count, enabled=args.backchannel_mode == "all",
                min_events=args.backchannel_min_events, max_events=args.backchannel_max_events,
                max_per_turn=args.backchannel_max_per_turn) if turn_count else {
                    "enabled": args.backchannel_mode == "all", "min_events": 0,
                    "max_events": 0, "max_events_per_turn": args.backchannel_max_per_turn}
        except ValueError as exc:
            parser.error(f"{case_id}: {exc}")
        prepared_sources.append((case_id, source_copy))
        if args.repair_from_run:
            saved_input = _load_json(args.repair_from_run / case_id / "input.json", {})
            if not saved_input: parser.error(f"{case_id}: saved repair input is missing")
            source_copy["backchannel_policy"] = copy.deepcopy(saved_input["backchannel_policy"])
            source_copy["side_speech_policy"] = copy.deepcopy(saved_input["side_speech_policy"])
    # 并行处理所有用例
    # 使用 ThreadPoolExecutor 而非 ProcessPoolExecutor，因为瓶颈是 API 等待而非 CPU
    results = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {executor.submit(_generate_one, case_id, source, run_dir, client,
                                resume_from=args.repair_from_run / case_id if args.repair_from_run else None): case_id for case_id, source in prepared_sources}
        for job in as_completed(jobs):
            case_id = jobs[job]
            try:
                results.append(job.result())
            except Exception as exc:
                # 捕获线程级别的异常（如未处理的 RuntimeError），记录但不中断其他用例
                failures.append(f"{case_id}: {_redact_secret(str(exc), client)}")
    # 按用例 ID 排序结果，确保输出顺序一致
    results.sort(key=lambda x: x["case_id"])
    # 合并已有 manifest 的 case_ids（支持增量运行）
    # 增量运行：如果本次只处理部分用例，保留之前已处理的用例 ID
    existing_case_ids = existing_manifest.get("case_ids", []) if isinstance(existing_manifest, dict) else []
    case_ids = [case_id for case_id in existing_case_ids if isinstance(case_id, str)]
    for case_id, _ in prepared_sources:
        if case_id not in case_ids:
            case_ids.append(case_id)
    if not selected:
        # 未指定 --cases 时，case_ids 只包含本次处理的用例
        case_ids = [case_id for case_id, _ in prepared_sources]
    # 统计各状态数量，用于 manifest 和运行报告
    status_counts = {
        status: 0
        for status in ("enriched", "skipped_stage3_failed", "rejected_judge", "rejected_structural", "rejected_request_error", "rejected_judge_invalid", "rejected_repair_limit")
    }
    for case_id in case_ids:
        # 从每个用例的 duplex_enrichment.json 读取最终状态
        enrichment = _load_json(run_dir / case_id / "duplex_enrichment.json", {})
        status = enrichment.get("status") if isinstance(enrichment, dict) else None
        if status in status_counts:
            status_counts[status] += 1
    # 写入 manifest，记录本次运行的完整元数据
    _write_json_atomic(run_dir / "manifest.json", {
        "run_id": args.run_id,
        "source_run": str(source_run),
        "stage": "duplex_enrichment",
        "case_ids": case_ids,
        "workers": args.workers,
        "model": client.model,
        "provider": client.provider,
        "judge_model": client.judge_model,
        "repair_enabled": True,
        "max_candidate_attempts": MAX_ATTEMPTS,
        "repair_from_run": str(args.repair_from_run.resolve()) if args.repair_from_run else None,
        "rollout_generated": False,
        "actor_outputs_generated": False,
        "side_speech_mode": args.side_speech_mode,
        "backchannel_mode": args.backchannel_mode,
        "backchannel_policies": {case_id: source["backchannel_policy"] for case_id, source in prepared_sources},
        "side_speech_probability": args.side_speech_probability,
        "failures": failures,
        "stage35_status_counts": status_counts,
    })
    if failures:
        print("Stage 3.5 completed with case errors:", "; ".join(failures))
    # 打印运行目录路径，便于后续查看结果
    print(run_dir)
    if failures or any(value for status,value in status_counts.items() if status != "enriched"):
        raise SystemExit(1)


# 入口点：直接运行时执行 Stage 3.5 增强流程
# 用法：python step_03_5_generate_duplex_enrichment.py --source-run <stage3_run_dir> [options]
if __name__ == "__main__":
    main()
