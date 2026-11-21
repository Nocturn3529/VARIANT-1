import {useEffect, useId} from "react";
import {usePlatformState} from "./store";
import {Button} from "./ui/Button";
import {SettingsSection,SettingToggleRow} from "./ui/Settings";
import {requestModelOptions, useSessionContextState, type ComposerModelProvider} from "./sessionContextStore";
import {useSessionState} from "./state/sessionStore";
import {editProviderRouting,revertProviderRouting,refreshProviderRouting,saveProviderRouting,useProviderRouting,ROUTING_PROFILES,type RecoveryRoute} from "./providerRoutingStore";
const profileNames={internal_json:"Structured internal work",internal_prose:"Internal prose",vision:"Vision"};

function RouteChain({label,rows,disabled,catalog,onChange}:{label:string;rows:RecoveryRoute[];disabled:boolean;catalog:ComposerModelProvider[];onChange:(rows:RecoveryRoute[])=>void}){
  const {config}=usePlatformState();const providers=config.providers || [];
  const listId=useId();
  const edit=(index:number,row:RecoveryRoute)=>onChange(rows.map((prior,i)=>i===index?row:prior));
  function move(index:number,offset:number){const next=[...rows];[next[index],next[index+offset]]=[next[index+offset],next[index]];onChange(next);}
  return <div className="routing-chain">
    {!rows.length?<p className="settings-empty">{label==="Main backups"?"No backup routes configured.":"Uses the main route and its configured backups."}</p>:null}
    {rows.map((row,index)=>{
      const models=catalog.find(provider=>provider.mode===row.mode && provider.id===row.provider)?.models || [];
      const selected=models.find(model=>model.id===row.model);
      const efforts=selected?.selectable ? selected.reasoningEfforts : [];
      const toggle=selected?.selectable && selected.reasoningToggle;
      const setting=row.reasoning_enabled===undefined ? row.reasoning_effort || "" : row.reasoning_enabled ? "toggle:on" : "toggle:off";
      return <fieldset key={index} disabled={disabled} className="routing-route"><legend>{label} · route {index+1}</legend>
      <label>Mode<select aria-label={`${label} route ${index+1} mode`} value={row.mode} onChange={event=>edit(index,{mode:event.target.value as RecoveryRoute["mode"],provider:event.target.value==="local"?"local":"",model:""})}><option value="cloud">Cloud</option><option value="local">Local</option></select></label>
      <label>Provider<select aria-label={`${label} route ${index+1} provider`} value={row.provider} disabled={disabled || row.mode==="local"} onChange={event=>{
        const id=event.target.value, configured=providers.find(provider=>provider.name===id)?.model || "";
        const group=catalog.find(provider=>provider.id===id && provider.mode===row.mode);
        edit(index,{mode:row.mode,provider:id,model:group && !group.models.some(model=>model.selectable && model.id===configured) ? "" : configured});
      }}>
        {row.mode==="local"?<option value="local">Local runtime</option>:<><option value="">Choose provider</option>{providers.map(provider=><option key={provider.name} value={provider.name}>{provider.display_name}{provider.configured?"":" · not configured"}</option>)}{row.provider && !providers.some(provider=>provider.name===row.provider)?<option value={row.provider}>{row.provider} · unavailable</option>:null}</>}
      </select></label>
      <label>Model<input aria-label={`${label} route ${index+1} model`} list={listId+index} value={row.model} maxLength={512} placeholder="Explicit model ID" onChange={event=>edit(index,{mode:row.mode,provider:row.provider,model:event.target.value})}/>
        <datalist id={listId+index}>{models.filter(model=>model.selectable).map(model=><option key={model.id} value={model.id}/>)}</datalist>
      </label>
      {selected?.selectable===false && <p role="status">{selected.detail || "Saved model unavailable. Choose another model."}</p>}
      <label>{toggle ? "Reasoning" : "Reasoning effort"}<select aria-label={`${label} route ${index+1} effort`} value={setting} onChange={event=>{
        const value=event.target.value, next:RecoveryRoute={mode:row.mode,provider:row.provider,model:row.model};
        if (value==="toggle:on" || value==="toggle:off") next.reasoning_enabled=value==="toggle:on";
        else if(value)next.reasoning_effort=value;
        edit(index,next);
      }}>
        <option value="">Provider default{selected?.defaultReasoningEffort ? " · "+selected.defaultReasoningEffort :
          toggle && selected.reasoningDefaultEnabled!==undefined ? (selected.reasoningDefaultEnabled ? " · on" : " · off") : ""}</option>
        {toggle ? <><option value="toggle:on">On</option><option value="toggle:off">Off</option></> :
          row.reasoning_enabled!==undefined ? <option value={setting} disabled>{row.reasoning_enabled ? "On" : "Off"} · saved</option> : null}
        {row.reasoning_effort && !efforts.includes(row.reasoning_effort) ? <option value={row.reasoning_effort} disabled>{row.reasoning_effort} · {selected ? "unsupported" : "saved"}</option> : null}
        {efforts.map(effort=><option key={effort} value={effort}>{effort}</option>)}
      </select></label>
      <div className="routing-route__actions"><Button aria-label={`Move ${label} route ${index+1} earlier`} disabled={disabled || index===0} onClick={()=>move(index,-1)}>↑</Button><Button aria-label={`Move ${label} route ${index+1} later`} disabled={disabled || index===rows.length-1} onClick={()=>move(index,1)}>↓</Button><Button onClick={()=>onChange(rows.filter((_,i)=>i!==index))}>Remove</Button></div>
    </fieldset>})}
    <Button disabled={disabled || rows.length>=4} onClick={()=>onChange([...rows,{mode:"cloud",provider:"",model:""}])}>Add route</Button>
  </div>;
}
export function ProviderRoutingSettings(){
  const state=useProviderRouting(),draft=state.draft,busy=!!state.pending.config;
  useEffect(()=>{if(state.connected)refreshProviderRouting();},[state.connected]);
  const sessions=useSessionState(), sessionId=sessions.displayedSessionId || sessions.activeSessionId;
  const context=useSessionContextState();
  const catalog=context.sessionId===sessionId ? context.modelProviders : [];
  useEffect(()=>{if(state.connected && sessionId)requestModelOptions(sessionId);},[state.connected,sessionId]);
  const disabled=busy || !state.connected || !state.config;
  return <div className="provider-routing-settings">
    <SettingsSection title="Provider recovery" description="Choose explicit backups for provider failures. Recovery starts disabled and does not replay completed tool actions." action={<Button disabled={busy || !state.connected} onClick={()=>{
      refreshProviderRouting();
      if(sessionId)requestModelOptions(sessionId,{refresh:true});
    }}>Refresh saved settings</Button>}>
      <SettingToggleRow title="Enable configured recovery" checked={draft.enabled} disabled={disabled} onChange={enabled=>editProviderRouting({...draft,enabled})}/>
      <div className="routing-budgets"><label>Maximum attempts<input aria-label="Maximum attempts" type="number" min={1} max={12} step={1} disabled={disabled} value={Number.isNaN(draft.max_attempts)?"":draft.max_attempts} onChange={event=>editProviderRouting({...draft,max_attempts:event.target.valueAsNumber})}/></label><label>Wait budget (seconds)<input aria-label="Wait budget in seconds" type="number" min={0} max={600} disabled={disabled} value={Number.isNaN(draft.max_wait_seconds)?"":draft.max_wait_seconds} onChange={event=>editProviderRouting({...draft,max_wait_seconds:event.target.valueAsNumber})}/></label></div>
    </SettingsSection>
    <SettingsSection title="Main backups" description="The chat’s chosen model remains primary. These routes are tried in order when recovery is enabled."><RouteChain label="Main backups" rows={draft.fallback_routes} disabled={disabled} catalog={catalog} onChange={fallback_routes=>editProviderRouting({...draft,fallback_routes})}/></SettingsSection>
    {ROUTING_PROFILES.map(profile=><SettingsSection key={profile} title={profileNames[profile]} description="An optional ordered chain for this internal request profile. A configured chain supplies its first route and backups."><RouteChain label={profileNames[profile]} rows={draft.auxiliary_routes[profile] || []} disabled={disabled} catalog={catalog} onChange={routes=>editProviderRouting({...draft,auxiliary_routes:{...draft.auxiliary_routes,[profile]:routes}})}/></SettingsSection>)}
    {!state.connected?<p role="status">Backend disconnected. Your draft is retained.</p>:null}
    {state.error?<p className="settings-feedback is-error" role="alert">{state.error}</p>:null}{state.notice?<p className="settings-feedback" role="status">{state.notice}</p>:null}
    <div className="settings-save-actions"><Button tone="primary" disabled={disabled || !state.dirty} onClick={()=>saveProviderRouting()}>{busy?"Waiting…":"Save routing"}</Button><Button disabled={busy || !state.config || !state.dirty} onClick={()=>revertProviderRouting()}>Revert draft</Button>{!draft.enabled?<span>Configured chains remain inactive while recovery is off.</span>:null}</div>
  </div>;
}
