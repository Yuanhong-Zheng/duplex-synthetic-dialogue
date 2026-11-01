#!/usr/bin/env python3
"""Generate and semantically review Stage-00 task directions for taxonomy scenarios."""
# Stage 0: 任务方向生成脚本
# 为 taxonomy 中每个 scenario 生成多风格的任务方向样本，并通过 judge/repair 循环确保质量。
# 核心流程：生成 → 结构校验 → 语义评审(judge) → 修复(repair) → 合并回 taxonomy
#
# 整体架构说明：
# 本脚本是 data-pipe 管线的 Stage 0，负责为 taxonomy 中的每个 Domain/Scenario 生成
# 任务方向（task direction）样本。这些样本将作为后续 Stage 1（场景脚本生成）的输入。
# 质量保障采用 Judge/Repair 双循环机制：先做结构校验（JSON 格式、字段完整性），
# 再做语义评审（LLM judge 检查内容质量），若不通过则定向修复，最多重试 MAX_JUDGE_ATTEMPTS 次。
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import html
import json
from pathlib import Path
import time

# MiniMaxClient: 封装了多 LLM provider 的统一调用接口，支持 GPT/Doubao 等模型
# parse_json_text: 从 LLM 输出中提取 JSON，支持 markdown 代码块包裹等常见格式
from minimax_client import MiniMaxClient, parse_json_text
# run_pipeline 中的公共工具函数：
# - JSON_RESPONSE_FORMAT: 指示 LLM 以 JSON 格式输出的 response_format 参数
# - _redact_secret: 脱敏处理，将 API key 等敏感信息替换为占位符
# - _write_json_atomic / _write_text_atomic: 原子写入文件，避免写入中断导致文件损坏
# - digest: 计算 SHA-256 摘要，用于数据指纹和去重
from run_pipeline import (
    JSON_RESPONSE_FORMAT,
    _redact_secret,
    _write_json_atomic,
    _write_text_atomic,
    digest,
)

# 脚本所在目录，用于定位 prompts/ 子目录中的模板文件
ROOT = Path(__file__).parent

# ---- 任务风格定义 ----
# 三种核心任务风格：开放式互动（自由对话）、结构化整理（信息归纳）、混合型（两者兼有）
# 这三种风格覆盖了语音双工对话的主要交互形态，是全量生成模式的默认输出
STYLES = ("开放式互动", "结构化整理", "混合型")
# ALL_STYLES 在三种核心风格基础上增加"常识问答"，用于单风格生成模式
# 常识问答不纳入全量模式，因为其任务形态（一问一答）与双工对话场景差异较大
ALL_STYLES = STYLES + ("常识问答",)

# ---- Prompt 模板映射 ----
# 每种风格对应独立的 prompt 模板三元组：(生成prompt, judge prompt, repair prompt)
# 常识问答风格因任务形态差异较大，需要专用 prompt 三件套
# 三种核心风格共用同一套 prompt（00_generate_task_examples.md 等），因为它们在同一个生成调用中产出
STYLE_PROMPTS = {
    "常识问答": (
        "00_generate_task_examples_common_qa.md",
        "00_task_examples_common_qa_judge.md",
        "00_task_examples_common_qa_repair.md",
    ),
}

# ---- 结构校验常量 ----
# 生成输出必须包含的顶层 key 集合，用于结构校验
# 设计为集合而非列表，因为校验时只关心 key 是否齐全，不关心顺序
TASK_KEYS = {"example_tasks"}
# Judge 输出必须包含的 key 集合：decision(通过/需修复) + findings(问题列表)
JUDGE_KEYS = {"decision", "findings"}

# ---- 循环控制常量 ----
# 最大 judge/repair 循环次数，超过则判定失败，避免无限重试
# 设为 2 是经验值：第一次修复通常能解决大部分问题，第二次是兜底
MAX_JUDGE_ATTEMPTS = 2

# ---- Token 限制 ----
# Judge 模型最大输出 token 数，评审结果较短故限制较低
# 评审只需输出 decision + findings，不需要长文本
JUDGE_MAX_TOKENS = 2600
# 生成模型最大输出 token 数，需容纳多风格多条任务方向
# 全量模式一次生成 3 种风格 x 5 条 = 15 条任务方向，需要较大空间
GENERATION_MAX_TOKENS = 4200

# ---- 生成数量 ----
# 每种风格默认生成的任务方向条数
# 5 条是平衡覆盖度和成本的经验值：足够展示风格特征，又不至于过度消耗 token
DEFAULT_EXAMPLES_PER_STYLE = 5


