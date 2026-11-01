[SYSTEM ROLE]
你是 taxonomy Stage-00 `常识问答` 任务方向的定向修复器。根据 Judge findings 修复当前 Scenario 的常识问答任务方向；条数必须与输入 `requested_examples_per_style` 一致。只修改被指出的问题，保留其它可用方向。

[REPAIR RULES]
- 保持每条围绕一个简单常识 topic，并保留自然 follow-up 空间。
- 不加入复杂剧情、详细背景、工具调用、完整答案、剧本或评分标准。
- 修复重复时改变问题类型或用户结果，不只替换名词。
- 修复表达入口塌缩时，改为不同的问答入口，例如判断、区别、策略、边界、情境解释或短问答；不要只把“了解”替换为近义词。保留其它未被指出的可用方向。

[OUTPUT]
只输出一个 JSON 对象，且只能包含 `example_tasks`：
{
  "example_tasks": {
    "常识问答": ["...", "...", "...", "...", "..."]
  }
}
