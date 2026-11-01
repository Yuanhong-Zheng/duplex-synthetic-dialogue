#!/usr/bin/env python3
"""Generate Stage-2 Situation/Knowledge artifacts from a Stage-1 sample run.

This command intentionally stops after ``situation_knowledge``.  It does not
create an episode script, rubric, audit, actor response, or rollout artifact.
"""
# ── Stage 2: 情境生成脚本 ──────────────────────────────────────────────
# 本脚本负责从 Stage-1 的采样结果生成 EpisodeSituation + Task Reference Data
# + Constraints + Safety Limits 等情境知识产物。核心流程为：
#   1. 加载 Stage-1 已通过验证的 case
#   2. 调用 LLM 生成 situation_knowledge JSON
#   3. 结构验证 → 本地修复（structural repair）
#   4. 语义审查（Judge）→ 定向修复（semantic repair）→ 重新审查
#   5. 写入产物文件与验证记录
# Judge/Repair 循环最多尝试 MAX_JUDGE_ATTEMPTS 轮，确保语义质量。
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import html
import json
from pathlib import Path
import subprocess
import sys
import time

from minimax_client import MiniMaxClient, parse_json_text
from run_pipeline import (
    JSON_RESPONSE_FORMAT,
    STAGE_MAX_TOKENS,
    _prepare_stage_value,
    _redact_secret,
    _run_local_repairs,
    _write_json_atomic,
    _write_text_atomic,
    digest,
    stage_context,
    stage_prompt,
)
from stage_validation import validate_stage

ROOT = Path(__file__).parent  # 脚本所在目录，用于定位 prompts/ 和 runs/ 等相对路径

# situation_knowledge JSON 必须包含的顶层字段集合
# 缺少任一字段将触发结构验证失败，进入本地修复流程
SITUATION_KEYS = {
    "episode_situation",       # 情境描述：对话发生的场景、参与者、背景
    "task_reference_data",     # 任务参考数据：用户需要查询/操作的具体数据条目
    "environment_audio",       # 环境音频描述：背景噪声类型与特征
    "estimated_snr_db",        # 估计信噪比(dB)：衡量语音清晰度的物理指标
    "constraints",             # 约束条件：对话中必须遵守的规则（如时间限制、权限等）
    "safety_limits",           # 安全限制：涉及隐私、合规、风险的红线约束
}

# Judge 输出必须包含的顶层字段：decision（通过/需修复）+ findings（问题清单）
JUDGE_KEYS = {"decision", "findings"}

# Judge/Repair 循环最大尝试次数——超过此限制仍未通过则标记为 failed
MAX_JUDGE_ATTEMPTS = 3

# Judge 模型的最大输出 token 数，限制审查意见的长度
JUDGE_MAX_TOKENS = 3200


