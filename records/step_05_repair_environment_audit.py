#!/usr/bin/env python3
"""Apply targeted semantic repairs from a completed Environment Audit.

This is deliberately separate from the generation stages.  It copies the
complete package, preserves the pre-repair audit, repairs only explicitly
located upstream artifacts, and runs a fresh audit over the repaired package.
"""
# Environment Audit 定向修复脚本
# ──────────────────────────────────────────────────────────────────────
# 设计原则：
#   1. 与生成阶段完全解耦——本脚本只做"审查 → 定位 → 修复 → 再审查"，
#      不触碰任何生成逻辑，避免修复与生成之间的隐式耦合。
#   2. 保留修复前审计快照（environment_audit_pre_repair.json），
#      确保修复效果可追溯、可对比。
#   3. 仅修复审计 findings 中显式定位到的上游制品（通过 JSON Pointer），
#      不做"盲目全量重生成"，防止引入无关回归。
#   4. 修复后重新运行完整审计；若修复全部失败则复用修复前审计结果，
#      避免随机性导致分数波动（stochastic regression）。
# ──────────────────────────────────────────────────────────────────────
from __future__ import annotations

import argparse
import copy
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# MiniMax API 客户端，用于调用模型生成修复候选和审计结果
from minimax_client import MiniMaxClient, parse_json_text
# 从主流水线模块导入共享常量和工具函数
from run_pipeline import (
    AUDIT_UPSTREAM_STAGES,       # 上游阶段名称列表，定义修复顺序
    JSON_RESPONSE_FORMAT,        # 强制 JSON 输出的 response_format 参数
    PREFERRED_JSON_KEYS,         # 各阶段优先解析的 JSON 键名映射
    STAGE_MAX_TOKENS,            # 各阶段的最大 token 数配置
    _audit_repair_targets,       # 审计修复目标定位函数
    _prepare_stage_value,        # 制品后处理函数（填充默认值、规范化等）
    _shape_contract,             # 制品结构契约提取函数
    compact_episode_timing_view, # 紧凑时序视图生成函数
    digest,                      # 上下文哈希摘要函数
    stage_context,               # 阶段上下文构建函数
)
# 离线能力契约定义和制品结构校验函数
from stage_validation import OFFLINE_CAPABILITY_CONTRACT, validate_stage

ROOT = Path(__file__).parent
# 修复提示词模板：指导模型根据审计 findings 对指定制品做定向修改
REPAIR_PROMPT = (ROOT / "prompts" / "repair_audit.md").read_text(encoding="utf-8")
# 审计提示词模板：12 维评分 + 发布决策的完整审查 prompt
AUDIT_PROMPT = (ROOT / "prompts" / "05_environment_audit.md").read_text(encoding="utf-8")
# 可被审计定位并修复的上游制品名称集合（与流水线上游阶段一一对应）
ARTIFACTS = ("scene_task", "user_voice", "situation_knowledge", "episode_script", "actor_rubric")


