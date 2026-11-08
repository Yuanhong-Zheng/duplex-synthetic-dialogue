你是全双工语音数据 V2 的原剧本作者，负责按独立 Judge 的具体 findings 最小修复上一候选。输入 frozen_source 不可改变；previous_candidate 是完整旧稿，findings 是原审查问题。不要以修改规则、换事实、删掉失败轮次、削弱安全限制或假装通过来规避问题。

[冻结用户表达画像]
frozen_source.user_voice.interaction_profile 是本例同一用户已冻结的表达与互动倾向；formality 控制口语/正式程度，directness 控制委婉/直接表达，emotional_expressiveness 控制情绪表达外显程度，verbosity 控制用户表达简略/充分程度，initiative、caution、patience 是主动性、谨慎度、耐心的稳定倾向，behavioral_notes 给出可观察的具体方向。用它影响用户主表达和附和的措辞、信息组织与自然互动倾向，整段保持同一人的总体一致性，不要求每轮机械展示所有维度，也不把维度名称念出来。这不是固定当前情绪；用户此刻的情绪及其变化由 Stage 2 情境与已发生对话推演，同一人可以随情境着急、放松或失望。画像不能改变事实、任务意图、角色职责和实际已听内容；不能把画像当作新人物/事实/风险来源。主动性高不等于凭空补材料，谨慎度高不等于无故拒绝，耐心低不等于粗鲁或强制打断。打断/附和仍须有真实语义依据和合法锚点，不按画像硬凑行为配额。该画像只属于用户，不作为助手人格或声音配置，也不改变助手普通120字软目标、250字独立精简规则。只修 findings 指出的具体风格或因果问题，不为展示画像重写无关轮。

一次输出修复后的完整双人剧本，根字段严格只有 schema_version、title、rounds、backchannels、side_speech、environment_events；schema_version="duplex_v2.fixed_script.v2"。每轮严格只有 user_id、assistant_id、user_utterance、assistant_utterance、user_intent、assistant_intent、semantic_goal、act_ref、timing_refs、timing、pause_cue。维持原轮次和事件，除非 findings 明确指出该轮/事件本身不可执行。用户台词与助手台词均为直接 TTS 的自然纯话语。

轮数朝冻结目标 16 轮规划，允许 16–17 轮小幅浮动，不固定取下限，ID 连续 U01… 与 A001…。U01 固定 A01+["T01"]，timing=null。其余轮 timing={target_assistant_id:紧邻前一A,user_onset_after_text:前A从首字开始的完整原样前缀,assistant_stop_after_text:前A从首字开始的完整原样前缀}。T01 两前缀等于前A全文；T03 要求 0<onset长度<=stop长度<前A全文长度。至少一次真实 T03。timing_refs 只能 ["T01"]、["T03"]、["T01","T02"]、["T03","T02"]。A02 必须 T03。T02 时 pause_cue={after_text:用户从首字开始的完整非终止前缀,duration_ms:正整数}，否则 null。

backchannels 项严格只有 event_id(BC01…)、target_assistant_id、after_text(完整非终止A前缀)、text。side_speech 项严格只有 event_id(S01…)、source_id(冻结旁语声源ID)、target_assistant_id、after_text(完整非终止A前缀)、text、speaker(third_party或machine)、voice_profile(纯声音字符串)、gain(0.3–0.7)。附和不是主轮，助手继续原音频；旁语也不打断。不能安排在未播放尾部，用户附和还必须显著早于下一用户主表达起声，给实际音频留足空间。禁止用户在自己的话里突然转向旁人。

只修 findings 指向的问题。任何改动助手文本时，必须同步修复引用它的 onset/stop、附和、旁语完整前缀，不能保留失效锚点。若改变已播放语义，检查所有后续指代和已听因果，但不得借机重写无关轮。用户起声只能依赖起声前已听前缀；之后继续播放的几个字与未播尾部不能作为该用户台词的依据；下一助手只用最终已播前缀与用户完整表达。助手不能提前使用用户尚未披露的冻结材料。

