import {useEffect, useMemo, useState, type RefObject} from "react";
import {requestModelOptions, useSessionContextState, type ComposerModelOption, type ComposerModelProvider} from "../sessionContextStore";
import {AnchoredPopover} from "../ui/AnchoredPopover";
import {Icon} from "../ui/Icon";
import {shortModelName} from "./receipt";

const EFFORT_LABELS: Readonly<Record<string,string>> = {minimal:"Minimal",low:"Low",medium:"Medium",high:"High",xhigh:"XHigh",max:"Max",ultra:"Ultra"};
const DEFAULT_VISIBLE_MODELS = 3;
export type ComposerModelSelection = Readonly<{mode:"local"|"cloud";provider:string;model:string;label:string;reasoningEffort?:string}>;
export function reasoningEffortLabel(value:string):string {return EFFORT_LABELS[value] || value;}

function isCurrentModel(context:ReturnType<typeof useSessionContextState>,provider:ComposerModelProvider,model:ComposerModelOption):boolean {
  const normalize=(value:string)=>value.trim().replace(/\\/g,"/").toLowerCase();
  return context.route===provider.mode && context.provider===(provider.mode==="local" ? "local" : provider.id)
    && (provider.mode==="local" ? normalize(context.model)===normalize(model.id) : context.model===model.id);
}

