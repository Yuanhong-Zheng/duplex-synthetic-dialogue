#!/usr/bin/env python3
"""Generate Actor Rubrics for selected frozen script cases and a review run."""
# Actor Rubric 审查脚本：为已冻结的剧本用例生成轮次级语义+行为评分细则。
# 与完整流水线不同，本脚本不含修复循环（repair loop），仅执行一次性生成。
# 主要用途：对指定用例批量生成 actor_rubric，用于审查和评估生成质量。
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import json
from pathlib import Path

# MiniMaxClient: 调用 MiniMax 模型的客户端封装
from minimax_client import MiniMaxClient, parse_json_text
# 从主流水线模块复用核心组件，避免重复实现
from run_pipeline import (
    JSON_RESPONSE_FORMAT,       # 强制 JSON 输出格式的配置
    STAGE_MAX_TOKENS,           # 各阶段最大 token 数配置
    _prepare_stage_value,       # 阶段输出后处理（字段补全、默认值填充等）
    _run_local_repairs,
    compact_actor_rubric_script, # 将完整剧本压缩为 rubric 生成所需的精简格式
    digest,                     # 计算输入数据的哈希摘要，用于追踪数据版本
)
# OFFLINE_CAPABILITY_CONTRACT: 离线能力契约，定义模型可用的交互模态和能力边界
# validate_stage: 阶段输出校验器，检查生成结果是否符合 schema 约束
from stage_validation import OFFLINE_CAPABILITY_CONTRACT, validate_stage


# 项目根目录，用于定位 prompts 等相对路径资源
ROOT = Path(__file__).parent