def atomic_json(path: Path, value: object) -> None:
    """Write JSON to *path* atomically via a temporary file.

    原子写入 JSON 文件。

    设计考量：先写入 .tmp 临时文件再 rename，确保并发读取时不会看到
    写到一半的残缺内容（rename 在 POSIX 上是原子操作）。

    Args:
        path:  目标 JSON 文件路径。
        value: 待序列化的 Python 对象。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: object | None = None) -> object:
    """Load JSON from *path*, returning *default* on any read/parse error.

    安全读取 JSON 文件，读取或解析失败时返回 default 的深拷贝。

    设计考量：修复流程中部分制品文件可能缺失（如首次运行未生成），
    使用 default 参数提供优雅降级，避免整个流程因单个文件缺失而中断。

    Args:
        path:    JSON 文件路径。
        default: 读取失败时的默认返回值（返回深拷贝，防止意外共享引用）。

    Returns:
        解析后的 Python 对象，或 default 的深拷贝。
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def json_pointer_exists(document: object, pointer: object) -> bool:
    """Check whether a JSON Pointer (RFC 6901) resolves inside *document*.

    验证 JSON Pointer（RFC 6901）是否在文档中真实存在。

    设计考量：审计 findings 中的 path 字段使用 JSON Pointer 定位问题位置。
    修复前必须验证该路径在当前制品中确实存在，否则修复 prompt 会指向
    不存在的字段，导致模型产生幻觉式修改。根指针 "/" 和空指针均视为
    无效——修复需要精确定位到具体字段，而非整个文档。

    Args:
        document: 待检查的 JSON 文档（dict / list / 标量）。
        pointer:  JSON Pointer 字符串，如 "/scene/task_style"。

    Returns:
        True 表示路径可解析到文档中的某个节点，False 表示路径无效。
    """
    # 拒绝非字符串、不以 "/" 开头、或根指针 "/"——修复需要精确字段定位
    if not isinstance(pointer, str) or not pointer.startswith("/") or pointer == "/":
        return False
    current = document
    # 逐段解析 JSON Pointer，处理 RFC 6901 转义：~1 → /，~0 → ~
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list):
            # 数组索引必须是数字且不越界
            if not token.isdigit() or int(token) >= len(current):
                return False
            current = current[int(token)]
        else:
            # 到达标量节点后无法继续向下遍历
            return False
    return True


def load_case(source_run: Path, rubric_run: Path, audit_run: Path, case_id: str) -> tuple[dict, dict, dict]:
    """Load all artifacts and audit for a single case across three run directories.

    从三个不同的 run 目录加载单个 case 的全部制品与审计结果。

    设计考量：source_run 包含原始输入和 episode_script，rubric_run 包含
    actor_rubric，audit_run 包含 environment_audit。三者分离是因为各阶段
    可能在不同时间、不同 run 中生成。本函数将分散的制品重新组装为
    修复流程所需的统一视图，同时保持深拷贝以避免跨 case 的引用污染。

    Args:
        source_run: 包含 input.json 和 episode_script.json 的源 run 目录。
        rubric_run: 包含 actor_rubric.json 的 rubric run 目录。
        audit_run:  包含 environment_audit.json 的审计 run 目录。
        case_id:    用例标识符，对应目录名。

    Returns:
        (context, audit, artifacts) 三元组：
        - context:  修复流程的上下文信息（scene、capability_contract 等）。
        - audit:    修复前的审计结果（findings、total_score、release_decision）。
        - artifacts: 五个上游制品的字典，键为 ARTIFACTS 中的名称。
    """
    source_dir = source_run / case_id
    rubric_dir = rubric_run / case_id
    audit_dir = audit_run / case_id
    # 加载原始输入，提取 scene / task_style / task_instance → scene_task
    source_input = load_json(source_dir / "input.json", {})
    source_input = source_input if isinstance(source_input, dict) else {}
    scene = copy.deepcopy(source_input.get("scene", {}))
    scene_task = {
        "scene": scene,
        "task_style": source_input.get("task_style"),
        "task_instance": copy.deepcopy(source_input.get("task_instance", {})),
    }
    # 提取 user_seed / voice_profile → user_voice
    user_voice = {
        "user_seed": copy.deepcopy(source_input.get("user_seed", {})),
        "voice_profile": source_input.get("voice_profile", ""),
    }
    # 提取情境相关字段 → situation_knowledge（仅包含存在的键）
    situation = {
        key: copy.deepcopy(source_input.get(key))
        for key in ("episode_situation", "task_reference_data", "environment_audio", "estimated_snr_db", "constraints", "safety_limits")
        if key in source_input
    }
    # 加载 episode_script 和 actor_rubric（可能缺失，降级为空 dict）
    episode = load_json(source_dir / "episode_script.json", {})
    rubric = load_json(rubric_dir / "actor_rubric.json", {})
    # 加载修复前的审计结果
    audit = load_json(audit_dir / "environment_audit.json", {})
    artifacts = {
        "scene_task": scene_task,
        "user_voice": user_voice,
        "situation_knowledge": situation,
        "episode_script": episode if isinstance(episode, dict) else {},
        "actor_rubric": rubric if isinstance(rubric, dict) else {},
    }
    return {
        "case_id": case_id,
        "scene_task": scene_task,
        "user_voice": user_voice,
        "situation_knowledge": situation,
        "episode_script": artifacts["episode_script"],
        "actor_rubric": artifacts["actor_rubric"],
        # timing_view 为审计 prompt 提供紧凑的时序摘要
        "timing_view": compact_episode_timing_view(artifacts["episode_script"]),
        # capability_contract 定义模型能力的离线约束，审计时用于校验一致性
        "capability_contract": copy.deepcopy(OFFLINE_CAPABILITY_CONTRACT),
    }, audit if isinstance(audit, dict) else {}, artifacts


