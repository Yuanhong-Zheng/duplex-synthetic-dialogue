#!/usr/bin/env python3
"""Generate and semantically review Stage-3 offline episode scripts.

The source run supplies frozen Stage-1/2 state. This command deliberately
stops after ``episode_script`` and never generates actor output, a rubric, an
audit, or a rollout.
"""
# ── Stage 3: 剧本生成脚本 ──────────────────────────────────────────
# 本脚本负责生成 5-15 个有序用户轮次 (U01-U15) 的 episode script，
# 并通过 Judge/Repair 循环确保剧本的结构合规与语义质量。
#
# 核心流程：生成初稿 → 结构验证 → 语义审查(Judge) → 定向修复(Repair) → 重新审查
# - Repair 使用零温度 (temperature=0.0) 以确保修复是确定性的，不会在修一个问题的同时引入新分支
# - 最多尝试 MAX_ATTEMPTS=3 轮，超过则保留最后候选并标记为 failed
#
# 冻结控制层设计：
# - 剧本 (episode_script) 是冻结的，下游 Stage 只能改变表层措辞，不能改变行为/时机/轮次结构
# - 这保证了评测的可复现性：同一剧本在不同 rollout 中产生相同的对话骨架
#
# Act 行为体系（剧本中每个轮次必须引用一个 Act）：
#   A01 建立任务   — 用户发起或确认任务目标
#   A02 控制播报   — 用户打断/控制 AI 的播报（必须搭配 T03 播报中途起声）
#   A03 追问/澄清  — 用户追问细节或要求澄清
#   A04 更新状态   — 用户提供新信息或修正先前信息
#   A05 异议/拒绝  — 用户对 AI 的建议表示异议或拒绝
#   A06 话题管理   — 用户切换话题或结束对话
#
# Timing 时机体系（每个轮次必须引用至少一个 Timing）：
#   T01 自然接话       — 用户在 AI 话轮结束后自然接话
#   T02 句内停顿       — 用户在 AI 句中停顿处插入（必须搭配 pause_cue 字段）
#   T03 播报中途起声   — 用户在 AI 播报中途直接起声打断
#
# Act/Timing 耦合规则：
#   A02(控制播报) 必须搭配 T03(播报中途起声)，因为控制播报的本质就是中途打断
#   T02(句内停顿) 必须搭配 pause_cue 字段，标明停顿触发点
# ────────────────────────────────────────────────────────────────────
from __future__ import annotations

from judge_output_repair import complete_checked_judge

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import html
import json
from pathlib import Path
import time

# MiniMaxClient: 统一的 LLM API 客户端，支持 MiniMax/GPT 等多模型端点
# parse_json_text: 从 LLM 原始文本中提取 JSON，支持 markdown 代码块包裹
from minimax_client import MiniMaxClient, parse_json_text
from project_paths import resolve_run_reference
# run_pipeline 公共工具：
#   JSON_RESPONSE_FORMAT — 强制 LLM 输出 JSON 的 response_format 参数
#   STAGE_MAX_TOKENS     — 各阶段的最大输出 token 数配置
#   _prepare_stage_value — 对 LLM 输出做后处理（如填充默认值、规范化字段）
#   _redact_secret       — 脱敏 API key，防止泄露到日志/文件
#   _write_json_atomic   — 原子写入 JSON 文件（先写临时文件再 rename，防止半写）
#   _write_text_atomic   — 原子写入文本文件
#   digest               — SHA-256 摘要，用于校验文件完整性
#   stage_context        — 构建某阶段的 LLM 上下文（scene + prior stages）
#   stage_prompt         — 加载某阶段的 prompt 模板
from run_pipeline import (
    JSON_RESPONSE_FORMAT,
    STAGE_MAX_TOKENS,
    _prepare_stage_value,
    _redact_secret,
    _write_json_atomic,
    _write_text_atomic,
    digest,
    stage_context,
    stage_prompt,
)
# stage_validation: 统一的结构校验引擎，根据阶段名和上下文校验 JSON 结构
from stage_validation import validate_stage