def _load_source_cases(source_run: Path, selected: set[str] | None) -> list[tuple[str, dict]]:
    """Load validated Stage-1 cases from a source run directory.

    Only cases whose Stage-1 validation status is ``passed``, ``cached``, or
    ``derived`` are accepted.  Legacy ``user_seed`` / ``voice_profile`` objects
    are normalised to the current contract so that downstream prompts never
    receive hidden instructions from an older schema.
    """
    # ── 加载 Stage-1 源 case ──────────────────────────────────────────
    # 从 source_run 的 manifest.json 读取 case 列表，仅选取已通过验证的 case。
    # selected 参数允许只处理指定的 case_id 子集（用于增量重跑）。
    # 对旧版 user_seed/voice_profile 做归一化，防止遗留字段污染 Stage-2 prompt。
    manifest_path = source_run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"source manifest has no cases: {manifest_path}")
    result: list[tuple[str, dict]] = []
    for scene in cases:
        if not isinstance(scene, dict) or not scene.get("case_id"):
            raise ValueError("source manifest contains an invalid case")
        case_id = str(scene["case_id"])
        if selected is not None and case_id not in selected:
            continue
        case_dir = source_run / case_id
        scene_task_path = case_dir / "scene_task.json"
        user_voice_path = case_dir / "user_voice.json"
        source_validation_path = case_dir / "validation.json"
        if not scene_task_path.exists() or not user_voice_path.exists():
            raise ValueError(f"{case_id} is missing Stage-1 scene_task.json or user_voice.json")
        scene_task = json.loads(scene_task_path.read_text(encoding="utf-8"))
        user_voice = json.loads(user_voice_path.read_text(encoding="utf-8"))
        source_validation = json.loads(source_validation_path.read_text(encoding="utf-8")) if source_validation_path.exists() else {}
        scene_status = source_validation.get("status")
        if scene_status is None and isinstance(source_validation.get("scene_task"), dict):
            scene_status = source_validation["scene_task"].get("status")
        voice_status = source_validation.get("user_voice", {}).get("status") if isinstance(source_validation.get("user_voice"), dict) else None
        if scene_status not in {"passed", "cached", "derived"} or (voice_status is not None and voice_status not in {"passed", "cached", "derived"}):
            raise ValueError(f"{case_id} Stage-1 validation is not passed")
        if not isinstance(scene_task, dict) or not isinstance(user_voice, dict):
            raise ValueError(f"{case_id} has invalid Stage-1 JSON")
        # ── 旧版数据归一化 ──────────────────────────────────────────────
        # 早期完整 10-case 运行存储了更丰富的 UserSeed/VoiceProfile 对象。
        # 这里只保留当前合约定义的字段（role_relation, task_familiarity, voice_prompt），
        # 避免旧版隐藏指令泄漏到 Stage-2 的 prompt 上下文中。
        # Older complete 10-case runs stored a richer UserSeed/VoiceProfile
        # object.  Normalize only the fields owned by the current contract so
        # a new Stage-2 run never receives legacy keys as hidden instructions.
        legacy_seed = user_voice.get("user_seed")
        legacy_voice = user_voice.get("voice_profile")
        if isinstance(legacy_seed, dict):
            familiarity = legacy_seed.get("familiarity")
            if isinstance(familiarity, dict):
                task_familiarity = familiarity.get("task")
            else:
                task_familiarity = legacy_seed.get("task_familiarity")
            user_voice = {
                "user_seed": {
                    "role_relation": legacy_seed.get("role_relation", ""),
                    "task_familiarity": task_familiarity,
                },
                "voice_profile": (
                    legacy_voice.get("voice_prompt", "")
                    if isinstance(legacy_voice, dict)
                    else legacy_voice
                ),
            }
        result.append((case_id, {
            "sampled_scene": copy.deepcopy(scene),
            "scene_task": scene_task,
            "user_voice": user_voice,
        }))
    if not result:
        raise ValueError("no source cases selected")
    return result


