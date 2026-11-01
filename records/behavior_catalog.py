"""Act (A01-A06) 和 Timing (T01-T03) 的行为定义目录。

本模块定义了离线流水线使用的六种用户发起行为族（Act）和三种时序类型（Timing）。
这些定义是 episode script 中行为控制器的语义基础，被多个模块引用：
- build_casebook_html.py：在 HTML 报告中展示 Act/Timing 的含义
- episode script 生成：作为 act_ref/timing_refs 的语义锚点
- environment audit：作为评分标准的参考

设计要点：
- 环境音（ambient sound）是 episode 级别的音频层，不是用户 Act 或行为分支，
  因此故意不包含在此目录中
- 每个定义包含 ref、中文名称、行为分组和详细定义
"""
from __future__ import annotations

# 六种用户发起行为族（Act）的定义
# 按行为分组：主任务、播报控制、信息请求、状态更新、立场、话题管理
ACT_DEFINITIONS = {
    "A01": {
        "act_ref": "A01", "behavior_name": "建立任务或提供信息", "behavior_group": "主任务",
        "definition": "建立初始目标、首次提供或继续补充尚未进入 task state 的任务信息，或回答助手对当前任务的提问；基于已有对话状态或已播放内容请求解释、总结、确认或重述属于 A03。",
    },
    "A02": {
        "act_ref": "A02", "behavior_name": "控制当前播报", "behavior_group": "播报控制",
        "definition": "要求助手停止、跳过、缩短、收束为总结、加速或放慢当前播报；不自动取消任务。",
    },
    "A03": {
        "act_ref": "A03", "behavior_name": "内容追问、澄清或重述请求", "behavior_group": "信息请求",
        "definition": "针对已进入对话状态或已播放内容，请求解释、总结、确认、重复、换一种表达或让助手补充回答；不直接改变任务状态。",
    },
    "A04": {
        "act_ref": "A04", "behavior_name": "更新任务状态", "behavior_group": "状态更新",
        "definition": "纠正旧信息，或新增、替换、删除任务条件。",
    },
    "A05": {
        "act_ref": "A05", "behavior_name": "异议、拒绝或不满", "behavior_group": "立场",
        "definition": "质疑、拒绝或表达不满，但没有明确的新事实或状态变化。",
    },
    "A06": {
        "act_ref": "A06", "behavior_name": "话题或目标管理", "behavior_group": "话题管理",
        "definition": "暂停主任务并进入可分离的临时支线，或明确返回先前主任务；不得永久替换本 episode 的冻结目标。",
    },
}

# 三种时序类型（Timing）的定义
# T01=自然接话，T02=句内节奏保持，T03=播报中途起声
TIMING_DEFINITIONS = {
    "T01": {"timing_ref": "T01", "timing_name": "自然接话", "definition": "助手已完成或让出当前语义-韵律单元后，主用户在自然话轮边界开始说话；边界处轻微声学重叠仍归此类。"},
    "T02": {"timing_ref": "T02", "timing_name": "句内节奏保持", "definition": "主用户在自己的同一话轮内用停顿、犹豫、找词、延长音或话语标记（如 you know、well、就是、我想一下）组织节奏并继续主要表达；这些现象不表示交出话权，且可与 T01 或 T03 组合。"},
    "T03": {"timing_ref": "T03", "timing_name": "播报中途起声", "definition": "主用户在助手明确尚未完成或让出当前语义-韵律单元时开始说话。"},
}


def act_by_ref() -> dict[str, dict]:
    """Return the active A01-A06 directory used by offline artifacts.

    返回当前活跃的 A01-A06 行为定义目录的深拷贝。
    使用 dict(item) 创建浅拷贝，防止调用者意外修改原始定义。

    Returns:
        ref → 定义 dict 的映射
    """
    return {ref: dict(item) for ref, item in ACT_DEFINITIONS.items()}


def timing_by_ref() -> dict[str, dict]:
    """Return the stable three-timing directory used by renderers.

    返回稳定的 T01-T03 时序定义目录的深拷贝。
    与 act_by_ref() 一样使用浅拷贝保护原始数据。

    Returns:
        ref → 定义 dict 的映射
    """
    return {ref: dict(item) for ref, item in TIMING_DEFINITIONS.items()}