# ROOT: 脚本所在目录，用于定位 prompts/ 等相对路径资源
ROOT = Path(__file__).parent
# SCRIPT_KEYS: 剧本输出必须包含的唯一顶层键 — episode_turns 轮次列表
SCRIPT_KEYS = {"episode_turns"}
# JUDGE_KEYS: Judge 输出必须包含的两个顶层键 — decision(判定决策) + findings(问题清单)
JUDGE_KEYS = {"decision", "findings"}
# MAX_ATTEMPTS: Judge/Repair 循环最大尝试次数
# 设为 3 是经验值：1 次通常不够（初稿常有结构问题），超过 3 次收益递减且耗时过长
MAX_ATTEMPTS = 3
# JUDGE_MAX_TOKENS: Judge 模型的最大输出 token 数
# 需要容纳 findings 数组（每条含 5 个字段），3600 足以覆盖 10+ 条 findings
JUDGE_MAX_TOKENS = 3600


def _normalize_timing_shape(value: dict) -> list[str]:
    """Do not infer or rewrite model-authored timing semantics.

    Invalid timing combinations remain visible to ``validate_stage`` and the
    targeted repair prompt. Choosing T01/T03 or dropping T02 here would change
    the intended duplex event rather than merely validate its JSON shape.

    不推断或改写模型生成的 timing 语义。

    设计理由：timing 组合（如 T01/T03 同时出现）表达了模型对双工事件的设计意图，
    自动修正会改变语义而非仅校验 JSON 结构。无效组合应留给 validate_stage
    检测，再由 repair prompt 定向修复，这样修复是有据可依的。

    例如：如果模型同时指定了 T01(自然接话) 和 T03(播报中途起声)，
    这在逻辑上矛盾（不可能既自然接话又中途打断），但本函数不做判断，
    而是让后续的结构验证和 Judge/Repair 循环来处理。

    Args:
        value: 待规范化的剧本字典（包含 episode_turns 等）

    Returns:
        规范化操作列表（本函数始终返回空列表，即不做任何规范化）
    """
    # 设计决策：不对 timing 做任何推断或改写。
    # 原因：timing 组合（如 T01/T03 同时出现）表达了模型对双工事件的设计意图，
    # 自动修正会改变语义而非仅校验 JSON 结构。无效组合应留给 validate_stage
    # 检测，再由 repair prompt 定向修复，这样修复是有据可依的。
    return []


