# 通用训练数据格式 v1

这是独立 GPT 文本策略加 MiniMax TTS 的合成交互导出。当前小批量沿用一个已冻结的
Stage 5 case，各 episode 从 U01 独立调用用户和助手，没有重新生成 Stage 1–5。
它提供可核验的观测、动作、原 Rubric 奖励和已消费 PCM；不包括 tokenizer、
优化器、特定模型训练器，也不标作原生语音模型 on-policy 数据。

## 文件

| 文件 | 内容 |
|---|---|
| `manifest.json` | schema、episode/case ID、源 rollout 与评分目录、全部源文件哈希、样本数、评分摘要、音频资产哈希 |
| `samples.jsonl` | 每行一个实际策略调用的观测、动作、训练区域、来源与奖励引用 |
| `rewards.jsonl` | 每条原 Rubric 恰好一行，保留原文、结论、理由、证据、值和归属 |
| `side-speech.json` | 原环境旁语及其已消费音频；不是新增策略目标或奖励 |
| `audio/<sha256>.wav` | 16 kHz、PCM16、单声道的实际消费前缀；按内容哈希去重 |
| `artifact-hashes.json` | 导出文件的校验和；验证输出文件可另行添加 |

源目录中的完整生成响应、未播文字/音频仍用于审计，不能直接拼入训练目标。
文件哈希校验需要保留源 rollout 与评分目录；迁移文件后应显式更新外部目录映射，
不能用别的同名 case 代替来源。

## 策略样本

共同字段：

- `sample_id` / `episode_id`：跨 episode 唯一；同样输入也不跨 episode 重用调用。
- `policy_role`：`assistant`、`assistant_listener` 或 `user`。
- `observation.system_prompt` / `observation.context`：该模型调用实际收到的 Prompt 和上下文。
- `observation.aligned_pcm_prefixes`：与已听完成单词对应的 PCM 前缀；每项标明资产路径、
  原音频 ID、源路径和帧边界。模型原请求使用文字，这些音频是训练导出的对齐附件，
  不能据此宣称原策略实际输入了音频。
- `provenance`：原调用目录、输入哈希、模型，以及最终运行尝试是否新增网络请求。
- `trainable`：该样本是否存在可用的策略训练区域；它不表示任务评分通过。
- `reward_annotation_ids`：只引用该动作所属的原标准；没有新奖励时是空数组。

三种动作：

| 角色 | 动作与目标 | 奖励 |
|---|---|---|
| `assistant` | `text_response`，正文仅为实际 `heard_text`；附件为消费到停播/自然结束的 PCM | 原 Semantic 与语义恢复标准，绑定本轮回复组 |
| `assistant_listener` | `listener_decision`，保留实际的等待、停声、继续或响应决定及原 JSON 输出 | 原 T02 等待 / T03 停声标准，绑定对应控制动作 |
| `user` | `user_policy`，保留原候选 JSON 和实际用户语音 | 不接受助手奖励；user guard 是独立验证标签 |

环境触发观察器不是被训助手策略，不能导出为助手动作或获得助手奖励。
用户/收听控制器的 JSON 训练目标与配对语音不是同一种 token 序列；用户语音对应候选
中的实际 utterance，不能把整个 JSON 当作音频字幕。

## 训练区域

`training_region.target_text` 给出允许训练的原目标。`character_ranges` 使用半开区间
`[begin,end)`，单位是 Python/Unicode 字符；`audio_frame_ranges` 单位是 16 kHz PCM 帧。
它们不是 tokenizer token 或语音 codec token。模型专用 adapter 必须选择 tokenizer、
完成映射，并重新核验边界后才能形成模型 loss mask。

助手生成音频可能在单词中途被停止。`action.paired_consumed_audio` 保留真实已消费 PCM，
而 audio loss 仅到最后一个完整 provider 单词的结束帧；截断末词不进入 loss。
正文目标也只保留已完成单词释放的 `heard_text`，不含收到但未播出的尾部。
`generated_boundary` 记录收到、消费和可训练的帧数及停播结果，三者可能不同。
模型 adapter 应区分被打断的前缀与自然结束，不得自行给停播前缀添加表示模型
自然完成的 EOS/completion loss 标签；当前通用格式没有定义这类 token 标签。

## 原标准奖励

`rewards.jsonl` 中 `criterion` 完整保留评分项及其证据，`value` 为 1 / 0 / null：

- `pass` → 1，`fail` → 0。
- `insufficient_evidence` / `not_triggered` → null，`reward_mask=false`。
- `sample_ids` 指向对应回复组或实际收听动作，`credit_scope` 明确归属。
- 一条标准只写一次，`duplicated_per_group_member=false`。多回复组不能按成员重复累加。
- 若控制失败且没有可观察的收听动作，保留原失败奖励并将策略归属置空，
  不把奖励转给下一条正文回复。缺少动作归属的行不能自动用于策略梯度。

训练器应显式筛选 `policy_role`，并按 annotation ID 连接奖励。用户 guard、
环境触发或“发生了一次打断”本身都不能当作助手正奖励。
这套二值奖励是原标准的逐项记录，不定义新的 episode 奖励聚合或 advantage。

## 使用与核验

```bash
python3 -B -m duplex_runtime.export_dataset \
  --run-dir runtime_runs/<completed-rollout> \
  --score-dir runtime_runs/<verified-scoring> \
  --output-dir runtime_runs/<new-dataset> --episode-id E001

python3 -B -m duplex_runtime.verify_dataset \
  --dataset-dir runtime_runs/<new-dataset>
```

验证器核对实际调用输入、消费文字、完成单词边界、原 PCM 前缀、奖励覆盖与控制归属。
拒绝不完整 rollout；需要部分轨迹训练时先定义独立的 partial-export 策略。
失败评分仍保留并正常导出，不通过重新生成或过滤掉失败来构造全通过数据。

## 版本、恢复与费用

batch 固定采样来源、随机种子、模型、核心源码/Prompt 哈希和源码快照。
每个 episode 有独立持久 journal；同 episode 的恢复核对请求身份和提交文件哈希。
重放成功调用不产生新网络请求；失败/未完成调用及其重试各自保留。
进程恢复必须重建并匹配已接受事件和 PCM 检查点后才能继续。
受控退出 86 是进程恢复验证，不是供应商服务故障。

`cost.json` 按实际 journal try 记账，保留供应商原 token usage、TTS 请求字数和状态。
缺少供应商金额或可靠价目时 `amount` / `currency` 为 null，失败请求是否收费也未知。
API 墙钟等待与离线 PCM 时间分别记录，不能据采样停声时间推出原生实时延迟。
每个 batch runner 的 `workers` 限制自身并发；多个同时启动的 runner 不提供全局配额。