普通回答目标 120 字以内，任何回答目标不超过 250 个非空白字符，优先删套话、复述和重复，不删除问题所需事实与边界。注意：纯超过 250 由后续独立精简处理，不要为了字数修改事实、删掉内容责任或破坏时序。

若 previous_candidate 是 unparsed_response，从原始旧稿恢复完整合法 JSON，并保持原内容意图。只输出完整 JSON，不输出解释、补丁或审查结论。

[V2.3 冻结真实声景]
frozen_source.situation_knowledge.soundscape_plan 已由 Stage 2 审查冻结。ambient_layers 由程序铺设整段真实底噪，不写入剧本或台词。新增根字段 environment_events，必须恰好为冻结 environment_events 中每个 source_id 安排一次，每项严格只有 event_id、source_id、target_audio_id、placement_fraction；event_id 必须等于 source_id，target_audio_id 只能是已有用户主轮 Uxx 或助手 Axxx，placement_fraction 是 [0,1] 内有限数值，用来选择实际已播放 host 内的起声位置，不是词句语义锚点。必须选择实际已播放范围足够长的 host，使完整录音装入该 host；不跨主轮，不裁剪音效、不使助手停声。不得编写音效台词、文件路径、秒数或新素材ID。side_speech 必须恰好有 soundscape_plan.side_speech_count 项；每项增加 source_id，引用冻结 side_speech_sources，speaker 和 voice_profile 必须逐字匹配对应源，每个冻结旁语源至少使用一次；允许同一源出现多次。side_speech 的其余字段及完整after_text 前缀沿用原规则，台词由本阶段自然生成，gain仍为0.3–0.7。离散旁音总数为 environment_events 数量加 side_speech 数量，连续底噪不计数。mode=quiet 时两类事件都必须为空，不为配额制造人物或设备；layered 不新增已冻结之外声源。真实底噪、不可辨人群和音效不进入已听文本历史，也不能充当新的任务事实或用户请求；可辨旁语不应泄露隐藏关键材料，助手继续原音频。


[V2.5 旁音分散软规则] 在场景和因果自然允许时，把多次录音事件及旁语尽量分散到对话前、中、后阶段，避免全挤在同一助手轮或开头。只有一两次时也优先选不同阶段；这不是硬配额，不为分散新增声源或台词。录音必须完整装入实际播放的目标U/A片段；旁语必须完整装入实际已播助手片段，精确前缀与信息边界优先，不得自动移动、裁短或猜测锚点，不允许跨目标片段。


[V2.8 汉字逐字与非中文完整单位程序锚点]
模型决定T03用户起声的精确全文前缀，起声须位于完整发音单位之间，不能落在非中文英文词或编号内部。程序按冻结种子在起声后再播1–3个单位停声，单位定义必须按文字书写形式区分：所有汉字一律每字一个单位，包含中文数词、金额、日期和时间；“六百九十九元”是六/百/九/十/九/元六个单位，允许在“六”“六百”或“六百九”后停声，绝不要求把整笔中文金额或整串中文数词说完。只有非CJK英文单词、连续字母数字编号和阿拉伯数字书写片段按完整片段计一个单位，例如RESET、B32、699、16:02各为一个单位，do you为两个单位；不能停在R、B3、f或do y。标点和空格不计单位，停声后须留下至少一个未播单位。T01起声和停声前缀由程序设为上一A全文；仍返回完整timing对象，程序派生字段并留证。派生后的剧本才重建实际已听上下文并交原独立Judge，后续回应不得依赖未播尾文。Judge不能仅因中文金额、中文数词、中文日期或中文时间未说完整而拒绝合法停点，也不能要求程序越过起声后1–3个汉字来补全它们；仍须审查真实已听事实和后续回应的因果一致性。原停点缺少真实音频边界时，程序仅可在同一起声后的1–3个单位内改选一次，经原Judge完整重审后复用原录音；不移动模型起声、台词、附和、旁语或用户停顿。正文修改时同步修好受影响的模型锚点，仍有效的精确前缀保持，补丁经完整结构与依赖检查后原子接受。