def _source_cases(source_run: Path, selected: set[str] | None) -> list[tuple[str, dict]]:
    """从源运行目录加载冻结的 Stage-1/2 数据，为每个 case 构建生成上下文。

    Args:
        source_run: 源运行目录路径，包含 manifest.json 和各 case 子目录
        selected: 指定要处理的 case_id 集合；None 表示处理全部

    Returns:
        [(case_id, source_dict), ...] 列表，每个 source_dict 包含
        sampled_scene、scene_task、user_voice、situation_knowledge 和 source_run

    设计要点：
        - 支持两级 manifest 引用：Stage-only manifest 通过 source_run 指回
          完整采样 manifest 获取场景来源信息
        - 对 situation_knowledge 做严格校验，确保与当前六字段契约兼容；
          旧版 Stage-2 数据必须重新生成，而非静默泄露已废弃字段
    """
    manifest = json.loads((source_run / "manifest.json").read_text(encoding="utf-8"))
    base_source_run = source_run
    source_ref = manifest.get("source_run")
    if isinstance(source_ref, str):
        resolved_source = resolve_run_reference(source_ref, source_run)
        if resolved_source is not None:
            base_source_run = resolved_source
    scenes = manifest.get("cases")
    if not isinstance(scenes, list):
        # Stage-only manifest 只含 case_ids，需回溯到完整采样 manifest 获取场景来源
        if base_source_run != source_run:
            source_manifest_path = base_source_run / "manifest.json"
            if source_manifest_path.exists():
                source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
                scenes = source_manifest.get("cases")
        if not isinstance(scenes, list):
            # 最终兜底：从 case_ids 列表构造最小场景条目
            scenes = [{"case_id": case_id} for case_id in manifest.get("case_ids", [])]
    if not isinstance(scenes, list) or not scenes:
        raise ValueError(f"source manifest has no cases: {source_run / 'manifest.json'}")
    result: list[tuple[str, dict]] = []
    for sampled_scene in scenes:
        if not isinstance(sampled_scene, dict) or not sampled_scene.get("case_id"):
            raise ValueError("source manifest contains an invalid case")
        case_id = str(sampled_scene["case_id"])
        if selected is not None and case_id not in selected:
            continue
        case_dir = source_run / case_id
        base_case_dir = base_source_run / case_id
        paths = {
            "scene_task": case_dir / "scene_task.json",
            "user_voice": case_dir / "user_voice.json",
            "situation_knowledge": case_dir / "situation_knowledge.json",
        }
        # 回退策略：当前运行目录缺失时，从基础源运行目录读取
        for name in ("scene_task", "user_voice"):
            if not paths[name].exists():
                paths[name] = base_case_dir / f"{name}.json"
        if not all(path.exists() for path in paths.values()):
            raise ValueError(f"{case_id} is missing frozen Stage-1/2 artifacts")
        scene_task = json.loads(paths["scene_task"].read_text(encoding="utf-8"))
        user_voice = json.loads(paths["user_voice"].read_text(encoding="utf-8"))
        situation = json.loads(paths["situation_knowledge"].read_text(encoding="utf-8"))
        if not all(isinstance(value, dict) for value in (scene_task, user_voice, situation)):
            raise ValueError(f"{case_id} has invalid frozen artifacts")
        scene = scene_task.get("scene")
        if not isinstance(scene, dict):
            raise ValueError(f"{case_id} scene_task.scene is not an object")
        # 严格校验 situation_knowledge 与当前六字段契约的兼容性
        # 旧版 Stage-2 数据必须重新生成，不能静默泄露已废弃字段到 Stage 3
        situation_errors = validate_stage(
            "situation_knowledge",
            situation,
            stage_context(case_id, "situation_knowledge", scene, {
                "scene_task": scene_task,
                "user_voice": user_voice,
                "situation_knowledge": situation,
            }),
        )
        if situation_errors:
            raise ValueError(f"{case_id} frozen situation_knowledge is incompatible: {'; '.join(situation_errors[:6])}")
        result.append((case_id, {
            "sampled_scene": copy.deepcopy(sampled_scene),
            "scene_task": scene_task,
            "user_voice": user_voice,
            "situation_knowledge": situation,
            "source_run": str(source_run),
        }))
    if not result:
        raise ValueError("no source cases selected")
    return result


