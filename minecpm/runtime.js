"use strict";
(() => {
  const names = {input:'处理输入 / 决策',text:'生成文字',speech:'生成语音码',wave:'合成波形 / 返回',control:'提交控制 / 返回'};
  function phases(step) {
    const start=step.call_start_s, end=step.result_available_s, result=[];
    let at=start;
    const add=(until,kind)=>{
      until=Math.min(end,Math.max(at,until ?? at));
      if(until>at) result.push({unit:step.unit,start:at,end:until,kind,label:names[kind]});
      at=until;
    };
    add(step.action_ready_s ?? end,'input');
    if(!step.is_listen && !step.interrupted) add(step.text_ready_s,'text');
    if(step.speech_codes_ready_s!=null) add(step.speech_codes_ready_s,'speech');
    add(end,step.speech_codes_ready_s!=null?'wave':'control');
    return result;
  }
  function at(runtime,time) {
    const steps=runtime.steps, through=runtime.input_observed_through_s;
    const captured=Math.min(time,through);
    const processing=steps.find(s=>s.call_start_s<=time && time<s.result_available_s) || null;
    const completed=steps.filter(s=>s.result_available_s<=time).at(-1) || null;
    const waiting=steps.filter(s=>s.input_end_s<=captured && s.call_start_s>time);
    const phase=processing ? phases(processing).find(p=>p.start<=time && time<p.end) : null;
    // Replay only events already observed at this point. Future cancellations
    // must not erase the backlog that really existed before an interruption.
    let queued=[];
    for(const event of runtime.playback_events) {
      const when=event.available_t ?? event.t;
      if(when>time) continue;
      if(event.type==='native_playback_cancel') {
        queued=queued.filter(s=>s.start<event.t).map(s=>({...s,end:Math.min(s.end,event.t)}));
      } else if(event.type==='playback_delta') {
        queued.push({unit:event.source_step_index,start:event.start_t,end:event.end_t});
      }
    }
    queued=queued.filter(s=>s.end>time).map(s=>({...s,remaining:s.end-Math.max(time,s.start)}));
    return {time,processing,completed,phase,waiting,queued,
      captureUnit:time<through?Math.floor(time):null,
      lastCaptureUnit:steps.at(-1)?.unit,
      inputPending:waiting.reduce((n,s)=>n+s.input_end_s-s.input_start_s,0),
      audioPending:queued.reduce((n,s)=>n+s.remaining,0),
      audible:queued.filter(s=>s.start<=time).map(s=>s.unit)};
  }
  window.NativeRuntime={at,phases};
})();
