import {useEffect} from "react";
import {usePlatformState} from "./store";
import {Button} from "./ui/Button";
import {SettingsSection,SettingToggleRow} from "./ui/Settings";
import {editProviderRouting,revertProviderRouting,refreshProviderRouting,saveProviderRouting,useProviderRouting,ROUTING_PROFILES,type RecoveryRoute} from "./providerRoutingStore";
const profileNames={internal_json:"Structured internal work",internal_prose:"Internal prose",vision:"Vision"};

function RouteChain({label,rows,disabled,onChange}:{label:string;rows:RecoveryRoute[];disabled:boolean;onChange:(rows:RecoveryRoute[])=>void}){
  const {config}=usePlatformState();const providers=config.providers || [];
  const edit=(index:number,row:RecoveryRoute)=>onChange(rows.map((prior,i)=>i===index?row:prior));
  function move(index:number,offset:number){const next=[...rows];[next[index],next[index+offset]]=[next[index+offset],next[index]];onChange(next);}
  return <div className="routing-chain">
    {!rows.length?<p className="settings-empty">{label==="Main backups"?"No backup routes configured.":"Uses the main route and its configured backups."}</p>:null}
    {rows.map((row,index)=><fieldset key={index} disabled={disabled} className="routing-route"><legend>{label} · route {index+1}</legend>
      <label>Mode<select aria-label={`${label} route ${index+1} mode`} value={row.mode} onChange={event=>edit(index,{...row,mode:event.target.value as RecoveryRoute["mode"],provider:event.target.value==="local"?"local":"",model:"",reasoning_effort:undefined})}><option value="cloud">Cloud</option><option value="local">Local</option></select></label>
      <label>Provider<select aria-label={`${label} route ${index+1} provider`} value={row.provider} disabled={disabled || row.mode==="local"} onChange={event=>edit(index,{...row,provider:event.target.value,model:providers.find(provider=>provider.name===event.target.value)?.model || "",reasoning_effort:undefined})}>
        {row.mode==="local"?<option value="local">Local runtime</option>:<><option value="">Choose provider</option>{providers.map(provider=><option key={provider.name} value={provider.name}>{provider.display_name}{provider.configured?"":" · not configured"}</option>)}{row.provider && !providers.some(provider=>provider.name===row.provider)?<option value={row.provider}>{row.provider} · unavailable</option>:null}</>}
      </select></label>
      <label>Model<input aria-label={`${label} route ${index+1} model`} value={row.model} maxLength={512} placeholder="Explicit model ID" onChange={event=>edit(index,{...row,model:event.target.value})}/></label>
      <label>Reasoning effort<select aria-label={`${label} route ${index+1} effort`} value={row.reasoning_effort || ""} onChange={event=>{const next={...row};if(event.target.value)next.reasoning_effort=event.target.value;else delete next.reasoning_effort;edit(index,next);}}><option value="">Provider default</option>{row.reasoning_effort && !(providers.find(provider=>provider.name===row.provider)?.reasoning_efforts || []).includes(row.reasoning_effort)?<option value={row.reasoning_effort} disabled>{row.reasoning_effort} · unsupported</option>:null}{row.mode==="cloud"?(providers.find(provider=>provider.name===row.provider)?.reasoning_efforts || []).map(effort=><option key={effort} value={effort}>{effort}</option>):null}</select></label>
      <div className="routing-route__actions"><Button aria-label={`Move ${label} route ${index+1} earlier`} disabled={disabled || index===0} onClick={()=>move(index,-1)}>↑</Button><Button aria-label={`Move ${label} route ${index+1} later`} disabled={disabled || index===rows.length-1} onClick={()=>move(index,1)}>↓</Button><Button onClick={()=>onChange(rows.filter((_,i)=>i!==index))}>Remove</Button></div>
    </fieldset>)}
    <Button disabled={disabled || rows.length>=4} onClick={()=>onChange([...rows,{mode:"cloud",provider:"",model:""}])}>Add route</Button>
  </div>;
}
export function ProviderRoutingSettings(){
  const state=useProviderRouting(),draft=state.draft,busy=!!state.pending.config;
  useEffect(()=>{if(state.connected)refreshProviderRouting();},[state.connected]);
  const disabled=busy || !state.connected || !state.config;
  return <div className="provider-routing-settings">
    <SettingsSection title="Provider recovery" description="Choose explicit backups for provider failures. Recovery starts disabled and does not replay completed tool actions." action={<Button disabled={busy || !state.connected} onClick={()=>refreshProviderRouting()}>Refresh saved settings</Button>}>
      <SettingToggleRow title="Enable configured recovery" checked={draft.enabled} disabled={disabled} onChange={enabled=>editProviderRouting({...draft,enabled})}/>
      <div className="routing-budgets"><label>Maximum attempts<input aria-label="Maximum attempts" type="number" min={1} max={12} step={1} disabled={disabled} value={Number.isNaN(draft.max_attempts)?"":draft.max_attempts} onChange={event=>editProviderRouting({...draft,max_attempts:event.target.valueAsNumber})}/></label><label>Wait budget (seconds)<input aria-label="Wait budget in seconds" type="number" min={0} max={600} disabled={disabled} value={Number.isNaN(draft.max_wait_seconds)?"":draft.max_wait_seconds} onChange={event=>editProviderRouting({...draft,max_wait_seconds:event.target.valueAsNumber})}/></label></div>
    </SettingsSection>
    <SettingsSection title="Main backups" description="The chat’s chosen model remains primary. These routes are tried in order when recovery is enabled."><RouteChain label="Main backups" rows={draft.fallback_routes} disabled={disabled} onChange={fallback_routes=>editProviderRouting({...draft,fallback_routes})}/></SettingsSection>
    {ROUTING_PROFILES.map(profile=><SettingsSection key={profile} title={profileNames[profile]} description="An optional ordered chain for this internal request profile. A configured chain supplies its first route and backups."><RouteChain label={profileNames[profile]} rows={draft.auxiliary_routes[profile] || []} disabled={disabled} onChange={routes=>editProviderRouting({...draft,auxiliary_routes:{...draft.auxiliary_routes,[profile]:routes}})}/></SettingsSection>)}
    {!state.connected?<p role="status">Backend disconnected. Your draft is retained.</p>:null}
    {state.error?<p className="settings-feedback is-error" role="alert">{state.error}</p>:null}{state.notice?<p className="settings-feedback" role="status">{state.notice}</p>:null}
    <div className="settings-save-actions"><Button tone="primary" disabled={disabled || !state.dirty} onClick={()=>saveProviderRouting()}>{busy?"Waiting…":"Save routing"}</Button><Button disabled={busy || !state.config || !state.dirty} onClick={()=>revertProviderRouting()}>Revert draft</Button>{!draft.enabled?<span>Configured chains remain inactive while recovery is off.</span>:null}</div>
  </div>;
}