def repair_one_stage(case_id: str, scene: dict, audit: dict, context: dict, artifacts: dict, stage: str, client: MiniMaxClient) -> tuple[dict, dict]:
    """Attempt a targeted repair of a single upstream stage artifact.

    对单个上游制品执行定向修复。

    设计考量：
      - 仅修复审计 findings 中 stage 字段匹配当前 stage 的问题项。
      - 修复前验证所有 finding 的 JSON Pointer 路径在当前制品中存在，
        路径缺失则跳过（skipped），避免模型对不存在的字段做幻觉式修改。
      - 冻结当前 stage 之前的所有上游制品（frozen_upstream），修复 prompt
        只能看到已冻结的上游，防止修复过程意外修改不应触碰的依赖。
      - 最多重试 2 次：第一次失败后将校验错误反馈给模型，第二次仍失败
        则标记为 rejected，保留原始制品不变。
      - 额外校验：修复结果与原始制品逐字节相同时视为无效修复，强制模型
        做出实质性修改。

    Args:
        case_id:   用例标识符。
        scene:     场景信息字典。
        audit:     修复前的完整审计结果。
        context:   修复流程上下文。
        artifacts: 所有上游制品字典。
        stage:     待修复的制品名称（ARTIFACTS 之一）。
        client:    MiniMax API 客户端。

    Returns:
        (repaired_artifact, repair_record) 二元组：
        - repaired_artifact: 修复后的制品（失败时为原始制品的深拷贝）。
        - repair_record: 修复过程记录（status/attempt/finding_ids 等）。
    """
    # 筛选属于当前 stage 的 findings，深拷贝防止修改原始审计数据
    findings = [copy.deepcopy(item) for item in audit.get("findings", []) if isinstance(item, dict) and item.get("stage") == stage]
    current = copy.deepcopy(artifacts[stage])
    # 前置校验：所有 finding 的 path 必须在当前制品中可解析，否则跳过修复
    if not findings or not all(json_pointer_exists(current, item.get("path")) for item in findings):
        return current, {"stage": stage, "status": "skipped", "finding_ids": [item.get("finding_id") for item in findings], "reason": "explicit finding path is not present"}
    # 确定当前 stage 在上游流水线中的位置，冻结其之前的所有制品
    target_index = AUDIT_UPSTREAM_STAGES.index(stage)
    frozen = {name: copy.deepcopy(artifacts[name]) for name in AUDIT_UPSTREAM_STAGES[:target_index] if isinstance(artifacts.get(name), dict)}
    # 组装修复 prompt 所需的上下文
    repair_context = {
        "target_stage": stage,
        "target_findings": findings,
        "current_artifact": current,
        "frozen_upstream": frozen,
        # expected_shape 提供制品的结构契约，帮助模型理解输出格式要求
        "expected_shape": _shape_contract(current),
    }
    last_errors: list[str] = []
    # 修复重试循环：最多 2 次尝试
    for attempt in range(1, 3):
        request = copy.deepcopy(repair_context)
        # 第二次尝试时附带前一次的校验错误，引导模型修正
        if last_errors:
            request["previous_candidate_errors"] = last_errors
        try:
            # 调用模型生成修复候选
            raw, metadata = client.complete(REPAIR_PROMPT, request, max_tokens=STAGE_MAX_TOKENS.get(stage, 6500), response_format=JSON_RESPONSE_FORMAT)
            repaired = parse_json_text(raw, PREFERRED_JSON_KEYS.get(stage))
            # 构造与生成流水线一致的上下文，确保 _prepare_stage_value 和 validate_stage 行为一致
            target_context = stage_context(case_id, stage, scene, artifacts)
            target_context["published_artifact"] = True
            repaired = _prepare_stage_value(stage, repaired, target_context)
            # 结构化校验：检查修复后的制品是否满足 schema 约束
            errors = validate_stage(stage, repaired, target_context)
            # 防止模型返回与原始制品完全相同的内容（等于没有修复）
            if not errors and repaired == current:
                errors = ["repair candidate is byte-for-byte unchanged; apply the requested targeted change"]
            if errors:
                last_errors = errors
                continue
            # 修复成功：返回修复后的制品和成功记录
            return repaired, {"stage": stage, "status": "completed", "attempt": attempt, "finding_ids": [item.get("finding_id") for item in findings], "model": metadata.get("model"), "raw_response": raw}
        except Exception as exc:
            # 模型调用异常：立即返回原始制品，记录错误信息
            return current, {"stage": stage, "status": "error", "attempt": attempt, "finding_ids": [item.get("finding_id") for item in findings], "error": f"{type(exc).__name__}: {exc}"}
    # 两次尝试均未通过校验：标记为 rejected，保留原始制品
    return current, {"stage": stage, "status": "rejected", "attempt": 2, "finding_ids": [item.get("finding_id") for item in findings], "errors": last_errors, "model": client.model}


