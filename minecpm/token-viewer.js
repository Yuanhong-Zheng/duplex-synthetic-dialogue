"use strict";
(() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const time = n => `${Math.floor(n / 60)}:${String(Math.floor(n % 60)).padStart(2,'0')}${n%1 ? '.'+String(Math.round((n%1)*1000)).padStart(3,'0').replace(/0+$/,'') : ''}`;
  const nativeEOS = data => ['minicpm_tail_turn_eos','minicpm_tail_delayed_eos','minicpm_tail_turn_relative'].includes(data?.protocol);
  const minecpm = data => ['minecpm_fixed16_tts_eos','minecpm_fixed16_tts_eos_post_chunk'].includes(data?.protocol);
  const postChunk = data => data?.protocol === 'minecpm_fixed16_tts_eos_post_chunk';
  const syntheticInput = (data, unit) => data?.mode !== 'native_inference' && (minecpm(data) || ['minicpm_tail_delayed_eos','minicpm_tail_turn_relative'].includes(data?.protocol)) && !!data.audio_inputs?.find(a => a.source?.block_index === unit && a.source?.synthetic && a.source?.kind === 'silence');
  const turnRelative = data => data?.mode !== 'native_inference' && data?.protocol === 'minicpm_tail_turn_relative';
  const timingLabel = row => `训练生成 unit ${row.generation_unit_index} · 原声学 unit ${row.source_unit_index} · 原声学对齐区间 [${(row.timeline_start_frame/16000).toFixed(3)}, ${(row.timeline_end_frame/16000).toFixed(3)}) 秒`;
  const actions = {151705:'listen',151706:'speak',151707:'interrupt',151717:'turn_eos'};
  class TrainingTokenView {
    unitSeconds() {
      return (this.data?.config?.unit_duration_ms ?? this.data?.unit_duration_ms ?? 1000) / 1000;
    }

    constructor(host, {seek, onUnitSelect, returnedAudio = false} = {}) {
      this.host = host; this.seek = seek; this.onUnitSelect = onUnitSelect; this.request = 0; this.playGeneration = 0;
      this.returnedAudioEnabled = returnedAudio; this.playingReturnedUnit = null;
      host.classList.add('tv');
      host.innerHTML = `<p class="tv-status" aria-live="polite">选择案例后加载真实序列。</p><div class="tv-toolbar"><label>阅读方式 <select class="tv-mode"><option value="units">按 unit 标注</option><option value="continuous">完整连续序列</option><option value="raw">原始 ID / label 数组</option></select></label><label>跳到 <select class="tv-jump"><option>unit</option></select></label><button type="button" class="tv-interrupt">下一个 interrupt</button><button type="button" class="tv-stop-audio">停止片段</button><a class="tv-download" download>下载完整 token 数据</a></div><p class="tv-note"></p><div class="tv-legend"><span class="tv-audio">音频特征位</span><span class="tv-context">输入文字 / 提示</span><span class="tv-special">结构 / 控制 token</span><span class="tv-target tv-label">助手文字 / label</span><span>绿色下边框 = LLM loss 生效</span></div><div class="tv-inspector" aria-live="polite">点击任意 token，查看真实位置、ID 与监督标注。</div><div class="tv-content"></div><div class="tv-talker"></div>`;
      this.$ = s => host.querySelector(s);
      this.$('.tv-mode').addEventListener('change', () => this.render());
      this.$('.tv-stop-audio').addEventListener('click', () => this.stopAudio());
      this.$('.tv-jump').addEventListener('change', e => this.jump(Number(e.target.value)));
      this.$('.tv-interrupt').addEventListener('click', () => {
        if (!this.data) return;
        const marked = this.interruptionUnits();
        const units = this.data.units.filter(u => marked.includes(u.unit));
        const next = units.find(u => u.unit > (this.lastJump ?? -1)) || units[0];
        if (next) this.jump(next.unit);
      });
      host.addEventListener('click', e => {
        const token = e.target.closest('[data-token-position]');
        if (token) {
          const position = Number(token.dataset.tokenPosition);
          this.inspect(position);
          const unit = this.data?.unit_ids[position];
          if (unit >= 0) {
            const timing = this.generationRecord(position);
            this.onUnitSelect?.(unit,timing ? {position,generationUnit:timing.generation_unit_index,sourceUnit:timing.source_unit_index,sourceSeconds:timing.timeline_start_frame/16000} : undefined);
          }
        }
        const play = e.target.closest('[data-seek]');
        if (play && this.seek) this.seek(Number(play.dataset.seek));
        const unitAudio = e.target.closest('[data-unit-audio]');
        if (unitAudio) this.playAudio(Number(unitAudio.dataset.unitAudio));
        const returnedAudio = e.target.closest('[data-unit-returned-audio]');
        if (returnedAudio && !returnedAudio.disabled) this.playAudio(Number(returnedAudio.dataset.unitReturnedAudio), {returned:true});
      });
      document.addEventListener('play', e => { if (e.target instanceof HTMLMediaElement) this.stopAudio(); }, true);
    }
    async load(url, audioUrl, transcriptUrl, returnedAudioUrl, resourceBase) {
      this.stopAudio(); this.resourceBase = new URL(resourceBase || './', document.baseURI || window.location.href);
      this.audioUrl = audioUrl; this.audioHeader = null; this.transcripts = null;
      this.returnedAudio = null; this.returnedAudioHeaders = new Map();
      const request = ++this.request; this.data = null; this.textGenerationByPosition = new Map(); this.textGenerationByUnit = new Map(); this.lastJump = -1; this.playingUnit = null; this.runtimeProgress = null; this.runtimeFocus = null;
      this.$('.tv-status').textContent = '正在读取完整 token 序列…';
      this.$('.tv-content').replaceChildren(); this.$('.tv-talker').replaceChildren();
      this.$('.tv-inspector').textContent = '点击任意 token，查看真实位置、ID 与监督标注。';
      try {
        const transcriptFetch = transcriptUrl ? fetch(transcriptUrl, {cache:'no-cache'}).then(r => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json();
        }).catch(e => ({units:{}, notice:`音频文本加载失败：${e.message}`})) : Promise.resolve(null);
        const returnedFetch = this.returnedAudioEnabled && returnedAudioUrl ? fetch(returnedAudioUrl, {cache:'no-cache'}).then(r => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json();
        }).catch(e => ({units:{},notice:`返回音频块索引加载失败：${e.message}`})) : Promise.resolve(null);
        const response = await fetch(url, {cache:'no-cache'}); if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const [data, transcripts, returnedAudio] = await Promise.all([response.json(), transcriptFetch, returnedFetch]); if (request !== this.request) return;
        if (transcripts?.sample_id && transcripts.sample_id !== data.sample_id) throw new Error('音频文本与当前样本不匹配');
        if (returnedAudio?.sample_id && returnedAudio.sample_id !== data.sample_id) throw new Error('返回音频块与当前样本不匹配');
        if (data.provenance?.runtime_compatibility_report_url) data.provenance.runtime_compatibility_report_url = new URL(data.provenance.runtime_compatibility_report_url,this.resourceBase).href;
        for (const note of data.runtime_compatibility_notes || []) if (note.report_url) note.report_url = new URL(note.report_url,this.resourceBase).href;
        if (returnedAudio?.units) for (const item of Object.values(returnedAudio.units)) if (item.url) item.url = new URL(item.url,this.resourceBase).href;
        this.data = data;
        if (turnRelative(data)) for (const row of data.text_generation_records || []) {
          this.textGenerationByPosition.set(row.position,row);
          if (!this.textGenerationByUnit.has(row.generation_unit_index)) this.textGenerationByUnit.set(row.generation_unit_index,[]);
          this.textGenerationByUnit.get(row.generation_unit_index).push(row);
        }
        this.transcripts = transcripts;
        this.returnedAudio = returnedAudio;
        this.$('.tv-download').href = url;
        this.$('.tv-download').download = `${data.key}.tokens.json`;
        const c = data.counts, w = data.config;
        this.$('.tv-status').textContent = `${data.sample_id} · ${data.mode === 'native_inference' ? `${data.inference_model || 'MiniCPM-o 4.5'} 实际推理` : `${data.format_label ? data.format_label + ' · ' : ''}${data.stage === 'thinker' ? 'Thinker' : 'Talker'}`} · ${c.tokens.toLocaleString()} 个位置 · ${c.units} 个 unit · 完整记录`;
        this.statusBase = this.$('.tv-status').textContent;
        const native = data.mode === 'native_inference';
        this.$('.tv-legend').innerHTML = `<span class="tv-audio">音频特征位</span><span class="tv-context">输入文字 / 提示</span><span class="tv-special">结构 / 控制 token</span>${native ? '<span class="tv-target">助手生成文字</span>' : '<span class="tv-target tv-label">助手文字 / label</span><span>绿色下边框 = LLM loss 生效</span>'}`;
        if (data.token_playback_status?.some(s => s === 'unplayed' || s === 'partial')) this.$('.tv-legend').insertAdjacentHTML('beforeend','<span class="tv-unplayed-key">虚线上边框 = 未播放或仅播放部分，仍参与训练</span>');
        this.$('.tv-mode option[value="raw"]').textContent = native ? '原始 ID 数组' : '原始 ID / label 数组';
        this.$('.tv-inspector').textContent = native ? '点击任意 token，查看实际 ID 与输入、生成或边界来源。' : '点击任意 token，查看真实位置、ID 与监督标注。';
        this.$('.tv-note').textContent = native ? '直接记录本次实际推理送入解码器的顺序。音频特征没有词表 ID，以 null 表示。生成 token 与运行时插入的边界分别标注，无训练 labels。' : `直接由实际训练 collator 导出。${this.unitSeconds()===1 ? '每秒音频占 10 个特征位' : `每个 ${this.unitSeconds()*1000}ms unit 占 ${w.audio_features_per_unit} 个音频特征位`}，input_ids 中占位 ID 为 0，模型嵌入随后由音频编码特征替换；“音频位”是页面注释，不是新增 token。点击带 ▶ 的第一个音频位，播放该 unit 的 ${this.unitSeconds()} 秒麦克风输入；其他音频位可查看 token 信息。${transcripts ? transcripts.notice : ''} 标签 -100 表示不预测；labels 未做因果移位，由 loss 内部移位。当前 text loss=${w.text_loss_weight}，audio loss=${w.audio_loss_weight}。`;
        if (turnRelative(data)) this.$('.tv-note').textContent += ' V1.3 正文按轮内完整 1 秒窗口安排生成；生成 unit 与原声学 unit 分开标注。麦克风输入仍是全局 [unit, unit+1) 秒，正文可提前一格生成，不代表已播放；点击正文 token 定位原声学起点，unit 定位按钮仍定位全局输入桶。';
        if (this.returnedAudioEnabled) this.$('.tv-note').textContent += native
          ? ' 标题旁可试听该 unit 的原始返回音频块，内容可能延续先前生成的文字；收听返回的静音也原样保留。'+(returnedAudio?.notice || '')
          : ' 本训练视图没有保存模型逐 unit 返回的独立输出音频，因此输出试听按钮置灰；麦克风输入仍可点击音频位试听。';
        this.$('.tv-jump').innerHTML = data.units.map(u => `<option value="${u.unit}">${u.unit < 0 ? '系统 / 保护前缀' : native ? `unit ${u.unit}` : `unit ${u.unit} · ${time(u.unit*this.unitSeconds())}`}</option>`).join('');
        this.$('.tv-interrupt').textContent = minecpm(data) ? '下一个打断 · LISTEN' : nativeEOS(data) ? '下一个打断 · turn_eos' : data.training_format === 'minicpm' && data.protocol !== 'minicpm_tail_interrupt' ? '下一个源打断' : '下一个 interrupt';
        this.$('.tv-interrupt').disabled = !this.interruptionUnits().length;
        this.render(); this.renderTalker();
      } catch (e) { if (request === this.request) this.$('.tv-status').textContent = `序列加载失败：${e.message}`; }
    }
    interruptionUnits() {
      if (!this.data) return [];
      if (Array.isArray(this.data.interrupt_units)) return this.data.interrupt_units;
      if (nativeEOS(this.data)) return [...new Set((this.data.generation_close_records || []).filter(r => r.is_interrupted).map(r => r.unit_index))];
      return this.data.units.filter(u => this.data.input_ids.slice(u.start,u.end).includes(151707)).map(u => u.unit);
    }
    generationRecord(position) {
      return turnRelative(this.data) ? this.textGenerationByPosition?.get(position) : null;
    }
    token(p) {
      const d = this.data, kind = d.kinds[p], id = d.input_ids[p];
      const feature = kind === 'audio' || kind === 'image';
      const firstAudio = kind === 'audio' && d.unit_ids[p] >= 0 && (p === 0 || d.kinds[p-1] !== 'audio' || d.unit_ids[p-1] !== d.unit_ids[p]);
      const synthetic = firstAudio && syntheticInput(d,d.unit_ids[p]);
      let text = feature ? (kind === 'audio' ? (firstAudio ? (synthetic ? '▶ 补充静音' : '▶ 音频位') : '音频位') : '图像位') : d.token_text[p];
      if (!feature && text.includes('\uFFFD')) text = d.token_pieces[p];
      if (text === ' ') text = '␠'; if (text === '\n') text = '↵';
      const label = d.labels[p] !== -100 && d.config.text_loss_weight > 0;
      const timing = this.generationRecord(p);
      const playback = d.token_playback_status?.[p];
      const playbackLabel = playback === 'unplayed' ? '纳入训练，但原音频未播放' : playback === 'partial' ? '纳入训练，原音频只播放了此 token 的一部分' : playback === 'played' ? '纳入训练，原音频已播放' : '';
      const unit = d.unit_ids[p], silence = firstAudio && d.audio_inputs?.find(a => a.source?.block_index === unit)?.source?.kind === 'silence';
      const play = firstAudio ? ` data-unit-audio="${unit}" aria-label="播放 unit ${unit} 的 ${this.unitSeconds()} 秒输入音频${synthetic ? '，新增的合成静音输入，不是原录音' : silence ? '，静音' : ''}"` : '';
      return `<button type="button" class="tv-token tv-${kind}${firstAudio ? ' tv-audio-play' : ''}${label ? ' tv-label' : ''}${['unplayed','partial'].includes(playback) ? ' tv-unplayed' : ''}" data-token-position="${p}"${play} title="位置 ${p} · ${feature && d.mode === 'native_inference' ? '音频特征，无词表 ID' : 'ID '+id}${d.mode === 'native_inference' ? ' · 来源 '+(d.token_origins?.[p] || '') : ' · label '+d.labels[p]}${timing ? ' · '+escape(timingLabel(timing)) : ''}${playbackLabel ? ' · '+playbackLabel : ''}${firstAudio ? (synthetic ? ` · 点击试听新增的 ${this.unitSeconds()} 秒静音输入；原录音不含此 unit` : ` · 点击播放本 unit 的 ${this.unitSeconds()} 秒输入音频`) : ''}">${escape(text)}</button>`;
    }
    tokensBetween(start, end) {
      const parts = [];
      for (let p = start; p < end; p++) {
        parts.push(this.token(p));
        if (this.data.kinds[p] === 'audio' && (p + 1 === end || this.data.kinds[p + 1] !== 'audio')) {
          const entries = this.transcripts?.units?.[this.data.unit_ids[p]] || [];
          if (entries.length) parts.push(`<div class="tv-audio-caption"><span>音频文本 · 仅供阅读：</span>${entries.map(e => `<span class="tv-audio-utterance"><b>${escape(e.label)}</b> ${escape(e.text)}</span>`).join('')}</div>`);
        }
      }
      return parts.join('');
    }
    render() {
      if (!this.data) return;
      const d = this.data, mode = this.$('.tv-mode').value;
      this.runtimeFocus = null;
      const content = this.$('.tv-content');
      if (mode === 'raw') {
        const pre = document.createElement('pre');
        const end=this.visibleTokenEnd();
        pre.textContent = JSON.stringify({input_ids:d.input_ids.slice(0,end), ...(d.mode === 'native_inference' ? {label_semantics:'实际推理，没有训练 labels；null 输入位置为真实音频 embedding，无词表 ID', visible_positions:end,total_positions:d.input_ids.length} : {labels:d.labels}), position_ids:d.position_ids.slice(0,end), unit_ids:d.unit_ids.slice(0,end), audio_bounds:d.audio_bounds.filter(b=>b[0]<end).map(b=>[b[0],Math.min(b[1],end)]), image_bounds:d.image_bounds}, null, 2);
        content.replaceChildren(pre); this.applyRuntime(); return;
      }
      if (mode === 'continuous') {
        content.innerHTML = `<div class="tv-continuous">${this.tokensBetween(0,d.input_ids.length)}</div>`; this.applyRuntime(); return;
      }
      content.innerHTML = d.units.map(u => {
        const prefix = u.unit < 0;
        const body = this.tokensBetween(u.start,u.end);
        const action = d.input_ids.slice(u.start,u.end).map(id => actions[id] || (d.mode === 'native_inference' && id === 151703 ? 'tts_bos' : null)).filter(Boolean).join(' / ') || (d.mode === 'native_inference' ? '原始模型输出' : 'tool / context');
        const heading = prefix ? `系统 / 保护前缀 · ${u.end-u.start} tokens` : d.mode === 'native_inference' ? `unit ${u.unit} · 输入区间 [${u.unit}, ${u.unit+1})u <span class="tv-unit-action">· ${action}</span> <span class="tv-unit-state"></span>` : turnRelative(d) ? `训练生成 unit ${u.unit} · 全局麦克风输入 [${u.unit}, ${u.unit+1}) 秒 · ${action}` : `unit ${u.unit} · 输入 [${u.unit*this.unitSeconds()}, ${(u.unit+1)*this.unitSeconds()}) 秒 · ${action}`;
        const bounds = `位置 ${u.start}–${u.end-1}`;
        if (prefix) return `<details class="tv-prefix" data-tv-unit="${u.unit}"><summary>${heading} <small>${bounds} · 点击展开全部</small></summary><div class="tv-tokens">${body}</div><pre class="tv-decoded">${escape(u.decoded)}</pre></details>`;
        const returned = this.returnedAudio?.units?.[String(u.unit)], hasReturned = returned?.status === 'available' && returned.sample_count > 0;
        const returnedButton = this.returnedAudioEnabled ? `<button type="button" class="tv-returned-audio" data-unit-returned-audio="${u.unit}" aria-pressed="false" title="${hasReturned ? `原始返回 PCM · ${returned.duration_s.toFixed(3)} 秒${returned.is_listen ? ' · 收听时返回的静音' : ''}；可能包含整段录音未播出的部分` : '该 unit 未保存独立返回音频块'}"${hasReturned ? '' : ' disabled'}>${hasReturned ? '▶ 试听返回音频块' : '无独立输出块'}</button>` : '';
        const actualTime = d.mode === 'native_inference' && u.output_available_wall_s != null ? `<div class="tv-native-time">实际处理区间：${u.call_start_s?.toFixed(2) ?? '—'}–${u.output_available_wall_s.toFixed(2)}u${u.playback_start_s != null ? ` · 声音开始播放：${u.playback_start_s.toFixed(2)}u` : ''}（均为录音时间轴位置）</div>` : '';
        const compatibility = (d.runtime_compatibility_notes || d.runtime_warnings || []).filter(note => note.unit_index === u.unit).map(note => `<p class="tv-compatibility-warning">保留论文 TAIL 分块：${note.lexical_tokens} 个正文 token、${note.decoded_characters} 个字符；这一块超过官方 Demo 的 28 字符保护，按已确认方案保留并标注。</p>`).join('');
        const closure = nativeEOS(d) ? (d.generation_close_records || []).find(r => r.unit_index === u.unit) : null;
        const closeNote = closure ? `<p class="tv-note">${closure.is_interrupted ? '用户打断 · 提前结束旧轮' : ['minicpm_tail_delayed_eos','minicpm_tail_turn_relative'].includes(d.protocol) ? '正常结束话轮 · 最后正文的下一空正文 unit' : '正常结束话轮'}：<code>&lt;|turn_eos|&gt;</code> 结束生成并收尾，已有待播语音继续播放。</p>` : '';
        const minecpmNote = !minecpm(d) ? '' : this.interruptionUnits().includes(u.unit)
          ? `<p class="tv-note">真正打断：直接监督 LISTEN；本 unit 不生成旧正文，也不插入 tts_eos。运行时应取消旧 TTS 与待播音频；这里是所在${this.unitSeconds()===1?'一秒':this.unitSeconds()+'秒'} unit 的训练标签。</p>`
          : d.input_ids.slice(u.start,u.end).includes(151704)
          ? postChunk(d)
            ? '<p class="tv-note">播放完成反馈：本 unit 保持 SPEAK；外部播放端在 chunk_eos 后、unit 结束前插入 tts_eos（label=-100），下一 unit 监督 LISTEN。</p>'
            : '<p class="tv-note">播放完成反馈：tts_eos 由外部播放端插入，label=-100；随后监督 LISTEN。此标记不由 Thinker 预测。</p>'
          : postChunk(d) && d.minecpm_external_inputs?.some(event=>event.listen_unit===u.unit)
          ? '<p class="tv-note">正常转入 LISTEN：上一 unit 的 chunk_eos 后已收到外部 tts_eos；本 unit 的首个预测 token 为 LISTEN。</p>'
          : '';
        const wholeTurnShiftNote = turnRelative(d) ? (d.generation_unit_shift_records || []).filter(record=>record.target_unit_index===u.unit).map(record=>`<p class="tv-generation-timing">${escape(record.assistant_id)}：为避开前轮结束桶，本轮全部正文窗口顺延 1 unit；原录音与打断停声不变。</p>`).join('') : '';
        const generationRecords = turnRelative(d) ? this.textGenerationByUnit?.get(u.unit) || [] : [];
        const generationNote = turnRelative(d) && Array.isArray(d.text_generation_records) && action.includes('speak')
          ? generationRecords.length ? `<p class="tv-generation-timing">正文生成 unit ${u.unit} · 对应原声学 unit ${[...new Set(generationRecords.map(r=>r.source_unit_index))].join('、')}；原声学对齐范围 [${(Math.min(...generationRecords.map(r=>r.timeline_start_frame))/16000).toFixed(3)}, ${(Math.max(...generationRecords.map(r=>r.timeline_end_frame))/16000).toFixed(3)}) 秒。生成桶不表示这些正文已在此时被听到；逐 token 位置可点击查看。</p>` : '<p class="tv-generation-timing">此训练生成 unit 没有正文 token，保留空 SPEAK 与原有控制监督；麦克风输入仍按全局时间读取。</p>'
          : '';
        const syntheticNote = syntheticInput(d,u.unit) ? `<p class="tv-synthetic-note">新增静音输入 · synthetic：原录音已结束，本 unit 的麦克风输入为明确补充的 ${this.unitSeconds()} 秒零波形，用于独立 EOS；原音频文件未修改。</p>` : '';
        const context = d.mode === 'native_inference' ? '此处保留实际推理顺序；音频特征位没有词表 ID。文字产生时间与音频开始播放时间分别记录。' : d.config.sliding_window_training === 'off' && !d.config.kv_delete_mask ? `音频 / 图像特征位不解码为文字。本配置使用完整因果上下文，可关注样本开头至 unit ${u.unit} 的历史及系统前缀；同一 unit 内仍遵守 token 因果顺序。` : `音频 / 图像特征位不解码为文字。本 unit 可直接关注 unit ${Math.max(0,u.unit-d.config.context_max_units)}–${u.unit} 及保护前缀；同一 unit 内仍遵守 token 因果顺序。`;
        return `<article class="tv-unit" data-tv-unit="${u.unit}"><header><strong>${heading}</strong><small>${bounds} ${returnedButton} ${this.seek ? `<button type="button" data-seek="${u.unit*this.unitSeconds()}">定位播放器</button>` : ''}</small></header>${actualTime}${compatibility}${closeNote}${minecpmNote}${wholeTurnShiftNote}${generationNote}${syntheticNote}<div class="tv-tokens">${body}</div><details class="tv-decoding"><summary>整段文字解码与上下文范围</summary><pre>${escape(u.decoded)}</pre><p>${context}</p></details></article>`;
      }).join('');
      this.syncReturnedAudioButtons();
      this.applyRuntime();
    }
    isLive() { return this.data?.mode === 'native_inference' && this.liveProgress && Array.isArray(this.data.token_available_s); }
    visibleTokenEnd() {
      if(!this.isLive()) return this.data.input_ids.length;
      const time=this.runtimeProgress?.time ?? 0;
      const first=this.data.token_available_s.findIndex(t=>t>time);
      return first<0?this.data.input_ids.length:first;
    }
    setLiveProgress(enabled) {
      this.liveProgress=enabled;
      if(this.data) { this.render();this.renderTalker();this.applyRuntime(); }
    }
    setRuntimeProgress(time,state,follow=false) {
      this.runtimeProgress={time,state};
      if(!this.data || this.data.mode!=='native_inference')return;
      if(this.$('.tv-mode').value==='raw')this.render();
      this.applyRuntime(follow);
    }
    applyRuntime(follow=false) {
      if(!this.data)return;
      if(this.data.mode!=='native_inference') {this.host.classList.remove('tv-native-live');return;}
      const live=this.isLive(), {time=0,state=null}=this.runtimeProgress || {};
      const end=this.visibleTokenEnd();
      this.host.classList.toggle('tv-native-live',live);
      for(const token of this.host.querySelectorAll('[data-token-position]')) token.hidden=live && Number(token.dataset.tokenPosition)>=end;
      for(const codes of this.host.querySelectorAll('[data-speech-ready]')) codes.hidden=live && Number(codes.dataset.speechReady)>time;
      for(const unit of this.data.units) {
        if(unit.unit<0)continue;
        const node=this.host.querySelector(`[data-tv-unit="${unit.unit}"]`);if(!node)continue;
        const processing=state?.processing?.unit===unit.unit;
        const completed=unit.output_available_wall_s<=time;
        const waiting=state?.waiting.some(s=>s.unit===unit.unit);
        const capturing=state?.captureUnit===unit.unit;
        node.classList.toggle('tv-processing',processing);
        node.classList.toggle('tv-waiting',!!waiting);
        node.classList.remove('tv-playing');
        const badge=node.querySelector('.tv-unit-state');
        if(badge)badge.textContent=processing ? '正在处理 · '+(state.phase?.label || '') : completed ? '已完成处理' : waiting ? '输入已收完 · 排队中' : capturing ? '正在收音 · 尚未处理' : '尚未处理';
        const action=node.querySelector('.tv-unit-action');
        if(action)action.hidden=live && time<(unit.action_ready_s ?? unit.output_available_wall_s);
        const timing=node.querySelector('.tv-native-time');if(timing)timing.hidden=live && !completed;
      }
      if(this.statusBase)this.$('.tv-status').textContent=this.statusBase+(live?` · 当前已进入模型 ${end} 个位置`:'');
      const target=state?.processing?.unit ?? state?.completed?.unit;
      if(follow && target!=null && target!==this.runtimeFocus) {
        this.runtimeFocus=target;this.$('.tv-jump').value=String(target);
        const node=this.host.querySelector(`[data-tv-unit="${target}"]`);
        if(node)this.$('.tv-content').scrollTo({top:node.offsetTop-12,behavior:'smooth'});
      }
    }
    syncReturnedAudioButtons() {
      this.host.querySelectorAll('[data-unit-returned-audio]').forEach(button => {
        const active = Number(button.dataset.unitReturnedAudio) === this.playingReturnedUnit;
        button.textContent = button.disabled ? '无独立输出块' : active ? this.audioSource ? '■ 停止试听' : '加载中…' : '▶ 试听返回音频块';
        button.setAttribute('aria-pressed', String(active));
      });
    }
    stopAudio() {
      const stoppedUnit = this.playingReturnedUnit;
      this.playGeneration++;
      this.audioAbort?.abort(); this.audioAbort = null;
      if (this.audioSource) {
        this.audioSource.onended = null;
        try { this.audioSource.stop(); } catch (_) {}
        this.audioSource.disconnect(); this.audioSource = null;
      }
      this.playingReturnedUnit = null; this.syncReturnedAudioButtons();
      if (stoppedUnit != null) this.$('.tv-inspector').textContent = `unit ${stoppedUnit} 返回音频块试听已停止。`;
    }
    async playAudio(unit, {returned = false} = {}) {
      const entry = returned ? this.returnedAudio?.units?.[String(unit)] : null;
      const synthetic = !returned && syntheticInput(this.data,unit);
      const url = returned ? entry?.url : this.audioUrl;
      if (!url || unit < 0 || (returned && !(entry.sample_count > 0))) return;
      if (returned && this.playingReturnedUnit === unit) { this.stopAudio(); return; }
      this.stopAudio();
      const generation = this.playGeneration;
      this.audioAbort = new AbortController();
      const signal = this.audioAbort.signal;
      if (returned) { this.playingReturnedUnit = unit; this.syncReturnedAudioButtons(); }
      document.dispatchEvent(new CustomEvent('training-token-audio-start',{detail:{unit,returned,synthetic}}));
      document.querySelectorAll('audio').forEach(audio => audio.pause());
      this.$('.tv-inspector').textContent = returned ? `正在加载 unit ${unit} 的原始返回音频块…` : synthetic ? `正在准备 unit ${unit} 的补充静音输入（synthetic）…` : `正在加载 unit ${unit} 的 ${this.unitSeconds()} 秒麦克风输入…`;
      try {
        const Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) throw new Error('浏览器不支持 Web Audio');
        this.audioContext ||= new Context();
        await this.audioContext.resume();
        if (generation !== this.playGeneration) return;
        let header = synthetic ? {format:1,channels:1,rate:16000,bits:16,offset:0,size:0} : returned ? this.returnedAudioHeaders.get(url) : this.audioHeader;
        if (!header) {
          const response = await fetch(url, {headers:{Range:'bytes=0-65535'}, signal});
          if (response.status !== 206) throw new Error('音频服务不支持范围读取');
          const bytes = await response.arrayBuffer(), v = new DataView(bytes);
          const four = p => String.fromCharCode(...new Uint8Array(bytes,p,4));
          if (four(0) !== 'RIFF' || four(8) !== 'WAVE') throw new Error('不是 WAV 文件');
          let pos = 12, fmt, data;
          while (pos + 8 <= bytes.byteLength) {
            const name = four(pos), size = v.getUint32(pos + 4,true), begin = pos + 8;
            if (name === 'fmt ') {
              if (begin + 16 > bytes.byteLength) throw new Error('WAV 格式头不完整');
              fmt = {format:v.getUint16(begin,true), channels:v.getUint16(begin+2,true),
                     rate:v.getUint32(begin+4,true), bits:v.getUint16(begin+14,true)};
            }
            if (name === 'data') { data = {offset:begin, size}; break; }
            pos = begin + size + size % 2;
          }
          if (!fmt || !data || fmt.channels !== 1 || fmt.rate !== (returned ? entry.sample_rate_hz : 16000) ||
              !((fmt.format === 1 && fmt.bits === 16) || (fmt.format === 3 && fmt.bits === 32)))
            throw new Error('音频采样率或 PCM 格式与记录不符');
          if (generation !== this.playGeneration) return;
          header = {...fmt,...data};
          if (returned) this.returnedAudioHeaders.set(url, header); else this.audioHeader = header;
        }
        const h = header, bytesPerSample = h.bits / 8, frames = returned ? entry.sample_count : Math.round(16000*this.unitSeconds());
        const offset = returned ? 0 : unit * frames * bytesPerSample;
        const available = Math.max(0, Math.min(frames * bytesPerSample, h.size - offset));
        if (returned && available !== frames * bytesPerSample) throw new Error('返回音频块的采样数与记录不符');
        let payload = new ArrayBuffer(0);
        if (available) {
          const start = h.offset + offset, end = start + available - 1;
          const response = await fetch(url, {headers:{Range:`bytes=${start}-${end}`}, signal});
          if (response.status !== 206) throw new Error('音频服务不支持范围读取');
          payload = await response.arrayBuffer();
          if (payload.byteLength !== available) throw new Error('音频分段长度不符');
        }
        if (generation !== this.playGeneration) return;
        const buffer = this.audioContext.createBuffer(1,frames,h.rate), channel = buffer.getChannelData(0), pcm = new DataView(payload);
        for (let i = 0; i < available / bytesPerSample; i++)
          channel[i] = h.format === 1 ? pcm.getInt16(i * 2,true) / 32768 : pcm.getFloat32(i * 4,true);
        const source = this.audioContext.createBufferSource(); source.buffer = buffer;
        source.connect(this.audioContext.destination); this.audioSource = source;
        this.$('.tv-inspector').textContent = returned ? `正在试听 unit ${unit} 的原始返回音频块，${(frames/h.rate).toFixed(3)} 秒；音频内容与本次新增正文不保证逐段对应。` : synthetic ? `正在试听 unit ${unit} 的 ${this.unitSeconds()} 秒补充静音输入（synthetic）；此 unit 不在原录音内。` : `正在播放 unit ${unit} 的麦克风输入 [${unit*this.unitSeconds()}, ${(unit+1)*this.unitSeconds()}) 秒；播放 ${this.unitSeconds()} 秒后自动停止。`;
        source.onended = () => {
          if (generation === this.playGeneration) {
            source.disconnect(); this.audioSource = null; this.playingReturnedUnit = null; this.syncReturnedAudioButtons();
            this.$('.tv-inspector').textContent = `unit ${unit} ${returned ? '返回音频块试听' : synthetic ? '补充静音输入试听' : '输入音频播放'}完毕。`;
          }
        };
        source.start(); this.syncReturnedAudioButtons();
      } catch (e) {
        if (generation === this.playGeneration && e.name !== 'AbortError') {
          this.stopAudio(); this.$('.tv-inspector').textContent = `无法播放 unit ${unit}：${e.message}`;
        }
      }
    }
    inspect(p) {
      const d = this.data, feature = ['audio','image'].includes(d.kinds[p]);
      const active = d.labels[p] !== -100 && d.config.text_loss_weight > 0;
      this.$('.tv-inspector').textContent = d.mode === 'native_inference'
        ? `位置 ${p} · 输入 unit ${d.unit_ids[p]} · ${feature ? '直接输入的音频特征，没有词表 token ID' : `token ID ${d.input_ids[p]} · ${JSON.stringify(d.token_text[p])}`} · ${d.inference_model || 'MiniCPM-o 4.5'} 实际推理记录，无训练 label${d.token_origins?.[p] ? ' · 来源：'+d.token_origins[p] : ''}`
        : `位置 ${p} · unit ${d.unit_ids[p]} · token ID ${d.input_ids[p]} · label ${d.labels[p]} · ${active ? '参与 LLM 文本 / 控制 loss' : d.labels[p] === -100 ? '不参与 LLM loss（mask）' : '保留 label，但此训练阶段 text loss 权重为 0'} · ${feature ? '模态特征替换位；此 ID 不作为可读文字送入' : `词表片段 ${JSON.stringify(d.token_pieces[p])}；单 token 解码 ${JSON.stringify(d.token_text[p])}`}`;
      const timing = this.generationRecord(p);
      if (timing) this.$('.tv-inspector').textContent += ` · ${timingLabel(timing)}；源时间戳未改动，生成 unit 不是播放时间`;
      const closure = nativeEOS(d) ? (d.generation_close_records || []).find(r => r.token_position === p) : null;
      if (closure) this.$('.tv-inspector').textContent += ` · ${closure.is_interrupted ? '被打断话轮提前结束' : '话轮正常结束'}：原生 turn_eos；不取消已排队语音`;
      if (minecpm(d) && d.input_ids[p] === 151704) this.$('.tv-inspector').textContent += ' · 外部 TTS 播放完成反馈；输入位置，不预测该 token';
      const playback = d.token_playback_status?.[p];
      if (playback) this.$('.tv-inspector').textContent += playback === 'unplayed' ? ' · 训练保留，实际未播放' : playback === 'partial' ? ' · 训练保留，实际仅播放部分；整个 BPE token 仍作为一个监督目标' : ' · 实际已播放';
      this.host.querySelector('.tv-selected')?.classList.remove('tv-selected');
      this.host.querySelector(`[data-token-position="${p}"]`)?.classList.add('tv-selected');
    }
    jump(unit, {notify = true} = {}) {
      this.lastJump = unit; this.$('.tv-jump').value = String(unit);
      const target = this.host.querySelector(`[data-tv-unit="${unit}"]`);
      if (target) { if (target.tagName === 'DETAILS') target.open = true; target.scrollIntoView({behavior:'smooth',block:'center'}); if (notify && unit >= 0) this.onUnitSelect?.(unit); }
      else if (this.$('.tv-mode').value !== 'units') { this.$('.tv-mode').value = 'units'; this.render(); this.jump(unit, {notify}); }
    }
    setTime(seconds, follow = false) {
      const unit = Math.floor(seconds/this.unitSeconds()); if (this.playingUnit === unit) return;
      this.playingUnit = unit;
      this.host.querySelector('.tv-playing')?.classList.remove('tv-playing');
      const node = this.host.querySelector(`[data-tv-unit="${unit}"]`); node?.classList.add('tv-playing');
      if (follow && node) this.$('.tv-content').scrollTo({top:node.offsetTop-12,behavior:'smooth'});
    }
    setOutputUnits(units, follow = false) {
      this.host.querySelectorAll('.tv-output-playing').forEach(node => node.classList.remove('tv-output-playing'));
      for (const unit of units) {
        const node = this.host.querySelector(`[data-tv-unit="${unit}"]`);
        node?.classList.add('tv-output-playing');
        if (follow && node) this.$('.tv-content').scrollTo({top:node.offsetTop-12,behavior:'smooth'});
      }
    }
    renderTalker() {
      const d = this.data, box = this.$('.tv-talker');
      if (d.mode === 'native_inference') {
        const visibility = this.isLive() ? '语音码在实际生成完成后显示；关闭逐步显示可查看完整记录。' : '完整展示本次已记录的语音码，处理状态随回放更新。';
        box.innerHTML = `<p>${escape(d.inference_model || 'MiniCPM-o 4.5')} 实际推理记录：没有训练 loss。${visibility}</p>${(d.native_audio_codes || []).map(s => `<details data-speech-ready="${s.available_wall_s}"><summary>unit ${s.unit} · 实际输出 ${s.codes.length} 个语音码</summary><pre>${escape(JSON.stringify(s.codes))}</pre></details>`).join('')}`;
        this.applyRuntime();
        return;
      }
      if (d.stage !== 'talker') { box.innerHTML = '<p>Thinker 只监督文本和控制。本样本没有启用 Talker 的 S3 loss。</p>'; return; }
      box.innerHTML = `<h3>Talker 解码器的完整序列</h3><p>这是独立的嵌入序列，S3 ID 不属于 LLM 词表。同一 turn_group 按 unit 顺序拼接、共享 Talker 上下文。text+hidden 表示文本嵌入加 Thinker 隐状态投影；audio_bos 使用文本嵌入表，s3 使用语音码嵌入表。这里的 label 已按代码的 next-code 方式构造，不再移位；-100 不计损失，6561 是语音 EOS 目标。</p>${d.talker_sequences.map(s => `<details><summary>turn_group ${s.turn_group} · unit ${s.unit} · ${s.embedding_ids.length} 个位置 · ${s.predict_audio_eos ? '预测语音 EOS' : '继续同一轮'}</summary><p>对应 LLM 条件位置：${escape(s.condition_positions.join(', '))}</p><table><thead><tr><th>片段内位置</th><th>嵌入类型</th><th>输入 ID</th><th>预测 label</th></tr></thead><tbody>${s.embedding_ids.map((id,p) => `<tr><td>${p}</td><td>${escape(s.embedding_kinds[p])}</td><td>${id}</td><td>${s.labels[p]}</td></tr>`).join('')}</tbody></table></details>`).join('')}`;
    }
  }
  window.TrainingTokenView = TrainingTokenView;
})();