def _judge_errors(value: object) -> list[str]:
    """校验 Judge 输出的结构合规性。

    Args:
        value: Judge 模型返回的 JSON 对象

    Returns:
        错误列表，空列表表示合规

    设计要点：
        - decision 只允许 "pass" 或 "repair"，不允许 "skip" 等模糊状态
        - findings 中每条必须包含 severity/path/problem/evidence/repair 五字段
        - severity 必须为 "must_fix"，确保所有发现都是可操作的
        - pass 时 findings 必须为空，repair 时 findings 至少一条，防止逻辑矛盾
    """
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["judge output must be an object"]
    if set(value) != JUDGE_KEYS:
        errors.append("judge output must contain exactly decision and findings")
    if value.get("decision") not in {"pass", "repair"}:
        errors.append("judge decision must be pass or repair")
    findings = value.get("findings")
    if not isinstance(findings, list):
        return errors + ["judge findings must be an array"]
    fields = {"severity", "path", "problem", "evidence", "repair"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{index}] must be an object")
            continue
        if set(finding) != fields:
            errors.append(f"findings[{index}] has an invalid field set")
        if finding.get("severity") != "must_fix":
            errors.append(f"findings[{index}].severity must be must_fix")
        for key in ("path", "problem", "evidence", "repair"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                errors.append(f"findings[{index}].{key} must be non-empty")
    if value.get("decision") == "pass" and findings:
        errors.append("judge pass must have an empty findings array")
    if value.get("decision") == "repair" and not findings:
        errors.append("judge repair must contain at least one finding")
    return errors


def _base_context(source: dict, case_id: str) -> dict:
    """构建 episode_script 阶段的基础上下文，包含 Stage-1/2 的全部冻结数据。

    Args:
        source: 包含 scene_task、user_voice、situation_knowledge 的源数据字典
        case_id: 当前 case 的标识符

    Returns:
        供 LLM 使用的完整上下文字典，由 stage_context() 组装
    """
    scene_task = source["scene_task"]
    scene = scene_task["scene"]
    prior = {
        "scene_task": source["scene_task"],
        "user_voice": source["user_voice"],
        "situation_knowledge": source["situation_knowledge"],
    }
    return stage_context(case_id, "episode_script", scene, prior)


def _judge_input(context: dict, script: dict) -> dict:
    """构建 Judge 审查的输入：基础上下文 + 待审查的剧本。

    Args:
        context: 基础上下文字典
        script: 待审查的 episode_script 候选

    Returns:
        Judge 输入字典，深拷贝以避免副作用
    """
    return {**copy.deepcopy(context), "episode_script": copy.deepcopy(script)}


def _repair_input(context: dict, script: dict, findings: list[dict]) -> dict:
    """构建 Repair 修复的输入：基础上下文 + 当前剧本 + Judge 发现的问题。

    Args:
        context: 基础上下文字典
        script: 当前需要修复的 episode_script
        findings: Judge 标记的 must_fix 问题列表

    Returns:
        Repair 输入字典，包含 current_episode_script 和 judge_findings
    """
    return {
        **copy.deepcopy(context),
        "current_episode_script": copy.deepcopy(script),
        "judge_findings": copy.deepcopy(findings),
    }


def _generate_one(
    case_id: str,
    source: dict,
    run_dir: Path,
    client: MiniMaxClient,
    initial_script: dict | None = None,
    repair_existing: bool = False,
) -> dict:
    """为单个 case 生成并审查 episode script（Stage 3 核心函数）。

    执行 Judge/Repair 循环：生成初稿 → 结构验证 → 语义审查 → 定向修复 → 重新审查，
    最多 MAX_ATTEMPTS 轮。通过后写入 episode_script.json，否则写入候选文件。

    Args:
        case_id: case 标识符
        source: Stage-1/2 冻结数据（scene_task, user_voice, situation_knowledge）
        run_dir: 输出运行目录
        client: MiniMax API 客户端（用于生成和修复）
        initial_script: 已有剧本（repair_existing 模式下使用）
        repair_existing: 是否对已有候选剧本执行 Judge/Repair 而非重新生成

    Returns:
        包含 case_id、episode_script、validation、模型信息的结果字典

    设计要点：
        - Repair 使用 temperature=0.0 确保修复是确定性的，不会在修一个问题的同时
          发明新的剧情分支
        - Judge 也使用 temperature=0.0 确保审查结果稳定可复现
        - 结构验证失败时自动构造 findings 并进入 repair 流程
        - Judge 输出格式不合规时视为 invalid，不采纳其 findings
    """
    case_dir = run_dir / case_id
    cache_dir = case_dir / "stage_cache" / "episode_script"
    case_dir.mkdir(parents=True, exist_ok=True)
    context = _base_context(source, case_id)
    prompt = stage_prompt("episode_script", context["scene"])
    # ── 温度策略 ──
    # Repair 使用零温度 (temperature=0.0)：确保修复是确定性的，
    # 不会在修一个问题的同时发明新的剧情分支。初稿保留创造性温度。
    repair_client = client.for_model(client.model, temperature=0.0)
    # Judge 客户端：GPT 系列走专用端点，其他模型使用零温度确保审查稳定可复现
    if client.judge_model.lower().startswith("gpt-"):
        judge_client = MiniMaxClient.judge_from_env(client.judge_model)
    else:
        judge_client = client.for_model(client.judge_model, temperature=0.0)
    judge_prompt = (ROOT / "prompts" / "03_episode_script_judge.md").read_text(encoding="utf-8")
    repair_prompt = (ROOT / "prompts" / "03_episode_script_repair.md").read_text(encoding="utf-8")
    script: dict | None = copy.deepcopy(initial_script) if initial_script is not None else None
    generation_raw = json.dumps(script, ensure_ascii=False, indent=2) if script is not None else ""
    generation_meta: dict = {"model": client.model, "mode": "repair_existing"} if script is not None else {}
    attempts: list[dict] = []
    final_errors: list[str] = []
    structural_normalizations: list[str] = []
    pending_findings: list[dict] | None = None

    if repair_existing and script is not None:
        # 修复前保存原始剧本快照，便于对比修复前后差异
        _write_json_atomic(case_dir / "episode_script.before_repair.json", script)

    # ── Judge/Repair 循环 ──
    # 每轮：生成/修复 → 结构验证 → 语义审查(Judge) → 判定
    # - 结构验证失败：自动构造 findings，进入下一轮 repair
    # - Judge 判定 pass：循环结束
    # - Judge 判定 repair：携带 findings 进入下一轮 repair
    # - 达到 MAX_ATTEMPTS：保留最后候选，标记为 failed
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if script is None:
            # 首轮：生成全新初稿
            raw, meta = client.complete(
                (prompt),
                context,
                max_tokens=STAGE_MAX_TOKENS["episode_script"],
                response_format=JSON_RESPONSE_FORMAT,
            )
            candidate_raw, candidate_meta = raw, meta
        elif pending_findings is not None:
            # 后续轮次：基于 Judge findings 执行定向修复（零温度）
            findings = pending_findings
            repair_input = _repair_input(context, script, findings)
            raw, meta = repair_client.complete(
                (repair_prompt),
                repair_input,
                max_tokens=STAGE_MAX_TOKENS["episode_script"],
                response_format=JSON_RESPONSE_FORMAT,
            )
            candidate_raw, candidate_meta = raw, meta
            repair_dir = cache_dir / "repair"
            _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-input.json", repair_input)
            _write_text_atomic(repair_dir / f"attempt-{attempt:02d}-prompt.md", repair_prompt)
            _write_text_atomic(repair_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(raw, repair_client))
        else:
            # repair_existing 模式且无 pending_findings：直接使用已有剧本
            candidate_raw, candidate_meta = generation_raw, generation_meta
        try:
            candidate = parse_json_text(candidate_raw, SCRIPT_KEYS)
        except Exception as exc:
            # JSON 解析失败：记录错误，构造带 _parse_error 标记的候选
            candidate = {"_parse_error": str(exc)}
        candidate = _prepare_stage_value("episode_script", candidate, context)
        structural_normalizations.extend(_normalize_timing_shape(candidate))
        # ── 结构验证 ──
        structural_errors = validate_stage("episode_script", candidate, context)
        if structural_errors:
            # 结构验证失败：自动构造 findings 并进入 repair 流程
            # 不直接丢弃候选，而是让 repair prompt 定向修复结构问题
            final_errors = list(structural_errors)
            attempts.append({
                "attempt": attempt,
                "decision": "structural_failure",
                "findings": [{
                    "severity": "must_fix",
                    "path": "/episode_turns",
                    "problem": "结构校验失败",
                    "evidence": "; ".join(structural_errors[:6]),
                    "repair": "修复 JSON 结构、轮次 ID、Act/Timing 和 pause_cue。",
                }],
                "model": candidate_meta.get("model", client.model),
            })
            script = candidate
            generation_raw, generation_meta = candidate_raw, candidate_meta
            # 将结构错误转为 findings，供下一轮 repair 使用
            pending_findings = attempts[-1]["findings"]
            continue

        # ── 语义审查 (Judge) ──
        judge_input = _judge_input(context, candidate)
        judge_raw, judge_meta = complete_checked_judge(judge_client, judge_prompt, judge_input, keys=JUDGE_KEYS, validator=_judge_errors, directory=cache_dir / "judge" / f"format-{attempt:02d}", max_tokens=JUDGE_MAX_TOKENS)
        try:
            judge_value = parse_json_text(judge_raw, JUDGE_KEYS)
        except Exception as exc:
            judge_value = {"_parse_error": str(exc)}
        judge_errors = _judge_errors(judge_value)
        judge_dir = cache_dir / "judge"
        # 保存 Judge 审查的完整中间产物，便于调试和审计
        _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-input.json", judge_input)
        _write_text_atomic(judge_dir / f"attempt-{attempt:02d}-prompt.md", judge_prompt)
        _write_text_atomic(judge_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(judge_raw, judge_client))
        _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-review.json", judge_value)
        _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-validation.json", {"errors": judge_errors})
        if judge_errors:
            # Judge 输出格式不合规：视为 invalid，不采纳其 findings
            # 下一轮将重新生成（而非基于错误 findings 修复）
            final_errors = [f"invalid script judge response: {error}" for error in judge_errors]
            attempts.append({"attempt": attempt, "decision": "invalid", "findings": [], "errors": judge_errors})
            script = candidate
            generation_raw, generation_meta = candidate_raw, candidate_meta
            pending_findings = attempts[-1]["findings"]
            continue
        # 只保留 must_fix 级别的 findings，忽略建议性意见
        findings = [item for item in judge_value["findings"] if item.get("severity") == "must_fix"]
        attempts.append({
            "attempt": attempt,
            "decision": judge_value["decision"],
            "finding_count": len(judge_value["findings"]),
            "findings": findings,
            "judge_model": judge_meta.get("model", judge_client.model),
            "response_sha256": digest(judge_raw),
        })
        script = candidate
        generation_raw, generation_meta = candidate_raw, candidate_meta
        if judge_value["decision"] == "pass":
            # Judge 判定通过：清空错误，退出循环
            final_errors = []
            break
        # Judge 判定需要修复：记录问题，准备下一轮 repair
        final_errors = [finding["problem"] for finding in findings]
        pending_findings = findings
        if attempt == MAX_ATTEMPTS:
            # 达到最大尝试次数：退出循环，保留最后候选
            break

    # ── 结果判定与持久化 ──
    passed = bool(script is not None and not final_errors)
    if script is None:
        # 极端情况：连空候选都没有，构造空轮次列表
        script = {"episode_turns": []}
    safe_raw = _redact_secret(generation_raw, client)
    _write_json_atomic(case_dir / "input.json", context)
    _write_text_atomic(case_dir / "prompt.md", prompt)
    _write_text_atomic(case_dir / "raw_response.txt", safe_raw)
    validation = {
        "status": "passed" if passed else "failed",
        "structural_status": "passed" if not validate_stage("episode_script", script, context) else "failed",
        "semantic_status": "passed" if passed else "failed",
        "errors": final_errors,
        "judge_attempts": attempts,
        "max_attempts": MAX_ATTEMPTS,
        "structural_normalizations": structural_normalizations,
    }
    if passed:
        # 通过：写入正式剧本文件
        _write_json_atomic(case_dir / "episode_script.json", script)
    else:
        # 未通过：写入候选文件，保留供人工审查
        _write_json_atomic(case_dir / "episode_script.candidate.json", script)
    _write_json_atomic(case_dir / "validation.json", validation)
    _write_json_atomic(case_dir / "metadata.json", {
        "source_run": source["source_run"],
        "model_identity": {"provider": client.provider, "model": client.model},
        "judge_model_identity": {"provider": judge_client.provider, "model": judge_client.model},
        "prompt_sha256": digest(prompt),
        "input_sha256": digest(context),
        "response_sha256": digest(safe_raw),
        "api_key_serialized": False,
        "judge_attempts": attempts,
        "structural_normalizations": structural_normalizations,
    })
    return {
        "case_id": case_id,
        "sampled_scene": source["sampled_scene"],
        # 即使语义 Judge 在修复预算内仍未通过，也保留最后结构有效的候选。
        # 该产物命名为 episode_script.candidate.json，不视为已发布剧本；
        # HTML 报告仅作审查用途。
        "episode_script": script,
        "validation": validation,
        "model": client.model,
        "provider": client.provider,
        "judge_model": judge_client.model,
    }


def _render_html(run_dir: Path, run_id: str, results: list[dict], failures: list[str] | None = None) -> Path:
    """将生成结果渲染为可审查的 HTML 报告。

    Args:
        run_dir: 运行输出目录
        run_id: 运行标识符
        results: 各 case 的生成结果列表
        failures: 模型请求失败的 case 列表（网络/配置错误，非语义判定）

    Returns:
        HTML 报告文件路径

    设计要点：
        - 每个 case 渲染为一张卡片，包含轮次表格和 Judge/validation 详情
        - 失败 case 区分"模型请求失败"和"语义审查未通过"两种类型
        - 表格列：ID / Act / Timing / Interrupt Anchor / 用户目标 / 用户台词 / Assistant 预期语义回复
    """
    cards: list[str] = []
    for result in results:
        scene = result["sampled_scene"]
        script = result.get("episode_script", {})
        validation = result["validation"]
        turns = script.get("episode_turns", []) if isinstance(script, dict) else []
        rows = []
        for turn in turns:
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(turn.get('turn_id', '')))}</td>"
                f"<td>{html.escape(str(turn.get('act_ref', '')))}</td>"
                f"<td>{html.escape(', '.join(turn.get('timing_refs', [])))}</td>"
                f"<td>{html.escape(str(turn.get('interrupt_anchor', '')))}</td>"
                f"<td>{html.escape(str(turn.get('semantic_goal', '')))}</td>"
                f"<td>{html.escape(str(turn.get('user_utterance', '')))}</td>"
                f"<td>{html.escape(str(turn.get('assistant_semantic_goal', '')))}</td>"
                "</tr>"
            )
        passed = validation["status"] == "passed"
        status = "通过" if passed else "未通过（保留最后候选）"
        status_class = "ok" if passed else "bad"
        cards.append(f"""
        <article class='case'>
          <header><b>{html.escape(result['case_id'])}</b><span class='status {status_class}'>{status}</span></header>
          <h2>{html.escape(str(scene.get('domain', '')))} / {html.escape(str(scene.get('scenario', '')))}</h2>
          <p class='meta'>{html.escape(str(scene.get('domain_id', '')))} · {html.escape(str(scene.get('scenario_id', '')))} · {len(turns)} turns</p>
          <table><thead><tr><th>ID</th><th>Act</th><th>Timing</th><th>Interrupt Anchor</th><th>用户目标</th><th>用户台词</th><th>Assistant 预期语义回复</th></tr></thead>
          <tbody>{''.join(rows)}</tbody></table>
          <details><summary>Judge / validation</summary><pre>{html.escape(json.dumps(validation, ensure_ascii=False, indent=2))}</pre></details>
        </article>""")
    for failure in failures or []:
        case_id, _, message = failure.partition(": ")
        cards.append(f"""
        <article class='case failure'>
          <header><b>{html.escape(case_id)}</b><span class='status bad'>模型请求失败</span></header>
          <p class='meta'>未生成可审查的 episode script；这是网络/配置错误，不是 Judge 的语义判定。</p>
          <pre>{html.escape(message or failure)}</pre>
        </article>""")
    model = results[0].get("model", "") if results else ""
    provider = results[0].get("provider", "") if results else ""
    judge_model = results[0].get("judge_model", "") if results else ""
    output = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Stage-3 Offline Episode Scripts</title><style>