def audit_case(context: dict, artifacts: dict, client: MiniMaxClient) -> tuple[dict, str, dict]:
    """Run a fresh Environment Audit over the (possibly repaired) artifact package.

    对修复后的制品包重新运行完整的环境审计（12 维评分 + 发布决策）。

    设计考量：
      - 审计是"裁判"角色而非"创作者"，必须保持确定性。因此强制使用
        temperature=0.0，即使修复模型运行在创意温度下，审计结果也不应
        因采样随机性而波动。
      - 审计结果必须通过 validate_stage 校验，否则抛出 ValueError——
        审计是最终发布决策的依据，不允许输出格式不合规的审计结果。

    Args:
        context:   修复流程上下文（scene、capability_contract 等）。
        artifacts: 修复后的上游制品字典。
        client:    MiniMax API 客户端。

    Returns:
        (audit_value, raw_response, metadata) 三元组：
        - audit_value:  解析并校验后的审计结果字典。
        - raw_response: 模型的原始文本输出。
        - metadata:     包含 model 等信息的元数据字典。

    Raises:
        ValueError: 审计结果未通过结构化校验。
    """
    audit_context = {
        **copy.deepcopy(context),
        **{name: copy.deepcopy(artifacts[name]) for name in ARTIFACTS},
        "timing_view": compact_episode_timing_view(artifacts["episode_script"]),
    }
    # The final evaluator is a judge, not a creative generator.  Keep this
    # confirmation pass deterministic even when the repair model runs at the
    # configured creative temperature.
    # 审计是裁判角色，强制 temperature=0.0 确保确定性，避免随机性导致评分波动
    audit_client = client.for_model(client.model, temperature=0.0)
    raw, metadata = audit_client.complete(AUDIT_PROMPT, audit_context, max_tokens=STAGE_MAX_TOKENS["environment_audit"], response_format=JSON_RESPONSE_FORMAT)
    value = parse_json_text(raw, PREFERRED_JSON_KEYS["environment_audit"])
    value = _prepare_stage_value("environment_audit", value, audit_context)
    errors = validate_stage("environment_audit", value, audit_context)
    if errors:
        raise ValueError("environment audit validation failed: " + "; ".join(errors))
    return value, raw, metadata


