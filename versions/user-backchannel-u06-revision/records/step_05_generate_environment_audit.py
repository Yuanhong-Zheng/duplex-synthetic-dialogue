#!/usr/bin/env python3
"""Run Environment Audit on already-generated Stage 3.5 and Actor Rubric runs."""
# 环境审计审查脚本：对已生成的 Stage 3.5（场景+剧本）和 Actor Rubric（角色评分标准）运行结果
# 执行环境审计，产出 12 维度评分与发布决策。本脚本不含修复循环，仅生成审计结果。
from __future__ import annotations

import argparse
import copy
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from minimax_client import MiniMaxClient, parse_json_text
from project_paths import frozen_artifact_in_lineage
from run_pipeline import JSON_RESPONSE_FORMAT, STAGE_MAX_TOKENS, _prepare_stage_value, compact_episode_timing_view, digest, published_stage_contexts
from stage_validation import OFFLINE_CAPABILITY_CONTRACT, frozen_script_validation_stage, validate_stage

# 项目根目录，用于定位 prompts 和 runs 等相对路径资源
ROOT = Path(__file__).parent


def atomic_json(path: Path, value: object) -> None:
    """Write JSON to a temp file then atomically rename — avoids partial writes on crash."""
    # 原子写入：先写 .tmp 临时文件，再 rename 替换目标文件。
    # 防止进程崩溃时留下半截 JSON，保证下游读取的文件始终完整可用。
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_case(source_run: Path, rubric_run: Path, case_id: str) -> tuple[dict, dict[str, dict]]:
    """Load source + rubric artifacts and assemble the full audit context."""
    # 从 Stage 3.5 源运行和 Actor Rubric 运行中加载所有制品，
    # 组装成审计所需的完整上下文（context）和制品字典（artifacts）。
    # 设计要点：context 送入 LLM 做审计生成，artifacts 则逐个落盘供溯源。
    source_dir = source_run / case_id
    rubric_dir = rubric_run / case_id
    # 读取三份核心输入：原始场景配置、剧本、角色评分标准
    source = json.loads((source_dir / "input.json").read_text(encoding="utf-8"))
    script = json.loads((source_dir / "episode_script.json").read_text(encoding="utf-8"))
    rubric = json.loads((rubric_dir / "actor_rubric.json").read_text(encoding="utf-8"))

    # 场景与任务：场景描述 + 任务风格 + 任务实例，构成审计的"做什么"维度
    scene_task = frozen_artifact_in_lineage(source_run, case_id, "scene_task.json")
    if not scene_task:
        scene_task = copy.deepcopy(source.get("scene_task")) if isinstance(source.get("scene_task"), dict) else {
            "scene": copy.deepcopy(source["scene"]),
            "task_style": source["task_style"],
            "task_instance": copy.deepcopy(source["task_instance"]),
        }
    # 用户语音画像：种子设定 + 声音特征，构成审计的"谁在说"维度
    user_voice = frozen_artifact_in_lineage(source_run, case_id, "user_voice.json")
    if not user_voice:
        user_voice = copy.deepcopy(source.get("user_voice")) if isinstance(source.get("user_voice"), dict) else {
            "user_seed": copy.deepcopy(source["user_seed"]),
            "voice_profile": source["voice_profile"],
        }
    # 情境知识：仅选取存在的键，避免因上游阶段未产出某些字段而报错
    # 包含场景情境、参考数据、环境音频、信噪比、约束、安全限制等
    situation_knowledge = frozen_artifact_in_lineage(source_run, case_id, "situation_knowledge.json")
    if not situation_knowledge:
        situation_knowledge = copy.deepcopy(source.get("situation_knowledge")) if isinstance(source.get("situation_knowledge"), dict) else {
            key: copy.deepcopy(source.get(key))
            for key in (
                "episode_situation",
                "task_reference_data",
                "environment_audio",
                "estimated_snr_db",
                "constraints",
                "safety_limits",
            )
            if key in source
        }
    # 组装完整审计上下文：将所有制品与能力契约合并，供 LLM 一次性审阅
    context = {
        "case_id": case_id,
        "scene_task": scene_task,
        "user_voice": user_voice,
        "situation_knowledge": situation_knowledge,
        "episode_script": script,
        "timing_view": compact_episode_timing_view(script),  # 压缩时序视图，减少 token 消耗
        "actor_rubric": rubric,
        "capability_contract": copy.deepcopy(OFFLINE_CAPABILITY_CONTRACT),  # 离线能力契约，审计的评判基准
    }
    # artifacts 与 context 内容重叠但用途不同：
    # artifacts 逐个落盘便于独立查看，context 整体送入 LLM
    artifacts = {
        "scene_task": scene_task,
        "user_voice": user_voice,
        "situation_knowledge": situation_knowledge,
        "episode_script": script,
        "actor_rubric": rubric,
    }
    # Keep original frozen values authoritative; a compact input may omit a
    # field such as SNR, but it must not conflict with the full artifact.
    for stage in ("scene_task", "user_voice", "situation_knowledge"):
        if isinstance(source.get(stage), dict) and source[stage] != artifacts[stage]:
            raise ValueError(f"{case_id} frozen {stage} conflicts with embedded source input")
        for field, value in artifacts[stage].items():
            if field in source and source[field] != value:
                raise ValueError(f"{case_id} frozen {stage}.{field} conflicts with source input")
    stage_contexts = published_stage_contexts(artifacts)
    for stage, value in artifacts.items():
        validation_stage = frozen_script_validation_stage(value) if stage == "episode_script" else stage
        validation_context = stage_contexts[stage]
        if stage == "episode_script" and isinstance(source.get("side_speech_policy"), dict):
            validation_context["side_speech_policy"] = copy.deepcopy(source["side_speech_policy"])
        errors = validate_stage(validation_stage, value, validation_context)
        if errors:
            raise ValueError(f"{case_id} frozen {stage} is incompatible: {'; '.join(errors[:6])}")
    return context, artifacts