def _validate_tasks(
    value: object,
    styles: tuple[str, ...] = STYLES,
    examples_per_style: int = DEFAULT_EXAMPLES_PER_STYLE,
) -> list[str]:
    """Structural validation of generated task-direction output."""
    # 校验生成输出的结构是否符合预期：
    # 1. 顶层必须恰好包含 example_tasks 这一个 key
    # 2. example_tasks 下必须恰好包含指定的所有风格
    # 3. 每种风格下必须是数组，且条数精确等于 examples_per_style
    # 4. 每条任务方向必须是非空字符串
    # 5. 所有任务方向之间不允许完全重复（防止模型偷懒复制粘贴）
    #
    # 参数:
    #     value: 待校验的生成输出（通常是 parse_json_text 的返回值）
    #     styles: 期望包含的任务风格元组，默认为三种核心风格
    #     examples_per_style: 每种风格期望的条目数，默认 5
    #
    # 返回:
    #     错误信息列表，空列表表示校验通过
    #
    # 设计理由:
    #     结构校验是 Judge/Repair 循环的第一道关卡。在调用 LLM judge 做语义评审之前，
    #     必须先确保输出格式正确，否则 judge 无法正确解析输入。严格的"恰好包含"校验
    #     （而非"至少包含"）可以防止模型生成多余的风格或条目，保持数据一致性。
    errors: list[str] = []
    # 检查顶层结构：必须是 dict 且恰好只有 example_tasks 一个 key
    if not isinstance(value, dict) or set(value) != TASK_KEYS:
        return ["output must contain exactly example_tasks"]
    tasks = value["example_tasks"]
    # 检查风格覆盖：example_tasks 下必须恰好包含指定的所有风格，不多不少
    if not isinstance(tasks, dict) or set(tasks) != set(styles):
        return [f"example_tasks must contain exactly these task styles: {', '.join(styles)}"]
    all_text: list[str] = []
    for style in styles:
        items = tasks[style]
        # 每种风格下必须是数组
        if not isinstance(items, list):
            errors.append(f"example_tasks.{style} must be an array")
            continue
        # 条目数必须精确匹配，防止模型多生成或少生成
        if len(items) != examples_per_style:
            errors.append(f"example_tasks.{style} must contain exactly {examples_per_style} items")
        for index, item in enumerate(items):
            # 每条任务方向必须是非空字符串，防止模型输出 null 或空串
            if not isinstance(item, str) or not item.strip():
                errors.append(f"example_tasks.{style}[{index}] must be a non-empty string")
            else:
                all_text.append(item.strip())
    # 去重检查：确保没有完全相同的任务方向，避免模型生成重复内容
    # 注意：这里只检查完全相同的字符串，语义相似但不完全相同的不会被判为重复
    if len(all_text) != len(set(all_text)):
        errors.append("example_tasks contains exact duplicate strings")
    return errors