export function ModelPickerControl({sessionId,open,disabled,pendingLabel,buttonRef,onToggle,onClose,onChoose,onChooseReasoning}: {
  sessionId:string|null;
  open:boolean;
  disabled:boolean;
  pendingLabel?:string;
  buttonRef:RefObject<HTMLButtonElement|null>;
  onToggle:()=>void;
  onClose:()=>void;
  onChoose:(selection:ComposerModelSelection)=>void;
  onChooseReasoning:(effort:string)=>void;
}) {
  const context=useSessionContextState();
  const [query,setQuery]=useState("");
  const [expanded,setExpanded]=useState<Set<string>>(new Set());
  const [collapsed,setCollapsed]=useState<Set<string>>(new Set());
  const [options,setOptions]=useState("");
  const ownsContext=context.sessionId===sessionId;
  const model=ownsContext ? shortModelName(context.model) || (context.route==="cloud" ? "Cloud" : "Local") : "Model";
  const effort=ownsContext ? reasoningEffortLabel(context.reasoningEffort) : "";
  const label=pendingLabel || (effort ? `${model} · ${effort}` : model);
  const groups=useMemo(()=>{
    const search=query.trim().toLowerCase();
    if(!ownsContext) return [];
    return context.modelProviders.flatMap(provider=>{
      const models=provider.models.filter(model=>!search || `${provider.name} ${model.label} ${model.id}`.toLowerCase().includes(search));
      return models.length ? [{...provider,models}] : [];
    });
  },[context.modelProviders,ownsContext,query]);

  useEffect(()=>{
    if(!open || !sessionId) return;
    setQuery("");setExpanded(new Set());setCollapsed(new Set());setOptions("");requestModelOptions(sessionId);
  },[open,sessionId]);

  const close=()=>{onClose();buttonRef.current?.focus();};
  const choose=(provider:ComposerModelProvider,model:ComposerModelOption,reasoningEffort?:string)=>{
    if(disabled || !ownsContext || !model.selectable) return;
    onChoose({mode:provider.mode,provider:provider.id,model:model.id,label:model.label,...(reasoningEffort ? {reasoningEffort} : {})});close();
  };
  const toggleProvider=(key:string)=>setExpanded(current=>{const next=new Set(current);if(next.has(key))next.delete(key);else next.add(key);return next;});

  return <div className="composer__model-control">
    <button type="button" className="model-button" id="model-button" ref={buttonRef}
      aria-haspopup="dialog" aria-expanded={open} aria-controls="model-menu" aria-label="Choose model"
      aria-busy={!!pendingLabel} title={pendingLabel || (ownsContext && context.model ? `${context.provider} · ${context.model}` : "Choose model")}
      disabled={disabled} onClick={onToggle}>
      {pendingLabel ? <i className="composer-spinner" aria-hidden="true"/> : <span className="model-spark" aria-hidden="true">✦</span>}
      <span className="model-button__label">{label}</span><Icon name="down"/>
    </button>
    {open ? <AnchoredPopover anchor={buttonRef} className="model-picker" id="model-menu" label="Choose model" onClose={onClose} focusSelector="input" width={350}>
      <header className="model-picker__search">
        <Icon name="search"/><input type="search" aria-label="Search models" placeholder="Find a model…" value={query} onChange={event=>{setQuery(event.target.value);setOptions("");setCollapsed(new Set());}}
          onKeyDown={event=>{
            if(event.key!=="ArrowDown" || event.nativeEvent.isComposing) return;
            event.preventDefault();event.currentTarget.closest(".model-picker")?.querySelector<HTMLButtonElement>('[data-model-option]:not(:disabled)')?.focus();
          }}/>
        <button type="button" aria-label="Refresh models" title="Refresh models" disabled={context.modelOptionsStatus==="loading"} onClick={()=>requestModelOptions(sessionId,{refresh:true})}><Icon name="refresh"/></button>
      </header>
      {context.modelOptionsStatus==="loading" ? <p className="model-picker__status" role="status"><i className="composer-spinner" aria-hidden="true"/>Loading models…</p> : null}
      {context.modelOptionsStatus==="error" ? <div className="model-picker__error" role="alert"><span>{context.modelOptionsError || "Could not load models."}</span><button type="button" onClick={()=>requestModelOptions(sessionId,{refresh:true})}>Retry</button></div> : null}
      <div className="model-picker__groups" onKeyDown={event=>{
        if(event.nativeEvent.isComposing || !["ArrowDown","ArrowUp","Home","End"].includes(event.key))return;
        const rows=[...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
        const index=rows.indexOf(event.target as HTMLButtonElement);if(index<0 || !rows.length)return;
        event.preventDefault();
        const next=event.key==="Home" ? 0 : event.key==="End" ? rows.length-1 : (index+(event.key==="ArrowDown" ? 1 : -1)+rows.length)%rows.length;
        rows[next].focus();
      }}>
        {groups.map(provider=>{
          const groupKey=`${provider.mode}:${provider.id}`,isExpanded=expanded.has(groupKey)||!!query.trim();
          const isCollapsed=collapsed.has(groupKey);
          const currentIndex=provider.models.findIndex(model=>isCurrentModel(context,provider,model));
          const visible=isExpanded ? provider.models : provider.models.filter((_,index)=>index<DEFAULT_VISIBLE_MODELS||index===currentIndex);
          return <section className="model-picker__group" key={groupKey}>
            <button type="button" className="model-picker__provider" aria-expanded={!isCollapsed} title={provider.warning||provider.description} onClick={()=>setCollapsed(current=>{const next=new Set(current);if(next.has(groupKey))next.delete(groupKey);else next.add(groupKey);return next;})}>
              <strong>{provider.name}</strong><small>{provider.models.length}</small><Icon name={isCollapsed ? "down" : "up"}/>
            </button>
            {!isCollapsed&&visible.map(model=>{
              const current=isCurrentModel(context,provider,model),key=`${groupKey}:${model.id}`;
              return <div className="model-picker__row-wrap" key={key}>
                <div className="model-picker__row">
                  <button type="button" className={`model-picker__model${current ? " active" : ""}`} data-model-option="" aria-pressed={current}
                    disabled={disabled||!model.selectable} title={model.detail||model.id} onClick={()=>choose(provider,model)}>
                    <span><strong>{model.label}</strong>{model.detail||model.label!==model.id ? <small>{model.detail||model.id}</small> : null}</span>
                    {current&&context.reasoningEffort ? <em>{reasoningEffortLabel(context.reasoningEffort)}</em> : null}
                    <b aria-label={current ? "Current model" : undefined}>{current ? "✓" : ""}</b>
                  </button>
                  {model.reasoningEfforts.length ? <button type="button" className="model-picker__options-trigger" aria-expanded={options===key} aria-label={`${model.label} options`} disabled={disabled||!model.selectable}
                    onClick={()=>setOptions(value=>value===key ? "" : key)}><Icon name={options===key ? "up" : "down"}/></button> : null}
                </div>
                {options===key ? <div className="model-picker__effort-menu" role="group" aria-label={`${model.label} reasoning effort`}>
                  <span>Reasoning effort</span><div>{model.reasoningEfforts.map(effort=><button type="button" key={effort} aria-pressed={current&&context.reasoningEffort===effort} disabled={disabled||!model.selectable}
                    onClick={()=>{if(current){onChooseReasoning(effort);close();}else choose(provider,model,effort);}}>{reasoningEffortLabel(effort)}</button>)}</div>
                </div> : null}
              </div>;
            })}
            {!isCollapsed&&provider.models.length>DEFAULT_VISIBLE_MODELS&&!query.trim() ? <button type="button" className="model-picker__more" onClick={()=>toggleProvider(groupKey)}>{isExpanded ? "Show fewer" : `Show all ${provider.models.length}`}</button> : null}
          </section>;
        })}
      </div>
      {!groups.length&&context.modelOptionsStatus!=="loading" ? <p className="model-picker__empty">{query ? "No models match this search." : "No connected models."}</p> : null}
    </AnchoredPopover> : null}
  </div>;
}