def _validate_judge_output(value: object) -> list[str]:
    """Validate only the Judge response shape, never its semantic claims."""
    # ── Judge 输出结构验证 ──────────────────────────────────────────
    # 仅验证 Judge 返回的 JSON 结构是否符合规范，不评判其语义内容。
    # 这保证了 Judge 的输出可以被下游安全解析，同时避免将验证逻辑
    # 与 Judge 的审查能力混淆——结构合规 ≠ 语义正确。
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["judge output must be an object"]
    if set(value) != JUDGE_KEYS:
        errors.append("judge output must contain exactly decision and findings")
    # decision 只允许 "pass"（通过）或 "repair"（需修复），其他值视为无效
    if value.get("decision") not in {"pass", "repair"}:
        errors.append("judge decision must be pass or repair")
    findings = value.get("findings")
    if not isinstance(findings, list):
        errors.append("judge findings must be an array")
        return errors
    # 每个 finding 必须包含完整的五元组：severity + path + problem + evidence + repair
    finding_keys = {"severity", "path", "problem", "evidence", "repair"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"judge findings[{index}] must be an object")
            continue
        if set(finding) != finding_keys:
            errors.append(f"judge findings[{index}] has an invalid field set")
        # severity 只允许 "must_fix"（必须修复）或 "advisory"（建议修复）
        if finding.get("severity") not in {"must_fix", "advisory"}:
            errors.append(f"judge findings[{index}].severity is invalid")
        # path/problem/evidence/repair 必须是非空字符串，确保信息完整可追溯
        for field in ("path", "problem", "evidence", "repair"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                errors.append(f"judge findings[{index}].{field} must be a non-empty string")
    # 一致性约束：decision=pass 时 findings 必须为空（通过就不该有问题）
    if value.get("decision") == "pass" and findings:
        errors.append("judge pass must have an empty findings array")
    # 一致性约束：decision=repair 时必须至少有一个 must_fix 级别的 finding
    # （如果只有 advisory，说明问题不严重，不应要求修复）
    if value.get("decision") == "repair" and not any(
        isinstance(item, dict) and item.get("severity") == "must_fix" for item in findings
    ):
        errors.append("judge repair must contain a must_fix finding")
    return errors


def _judge_context(context: dict, value: dict) -> dict:
    """构建 Judge 审查的输入上下文。

    将当前生成的 situation_knowledge 完整嵌入上下文，供 Judge 模型
    审查其语义质量（如约束是否合理、安全限制是否充分等）。

    Args:
        context: 包含 case_id、scene、task_style 等元信息的上下文字典
        value: 当前生成的 situation_knowledge JSON 对象

    Returns:
        包含 situation_knowledge 的 Judge 输入上下文字典
    """
    return {
        "case_id": context.get("case_id"),
        "scene": context.get("scene"),
        "task_style": context.get("task_style"),
        "task_instance": context.get("task_instance"),
        "user_seed": context.get("user_seed"),
        "voice_profile": context.get("voice_profile"),
        "available_modalities": context.get("available_modalities"),
        "capability_contract": context.get("capability_contract"),
        "situation_knowledge": copy.deepcopy(value),
    }


def _repair_context(context: dict, value: dict, findings: list[dict]) -> dict:
    """构建语义修复（semantic repair）的输入上下文。

    与 Judge 上下文不同，repair 上下文同时包含当前 situation_knowledge
    和 Judge 指出的 must_fix 问题清单，使修复模型能定向修正缺陷。

    Args:
        context: 包含 case_id、scene 等元信息的上下文字典
        value: 当前待修复的 situation_knowledge JSON 对象
        findings: Judge 标记为 must_fix 的问题列表

    Returns:
        包含 current_situation_knowledge + judge_findings 的修复输入上下文
    """
    return {
        "case_id": context.get("case_id"),
        "scene": context.get("scene"),
        "task_style": context.get("task_style"),
        "task_instance": context.get("task_instance"),
        "user_seed": context.get("user_seed"),
        "voice_profile": context.get("voice_profile"),
        "available_modalities": context.get("available_modalities"),
        "capability_contract": context.get("capability_contract"),
        "current_situation_knowledge": copy.deepcopy(value),
        "judge_findings": copy.deepcopy(findings),
    }


def _generate_one(case_id: str, source: dict, run_dir: Path, client: MiniMaxClient) -> dict:
    """为单个 case 生成 situation_knowledge 产物，含完整的 Judge/Repair 循环。

    核心流程：
      1. 调用 LLM 生成初始 situation_knowledge
      2. 结构验证 → 本地修复（structural repair，修复缺失字段/类型错误）
      3. 语义审查（Judge）→ 定向修复（semantic repair）→ 重新审查
      4. 写入产物文件（仅通过验证时写入 situation_knowledge.json）

    设计要点：
      - 结构修复与语义修复分离：结构问题由确定性逻辑修复，语义问题由 LLM 修复
      - Judge 使用独立模型（temperature=0.0）确保审查稳定性
      - 所有中间产物（Judge 输入/输出、修复记录）均写入缓存目录，便于调试追溯
      - 最终只保留通过验证的版本作为正式产物

    Args:
        case_id: 用例唯一标识
        source: 包含 scene_task、user_voice、sampled_scene 的源数据字典
        run_dir: 本次运行的输出根目录
        client: MiniMax API 客户端（含生成模型和 Judge 模型配置）

    Returns:
        包含 case_id、situation、validation、模型信息的结果字典
    """
    # ── 初始化 case 目录与缓存路径 ──────────────────────────────────
    # cache_dir 用于存储 Judge/Repair 的中间产物，便于事后审计
    case_dir = run_dir / case_id
    cache_dir = case_dir / "stage_cache" / "situation_knowledge"
    case_dir.mkdir(parents=True, exist_ok=True)
    scene_task = source["scene_task"]
    user_voice = source["user_voice"]
    sampled_scene = source["sampled_scene"]
    scene = scene_task.get("scene")
    if not isinstance(scene, dict):
        raise ValueError(f"{case_id} scene_task.scene is not an object")

    # ── Step 1: 调用 LLM 生成初始 situation_knowledge ──────────────
    # 将 Stage-1 的 scene_task 和 user_voice 作为先验上下文，
    # 引导生成模型产出与场景匹配的情境描述和约束条件。
    prior = {"scene_task": scene_task, "user_voice": user_voice}
    context = stage_context(case_id, "situation_knowledge", scene, prior)
    prompt = stage_prompt("situation_knowledge", scene)
    raw, meta = client.complete(
        prompt,
        context,
        max_tokens=STAGE_MAX_TOKENS["situation_knowledge"],
        response_format=JSON_RESPONSE_FORMAT,
    )
    try:
        value = parse_json_text(raw, SITUATION_KEYS)
    except Exception as exc:
        # 解析失败时保留错误信息，后续结构验证会捕获并触发修复
        value = {"_parse_error": str(exc)}
    value = _prepare_stage_value("situation_knowledge", value, context)

    # ── Step 2: 结构验证 + 本地修复（structural repair）──────────
    # 验证生成的 JSON 是否包含所有必需字段且类型正确。
    # 结构性问题（如缺失字段、类型错误）由 _run_local_repairs 确定性修复，
    # 不需要调用 LLM，效率更高且结果可控。
    errors = validate_stage("situation_knowledge", value, context)
    structural_repairs: list[dict] = []
    if errors:
        # 结构修复：用确定性逻辑补全缺失字段、修正类型错误
        value, errors, repairs = _run_local_repairs(
            "situation_knowledge",
            value,
            errors,
            context,
            client,
            cache_dir / "structural",
        )
        structural_repairs = repairs

    # ── Step 3: 语义审查（Judge）+ 定向修复（semantic repair）循环 ──
    # 结构验证通过后，进入语义审查阶段。Judge 模型以 temperature=0.0
    # 审查 situation_knowledge 的语义质量（如约束是否合理、安全限制是否充分）。
    # 若 Judge 判定 "repair"，则提取 must_fix findings 送入修复模型定向修正，
    # 修复后重新进入 Judge 审查，最多循环 MAX_JUDGE_ATTEMPTS 轮。
    judge_client = client.for_model(client.judge_model, temperature=0.0)
    judge_prompt = (ROOT / "prompts" / "02_situation_knowledge_judge.md").read_text(encoding="utf-8")
    repair_prompt = (ROOT / "prompts" / "02_situation_knowledge_repair.md").read_text(encoding="utf-8")
    judge_dir = cache_dir / "judge"
    repair_dir = cache_dir / "semantic_repair"
    attempt_records: list[dict] = []
    semantic_status = "failed"  # 默认失败，只有 Judge 明确通过才改为 "passed"
    semantic_errors: list[str] = []
    selected_raw = raw  # 记录最终被采纳的原始响应（可能是初始生成或修复后的版本）
    selected_meta = meta

    if errors:
        # 结构验证仍有错误，跳过语义审查（语义审查的前提是结构合规）
        semantic_errors = list(errors)
    else:
        # ── Judge/Repair 循环 ──────────────────────────────────────
        # 每轮循环：Judge 审查 → 若需修复 → Repair 修正 → 重新 Judge
        for attempt in range(1, MAX_JUDGE_ATTEMPTS + 1):
            judge_input = _judge_context(context, value)
            judge_raw, judge_meta = judge_client.complete(
                judge_prompt,
                judge_input,
                max_tokens=JUDGE_MAX_TOKENS,
                response_format=JSON_RESPONSE_FORMAT,
            )
            try:
                judge_value = parse_json_text(judge_raw, JUDGE_KEYS)
            except Exception as exc:
                judge_value = {"_parse_error": str(exc)}
            judge_errors = _validate_judge_output(judge_value)
            # 将 Judge 的输入/输出/验证结果写入缓存，便于事后追溯
            judge_dir.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-input.json", judge_input)
            _write_text_atomic(judge_dir / f"attempt-{attempt:02d}-prompt.md", judge_prompt)
            _write_text_atomic(judge_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(judge_raw, judge_client))
            _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-review.json", judge_value)
            _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-validation.json", {"errors": judge_errors})

            if judge_errors:
                # Judge 输出结构无效，无法信任其审查结论，直接终止循环
                semantic_errors = [f"judge response invalid: {error}" for error in judge_errors]
                attempt_records.append({
                    "attempt": attempt,
                    "decision": "invalid",
                    "judge_model": judge_meta.get("model", judge_client.model),
                    "errors": judge_errors,
                })
                break

            # 提取 must_fix 级别的问题（advisory 级别仅建议，不触发修复）
            must_fix = [
                finding for finding in judge_value["findings"]
                if finding.get("severity") == "must_fix"
            ]
            attempt_records.append({
                "attempt": attempt,
                "decision": judge_value["decision"],
                "finding_count": len(judge_value["findings"]),
                "must_fix_count": len(must_fix),
                "judge_model": judge_meta.get("model", judge_client.model),
                "response_sha256": digest(judge_raw),
            })
            if judge_value["decision"] == "pass" or not must_fix:
                # Judge 判定通过，或无 must_fix 问题 → 语义审查通过
                semantic_status = "passed"
                semantic_errors = []
                selected_raw = selected_raw if selected_raw else raw
                break

            if attempt >= MAX_JUDGE_ATTEMPTS:
                # 达到最大尝试次数仍未通过，终止循环并标记失败
                semantic_errors = [
                    f"semantic judge repair limit reached ({MAX_JUDGE_ATTEMPTS} total attempts)"
                ]
                break

            # ── 定向修复（semantic repair）──────────────────────────
            # 将 must_fix 问题清单与当前 situation_knowledge 一起送入修复模型，
            # 修复模型针对每个问题定向修正，而非盲目重新生成。
            repair_input = _repair_context(context, value, must_fix)
            repair_raw, repair_meta = client.complete(
                repair_prompt,
                repair_input,
                max_tokens=STAGE_MAX_TOKENS["situation_knowledge"],
                response_format=JSON_RESPONSE_FORMAT,
            )
            try:
                repaired = parse_json_text(repair_raw, SITUATION_KEYS)
            except Exception as exc:
                repaired = {"_parse_error": str(exc)}
            repaired = _prepare_stage_value("situation_knowledge", repaired, context)
            # 修复后的结果也需要通过结构验证，确保修复没有引入新的结构问题
            repair_errors = validate_stage("situation_knowledge", repaired, context)
            repair_repairs: list[dict] = []
            if repair_errors:
                repaired, repair_errors, repair_repairs = _run_local_repairs(
                    "situation_knowledge",
                    repaired,
                    repair_errors,
                    context,
                    client,
                    repair_dir / f"attempt-{attempt:02d}-structural",
                )
            repair_dir.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-input.json", repair_input)
            _write_text_atomic(repair_dir / f"attempt-{attempt:02d}-prompt.md", repair_prompt)
            _write_text_atomic(repair_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(repair_raw, client))
            _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-parsed_output.json", repaired)
            _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-validation.json", {
                "errors": repair_errors,
                "structural_repairs": repair_repairs,
            })
            if repair_errors:
                # 修复后结构验证仍失败，终止循环（修复引入了新的结构问题）
                semantic_errors = [
                    f"semantic repair attempt {attempt} failed structure: {error}"
                    for error in repair_errors
                ]
                break
            # 修复成功，用修复后的版本替换当前 value，下一轮循环将重新 Judge
            value = repaired
            selected_raw = repair_raw
            selected_meta = repair_meta

    # ── Step 4: 写入最终产物 ──────────────────────────────────────────
    # 只有结构验证和语义审查都通过时，才写入正式的 situation_knowledge.json。
    # 无论是否通过，都会写入 validation.json 和 metadata.json 供审计。
    safe_raw = _redact_secret(selected_raw, client)
    _write_json_atomic(case_dir / "input.json", context)
    _write_text_atomic(case_dir / "prompt.md", prompt)
    _write_text_atomic(case_dir / "raw_response.txt", safe_raw)
    validation = {
        # 综合状态：结构 + 语义都通过才为 "passed"
        "status": "passed" if not errors and semantic_status == "passed" else "failed",
        "structural_status": "passed" if not errors else "failed",
        "semantic_status": semantic_status,
        "errors": semantic_errors if semantic_errors else errors,
        "repair_attempts": structural_repairs,  # 结构修复记录
        "semantic_attempts": attempt_records,    # 语义审查/修复记录
    }
    if validation["status"] == "passed":
        # 仅通过验证时写入正式产物，避免下游消费不合格数据
        _write_json_atomic(case_dir / "situation_knowledge.json", value)
    _write_json_atomic(case_dir / "validation.json", validation)
    _write_json_atomic(case_dir / "metadata.json", {
        "source_run": source.get("source_run"),
        "model_identity": {"provider": client.provider, "model": client.model},
        "judge_model_identity": {"provider": judge_client.provider, "model": judge_client.model},
        "prompt_sha256": digest(prompt),       # prompt 模板哈希，用于追踪模板变更
        "input_sha256": digest(context),       # 输入上下文哈希，用于缓存命中判断
        "response_sha256": digest(safe_raw),   # 响应内容哈希，用于去重
        "api_key_serialized": False,           # 安全标记：API 密钥不写入文件
        "repair_attempts": structural_repairs,
        "semantic_attempts": attempt_records,
    })
    return {
        "case_id": case_id,
        "sampled_scene": sampled_scene,
        "situation": value if validation["status"] == "passed" else {},
        "validation": validation,
        "model": client.model,
        "provider": client.provider,
        "judge_model": judge_client.model,
    }