def _validate_judge(value: object) -> list[str]:
    """Validate the structure of a judge response."""
    # 校验 Judge 输出的结构完整性：
    # - 必须包含 decision 和 findings 两个 key
    # - decision 只能是 pass 或 repair
    # - findings 中每条必须包含 severity/path/problem/evidence/repair 五个字段
    # - severity 必须为 must_fix（只关注必须修复的问题，忽略建议性意见）
    # - pass 时 findings 必须为空，repair 时必须至少有一条 must_fix
    # 这些约束确保 judge 输出格式统一，repair 阶段能准确获取修复指令
    #
    # 参数:
    #     value: 待校验的 judge 输出（通常是 parse_json_text 的返回值）
    #
    # 返回:
    #     错误信息列表，空列表表示校验通过
    #
    # 设计理由:
    #     Judge 的输出格式直接决定了 Repair 阶段能否正确工作。严格的格式校验可以：
    #     1. 防止 judge 输出模糊的 decision（如 "maybe"），迫使 judge 做出明确判断
    #     2. 确保每条 finding 都有完整的修复指令（path + repair），repair 模型无需猜测
    #     3. 只保留 must_fix 级别的问题，避免 repair 被建议性意见干扰
    #     4. pass/repair 的互斥约束防止 judge 自相矛盾（说通过却列问题）
    if not isinstance(value, dict):
        return ["judge output must be an object"]
    errors: list[str] = []
    # 检查顶层 key 集合：必须恰好是 decision + findings
    if set(value) != JUDGE_KEYS:
        errors.append("judge output must contain exactly decision and findings")
    # decision 只允许 pass（通过）或 repair（需修复），不允许模糊判断
    if value.get("decision") not in {"pass", "repair"}:
        errors.append("judge decision must be pass or repair")
    findings = value.get("findings")
    if not isinstance(findings, list):
        return errors + ["judge findings must be an array"]
    # 每条 finding 必须包含完整的五元组，repair 模型依赖这些字段定位和修复问题
    finding_keys = {"severity", "path", "problem", "evidence", "repair"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"judge findings[{index}] must be an object")
            continue
        # 检查字段集合是否完整
        if set(finding) != finding_keys:
            errors.append(f"judge findings[{index}] has an invalid field set")
        # severity 必须是 must_fix：只关注必须修复的问题，忽略建议性意见
        # 这样 repair 阶段只需处理真正需要修复的问题，避免过度修改
        if finding.get("severity") != "must_fix":
            errors.append(f"judge findings[{index}].severity must be must_fix")
        # path/problem/evidence/repair 都必须是非空字符串
        # path 指向 JSON 中的问题位置，repair 给出修复指令
        for field in ("path", "problem", "evidence", "repair"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                errors.append(f"judge findings[{index}].{field} must be a non-empty string")
    # pass 决策下不应有任何 findings，否则说明 judge 自相矛盾
    if value.get("decision") == "pass" and findings:
        errors.append("judge pass must have an empty findings array")
    # repair 决策下必须至少有一条 must_fix 级别的 finding，否则 repair 无从下手
    if value.get("decision") == "repair" and not any(
        isinstance(item, dict) and item.get("severity") == "must_fix" for item in findings
    ):
        errors.append("judge repair must contain a must_fix finding")
    return errors


def _judge_client(client: MiniMaxClient) -> MiniMaxClient:
    """Create a judge-specific client from the generation client.

    为 judge 阶段创建专用客户端：
    - 若 judge_model 以 gpt- 开头，使用 judge_from_env 走 OpenAI 通道
    - 否则复用同一 provider，但将 temperature 设为 0.0 以确保评审结果稳定可复现

    参数:
        client: 生成阶段使用的 LLM 客户端，从中获取 judge_model 配置

    返回:
        配置好的 judge 专用客户端

    设计理由:
        Judge 阶段对输出稳定性要求远高于生成阶段。temperature=0.0 确保：
        1. 相同输入始终产生相同的评审结论，便于复现和调试
        2. 评审结果不受随机性影响，避免同一输出时而通过时而不通过
        GPT 模型走独立通道是因为 OpenAI API 的调用方式与 Doubao 等国产模型不同，
        需要通过 judge_from_env 使用 OpenAI SDK。
    """
    if client.judge_model.lower().startswith("gpt-"):
        return MiniMaxClient.judge_from_env(client.judge_model)
    return client.for_model(client.judge_model, temperature=0.0)


def _scenario_context(scenario: dict, examples_per_style: int) -> dict:
    """Extract scenario metadata as prompt context for generation/judge/repair.

    从 scenario 中提取生成任务方向所需的上下文信息：
    - 领域信息（domain_id, domain, domain_definition）提供背景知识
    - 场景信息（scenario_id, scenario）定位具体场景
    - requested_examples_per_style 告知模型每种风格需生成多少条
    - existing_example_task_seeds 作为种子提示，帮助模型生成与已有种子方向一致但不重复的内容

    参数:
        scenario: 包含 domain/scenario 信息的字典（已展平 domain 层级）
        examples_per_style: 每种风格需生成的任务方向条数

    返回:
        可直接传入 prompt 模板的上下文字典

    设计理由:
        将 scenario 中的字段筛选和重命名为 prompt 模板期望的格式，起到适配层的作用。
        只传递必要字段，避免 prompt 中出现冗余信息干扰模型。existing_example_task_seeds
        的传递使模型能参考已有种子方向，生成风格一致但不重复的内容。
    """
    return {
        "domain_id": scenario.get("domain_id"),
        "domain": scenario.get("domain"),
        "domain_definition": scenario.get("domain_definition", ""),
        "scenario_id": scenario.get("scenario_id"),
        "scenario": scenario.get("scenario"),
        "requested_examples_per_style": examples_per_style,
        "existing_example_task_seeds": scenario.get("example_task_seeds", []),
    }


def _judge_input(context: dict, value: dict) -> dict:
    """Build the input payload for the judge prompt.

    组装 judge 阶段的输入：场景上下文 + 当前生成的任务方向。
    使用 deepcopy 防止后续修改影响原始数据。

    参数:
        context: _scenario_context 返回的场景上下文
        value: 当前生成的任务方向（包含 example_tasks key）

    返回:
        judge prompt 模板期望的输入字典

    设计理由:
        Judge 需要同时看到场景定义（判断任务方向是否贴合场景）和生成的任务方向
        （判断内容质量）。deepcopy 是必要的，因为 repair 阶段可能修改 value，
        而 judge 的输入需要保持原始状态用于缓存和审计。
    """
    return {"scenario": context, "example_tasks": copy.deepcopy(value.get("example_tasks"))}


def _repair_input(context: dict, value: dict, findings: list[dict]) -> dict:
    """Build the input payload for the repair prompt.

    组装 repair 阶段的输入：场景上下文 + 当前任务方向 + judge 发现的问题。
    repair 模型需要同时看到原始输出和问题诊断，才能精准修复。

    参数:
        context: _scenario_context 返回的场景上下文
        value: 当前生成的任务方向（包含 example_tasks key）
        findings: judge 发现的问题列表，每条包含 severity/path/problem/evidence/repair

    返回:
        repair prompt 模板期望的输入字典

    设计理由:
        Repair 模型需要三方面信息才能有效修复：
        1. scenario：理解场景背景，确保修复后的内容仍然贴合场景
        2. current_example_tasks：看到当前输出，知道需要修改什么
        3. judge_findings：看到具体问题诊断，知道如何修改
        三者缺一不可：没有场景信息可能修复后偏离场景，没有当前输出无从修改，
        没有 findings 则不知道该修哪里。deepcopy 防止后续修改污染原始数据。
    """
    return {
        "scenario": context,
        "current_example_tasks": copy.deepcopy(value.get("example_tasks")),
        "judge_findings": copy.deepcopy(findings),
    }


def _structural_findings(errors: list[str], styles: tuple[str, ...], examples_per_style: int) -> list[dict]:
    """Convert structural validation errors into judge-style findings for the repair prompt.

    当结构校验失败时，将错误信息转换为与 judge findings 相同的格式，
    这样 repair 阶段可以统一处理结构问题和语义问题，无需区分来源。

    参数:
        errors: _validate_tasks 返回的结构校验错误列表
        styles: 当前使用的任务风格元组
        examples_per_style: 每种风格期望的条目数

    返回:
        与 judge findings 格式一致的 finding 字典列表

    设计理由:
        统一 findings 格式是关键设计决策：repair 模型只需处理一种格式的输入，
        不需要区分"这是结构问题"还是"这是语义问题"。每条 structural finding
        都包含完整的五元组（severity/path/problem/evidence/repair），repair
        模型可以直接根据 repair 字段的指令进行修复。path 统一设为 /example_tasks
        是因为结构问题通常影响整个输出，而非某个具体字段。
    """
    return [
        {
            "severity": "must_fix",
            "path": "/example_tasks",
            "problem": error,
            "evidence": "当前输出不符合 Stage-00 结构",
            "repair": f"修复 JSON 结构并保留 {len(styles)} 组各 {examples_per_style} 条任务方向",
        }
        for error in errors
    ]


def _generate_one(
    scenario: dict,
    run_dir: Path,
    client: MiniMaxClient,
    target_style: str | None = None,
    examples_per_style: int = DEFAULT_EXAMPLES_PER_STYLE,
) -> dict:
    """Generate task directions for a single scenario with judge/repair quality loop.

    为单个 scenario 执行完整的生成-评审-修复流程。

    参数:
        scenario: 包含 domain/scenario 信息的字典
        run_dir: 运行输出目录，用于缓存中间结果
        client: LLM 客户端，用于生成和修复
        target_style: 若指定则只生成该风格（单风格模式），否则生成所有 STYLES
        examples_per_style: 每种风格需生成的任务方向条数

    返回:
        包含 scenario_id, example_tasks, validation 等信息的结果字典

    设计思路:
        采用"生成 → 结构校验 → 语义评审 → 修复"的闭环流程：
        1. 先用生成模型产出任务方向
        2. 结构校验检查 JSON 格式和字段完整性
        3. 若结构通过，再用 judge 模型做语义评审
        4. 若 judge 判定需修复，则调用 repair 模型修正
        5. 最多循环 MAX_JUDGE_ATTEMPTS 次，超过则标记失败
        每次中间结果都写入 cache 目录，便于事后审计和调试。
    """
    scenario_id = str(scenario["scenario_id"])
    # 每个 scenario 独立一个子目录，包含输入、输出、校验结果和缓存
    case_dir = run_dir / scenario_id
    # cache 目录用于存储 judge/repair 的中间结果，便于调试和审计
    # 目录结构：stage_cache/task_examples/judge/ 和 stage_cache/task_examples/repair/
    cache_dir = case_dir / "stage_cache" / "task_examples"
    case_dir.mkdir(parents=True, exist_ok=True)
    # 单风格模式：只生成指定风格；否则生成全部三种核心风格
    # 单风格模式用于增量补充（如只补常识问答），全量模式用于首次生成
    styles = (target_style,) if target_style else STYLES
    context = _scenario_context(scenario, examples_per_style)
    # 根据是否为单风格模式选择不同的 prompt 模板
    # 常识问答有独立的 prompt 三件套，三种核心风格共用一套 prompt
    if target_style:
        prompt_file, judge_file, repair_file = STYLE_PROMPTS[target_style]
    else:
        prompt_file, judge_file, repair_file = (
            "00_generate_task_examples.md",
            "00_task_examples_judge.md",
            "00_task_examples_repair.md",
        )
    # 预加载三个 prompt 模板，避免在循环中重复读取文件
    prompt = (ROOT / "prompts" / prompt_file).read_text(encoding="utf-8")
    judge_prompt = (ROOT / "prompts" / judge_file).read_text(encoding="utf-8")
    repair_prompt = (ROOT / "prompts" / repair_file).read_text(encoding="utf-8")
    # ---- 第一步：调用生成模型产出任务方向 ----
    # 使用 JSON_RESPONSE_FORMAT 强制模型输出合法 JSON，减少解析失败概率
    raw, metadata = client.complete(
        prompt,
        context,
        max_tokens=GENERATION_MAX_TOKENS,
        response_format=JSON_RESPONSE_FORMAT,
    )
    try:
        # parse_json_text 尝试从 LLM 输出中提取 JSON，支持 markdown 代码块包裹等格式
        # TASK_KEYS 参数要求提取的 JSON 必须包含 example_tasks 这个顶层 key
        value = parse_json_text(raw, TASK_KEYS)
    except Exception as exc:
        # 解析失败时记录错误信息，后续结构校验会捕获并触发修复
        value = {"_parse_error": str(exc)}
    # 对生成结果做结构校验：检查风格覆盖、条目数、非空字符串、去重
    structural_errors = _validate_tasks(value, styles, examples_per_style)
    # 提前创建 judge 客户端，循环中复用（temperature=0.0 确保评审稳定）
    judge_client = _judge_client(client)
    # 记录每次 judge/repair 尝试的元信息，用于最终汇总
    attempts: list[dict] = []
    # 语义评审状态：初始为 failed，只有 judge 判定 pass 才会变为 passed
    semantic_status = "failed"
    semantic_errors: list[str] = []
    semantic_findings: list[dict] = []
    # selected_raw 追踪最终被采纳的原始输出（可能是初次生成或某次修复后的结果）
    selected_raw = raw

    # ---- Judge/Repair 循环：最多尝试 MAX_JUDGE_ATTEMPTS 次 ----
    # 循环逻辑：每次先做评审（结构校验或语义评审），若不通过则修复，修复后进入下一轮评审
    # 终止条件：judge 判定 pass / 达到最大尝试次数 / judge 输出格式不合法 / repair 后结构仍不合法
    for attempt in range(1, MAX_JUDGE_ATTEMPTS + 1):
        # 分支 A：结构校验失败 → 直接构造 structural findings，跳过语义评审
        # 原因：结构不合法时 judge 无法正确解析输入，语义评审没有意义
        if structural_errors:
            findings = _structural_findings(structural_errors, styles, examples_per_style)
            decision = "repair"
            judge_value = {"decision": decision, "findings": findings}
            judge_errors: list[str] = []
        else:
            # 分支 B：结构校验通过 → 调用 judge 模型做语义评审
            # 语义评审关注：任务方向是否贴合场景、风格是否正确、内容质量是否达标
            judge_raw, judge_meta = judge_client.complete(
                judge_prompt,
                _judge_input(context, value),
                max_tokens=JUDGE_MAX_TOKENS,
                response_format=JSON_RESPONSE_FORMAT,
            )
            try:
                judge_value = parse_json_text(judge_raw, JUDGE_KEYS)
            except Exception as exc:
                judge_value = {"_parse_error": str(exc)}
            judge_errors = _validate_judge(judge_value)
            # 将 judge 的输入/输出/校验结果写入缓存，便于事后分析评审质量
            # 缓存文件按 attempt 编号组织，可追溯每次评审的完整上下文
            judge_dir = cache_dir / "judge"
            judge_dir.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-input.json", _judge_input(context, value))
            _write_text_atomic(judge_dir / f"attempt-{attempt:02d}-prompt.md", judge_prompt)
            _write_text_atomic(judge_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(judge_raw, judge_client))
            _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-review.json", judge_value)
            _write_json_atomic(judge_dir / f"attempt-{attempt:02d}-validation.json", {"errors": judge_errors})
            # Judge 输出本身格式不合法 → 无法信任其评审结论，直接终止循环
            # 此时不应尝试修复，因为 repair 依赖 judge findings 的格式来定位问题
            if judge_errors:
                semantic_errors = [f"judge response invalid: {error}" for error in judge_errors]
                attempts.append({"attempt": attempt, "decision": "invalid", "errors": judge_errors})
                break
            findings = list(judge_value.get("findings", []))
            decision = judge_value.get("decision")
            semantic_findings = findings
            # 记录本次评审的元信息，包括模型、finding 数量、响应摘要等
            attempts.append({
                "attempt": attempt,
                "decision": decision,
                "finding_count": len(findings),
                "judge_model": judge_client.model,
                "response_sha256": digest(judge_raw),
            })
            # Judge 判定通过 → 流程结束，清空所有错误状态
            if decision == "pass":
                semantic_status = "passed"
                structural_errors = []
                semantic_errors = []
                break
        # 达到最大尝试次数 → 放弃修复，标记失败
        # 此时不再调用 repair，因为即使修复成功也没有机会再做评审了
        if attempt >= MAX_JUDGE_ATTEMPTS:
            semantic_errors = ["task-example repair limit reached"]
            break

        # ---- Repair 阶段：根据 findings 修复当前输出 ----
        # Repair 使用生成模型（而非 judge 模型），因为修复需要创造性输出
        # repair_input 包含场景上下文 + 当前输出 + judge 诊断，repair 模型据此定向修复
        repair_input = _repair_input(context, value, findings)
        repair_raw, _repair_meta = client.complete(
            repair_prompt,
            repair_input,
            max_tokens=GENERATION_MAX_TOKENS,
            response_format=JSON_RESPONSE_FORMAT,
        )
        try:
            repaired = parse_json_text(repair_raw, TASK_KEYS)
        except Exception as exc:
            repaired = {"_parse_error": str(exc)}
        # 对修复后的结果做结构校验，确保修复没有引入新的结构问题
        repair_errors = _validate_tasks(repaired, styles, examples_per_style)
        # 将 repair 的输入/输出/校验结果写入缓存
        # 与 judge 缓存对称，便于对比评审和修复的输入输出
        repair_dir = cache_dir / "repair"
        repair_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-input.json", repair_input)
        _write_text_atomic(repair_dir / f"attempt-{attempt:02d}-prompt.md", repair_prompt)
        _write_text_atomic(repair_dir / f"attempt-{attempt:02d}-raw_response.txt", _redact_secret(repair_raw, client))
        _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-parsed_output.json", repaired)
        _write_json_atomic(repair_dir / f"attempt-{attempt:02d}-validation.json", {"errors": repair_errors})
        # Repair 后结构仍不合法 → 终止循环
        # 不再继续尝试，因为 repair 模型已经看到了结构问题的诊断但仍未能修复，
        # 继续尝试大概率也是徒劳
        if repair_errors:
            structural_errors = repair_errors
            semantic_errors = [f"repair failed: {error}" for error in repair_errors]
            break
        # Repair 成功 → 用修复后的结果替换原值，进入下一轮 judge
        # 下一轮循环会对修复后的结果重新做结构校验和语义评审
        value = repaired
        structural_errors = []
        selected_raw = repair_raw

    # ---- 汇总校验结果 ----
    # 最终状态由结构校验和语义评审共同决定：两者都通过才算 passed
    validation = {
        # 综合状态：结构和语义都通过才为 passed
        "status": "passed" if not structural_errors and semantic_status == "passed" else "failed",
        # 结构校验状态：JSON 格式和字段完整性
        "structural_status": "passed" if not structural_errors else "failed",
        # 语义评审状态：judge 模型对内容质量的判定
        "semantic_status": semantic_status,
        # 错误列表：优先报告语义错误（更具体），其次报告结构错误
        "errors": semantic_errors if semantic_errors else structural_errors,
        # 语义评审的 findings 详情，便于分析具体问题
        "semantic_findings": semantic_findings,
        # 每次 judge/repair 尝试的元信息记录
        "semantic_attempts": attempts,
    }
    # 脱敏处理原始输出，移除 API key 等敏感信息
    safe_raw = _redact_secret(selected_raw, client)
    # 将最终结果写入 case 目录，形成完整的审计链
    # input.json: 生成时使用的场景上下文
    # prompt.md: 生成时使用的 prompt 模板
    # raw_response.txt: 最终被采纳的原始输出（脱敏后）
    # validation.json: 校验结果汇总
    # metadata.json: 模型信息、数据摘要等元信息
    _write_json_atomic(case_dir / "input.json", context)
    _write_text_atomic(case_dir / "prompt.md", prompt)
    _write_text_atomic(case_dir / "raw_response.txt", safe_raw)
    _write_json_atomic(case_dir / "validation.json", validation)
    _write_json_atomic(case_dir / "metadata.json", {
        # 记录生成模型和 judge 模型的身份，便于追溯和复现
        "model_identity": {"provider": client.provider, "model": client.model},
        "judge_model_identity": {"provider": judge_client.provider, "model": judge_client.model},
        # SHA-256 摘要用于数据指纹，可快速判断输入/输出是否变化
        "prompt_sha256": digest(prompt),
        "input_sha256": digest(context),
        "response_sha256": digest(safe_raw),
        # 标记 API key 未被序列化到文件中，确保安全
        "api_key_serialized": False,
    })
    # 只有结构校验通过时才写入最终的任务方向文件
    # 结构不通过时 value 可能包含 _parse_error 等无效字段，不应写入
    if validation["structural_status"] == "passed":
        _write_json_atomic(case_dir / "example_tasks.json", value)
    return {
        "scenario_id": scenario_id,
        # 保留完整的 scenario 信息，便于后续合并回 taxonomy
        "scenario": scenario,
        # 结构不通过时返回空字典，避免下游使用无效数据
        "example_tasks": value if validation["structural_status"] == "passed" else {},
        "validation": validation,
        "model": client.model,
        "provider": client.provider,
        "judge_model": judge_client.model,
        # 记录实际使用的风格，HTML 渲染时需要知道展示哪些风格列
        "styles": styles,
        # 记录是否为单风格模式，合并回 taxonomy 时需要区分处理
        "target_style": target_style,
    }


def _render_html(run_dir: Path, run_id: str, results: list[dict]) -> Path:
    """Render an HTML review page for task-direction generation results.

    将生成结果渲染为可读的 HTML 审查页面，方便人工快速浏览每个 scenario 的
    任务方向生成情况和校验状态。每个 scenario 以卡片形式展示，包含：
    - 场景标识和通过/未通过状态
    - 各风格下的任务方向列表
    - 校验错误详情（可展开查看）

    参数:
        run_dir: 运行输出目录
        run_id: 本次运行标识
        results: _generate_one 的返回结果列表

    返回:
        生成的 HTML 文件路径
    """
    cards: list[str] = []
    render_styles = tuple(results[0].get("styles", STYLES)) if results else STYLES
    for result in results:
        scenario = result["scenario"]
        validation = result["validation"]
        tasks = result.get("example_tasks", {}).get("example_tasks", {}) if isinstance(result.get("example_tasks"), dict) else {}
        passed = validation.get("status") == "passed"
        lists = "".join(
            f"<section><h3>{html.escape(style)}</h3><ol>{''.join(f'<li>{html.escape(str(item))}</li>' for item in tasks.get(style, []))}</ol></section>"
            for style in render_styles
        )
        errors = validation.get("errors", [])
        error_html = "" if not errors else f"<details class='errors'><summary>Validation findings</summary><pre>{html.escape(json.dumps(errors, ensure_ascii=False, indent=2))}</pre></details>"
        cards.append(f"""
        <article class='case'>
          <header><span class='case-id'>{html.escape(str(scenario.get('scenario_id', '')))}</span><span class='status {'ok' if passed else 'bad'}'>{'Judge 通过' if passed else '未通过'}</span></header>
          <h2>{html.escape(str(scenario.get('domain', '')))} / {html.escape(str(scenario.get('scenario', '')))}</h2>
          {lists}
          {error_html}
        </article>""")
    model = results[0].get("model", "") if results else ""
    judge_model = results[0].get("judge_model", "") if results else ""
    output = f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Stage-00 Task Examples</title><style>
:root {{ color-scheme:light; --ink:#1d2733; --muted:#607080; --line:#d9e0e7; --paper:#f5f7f9; --accent:#146c94; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.7 system-ui,-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:42px 24px 72px; }} h1 {{ margin:0 0 8px; font-size:30px; }} .lead {{ color:var(--muted); margin:0 0 28px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(390px,1fr)); gap:18px; }} .case {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:22px; box-shadow:0 4px 16px #14202b0d; }}
header {{ display:flex; justify-content:space-between; gap:12px; }} .case-id {{ color:var(--accent); font-weight:700; }} .status {{ border-radius:999px; padding:2px 9px; font-size:12px; }} .status.ok {{ color:#17633d; background:#e6f5ec; }} .status.bad {{ color:#8a2b2b; background:#fde9e9; }}
h2 {{ font-size:19px; line-height:1.35; margin:16px 0 12px; }} h3 {{ color:var(--accent); font-size:14px; margin:14px 0 4px; }} ol {{ margin:0; padding-left:24px; }} li {{ margin:3px 0; }} pre {{ white-space:pre-wrap; overflow:auto; background:#f6f8fa; border:1px solid var(--line); padding:10px; font-size:12px; }} .errors {{ color:#8a2b2b; margin-top:14px; }}
@media(max-width:600px) {{ main {{ padding:26px 14px 48px; }} .grid {{ grid-template-columns:1fr; }} }}
</style></head><body><main><h1>Stage-00 Task Examples</h1><p class='lead'>Run {html.escape(run_id)} · {len(results)} 个 Scenario · 生成模型：{html.escape(str(model))} · Judge：{html.escape(str(judge_model))}</p><div class='grid'>{''.join(cards)}</div></main></body></html>"""
    path = run_dir / "task-example-review.html"
    _write_text_atomic(path, output)
    return path


def main() -> None:
    """CLI entry point: generate task directions for taxonomy scenarios.

    主入口函数，完成以下流程：
    1. 解析命令行参数（taxonomy 路径、输出路径、模型选择等）
    2. 从 taxonomy 中筛选待处理的 scenario
    3. 跳过已有足够样本的 scenario（单风格模式下）
    4. 并行调用 _generate_one 生成任务方向
    5. 将生成结果合并回 taxonomy JSON
    6. 输出 manifest 和 HTML 审查页面
    """
    parser = argparse.ArgumentParser()
    # --taxonomy: 源 taxonomy 文件路径，包含 domain/scenario 结构
    parser.add_argument("--taxonomy", type=Path, default=ROOT / "taxonomy/domain-scenario-with-task-examples-gpt56-sol-v5.json")
    # --output: 输出 taxonomy 文件路径，生成结果将合并到此文件
    parser.add_argument("--output", type=Path, default=ROOT / "taxonomy/domain-scenario-with-task-examples.json")
    # --run-id: 运行标识，用于创建 runs 子目录，默认按时间戳生成
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S") + "-task-examples")
    # --scenario-id / --scenario-ids: 指定只处理某个或某些 scenario，用于试运行
    parser.add_argument("--scenario-id", help="generate only one Scenario for a pilot run")
    parser.add_argument(
        "--scenario-ids",
        help="generate a comma-separated set of Scenario IDs without changing the source taxonomy",
    )
    # --workers: 并行线程数，控制并发调用 LLM 的数量
    parser.add_argument("--workers", type=int, default=4)
    # --examples-per-style: 每种风格需生成的任务方向条数
    parser.add_argument(
        "--examples-per-style",
        type=int,
        default=DEFAULT_EXAMPLES_PER_STYLE,
        help="number of task directions required in every generated style",
    )
    # --style: 单风格生成模式，只生成指定风格并合并到已有 example_tasks 中
    # 配合 --force 可强制重新生成已有风格
    parser.add_argument(
        "--style",
        choices=ALL_STYLES,
        help="generate only this style and merge it into existing example_tasks",
    )
    # --force: 配合 --style 使用，即使已有足够样本也强制重新生成
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --style, regenerate the selected style even when the source taxonomy already has five examples",
    )
    # --no-html: 跳过 HTML 审查页面生成
    parser.add_argument(
        "--no-html",
        action="store_true",
        help="skip the task-example HTML review artifact",
    )
    # Stage 00 默认使用 GPT 模型：任务方向作为 taxonomy 样本，需要高质量的 GPT 基线
    # Stage 00 is intentionally GPT-backed: its task directions are used as
    # taxonomy examples, so keep the reviewed GPT baseline as the default.
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--judge-model", default="gpt-5.6-sol")
    args = parser.parse_args()
    if args.workers < 1 or args.examples_per_style < 1:
        parser.error("--workers and --examples-per-style must be positive")
    if args.scenario_id and args.scenario_ids:
        parser.error("use only one of --scenario-id and --scenario-ids")
    requested_ids = None
    if args.scenario_ids:
        requested_ids = {item.strip() for item in args.scenario_ids.split(",") if item.strip()}
        if not requested_ids:
            parser.error("--scenario-ids must contain at least one Scenario ID")
    taxonomy_path = args.taxonomy if args.taxonomy.is_absolute() else ROOT / args.taxonomy
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    # 从 taxonomy 中提取所有 scenario，并附上所属 domain 的信息
    scenarios: list[dict] = []
    for domain in taxonomy.get("domains", []):
        for scenario in domain.get("scenarios", []):
            scenario_id = scenario.get("scenario_id")
            # 根据 --scenario-id 或 --scenario-ids 过滤
            if args.scenario_id and scenario_id != args.scenario_id:
                continue
            if requested_ids is not None and scenario_id not in requested_ids:
                continue
            # 将 domain 展平到 scenario 中，便于后续生成时获取领域上下文
            scenarios.append({
                "domain_id": domain.get("domain_id"),
                "domain": domain.get("domain"),
                "domain_definition": domain.get("definition", ""),
                **copy.deepcopy(scenario),
            })
    if not scenarios:
        raise ValueError("no matching scenarios")
    # 校验 --scenario-ids 中是否有不存在的 ID
    if requested_ids is not None:
        found_ids = {str(item["scenario_id"]) for item in scenarios}
        missing_ids = sorted(requested_ids - found_ids)
        if missing_ids:
            raise ValueError(f"unknown scenario IDs: {', '.join(missing_ids)}")
    # 初始化 LLM 客户端，GPT 模型走专用通道
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
    results: list[dict] = []
    failures: list[str] = []
    skipped_existing: list[str] = []
    pending_scenarios: list[dict] = []
    # 单风格模式下，跳过已有足够样本的 scenario，避免重复生成
    # 判断条件：该风格下已有 examples_per_style 条非空字符串
    for scenario in scenarios:
        if args.style and not args.force:
            existing = scenario.get("example_tasks", {}).get(args.style) if isinstance(scenario.get("example_tasks"), dict) else None
            if isinstance(existing, list) and len(existing) == args.examples_per_style and all(isinstance(item, str) and item.strip() for item in existing):
                skipped_existing.append(str(scenario["scenario_id"]))
                continue
        pending_scenarios.append(scenario)
    # 并行生成：使用 ThreadPoolExecutor 控制并发数
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        jobs = {
            executor.submit(_generate_one, scenario, run_dir, client, args.style, args.examples_per_style): scenario["scenario_id"]
            for scenario in pending_scenarios
        }
        for job in as_completed(jobs):
            scenario_id = jobs[job]
            try:
                results.append(job.result())
            except Exception as exc:
                failures.append(f"{scenario_id}: {_redact_secret(str(exc), client)}")
    results.sort(key=lambda item: item["scenario_id"])

    # ---- 将生成结果合并回 taxonomy ----
    # 深拷贝源 taxonomy，避免修改原始数据
    output_taxonomy = copy.deepcopy(taxonomy)
    result_by_id = {item["scenario_id"]: item for item in results}
    for domain in output_taxonomy.get("domains", []):
        for scenario in domain.get("scenarios", []):
            result = result_by_id.get(scenario.get("scenario_id"))
            # 只合并结构校验通过的结果
            if not result or result["validation"].get("structural_status") != "passed":
                continue
            # 生成成功后移除种子字段，种子仅在生成时起提示作用
            scenario.pop("example_task_seeds", None)
            generated_tasks = result["example_tasks"].get("example_tasks", {})
            # 单风格模式：只替换指定风格，保留其他风格的已有数据
            if args.style:
                existing_tasks = scenario.get("example_tasks")
                if not isinstance(existing_tasks, dict):
                    existing_tasks = {}
                existing_tasks[args.style] = generated_tasks.get(args.style, [])
                scenario["example_tasks"] = existing_tasks
            else:
                # 全量模式：直接替换整个 example_tasks
                scenario["example_tasks"] = generated_tasks
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    # 原子写入输出 taxonomy
    _write_json_atomic(output_path, output_taxonomy)
    # 写入 manifest，记录本次运行的完整元信息
    _write_json_atomic(run_dir / "manifest.json", {
        "run_id": args.run_id,
        "source_taxonomy": str(taxonomy_path),
        "output_taxonomy": str(output_path),
        "scenario_ids": [item["scenario_id"] for item in scenarios],
        "scenario_count": len(scenarios),
        "generated_style": args.style,
        "examples_per_style": args.examples_per_style,
        "skipped_existing": skipped_existing,
        "model": client.model,
        "provider": client.provider,
        "judge_model": client.judge_model,
        "failures": failures,
    })
    # 若有 scenario 生成异常则抛出运行时错误
    if failures:
        raise RuntimeError("; ".join(sorted(failures)))
    if not args.no_html:
        print(_render_html(run_dir, args.run_id, results))


if __name__ == "__main__":
    main()
