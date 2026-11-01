(() => {
  'use strict';
  const root = document.querySelector('[data-workflow-explorer]');
  if (!root) return;
  const data = JSON.parse(document.getElementById('workflow-explorer-data').textContent);
  const byId = new Map(data.nodes.map(n => [n.id, n]));
  const dialogueFrame = document.getElementById('wf-dialogue-frame');
  function resizeDialogue() {
    if (!dialogueFrame) return;
    const doc = dialogueFrame.contentDocument;
    if (doc?.body) dialogueFrame.style.height = Math.ceil(doc.querySelector('main')?.getBoundingClientRect().bottom || doc.body.scrollHeight) + 16 + 'px';
  }
  if (dialogueFrame) {
    dialogueFrame.addEventListener('load', () => {
      resizeDialogue();
      if (dialogueFrame.contentDocument?.body) new ResizeObserver(resizeDialogue).observe(dialogueFrame.contentDocument.querySelector('main') || dialogueFrame.contentDocument.body);
    });
    root.querySelectorAll('[data-dialogue-version]').forEach(button => button.addEventListener('click', () => {
      dialogueFrame.hidden = false;
      root.querySelectorAll('[data-dialogue-version]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
      dialogueFrame.src = button.dataset.dialogueSrc;
      document.getElementById('wf-dialogue-version-status').textContent = data.dialogue_versions.find(v => v.id === button.dataset.dialogueVersion).description;
      if (button.dataset.dialogueCase && button.dataset.dialogueCase !== activeCase) setCase(button.dataset.dialogueCase);
    }));
  }
  const $ = s => root.querySelector(s);
  const $$ = s => Array.from(root.querySelectorAll(s));
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const pretty = value => JSON.stringify(value, null, 2);
  const phases = {offline:'离线环境',prepare:'准备与批量',interaction:'交互循环',scoring:'事后评分',dataset:'导出与核验',branches:'修复与可选分支'};
  const kinds = {program:'程序步骤 · 无模型 Prompt',llm:'独立模型调用',tts:'TTS · 文本与音频参数'};
  const statuses = {pass:'通过',fail:'失败',insufficient_evidence:'证据不足',not_triggered:'未触发'};
  let activeCase = data.default_case || data.cases?.[0]?.id;
  let taxonomy = data.taxonomy_catalog_by_case?.[activeCase] || data.taxonomy_catalog;
  let taxScenes = taxonomy ? taxonomy.value.domains.flatMap(domain => domain.scenarios.map(scene => ({domain,scene}))) : [];
  let taxSceneId = taxonomy?.selected.scenario_id;
  let current = data.nodes[0], exampleIndex = 0, tab = 'purpose', filter = 'all', traceIndex = 0, callVersion = 0;
  let textRecords = new Map();
  const url = path => esc(path);
  const goButton = (id, label) => `<button class="wf-link-button" data-open-node="${esc(id)}">${esc(label || byId.get(id).title)}</button>`;
  function record(item, label, open = false, prompt = false) {
    const key = 'record-' + textRecords.size;
    textRecords.set(key, item.text ?? pretty(item.value));
    const name = item.label || label;
    return `<details class="wf-record" ${open ? 'open' : ''}><summary>${esc(label || name)}</summary><div class="wf-record-meta">${esc(item.source_path || name)}${item.sha256 ? `<br><span class="wf-hash">SHA-256 · ${esc(item.sha256)} · ${item.bytes} bytes</span>` : ''}</div><div class="wf-actions"><button data-copy="${key}">复制全文</button>${item.path ? `<a href="${url(item.path)}" download>下载原文件 ↓</a><a href="${url(item.path)}" target="_blank" rel="noopener">打开原文件 ↗</a>` : `<button data-save="${key}">下载全文 ↓</button>`}<span class="wf-status" role="status"></span></div><pre class="${prompt ? 'wf-prompt' : ''}" data-record-key="${key}">${esc(item.text ?? pretty(item.value))}</pre></details>`;
  }
  const inline = (value, label, open = false) => record({value,label}, label, open);
  const contract = (fields, heading) => `<div class="wf-card"><h4>${esc(heading)}</h4><ul>${fields.map(f => `<li>${esc(f)}</li>`).join('')}</ul></div>`;
  function scriptOutput(example, files) {
    const source = example?.script_snapshot?.source || files.find(item => item.source_path.endsWith('/episode_script.json')) || files[0];
    if (!source) return '<p class="wf-empty-note">未生成用户主线剧本；没有可解析的输出。</p>';
    let script;
    try {
      script = JSON.parse(source.text);
      if (example.script_snapshot) script = script[example.script_snapshot.json_key];
    } catch (_) {
      return '<p class="wf-empty-note">这份原始输出无法解析为 JSON；完整响应保留在下方。</p>';
    }
    if (!Array.isArray(script?.episode_turns)) return '<p class="wf-empty-note">这份输出没有 episode_turns 轮次列表；请查看下方原文。</p>';
    const turns = script.episode_turns, catalog = data.behavior_catalog;
    const actName = ref => `${ref} · ${catalog?.acts[ref]?.behavior_name || '未找到定义'}`;
    const timingName = ref => `${ref} · ${catalog?.timings[ref]?.timing_name || '未找到定义'}`;
    const review = example.script_review;
    const reviewer = current.id === 'stage35' ? '3.5J' : '3J';
    const decision = {pass:reviewer+' 已通过',repair:reviewer+' 要求修复',fail:reviewer+' 未通过'}[review?.value.decision] || '未记录语义检查结论';
    const counts = ['T03','T02'].map(ref => turns.filter(t => t.timing_refs?.includes(ref)).length);
    const legend = catalog ? `<details class="wf-script-legend"><summary>展开行为与发言时机的完整定义 · A01–A06 / T01–T03</summary><dl>${[...Object.entries(catalog.acts).map(([ref,item]) => [actName(ref),item.definition]),...Object.entries(catalog.timings).map(([ref,item]) => [timingName(ref),item.definition])].map(([name,definition]) => `<dt>${esc(name)}</dt><dd>${esc(definition)}</dd>`).join('')}</dl><a href="${url(catalog.source.path)}" target="_blank" rel="noopener">定义来源：behavior_catalog.py ↗</a></details>` : '';
    const findings = review?.value.findings?.length ? `<div class="wf-note wf-script-warning"><b>这份候选仍需修复</b>${review.value.findings.map(f => `<p>${esc(f.problem)}<br>修复要求：${esc(f.repair)}</p>`).join('')}<p>上方「选择实际记录」可切换到最终冻结版本。</p></div>` : '';
    const cards = turns.map((turn, i) => {
      const timings = Array.isArray(turn.timing_refs) ? turn.timing_refs : [];
      const pause = turn.pause_cue;
      const speakers = {user:'用户',assistant:'助手',third_party:'旁人',machine:'机器',none:'无特定对象'};
      const sideSpeech = (turn.side_speech || []).map(event => `<div class="wf-script-pause"><b>旁语 · ${esc(speakers[event.speaker])} → ${esc(speakers[event.target])}</b><p>${esc(event.text)}</p><p>${event.placement === 'during_assistant' ? '发生在助手播报中' : '发生在用户发言中'}</p></div>`).join('');
      const backchannels = (turn.user_backchannels || []).map(event => `<div class="wf-script-pause"><b>用户附和 · 助手继续原播报</b><p>台词：${esc(event.text)}</p><p>触发：${esc(event.trigger_anchor)}</p></div>`).join('');
      const pauseText = pause ? `<div class="wf-script-pause"><b>句内停顿 · pause_cue</b><p>说完「<span data-script-field="pause_after">${esc(pause.after_text)}</span>」后，暂停 <strong data-script-field="pause_ms">${esc(pause.duration_ms)}</strong> 毫秒，再继续同一表达。</p></div>` : '';
      return `<article class="wf-script-turn" id="wf-script-turn-${i}" data-script-turn="${esc(turn.turn_id)}"><header><span class="wf-script-number">${esc(turn.turn_id)}</span><div><small>用户本轮目标 · semantic_goal</small><h4 data-script-field="semantic_goal">${esc(turn.semantic_goal ?? '未生成')}</h4></div></header><dl class="wf-script-controls"><div><dt>用户行为 · act_ref</dt><dd data-script-field="act_ref">${esc(actName(turn.act_ref))}</dd></div><div><dt>发言时机 · timing_refs</dt><dd data-script-field="timing_refs">${timings.map(ref => esc(timingName(ref))).join('<br>') || '未生成'}</dd></div></dl><p class="wf-script-label">用户计划台词 · user_utterance</p><blockquote data-script-field="user_utterance">${esc(turn.user_utterance ?? '未生成')}</blockquote>${pauseText}${sideSpeech}${backchannels}<div class="wf-script-expectation"><p class="wf-script-label">预期助手回应 · assistant_semantic_goal</p><p data-script-field="assistant_semantic_goal">${esc(turn.assistant_semantic_goal ?? '未生成')}</p></div></article>`;
    }).join('');
    return `<section class="wf-script-reading" aria-label="用户主线剧本逐轮解读"><div class="wf-script-heading"><p class="wf-eyebrow">EPISODE SCRIPT · READABLE OUTPUT</p><h4>用户主线剧本 · 逐轮解读</h4><div class="wf-chips"><span>${esc(example.script_version || example.label)}</span><span>${esc(decision)}</span><span>${turns.length} 个主轮</span><span>${counts[0]} 轮播报中插话</span><span>${counts[1]} 轮句内停顿</span></div><p>每轮按「用户目标 → 行为与时机 → 用户台词 → 预期助手回应」展开。预期助手回应是离线设计目标，实际交互时不会注入助手。</p>${example.script_note ? `<p>${esc(example.script_note)}</p>` : ''}<p class="wf-script-source">解析来源：<a href="${url(source.path)}" target="_blank" rel="noopener">${esc(source.source_path)}</a>${example.script_snapshot ? `<br>提取字段：/${esc(example.script_snapshot.json_key)}` : ''}<br>SHA-256 · ${esc(source.sha256)}${review ? `<br>检查依据：<a href="${url(review.source.path)}" target="_blank" rel="noopener">${esc(review.source.source_path)}</a>` : ''}</p></div>${findings}${legend}<nav class="wf-script-jumps" aria-label="定位剧本轮次">${turns.map((turn,i) => `<button data-script-jump="${i}">${esc(turn.turn_id)}</button>`).join('')}</nav>${cards || '<p class="wf-empty-note">无剧本轮次</p>'}</section>`;
  }
  function mode(name, scroll = false) {
    $$('[data-panel]').forEach(p => p.hidden = p.dataset.panel !== name);
    $$('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === name)));
    if (name !== 'traces') $('#wf-dialogue').pause();
    if (scroll) $('.wf-modebar').scrollIntoView({block:'start',behavior:'smooth'});
  }
  function filtered() {
    const query = $('#wf-search').value.trim().toLowerCase();
    const visible = data.nodes.filter(n => (filter === 'all' || n.phase === filter) && [n.title,n.purpose,...n.input_fields,...n.output_fields,...n.rules].join(' ').toLowerCase().includes(query));
    const ids = new Set(visible.map(n => n.id));
    $$('.wf-catalog .wf-step').forEach(b => b.hidden = !ids.has(b.dataset.node));
    $$('[data-filter]').forEach(b => b.setAttribute('aria-pressed',String(b.dataset.filter === filter)));
    $('#wf-count').textContent = `${visible.length} / ${data.nodes.length} 步`;
    $('#wf-empty').hidden = visible.length !== 0;
    return visible;
  }
  function select(id, scroll = false, selectedTab) {
    if (!byId.has(id)) return;
    current = byId.get(id);
    const choices = current.examples.map((e,i) => ({e,i})).filter(({e}) => !activeCase || e.dataset_case === activeCase);
    const preferred = current.default_examples_by_case?.[activeCase] ?? current.default_example ?? 0;
    exampleIndex = choices.some(({i}) => i === preferred) ? preferred : choices[0]?.i ?? -1;
    if (selectedTab) tab = selectedTab;
    mode('flow');
    if (filter !== 'all' && filter !== current.phase) filter = 'all';
    $('#wf-search').value = ''; filtered();
    $$('.wf-catalog .wf-step').forEach(b => b.setAttribute('aria-current', b.dataset.node === id ? 'step' : 'false'));
    const actual = current.actual_by_case?.[activeCase] || current.actual;
    const state = actual.includes('未') || actual.includes('失败') ? 'warn' : '';
    $('#wf-step-head').innerHTML = `<div class="wf-chips"><span>${esc(phases[current.phase])}</span><span>${esc(kinds[current.kind])}</span><span class="${state}">${esc(actual)}</span></div><h3>${esc(current.title)}</h3><p>${esc(current.purpose)}</p>`;
    const choose = $('#wf-example');
    choose.innerHTML = choices.length ? choices.map(({e,i}) => `<option value="${i}">${esc(e.label)}</option>`).join('') : '<option>此案例无该分支实际调用；查看源码契约</option>';
    if (choices.length) choose.value = String(exampleIndex);
    choose.disabled = !choices.length;
    const source = current.source_by_case?.[activeCase] || current.source;
    $('#wf-step-links').innerHTML = `<b>上游</b><div class="wf-actions">${current.upstream.map(id => goButton(id)).join('') || '入口 / 独立背景分支'}</div><b>下游</b><div class="wf-actions">${current.downstream.map(id => goButton(id)).join('') || '此分支终点'}</div><a href="${url(source.path)}" target="_blank" rel="noopener">查看实现：${esc(source.source_path)} ↗</a>`;
    detail();
    const button = $$('.wf-catalog .wf-step').find(b => b.dataset.node === id);
    if (button && scroll && innerWidth > 700) $('.wf-catalog').scrollTop = button.offsetTop - $('.wf-catalog').offsetTop - 40;
    if (scroll) $('#wf-detail').scrollIntoView({block:'start',behavior:'smooth'});
    root.dataset.selectedNode = id;
  }
  function setCase(id) {
    if (!data.cases?.some(c => c.id === id)) return;
    activeCase = id;
    const picker = $('#wf-case');
    if (picker) picker.value = id;
    const label = $('#wf-current-case');
    if (label) label.textContent = data.cases.find(c => c.id === id).label;
    $$('[data-case-choice]').forEach(button => button.setAttribute('aria-current', String(button.dataset.caseChoice === id)));
    root.dataset.selectedCase = id;
    taxonomy = data.taxonomy_catalog_by_case?.[id] || data.taxonomy_catalog;
    taxScenes = taxonomy ? taxonomy.value.domains.flatMap(domain => domain.scenarios.map(scene => ({domain,scene}))) : [];
    taxSceneId = taxonomy?.selected.scenario_id;
    taxonomyInit();
    select(current.id);
    if (dialogueFrame && !data.dialogue_versions?.some(v => v.dataset_case === id)) {
      dialogueFrame.hidden = true;
      dialogueFrame.src = 'about:blank';
      document.getElementById('wf-dialogue-version-status').textContent = '此案例未完成并保留为失败；没有完整语音或训练数据。可在下方查看生成、修复和审查记录。';
      root.querySelectorAll('[data-dialogue-version]').forEach(b => b.setAttribute('aria-pressed','false'));
    }
  }
  function detail() {
    const example = current.examples[exampleIndex];
    $$('[data-tab]').forEach(b => b.setAttribute('aria-selected',String(b.dataset.tab === tab)));
    $('#wf-example-note').textContent = example ? (example.prompt ? '当前选择有实际保存的 Prompt；输入/输出都是这次记录的原文件。' : '当前选择为实际程序/TTS记录或派生摘要；原文件在下方。') : '没有实际调用就保留未触发状态，不补写示例。';
    let body = '';
    if (tab === 'purpose') {
      body = `<p>${esc(current.purpose)}</p><div class="wf-note"><b>执行频率：</b>${esc(current.cadence)}<br><b>当前记录：</b>${esc(current.actual_by_case?.[activeCase] || current.actual)}</div><div class="wf-contract">${contract(current.input_fields,'接收什么')}${contract(current.output_fields,'交付什么')}</div><p>选择上方实际记录，再点击「完整输入」「完整 Prompt」「完整输出」即可对照一次调用。</p>`;
      if (current.id === 'taxonomy') body += `<div class="wf-note"><strong>程序采样，无 Prompt：</strong>选定场景和风格后，整组参考方向进入 Stage 1；具体 goal 由下一步模型生成。</div><button class="wf-link-button" data-show-taxonomy>浏览本版本 ${taxonomy?.stats.scenarios || 0} 个场景与 ${taxonomy?.stats.directions || 0} 个任务方向 ↗</button>`;
      if (example?.audio_url) body += `<audio controls preload="none" src="${url(example.audio_url)}" aria-label="当前用户发言音频"></audio>`;
      if (example?.metadata) body += inline(example.metadata.value, '真实调用元数据', false);
    } else if (tab === 'inputs' || tab === 'outputs') {
      const isInput = tab === 'inputs';
      body = contract(isInput ? current.input_fields : current.output_fields, isInput ? '输入字段与边界' : '输出字段与去向');
      const files = example?.[tab] || [];
      const readableScript = !isInput && ['stage3','stage35'].includes(current.id);
      if (readableScript) body += scriptOutput(example, files) + '<h4 class="wf-script-raw-heading">原始输出 · 完整 JSON / 响应与检查记录</h4>';
      if (readableScript && example?.script_snapshot) body += record(example.script_snapshot.source,'首次候选的保存来源 · 3J 输入中的 episode_script');
      body += files.map((item,i) => record(item,`${i+1} · ${item.label}`,!readableScript && i===0)).join('');
      if (!isInput && example?.inline_output) {
        body += inline(example.inline_output,'从原文件选出的实际样本与控制奖励',true);
        body += '<div class="wf-actions">' + (example.derived_from || []).map(item => `<a href="${url(item.path)}" target="_blank" rel="noopener">派生来源：${esc(item.label)} ↗</a>`).join('') + '</div>';
      }
      if (!files.length && (isInput || !example?.inline_output)) body += `<p class="wf-empty-note">${example ? '该记录没有单独归档此类文件；请查看字段契约和关联源码。' : '此案例未执行该分支，因此没有实际输入/输出文件。'}</p>`;
    } else if (tab === 'prompt') {
      body = '<p class="wf-note">一次文本模型调用由 <strong>Prompt 指令 + 输入上下文 + 调用参数</strong>组成。下方完整展示保存的指令；动态 JSON 上下文在「完整输入」中。这里不把源码模板的占位符冒充已填充调用。</p>';
      if (example?.prompt) body += record(example.prompt,'本次调用保存的完整 Prompt',true,true);
      else body += `<p class="wf-empty-note">${current.kind === 'tts' ? '这是 TTS 调用：没有 GPT system Prompt。请在「完整输入」查看实际文本、音色和音频参数。' : current.kind === 'program' ? '这是确定性程序步骤，没有模型 Prompt。输入、输出和实现源码说明了具体处理规则。' : '当前选择没有单独保存的 Prompt；此分支的模板在下方。没有虚构历史请求。'}</p>`;
      body += current.prompts.map((item,i) => record(item,`当前源码模板 ${i+1} · ${item.label}`,!example?.prompt && i===0,true)).join('');
      if (!current.prompts.length && current.kind === 'program') body += `<div class="wf-actions"><a href="${url(current.source.path)}" target="_blank" rel="noopener">查看程序实现 ↗</a></div>`;
    } else {
      body = `<div class="wf-card"><h4>契约与实际限制</h4><ul>${(current.rules_by_case?.[activeCase] || current.rules).map(rule => `<li>${esc(rule)}</li>`).join('')}</ul></div>`;
      body += data.failure_reviews.filter(f => f.node === current.id && (!activeCase || f.dataset_case === activeCase)).map(f => `<div class="wf-card" style="margin-top:14px"><h4>${esc(f.episode)} · ${esc(f.where)}</h4><p><b>原记录：</b>${esc(f.recorded)}</p><p><b>后续判断：</b>${esc(f.latest)}</p></div>`).join('');
      const implementation = current.source_by_case?.[activeCase] || current.source;
      body += record({...implementation,text:undefined,value:{implementation:implementation.source_path,sha256:implementation.sha256}},'实现来源与哈希');
    }
    $('#wf-detail-content').innerHTML = body;
    root.dataset.selectedTab = tab;
  }
  function visibility() {
    $('#wf-visibility').innerHTML = data.visibility.map(role => `<div class="wf-card"><h4>${esc(role.role)}</h4><dl><dt>实际可见</dt><dd>${esc(role.allowed)}</dd><dt class="hidden-label">未注入 / 职责边界</dt><dd>${esc(role.hidden)}</dd></dl>${goButton(role.prompt,'打开实际输入与 Prompt')}</div>`).join('');
  }
  function taxonomyDetail(row) {
    const {domain,scene} = row, style = $('#wf-taxonomy-style').value;
    const selected = scene.scenario_id === taxonomy.selected.scenario_id;
    const styles = taxonomy.styles.filter(s => style === 'all' || s === style);
    $('#wf-taxonomy-detail').innerHTML = `<div class="wf-chips"><span>${esc(domain.domain_id)} · ${esc(domain.domain)}</span>${selected ? '<span>本次 case 使用的场景</span>' : ''}</div><h3>${esc(scene.scenario_id)} · ${esc(scene.scenario)}</h3><p>${esc(domain.definition || '')}</p><p class="wf-status">本场景共有 ${Object.values(scene.example_tasks).reduce((n,tasks)=>n+tasks.length,0)} 个参考方向；当前显示 ${styles.reduce((n,s)=>n+(scene.example_tasks[s] || []).length,0)} 个。</p>${styles.map(s => `<section class="wf-direction-group" data-direction-style="${esc(s)}"><header><h4>${esc(s)} · ${(scene.example_tasks[s] || []).length} 个方向</h4>${selected && s === taxonomy.selected.task_style ? '<span class="wf-status">这 5 个方向整组传入本次 Stage 1</span>' : ''}</header><ol>${(scene.example_tasks[s] || []).map(text => `<li data-taxonomy-direction>${esc(text)}</li>`).join('')}</ol>${!(scene.example_tasks[s] || []).length ? '<p class="wf-empty-note">该版本未生成此风格方向。</p>' : ''}</section>`).join('')}${selected ? `<div class="wf-note"><b>本次 Stage 1 实际生成的具体 goal</b><p>${esc(taxonomy.actual_goal)}</p><p>这是模型生成的目标；采样阶段没有把某一条方向直接指定为最终目标。</p>${goButton('stage1','查看 Stage 1 的完整输入与 Prompt')}</div>` : ''}${inline(scene,'此场景的完整原始 JSON')}`;
    root.dataset.selectedScenario = scene.scenario_id;
  }
  function taxonomyFilter(keep = true) {
    const domain = $('#wf-taxonomy-domain').value, style = $('#wf-taxonomy-style').value, query = $('#wf-taxonomy-search').value.trim().toLowerCase();
    const rows = taxScenes.filter(row => (domain === 'all' || row.domain.domain_id === domain) && [row.domain.domain,row.domain.domain_id,row.scene.scenario,row.scene.scenario_id,...taxonomy.styles.filter(s=>style==='all'||s===style).flatMap(s=>row.scene.example_tasks[s] || [])].join(' ').toLowerCase().includes(query));
    if (!keep || !rows.some(row=>row.scene.scenario_id===taxSceneId)) taxSceneId = rows[0]?.scene.scenario_id;
    $('#wf-taxonomy-list').innerHTML = rows.map(({domain,scene})=>`<button class="wf-step" data-taxonomy-scene="${esc(scene.scenario_id)}" aria-current="${scene.scenario_id===taxSceneId?'true':'false'}"><small>${esc(domain.domain_id)} · ${esc(domain.domain)}</small><strong>${esc(scene.scenario_id)} · ${esc(scene.scenario)}</strong></button>`).join('');
    const directions = rows.reduce((n,row)=>n+taxonomy.styles.filter(s=>style==='all'||s===style).reduce((sum,s)=>sum+(row.scene.example_tasks[s] || []).length,0),0);
    $('#wf-taxonomy-count').textContent = `匹配 ${rows.length} / ${taxonomy.stats.scenarios} 个场景 · 所选风格下共有 ${directions} 个方向。搜索用于定位场景，选中后完整显示该场景方向。`;
    const row = rows.find(row=>row.scene.scenario_id===taxSceneId);
    if (row) taxonomyDetail(row);else $('#wf-taxonomy-detail').innerHTML = '<p>没有匹配的场景；可清空搜索或切换领域 / 风格。</p>';
  }
  function taxonomyInit() {
    if (!taxonomy) return;
    const stats = taxonomy.stats;
    $('#wf-taxonomy-metrics').innerHTML = [[stats.domains,'领域'],[stats.scenarios,'场景'],[taxonomy.styles.length,'任务风格'],[stats.directions,'任务方向']].map(([n,label])=>`<div><strong>${n}</strong><span>${label}</span></div>`).join('');
    $('#wf-taxonomy-source').innerHTML = `<a href="${url(taxonomy.artifact.path)}" download>下载完整库 JSON ↓</a><a href="${url(taxonomy.artifact.path)}" target="_blank" rel="noopener">查看完整库原文 ↗</a>${goButton('taxonomy','回到采样步骤')}${goButton('task_library','查看 Stage 0 造库的 Prompt')}<p class="wf-taxonomy-hash">版本：${esc(taxonomy.value.taxonomy_version)}<br>文件 SHA-256：${esc(taxonomy.artifact.sha256)}<br>taxonomy 内容哈希：${esc(taxonomy.content_sha256)}（与本次采样清单一致）</p>`;
    $('#wf-taxonomy-domain').innerHTML = '<option value="all">全部 32 个领域</option>'+taxonomy.value.domains.map(d=>`<option value="${esc(d.domain_id)}">${esc(d.domain_id)} · ${esc(d.domain)}</option>`).join('');
    $('#wf-taxonomy-style').innerHTML = '<option value="all">全部 4 种风格</option>'+taxonomy.styles.map(s=>`<option value="${esc(s)}">${esc(s)} · 全库 ${stats.by_style[s]} 个方向</option>`).join('');
    $('#wf-taxonomy-domain').onchange = ()=>taxonomyFilter(false);
    $('#wf-taxonomy-style').onchange = ()=>taxonomyFilter();
    $('#wf-taxonomy-search').oninput = ()=>taxonomyFilter();
    taxonomyFilter();
  }
  function trace(index) {
    traceIndex = index; callVersion++; $('#wf-call-detail').hidden = true;
    const t = data.traces[index];
    $$('#wf-trace-buttons button').forEach((b,i) => b.setAttribute('aria-pressed',String(i===index)));
    const responseText = t.responses.map(r => `<div class="wf-response"><small>${esc(r.audio_id || r.response_id || '')} · ${((r.start_frame || 0)/16000).toFixed(2)} 秒起</small>${esc(r.heard_text ?? r.text ?? r.played_text ?? pretty(r))}</div>`).join('');
    $('#wf-trace-detail').innerHTML = `<div class="wf-trace-top"><h3>${esc(t.turn_id)} · ${esc(t.timing_refs.join(' / '))}</h3><button class="wf-link-button" data-seek="${t.start_seconds}">从本轮 ${t.start_seconds.toFixed(2)} 秒试听 ▶</button></div>${t.end_seconds > t.scoring_end_seconds ? '<p class="wf-note">本轮实录还包含主任务评分片段结束后的告别和最终收听动作；它们保留为实际策略样本，没有新增任务评分标准。</p>' : ''}<div class="wf-turn-grid"><div class="wf-card"><h4>本轮用户主发言</h4><p class="wf-user-quote">${esc(t.user_text)}</p>${t.pause ? inline(t.pause,'本轮 T02 静音与片段边界') : ''}<h4>实际模型调用</h4><div class="wf-call-buttons">${t.members.map((m,i) => `<button class="wf-link-button" data-call="${i}">${esc(m.label)} · 完整输入 / Prompt / 输出 ↗</button>`).join('')}</div><h4>导出到哪些样本</h4><p>${t.sample_ids.map(esc).join('<br>')}</p></div><div class="wf-card"><h4>助手实际已播回复</h4>${responseText || '<p>此轮没有归属的已播回复。</p>'}<p>这里仅展示实际已播部分；完整生成正文在调用原记录中，未播尾部不参与评分或训练目标。</p></div></div><h4>原标准与实际评分</h4>${t.criteria.map(c => `<div class="wf-criterion ${esc(c.status)}"><small>${esc(c.criterion_id)} · ${esc(statuses[c.status] || c.status)}</small><p>${esc(c.text)}</p><p>${esc(c.reason)}</p>${(c.evidence || []).map(e => `<blockquote>${esc(e.quote)}${e.start_frame != null ? ` <button class="wf-link-button" data-seek="${e.start_frame/16000}">试听引用</button>` : ''}</blockquote>`).join('')}</div>`).join('')}${inline(t.measurement,'程序 PCM 测量')}${inline(t.responses,'已播回复原 JSON')}`;
    root.dataset.selectedTurn = t.turn_id;
  }
  const cache = new Map();
  async function sourceText(item) {
    if (item.text != null) return item;
    if (!cache.has(item.path)) cache.set(item.path,fetch(item.path).then(r => {if (!r.ok) throw Error(`原文件读取失败：${r.status}`);return r.text();}));
    return {...item,text:await cache.get(item.path)};
  }
  async function call(index) {
    const version = ++callVersion, member = data.traces[traceIndex].members[index], panel = $('#wf-call-detail');
    panel.hidden = false; panel.className = 'wf-call-panel';
    panel.innerHTML = '<p role="status">正在读取本次调用的原始文件…</p>';
    panel.scrollIntoView({block:'start',behavior:'smooth'});
    try {
      const items = await Promise.all([sourceText(member.input),member.prompt ? sourceText(member.prompt) : null,sourceText(member.output)]);
      if (version !== callVersion) return;
      panel.innerHTML = `<h3>${esc(member.label)}</h3><p>这三份文件属于同一次真实调用。</p>${record(items[0],'完整输入上下文',true)}${items[1] ? record(items[1],'本次完整 Prompt',true,true) : '<p>此记录未单独保存 Prompt。</p>'}${record(items[2],'完整原输出',true)}<div class="wf-actions">${goButton(member.node,'回到这个步骤的完整说明')}</div>`;
    } catch (error) {
      if (version === callVersion) panel.innerHTML = `<p role="alert">${esc(error.message)}</p><div class="wf-actions"><a href="${url(member.input.path)}">打开原输入</a><a href="${url(member.output.path)}">打开原输出</a></div>`;
    }
  }
  function failures() {
    if (data.successful_cases_only) {$('#wf-failures').replaceChildren();return;}
    $('#wf-failures').innerHTML = `<div class="wf-card"><h4>E001 · 完整交互，53 条新样本</h4><p>13 个主轮，另有一次同轮纠错和告别。53 = 14 条助手正文 + 24 条助手收听控制 + 15 条用户行动；样本按策略决策计数，一条轨迹可以有很多样本。</p><p>20 条原 Semantic 标准：14 通过 / 2 失败 / 4 证据不足；10 条 Behavior 标准全部通过。原 30 条标准分别导出奖励。</p>${goButton('export','查看真实样本、奖励与导出规则')}</div><div class="wf-failure-grid" style="margin-top:14px">${data.failure_reviews.map(f => `<div class="wf-card"><small>历史运行状态 · 失败保留</small><h4>${esc(f.episode)} · ${esc(f.where)}</h4><p><b>原始停止原因：</b>${esc(f.recorded)}</p><p><b>用户判断与处理：</b>${esc(f.latest)}</p><p>部分交互和失败记录保留，没有进入完整轨迹训练导出。</p>${goButton(f.node,'查看失败的原输入、Prompt 和输出')}</div>`).join('')}</div><div class="wf-contract wf-key-facts"><div class="wf-card"><h4>数据已经到哪里</h4><p>已完成通用观测 / 动作 / 奖励 / 音频片段格式与边界核验。本批 1 条完整轨迹 + 之前 1 条完整轨迹，共 <strong>103 条样本</strong>。</p>${goButton('dataset_verify','查看全批与训练边界核验')}</div><div class="wf-card"><h4>下一层尚未实现</h4><p>还没有目标模型 tokenizer、音频 codec、训练 adapter 或优化器；没有实际权重更新。当前生成方式是文本策略 + TTS，PCM 采样时间与 API 墙钟耗时分开记录。</p>${goButton('training','查看当前终点与训练接口边界')}</div></div>`;
  }
  function seek(seconds) {
    const audio = $('#wf-dialogue');
    const start = () => {audio.currentTime = seconds;audio.play().catch(() => {$('#wf-audio-status').textContent = '已定位；点击播放器开始试听。';});};
    if (audio.readyState >= 1) start();
    else {audio.addEventListener('loadedmetadata',start,{once:true});audio.load();}
    $('#wf-audio-status').textContent = `定位到 ${seconds.toFixed(2)} 秒，左声道用户、右声道助手。`;
  }
  root.addEventListener('click',async event => {
    const target = event.target.closest('button,a,[data-open-node]');
    if (!target) return;
    if (target.hasAttribute('data-show-taxonomy')) mode('taxonomy',true);
    else if (target.hasAttribute('data-taxonomy-current')) {$('#wf-taxonomy-domain').value=taxonomy.selected.domain_id;$('#wf-taxonomy-style').value=taxonomy.selected.task_style;$('#wf-taxonomy-search').value='';taxSceneId=taxonomy.selected.scenario_id;taxonomyFilter();}
    else if (target.dataset.taxonomyScene) {taxSceneId=target.dataset.taxonomyScene;taxonomyFilter();if(innerWidth<=700)$('#wf-taxonomy-detail').scrollIntoView({block:'start',behavior:'smooth'});}
    else if (target.dataset.mode) mode(target.dataset.mode,true);
    else if (target.dataset.scriptJump != null) $(`#wf-script-turn-${Number(target.dataset.scriptJump)}`)?.scrollIntoView({block:'start',behavior:'smooth'});
    else if (target.dataset.node) select(target.dataset.node,innerWidth<=700);
    else if (target.dataset.openNode) select(target.dataset.openNode,true,'prompt');
    else if (target.dataset.phase || target.dataset.filter) {
      filter = target.dataset.phase || target.dataset.filter; const found = filtered();
      if (found.length) select(found[0].id,false);
      if (target.dataset.phase) $('#workflow-steps').scrollIntoView({block:'start',behavior:'smooth'});
    } else if (target.dataset.tab) {tab = target.dataset.tab;detail();}
    else if (target.dataset.trace != null) trace(Number(target.dataset.trace));
    else if (target.dataset.call != null) await call(Number(target.dataset.call));
    else if (target.dataset.seek != null) seek(Number(target.dataset.seek));
    else if (target.dataset.copy) {
      const text = textRecords.get(target.dataset.copy), status = target.parentElement.querySelector('[role=status]');
      let success = false;
      try {if (navigator.clipboard?.writeText) {await navigator.clipboard.writeText(text);success = true;}} catch (_) {}
      if (!success) {
        const area = document.createElement('textarea');area.value = text;area.style.position = 'fixed';area.style.opacity = '0';document.body.append(area);area.select();
        success = document.execCommand('copy');area.remove();
      }
      status.textContent = success ? '已复制全文' : '浏览器未允许复制；可下载原文件。';
    } else if (target.dataset.save) {
      const blob = new Blob([textRecords.get(target.dataset.save)],{type:'text/plain;charset=utf-8'}), anchor = document.createElement('a');
      const objectUrl = URL.createObjectURL(blob);anchor.href = objectUrl;anchor.download = 'workflow-record.json';anchor.click();setTimeout(() => URL.revokeObjectURL(objectUrl),1000);
    }
  });
  root.addEventListener('keydown',event => {
    const target = event.target.closest('g[data-open-node]');
    if (target && ['Enter',' '].includes(event.key)) {event.preventDefault();select(target.dataset.openNode,true,'prompt');}
  });
  $('#wf-example').addEventListener('change',event => {exampleIndex = Number(event.target.value);detail();});
  $('#wf-case')?.addEventListener('change', event => {
    setCase(event.target.value);
    const version = data.dialogue_versions.find(v => v.dataset_case === activeCase);
    if (version) $$('[data-dialogue-version]').find(b => b.dataset.dialogueVersion === version.id)?.click();
  });
  $('#wf-search').addEventListener('input',filtered);
  $('#wf-dialogue').addEventListener('error',() => {$('#wf-audio-status').textContent = '音频未加载，可通过现有结果与音频报告查看原文件。';});
  $('#wf-trace-buttons').innerHTML = data.traces.map((t,i) => `<button data-trace="${i}" aria-pressed="${i===0}">${esc(t.turn_id)}</button>`).join('');
  taxonomyInit();visibility();if (data.traces.length) trace(0);failures();select(current.id);filtered();
  if (activeCase) root.dataset.selectedCase = activeCase;
  if (location.hash === '#taxonomy-library') mode('taxonomy');
  if (location.hash === '#stage3-output') select('stage3',true,'outputs');
  root.dataset.ready = 'true';
})();