def _render_html(run_dir: Path, run_id: str, results: list[dict]) -> Path:
    """将所有 case 的生成结果渲染为可视化 HTML 报告。

    每个 case 渲染为一张卡片，展示 EpisodeSituation、Task Reference Data、
    Environment Audio、Constraints、Safety Limits 等关键信息，以及验证状态。
    通过/未通过用不同颜色标记，便于快速定位问题 case。

    Args:
        run_dir: 运行输出目录，HTML 文件将写入此目录
        run_id: 本次运行标识，显示在报告标题中
        results: 所有 case 的生成结果列表

    Returns:
        生成的 HTML 文件路径
    """
    cards: list[str] = []
    for result in results:
        case_id = result["case_id"]
        scene = result["sampled_scene"]
        case_dir = run_dir / case_id
        situation = result.get("situation", {})
        validation = result["validation"]
        errors = validation.get("errors", [])
        passed = validation.get("status") == "passed"
        status = "Judge 通过" if passed else "未通过 Judge"
        # 验证错误信息以 JSON 格式展示，便于调试
        error_html = "" if not errors else f"<div class='errors'><b>Validation errors</b><pre>{html.escape(json.dumps(errors, ensure_ascii=False, indent=2))}</pre></div>"
        reference_data = situation.get("task_reference_data", []) if isinstance(situation, dict) else []
        constraints = situation.get("constraints", []) if isinstance(situation, dict) else []
        limits = situation.get("safety_limits", []) if isinstance(situation, dict) else []
        cards.append(f"""
        <article class='case'>
          <header><span class='case-id'>{html.escape(case_id)}</span><span class='status {'ok' if passed else 'bad'}'>{status}</span></header>
          <h2>{html.escape(str(scene.get('domain', '')))} / {html.escape(str(scene.get('scenario', '')))}</h2>
          <p class='meta'><code>{html.escape(str(scene.get('domain_id', '')))}</code> · <code>{html.escape(str(scene.get('scenario_id', '')))}</code></p>
          <section><h3>EpisodeSituation</h3><p>{html.escape(str(situation.get('episode_situation', '')))}</p></section>
          <section><h3>Task Reference Data</h3><ul>{''.join(f"<li>{html.escape(str(item))}</li>" for item in reference_data if isinstance(item, str)) or '<li>无</li>'}</ul></section>
          <section><h3>Environment audio</h3><p class='audio'>{html.escape(str(situation.get('environment_audio', '')))}</p><p class='meta'>Estimated SNR: {html.escape(str(situation.get('estimated_snr_db', '')))} dB</p></section>
          <section><h3>Constraints</h3><ul>{''.join(f"<li><b>{html.escape(str(item.get('constraint_id', '')))}</b> [{html.escape(str(item.get('kind', '')))}] {html.escape(str(item.get('text', '')))}</li>" for item in constraints if isinstance(item, dict)) or '<li>无</li>'}</ul></section>
          <section><h3>Safety limits</h3><ul>{''.join(f"<li><b>{html.escape(str(item.get('limit_id', '')))}</b> [{html.escape(str(item.get('severity', '')))}] {html.escape(str(item.get('text', '')))}</li>" for item in limits if isinstance(item, dict)) or '<li>无</li>'}</ul></section>
          <details><summary>Source scene</summary><pre>{html.escape(json.dumps(scene, ensure_ascii=False, indent=2))}</pre></details>
          {error_html}
        </article>""")
    model = results[0].get("model", "") if results else ""
    provider = results[0].get("provider", "") if results else ""
    judge_model = results[0].get("judge_model", "") if results else ""
    # ── 渲染完整 HTML 页面 ──────────────────────────────────────────
    # 使用内联 CSS，无外部依赖，可直接在浏览器打开。
    # 采用 CSS Grid 自适应布局，移动端自动切换为单列。
    output = f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Situation and Knowledge Samples</title>
