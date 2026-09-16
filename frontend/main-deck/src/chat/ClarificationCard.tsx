import {useEffect, useMemo, useRef} from "react";
import {refreshClarification, submitClarification, updateClarificationDraft, useClarificationState} from "../state/clarificationStore";

export function ClarificationCard() {
  const {pending,submitting,connected,error,draft}=useClarificationState();
  const section=useRef<HTMLElement>(null);
  const index=Math.min(draft.index, Math.max(0,(pending?.questions.length || 1)-1));
  const question=pending?.questions[index];
  useEffect(()=>{
    const card=section.current;
    if (!pending || !card) return;
    const active=card.ownerDocument.activeElement as HTMLInputElement | null;
    if (!active || active===card.ownerDocument.body || (active.tagName==="TEXTAREA" && !active.value)) {
      card.querySelector<HTMLElement>("header")?.focus({preventScroll:true});
    }
  },[pending?.id]);
  const answers=useMemo(()=>{
    const output: Record<string,string|string[]>={};
    for(const item of pending?.questions || []) {
      const values=[...(draft.selected[item.id] || [])],custom=String(draft.other[item.id] || "").trim();
      if(custom)values.push(custom);
      if(values.length)output[item.id]=item.multiSelect ? values : values[0];
    }
    return output;
  },[pending,draft]);
  if(!pending || !question) return error ? <section className="runtime-clarification" aria-label="Question status">
    <p role="alert">{error}</p><button type="button" disabled={!connected} onClick={()=>refreshClarification()}>Refresh questions</button>
  </section> : null;
  const values=draft.selected[question.id] || [],hasAnswer=values.length>0 || !!String(draft.other[question.id] || "").trim();
  const last=index===pending.questions.length-1;
  const toggle=(label:string)=>updateClarificationDraft(pending.id,current=>{
    const active=current.selected[question.id] || [];
    return {...current,selected:{...current.selected,[question.id]:question.multiSelect ? (active.includes(label) ? active.filter(value=>value!==label) : [...active,label]) : [label]},
      other:question.multiSelect ? current.other : {...current.other,[question.id]:""}};
  });
  return <section ref={section} className="runtime-clarification deck-instrument" aria-label="Clarifying questions" data-question-chat={pending.chatId} data-question-id={pending.id}
    onKeyDown={event=>{
      if(submitting || (event.target as HTMLElement).closest("input,textarea")) return;
      const option=question.options[Number(event.key)-1];
      if(/^[1-4]$/.test(event.key) && option){event.preventDefault();toggle(option.label);}
    }}>
    <header className="runtime-clarification__header" tabIndex={-1}>
      <span>{index+1}/{pending.questions.length}</span><strong>{question.header}</strong>
    </header>
    <p className="runtime-clarification__question" aria-live="polite">{question.question}</p>
    <div className="runtime-clarification__options">
      {question.options.map((option,optionIndex)=><button key={option.label} type="button" disabled={submitting}
        className={values.includes(option.label) ? "active" : undefined} aria-pressed={values.includes(option.label)} onClick={()=>toggle(option.label)}>
        <span><strong>{option.label}</strong><small>{option.description}</small></span><kbd>{optionIndex+1}</kbd>
      </button>)}
      <label className={draft.other[question.id] ? "active" : undefined}>
        <strong>Other</strong><input aria-label="Your answer" disabled={submitting} value={draft.other[question.id] || ""} maxLength={500} placeholder="Type your own answer"
          onFocus={()=>{if(!question.multiSelect)updateClarificationDraft(pending.id,current=>({...current,selected:{...current.selected,[question.id]:[]}}));}}
          onChange={event=>{
            const value=event.target.value;
            updateClarificationDraft(pending.id,current=>({...current,other:{...current.other,[question.id]:value},
              selected:question.multiSelect ? current.selected : {...current.selected,[question.id]:[]}}));
          }}/>
      </label>
    </div>
    {error ? <small className="runtime-clarification__error" role="alert">{error}</small> : null}
    {!connected ? <small className="runtime-clarification__error">Reconnect to send. Your answer is kept.</small> : null}
    <footer className="runtime-clarification__actions">
      <button type="button" disabled={submitting || index===0} onClick={()=>updateClarificationDraft(pending.id,current=>({...current,index:index-1}))}>Back</button>
      <span/>
      <button type="button" disabled={submitting || !connected} onClick={()=>submitClarification(pending.id,{},true)}>Skip</button>
      <button type="button" disabled={submitting || !hasAnswer || !connected} onClick={()=>{
        if(!last)updateClarificationDraft(pending.id,current=>({...current,index:index+1}));
        else submitClarification(pending.id,answers);
      }}>{submitting ? "Sending…" : last ? "Submit" : "Next"}</button>
    </footer>
  </section>;
}
