"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clock = seconds => `${Math.floor(seconds/60)}:${String(Math.floor(seconds)%60).padStart(2,'0')}`;
  const eventTitle = e => (['native_generation','native_interrupt'].includes(e.kind) ? `${e.label} · 录音轴 ${e.start.toFixed(2)} unit处（标记宽度仅用于显示） · ${e.text}` : `${e.label} · ${timeLabel(e.start)}–${timeLabel(e.end)} · ${e.text}`) + (e.shifts?.length ? ` · ${e.shifts.length} 处文本监督顺延 1u，原音频时序不变` : '');
  const mixRoot = '../duplex-env-scaling-v2-workflow-20261103/versions/v2_12_full_request_weighted_chinese_hundred/audio/';
  let cases = [], request = 0, loadedId = '', activePlayer = $('mixed-audio'), dialogueEvents = [], duration = 0;
  let runtime = null, runtimeState = null;
  let dialogueLoading = true;
  const pageBase = new URL('./', window.location.href);
  let dataVersion = 'V1.4';
  let dataVersions = [{id:'V1.4', label:'V1.4 · MineCPM', root:'data/'}];
  let versionRegistryPromise = null;
  let latestDataVersion = 'V1.4';
  let trainingTimeline = null, tailAnimation = 0;
  const nowSeconds = () => performance.now() / 1000;
  const isTurnRelative = () => isMiniTraining() && trainingProtocol === 'minicpm_tail_turn_relative';
  const isDelayedEOS = () => isMiniTraining() && ['minicpm_tail_delayed_eos','minicpm_tail_turn_relative'].includes(trainingProtocol);
  const isMineCPM = () => isMiniTraining() && trainingProtocol === 'minecpm_fixed16_tts_eos';
  const selectedVersion = () => dataVersions.find(v => v.id === dataVersion) || dataVersions[0];
  const datasetBase = () => isMiniTraining() ? new URL(selectedVersion().root, pageBase) : pageBase;
  const datasetURL = path => path ? new URL(path, datasetBase()).href : '';
  function resolveCaseURLs(info) {
    const value = {...info};
    for (const key of ['mixed_audio','input_audio','token_url','transcript_url','warmup_url','compatibility_notes_url','quantization_shifts_url','tts_trace_url'])
      if (value[key]) value[key] = datasetURL(value[key]);
    if (value.compatibility_notes) value.compatibility_notes = value.compatibility_notes.map(n=>({...n, report_url:datasetURL(n.report_url)}));
    return value;
  }
  async function loadDataVersions() {
    if (versionRegistryPromise) return versionRegistryPromise;
    versionRegistryPromise = (async () => {
    try {
      const response = await fetch(new URL('data-versions.json', pageBase), {cache:'no-cache'});
      if (response.status === 404) return;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const value = await response.json();
      if (!Array.isArray(value.versions) || !value.versions.length || new Set(value.versions.map(v=>v.id)).size !== value.versions.length ||
          value.versions.some(v=>!['V1.1','V1.2','V1.3','V1.4'].includes(v.id) || typeof v.root !== 'string' || new URL(v.root,pageBase).origin !== pageBase.origin))
        throw new Error('训练版本索引格式不正确');
      dataVersions = value.versions;
      latestDataVersion = dataVersions.some(v=>v.id===value.latest) ? value.latest : dataVersions[dataVersions.length-1].id;
    } catch (error) {
      versionRegistryPromise = null;
      throw new Error(`训练版本索引读取失败：${error.message}`);
    }
    })();
    return versionRegistryPromise;
  }
  function renderDataVersions() {
    $('data-version-picker').hidden = !isMiniTraining();
    $('data-version-note').hidden = !isMiniTraining();
    if (!isMiniTraining()) return;
    if (!dataVersions.some(v=>v.id===dataVersion)) dataVersion = latestDataVersion;
    $('data-version').innerHTML = dataVersions.map(v=>`<option value="${escape(v.id)}">${escape(v.label || v.id)}</option>`).join('');
    $('data-version').value = dataVersion;
    $('data-source').value = sourcePickerValue();
    $('data-version-note').textContent = dataVersion === 'V1.4' ? 'V1.4 · MineCPM：每秒生成 16 个正文 token，轮末允许不足；每个 SPEAK unit 保留 chunk_eos，不使用 turn_eos。文字结束后保留空 SPEAK，直到真实 TTS 播放完成，由外部插入不监督的 tts_eos，再进入 LISTEN；打断直接进入 LISTEN 并取消旧语音。' : dataVersion === 'V1.3' ? 'V1.3：正文按每轮首个保留 token 起点划完整 1 秒窗口；生成 unit 与原声学 unit 分开标注，麦克风、音频与源时间戳保持不变。' : dataVersion === 'V1.2' ? 'V1.2：正常结束在最后正文的下一 unit 独立输出 turn_eos；打断标注沿用 V1.1。' : 'V1.1：保留此前的转换数据；正常结束的 turn_eos 与最后正文位于同一 unit。';
  }
  function resetTrainingTimeline() {
    if (tailAnimation) cancelAnimationFrame(tailAnimation);
    tailAnimation = 0; trainingTimeline = null;
    $('training-timeline-controls').hidden = true;
  }
  function currentPlaybackTime() {
    if (trainingTimeline?.active) return trainingTimeline.playing ? Math.min(duration, trainingTimeline.anchorTime + (nowSeconds()-trainingTimeline.anchorWall)*trainingTimeline.rate) : trainingTimeline.time;
    const value = activePlayer.currentTime || 0;
    return trainingTimeline ? Math.min(value, trainingTimeline.originalEnd) : value;
  }
  function trainingUnitAt(seconds) {
    const unit = Math.floor(seconds);
    return trainingTimeline ? Math.min(Math.max(0,Math.ceil(duration)-1),unit) : unit;
  }
  function dialoguePlaying() {
    return trainingTimeline?.active ? trainingTimeline.playing : !$('mixed-audio').paused && !$('mixed-audio').ended;
  }
  function pauseTrainingTail() {
    if (!trainingTimeline?.active) return;
    trainingTimeline.time = currentPlaybackTime(); trainingTimeline.playing = false;
    if (tailAnimation) cancelAnimationFrame(tailAnimation);
    tailAnimation = 0;
  }
  function leaveTrainingTail() {
    pauseTrainingTail();
    if (trainingTimeline) trainingTimeline.active = false;
  }
  function enterTrainingTail(seconds, play) {
    if (!trainingTimeline) return;
    pauseTrainingTail();
    const state = trainingTimeline;
    state.active = true; state.time = Math.max(state.originalEnd, Math.min(duration, seconds));
    state.playing = !!play && state.time < duration;
    state.anchorTime = state.time; state.anchorWall = nowSeconds(); state.rate = Number(activePlayer.playbackRate) || 1;
    for (const player of [$('mixed-audio'),$('input-audio')]) player.pause();
    function advance() {
      if (trainingTimeline !== state || !state.active || !state.playing) return;
      state.time = currentPlaybackTime();
      if (state.time >= duration) { state.time = duration; state.playing = false; tailAnimation = 0; }
      drawPosition(state.time,$('follow-audio').checked); syncDialogueControls();
      if (state.playing) tailAnimation = requestAnimationFrame(advance);
    }
    drawPosition(state.time,$('follow-audio').checked); syncDialogueControls();
    if (state.playing) tailAnimation = requestAnimationFrame(advance);
  }
  function configureTrainingTimeline(info, sample = null) {
    const meta = sample?.meta || {};
    if (!isMiniTraining() || !['V1.2','V1.3','V1.4'].includes(dataVersion)) { resetTrainingTimeline(); return; }
    const sourceFrames = meta.original_source_frames ?? meta.source_frames;
    const originalEnd = Number(info.original_audio_seconds ?? (sourceFrames != null ? sourceFrames / 16000 : info.seconds));
    const total = Number(info.training_duration_seconds ?? info.training_total_units ?? meta.training_total_units ?? info.seconds);
    if (!Number.isFinite(originalEnd) || originalEnd < 0 || !Number.isFinite(total) || total < originalEnd) throw new Error('训练尾部时长元数据不正确');
    const appended = Number(info.appended_silence_units ?? meta.appended_silence_units ?? 0);
    const previous = trainingTimeline;
    if (previous && (previous.originalEnd !== originalEnd || previous.end !== total)) resetTrainingTimeline();
    trainingTimeline = previous && previous.originalEnd === originalEnd && previous.end === total ? previous :
      {originalEnd,end:total,appended,active:false,playing:false,time:0,rate:1,anchorTime:0,anchorWall:0};
    duration = total;
    $('training-timeline-controls').hidden = false;
    $('training-timeline-progress').max = String(total);
    const partial = Math.max(0, Math.ceil(originalEnd) - originalEnd);
    $('training-tail-note').textContent = isMineCPM()
      ? `本版录音 ${originalEnd.toFixed(3)} 秒；训练时间轴 ${total.toFixed(0)} 个 1 秒 unit。${partial > 0.000001 ? `末块补齐 ${partial.toFixed(3)} 秒。` : ''}${appended ? `另补 ${appended} 个明确标记的静音 unit。` : ''} tts_eos 的位置由真实语音播放结束决定，label 为 -100；文字生成桶与声音实际播放时间分别查看。`
      : `原录音 ${originalEnd.toFixed(3)} 秒；训练时间轴 ${total.toFixed(0)} 个 1 秒 unit。${partial > 0.000001 ? `原末块补齐 ${partial.toFixed(3)} 秒。` : ''}${appended ? `另新增 ${appended} 个明确标记的静音 unit，用于独立 EOS。` : '没有新增整块静音 unit。'} 原音频文件没有改写。`;
  }
  async function resumeSelectedPlayer() {
    if (trainingTimeline?.active) { enterTrainingTail(currentPlaybackTime(),true); return; }
    await activePlayer.play();
  }
  const batchSize = 100;
  let batchIndex = 0;
  let filterReportURL = '';
  let trainingProtocol = null;
  const filterReports = new Map();
  // This public bundle intentionally contains only the current V1.4 cohort.
  let sourceMode = 'training-minicpm-en';
  const isInference = () => ['native','gander'].includes(sourceMode);
  const isEnglishTraining = () => ['training-gander-en','training-minicpm-en'].includes(sourceMode);
  const isMiniTraining = () => ['training-minicpm','training-minicpm-en'].includes(sourceMode);
  const sourcePickerValue = () => isMiniTraining() && dataVersion === 'V1.4' ? 'minecpm-v14' : sourceMode;
  const isMiniInterruptExtension = () => isMiniTraining() && trainingProtocol === 'minicpm_tail_interrupt';
  const trainingName = () => isMineCPM() ? 'MineCPM · V1.4 · 固定16 token＋外部TTS结束反馈' : `${isEnglishTraining() ? 'English · ' : ''}${isMiniTraining() ? `MiniCPM-o 4.5 · ${dataVersion || 'V1.1'} · ${isTurnRelative() ? '轮内完整1秒正文＋原生 turn_eos' : `TAIL＋${isMiniInterruptExtension() ? '历史打断扩展' : '原生 turn_eos'}`}` : 'Gander · 固定 8 token'}`;
  const trainingCloseName = () => isMineCPM() ? 'listen' : isMiniTraining() && !isMiniInterruptExtension() ? 'turn_eos' : 'interrupt';
  const inferenceName = () => sourceMode === 'gander' ? 'Gander' : 'MiniCPM-o 4.5';
  const caseRoot = () => ({native:'native-cases',gander:'gander-cases','training-gander':'converted-cases','training-minicpm':'minicpm-training-cases','training-gander-en':'english-gander-cases','training-minicpm-en':'english-minicpm-cases'})[sourceMode];
  const unitWording = text => String(text || '').replace(/秒/g,'unit时长');
  const timeLabel = t => isInference() ? `${Number(t).toFixed(2)}u` : clock(t);
  const modelTime = unit => runtime?.steps.find(s=>s.unit===unit)?.call_start_s ?? unit;
  const miniSummaryCache = new Map();
  function renderSourceLabels(summary = null) {
    if (isMiniTraining() && summary) miniSummaryCache.set(`${dataVersion}:${sourceMode}`,{samples:cases.length,filtered:summary.filtered_samples || 0});
    for (const option of $('data-source').options) {
      if (!['training-minicpm','training-minicpm-en'].includes(option.value)) continue;
      const legacyVersion = dataVersion === 'V1.4' ? [...dataVersions].reverse().find(v => v.id !== 'V1.4') || dataVersions[0] : selectedVersion();
      const english = option.value.endsWith('-en'), cohort = legacyVersion.cohorts?.[english ? 'english' : 'chinese'];
      const cached = miniSummaryCache.get(`${dataVersion}:${option.value}`);
      const samples = cached?.samples ?? cohort?.samples;
      option.disabled = false;
      option.textContent = dataVersion === 'V1.4'
        ? `${english ? 'English · ' : ''}MiniCPM-o 4.5 · V1.1–V1.3 · ${english ? '英文' : '中文'}训练数据`
        : `${english ? 'English · ' : ''}MiniCPM-o 4.5 · ${dataVersion || '训练数据'} · ${english ? '英文' : '中文'}${samples != null ? ` ${samples} 条` : '训练数据'}${cached?.filtered ? ` · ${cached.filtered} 条预算过滤` : ''}`;
    }
  }
  function miniSequenceDescription() {
    if (isMiniInterruptExtension()) return '此为历史 TAIL 打断扩展：正常结束使用 turn_eos，打断使用块末 interrupt。';
    const body = isTurnRelative()
      ? 'V1.3 从每轮首个保留正文 token 的起点划分完整 1 秒正文窗口。训练生成 unit 与原声学 unit 分开记录，正文可能在声学起声桶之前的 unit 生成；全局麦克风 unit、原录音和源时间戳不变。正文 token 的提示与详情显示两套位置；点击正文定位原声学起点，unit 的“定位播放器”定位该全局输入桶。'
      : '时间轴按全局 1 秒划分 unit，正文 token 数量可变。';
    const eos = isDelayedEOS() ? `${dataVersion} 正常结束在最后正文的下一空正文 SPEAK unit 输出 turn_eos；打断仍在原停声桶结束，期间允许空 SPEAK。` : 'V1.1 正常结束把 turn_eos 放在最后正文桶。';
    return body + eos + 'BPE 起声由原有词／片段对齐区间估算，同一区间内多个 token 可能共享起点，并非逐 BPE 精确声学时间。正常结束和打断均使用原生 <|turn_eos|>，不监督 interrupt；turn_eos 结束生成并收尾，已排队语音仍会播完。';
  }

  function renderMineCPM(info = {}, sample = null) {
    const host = $('minecpm-details');
    host.hidden = !isMineCPM();
    if (host.hidden) { host.replaceChildren(); return; }
    const details = sample?.meta?.minecpm || info.minecpm || {};
    const eos = Array.isArray(details.tts_eos_events) ? details.tts_eos_events : null;
    const interruptions = Array.isArray(details.interruptions) ? details.interruptions : [];
    const allGaps = Array.isArray(details.delivery_gaps) ? details.delivery_gaps : null;
    const gaps = allGaps?.filter(gap => !gap.initial);
    const gapSeconds = gaps?.reduce((sum,gap) => sum + Math.max(0,Number(gap.end_frame)-Number(gap.start_frame))/16000,0);
    const jump = (frame, label) => Number.isFinite(Number(frame)) && frame != null
      ? `<button type="button" class="event-control" data-minecpm-seconds="${Number(frame)/16000}">${escape(label ?? `${(Number(frame)/16000).toFixed(3)} 秒`)} →</button>` : '未提供';
    const unit = value => Number.isInteger(value) ? `unit ${value}` : '未提供';
    const eosRows = eos?.map(event => `<div class="reply-plan"><strong>${escape(event.assistant_id || '助手')} · 自然播放结束</strong><dl><dt>语音实际播完</dt><dd>${jump(event.playback_end_frame)}</dd><dt>外部反馈位置</dt><dd>${escape(unit(event.feedback_unit))}${Number.isInteger(event.token_position) ? ` · 序列位置 ${event.token_position}` : ''}</dd><dt>输入与监督</dt><dd><code>&lt;|tts_eos|&gt;</code> · label=${escape(event.label ?? -100)}；随后 LISTEN</dd></dl></div>`).join('');
    const stopRows = interruptions.map(event => `<div class="reply-plan"><strong>${escape(event.assistant_id || '助手')} · 用户打断</strong><dl><dt>切换收听</dt><dd>${escape(unit(event.listen_unit))} · 仅 LISTEN，无正文、无 tts_eos</dd><dt>实际截断</dt><dd>${jump(event.stop_frame)}</dd></dl></div>`).join('');
    const gapRows = allGaps?.map(gap => `<li>${escape(gap.assistant_id || '助手')} · ${gap.initial ? '首包等待' : '播放断供'} · ${jump(gap.start_frame,`${(Number(gap.start_frame)/16000).toFixed(3)}–${(Number(gap.end_frame)/16000).toFixed(3)} 秒`)} · ${((Number(gap.end_frame)-Number(gap.start_frame))/16000).toFixed(3)} 秒</li>`).join('');
    host.innerHTML = `<div class="section-top"><h2>MineCPM · 文字生成与语音播放</h2><span><a href="${escape(datasetURL('tts-eos-estimation.html'))}" target="_blank" rel="noopener">本版 TTS 结束反馈与统计报告 ↗</a> · <a href="${escape(datasetURL('tts-eos-estimation.json'))}" download>下载统计 JSON</a></span></div><p class="annotation">这是训练目标与真实流式语音记录的配对展示。先生成的文字可能仍在语音队列中；tts_eos 表示语音已播放完成，由外部作为输入提供，不让 Thinker 预测。</p><div class="runtime-cards"><div><small>常规正文批量</small><strong>16 token / unit</strong><span>轮末允许不足；不使用 turn_eos</span></div><div><small>正常结束的外部反馈</small><strong>${eos ? eos.length + ' 次' : '等待样本记录'}</strong><span>tts_eos 输入 · label=-100</span></div><div><small>真实流式播放断供</small><strong>${gaps ? gaps.length + ' 处' : '等待样本记录'}</strong><span>${gapSeconds != null ? `累计 ${gapSeconds.toFixed(3)} 秒；不含首次等待` : '缺失记录不记为 0 次'}</span></div></div><details class="reply-plans"><summary>查看 TTS 结束反馈与打断位置</summary><div class="reply-plan-list">${eosRows || (eos ? '<p>本样本没有自然结束反馈。</p>' : '<p>尚未提供本样本的反馈位置记录。</p>')}${stopRows}</div></details><details class="reply-plans"><summary>查看真实音频供给空档${allGaps ? ` · ${allGaps.length} 处（含首次等待）` : ''}</summary><p class="annotation">这些空档来自真实音频块的到达与播放记录；与录音内自然停顿分别统计。点击可定位完整对话音频。</p>${gapRows ? `<ol>${gapRows}</ol>` : `<p>${allGaps ? '没有记录到音频供给空档。' : '尚未提供本样本的真实供给空档记录。'}</p>`}</details>${info.tts_trace_url ? `<p><a href="${escape(info.tts_trace_url)}" target="_blank" rel="noopener">查看本样本原始 TTS 记录 ↗</a></p>` : ''}`;
  }
  const view = new window.TrainingTokenView($('training-tokens'), {returnedAudio:true, seek: seconds => {
    seekTo(isInference()?modelTime(seconds):seconds,false); resumeSelectedPlayer().catch(() => {});
  }, onUnitSelect: (unit,detail) => seekTo(isInference() ? modelTime(unit) : isTurnRelative() && Number.isFinite(detail?.sourceSeconds) ? detail.sourceSeconds : unit, false)});
  view.setLiveProgress(false);
  function syncDialogueControls() {
    const player = $('mixed-audio');
    const ready = !dialogueLoading && !!loadedId && !!player.getAttribute('src');
    const playing = dialoguePlaying();
    for (const button of document.querySelectorAll('[data-dialogue-action]')) {
      button.disabled = !ready;
      if (button.dataset.dialogueAction === 'restart') continue;
      const ended = trainingTimeline?.active ? trainingTimeline.time >= duration : player.ended;
      button.textContent = playing ? 'Ⅱ 暂停对话' : ended ? '▶ 重新播放' : '▶ 开始对话';
      button.setAttribute('aria-pressed', String(playing));
      button.title = playing ? '暂停当前对话' : '从当前位置开始或继续播放完整对话';
    }
  }
  async function startDialogue() {
    if (dialogueLoading || !$('mixed-audio').getAttribute('src')) return;
    view.stopAudio();
    const player = $('mixed-audio');
    if (activePlayer !== player) { const time = currentPlaybackTime(); activePlayer = player; seekTo(time,false); }
    if (trainingTimeline?.active) {
      if (currentPlaybackTime() >= duration) seekTo(0,false);
      else { enterTrainingTail(currentPlaybackTime(),true); return; }
    } else if (player.ended) seekTo(0,false);
    try {
      await player.play();
    } catch (error) {
      $('timeline-now').textContent = `对话音频播放失败：${error.message}`;
    }
    syncDialogueControls();
  }
  function pauseDialogue() {
    pauseTrainingTail();
    for (const player of [$('mixed-audio'), $('input-audio')]) player.pause();
    view.stopAudio();
    syncDialogueControls();
  }
  function restartDialogue() {
    if (dialogueLoading || !$('mixed-audio').getAttribute('src')) return;
    pauseDialogue();
    activePlayer = $('mixed-audio');
    seekTo(0, true, 0);
    $('timeline-scroll').scrollLeft = 0;
    $('events').scrollTop = 0;
    startDialogue();
  }
  function renderRuntime(state) {
    $('runtime-panel').hidden = !isInference();
    if (!state) {
      for(const id of ['runtime-capture','runtime-model','runtime-output','runtime-input-size','runtime-audio-size']) $(id).textContent='正在读取记录…';
      for(const id of ['runtime-capture-note','runtime-phase','runtime-input-queue','runtime-audio-queue']) $(id).textContent='';
      return;
    }
    $('runtime-capture').textContent = state.captureUnit == null ? '收音已结束' : `正在收音 unit ${state.captureUnit}`;
    $('runtime-capture-note').textContent = `录音轴 ${Math.min(state.time,runtime.input_observed_through_s).toFixed(2)} unit时长`;
    $('runtime-model').textContent = state.processing ? `正在处理 unit ${state.processing.unit}` : state.completed ? `unit ${state.completed.unit} 已完成` : '尚未开始处理';
    $('runtime-phase').textContent = state.phase?.label || (state.waiting.length ? '等待调度下一块输入' : '等待下一块输入');
    $('runtime-output').textContent = state.audible.length ? `正在播放 unit ${[...new Set(state.audible)].join('、')}` : '当前没有声音播放';
    $('runtime-input-size').textContent = `${state.waiting.length} 个完整 unit`;
    $('runtime-input-queue').innerHTML = state.waiting.map(s=>`<button type="button" data-runtime-unit="${s.unit}">unit ${s.unit} · 排队</button>`).join('') || '<span class="runtime-empty">没有完整输入块在排队</span>';
    $('runtime-audio-size').textContent = `${state.audioPending.toFixed(2)} unit时长`;
    $('runtime-audio-queue').innerHTML = state.queued.map(s=>`<button type="button" class="${s.start<=state.time?'is-playing':''}" data-runtime-unit="${s.unit}">unit ${s.unit} · ${s.start<=state.time?'播放中，余':'待播'} ${s.remaining.toFixed(2)}u</button>`).join('') || '<span class="runtime-empty">播放队列为空</span>';
  }
  function drawPosition(seconds, follow = false) {
    const t = Math.max(0, Math.min(duration || 0, Number(seconds) || 0));
    const cursor = $('timeline-chart').querySelector('.timeline-cursor');
    if (cursor) {
      cursor.style.left = `${duration ? 100 * t / duration : 0}%`;
      cursor.querySelector('span').textContent = timeLabel(t);
      if (follow) {
        const scroll = $('timeline-scroll'), pixel = 76 + (scroll.scrollWidth - 76) * t / duration;
        if (pixel < scroll.scrollLeft + 25 || pixel > scroll.scrollLeft + scroll.clientWidth - 25)
          scroll.scrollLeft = Math.max(0, pixel - scroll.clientWidth / 2);
      }
    }
    runtimeState = isInference() && runtime ? window.NativeRuntime.at(runtime,t) : null;
    const audible = runtimeState?.audible || [];
    if (trainingTimeline) {
      const tail = t >= trainingTimeline.originalEnd;
      $('training-timeline-time').textContent = `${t.toFixed(2)} / ${duration.toFixed(2)} 秒`;
      $('training-timeline-progress').value = String(t);
      $('training-timeline-progress').setAttribute('aria-valuetext', `${t.toFixed(2)} 秒，${tail ? '训练补充静音' : '原录音'}`);
      const added = Math.floor(t) >= Math.ceil(trainingTimeline.originalEnd);
      $('training-tail-state').textContent = tail ? `${added && trainingTimeline.appended ? '新增的 EOS 静音 unit' : '原末块的静音补齐'} · 原录音已结束，此处是明确标记的训练输入补齐。` : '正在回放原始录音；末尾训练补齐另行标记。';
      $('training-tail-state').classList.toggle('is-tail',tail);
    }
    $('timeline-now').textContent = runtimeState ? `录音轴 ${timeLabel(t)} · ${runtimeState.processing ? '模型正在处理 unit '+runtimeState.processing.unit+'（'+runtimeState.phase?.label+'）' : '模型已完成 unit '+(runtimeState.completed?.unit ?? '—')} · ${audible.length ? '声音来自 unit '+[...new Set(audible)].join(', ') : '当前无声音播放'}` : `播放位置 ${timeLabel(t)} / ${timeLabel(duration)}`;
    renderRuntime(runtimeState);
    for (const bar of $('timeline-chart').querySelectorAll('[data-tl-start]'))
      bar.classList.toggle('active', Number(bar.dataset.tlStart) <= t && t < Number(bar.dataset.tlEnd));
    for (const row of $('events').querySelectorAll('[data-event-start]'))
      row.querySelector('.event')?.classList.toggle('active', Number(row.dataset.eventStart) <= t && t < Number(row.dataset.eventEnd));
    if (follow) {
      const list = $('events'), row = [...list.querySelectorAll('[data-event-lane]')].find(item => item.dataset.eventLane !== 'ambient' && item.querySelector('.event.active'));
      if (row && (row.offsetTop < list.scrollTop || row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight))
        list.scrollTop = Math.max(0, row.offsetTop - list.clientHeight / 3);
    }
    if (isInference()) {
      view.setRuntimeProgress(t, runtimeState, follow);
      view.setOutputUnits([...new Set(audible)], false);
    } else view.setTime(trainingTimeline ? Math.min(t,Math.max(0,duration-0.000001)) : t, follow);
  }
  function seekTo(seconds, jump = true, sourceUnit) {
    const t = Math.max(0, Math.min(duration || 0, Number(seconds) || 0));
    const tailWasPlaying = !!trainingTimeline?.active && trainingTimeline.playing;
    const wasPlaying = dialoguePlaying();
    if (trainingTimeline && t >= trainingTimeline.originalEnd) enterTrainingTail(t,wasPlaying);
    else {
      leaveTrainingTail();
      for (const player of [$('mixed-audio'),$('input-audio')]) {
        if (player.getAttribute('src')) { try { player.currentTime = t; } catch (_) {} }
      }
      if (tailWasPlaying) activePlayer.play().catch(()=>{});
    }
    drawPosition(t, true);
    if (jump) view.jump(sourceUnit ?? (isInference() ? runtimeState?.processing?.unit ?? runtimeState?.completed?.unit ?? -2 : trainingUnitAt(t)), {notify:false});
  }
  function renderTimeline() {
    const chart = $('timeline-chart'), scale = Number($('timeline-zoom').value), width = Math.max(680, duration * scale + 76);
    chart.style.width = `${width}px`;
    const lanes = isInference() ? [['user','用户输入'],['compute','模型处理'],['generation','输出可用'],['assistant','实际播放']] : [['user','用户'],['assistant','助手'],['aside','旁话'],['ambient','环境音'],...(trainingTimeline && trainingTimeline.end > trainingTimeline.originalEnd ? [['training','训练补齐']] : [])];
    const ticks = [];
    for (let t = 0; t <= duration; t += isInference()?5:30) ticks.push(`<span class="timeline-tick" style="left:${100*t/duration}%">${isInference()?'unit '+t:clock(t)}</span>`);
    chart.innerHTML = `<div class="timeline-ruler">${ticks.join('')}</div>${lanes.map(([id,name]) => `<div class="timeline-lane"><span class="timeline-lane-label">${name}</span><div class="timeline-track" data-track="${id}">${dialogueEvents.filter(e => e.lane === id).map(e => `<button type="button" class="timeline-bar tl-${e.style}" style="left:${100*e.start/duration}%;width:${100*(e.end-e.start)/duration}%" data-event-index="${e.index}" data-tl-start="${e.start}" data-tl-end="${e.end}" title="${escape(eventTitle(e))}" aria-label="${escape(e.label)}，${timeLabel(e.start)}">${escape(e.short)}</button>`).join('')}</div></div>`).join('')}<div class="timeline-cursor-area"><div class="timeline-cursor"><span>0:00</span></div></div>`;
    const compute = chart.querySelector('[data-track="compute"]');
    if (compute && runtime) compute.innerHTML = runtime.steps.flatMap(window.NativeRuntime.phases).map(p=>`<button type="button" class="timeline-bar tl-phase-${p.kind}" style="left:${100*p.start/duration}%;width:${100*(p.end-p.start)/duration}%" data-process-unit="${p.unit}" data-tl-start="${p.start}" data-tl-end="${p.end}" title="unit ${p.unit} · ${p.label} · 录音轴 ${timeLabel(p.start)}–${timeLabel(p.end)}">u${p.unit}</button>`).join('');
    drawPosition(currentPlaybackTime());
  }
  function renderEvents() {
    const filter = $('event-filter').value;
    const selected = dialogueEvents.filter(e => filter === 'all' || e.kind === filter || (filter === 'environment' && e.lane === 'ambient'));
    $('events').innerHTML = selected.map(e => `<div class="event-row" data-event-start="${e.start}" data-event-end="${e.end}" data-event-lane="${e.lane}"><button class="event" type="button" data-event-index="${e.index}"><span>${timeLabel(e.start)} · ${escape(e.label)}</span><span>${escape(e.text)}</span></button>${Number.isInteger(e.control) ? `<button type="button" class="event-control" data-control-unit="${e.control}" title="跳到训练控制所在 unit">${trainingCloseName()} unit ${e.control} →</button>` : ''}${(e.shifts || []).map(shift => `<button type="button" class="event-shift" data-quantization-unit="${shift.unit_index}" title="文本监督从原 unit ${shift.source_unit_index} 顺延 1u；原音频时序不变">监督顺延 1u → unit ${shift.unit_index}</button>`).join('')}</div>`).join('') || '<p>当前案例没有此类事件。</p>';
    drawPosition(currentPlaybackTime());
  }
  function currentBatch() {
    return isEnglishTraining() ? cases.slice(batchIndex * batchSize, (batchIndex + 1) * batchSize) : cases;
  }
  function renderBatchControls(matchCount = currentBatch().length) {
    $('batch-controls').hidden = !isEnglishTraining();
    $('case-search-label').textContent = isEnglishTraining() ? '搜索本批案例' : '搜索案例';
    $('case-search').placeholder = isEnglishTraining() ? '仅搜索当前批次的标题或案例 ID' : '搜索标题或案例 ID';
    $('case-search-note').hidden = !isEnglishTraining();
    if (!isEnglishTraining()) return;
    const total = Math.max(1, Math.ceil(cases.length / batchSize)), size = currentBatch().length;
    $('batch-status').textContent = `第 ${batchIndex + 1} / ${total} 批 · 本批 ${size} 条 · 共 ${cases.length} 条`;
    $('batch-next').disabled = total < 2;
    $('batch-next').textContent = batchIndex + 1 === total ? '换一批 ↻ 回到首批' : '换一批 →';
    $('case-search-note').textContent = $('case-search').value ? `仅搜索当前批次，匹配 ${matchCount} / ${size} 条；未匹配时保留当前播放案例。` : '搜索仅在当前批次内进行；换批后可继续搜索。';
  }
  function options(query = '', selected = $('case-select').value) {
    const caseBadge = c => isEnglishTraining() && !isMiniTraining() && c.minicpm_budget_filtered ? ' · MiniCPM预算过滤' : '';
    const matches = currentBatch().filter(c => `${c.id} ${c.title}${caseBadge(c)} ${compatibilityCount(c) ? 'Demo兼容备注' : ''}`.toLowerCase().includes(query.toLowerCase()));
    $('case-select').innerHTML = matches.length ? matches.map(c => `<option value="${escape(c.id)}">${escape(c.id)} · ${escape(c.title)}${caseBadge(c)}${compatibilityCount(c) ? ' · Demo兼容备注' : ''}</option>`).join('') : '<option value="">本批次没有匹配案例</option>';
    $('case-select').disabled = !matches.length;
    if (matches.some(c => c.id === selected)) $('case-select').value = selected;
    renderBatchControls(matches.length);
    return matches;
  }
  function renderDatasetFilter(summary) {
    const host = $('dataset-filtered');
    const count = summary?.counterpart_minicpm_filtered_samples ?? summary?.filtered_samples ?? 0;
    filterReportURL = !isInference() ? datasetURL(summary?.filtered_samples_report_url) : '';
    host.open = false;
    host.hidden = !filterReportURL || !count;
    host.dataset.count = String(count);
    $('dataset-filtered-list').textContent = '展开时读取名单。';
    if (host.hidden) return;
    $('dataset-filtered-title').textContent = isMineCPM() ? `查看 ${count} 条声学过滤记录` : isMiniTraining() ? `查看 ${count} 条预算过滤记录` : `查看 ${count} 条 MiniCPM 预算过滤记录 · Gander 均保留`;
    $('dataset-filtered-note').textContent = isMineCPM() ? '按用户确认，过滤人声重叠不可靠的案例；原录音和诊断保留，没有调整锚点或重排音频。' : isMiniTraining() ? '以下完整会话因正文块超出每个 unit 的 20 步生成预算而过滤；原始成功数据与 Gander 版本保留。' : '本 Gander 格式没有过滤这些会话；名单仅标注它们在 MiniCPM-o TAIL 格式中超出 20 步预算。';
    $('dataset-filtered-download').href = filterReportURL;
  }
  function fetchFilterPacket(url) {
    if (!filterReports.has(url)) filterReports.set(url,fetch(url,{cache:'no-cache'}).then(response=>{
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).catch(error=>{filterReports.delete(url);throw error;}));
    return filterReports.get(url);
  }
  async function missingCaseMessage(id, summary) {
    let reason = `未收录于当前 ${dataVersion} 训练集`;
    const url = datasetURL(summary?.filtered_samples_report_url);
    if (url) {
      try {
        const packet = await fetchFilterPacket(url);
        if (packet.filtered_cases?.some(row=>row.case_id===id)) reason = isMineCPM() ? '因重叠不可靠，按用户确认过滤' : `因该版本的每 unit 20 步生成预算被整条过滤`;
      } catch (_) { reason += '，过滤原因名单暂时读取失败'; }
    }
    return `案例 ${id} ${reason}；已显示${isEnglishTraining() ? '第一批的' : ''}首个可用案例。旧版本、原始数据与 Gander 版本保留，可切回查看。`;
  }
  async function loadDatasetFilter() {
    const url = filterReportURL, mode = sourceMode;
    if (!url || $('dataset-filtered').hidden || !$('dataset-filtered').open) return;
    $('dataset-filtered-list').textContent = '正在读取过滤名单…';
    try {
      const packet = await fetchFilterPacket(url);
      if (url !== filterReportURL || mode !== sourceMode) return;
      const rows = packet.filtered_cases;
      if (!Array.isArray(rows) || rows.length !== Number($('dataset-filtered').dataset.count) || new Set(rows.map(row => row.case_id)).size !== rows.length)
        throw new Error('过滤名单与本批统计不匹配');
      $('dataset-filtered-list').innerHTML = `<ol class="filtered-case-list">${rows.map(row => `<li><div><strong>${escape(row.case_id)}${row.title ? ` · ${escape(row.title)}` : ''}</strong><span>${isMineCPM() ? escape(row.failure_reason || row.reason || '声学重叠不可靠，已过滤') : `超出 MiniCPM-o 每个 unit 的 20 步生成预算${Array.isArray(row.buckets) ? ` · ${row.buckets.length} 处` : ''}`}</span></div>${isMineCPM() ? '<span>原始记录保留</span>' : `<button type="button" data-filtered-gander-case="${escape(row.case_id)}">在 Gander 中查看</button>`}</li>`).join('')}</ol>`;
    } catch (error) {
      if (url === filterReportURL && mode === sourceMode) $('dataset-filtered-list').textContent = `过滤名单加载失败：${error.message}`;
    }
  }
  async function loadQuantization(info, ticket) {
    const host = $('quantization-note'), count = info.quantization_shift_count || 0;
    host.hidden = isInference() || !count;
    host.replaceChildren();
    if (host.hidden) return [];
    const overview = `${count} 处文本监督顺延 1u，原音频时序不变。`;
    host.textContent = `${overview} 正在读取具体位置…`;
    try {
      if (!info.quantization_shifts_url) throw new Error('缺少顺延记录地址');
      const response = await fetch(info.quantization_shifts_url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const packet = await response.json();
      if (ticket !== request) return [];
      if (packet.case_id !== info.id || !Array.isArray(packet.shifts) || packet.shifts.length !== count ||
          packet.shifts.some(shift => shift.shift_units !== 1 || !Number.isInteger(shift.source_unit_index) || shift.unit_index !== shift.source_unit_index + 1))
        throw new Error('顺延记录与案例不匹配');
      host.innerHTML = `<strong>${overview}</strong><details><summary>查看监督位置</summary><ul>${packet.shifts.map(shift => `<li>${escape(shift.assistant_id)}：原 unit ${escape(shift.source_unit_index)} → <button type="button" data-quantization-unit="${escape(shift.unit_index)}">训练 unit ${escape(shift.unit_index)}</button></li>`).join('')}</ul><a href="${escape(info.quantization_shifts_url)}" download>下载本案例顺延记录</a></details>`;
      return packet.shifts;
    } catch (error) {
      if (ticket === request) host.textContent = `${overview} 位置加载失败：${error.message}`;
      return [];
    }
  }
  function compatibilityCount(info) {
    return info.compatibility_note_count ?? info.compatibility_notes?.length ?? 0;
  }
  async function loadCompatibility(info, ticket) {
    renderCompatibility(info);
    if (!info.compatibility_notes_url) return;
    try {
      const response = await fetch(info.compatibility_notes_url);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const packet = await response.json();
      if (ticket !== request) return;
      if (packet.case_id !== info.id || !Array.isArray(packet.notes) || packet.notes.length !== compatibilityCount(info))
        throw new Error('兼容说明与案例不匹配');
      renderCompatibility({...info, compatibility_notes:packet.notes});
    } catch(error) {
      if (ticket !== request) return;
      $('training-compatibility').hidden = false;
      $('training-compatibility').textContent = `兼容说明加载失败：${error.message}`;
    }
  }
  function renderCompatibility(info) {
    const notes = info.compatibility_notes || [], host = $('training-compatibility');
    host.hidden = isInference() || !notes.length;
    if (host.hidden) { host.replaceChildren(); return; }
    const unitNote = note => `unit ${escape(note.unit_index)} 的 TAIL 块含 ${escape(note.lexical_tokens)} 个正文 token、${escape(note.decoded_characters)} 个字符，超过官方 Demo 的 28 字符保护。<button type="button" data-training-warning-unit="${escape(note.unit_index)}">定位 unit ${escape(note.unit_index)}</button>`;
    const reports = [...new Set(notes.map(note => note.report_url).filter(Boolean))];
    const official = notes.find(note => note.official_source_url)?.official_source_url;
    const links = `${reports.map((url,index) => `<a href="${escape(datasetURL(url))}" download>下载兼容说明${reports.length > 1 ? ` ${index + 1}` : ''}</a>`).join('')}${official ? `<a href="${escape(official)}" target="_blank" rel="noopener">官方代码</a>` : ''}`;
    if (!isEnglishTraining() && notes.length === 1) {
      host.innerHTML = `<strong>已确认的 Demo 兼容差异：</strong>${unitNote(notes[0])} 按已确认方案保留源对齐和 TAIL 标签。${links}`;
    } else {
      host.innerHTML = `<details class="compatibility-details"><summary>本案例 ${notes.length} 个 TAIL 块超过 Demo 字符保护 · 展开定位</summary><p>下列备注保留原 TAIL 分块；官方 Demo 的 28 字符保护与本训练格式存在执行差异。</p><ol class="compatibility-list">${notes.map(note => `<li>${unitNote(note)}</li>`).join('')}</ol><div class="compatibility-links">${links}</div></details>`;
    }
  }
  function renderReplyPlans(sample) {
    const host = $('reply-plans');
    host.hidden = isInference();
    if (isInference()) { host.replaceChildren(); return; }
    const replies = sample.turns.filter(t => t.role === 'assistant');
    host.innerHTML = `<summary>完整回复、训练正文与实际播放 · ${replies.length} 条回复</summary><p class="annotation">完整计划全部保留。主序列只纳入打断前按本格式排定的正文；未播放文字仍可参与训练。剩余未生成计划只供查看，不作为后续模型输入。</p><div class="reply-plan-list">${replies.map(t => {
      const m = t.meta || {}, full = m.full_text ?? m.full_response?.text ?? t.text ?? '';
      const heard = m.heard_text ?? t.text ?? '', generated = m.generated_text ?? t.text ?? '';
      const tokens = m.supervised_token_ids || [], suppressed = m.suppressed_token_ids || [];
      const remaining = m.suppressed_text ?? m.full_response?.suppressed_text ?? (full.startsWith(generated) ? full.slice(generated.length) : '完整原文见上方；末字符跨 BPE 边界，剩余原始 token ID 保存在训练 JSON。');
      const textEnd = m.text_finish_unit ?? m.text_end_unit;
      const ttsEnd = m.tts_eos_unit ?? m.tts_eos_feedback_unit;
      const listen = m.interruption_unit ?? m.listen_unit ?? m.tts_eos_unit;
      const close = isMineCPM()
        ? `${Number.isInteger(textEnd) ? ` · 文字结束 unit ${textEnd}` : ''}${Number.isInteger(ttsEnd) ? ` · 外部 tts_eos unit ${ttsEnd}` : ''}${Number.isInteger(listen) ? ` · LISTEN unit ${listen}` : ''}`
        : isMiniTraining() && Number.isInteger(m.tail_close_unit) ? ` · ${m.is_interrupted ? trainingCloseName() + ' · 提前结束' : 'turn_eos · 正常结束'} unit ${m.tail_close_unit}` : '';
      return `<details class="reply-plan"><summary>${escape(m.assistant_id || '助手')} · ${m.is_interrupted || t.turn_state === 'interrupted' ? '播放被打断' : '完整播放'} · 训练正文 ${tokens.length} token${close}${suppressed.length ? ` · 未入主序列 ${suppressed.length} token` : ''}</summary><dl><dt>原始完整回答</dt><dd>${escape(full)}</dd><dt>实际已播放文字</dt><dd>${escape(heard)}</dd><dt>主序列纳入的正文</dt><dd>${escape(generated)}</dd>${suppressed.length ? `<dt>其余完整计划</dt><dd>${escape(remaining)}</dd>` : ''}</dl></details>`;
    }).join('')}</div>`;
  }
  async function choose() {
    const id = $('case-select').value, info = cases.find(c => c.id === id), ticket = ++request;
    if (!info) return;
    dialogueLoading = true;
    syncDialogueControls();
    loadedId = id;
    resetTrainingTimeline();
    for (const player of [$('mixed-audio'),$('input-audio')]) player.pause();
    activePlayer = $('mixed-audio');
    $('mixed-audio').src = info.mixed_audio || datasetURL(`${mixRoot}${encodeURIComponent(id)}/dialogue.mp3`);
    const inputAudio = info.input_audio || datasetURL(`${isEnglishTraining() ? 'english-input-audio' : 'converted-input-audio'}/${encodeURIComponent(id)}.wav`);
    $('input-audio').src = inputAudio;
    duration = info.seconds; dialogueEvents = []; runtime = null; runtimeState = null;
    dialogueLoading = false;
    syncDialogueControls();
    $('event-filter').value = 'all';
    renderTimeline();
    $('case-title').textContent = info.title;
    $('case-info').textContent = isInference() ? `${id} · ${info.seconds.toFixed(1)} unit时长 · ${unitWording(info.result_label) || '模型实际推理'} · GPU ${info.gpu}` : `${id} · ${clock(info.seconds)} · 附和 ${info.counts?.backchannel || 0} · 打断 ${info.interruptions} · 旁话 ${info.counts?.side_speech || 0}${isEnglishTraining() && !isMiniTraining() && info.minicpm_budget_filtered ? ' · MiniCPM 预算过滤；Gander 保留' : ''}`;
    $('case-download').href = datasetURL(`${caseRoot()}/${encodeURIComponent(id)}.json`);
    $('raw-trace-download').hidden = !isInference();
    $('run-config').hidden = !isInference();
    $('run-config').textContent = isInference() ? `${info.run_label || '历史记录'} · ${info.gpu_name || '历史环境'} · ${info.compile_enabled ? 'torch.compile 已开启' : '未开启编译加速'}${info.warmup_enabled ? ' · 正式会话前已执行独立预热' : ''}。不同条目的硬件与编译配置有差异，耗时变化不能仅归因于预热。` : '';
    $('warmup-download').hidden = !isInference() || !info.warmup_url;
    if(info.warmup_url) $('warmup-download').href = info.warmup_url;
    const compatibilityReady = loadCompatibility(info, ticket);
    const quantizationReady = loadQuantization(info, ticket);

    $('raw-trace-download').href = datasetURL(`native-audio/${encodeURIComponent(id)}/trace.json`);
    $('native-assessment').hidden = !isInference();
    $('native-assessment').textContent = isInference() ? unitWording(info.result_summary) : '';
    $('events').textContent = '正在加载…'; $('case-json').textContent = ''; $('reply-plans').hidden = true;
    renderMineCPM(info);
    const location = new URL(window.location); location.searchParams.set('case',id); location.searchParams.set('source',sourceMode);
    if (isMiniTraining()) location.searchParams.set('version',dataVersion); else location.searchParams.delete('version');
    if (isEnglishTraining()) location.searchParams.set('batch',String(batchIndex + 1)); else location.searchParams.delete('batch');
    history.replaceState(null,'',location);
    const tokenReady = view.load(info.token_url || datasetURL(`${isMiniTraining() ? 'minicpm-training-sequences' : 'token-sequences'}/${encodeURIComponent(id)}.json`), inputAudio, isInference() ? undefined : info.transcript_url || datasetURL(`${isEnglishTraining() ? 'english-audio-transcripts' : 'audio-transcripts'}/${encodeURIComponent(id)}.json`), isInference() ? datasetURL(`native-audio/${encodeURIComponent(id)}/returned-audio.json`) : undefined, datasetBase().href);
    try {
      const response = await fetch(datasetURL(`${caseRoot()}/${encodeURIComponent(id)}.json`), {cache:'no-cache'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const sample = await response.json(); if (ticket !== request) return;
      runtime = sample.runtime || null;
      configureTrainingTimeline(info,sample);
      $('case-json').textContent = JSON.stringify(sample,null,2);
      renderReplyPlans(sample);
      renderMineCPM(info,sample);
      const labels = {deliver:'用户',competitive_interruption:'用户 · 插话',backchannel:'用户 · 附和',side_speech:'旁话',ambient_layer:'持续环境声',environment_event:'环境音',native_generation:'模型输出可用',native_interrupt:'模型 · interrupt'};
      dialogueEvents = sample.turns.filter(t => t.role === 'assistant' || t.meta?.source_event).map((t,index) => {
        const e = t.meta?.source_event || {}, kind = t.role === 'assistant' ? 'assistant' : t.meta?.event_type;
        const label = kind === 'assistant' ? `助手${t.meta.is_interrupted ? ' · 被打断' : ''}` : labels[kind] || kind;
        const lane = ['native_generation','native_interrupt'].includes(kind) ? 'generation' : kind === 'assistant' ? 'assistant' : kind === 'side_speech' ? 'aside' : ['ambient_layer','environment_event'].includes(kind) ? 'ambient' : 'user';
        const style = ['competitive_interruption','native_interrupt'].includes(kind) ? 'interrupt' : kind === 'backchannel' ? 'backchannel' : lane;
        const start = (t.meta?.source_start_frame ?? e.start_frame ?? t.start_ms * 16) / 16000;
        const end = (t.meta?.source_stop_frame ?? (e.start_frame != null ? e.start_frame + e.frames : t.end_ms * 16)) / 16000;
        return {index,kind,label,lane,style,start,end,control:t.meta?.control_block_index,sourceUnit:t.meta?.native_unit_index,assistantId:t.meta?.assistant_id,text:(kind === 'assistant' ? t.meta?.heard_text : null) ?? t.text ?? e.text ?? e.description ?? e.audio_id ?? '环境声音',short:t.meta?.assistant_id || e.audio_id || label};
      });
      if (trainingTimeline && duration > trainingTimeline.originalEnd) dialogueEvents.push({index:dialogueEvents.length,kind:'training_silence',label:'训练尾部静音补齐',lane:'training',style:'synthetic',start:trainingTimeline.originalEnd,end:duration,text:'原录音已结束；此区间是明确标记的静音输入补齐，未改写原音频。',short:'静音补齐'});
      const [,,shifts] = await Promise.all([tokenReady, compatibilityReady, quantizationReady]); if (ticket !== request) return;
      for (const event of dialogueEvents) event.shifts = shifts.filter(shift => shift.assistant_id === event.assistantId);
      renderTimeline(); renderEvents();
    } catch(e) { if (ticket === request) $('events').textContent = `样本读取失败：${e.message}`; }
  }
  $('case-select').addEventListener('change',choose);
  $('case-search').addEventListener('input',e => {
    const matches = options(e.target.value);
    if (matches.length && $('case-select').value !== loadedId) choose();
  });
  $('batch-next').addEventListener('click', () => {
    if (!isEnglishTraining() || !cases.length) return;
    batchIndex = (batchIndex + 1) % Math.ceil(cases.length / batchSize);
    $('case-search').value = '';
    options('', '');
    choose();
  });
  $('events').addEventListener('click',e => {
    const shifted = e.target.closest('[data-quantization-unit]');
    if (shifted) { seekTo(Number(shifted.dataset.quantizationUnit)); return; }
    const control = e.target.closest('[data-control-unit]');
    if (control) { seekTo(Number(control.dataset.controlUnit)); return; }
    const item = e.target.closest('[data-event-index]'); if (!item) return;
    const event = dialogueEvents[Number(item.dataset.eventIndex)]; if (event) seekTo(event.start,true,event.sourceUnit);
  });
  $('timeline-chart').addEventListener('click',e => {
    const processing = e.target.closest('[data-process-unit]');
    if (processing) { seekTo(Number(processing.dataset.tlStart),true,Number(processing.dataset.processUnit)); return; }
    const item = e.target.closest('[data-event-index]');
    if (item) { const event = dialogueEvents[Number(item.dataset.eventIndex)]; if (event) seekTo(event.start,true,event.sourceUnit); return; }
    const track = e.target.closest('[data-track]'); if (!track) return;
    const rect = track.getBoundingClientRect(); seekTo(duration * Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)));
  });
  $('timeline-zoom').addEventListener('change',renderTimeline);
  $('event-filter').addEventListener('change',renderEvents);
  $('locate-audio').addEventListener('click', () => view.jump(isInference() ? runtimeState?.processing?.unit ?? runtimeState?.completed?.unit ?? -2 : trainingUnitAt(currentPlaybackTime()), {notify:false}));
  $('locate-output').addEventListener('click', () => { if(runtimeState?.audible.length) view.jump(runtimeState.audible[0],{notify:false}); });
  $('runtime-panel').addEventListener('click',e=>{const item=e.target.closest('[data-runtime-unit]');if(item)view.jump(Number(item.dataset.runtimeUnit),{notify:false});});
  $('training-compatibility').addEventListener('click',e=>{const item=e.target.closest('[data-training-warning-unit]');if(item)seekTo(Number(item.dataset.trainingWarningUnit));});
  $('quantization-note').addEventListener('click',e=>{const item=e.target.closest('[data-quantization-unit]');if(item)seekTo(Number(item.dataset.quantizationUnit));});
  $('minecpm-details').addEventListener('click',e=>{const item=e.target.closest('[data-minecpm-seconds]');if(item)seekTo(Number(item.dataset.minecpmSeconds));});
  $('dataset-filtered').addEventListener('toggle',loadDatasetFilter);
  $('dataset-filtered-list').addEventListener('click',e=>{
    const item=e.target.closest('[data-filtered-gander-case]');
    if (!item) return;
    sourceMode='training-gander-en';
    loadSource({id:item.dataset.filteredGanderCase});
  });
  for (const button of document.querySelectorAll('[data-dialogue-action]')) {
    button.addEventListener('click', () => {
      if (button.dataset.dialogueAction === 'restart') { restartDialogue(); return; }
      const player = $('mixed-audio');
      if (!dialoguePlaying()) startDialogue();
      else pauseDialogue();
    });
  }
  for (const player of [$('mixed-audio'),$('input-audio')]) {
    player.addEventListener('play', () => { leaveTrainingTail(); activePlayer = player; [$('mixed-audio'),$('input-audio')].filter(p => p !== player).forEach(p => p.pause()); syncDialogueControls(); });
    for (const event of ['pause','loadedmetadata','error']) player.addEventListener(event,syncDialogueControls);
    player.addEventListener('ended', () => {
      if (activePlayer === player && trainingTimeline && duration > trainingTimeline.originalEnd) enterTrainingTail(trainingTimeline.originalEnd,true);
      else syncDialogueControls();
    });
    player.addEventListener('seeking', () => {
      if (activePlayer !== player || !trainingTimeline?.active) return;
      const resume = trainingTimeline.playing, seconds = player.currentTime || 0;
      if (seconds >= trainingTimeline.originalEnd) { enterTrainingTail(seconds,resume); return; }
      leaveTrainingTail();
      drawPosition(seconds,$('follow-audio').checked);
      if (resume) player.play().catch(()=>{});
    });
    player.addEventListener('timeupdate', () => {
      if (activePlayer !== player || trainingTimeline?.active) return;
      drawPosition(currentPlaybackTime(),$('follow-audio').checked);
    });
  }
  async function loadSource(preferredInfo = null) {
    const loading = ++request;
    dialogueLoading = true;
    resetTrainingTimeline();
    $('data-version').disabled = true;
    filterReportURL = '';
    $('dataset-filtered').hidden = true;
    $('format-notice').hidden = true;
    $('quantization-note').hidden = true;
    $('minecpm-details').hidden = true;
    syncDialogueControls();
    $('batch-next').disabled = true;
    $('case-select').disabled = true;
    $('case-search').disabled = true;
    for (const player of [$('mixed-audio'),$('input-audio')]) player.pause();
    view.stopAudio(); $('case-search').value = ''; $('data-source').value = sourcePickerValue();
    try {
      if (isMiniTraining()) await loadDataVersions();
      if (loading !== request) return;
      renderDataVersions(); renderSourceLabels();
      if (isMiniTraining() && dataVersion === 'V1.4' && sourceMode === 'training-minicpm') {
        sourceMode = 'training-minicpm-en';
        $('data-source').value = sourcePickerValue();
        $('format-notice').textContent = 'V1.4 · MineCPM 本版只有英文数据；已切换到英文案例。V1.1–V1.3 的中文数据仍可切版查看。';
        $('format-notice').hidden = false;
      }
      const [response, summaryResponse] = await Promise.all([fetch(datasetURL(`${caseRoot()}/index.json`), {cache:'no-cache'}), isInference() ? Promise.resolve(null) : fetch(datasetURL(`${caseRoot()}/summary.json`), {cache:'no-cache'})]);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      if (summaryResponse && !summaryResponse.ok) throw new Error(`训练格式统计 HTTP ${summaryResponse.status}`);
      const [data, summary] = await Promise.all([response.json(), summaryResponse ? summaryResponse.json() : Promise.resolve(null)]); if (loading !== request) return;
      cases = data.map(resolveCaseURLs);
      trainingProtocol = summary?.protocol || null;
      const params = new URL(window.location).searchParams, preferredCase = preferredInfo?.id || params.get('case');
      const selectedIndex = cases.findIndex(c => c.id === preferredCase);
      const missingPreferred = isMiniTraining() && !!preferredCase && selectedIndex < 0;
      if (isEnglishTraining()) {
        // An explicit existing case takes precedence; missing Mini cases return to batch one.
        const requestedBatch = Number(params.get('batch'));
        batchIndex = missingPreferred ? 0 : selectedIndex >= 0 ? Math.floor(selectedIndex / batchSize) : params.has('batch') && Number.isInteger(requestedBatch) ? requestedBatch - 1 : batchIndex;
        batchIndex = Math.max(0,Math.min(Math.ceil(cases.length / batchSize)-1,batchIndex));
      }
      if (missingPreferred) {
        const message = await missingCaseMessage(preferredCase,summary); if (loading !== request) return;
        $('format-notice').textContent = message; $('format-notice').hidden = false;
      }
      const showSampling = !isInference() && (isEnglishTraining() || isMiniTraining());
      $('dataset-sampling').hidden = !showSampling;
      $('dataset-sampling').textContent = showSampling ? `${summary.planned_samples != null ? `本轮计划 ${summary.planned_samples} 条 · ` : ''}源成功 ${summary.source_successful_samples ?? cases.length+(summary.filtered_samples || 0)} 条${summary.failed_samples != null ? ` · 源生成失败 ${summary.failed_samples} 条（记录保留）` : ''} · ${summary.filtered_samples || 0} 条${isMineCPM() ? '声学过滤' : '本格式预算过滤'} · ${cases.length} 条训练数据` : '';
      renderSourceLabels(summary);
      renderDatasetFilter(summary);
      $('stat-cases').textContent = cases.length;
      $('stat-cases-label').textContent = isEnglishTraining() ? '训练数据' : '完整对话';
      $('stat-duration').textContent = isInference() ? `${Math.round(cases.reduce((n,c)=>n+c.seconds,0))} unit时长` : `${(summary.audio_seconds / 3600).toFixed(2)} 小时`;
      $('stat-replies').textContent = isInference() ? cases.reduce((n,c)=>n+(c.output_units||0),0) : summary.assistant_responses;
      $('stat-replies-label').textContent = isInference() ? '实际发言单元' : '助手回复';
      $('stat-interrupts').textContent = isInference() ? cases.reduce((n,c)=>n+(c.interruptions||0),0) : summary.interruptions;
      $('stat-interrupts-label').textContent = isInference() ? '模拟插话次数' : '真实打断';
      $('dataset-subtitle').textContent = isInference() ? `${inferenceName()} 实际推理 · 预热后与历史记录可切换 · 保留真实控制与播放结果` : `${summary.version_label} · ${trainingName()}`;
      $('dataset-footer').textContent = isInference() ? '实际推理记录 · 非训练目标 · 记录原生动作与播放规则' : `${trainingName()} · 仅 Thinker · 无 Talker / S3 目标 · ${cases.length} 条完整对话 · 未截断${summary.runtime_compatibility_exception_count ? ` · ${summary.runtime_compatibility_exception_count} 处 Demo 兼容备注` : ''}`;
      $('sequence').querySelector('h2').textContent = isInference() ? `${inferenceName()} 实际推理 token 序列` : `${trainingName()} · 完整训练 token 序列`;
      $('sequence-caption').textContent = isInference() ? (sourceMode === 'gander' ? '记录实际输入与生成 · 包含实际系统前缀' : '记录实际输入与生成 · 包含系统和参考音频前缀') : '由本地训练 collator 导出 · 所有训练位置均保留';
      $('sequence-description').hidden = isMineCPM();
      $('sequence-description').textContent = isMineCPM() ? '' : isInference() ? `${sourceMode === 'gander' ? '从实际系统前缀' : '从系统和参考声音前缀'}到最后一个 unit，完整展示本次实际记录的所有 token，包括回放进度中尚未处理完的 unit。橙色标记正在处理，蓝色标记声音来源；这些状态随播放更新，音频特征没有词表 ID。` : `${isMiniTraining() ? miniSequenceDescription() + ' 本数据以整段 full-context 序列导出，关闭训练滑窗与 KV 删除；配置中的 context_max_units=128 不表示已启用 128u 滑窗。' : '按 Gander 原生规则，每个常规发言 unit 最多 8 个正文 token，interrupt 独占控制 unit；实际训练仍使用所配的 128u 上下文记忆。'} 完整展示模型输入与监督标签；未播放的训练正文以虚线标注。`;
      $('timeline-caption').textContent = isInference() ? '录音时间轴、模型处理与声音播放分别记录' : '播放、事件和训练 unit 联动';
      $('timeline-description').textContent = isInference() ? '模型处理轨道按实际调用时间展开：处理输入、生成文字、生成语音码、合成波形。录音时间轴继续推进时，模型可能仍在处理较早的 unit。点击处理轨道或 token 可定位其实际处理时刻。' : isMineCPM() ? '音轨来自每秒 16 token 供字下的真实 dots.tts 双向流式合成，包含真实供给不足造成的播放空档。训练正文的生成 unit 与音频实际读到的位置分开；已生成不代表已播放。点击音轨、事件或 unit 定位同一条完整对话。' : isTurnRelative() ? '音轨和事件始终使用原始实际播放时间；点击正文 token 定位其原声学起点，生成 unit 可提前一格，并不表示该文字已在该时刻被听到。unit 定位按钮与麦克风输入仍按全局 1 秒时间轴。' : '音轨展示原始实际播放时间。点击音轨或事件定位音频和 unit；点击 token 或选择 unit，也会定位到相同的会话时间。';
      $('event-description').textContent = isInference() ? '片段中的生成文字用于查看模型输出，不是逐字音频字幕。是否停声以实际播放结果为准；没有人为插入 interrupt。' : `事件台词和类别仅用于人工审查，不作为额外文本输入模型。用户打断起声和训练 ${trainingCloseName()} 可能位于不同 unit；事件列表提供两个定位入口。`;
      $('input-description').textContent = isInference() ? '单声道：Qwen3-TTS 合成的模拟用户输入，其余时刻为静音。' : isMineCPM() ? '单声道：本版用户输入；不混入助手语音，本批关闭附和、旁话和背景音。' : '单声道：用户、附和、旁话和环境声。';
      $('case-json-title').textContent = isInference() ? '查看完整静态推理事件 JSON' : `查看 ${trainingName()} 训练 JSON`;
      $('case-download').textContent = isInference() ? '下载推理事件记录' : `下载 ${isMineCPM() ? 'MineCPM V1.4' : isMiniTraining() ? 'MiniCPM-o TAIL' : 'Gander'} 训练 JSON`;
      $('runtime-panel').hidden = !isInference();
      $('locate-output').hidden = !isInference();
      $('follow-label').textContent = isInference() ? '序列跟随模型实际处理' : '序列随音频在框内滚动';
      $('locate-audio').textContent = isInference() ? '定位模型处理 unit' : '定位当前 unit';

      $('playback-note').textContent = isInference() ? `用户录音复用 Qwen3-TTS 合成结果，助手由 ${inferenceName()} 实际生成。录音进度、实际处理与播放分别记录。序列默认跟随橙色的模型处理 unit；蓝色表示当前声音来源。输入积压和音频排队单独显示，生成文字不是逐字字幕。` : `原始音频继续只播放实际保留的部分；训练序列包含按本格式分块后、打断前可生成的正文，包括未播放文字。剩余完整计划另存，不进入模型上下文。${isMiniTraining() ? (isMiniInterruptExtension() ? '历史扩展在打断处用 interrupt 监督取消旧轮，需配套推理实现。' : `${isDelayedEOS() ? `${dataVersion} 正常结束另有独立的空正文 EOS unit；打断处仍沿用原停声桶。${isTurnRelative() ? '正文生成 unit 不是原声学播放时间；用户实际听到哪里仍按原 source 时间与录音判断。' : ''}` : 'V1.1 正常和提前结束均在 SPEAK 块末监督 <|turn_eos|>。'} 不监督 interrupt。EOS 结束旧轮生成，已有语音队列仍会收尾；混音中的精确停声沿用源录音，不代表原生播放器必然在同一采样点停声。`) : '打断按原停声所在的 1u 控制块映射。'} 这些是重建的训练目标调度，不是实测模型执行时间。`;
      if (isMineCPM()) $('playback-note').textContent = '本页展示重新合成并更新时序的 V1.4 dots.tts 双向流式音频。正文按每秒 16 token 模拟生成，完整回答、纳入训练的文字与实际已播文字分别展示。文字结束后仍可有多轮空 SPEAK；tts_eos 由真实播放完成事件作为外部输入插入（label=-100）。打断 unit 仅 LISTEN 并取消旧语音，不插入 tts_eos，也不使用 turn_eos。这里展示训练 collator 的真实序列与真实 TTS 记录，不是 Thinker 的推理速度。';
      const legend = document.querySelector('.timeline-legend');
      legend.innerHTML = isInference() ? '<span class="leg-user">用户输入</span><span class="leg-interrupt">用户插话 / 模型interrupt</span><span class="tl-phase-input">处理输入</span><span class="tl-phase-text">生成文字</span><span class="tl-phase-speech">生成语音码</span><span class="tl-phase-wave">合成波形</span><span class="leg-assistant">实际播放</span>' : '<span class="leg-user">用户</span><span class="leg-backchannel">附和</span><span class="leg-interrupt">用户打断</span><span class="leg-assistant">助手</span><span class="leg-aside">旁话</span><span class="leg-ambient">环境音</span>' + (isDelayedEOS() ? '<span class="leg-synthetic">训练尾部补齐</span>' : '');
      $('case-search').disabled = false;
      $('data-version').disabled = false;
      renderMineCPM();
      options('',preferredCase || currentBatch()[0]?.id); choose();
    } catch(e) { if (loading === request) $('case-info').textContent = `案例列表读取失败：${e.message}`; }
  }
  $('data-source').addEventListener('change',e => {
    const preferredInfo = cases.find(c => c.id === loadedId);
    if (e.target.value === 'minecpm-v14') {
      sourceMode = 'training-minicpm-en'; dataVersion = 'V1.4';
    } else {
      sourceMode = e.target.value;
      if (isMiniTraining() && (!dataVersion || dataVersion === 'V1.4'))
        dataVersion = dataVersions.length > 1 ? [...dataVersions].reverse().find(v => v.id !== 'V1.4')?.id || 'V1.3' : 'V1.3';
    }
    loadSource(preferredInfo);
  });
  $('data-version').addEventListener('change',e=>{
    const preferredInfo = cases.find(c=>c.id===loadedId);
    dataVersion=e.target.value; loadSource(preferredInfo);
  });
  $('training-timeline-progress').addEventListener('input',e=>seekTo(Number(e.target.value)));
  document.addEventListener('training-token-audio-start',()=>{ pauseTrainingTail(); syncDialogueControls(); });
  loadSource();
})();