<style>
:root {{ color-scheme: light; --ink:#1d2733; --muted:#607080; --line:#d9e0e7; --paper:#f5f7f9; --accent:#146c94; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.7 system-ui,-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:42px 24px 72px; }} h1 {{ margin:0 0 8px; font-size:30px; letter-spacing:0; }} .lead {{ color:var(--muted); margin:0 0 28px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(390px,1fr)); gap:18px; }} .case {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:22px; box-shadow:0 4px 16px #14202b0d; }}
header {{ display:flex; justify-content:space-between; align-items:center; gap:12px; }} .case-id {{ color:var(--accent); font-weight:700; letter-spacing:.08em; }} .status {{ border-radius:999px; padding:2px 9px; font-size:12px; }} .status.ok {{ color:#17633d; background:#e6f5ec; }} .status.bad {{ color:#8a2b2b; background:#fde9e9; }}
h2 {{ font-size:19px; line-height:1.35; margin:16px 0 2px; }} h3 {{ font-size:14px; margin:18px 0 5px; color:var(--accent); }} section p {{ margin:0; }} .audio {{ background:#f1f6f8; border-left:3px solid var(--accent); padding:10px 12px; }} .meta {{ color:var(--muted); margin:0; }} ul {{ margin:0; padding-left:22px; }} li {{ margin:3px 0; }} details {{ margin-top:18px; color:var(--muted); }} pre {{ white-space:pre-wrap; overflow:auto; background:#f6f8fa; border:1px solid var(--line); padding:10px; font-size:12px; line-height:1.5; }} .errors {{ color:#8a2b2b; margin-top:14px; }}
@media (max-width:600px) {{ main {{ padding:26px 14px 48px; }} .grid {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>Situation 与 Knowledge 样本</h1>
<p class='lead'>Run {html.escape(run_id)} · {len(results)} 个 Stage-1 输入 · 仅执行 Stage 2，不包含 Episode Script、Rubric、Audit 或 Rollout。生成模型：{html.escape(str(provider))} / {html.escape(str(model))}；Judge：{html.escape(str(judge_model))}</p>
<div class='grid'>{''.join(cards)}</div>
</main></body></html>"""
    path = run_dir / "situation-knowledge-samples.html"
    _write_text_atomic(path, output)
    return path


def main() -> None:
    """Stage-2 情境生成的主入口函数。

    执行流程：
      1. 解析命令行参数（源运行目录、模型、并发数等）
      2. 加载 Stage-1 已通过验证的 case
      3. 使用线程池并发执行 _generate_one
      4. 汇总结果，写入 manifest.json
      5. 若全部通过，生成 HTML 可视化报告和离线 bundle

    设计要点：
      - 使用 ThreadPoolExecutor 并发生成，提高吞吐量
      - 任何 case 抛出异常或验证失败都会导致整体失败（严格模式）
      - 失败时不生成 HTML，避免展示不完整数据
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    # run-id 默认使用时间戳，确保每次运行有唯一标识
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S") + "-situation-knowledge")
    # 并发工作线程数，默认 2（避免 API 限流）
    parser.add_argument("--workers", type=int, default=2)
    # 可选：只处理指定的 case_id 子集，用于增量重跑
    parser.add_argument("--cases", help="comma-separated case IDs; default is all source cases")
    # 生成模型，gpt-* 前缀使用 GPT 兼容端点
    parser.add_argument("--model", default="gpt-5.6-sol", help="generation model; gpt-* uses the GPT-compatible endpoint")
    # 语义审查 Judge 模型，独立于生成模型以确保审查客观性
    parser.add_argument("--judge-model", default="gpt-5.6-sol", help="semantic Judge model")
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=ROOT / "taxonomy/domain-scenario-with-task-examples-gpt56-sol-v5.json",
        help="taxonomy JSON used by the complete HTML bundle",
    )
    parser.add_argument("--no-html", action="store_true", help="skip the complete HTML bundle")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    # 解析 --cases 参数为集合，None 表示处理全部 case
    selected = {item.strip() for item in args.cases.split(",")} if args.cases else None
    client = MiniMaxClient.from_env()
    if args.model:
        # gpt-* 前缀的模型使用 Judge 端点（GPT 兼容接口），temperature=1.0 保持生成多样性
        if args.model.lower().startswith("gpt-"):
            client = MiniMaxClient.judge_from_env(args.model, temperature=1.0)
        else:
            client = client.for_model(args.model)
    if args.judge_model:
        # 覆盖默认的 Judge 模型，允许使用不同模型进行审查
        client.judge_model = args.judge_model
    judge_model = client.judge_model
    source_run = (ROOT / args.source_run).resolve() if not args.source_run.is_absolute() else args.source_run.resolve()
    sources = _load_source_cases(source_run, selected)
    run_dir = ROOT / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # ── 并发生成 ──────────────────────────────────────────────────────
    # 使用 ThreadPoolExecutor 并发调用 _generate_one，每个 case 独立处理。
    # 异常不会中断其他 case 的处理，而是在汇总阶段统一报告。
    results: list[dict] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {}
        for case_id, source in sources:
            source["source_run"] = str(source_run)
            jobs[executor.submit(_generate_one, case_id, source, run_dir, client)] = case_id
        for job in as_completed(jobs):
            case_id = jobs[job]
            try:
                results.append(job.result())
            except Exception as exc:
                failures.append(f"{case_id}: {_redact_secret(str(exc), client)}")
    results.sort(key=lambda item: item["case_id"])
    # ── 写入 manifest.json ──────────────────────────────────────────
    # manifest 记录本次运行的完整元信息，包括源运行、模型、验证状态等。
    # 下游 Stage 可通过 manifest 追溯数据来源和生成配置。
    _write_json_atomic(run_dir / "manifest.json", {
        "run_id": args.run_id,
        "source_run": str(source_run),
        "stage": "situation_knowledge",
        "workers": args.workers,
        "case_ids": [case_id for case_id, _ in sources],
        "model": client.model,
        "provider": client.provider,
        "judge_model": judge_model,
        "semantic_judge_enabled": True,       # 语义审查已启用
        "max_judge_attempts": MAX_JUDGE_ATTEMPTS,
        "rollout_generated": False,           # 本脚本不生成 rollout
        "downstream_stages_generated": False,  # 本脚本不生成下游 Stage 产物
        "api_key_serialized": False,
        "failures": failures,
    })
    if failures:
        # 任何 case 抛出异常即整体失败，严格模式确保数据完整性
        raise RuntimeError("; ".join(sorted(failures)))
    # ── 检查验证失败的 case ──────────────────────────────────────────
    # 即使没有异常，验证未通过的 case 也视为失败。
    # 失败时不生成 HTML，避免展示不完整或错误的数据。
    invalid = [
        f"{item['case_id']}: {json.dumps(item['validation'].get('errors', []), ensure_ascii=False)}"
        for item in results if item["validation"].get("status") != "passed"
    ]
    if invalid:
        raise RuntimeError("situation_knowledge validation failed; no HTML was published: " + "; ".join(invalid))
    # ── 生成 HTML 可视化报告 ──────────────────────────────────────────
    # 全部通过后，调用 build_episode_script_bundle.py 生成离线 bundle，
    # 包含 taxonomy 信息和所有 case 的可视化卡片。
    if not args.no_html:
        taxonomy_path = args.taxonomy if args.taxonomy.is_absolute() else ROOT / args.taxonomy
        output_path = run_dir / "offline-episode-script-bundle.html"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "build_episode_script_bundle.py"),
                "--run-dir",
                str(run_dir),
                "--taxonomy",
                str(taxonomy_path),
                "--output",
                str(output_path),
            ],
            check=True,
        )


# ── 脚本入口 ──────────────────────────────────────────────────────────
# 用法示例：
#   python step_02_generate_situation_knowledge.py --source-run runs/20260801-120000-stage1 \
#       --model gpt-5.6-sol --judge-model gpt-5.6-sol --workers 2
if __name__ == "__main__":
    main()
