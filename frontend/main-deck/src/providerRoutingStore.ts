import {createModuleStore} from "./state/createModuleStore";
import {asRecord} from "./state/storePrimitives";
import {settingsRequests} from "./state/settingsRequests";
import type {RuntimeContext} from "./types";
export type RecoveryRoute={mode:"cloud"|"local";provider:string;model:string;reasoning_effort?:string};
export const ROUTING_PROFILES=["internal_json","internal_prose","vision"] as const;
export type RoutingProfile=typeof ROUTING_PROFILES[number];
export type RecoveryConfig={enabled:boolean;max_attempts:number;max_wait_seconds:number;fallback_routes:RecoveryRoute[];auxiliary_routes:Partial<Record<RoutingProfile,RecoveryRoute[]>>};
export const defaultRecovery=():RecoveryConfig=>({enabled:false,max_attempts:4,max_wait_seconds:60,fallback_routes:[],auxiliary_routes:{}});
type State={connected:boolean;config:RecoveryConfig|null;draft:RecoveryConfig;revision:string;draftRevision:string;dirty:boolean;pending:Record<string,boolean>;error:string;notice:string};
const store=createModuleStore<State>({initialState:{connected:false,config:null,draft:defaultRecovery(),revision:"",draftRevision:"",dirty:false,pending:{},error:"",notice:""}});
const requests=settingsRequests("provider-recovery",command=>store.send(command),(key,value)=>store.setState(state=>({...state,pending:{...state.pending,[key]:value}})),(_key,error)=>store.setState({error,notice:""}));
export const useProviderRouting=store.useStore,getProviderRouting=store.getState;
export const setProviderRoutingContext=(context:RuntimeContext)=>store.setContext(context);
export function setProviderRoutingConnection(status:string){store.setConnected(status==="connected");if(status!=="connected")requests.cancel("Connection interrupted. Your draft is retained; refresh before saving again.");}
export function refreshProviderRouting(){if(!store.getState().connected)return false;return requests.request("config","get",{type:"provider-recovery:get"});}
export function editProviderRouting(draft:RecoveryConfig){store.setState({draft,dirty:true,error:"",notice:""});}
export function revertProviderRouting(){const state=store.getState();if(state.config)store.setState({draft:structuredClone(state.config),draftRevision:state.revision,dirty:false,error:"",notice:""});}
export function routingIssues(config:RecoveryConfig):string[]{
  const issues:string[]=[];
  if(!Number.isInteger(config.max_attempts) || config.max_attempts<1 || config.max_attempts>12)issues.push("Attempts must be an integer from 1 to 12.");
  if(!Number.isFinite(config.max_wait_seconds) || config.max_wait_seconds<0 || config.max_wait_seconds>600)issues.push("Wait budget must be between 0 and 600 seconds.");
  for(const [name,rows] of [["Main backups",config.fallback_routes],...ROUTING_PROFILES.map(profile=>[profile,config.auxiliary_routes[profile] || []])] as Array<[string,RecoveryRoute[]]>){
    if(rows.length>4)issues.push(`${name}: at most four routes are allowed.`);
    rows.forEach((row,index)=>{if(!row.provider.trim() || !row.model.trim())issues.push(`${name}, route ${index+1}: choose a provider and model.`);if(row.mode==="local" && row.provider!=="local")issues.push(`${name}, route ${index+1}: local routes use the local provider.`);if((row.reasoning_effort || "").length>32)issues.push(`${name}, route ${index+1}: effort is too long.`);});
  }
  return issues;
}
export function saveProviderRouting(){
  const state=store.getState(),issues=routingIssues(state.draft);
  if(issues.length){store.setState({error:issues.join(" "),notice:""});return false;}
  if(!state.connected || !state.config || state.pending.config)return false;
  if(state.draftRevision!==state.revision){store.setState({error:"Saved settings changed. Revert the draft to the latest saved configuration before saving.",notice:""});return false;}
  store.setState({error:"",notice:""});return requests.request("config","set",{type:"provider-recovery:set",config:state.draft,expected_revision:state.draftRevision});
}
function parseConfig(value:unknown):RecoveryConfig|null{
  const raw=asRecord(value);if(typeof raw.enabled!=="boolean" || typeof raw.max_attempts!=="number" || typeof raw.max_wait_seconds!=="number" || !Array.isArray(raw.fallback_routes))return null;
  const chains=asRecord(raw.auxiliary_routes);
  function routes(rows:unknown):RecoveryRoute[]|null{if(!Array.isArray(rows) || rows.length>4)return null;const result:RecoveryRoute[]=[];for(const item of rows){const row=asRecord(item);if(!["local","cloud"].includes(String(row.mode)) || typeof row.provider!=="string" || typeof row.model!=="string")return null;result.push({mode:row.mode as RecoveryRoute["mode"],provider:row.provider,model:row.model,...(typeof row.reasoning_effort==="string"?{reasoning_effort:row.reasoning_effort}:{})});}return result;}
  const fallback=routes(raw.fallback_routes);if(!fallback)return null;const auxiliary:RecoveryConfig["auxiliary_routes"]={};
  for(const profile of ROUTING_PROFILES){if(chains[profile]!==undefined){const chain=routes(chains[profile]);if(!chain)return null;auxiliary[profile]=chain;}}
  return {enabled:raw.enabled,max_attempts:raw.max_attempts,max_wait_seconds:raw.max_wait_seconds,fallback_routes:fallback,auxiliary_routes:auxiliary};
}
export function ingestProviderRouting(message:Record<string,unknown>){
  if(message.type!=="provider-recovery:result")return;const entry=requests.take(message);if(!entry)return;
  if(message.ok!==true){store.setState({error:String(asRecord(message.error).message || "Routing settings could not be saved."),notice:""});return;}
  const result=asRecord(message.result),config=parseConfig(result.config);
  if(!config || typeof result.revision!=="string"){store.setState({error:"Routing response was incomplete. Refresh to check saved settings.",notice:""});return;}
  const state=store.getState(),replace=entry.operation==="set" || !state.dirty;
  store.setState({config,revision:result.revision,...(replace?{draft:structuredClone(config),draftRevision:result.revision,dirty:false}:{}),error:"",notice:entry.operation==="set"?"Routing settings saved.":state.dirty && state.draftRevision!==result.revision?"Saved settings changed. Your draft has been retained.":""});
}