def generate_case(
    source_run: Path,
    rubric_run: Path,
    output_run: Path,
    case_id: str,
    prompt: str,
    client: MiniMaxClient,
) -> dict:
    """Generate environment audit for a single case: load artifacts, call LLM, validate, persist."""
    # 单 case 审计生成主流程：
    # 1. 加载制品并组装上下文
    # 2. 将制品和上下文落盘（供溯源和调试）
    # 3. 调用 LLM 生成环境审计结果
    # 4. 解析、校验、持久化审计结果
    # 5. 返回状态摘要（passed/failed + 错误列表 + 源哈希）
    # 注意：本函数不含修复循环，审计失败即标记为 failed
    context, artifacts = load_case(source_run, rubric_run, case_id)
    case_dir = output_run / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    # 逐个落盘制品文件，便于独立查看每个阶段的输出
    for stage, value in artifacts.items():
        atomic_json(case_dir / f"{stage}.json", value)
    # 落盘完整上下文和 prompt，供复现和调试
    atomic_json(case_dir / "input.json", context)
    (case_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    try:
        # 调用 LLM 生成环境审计，使用 JSON 格式约束和阶段专属 token 上限
        raw, metadata = client.complete(
            prompt,
            context,
            max_tokens=STAGE_MAX_TOKENS["environment_audit"],
            response_format=JSON_RESPONSE_FORMAT,
        )
        # 保存原始响应文本，便于调试解析失败等问题
        (case_dir / "environment_audit.raw_response.txt").write_text(raw, encoding="utf-8")
        # 解析 LLM 返回的 JSON，指定期望的顶层键集合作为解析提示
        # 包含：审查引用、发现、12维度评分、总分、频率校准、发布决策
        value = parse_json_text(raw, {"reviewed_refs", "findings", "dimension_scores", "total_score", "frequency_calibration", "release_decision"})
        # 标准化阶段输出：注入元信息、清洗字段格式
        value = _prepare_stage_value("environment_audit", value, context)
        # 校验审计结果是否符合 schema 约束
        errors = validate_stage("environment_audit", value, context)
        # 持久化审计结果和校验结果
        atomic_json(case_dir / "environment_audit.json", value)
        atomic_json(case_dir / "validation.json", {"errors": errors, "model": metadata.get("model")})
        # 返回状态摘要：source_hash 用于检测上游是否变更
        return {"case_id": case_id, "status": "passed" if not errors else "failed", "errors": errors, "source_hash": digest(context)}
    except Exception as exc:
        # 异常时将错误信息写入原始响应文件，便于排查 LLM 调用失败原因
        (case_dir / "environment_audit.raw_response.txt").write_text(str(exc), encoding="utf-8")
        atomic_json(case_dir / "validation.json", {"errors": [f"generation error: {type(exc).__name__}: {exc}"]})
        return {"case_id": case_id, "status": "failed", "errors": [str(exc)], "source_hash": digest(context)}


def main() -> None:
    """CLI entry point: parse args, fan out cases in parallel, write manifest."""
    # 主入口：解析命令行参数，并发执行多 case 审计，汇总结果写入 manifest。
    # 设计要点：使用线程池并发调用 LLM，提升批量审计效率；
    # 最终按原始 case 顺序排序结果，保证 manifest 的确定性输出。
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True, help="Stage 3.5 run")
    parser.add_argument("--rubric-run", type=Path, required=True, help="Stage 4 Actor Rubric run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cases", required=True, help="comma-separated case IDs")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    # 解析逗号分隔的 case ID 列表，去除空白
    cases = [item.strip() for item in args.cases.split(",") if item.strip()]
    # 加载环境审计 prompt 模板
    prompt = (ROOT / "prompts" / "05_environment_audit.md").read_text(encoding="utf-8")
    # 创建输出运行目录
    output_run = ROOT / "runs" / args.run_id
    output_run.mkdir(parents=True, exist_ok=True)
    # 从环境变量初始化 LLM 客户端
    client = MiniMaxClient.from_env()
    results = []
    # 使用线程池并发执行审计生成，每个 case 一个任务
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {
            executor.submit(generate_case, args.source_run, args.rubric_run, output_run, case_id, prompt, client): case_id
            for case_id in cases
        }
        # 按完成顺序收集结果（非提交顺序）
        for job in as_completed(jobs):
            results.append(job.result())
    # 恢复原始 case 顺序，保证 manifest 输出的确定性
    results.sort(key=lambda item: cases.index(item["case_id"]))
    # 写入运行清单，记录本次审计的完整元信息
    # 包含：运行 ID、源运行路径、rubric 运行路径、阶段名、case 列表、
    # 并发数、模型、提供商、各 case 结果
    atomic_json(
        output_run / "manifest.json",
        {
            "run_id": args.run_id,
            "source_run": str(args.source_run.resolve()),
            "rubric_run": str(args.rubric_run.resolve()),
            "stage": "environment_audit_review",
            "case_ids": cases,
            "workers": args.workers,
            "model": client.model,
            "provider": client.provider,
            "results": results,
        },
    )
    # 输出运行目录路径，供下游脚本使用
    print(output_run)
    # 任一 case 失败则退出码为 1，便于 CI 管道检测失败
    if any(item["status"] != "passed" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
