"""Interactive workflow introduction for the existing complete-case HTML bundle."""
from __future__ import annotations

import html
import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "report_assets"
PHASES = [
    ("offline", "01", "离线环境", "Stage 1–5：任务、情境、剧本、评分标准与 Audit"),
    ("prepare", "02", "准备与批量", "冻结环境 → 独立 episode → 持久调用与恢复"),
    ("interaction", "03", "反复交互", "从 U01 开始：用户行动、合成语音、助手收听与回应"),
    ("scoring", "04", "事后评分", "原标准 + 实际已播内容 + PCM 测量"),
    ("dataset", "05", "导出与核验", "观测、动作、奖励、音频前缀；训练器待接入"),
]


def workflow_css():
    return (ASSETS / "workflow_explorer.css").read_text()


def workflow_javascript():
    return "<script>\n" + (ASSETS / "workflow_explorer.js").read_text() + "\n</script>"


def json_payload(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def validate_catalog(data):
    if data.get("schema") != "duplex-workflow-explorer.v1" or not data.get("nodes"):
        raise ValueError("A real source-backed workflow catalog is required")
    ids = [node["id"] for node in data["nodes"]]
    cases = data.get('cases', [])
    case_ids = {case['id'] for case in cases}
    if len(case_ids) != len(cases):
        raise ValueError('Duplicate workflow case')
    if cases and data.get('default_case', cases[0]['id']) not in case_ids:
        raise ValueError('Unknown default workflow case')
    if cases and any(v.get('dataset_case') not in case_ids for v in data.get('dialogue_versions', [])):
        raise ValueError('Dialogue version must belong to a known case')
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate workflow node")
    for node in data["nodes"]:
        if set(node["upstream"] + node["downstream"]) - set(ids):
            raise ValueError("Unknown workflow edge")
        for example in node["examples"]:
            if cases and example.get('dataset_case') not in case_ids:
                raise ValueError('Workflow example must belong to a known case')
            if example.get("prompt") and example["prompt"]["origin"] != "recorded":
                raise ValueError("Recorded Prompt must retain its origin")
    successful_only = data.get('successful_cases_only', False)
    if successful_only:
        if not cases or any(c.get('status') != 'completed' for c in cases):
            raise ValueError('Successful-only view requires completed cases')
        if len(data.get('dialogue_versions', [])) != len(cases) or {
            v.get('dataset_case') for v in data.get('dialogue_versions', [])
        } != case_ids:
            raise ValueError('Successful-only view requires one dialogue per case')
        if data.get('failure_reviews'):
            raise ValueError('Successful-only view cannot display failed cases')
    elif not data.get("traces") or not data.get("failure_reviews"):
        raise ValueError("Actual turn traces and preserved failures are required")


def render_workflow_explorer(data):
    validate_catalog(data)
    h = lambda value: html.escape(str(value), quote=True)
    phase_cards = "".join(f'<button class="wf-phase" data-phase="{key}"><span>{number}</span><strong>{title}</strong><small>{description}</small><b aria-hidden="true">↗</b></button>' for key, number, title, description in PHASES)
    filters = ''.join(f'<button data-filter="{key}" aria-pressed="false">{title}</button>' for key, _, title, _ in PHASES)
    nodes = "".join(f'<button class="wf-step" data-node="{h(n["id"])}" data-phase-name="{h(n["phase"])}"><small>{h({"llm":"模型调用","tts":"语音合成","program":"程序步骤"}[n["kind"]])}</small><strong>{h(n["title"])}</strong></button>' for n in data["nodes"])
    versions = data.get("dialogue_versions", [])
    cases = data.get('cases', [])
    default_case = data.get('default_case', cases[0]['id'] if cases else None)
    expandable_list = data.get('case_navigation') == 'expandable_list'
    case_picker = ''
    if cases and not expandable_list:
        options = ''.join(f'<option value="{h(c["id"])}"'+(' selected' if c['id'] == default_case else '')+f'>{h(c["label"])}</option>' for c in cases)
        case_picker = f'<label class="wf-search">选择数据案例<select id="wf-case">{options}</select></label>'
    dialogue = ''
    if versions:
        selected = next((v for v in versions if v.get('dataset_case') == default_case), versions[0])
        if expandable_list:
            by_case = {v['dataset_case']: v for v in versions}
            choices = []
            for index, case in enumerate(cases, 1):
                version = by_case[case['id']]
                active = str(case['id'] == default_case).lower()
                choices.append(
                    f'<li><button type="button" class="wf-case-choice" data-case-choice="{h(case["id"])}" '
                    f'data-dialogue-version="{h(version["id"])}" data-dialogue-src="{h(version["path"])}" '
                    f'data-dialogue-case="{h(case["id"])}" aria-pressed="{active}" aria-current="{active}">'
                    f'<span class="wf-case-number">{index:02d}</span><span class="wf-case-copy">'
                    f'<strong>{h(case["label"])}</strong><small>{h(case.get("summary", "完整会话"))}</small></span>'
                    '<span class="wf-case-selected">当前</span></button></li>'
                )
            current_label = next(c['label'] for c in cases if c['id'] == default_case)
            picker = (
                f'<details class="wf-case-list" id="wf-case-list" open><summary>'
                f'<span>完整案例 · {len(cases)} 个</span><strong id="wf-current-case">{h(current_label)}</strong>'
                '<small>展开 / 收起列表</small></summary>'
                f'<ol class="wf-case-options">{"".join(choices)}</ol></details>'
                '<p class="wf-case-hint">选择案例，同步切换完整语音与下方流程记录。</p>'
            )
        else:
            buttons = ''.join(f'<button type="button" class="wf-link-button" data-dialogue-version="{h(v["id"])}" data-dialogue-src="{h(v["path"])}" data-dialogue-case="{h(v.get("dataset_case", ""))}" aria-pressed="{str(v == selected).lower()}">{h(v["label"])}</button>' for v in versions)
            picker = f'<h3>完整语音 · 用户附和与原版本对照</h3><div class="wf-actions">{buttons}</div>'
        dialogue = f'<section class="wf-dialogue-versions">{picker}<p id="wf-dialogue-version-status">{h(selected["description"])}</p><iframe id="wf-dialogue-frame" title="完整双侧语音与逐段时间轴" src="{h(selected["path"])}" loading="lazy"></iframe></section>'
    compact = ' wf-compact' if data.get('compact_view') else ''
    canonical_link = '' if compact else '<a href="#results-complete-case">完整冻结案例 ↓</a>'
    return f'''<section class="wf{compact}" data-workflow-explorer>
<div class="wf-hero"><p class="wf-eyebrow">DUPLEX ENVIRONMENT → INTERACTION → DATA</p><h2>从环境到训练数据，每一步都展开看</h2><p class="wf-lead">先造好一份用户任务环境，再让用户与助手独立交互；最后按原标准评分，导出实际发生的观测、动作和音频。点击步骤，查看它的职责、完整输入、实际 Prompt 和输出。</p>
<div class="wf-metrics"><div><strong>1</strong><span>复用的冻结环境</span></div><div><strong>3 → 1</strong><span>新交互尝试 → 完整轨迹</span></div><div><strong>53</strong><span>新完整轨迹的策略样本</span></div><div><strong>{len(data['nodes'])}</strong><span>可展开的流程与分支</span></div></div>
<div class="wf-banner"><b>这次从哪里开始？</b><p>Stage 1–5 已在前一轮完成。本批接入同一份 Stage 5 环境，创建 E001–E003，分别从 U01 开始真实模型交互。此前的完整轨迹另导出 50 条样本，合计 <strong>2 条完整轨迹 / 103 条样本</strong>。</p></div>
<div class="wf-actions"><a href="workflow-records.zip" download>下载全部流程资料 ↓</a><a href="workflow-data.json" target="_blank" rel="noopener">流程数据 JSON ↗</a><a href="{h(data['results_url'])}" target="_blank" rel="noopener">现有结果与音频报告 ↗</a>{canonical_link}</div></div>
<nav class="wf-modebar" aria-label="选择阅读视角"><button data-mode="flow" aria-pressed="true">完整流程</button><button data-mode="taxonomy" aria-pressed="false">场景与方向库</button><button data-mode="visibility" aria-pressed="false">谁能看到什么</button><button data-mode="traces" aria-pressed="false">U01–U13 真实实录</button><button data-mode="failures" aria-pressed="false">失败与当前终点</button></nav>
<div data-panel="flow"><div class="wf-phase-grid">{phase_cards}</div>
<div class="wf-note"><strong>两种循环：</strong>离线生成遇到语义问题，进入受限修复后重审；在线交互则在已听前缀上反复生成和播放。评分在交互结束后发生，本批评分结果没有回流给当前助手。</div>
<details class="wf-diagram" open><summary>展开交互循环 · 触发观察器与助手收听控制器是两个角色</summary>
<div class="wf-diagram-scroll"><svg viewBox="0 0 980 335" role="img" aria-label="交互循环：调度、触发观察、用户生成检查与TTS，PCM消费后助手收听回应和TTS，实际已播前缀反馈调度">
<defs><marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#65818d"/></marker></defs>
<text x="25" y="24" class="wf-svg-label">环境与用户侧</text><text x="25" y="178" class="wf-svg-label">实际消费与助手侧</text>
<g fill="none" stroke="#65818d" stroke-width="1.6" marker-end="url(#wf-arrow)"><path d="M175 70H205M365 70H395M555 70H585M745 70H775"/><path d="M855 106V135H105V203"/><path d="M185 239H215M375 239H405M565 239H595M755 239H785"/><path d="M865 275V309H10V70H25"/></g>
<text x="350" y="130" class="wf-svg-label">用户 WAV → 字幕/单词帧边界 → 实际听到的前缀</text><text x="275" y="301" class="wf-svg-label">助手全文 TTS → 按 PCM 播放 → 已播前缀反馈调度；未播尾部排除</text>
<g class="wf-svg-node" data-open-node="driver" tabindex="0" role="button" aria-label="查看交互调度"><rect x="25" y="34" width="150" height="72"/><text x="100" y="63">交互调度</text><text x="100" y="86" class="sub">T01 / T03 的时机</text></g>
<g class="wf-svg-node" data-open-node="observer" tabindex="0" role="button" aria-label="查看触发观察器"><rect x="205" y="34" width="160" height="72"/><text x="285" y="63">触发观察器</text><text x="285" y="86" class="sub">T03：ready / wait</text></g>
<g class="wf-svg-node" data-open-node="user_policy" tabindex="0" role="button" aria-label="查看用户生成器"><rect x="395" y="34" width="160" height="72"/><text x="475" y="63">用户生成器</text><text x="475" y="86" class="sub">当前轮 + 已听历史</text></g>
<g class="wf-svg-node" data-open-node="user_guard" tabindex="0" role="button" aria-label="查看用户检查器"><rect x="585" y="34" width="160" height="72"/><text x="665" y="63">用户检查器</text><text x="665" y="86" class="sub">拒绝则停止、不播放</text></g>
<g class="wf-svg-node" data-open-node="user_tts" tabindex="0" role="button" aria-label="查看用户TTS"><rect x="775" y="34" width="160" height="72"/><text x="855" y="63">用户 TTS</text><text x="855" y="86" class="sub">台词、T02 静音、旁语</text></g>
<g class="wf-svg-node assistant" data-open-node="alignment" tabindex="0" role="button" aria-label="查看用户PCM消费"><rect x="25" y="203" width="160" height="72"/><text x="105" y="232">用户 PCM 消费</text><text x="105" y="255" class="sub">仅释放已听前缀</text></g>
<g class="wf-svg-node assistant" data-open-node="listener" tabindex="0" role="button" aria-label="查看助手收听控制"><rect x="215" y="203" width="160" height="72"/><text x="295" y="232">助手收听控制</text><text x="295" y="255" class="sub">等待 / 停声 / 回应</text></g>
<g class="wf-svg-node assistant" data-open-node="assistant_response" tabindex="0" role="button" aria-label="查看助手正文"><rect x="405" y="203" width="160" height="72"/><text x="485" y="232">助手正文</text><text x="485" y="255" class="sub">respond 才调用</text></g>
<g class="wf-svg-node assistant" data-open-node="assistant_tts" tabindex="0" role="button" aria-label="查看助手TTS"><rect x="595" y="203" width="160" height="72"/><text x="675" y="232">助手 TTS</text><text x="675" y="255" class="sub">真实回复 → 全文 WAV</text></g>
<g class="wf-svg-node assistant" data-open-node="playback" tabindex="0" role="button" aria-label="查看PCM播放与停播"><rect x="785" y="203" width="160" height="72"/><text x="865" y="232">PCM 播放与停播</text><text x="865" y="255" class="sub">停声丢弃未播队列</text></g></svg></div>
<p>示意图展示有声主路径：T01 直接进入用户生成，T03 才经过触发观察器；用户 wait 不合成声音；助手 listen / ignore 等决定不会生成新正文。详细分支在下方步骤中。</p></details>
{dialogue}<section class="wf-explorer" id="workflow-steps"><div class="wf-explorer-head"><div><p class="wf-eyebrow">STEP EXPLORER</p><h3>逐步看完整输入、Prompt 与输出</h3><div class="wf-actions"><a href="workflow-records.zip" download>下载完整流程资料 ↓</a><a href="workflow-data.json" target="_blank" rel="noopener">流程数据 JSON ↗</a></div></div>{case_picker}<label class="wf-search">搜索步骤<input id="wf-search" type="search" placeholder="例如：观察器、Stage 3、奖励"/></label></div>
<div class="wf-filters"><button data-filter="all" aria-pressed="true">全部</button>{filters}<button data-filter="branches" aria-pressed="false">修复与可选分支</button><span id="wf-count"></span></div>
<div class="wf-detail-layout"><aside class="wf-catalog" aria-label="步骤目录">{nodes}<p id="wf-empty" hidden>没有匹配步骤。</p></aside><article class="wf-detail" id="wf-detail" aria-label="当前步骤详情"><header id="wf-step-head"></header><div class="wf-example-choice"><label for="wf-example">选择实际记录</label><select id="wf-example"></select><p id="wf-example-note"></p></div>
<nav class="wf-tabs" aria-label="步骤详情"><button data-tab="purpose" aria-selected="true" role="tab">主要做什么</button><button data-tab="inputs" aria-selected="false" role="tab">完整输入</button><button data-tab="prompt" aria-selected="false" role="tab">完整 Prompt</button><button data-tab="outputs" aria-selected="false" role="tab">完整输出</button><button data-tab="rules" aria-selected="false" role="tab">规则与失败</button></nav><div id="wf-detail-content" role="tabpanel"></div><footer id="wf-step-links"></footer></article></div></section></div>
<section data-panel="taxonomy" id="taxonomy-library" hidden><div class="wf-section-head"><p class="wf-eyebrow">FULL TAXONOMY · ACTUAL SOURCE VERSION</p><h3>完整场景与任务方向库</h3><p>展示本案例实际使用的全部领域、场景与四种任务风格。任务方向是 Stage 1 的多样性软参考；它们还不是具体用户目标或交互轨迹。</p><div class="wf-metrics" id="wf-taxonomy-metrics"></div><div class="wf-actions" id="wf-taxonomy-source"></div></div>
<div class="wf-taxonomy-chain"><div><small>程序 · seed / 配置</small><strong>选领域 / 场景组合</strong></div><span>→</span><div><small>程序 · seed / 配置</small><strong>选任务风格</strong></div><span>→</span><div><small>程序 · 按风格筛选</small><strong>传入整组任务方向</strong></div><span>→</span><div><small>Stage 1 · 有 Prompt</small><strong>模型生成具体 goal</strong></div></div>
<p class="wf-note">采样这一步没有模型 Prompt。库本身可以由 Stage 0 的生成 / Judge / Repair Prompt 预先建立；下一步 Stage 1 也有自己的 Prompt。默认 Stage 1 入口按种子从领域 / 场景组合中无放回采样，另选风格，也支持指定与重放。</p>
<div class="wf-taxonomy-filters"><label>领域<select id="wf-taxonomy-domain" aria-label="筛选领域"></select></label><label>任务风格<select id="wf-taxonomy-style" aria-label="筛选任务风格"></select></label><label class="wf-taxonomy-search">搜索场景 / 任务方向<input id="wf-taxonomy-search" type="search" placeholder="名称、ID 或方向中的词"/></label><button class="wf-link-button" data-taxonomy-current>定位本次选中的场景</button></div>
<p id="wf-taxonomy-count" class="wf-status" role="status"></p><div class="wf-taxonomy-layout"><aside class="wf-taxonomy-list" id="wf-taxonomy-list" aria-label="全部场景"></aside><article class="wf-card wf-taxonomy-detail" id="wf-taxonomy-detail" aria-label="当前场景的全部任务方向"></article></div></section>
<section data-panel="visibility" hidden><div class="wf-section-head"><p class="wf-eyebrow">INPUT BOUNDARIES</p><h3>每个模型只收到自己的视角</h3><p>离线环境有完整任务信息，运行时按角色裁切。界面展示整份材料不代表这些材料曾注入助手。点击某个角色，可查看其实际输入和 Prompt。</p></div><div class="wf-visibility" id="wf-visibility"></div></section>
<section data-panel="traces" hidden><div class="wf-section-head"><p class="wf-eyebrow">E001 · ACTUAL COMPLETED EPISODE</p><h3>沿 13 个主轮查看真实调用</h3><p>这里对应本批唯一完成的 E001。每轮可看用户台词、助手实际已播回复、原标准和评分，并打开该轮的完整调用。左声道用户、右声道助手。</p></div><audio id="wf-dialogue" controls preload="none" src="{h(data['dialogue_audio_url'])}" aria-label="E001完整交互音频"></audio><p id="wf-audio-status" role="status"></p><div class="wf-trace-buttons" id="wf-trace-buttons"></div><div id="wf-trace-detail"></div><div id="wf-call-detail" hidden></div></section>
<section data-panel="failures" hidden><div class="wf-section-head"><p class="wf-eyebrow">PRESERVED OUTCOMES</p><h3>实际完成了什么，在哪里停止</h3><p>完整性核验通过，表示记录与边界可核验；它不等于全部任务评分通过，也不等于每次交互都完成。</p></div><div id="wf-failures"></div></section>
<p class="wf-footnote">本页展示保存的源码与运行实录，报告构建不发起模型调用。每份资料均提供原文件与 SHA-256。调用保存的 Prompt 和当前源码模板分开显示；API 等待期间 PCM 时钟冻结，这批数据来自 GPT 文本策略 + MiniMax TTS 的合成交互。</p>
<noscript>此详细浏览器需要 JavaScript；原始流程资料可通过 ZIP 和 JSON 下载。</noscript>
<script id="workflow-explorer-data" type="application/json">{json_payload(data)}</script></section>'''