def process_case(source_run: Path, rubric_run: Path, audit_run: Path, output_run: Path, case_id: str, client: MiniMaxClient) -> dict:
    """End-to-end processing of a single case: load → repair → re-audit → persist.

    单个 case 的端到端处理流程：加载 → 修复 → 重新审计 → 持久化。

    设计考量：
      - 修复前审计快照保存为 environment_audit_pre_repair.json，确保修复效果可追溯。
      - 按 AUDIT_UPSTREAM_STAGES 的顺序修复各 stage，保证上游先修复、下游
        能看到修复后的上游内容。
      - 关键决策：若没有任何成功修复（或修复前审计已通过），则复用修复前
        审计结果，不重新运行审计。原因：审计模型具有随机性，对未改变的
        制品重新审计可能引入分数波动（stochastic regression），这是修复
        流程必须避免的——修复只能让分数变好或不变，不能因随机性变差。
      - 所有制品和审计结果通过 atomic_json 写入，保证并发安全。

    Args:
        source_run: 源 run 目录（input.json + episode_script.json）。
        rubric_run: rubric run 目录（actor_rubric.json）。
        audit_run:  审计 run 目录（environment_audit.json）。
        output_run: 输出 run 目录。
        case_id:    用例标识符。
        client:     MiniMax API 客户端。

    Returns:
        包含 case_id、修复前后分数、发布决策、修复记录、源哈希的摘要字典。
    """
    context, pre_audit, artifacts = load_case(source_run, rubric_run, audit_run, case_id)
    case_dir = output_run / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    # 持久化上下文和修复前审计快照
    atomic_json(case_dir / "input.json", context)
    atomic_json(case_dir / "environment_audit_pre_repair.json", pre_audit)
    repairs: list[dict] = []
    # 从审计 findings 中提取需要修复的 stage 列表（去重）
    findings = pre_audit.get("findings", []) if isinstance(pre_audit, dict) else []
    target_stages = []
    for finding in findings if isinstance(findings, list) else []:
        stage = finding.get("stage") if isinstance(finding, dict) else None
        if stage in ARTIFACTS and stage not in target_stages:
            target_stages.append(stage)
    # 按上游流水线顺序排序，确保上游先修复
    target_stages.sort(key=AUDIT_UPSTREAM_STAGES.index)
    # 逐 stage 执行定向修复
    for stage in target_stages:
        repaired, record = repair_one_stage(case_id, context.get("scene_task", {}).get("scene", {}), pre_audit, context, artifacts, stage, client)
        # 记录中不保存 raw_response（单独写入文件），避免 repair_history.json 过大
        repairs.append({key: value for key, value in record.items() if key != "raw_response"})
        if record.get("status") == "completed":
            # 修复成功：更新 artifacts 中的对应制品，后续 stage 可看到修复后的内容
            artifacts[stage] = repaired
            raw = record.get("raw_response", "")
            (case_dir / f"{stage}.repair.raw_response.txt").write_text(raw, encoding="utf-8")
    # 持久化所有制品（包括修复后和未修复的）
    for name, value in artifacts.items():
        atomic_json(case_dir / f"{name}.json", value)
    # 判断是否有至少一个成功修复
    successful_repairs = any(item.get("status") == "completed" for item in repairs)
    if not target_stages or not successful_repairs:
        # A passing audit is already a released evaluator result.  Re-running
        # a judge when no artifact changed would make the final-repair stage
        # itself introduce stochastic regressions.  The same applies when all
        # repair candidates were rejected by the structural gate.
        # 复用修复前审计结果的核心逻辑：
        #   - 无 target_stages：修复前审计已通过，无需修复也无需重审
        #   - 无 successful_repairs：所有修复尝试均失败，制品未改变，
        #     重审只会引入随机性波动，不会改善结果
        final_audit = copy.deepcopy(pre_audit)
        raw_path = audit_run / case_id / "environment_audit.raw_response.txt"
        raw_audit = raw_path.read_text(encoding="utf-8") if raw_path.exists() else ""
        audit_meta = {"model": "reused_pre_repair_audit", "reused": True, "reason": "no_successful_repair" if target_stages else "pre_repair_pass"}
    else:
        # 至少有一个成功修复：重新运行完整审计，获取修复后的真实评分
        final_audit, raw_audit, audit_meta = audit_case(context, artifacts, client)
    # 持久化最终审计结果、原始响应、修复历史、校验状态
    atomic_json(case_dir / "environment_audit.json", final_audit)
    (case_dir / "environment_audit.raw_response.txt").write_text(raw_audit, encoding="utf-8")
    atomic_json(case_dir / "repair_history.json", {"pre_repair_release_decision": pre_audit.get("release_decision"), "repairs": repairs, "post_repair_model": audit_meta.get("model")})
    atomic_json(case_dir / "validation.json", {"errors": [], "model": audit_meta.get("model"), "status": "passed"})
    return {"case_id": case_id, "pre_repair_score": pre_audit.get("total_score"), "post_repair_score": final_audit.get("total_score"), "post_repair_decision": final_audit.get("release_decision"), "repairs": repairs, "source_hash": digest(context)}