*{{box-sizing:border-box}}body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f5f7;color:#202124;margin:0;padding:24px;line-height:1.55}}main{{max-width:1500px;margin:auto}}.case{{background:#fff;border:1px solid #dfe3e8;border-radius:8px;margin:0 0 24px;padding:20px;box-shadow:0 2px 8px #0000000b;overflow:hidden}}header{{display:flex;justify-content:space-between;font-size:18px}}.status{{color:#16803c}}.status.bad{{color:#a12828}}h2{{font-size:18px;margin:12px 0 4px}}.meta{{color:#697386;font-size:13px}}table{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px}}th,td{{border-top:1px solid #e5e7eb;padding:10px 9px;vertical-align:top;text-align:left;overflow-wrap:anywhere;word-break:break-word;line-height:1.55}}th{{background:#f8fafc;color:#586875;font-size:12px}}th:nth-child(1){{width:5%}}th:nth-child(2){{width:5%}}th:nth-child(3){{width:7%}}th:nth-child(4){{width:17%}}th:nth-child(5){{width:20%}}th:nth-child(6){{width:26%}}th:nth-child(7){{width:20%}}pre{{white-space:pre-wrap;overflow:auto}}@media(max-width:800px){{body{{padding:12px}}.case{{padding:14px}}table{{display:block;overflow-x:auto;font-size:12px}}}}</style></head><body><main><h1>Stage-3 Offline Episode Scripts</h1><p>Model: {html.escape(str(provider))} / {html.escape(str(model))} · Judge: {html.escape(str(judge_model))} · max attempts: {MAX_ATTEMPTS}</p>{''.join(cards)}</main></body></html>"""
    path = run_dir / "episode-script-samples.html"
    _write_text_atomic(path, output)
    return path