def atomic_json(path: Path, value: object) -> None:
    """Atomically write a JSON file via temp-file-then-rename.

    原子写入 JSON 文件：先写入临时文件，再通过 rename 替换目标文件。
    设计原因：避免并发读写时出现半写（partial write）状态，
    确保其他进程读取到的要么是完整的旧版本，要么是完整的新版本。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先写入 .tmp 后缀的临时文件，确保内容完整后再原子替换
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # rename 在同一文件系统上是原子操作，不会出现中间状态
    tmp.replace(path)


def load_context(source_run: Path, case_id: str) -> tuple[dict, dict]:
    """Load input and episode script for a case, returning context dict and raw source input.

    加载指定用例的输入数据和剧本，组装为 rubric 生成所需的上下文字典。

    Args:
        source_run: 源运行目录路径，包含已冻结的用例数据
        case_id: 用例标识符，对应 source_run 下的子目录名

    Returns:
        (context, source_input) 元组：
        - context: 组装好的上下文字典，将直接传给 LLM 作为生成 rubric 的输入
        - source_input: 原始输入数据，用于后续计算哈希摘要以追踪数据版本

    设计说明：
        context 中包含场景、任务风格、任务实例、剧本、约束条件等完整信息，
        其中 episode_script 经过 compact_actor_rubric_script 压缩，去除与
        rubric 生成无关的冗余字段，减少 token 消耗。同时注入能力契约
        （capability_contract），让模型了解可用的交互模态边界。
    """
    case_dir = source_run / case_id
    # 读取原始输入（场景、任务定义等）和已生成的剧本
    source_input = json.loads((case_dir / "input.json").read_text(encoding="utf-8"))
    episode_script = json.loads((case_dir / "episode_script.json").read_text(encoding="utf-8"))
    context = {
        "case_id": case_id,
        "scene": source_input.get("scene", {}),
        "task_style": source_input.get("task_style"),
        "task_instance": source_input.get("task_instance", {}),
        "episode_situation": source_input.get("episode_situation"),
        "task_reference_data": source_input.get("task_reference_data", []),
        "constraints": source_input.get("constraints", []),
        "safety_limits": source_input.get("safety_limits", []),
        # 将完整剧本压缩为精简格式，只保留 rubric 生成所需的关键轮次信息
        "episode_script": compact_actor_rubric_script(episode_script),
        # 注入模型可用的交互模态列表（如语音、文本等），约束生成范围
        "available_modalities": list(OFFLINE_CAPABILITY_CONTRACT["interaction_modalities"]),
        # 深拷贝能力契约，避免修改全局常量
        "capability_contract": copy.deepcopy(OFFLINE_CAPABILITY_CONTRACT),
    }
    return context, source_input


def generate_case(source_run: Path, output_run: Path, case_id: str, prompt: str, client: MiniMaxClient, frozen_rubric: dict | None = None, *, strict_execution=False) -> dict:
    """Generate an Actor Rubric for a single case and persist all artifacts.

    为单个用例生成 Actor Rubric 并持久化所有中间产物。

    Args:
        source_run: 源运行目录，包含已冻结的 input.json 和 episode_script.json
        output_run: 输出运行目录，生成的 rubric 和校验结果将写入此目录
        case_id: 用例标识符
        prompt: rubric 生成阶段的 prompt 模板文本
        client: MiniMax 模型客户端实例

    Returns:
        结果字典，包含 case_id、status（passed/failed）、errors 列表和
        source_hash（原始输入的哈希摘要，用于追踪数据版本一致性）。

    设计说明：
        本函数是审查模式的核心——无论校验是否通过，都会保存生成的 rubric，
        以便人工审查失败用例的输出。这与完整流水线不同：流水线中校验失败
        会触发修复循环，而审查模式只记录结果不做修复。
    """
    context, source_input = load_context(source_run, case_id)
    if frozen_rubric is not None:
        context["frozen_actor_rubric"] = copy.deepcopy(frozen_rubric)
    case_dir = output_run / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    # 持久化组装后的上下文，便于事后审查和复现
    atomic_json(case_dir / "input.json", context)
    # 保存 prompt 模板，便于追溯生成时使用的指令
    (case_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    try:
        # 调用 LLM 生成 rubric，使用 JSON 强制输出格式和阶段级 token 上限
        raw, metadata = client.complete(
            prompt,
            context,
            max_tokens=STAGE_MAX_TOKENS["actor_rubric"],
            response_format=JSON_RESPONSE_FORMAT,
        )
        # 保存原始响应文本，用于调试和审计
        (case_dir / "raw_response.txt").write_text(raw, encoding="utf-8")
        # 解析 JSON 响应，期望包含 turn_rubrics 字段
        try:value = parse_json_text(raw, {"turn_rubrics"})
        except ValueError as exc:
            if not strict_execution:raise
            value = {'_parse_error':str(exc)}
        # 后处理：补全缺失字段、填充默认值，确保输出结构完整
        value = _prepare_stage_value("actor_rubric", value, context)
        # 校验生成结果是否符合 actor_rubric 阶段的 schema 约束
        errors = validate_stage("actor_rubric", value, context)
        repairs = []
        if errors:
            value, errors, repairs = _run_local_repairs("actor_rubric",value,errors,context,client,
                                                       case_dir/'stage_cache/actor_rubric/structural')
        if frozen_rubric is not None:
            turns = context["episode_script"]["episode_turns"]
            for old, new, turn in zip(frozen_rubric["turn_rubrics"], value["turn_rubrics"], turns):
                count = len(old["behavior"])
                if new["semantic"] != old["semantic"] or new["behavior"][:count] != old["behavior"] or len(new["behavior"]) != count + bool(turn.get("user_backchannels")) + bool(not old["behavior"] and turn.get("side_speech")):
                    errors.append(f"{turn['turn_id']}: existing Rubric changed or backchannel criterion missing")
        # 审查模式：无论校验通过与否，都保存 rubric 结果，方便人工审查
        if not errors:
            atomic_json(case_dir / "actor_rubric.json", value)
        else:
            atomic_json(case_dir / "actor_rubric.json", value)
        # 保存校验结果，包含错误详情和使用的模型信息
        atomic_json(case_dir / "validation.json", {"errors": errors, "model": metadata.get("model"), "repair_attempts":repairs})
        atomic_json(case_dir / "metadata.json", {**metadata,"repair_attempts":repairs})
        return {"case_id": case_id, "status": "passed" if not errors else "failed", "errors": errors, "source_hash": digest(source_input)}
    except Exception as exc:
        if strict_execution:raise
        # 生成异常时记录错误信息，不中断批量处理
        (case_dir / "raw_response.txt").write_text(str(exc), encoding="utf-8")
        atomic_json(case_dir / "validation.json", {"errors": [f"generation error: {type(exc).__name__}: {exc}"]})
        return {"case_id": case_id, "status": "failed", "errors": [str(exc)]}


def main() -> None:
    """CLI entry point: generate Actor Rubrics for specified cases in parallel.

    命令行入口：对指定用例并行生成 Actor Rubric，并汇总结果。

    设计说明：
        使用 ThreadPoolExecutor 并行处理多个用例，提高审查效率。
        最终生成 manifest.json 汇总所有用例的生成状态，若存在任何
        失败用例则以退出码 1 终止，便于 CI/CD 流水线检测失败。
    """
    parser = argparse.ArgumentParser()
    # --source-run: 指定包含已冻结用例数据的源运行目录
    parser.add_argument("--source-run", type=Path, required=True)
    # --run-id: 本次审查运行的唯一标识，用于创建输出目录
    parser.add_argument("--run-id", required=True)
    # --cases: 逗号分隔的用例 ID 列表，指定需要生成 rubric 的用例
    parser.add_argument("--cases", required=True, help="comma-separated case IDs")
    # --workers: 并行工作线程数，默认 2，需注意 API 速率限制
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    # 解析用例 ID 列表，去除空白项
    cases = [item.strip() for item in args.cases.split(",") if item.strip()]
    if not cases:
        parser.error("--cases must not be empty")
    # 加载 actor_rubric 阶段的 prompt 模板
    prompt = (ROOT / "prompts" / "04_actor_rubric.md").read_text(encoding="utf-8")
    # 创建本次运行的输出目录
    output_run = ROOT / "runs" / args.run_id
    output_run.mkdir(parents=True, exist_ok=True)
    # 从环境变量初始化 MiniMax 客户端（API key 等配置）
    client = MiniMaxClient.from_env()
    results = []
    # 并行提交所有用例的生成任务
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {
            executor.submit(generate_case, args.source_run, output_run, case_id, prompt, client): case_id
            for case_id in cases
        }
        # 按完成顺序收集结果（as_completed 不保证原始顺序）
        for job in as_completed(jobs):
            results.append(job.result())
    # 恢复原始用例顺序，便于 manifest 中结果与输入一一对应
    results.sort(key=lambda item: cases.index(item["case_id"]))
    # 生成运行清单，记录本次审查的完整元信息
    manifest = {
        "run_id": args.run_id,
        "source_run": str(args.source_run.resolve()),
        "stage": "actor_rubric_review",  # 标记为审查阶段，区别于正式生成
        "case_ids": cases,
        "workers": args.workers,
        "model": client.model,
        "provider": client.provider,
        "results": results,
    }
    atomic_json(output_run / "manifest.json", manifest)
    # 输出运行目录路径，便于后续脚本定位结果
    print(output_run)
    # 若存在任何失败用例，以非零退出码终止，供上游流水线检测
    if any(item["status"] != "passed" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