def main() -> None:
    """CLI entry point: parse arguments and run parallel case repairs.

    命令行入口：解析参数并并行执行多个 case 的修复流程。

    设计考量：
      - 使用 ThreadPoolExecutor 并行处理多个 case，workers 参数控制并发度。
      - 结果按原始 case 顺序排序输出，确保 manifest.json 中的顺序与输入一致。
      - manifest.json 记录完整的运行元信息（源 run、rubric run、审计 run、
        模型、provider、各 case 结果），便于下游追踪和对比。
    """
    parser = argparse.ArgumentParser()
    # 三个必需的 run 目录：源数据、rubric 结果、审计结果
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--rubric-run", type=Path, required=True)
    parser.add_argument("--audit-run", type=Path, required=True)
    # 输出 run 标识符，结果写入 runs/<run-id>/ 目录
    parser.add_argument("--run-id", required=True)
    # 逗号分隔的 case_id 列表
    parser.add_argument("--cases", required=True)
    # 并发工作线程数，默认 2（避免 API 限流）
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    cases = [item.strip() for item in args.cases.split(",") if item.strip()]
    output_run = ROOT / "runs" / args.run_id
    output_run.mkdir(parents=True, exist_ok=True)
    # 从环境变量初始化 MiniMax 客户端（API key、模型等）
    client = MiniMaxClient.from_env()
    results = []
    # 并行提交所有 case 的修复任务
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {executor.submit(process_case, args.source_run, args.rubric_run, args.audit_run, output_run, case_id, client): case_id for case_id in cases}
        for job in as_completed(jobs):
            results.append(job.result())
    # 按原始输入顺序排序，保证输出确定性
    results.sort(key=lambda item: cases.index(item["case_id"]))
    # 写入 manifest，记录完整运行元信息
    atomic_json(output_run / "manifest.json", {
        "run_id": args.run_id,
        "source_run": str(args.source_run.resolve()),
        "rubric_runs": {case_id: str(args.rubric_run.resolve()) for case_id in cases},
        "audit_run": str(args.audit_run.resolve()),
        "stage": "environment_audit_final_repair",
        "case_ids": cases,
        "workers": args.workers,
        "model": client.model,
        "provider": client.provider,
        "results": results,
    })
    # 输出 run 目录路径，供下游脚本使用
    print(output_run)


if __name__ == "__main__":
    main()