def main() -> None:
    """Stage 3 主入口：批量生成并审查 episode scripts。

    流程：
        1. 解析命令行参数（源运行、模型、并发数等）
        2. 加载源 case 的冻结 Stage-1/2 数据
        3. 并发执行 _generate_one（生成 → Judge/Repair 循环）
        4. 汇总结果，写入 manifest 和 HTML 报告
        5. 若有 case 失败则抛出 RuntimeError

    关键参数：
        --repair-existing: 对已有候选剧本执行 Judge/Repair，而非重新生成
        --model: 生成模型（gpt-* 走 GPT 兼容端点）
        --judge-model: 语义审查模型
        --workers: 并发线程数
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S") + "-episode-script")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--cases", help="comma-separated case IDs; default is all source cases")
    parser.add_argument(
        "--repair-existing",
        action="store_true",
        help="judge and targeted-repair existing candidate scripts instead of sampling new drafts",
    )
    parser.add_argument("--model", default="gpt-5.6-sol", help="generation model; gpt-* uses the GPT-compatible endpoint")
    parser.add_argument("--judge-model", default="gpt-5.6-sol", help="semantic Judge model")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    selected = {item.strip() for item in args.cases.split(",")} if args.cases else None
    source_run = args.source_run.resolve()
    sources = _source_cases(source_run, selected)
    client = MiniMaxClient.from_env()
    if args.model:
        if args.model.lower().startswith("gpt-"):
            client = MiniMaxClient.judge_from_env(args.model, temperature=1.0)
        else:
            client = client.for_model(args.model)
    if args.judge_model:
        client.judge_model = args.judge_model
    run_dir = ROOT / "runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    existing_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")) if (run_dir / "manifest.json").exists() else {}
    results: list[dict] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {}
        for case_id, source in sources:
            initial_script = None
            if args.repair_existing:
                # repair_existing 模式：加载已有剧本作为修复起点
                # 优先使用已通过的正式剧本，其次使用未通过的候选剧本
                # 已通过的正式剧本具有权威性，不应被候选覆盖
                existing_path = run_dir / case_id / "episode_script.json"
                if not existing_path.exists():
                    existing_path = run_dir / case_id / "episode_script.candidate.json"
                if not existing_path.exists():
                    failures.append(f"{case_id}: missing existing episode script candidate")
                    continue
                initial_script = json.loads(existing_path.read_text(encoding="utf-8"))
            jobs[executor.submit(
                _generate_one,
                case_id,
                source,
                run_dir,
                client,
                initial_script,
                args.repair_existing,
            )] = case_id
        for job in as_completed(jobs):
            case_id = jobs[job]
            try:
                results.append(job.result())
            except Exception as exc:
                failures.append(f"{case_id}: {_redact_secret(str(exc), client)}")
    results.sort(key=lambda item: item["case_id"])
    # 写入运行 manifest，记录本次生成的完整元数据
    _write_json_atomic(run_dir / "manifest.json", {
        "run_id": args.run_id,
        "source_run": str(source_run),
        "stage": "episode_script",
        "case_ids": existing_manifest.get("case_ids", [case_id for case_id, _ in sources]) if args.repair_existing else [case_id for case_id, _ in sources],
        "workers": args.workers,
        "model": client.model,
        "provider": client.provider,
        "judge_model": client.judge_model,
        "max_attempts": MAX_ATTEMPTS,
        "rollout_generated": False,
        "actor_outputs_generated": False,
        "failures": failures,
    })
    _render_html(run_dir, args.run_id, results, failures)
    if failures:
        # 模型请求失败（网络/配置错误）：直接抛出异常
        raise RuntimeError("; ".join(sorted(failures)))
    failed = [item["case_id"] for item in results if item["validation"]["status"] != "passed"]
    if failed:
        # 语义审查未通过：列出失败的 case_id
        raise RuntimeError("episode_script validation failed for: " + ", ".join(failed))
    print(run_dir / "episode-script-samples.html")


if __name__ == "__main__":
    main()
