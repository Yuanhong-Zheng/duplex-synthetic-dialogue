"""Freeze a jointly planned dialogue before any speech synthesis.

There is no runtime actor, user simulator or trigger model in this module.
An independent Judge receives mechanically reconstructed role-visible history.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

from judge_output_repair import complete_checked_judge
from minimax_client import parse_json_text, sol_reasoning_effort, effective_output_tokens
from record_model_call import RecordedCalls

SCHEMA_VERSION = "duplex_v2.fixed_script.v1"
SOUNDSCAPE_SCHEMA_VERSION = "duplex_v2.fixed_script.v2"
ROOT_KEYS = {"schema_version", "title", "rounds", "backchannels", "side_speech"}
ROUND_KEYS = {"user_id", "assistant_id", "user_utterance", "assistant_utterance",
              "user_intent", "assistant_intent", "semantic_goal", "act_ref",
              "timing_refs", "timing", "pause_cue"}
PROMPTS = Path(__file__).with_name("prompts")
MAX_ASSISTANT_CHARS = 250
LEGACY_TOKEN_BUDGETS = {"generation": 16000, "judge": 6000, "compact": 6000}
TIER_TOKEN_BUDGETS = {
    "short": LEGACY_TOKEN_BUDGETS,
    "medium": {"generation": 24000, "judge": 8000, "compact": 8000},
    "long": {"generation": 48000, "judge": 12000, "compact": 10000},
}
LEGACY_PROMPT_VALUES = {
    "{{ROUND_PLANNING}}": "通常写 7–9 轮，可在 5–10 轮内自然安排",
    "{{ROUND_REQUIREMENT}}": "rounds 必须 5–10 轮",
    "{{REPAIR_ROUND_REQUIREMENT}}": "5–10 轮",
    "{{EVENT_PLANNING}}": "自然规划约 2–4 次真实打断、3–5 次用户附和、适当的一次第三方或设备旁语，有自然机会可使用一次用户句内停顿",
    "{{LENGTH_REVIEW_RULES}}": "",
    "{{INTERACTION_PROFILE_RULES}}": "",
    "{{SCRIPT_SCHEMA}}": SCHEMA_VERSION,
    "{{ENVIRONMENT_ROOT}}": "",
    "{{SIDE_SOURCE_FIELD}}": "",
    "{{SIDE_SOURCE_KEY}}": "",
    "{{REPAIR_ENVIRONMENT_ROOT}}": "",
    "{{SOUNDSCAPE_RULES}}": "",
    "{{COMPACTION_EXHAUSTION_RULE}}": "即使保留完整信息仍会超过 250，也不要为长度破坏内容；系统会保留此前已过审全文并标记超长。",
    "{{COMPACTION_BASE_RULE}}": "content_approved_script 已通过内容 Judge",
    "{{COMPACTION_CONTENT_RULE}}": "这是独立最多三次预算，不能丢掉已过审内容责任。用户事实、条件、来源限定、安全边界、后续指代所依赖的实质信息都必须保留。",
}


class ScriptGenerationError(RuntimeError):
    """A retained candidate exhausted its bounded owner-repair budget."""

    def __init__(self, message: str, report_path: Path):
        super().__init__(message)
        self.report_path = str(report_path)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def assistant_char_count(text: str) -> int:
    """Count every non-whitespace character, including punctuation."""
    return len("".join(text.split()))


def _length_errors(plan: Any) -> list[str]:
    from duplex_v2.length import validate_length_plan
    return validate_length_plan(plan)


def _source_controls(source: dict) -> tuple[dict | None, dict | None]:
    """Validate frozen controls; never silently turn invalid new data into legacy."""
    plan = source.get("dialogue_length_plan")
    if "dialogue_length_plan" in source:
        errors = _length_errors(plan)
        if errors:
            raise ValueError("invalid dialogue_length_plan: " + "; ".join(errors))
    voice = source.get("user_voice")
    profile = voice.get("interaction_profile") if isinstance(voice, dict) else None
    if isinstance(voice, dict) and "interaction_profile" in voice:
        from duplex_v2.persona import validate_interaction_profile
        errors = validate_interaction_profile(profile)
        if errors:
            raise ValueError("invalid interaction_profile: " + "; ".join(errors))
    return plan, profile


def script_token_budgets(source: dict) -> dict[str, int]:
    plan, _ = _source_controls(source)
    if source.get('text_output_token_policy') == 'omit':
        return {'generation': None, 'judge': None, 'compact': None}
    return dict(TIER_TOKEN_BUDGETS[plan["tier"]] if plan is not None else LEGACY_TOKEN_BUDGETS)


def _soundscape_source(source: dict) -> dict | None:
    situation = source.get("situation_knowledge", {})
    if not isinstance(situation, dict) or "soundscape_plan" not in situation:
        return None
    from duplex_v2.ambient import validate_soundscape_plan
    soundscape = situation["soundscape_plan"]
    errors = validate_soundscape_plan(soundscape, source)
    if errors:
        raise ValueError("invalid frozen soundscape_plan: " + "; ".join(errors))
    return soundscape


def render_prompt(name: str, source: dict) -> str:
    """Templates render the exact V2.1 bytes when frozen controls are absent."""
    from duplex_v2.language import language, script_prompt, read_prompt
    if language(source) == 'en':
        prompt = script_prompt(name, source)
        from duplex_v2.script_feedback import enabled as feedback_enabled
        if feedback_enabled(source) and name in {'script.md', 'repair.md', 'patch.md', 'judge.md'}:
            if name == 'judge.md':
                prompt = prompt.replace('At most six important findings, each with exactly those five fields.',
                                        'Report all concrete must-fix findings, each with exactly those five fields.')
            rules = 'judge_feedback_rules.md' if name == 'judge.md' else 'author_feedback_rules.md'
            prompt += '\n'+read_prompt(PROMPTS/rules, source)
        if (name == 'compact.md' and source.get('length_compaction_policy') ==
                'incremental_previous_candidate_target_100_105_v1'):
            prompt += '\n'+read_prompt(PROMPTS/'compact_incremental.md', source)
        return prompt
    plan, profile = _source_controls(source)
    from duplex_v2.compaction_contract import enabled as contract_enabled, render_output_contract, readonly_soundscape_rules
    compact_contract = name == 'compact.md' and contract_enabled(source)
    values = dict(LEGACY_PROMPT_VALUES)
    if plan is not None:
        target, lower, upper = plan["target_turns"], plan["min_turns"], plan["max_turns"]
        values["{{ROUND_PLANNING}}"] = (
            f"本例冻结 dialogue_length_plan 为 {plan['tier']} 档，朝 {target} 轮规划，"
            f"可在 {lower}–{upper} 轮内小幅浮动；不要总取下限。"
            "先安排有因果依赖的任务阶段和逐步披露的信息，再写每轮最小充分回应；"
            "不能靠同义复述、反复确认、无关支线或故意加长单条回复凑轮数")
        values["{{ROUND_REQUIREMENT}}"] = f"rounds 以 {target} 轮为目标，必须在冻结允许的 {lower}–{upper} 轮内"
        values["{{REPAIR_ROUND_REQUIREMENT}}"] = f"轮数朝冻结目标 {target} 轮规划，允许 {lower}–{upper} 轮小幅浮动，不固定取下限"
        values["{{EVENT_PLANNING}}"] = (
            "双工机会随整段长度自然分布：每约 2–3 个主轮规划一个用户附和机会、"
            "每约 3–5 个主轮规划一个真实打断机会，作为软多样性提示，不能机械计数或硬凑配额；"
            "适当的一次第三方或设备旁语必须已有冻结声景来源，有自然机会才用用户句内停顿")
        values["{{LENGTH_REVIEW_RULES}}"] = (
            f"\n\n[冻结轮数计划]\n本例目标 {target} 轮，允许 {lower}–{upper} 轮；"
            "程序只检查允许范围。审查是否有足够的真实任务阶段和信息增量，"
            "不要把目标当作每次必须精确相等的硬门，也不要以接近目标为由认可填充轮。"
            "不得要求为长档增加未冻结事实、无关人物、重复确认或无请求的长回答。")
    if profile is not None:
        rules = (
            "\n\n[冻结用户表达画像]\nfrozen_source.user_voice.interaction_profile 是本例同一用户"
            "已冻结的表达与互动倾向；formality 控制口语/正式程度，directness 控制委婉/直接表达，"
            "emotional_expressiveness 控制情绪表达外显程度，verbosity 控制用户表达简略/充分程度，"
            "initiative、caution、patience 是主动性、谨慎度、耐心的稳定倾向，behavioral_notes 给出"
            "可观察的具体方向。用它影响用户主表达和附和的措辞、信息组织与自然互动倾向，"
            "整段保持同一人的总体一致性，不要求每轮机械展示所有维度，也不把维度名称念出来。"
            "这不是固定当前情绪；用户此刻的情绪及其变化由 Stage 2 情境与已发生对话推演，"
            "同一人可以随情境着急、放松或失望。画像不能改变事实、任务意图、角色职责和实际"
            "已听内容；不能把画像当作新人物/事实/风险来源。主动性高不等于凭空补材料，"
            "谨慎度高不等于无故拒绝，耐心低不等于粗鲁或强制打断。"
            "打断/附和仍须有真实语义依据和合法锚点，不按画像硬凑行为配额。"
            "该画像只属于用户，不作为助手人格或声音配置，也不改变助手普通120字软目标、"
            "250字独立精简规则。")
        if name == "judge.md":
            rules += ("审查时看用户多轮表达是否整体违背冻结方向，只有能引用具体台词的实质"
                      "不一致才要求最小修复；不因单轮受任务或情境影响而强求风格标签完全一致。")
        elif name == "compact.md":
            rules += "本次只精简目标助手正文及其直接锚点，不能改用户画像、台词或互动计划。"
        elif name in {"repair.md", "patch.md"}:
            rules += "只修 findings 指出的具体风格或因果问题，不为展示画像重写无关轮。"
        values["{{INTERACTION_PROFILE_RULES}}"] = rules
    soundscape = _soundscape_source(source)
    if soundscape is not None:
        values.update({"{{SCRIPT_SCHEMA}}": SOUNDSCAPE_SCHEMA_VERSION,
                       "{{ENVIRONMENT_ROOT}}": ',\n  "environment_events": []',
                       "{{SIDE_SOURCE_FIELD}}": '"source_id":"SP01",',
                       "{{SIDE_SOURCE_KEY}}": "、source_id(冻结旁语声源ID)",
                       "{{REPAIR_ENVIRONMENT_ROOT}}": "、environment_events"})
        values["{{EVENT_PLANNING}}"] = (
            "双工机会随整段长度自然分布：每约 2–3 个主轮规划一个用户附和机会、"
            "每约 3–5 个主轮规划一个真实打断机会，作为软多样性提示，不能机械计数或硬凑配额；"
            "全部旁音严格遵守冻结 soundscape_plan，不新增声源或发生项；"
            "有自然机会才使用用户句内停顿")
        values["{{SOUNDSCAPE_RULES}}"] = (
            "\n\n[V2.3 冻结真实声景]\n"
            "frozen_source.situation_knowledge.soundscape_plan 已由 Stage 2 审查冻结。"
            "ambient_layers 由程序铺设整段真实底噪，不写入剧本或台词。"
            "新增根字段 environment_events，必须恰好为冻结 environment_events 中每个 source_id 安排一次，"
            "每项严格只有 event_id、source_id、target_audio_id、placement_fraction；"
            "event_id 必须等于 source_id，target_audio_id 只能是已有用户主轮 Uxx 或助手 Axxx，"
            "placement_fraction 是 [0,1] 内有限数值，用来选择实际已播放 host 内的起声位置，"
            "不是词句语义锚点。程序会按真实素材长度选择可完整播放的窗口，允许跨主轮，"
            "不裁剪音效、不使助手停声。不得编写音效台词、文件路径、秒数或新素材ID。"
            "side_speech 必须恰好有 soundscape_plan.side_speech_count 项；每项增加 source_id，"
            "引用冻结 side_speech_sources，speaker 和 voice_profile 必须逐字匹配对应源，"
            "每个冻结旁语源至少使用一次；允许同一源出现多次。side_speech 的其余字段及完整"
            "after_text 前缀沿用原规则，台词由本阶段自然生成，gain仍为0.3–0.7。"
            "离散旁音总数为 environment_events 数量加 side_speech 数量，连续底噪不计数。"
            "mode=quiet 时两类事件都必须为空，不为配额制造人物或设备；layered 不新增已冻结之外声源。"
            "真实底噪、不可辨人群和音效不进入已听文本历史，也不能充当新的任务事实或用户请求；"
            "可辨旁语不应泄露隐藏关键材料，助手继续原音频。"
            + ("本次精简只修改目标A及原精确前缀，environment_events 的所有字段原样保留，"
               "不得修改冻结声源、旁音数量或 fraction。" if name == "compact.md" else "")
            + ("审查声源与真实场景是否相容、台词是否符合冻结人物来源，"
               "不能以个性或事件配额为由编造任务材料。" if name == "judge.md" else ""))
    if name == 'compact.md':
        from duplex_v2.patch import enabled
        if enabled(source):
            values['{{COMPACTION_EXHAUSTION_RULE}}'] = (
                '必须同时满足内容责任和250字上限。独立三次精简后仍未同时通过，整条案例记为失败，'
                '不进入成功数据；原过审稿仅作为失败记录保留。不能为压字数删掉必要事实或安全边界。')
        from duplex_v2.anchor_dependencies import enabled as execution_enabled
        if execution_enabled(source):
            values['{{COMPACTION_BASE_RULE}}'] = 'content_approved_script 是当前逐条精简的基稿，原始内容过审全文另有保留'
            values['{{COMPACTION_CONTENT_RULE}}'] = (
                '这是独立最多三次预算，compaction_attempt 指明本次序号。前两次须同时满足语义与250字上限。'
                '第三次以不超过250字且结构、锚点合法为准，尽可能保留信息，但不再强求语义Judge通过。'
                'prior_semantic_waivers 记录先前轮次的已接受语义未通过项，不修改其他轮台词。')
            values['{{COMPACTION_EXHAUSTION_RULE}}'] = (
                '第三次的语义未通过会明确留档；不得伪称语义过审。250字仍为硬上限，'
                '超长、非法JSON、结构或锚点错误都不能凭语义豁免通过。')
    if source.get('text_policy', {}).get('workflow_version') == 'v2.5':
        values['{{SOUNDSCAPE_RULES}}'] = values['{{SOUNDSCAPE_RULES}}'].replace(
            '程序会按真实素材长度选择可完整播放的窗口，允许跨主轮，',
            '必须选择实际已播放范围足够长的 host，使完整录音装入该 host；不跨主轮，')
    if compact_contract:
        values['{{COMPACTION_OUTPUT_CONTRACT}}'] = render_output_contract('zh')
        # Full-script soundscape instructions must never enlarge the compact
        # response schema. The program retains those fields from the base.
        values['{{SOUNDSCAPE_RULES}}'] = readonly_soundscape_rules('zh') if soundscape is not None else ''
    prompt = (PROMPTS / ('compact_contract.md' if compact_contract else name)).read_text()
    for marker, value in values.items():
        prompt = prompt.replace(marker, value)
    if not compact_contract and source.get('text_policy', {}).get('workflow_version') == 'v2.5':
        from duplex_v2.ambient_v25 import spreading_prompt
        prompt += '\n' + spreading_prompt()
    from duplex_v2.anchor_dependencies import enabled as execution_enabled, prompt_rules
    if execution_enabled(source):
        prompt += prompt_rules(source)
    return prompt


def _call_controls(source: dict, max_tokens: int) -> dict:
    plan, profile = _source_controls(source)
    soundscape = _soundscape_source(source)
    from duplex_v2.compaction_contract import enabled as contract_enabled
    contract = contract_enabled(source)
    if plan is None and profile is None and soundscape is None and not contract:
        return {}
    controls = {"model_token_budgets": ({key: None for key in ('generation', 'judge', 'compact')}
                                       if max_tokens is None else script_token_budgets(source)),
                "output_token_budget": max_tokens}
    if 'text_policy' in source:
        controls['text_policy'] = copy.deepcopy(source['text_policy'])
        controls['reasoning_effort_policy'] = 'default'
    if 'script_execution_policy' in source:
        controls['script_execution_policy'] = copy.deepcopy(source['script_execution_policy'])
        controls['program_anchor_seed'] = source['program_anchor_seed']
    if contract:
        controls['compaction_output_policy'] = source['compaction_output_policy']
    if max_tokens is None:
        controls['text_output_token_policy'] = 'omit'
    if plan is not None:
        controls["dialogue_length_plan"] = copy.deepcopy(plan)
    if profile is not None:
        controls["interaction_profile"] = copy.deepcopy(profile)
    if soundscape is not None:
        controls["soundscape_plan"] = copy.deepcopy(soundscape)
        controls["ambient_control"] = copy.deepcopy(source["ambient_control"])
        controls["ambient_catalog_sha256"] = source["ambient_catalog"]["catalog_sha256"]
    return {"v2_controls": controls}


def validate_script(script: Any, length_plan: dict | None = None,
                    soundscape_plan: dict | None = None) -> list[str]:
    """Check shape, references and exact character-prefix causality only."""
    errors: list[str] = []
    if not isinstance(script, dict):
        return ["/: expected an object"]
    ambient = script.get("schema_version") == SOUNDSCAPE_SCHEMA_VERSION
    root_keys = ROOT_KEYS | ({"environment_events"} if ambient else set())
    if set(script) != root_keys:
        errors.append("/: keys must be " + ", ".join(sorted(root_keys)))
    if not isinstance(script.get("schema_version"), str) or script["schema_version"] not in {SCHEMA_VERSION, SOUNDSCAPE_SCHEMA_VERSION}:
        errors.append("/schema_version: unsupported schema")
    if soundscape_plan is not None and not ambient:
        errors.append("/schema_version: a frozen soundscape requires fixed_script.v2")
    if not _string(script.get("title")):
        errors.append("/title: expected nonempty string")
    if length_plan is not None:
        plan_errors = _length_errors(length_plan)
        if plan_errors:
            return errors + ["/dialogue_length_plan: " + error for error in plan_errors]
    lower, upper = ((length_plan["min_turns"], length_plan["max_turns"])
                    if length_plan is not None else (5, 10))
    rounds = script.get("rounds")
    if not isinstance(rounds, list) or not lower <= len(rounds) <= upper:
        return errors + [f"/rounds: expected {lower}–{upper} rounds"]
    assistants: dict[str, str] = {}
    onset_limits: dict[str, int] = {}
    stop_limits: dict[str, int] = {}
    interrupts = 0
    for i, row in enumerate(rounds):
        path = f"/rounds/{i}"
        if not isinstance(row, dict):
            errors.append(path + ": expected object")
            continue
        if set(row) != ROUND_KEYS:
            errors.append(path + ": round keys differ from schema")
        for key in ("user_id", "assistant_id", "user_utterance", "assistant_utterance",
                    "user_intent", "assistant_intent", "semantic_goal"):
            if not _string(row.get(key)):
                errors.append(path + "/" + key + ": expected nonempty string")
        if row.get("user_id") != f"U{i+1:02d}":
            errors.append(path + "/user_id: IDs must be consecutive U01…")
        if row.get("assistant_id") != f"A{i+1:03d}":
            errors.append(path + "/assistant_id: IDs must be consecutive A001…")
        if not isinstance(row.get("act_ref"), str) or row["act_ref"] not in {f"A{x:02d}" for x in range(1, 7)}:
            errors.append(path + "/act_ref: expected A01–A06")
        refs = row.get("timing_refs")
        if not isinstance(refs, list) or refs not in (["T01"], ["T03"], ["T01", "T02"], ["T03", "T02"]):
            errors.append(path + "/timing_refs: expected T01 or T03, optionally followed by T02")
            refs = []
        if row.get("act_ref") == "A02" and "T03" not in refs:
            errors.append(path + ": A02 requires T03")
        if i == 0:
            if row.get("act_ref") != "A01" or refs != ["T01"] or row.get("timing") is not None:
                errors.append(path + ": U01 must be A01/T01 with null timing")
        else:
            previous = rounds[i - 1]
            previous_id = f"A{i:03d}"
            previous_text = previous.get("assistant_utterance") if isinstance(previous, dict) else None
            timing = row.get("timing")
            if not isinstance(timing, dict) or set(timing) != {
                "target_assistant_id", "user_onset_after_text", "assistant_stop_after_text"
            }:
                errors.append(path + "/timing: expected exact target/onset/stop fields")
            elif not isinstance(previous_text, str):
                errors.append(path + "/timing: previous assistant text is unavailable")
            else:
                if timing.get("target_assistant_id") != previous_id:
                    errors.append(path + "/timing/target_assistant_id: must target previous A")
                onset = timing.get("user_onset_after_text")
                stop = timing.get("assistant_stop_after_text")
                valid = True
                for key, prefix in (("user_onset_after_text", onset), ("assistant_stop_after_text", stop)):
                    if not _string(prefix) or not previous_text.startswith(prefix):
                        errors.append(path + "/timing/" + key + ": must be an exact nonempty full prefix")
                        valid = False
                if valid:
                    onset_limits[previous_id] = len(onset)
                    stop_limits[previous_id] = len(stop)
                    if "T01" in refs and (onset != previous_text or stop != previous_text):
                        errors.append(path + "/timing: T01 starts after the complete previous A")
                    if "T03" in refs:
                        interrupts += 1
                        if not len(onset) <= len(stop) < len(previous_text):
                            errors.append(path + "/timing: T03 requires onset <= stop < complete A")
                        elif not any(character.isalnum() for character in previous_text[len(stop):]):
                            errors.append(path + '/timing/assistant_stop_after_text: T03 must leave unplayed spoken text, not only punctuation')
        utterance = row.get("user_utterance")
        pause = row.get("pause_cue")
        if "T02" in refs:
            if not isinstance(pause, dict) or set(pause) != {"after_text", "duration_ms"}:
                errors.append(path + "/pause_cue: T02 requires after_text/duration_ms")
            else:
                prefix = pause.get("after_text")
                if not _string(prefix) or not isinstance(utterance, str) or not utterance.startswith(prefix) or len(prefix) >= len(utterance):
                    errors.append(path + "/pause_cue/after_text: expected exact nonfinal user prefix")
                duration = pause.get("duration_ms")
                if type(duration) is not int or duration <= 0:
                    errors.append(path + "/pause_cue/duration_ms: expected positive integer")
        elif pause is not None:
            errors.append(path + "/pause_cue: must be null without T02")
        if isinstance(row.get("assistant_utterance"), str):
            assistants[f"A{i+1:03d}"] = row["assistant_utterance"]
    if interrupts == 0:
        errors.append("/rounds: at least one executable T03 interruption is required")
    event_ids: set[str] = set()
    for collection, event_prefix in (("backchannels", "BC"), ("side_speech", "S")):
        events = script.get(collection)
        if not isinstance(events, list):
            errors.append("/" + collection + ": expected array")
            continue
        for i, event in enumerate(events):
            path = f"/{collection}/{i}"
            keys = {"event_id", "target_assistant_id", "after_text", "text"}
            if collection == "side_speech":
                keys |= {"speaker", "voice_profile", "gain"}
                if ambient:
                    keys.add("source_id")
            if not isinstance(event, dict) or set(event) != keys:
                errors.append(path + ": event keys differ from schema")
                continue
            eid = event.get("event_id")
            if not isinstance(eid, str) or eid != f"{event_prefix}{i+1:02d}" or eid in event_ids:
                errors.append(path + "/event_id: expected unique consecutive event ID")
            if isinstance(eid, str):
                event_ids.add(eid)
            target = event.get("target_assistant_id")
            text = assistants.get(target) if isinstance(target, str) else None
            prefix = event.get("after_text")
            if text is None:
                errors.append(path + "/target_assistant_id: unknown assistant")
            elif not _string(prefix) or not text.startswith(prefix) or len(prefix) >= len(text):
                errors.append(path + "/after_text: expected exact nonfinal full assistant prefix")
            elif len(prefix) > stop_limits.get(target, len(text)):
                errors.append(path + "/after_text: event lies in an unplayed assistant tail")
            elif collection == "backchannels" and len(prefix) >= onset_limits.get(target, len(text)):
                errors.append(path + "/after_text: user backchannel must begin before their next main utterance")
            if not _string(event.get("text")):
                errors.append(path + "/text: expected nonempty string")
            if collection == "side_speech":
                if ambient and not _string(event.get("source_id")):
                    errors.append(path + "/source_id: expected frozen side-speech source")
                if not isinstance(event.get("speaker"), str) or event["speaker"] not in {"third_party", "machine"}:
                    errors.append(path + "/speaker: only third_party or machine is supported")
                if not _string(event.get("voice_profile")):
                    errors.append(path + "/voice_profile: expected pure voice-description string")
                gain = event.get("gain")
                if type(gain) not in (int, float) or not 0.3 <= gain <= 0.7:
                    errors.append(path + "/gain: expected number in [0.3,0.7]")
    if ambient:
        environment_events = script.get("environment_events")
        if not isinstance(environment_events, list):
            errors.append("/environment_events: expected array")
        else:
            hosts = {row.get(key) for row in rounds if isinstance(row, dict)
                     for key in ("user_id", "assistant_id") if isinstance(row.get(key), str)}
            sources = []
            for i, event in enumerate(environment_events):
                path = f"/environment_events/{i}"
                if not isinstance(event, dict) or set(event) != {
                        "event_id", "source_id", "target_audio_id", "placement_fraction"}:
                    errors.append(path + ": event keys differ from schema")
                    continue
                identity = event.get("source_id")
                if not _string(identity) or event.get("event_id") != identity or identity in event_ids:
                    errors.append(path + "/event_id: must equal a unique frozen source_id")
                elif isinstance(identity, str):
                    event_ids.add(identity)
                    sources.append(identity)
                if not isinstance(event.get("target_audio_id"), str) or event["target_audio_id"] not in hosts:
                    errors.append(path + "/target_audio_id: expected existing main user or assistant clip")
                fraction = event.get("placement_fraction")
                if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
                    errors.append(path + "/placement_fraction: expected finite number in [0,1]")
            if soundscape_plan is not None:
                expected = [event["source_id"] for event in soundscape_plan["environment_events"]]
                if sorted(sources) != sorted(expected):
                    errors.append("/environment_events: every frozen source must appear exactly once")
        if soundscape_plan is not None:
            speech = script.get("side_speech", [])
            if isinstance(speech, list):
                if len(speech) != soundscape_plan["side_speech_count"]:
                    errors.append("/side_speech: count differs from frozen soundscape plan")
                frozen = {item["source_id"]: item for item in soundscape_plan["side_speech_sources"]}
                used = set()
                for i, event in enumerate(speech):
                    if not isinstance(event, dict) or not isinstance(event.get("source_id"), str):
                        continue
                    source = frozen.get(event["source_id"])
                    if source is None:
                        errors.append(f"/side_speech/{i}/source_id: unknown frozen source")
                    else:
                        used.add(event["source_id"])
                        for key in ("speaker", "voice_profile"):
                            if event.get(key) != source[key]:
                                errors.append(f"/side_speech/{i}/{key}: differs from frozen source")
                if used != set(frozen):
                    errors.append("/side_speech: every frozen voice source must be used")
    return errors


def build_heard_context(script: dict, length_plan: dict | None = None) -> list[dict]:
    """Reconstruct distinct user-onset and final assistant-played histories."""
    errors = validate_script(script, length_plan)
    if errors:
        raise ValueError("cannot reconstruct an invalid script: " + "; ".join(errors))
    history: list[dict] = []
    contexts: list[dict] = []
    rows = script["rounds"]
    assistant_order = {row["assistant_id"]: i for i, row in enumerate(rows)}
    for i, row in enumerate(rows):
        user_history = copy.deepcopy(history)
        interruption = None
        if i:
            prior = rows[i-1]
            timing = row["timing"]
            onset = timing["user_onset_after_text"]
            stop = timing["assistant_stop_after_text"]
            user_history.append({"role": "assistant", "id": prior["assistant_id"], "text": onset})
            history.append({"role": "assistant", "id": prior["assistant_id"], "text": stop})
            interruption = {
                "target_assistant_id": prior["assistant_id"],
                "at_user_onset_heard_prefix": onset,
                "final_shared_played_prefix": stop,
                "discarded_unheard_tail": prior["assistant_utterance"][len(stop):],
            }
        history.append({"role": "user", "id": row["user_id"], "text": row["user_utterance"]})
        prior_backchannels = [
            {**event, "completion_status": "not_known_before_tts"}
            for event in script["backchannels"]
            if assistant_order[event["target_assistant_id"]] < i
        ]
        contexts.append({
            "user_id": row["user_id"], "assistant_id": row["assistant_id"],
            "user_at_onset_history": user_history,
            "assistant_before_reply_history": copy.deepcopy(history),
            "boundary": interruption,
            "prior_backchannel_onsets": prior_backchannels,
            "assistant_candidate": row["assistant_utterance"],
            "knowledge_rule": "User knows frozen situation; assistant knows only this heard history plus stable general knowledge. Side speech is not a user request.",
        })
    return contexts


def build_event_heard_context(script: dict, heard: list[dict] | None = None,
                             *, length_plan: dict | None = None) -> list[dict]:
    """Provide each backchannel with its own prefix-scoped causal evidence."""
    heard = build_heard_context(script, length_plan) if heard is None else heard
    by_assistant = {row["assistant_id"]: row for row in heard}
    events = []
    for collection in ("backchannels", "side_speech"):
        for event in script[collection]:
            target = event["target_assistant_id"]
            history = copy.deepcopy(by_assistant[target]["assistant_before_reply_history"])
            history.append({"role": "assistant", "id": target, "text": event["after_text"]})
            events.append({"event_id": event["event_id"], "kind": collection,
                           "target_assistant_id": target, "at_event_onset_history": history,
                           "at_event_onset_heard_prefix": event["after_text"],
                           "event_text": event["text"],
                           "expected_assistant_behavior": "Continue the same existing assistant audio without pausing or starting a new reply."})
    return events


def _judge_errors(value: Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != {"decision", "findings"}:
        return ["Judge requires exactly decision and findings"]
    decision, findings = value["decision"], value["findings"]
    if not isinstance(decision, str) or decision not in {"pass", "repair"} or not isinstance(findings, list):
        return ["invalid Judge decision/findings"]
    if (decision == "pass") != (len(findings) == 0):
        return ["pass requires no findings; repair requires findings"]
    errors = []
    for item in findings:
        if not isinstance(item, dict) or set(item) != {"severity", "path", "problem", "evidence", "repair"}:
            errors.append("each finding requires severity/path/problem/evidence/repair")
        elif item["severity"] != "must_fix" or any(not _string(item[k]) for k in ("path", "problem", "evidence", "repair")):
            errors.append("finding requires must_fix and nonempty strings")
        elif not item["path"].startswith("/"):
            errors.append("finding path must be a JSON pointer")
    return errors


def _complete(client, folder: Path, prompt_name: str, context: dict, max_tokens: int) -> tuple[str, dict]:
    max_tokens = effective_output_tokens(client, max_tokens)
    folder.mkdir(parents=True, exist_ok=True)
    source = context.get("frozen_source", {})
    prompt = render_prompt(prompt_name, source)
    (folder / "prompt.md").write_text(prompt)
    _write(folder / "input.json", context)
    raw, meta = client.complete(prompt, context, max_tokens=max_tokens, response_format={"type": "json_object"})
    (folder / "raw_response.txt").write_text(raw)
    secret = getattr(client, "api_key", "")
    clean = json.loads(json.dumps(meta, ensure_ascii=False).replace(secret, "[REDACTED]")) if secret else meta
    _write(folder / "metadata.json", {**clean, **_call_controls(source, max_tokens)})
    return raw, meta


class _OwnerJsonExhausted(ValueError):
    pass


def _complete_json_owner(client, folder, prompt_name, context, max_tokens, *, patch):
    """V2.4: three parse retries, independently of semantic owner repairs."""
    from duplex_v2.patch import POLICY, parse_response
    attempts = []
    previous_raw = error = None
    report = folder.parent / 'parse-report.json'
    for retry in range(POLICY['json_parse_max_retries'] + 1):
        actual = copy.deepcopy(context)
        name = prompt_name
        if retry:
            actual['json_parse_retry'] = {'retry': retry, 'max_retries': 3,
                'error': error, 'previous_unparseable_response': previous_raw}
            if not patch:
                name = 'repair.md'
                actual.update(previous_candidate={'unparsed_response': previous_raw}, findings=[{
                    'severity': 'must_fix', 'path': '/', 'problem': 'Invalid complete script JSON',
                    'evidence': error, 'repair': 'Return one complete legal script JSON, retaining the original task and facts.'}])
        target = folder if retry == 0 else folder.parent / 'json-retries' / f'retry-{retry:02d}'
        raw, _ = _complete(client, target, name, actual, max_tokens)
        try:
            value = parse_response(raw)
        except ValueError as exc:
            error, previous_raw = str(exc), raw
            attempts.append({'retry': retry, 'directory': str(target), 'status': 'unparseable', 'error': error})
            _write(report, {'max_retries': 3, 'retries_used': retry,
                'response_kind': 'patch' if patch else 'complete_script', 'attempts': attempts})
            continue
        attempts.append({'retry': retry, 'directory': str(target), 'status': 'parsed'})
        _write(report, {'max_retries': 3, 'retries_used': retry,
            'response_kind': 'patch' if patch else 'complete_script', 'attempts': attempts})
        return value
    raise _OwnerJsonExhausted('owner JSON response remained unparseable after three independent retries')


def _evaluate(script: Any, source: dict, directory: Path, judge_client, *, content_feedback=None,
              approved_semantic_waivers=None) -> dict:
    plan, _ = _source_controls(source)
    budgets = script_token_budgets(source)
    soundscape = _soundscape_source(source)
    errors = validate_script(script, plan, soundscape)
    from duplex_v2.anchor_dependencies import enabled as execution_enabled, program_anchor_errors
    if execution_enabled(source):
        errors += program_anchor_errors(script, source)
    if isinstance(script, dict) and soundscape is None and script.get("schema_version") != SCHEMA_VERSION:
        errors.append("/schema_version: source without soundscape must retain fixed_script.v1")
    _write(directory / "validation.json", {"errors": errors})
    if errors:
        return {"decision": "repair", "findings": [{"severity": "must_fix", "path": "/",
                "problem": "Deterministic schema/anchor validation failed", "evidence": "\n".join(errors),
                "repair": "Correct only the listed structural or exact-prefix errors, preserving the frozen facts."}]}
    heard = build_heard_context(script, plan)
    _write(directory / "heard_context.json", heard)
    event_heard = build_event_heard_context(script, heard, length_plan=plan)
    _write(directory / "event_heard_context.json", event_heard)
    context = {"frozen_source": source, "script": script, "reconstructed_heard_context": heard,
               "reconstructed_event_heard_context": event_heard,
               "length_rule": ("Ordinary replies aim at <=60 words; the hard 120-word limit has an independent three-attempt compaction budget." if source.get("language") == "en" else "Ordinary replies aim at <=120 characters. Only semantic redundancy is a content error; >250 character compaction has its own separate three-attempt budget.")}
    if content_feedback is not None:
        from duplex_v2.script_feedback import enabled as feedback_enabled
        if not feedback_enabled(source):
            raise ValueError('content feedback requires the explicit frozen policy')
        context['content_feedback'] = copy.deepcopy(content_feedback)
    if approved_semantic_waivers:
        context['approved_semantic_waivers'] = copy.deepcopy(approved_semantic_waivers)
        context['semantic_waiver_scope'] = (
            'These are retained original third-compaction semantic exceptions authorized before this audio-boundary review. '
            'Do not revoke or relitigate those existing exceptions merely because a boundary is being checked. '
            'Independently reject any new factual, visibility, causality or execution problem introduced by the changed boundary; '
            'the old exceptions do not authorize additional omissions or leakage. Program/schema/length/anchor validity cannot be waived.')
    folder = directory / "judge"
    folder.mkdir(parents=True, exist_ok=True)
    prompt = render_prompt("judge.md", source)
    (folder / "prompt.md").write_text(prompt)
    _write(folder / "input.json", context)
    judge_tokens = effective_output_tokens(judge_client, budgets['judge'])
    from duplex_v2.patch import enabled
    format_options = {'max_format_retries': 3} if enabled(source) else {}
    raw, meta = complete_checked_judge(judge_client, prompt, context, keys={"decision", "findings"},
                                      validator=_judge_errors, directory=folder / "format", max_tokens=judge_tokens,
                                      **format_options)
    (folder / "raw_response.txt").write_text(raw)
    _write(folder / "metadata.json", {**meta, **_call_controls(source, judge_tokens)})
    result = parse_json_text(raw, {"decision", "findings"})
    _write(folder / "result.json", result)
    return result


def _apply_compaction(script: dict, assistant_id: str, value: Any) -> dict:
    """Allow one assistant text and its directly referencing anchors to change."""
    from duplex_v2.compaction_contract import validate_shape
    validate_shape(value)
    candidate = copy.deepcopy(script)
    index = next(i for i, row in enumerate(candidate["rounds"]) if row["assistant_id"] == assistant_id)
    if not _string(value["assistant_utterance"]):
        raise ValueError("compacted assistant text must be nonempty")
    candidate["rounds"][index]["assistant_utterance"] = value["assistant_utterance"]
    if index + 1 < len(candidate["rounds"]):
        candidate["rounds"][index+1]["timing"] = value["next_user_timing"]
    elif value["next_user_timing"] is not None:
        raise ValueError("last assistant has no next user timing")
    for collection in ("backchannels", "side_speech"):
        updates = value[collection]
        expected = {e["event_id"] for e in script[collection] if e["target_assistant_id"] == assistant_id}
        if not isinstance(updates, list) or any(not isinstance(e, dict) or set(e) != {"event_id", "after_text"} for e in updates):
            raise ValueError("anchor updates must contain only event_id and after_text")
        if len(updates) != len(expected) or {e["event_id"] for e in updates} != expected:
            raise ValueError("compaction must retain all directly referencing events exactly once")
        lookup = {e["event_id"]: e["after_text"] for e in updates}
        for event in candidate[collection]:
            if event["event_id"] in lookup:
                event["after_text"] = lookup[event["event_id"]]
    return candidate


def _within_anchor_scope(candidate, baseline, paths):
    """Reject forbidden model edits instead of silently restoring their values."""
    checked = copy.deepcopy(candidate)
    try:
        for path in paths:
            parts = path.strip('/').split('/')
            if len(parts) != 4 or parts[0] != 'rounds' or parts[2:] != ['timing', 'assistant_stop_after_text']:
                return False
            index = int(parts[1])
            checked['rounds'][index]['timing']['assistant_stop_after_text'] = baseline['rounds'][index]['timing']['assistant_stop_after_text']
    except (ValueError, KeyError, TypeError, IndexError):
        return False
    return checked == baseline


def generate_script(source: dict, out_dir: str | Path, client, *, max_repairs: int = 5,
                    continuation: dict | None = None) -> dict:
    """Author all rounds once, review/refine, then compact overlong replies."""
    from duplex_v2.language import response_count, response_limit, language
    maximum = response_limit(source)
    count_key = 'words' if language(source) == 'en' else 'characters'
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if not isinstance(source, dict) or not {"scene_task", "user_voice", "situation_knowledge"} <= set(source):
        raise ValueError("source requires scene_task, user_voice, situation_knowledge")
    if sol_reasoning_effort(client.model) != "xhigh":
        raise ValueError("V2 requires the explicitly authorized GPT-5.6-sol model")
    text_policy = source.get('text_policy')
    if text_policy is not None:
        from duplex_v2.text_policy import role_client, validate_policy
        text_policy = validate_policy(text_policy)
        client = role_client(client, 'script_generation', text_policy)
    if type(max_repairs) is not int or max_repairs not in {2, 3, 5}:
        raise ValueError("V2 content owner-repair budget must be legacy two/three or authorized five")
    from duplex_v2.patch import enabled, apply_patch, script_hash
    incremental = enabled(source)
    from duplex_v2.script_feedback import (
        enabled as feedback_enabled, context as feedback_context, actual_changes,
    )
    feedback_active = feedback_enabled(source)
    review_history = []
    from duplex_v2.anchor_dependencies import (
        enabled as execution_enabled, derive_program_anchors, relevant_dependencies, changed_dependencies,
    )
    execution = execution_enabled(source)
    if execution and not incremental:
        raise ValueError('V2.6 requires the frozen atomic patch policy')
    if incremental and max_repairs != 5:
        raise ValueError('V2.4 freezes five semantic repairs, independent of three JSON retries')
    plan, _ = _source_controls(source)
    soundscape = _soundscape_source(source)
    budgets = script_token_budgets(source)
    summary_controls = _call_controls(source, effective_output_tokens(client, budgets["generation"]))
    if incremental:
        summary_controls.update(script_repair_policy=source['script_repair_policy'],
                                json_parse_max_retries=3, repair_output='changed_fields_only')
    if execution:
        summary_controls.update(script_execution_policy=source['script_execution_policy'],
                                program_anchor_seed=source['program_anchor_seed'])
    from duplex_v2.compaction_contract import enabled as contract_enabled, OUTPUT_FIELDS, normalize_response
    compact_contract = contract_enabled(source)
    if compact_contract:
        summary_controls['compaction_output_policy'] = source['compaction_output_policy']
    if feedback_active:
        summary_controls['script_feedback_policy'] = source['script_feedback_policy']
    source_path = directory / "source.json"
    if source_path.exists() and json.loads(source_path.read_text()) != source:
        raise ValueError("frozen source changed on resume; use a new branch directory")
    _write(source_path, source)
    owner = RecordedCalls(client, directory / "calls")
    judge = owner.for_model(client.model, temperature=0.0)
    if text_policy is not None:
        judge = role_client(owner, 'judge', text_policy, temperature=0.0)
    prior: Any = None
    feedback: Any = None
    accepted = None
    attempts = []
    first_attempt = 0
    previous_patch_failure = None
    if continuation is not None:
        used = continuation.get('content_repairs_used')
        if type(used) is not int or not 0 <= used < max_repairs:
            raise ValueError('anchor continuation requires remaining original content repairs')
        inherited = continuation.get('attempts', [])
        if len(inherited) != used+1 or (not continuation.get('allowed_paths') and
                continuation.get('scope') != 'original_script'):
            raise ValueError('anchor continuation has inconsistent prior budget/provenance')
        prior = copy.deepcopy(continuation['candidate'])
        feedback = copy.deepcopy(continuation['findings'])
        attempts = [{**copy.deepcopy(item), 'inherited': True} for item in inherited]
        first_attempt = used+1
        frozen_continuation = directory/'continuation.json'
        if frozen_continuation.exists() and json.loads(frozen_continuation.read_text()) != continuation:
            raise ValueError('anchor continuation changed on resume')
        _write(frozen_continuation, continuation)
    for attempt in range(first_attempt, max_repairs + 1):
        folder = directory / "attempts" / f"attempt-{attempt+1:02d}"
        before_candidate = copy.deepcopy(prior) if feedback_active else None
        context = {"frozen_source": source,
                   "schema_version": SOUNDSCAPE_SCHEMA_VERSION if soundscape is not None else SCHEMA_VERSION,
                   "owner_attempt": attempt+1}
        if attempt:
            context.update({"previous_candidate": prior, "findings": feedback})
            if feedback_active:
                context['content_feedback'] = feedback_context(source, review_history, findings=feedback or [])
            if execution:
                context['anchor_dependencies'] = relevant_dependencies(prior, feedback)
        if continuation is not None and continuation.get('allowed_paths'):
            context.update(allowed_repair_paths=continuation['allowed_paths'],
                repair_scope_instruction='Only modify the listed stop-anchor fields. Preserve every other script field and all utterances exactly.')
        parsed = True
        if incremental:
            patch_request = attempt > 0
            if patch_request:
                context['base_sha256'] = script_hash(prior)
                if previous_patch_failure is not None:
                    context['previous_patch_failure'] = copy.deepcopy(previous_patch_failure)
            try:
                value = _complete_json_owner(owner, folder / ('patch' if patch_request else 'generate'),
                    'patch.md' if patch_request else 'script.md', context, budgets['generation'], patch=patch_request)
            except _OwnerJsonExhausted as exc:
                _write(directory / 'summary.json', {'status': 'failed', 'reason': 'owner_json_parse_retries_exhausted',
                    'content_repair_budget': max_repairs, 'content_max_attempts': max_repairs+1,
                    'content_repairs_used': max(0, len(attempts)-1), 'attempts': attempts,
                    'parse_report': str(folder / 'parse-report.json'), **summary_controls})
                raise ScriptGenerationError(str(exc), directory / 'summary.json') from exc
            if patch_request:
                _write(folder / 'patch.json', value)
                try:
                    candidate, receipt = apply_patch(prior, value)
                    if execution:
                        proposed_hash = script_hash(candidate)
                        candidate, derivations, errors = derive_program_anchors(candidate, source)
                        errors += validate_script(candidate, plan, soundscape)
                        if soundscape is None and candidate.get('schema_version') != SCHEMA_VERSION:
                            errors.append('/schema_version: source without soundscape must retain fixed_script.v1')
                        dependencies = changed_dependencies(prior, candidate)
                        _write(folder / 'dependency-closure.json', {
                            'policy': source['script_execution_policy'], 'model_candidate_sha256': proposed_hash,
                            'derived_candidate_sha256': script_hash(candidate), 'program_derivations': derivations,
                            'changed_text_dependencies': dependencies, 'errors': errors})
                        if errors:
                            _write(folder / 'rejected-candidate.json', candidate)
                            raise ValueError('Atomic dependency validation failed:\n'+'\n'.join(errors))
                        receipt.update(model_candidate_sha256=proposed_hash, candidate_sha256=script_hash(candidate),
                                       program_derivations=derivations, full_structure_validated=True)
                except ValueError as exc:
                    parsed = False
                    unresolved = copy.deepcopy((previous_patch_failure or {}).get('unresolved_findings', feedback))
                    previous_patch_failure = {'rejected_patch': value, 'error': str(exc),
                                              'unresolved_findings': unresolved}
                    result = {'decision': 'repair', 'findings': [*(unresolved or []), {'severity': 'must_fix', 'path': '/',
                        'problem': 'Invalid atomic script patch', 'evidence': str(exc),
                        'repair': 'Return corrected patch operations against the unchanged base_sha256; never replace the complete script.'}]}
                    _write(folder / 'validation.json', {'errors': [str(exc)], 'base_preserved': True})
                else:
                    previous_patch_failure = None
                    prior = candidate
                    _write(folder / 'patch-applied.json', receipt)
            else:
                prior = value
                if execution:
                    prior, derivations, errors = derive_program_anchors(prior, source)
                    _write(folder / 'program-anchors.json', {'derivations': derivations, 'errors': errors})
        else:
            raw, _ = _complete(owner, folder / ("generate" if attempt == 0 else "repair"),
                               "script.md" if attempt == 0 else "repair.md", context, budgets["generation"])
            try:
                prior = parse_json_text(raw, ROOT_KEYS | ({"environment_events"} if soundscape is not None else set()))
            except ValueError as exc:
                parsed = False
                prior = {"unparsed_response": raw}
                result = {"decision": "repair", "findings": [{"severity": "must_fix", "path": "/",
                          "problem": "Invalid JSON", "evidence": str(exc), "repair": "Return the complete script as legal JSON matching the original schema."}]}
                _write(folder / "validation.json", {"errors": [str(exc)]})
        _write(folder / "candidate.json", prior)
        if parsed and continuation is not None and continuation.get('allowed_paths') and not _within_anchor_scope(
                prior, continuation['candidate'], continuation['allowed_paths']):
            result = {'decision': 'repair', 'findings': [{'severity': 'must_fix', 'path': '/',
                'problem': 'Model changed fields outside the approved anchor scope',
                'evidence': 'Only '+', '.join(continuation['allowed_paths'])+' may change.',
                'repair': 'Keep all utterances, facts and other fields exact; repair only the approved stop anchor.'}]}
            prior = copy.deepcopy(continuation['candidate'])
        elif parsed:
            if feedback_active:
                changes = actual_changes(before_candidate, prior) if before_candidate is not None else []
                result = _evaluate(prior, source, folder, judge,
                    content_feedback=feedback_context(source, review_history, changes=changes))
            else:
                result = _evaluate(prior, source, folder, judge)
        _write(folder / "decision.json", result)
        if feedback_active:
            judge_input, judge_result = folder/'judge/input.json', folder/'judge/result.json'
            independently_reviewed = (judge_input.is_file() and judge_result.is_file() and
                json.loads(judge_input.read_text()).get('script') == prior and
                json.loads(judge_input.read_text()).get('frozen_source') == source and
                json.loads(judge_result.read_text()) == result)
            entry = {'owner_attempt': attempt+1, 'candidate_sha256': script_hash(prior),
                     'origin': 'independent_judge' if independently_reviewed else 'program_validation',
                     'decision': result['decision'], 'findings': copy.deepcopy(result['findings']),
                     'applied_changes': actual_changes(before_candidate, prior) if before_candidate is not None else []}
            review_history.append(entry)
            _write(folder/'content-feedback.json', {'review_entry': entry,
                'cumulative_feedback': feedback_context(source, review_history, findings=result['findings'])})
        attempts.append({"attempt": attempt+1, "decision": result["decision"], "directory": str(folder)})
        if result["decision"] == "pass":
            accepted = prior
            break
        feedback = result["findings"]
    if accepted is None:
        _write(directory / "summary.json", {"status": "failed", "reason": "content_repair_budget_exhausted",
                                           "content_repair_budget": max_repairs, "content_max_attempts": max_repairs+1,
                                           "content_repairs_used": max(0, len(attempts)-1), "attempts": attempts,
                                           **summary_controls})
        raise ScriptGenerationError(f"V2 script failed after initial generation and {max_repairs} model repairs", directory / "summary.json")
    _write(directory / "content_approved_script.json", accepted)
    incremental_compaction = (language(source) == 'en' and source.get('length_compaction_policy') ==
                              'incremental_previous_candidate_target_100_105_v1')
    length_results = []
    semantic_waivers = []
    for original_row in list(accepted["rounds"]):
        assistant_id = original_row["assistant_id"]
        initial_count = response_count(original_row["assistant_utterance"], source)
        if initial_count <= maximum:
            continue
        if continuation is not None and continuation.get('allowed_paths'):
            length_results.append({'assistant_id': assistant_id, 'initial_characters': initial_count,
                'status': 'retained_previously_approved_overlength', 'attempts': []})
            continue
        feedback = None
        success = False
        waived = False
        length_attempts = []
        previous_compaction = None
        previous_compaction_review = None
        for attempt in range(3):
            folder = directory / "length" / assistant_id / f"attempt-{attempt+1:02d}"
            context = {"frozen_source": source, "content_approved_script": accepted,
                       "reconstructed_heard_context": build_heard_context(accepted, plan),
                       "target_assistant_id": assistant_id, ('maximum_words' if language(source) == 'en' else 'maximum_characters'): maximum,
                       "compaction_attempt": attempt+1,
                       "previous_compaction_feedback": feedback}
            if compact_contract:
                context['compaction_output_contract'] = {
                    'policy': source['compaction_output_policy'], 'required_fields': list(OUTPUT_FIELDS),
                    'program_preserved_script_fields': ['environment_events'] if 'environment_events' in accepted else []}
            if incremental_compaction:
                context.update(length_compaction_policy=source['length_compaction_policy'],
                    target_word_range=[100, 105],
                    previous_compaction_candidate=(copy.deepcopy(previous_compaction['candidate'])
                                                   if previous_compaction else None),
                    previous_compaction_candidate_attempt=(previous_compaction['attempt']
                                                           if previous_compaction else None),
                    previous_compaction_words=(previous_compaction['words'] if previous_compaction else None),
                    previous_compaction_candidate_status=(previous_compaction['status']
                                                          if previous_compaction else None),
                    previous_compaction_review_feedback=copy.deepcopy(previous_compaction_review),
                    previous_compaction_feedback_attempt=attempt if attempt else None)
            if execution:
                context['anchor_dependencies'] = [item for item in relevant_dependencies(accepted, [])
                                                  if item['target_id'] == assistant_id]
                context['prior_semantic_waivers'] = copy.deepcopy(semantic_waivers)
            compact_owner = owner
            if text_policy is not None:
                compact_owner = role_client(owner, 'compaction_first' if attempt == 0 else 'compaction_later', text_policy)
            raw, _ = _complete(compact_owner, folder / "compact", "compact.md", context, budgets["compact"])
            try:
                compacted = parse_json_text(raw, set(OUTPUT_FIELDS))
                if (incremental_compaction and isinstance(compacted, dict) and
                        isinstance(compacted.get('assistant_utterance'), str) and compacted['assistant_utterance'].strip()):
                    # Retain the author's actual parseable draft even when its
                    # anchor payload is invalid; it has no approval status.
                    previous_compaction = {'attempt': attempt+1, 'candidate': copy.deepcopy(compacted),
                        'words': response_count(compacted['assistant_utterance'], source),
                        'status': 'parsed_not_validated', 'accepted': False}
                if compact_contract:
                    compacted, normalization = normalize_response(compacted, accepted, source)
                    if normalization is not None:
                        import hashlib
                        raw_path = folder / 'compact' / 'raw_response.txt'
                        normalization['raw_response'] = {'path': str(raw_path),
                            'sha256': hashlib.sha256(raw_path.read_bytes()).hexdigest()}
                        _write(folder / 'compaction-output-normalization.json', normalization)
                candidate = _apply_compaction(accepted, assistant_id, compacted)
                if execution:
                    candidate, derivations, errors = derive_program_anchors(candidate, source)
                    _write(folder / 'program-anchors.json', {'derivations': derivations, 'errors': errors})
            except (ValueError, TypeError, KeyError) as exc:
                feedback = {"format_error": str(exc)}
                if incremental_compaction and previous_compaction and previous_compaction['attempt'] == attempt+1:
                    previous_compaction.update(status='invalid_compaction', format_error=str(exc))
                    feedback.update(words=previous_compaction['words'],
                                    excess_words=max(0, previous_compaction['words']-maximum))
                    _write(folder / 'retained-compaction.json', previous_compaction)
                _write(folder / "validation.json", {"errors": [str(exc)]})
                length_attempts.append({"attempt": attempt+1, "decision": "repair", "reason": "invalid_compaction"})
                if incremental_compaction and previous_compaction and previous_compaction['attempt'] == attempt+1:
                    length_attempts[-1]['words'] = previous_compaction['words']
                continue
            _write(folder / "candidate.json", candidate)
            result = _evaluate(candidate, source, folder, judge)
            count = response_count(compacted["assistant_utterance"], source)
            length_attempts.append({"attempt": attempt+1, "content_decision": result["decision"], count_key: count})
            _write(folder / "decision.json", result)
            if incremental_compaction:
                previous_compaction_review = {'attempt': attempt+1, 'decision': result['decision'],
                    'findings': copy.deepcopy(result['findings']),
                    'structural_errors': json.loads((folder / 'validation.json').read_text())['errors']}
                previous_compaction.update(status='evaluated_not_accepted', judge_decision=result['decision'],
                    findings=copy.deepcopy(result['findings']),
                    structural_errors=previous_compaction_review['structural_errors'])
                _write(folder / 'retained-compaction.json', previous_compaction)
            structurally_valid = execution and not json.loads((folder / 'validation.json').read_text())['errors']
            waived = bool(execution and attempt == 2 and count <= maximum
                          and structurally_valid and result['decision'] != 'pass')
            if waived:
                waiver = {'assistant_id': assistant_id, 'attempt': attempt+1, count_key: count,
                          'judge_decision': result['decision'], 'findings': result['findings'],
                          'reason': 'third_compaction_length_and_structure_accepted_without_semantic_pass'}
                semantic_waivers.append(waiver)
                length_attempts[-1]['accepted_with_semantic_waiver'] = True
                _write(folder / 'acceptance.json', {'accepted': True, **waiver})
            if (result["decision"] == "pass" or waived) and count <= maximum:
                if incremental_compaction:
                    previous_compaction.update(accepted=True, status='accepted_with_semantic_waiver' if waived else 'accepted')
                    _write(folder / 'retained-compaction.json', previous_compaction)
                accepted = candidate
                success = True
                break
            feedback = {"findings": result["findings"], count_key: count,
                        "length_error": None if count <= maximum else f"Still exceeds {maximum} {count_key}; preserve required semantics while compacting."}
            if incremental_compaction:
                feedback['excess_words'] = max(0, count-maximum)
        length_results.append({"assistant_id": assistant_id, ('initial_words' if count_key == 'words' else 'initial_characters'): initial_count,
                               "status": "compacted_with_semantic_waiver" if waived and success else "compacted" if success else "failed_overlength" if incremental else "retained_content_approved_overlength",
                               "attempts": length_attempts})
    warnings = [{"assistant_id": row["assistant_id"], count_key: response_count(row["assistant_utterance"], source),
                 "limit": maximum, "retained_content_approved": True}
                for row in accepted["rounds"] if response_count(row["assistant_utterance"], source) > maximum]
    _write(directory / "length_report.json", {"limit": maximum, "ordinary_reply_soft_target": 60 if count_key == 'words' else 120, "counting_unit": count_key,
                                               "results": length_results, "warnings": warnings,
                                               **({'length_compaction_policy': source['length_compaction_policy'],
                                                   'target_word_range': [100, 105]} if incremental_compaction else {}),
                                               **({'semantic_waivers': semantic_waivers,
                                                   'third_attempt_acceptance': 'length_and_structure_only'} if execution else {}),
                                               **({'compaction_max_attempts': 3, 'exhausted_action': 'fail_case',
                                                   'status': 'failed' if warnings else 'passed'} if incremental else {})})
    if incremental and warnings:
        rejected = directory / 'length_rejected_script.json'
        _write(rejected, accepted)
        scoped = continuation is not None and continuation.get('allowed_paths')
        _write(directory / 'summary.json', {'status': 'failed',
            'reason': 'length_constraint_unresolved_in_anchor_scope' if scoped else 'length_compaction_budget_exhausted',
            'content_repair_budget': max_repairs, 'content_max_attempts': max_repairs+1,
            'content_repairs_used': max(0, len(attempts)-1), 'attempts': attempts,
            'length_compaction_budget': 3, 'length_report': str(directory / 'length_report.json'),
            'length_warnings': warnings, 'rejected_script_path': str(rejected), **summary_controls})
        raise ScriptGenerationError(f'Assistant replies still exceed {maximum} {count_key}; case is ineligible for successful data', directory / 'summary.json')
    _write(directory / "script.json", accepted)
    _write(directory / "heard_context.json", build_heard_context(accepted, plan))
    _write(directory / "event_heard_context.json", build_event_heard_context(accepted, length_plan=plan))
    _write(directory / "summary.json", {"status": "passed_with_length_warnings" if warnings else "passed",
                                        "content_repair_budget": max_repairs, "content_max_attempts": max_repairs+1,
                                        "content_repairs_used": max(0, len(attempts)-1),
                                        **summary_controls,
                                        "attempts": attempts, "rounds": len(accepted["rounds"]),
                                        "backchannels": len(accepted["backchannels"]), "side_speech": len(accepted["side_speech"]),
                                        **({"environment_events": len(accepted["environment_events"])} if soundscape is not None else {}),
                                        "interruptions": sum("T03" in row["timing_refs"] for row in accepted["rounds"]),
                                        "length_warnings": warnings, "script_path": str(directory / "script.json")})
    if execution:
        summary = json.loads((directory / 'summary.json').read_text())
        summary.update(semantic_waivers=semantic_waivers,
                       semantic_review_status='waived_after_third_compaction' if semantic_waivers else 'passed')
        _write(directory / 'summary.json', summary)
    return accepted
