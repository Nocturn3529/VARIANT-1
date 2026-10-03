import {createModuleStore} from "./state/createModuleStore";
import {asRecord} from "./state/storePrimitives";
import {settingsRequests} from "./state/settingsRequests";
import type {RuntimeContext} from "./types";
export type ContextSource={source_id:string;kind:string;ordinal:number;preview?:string;snippet?:string;role?:string;[key:string]:unknown};
export type ContextView={view_id:string;created_at?:string|number;source_chat_id?:string;child_id?:string;[key:string]:unknown};
export type ContextChild={child_id:string;name?:string;status?:string;deletion_state?:string};
type Detail={source:Record<string,unknown>;text:string;offset:number;has_more:boolean;next_offset:number|null;total_chars:number;[key:string]:unknown};
type State={connected:boolean;chatId:string;childId:string;children:ContextChild[];childrenTruncated:boolean;viewId:string;views:ContextView[];viewsCursor:string|null;status:Record<string,unknown>|null;items:ContextSource[];next:number|null;after:number;pageStack:number[];query:string;kind:string;sourceId:string;part:string;detail:Detail|null;detailStack:number[];pending:Record<string,boolean>;error:string;notice:string;exportResult:Record<string,unknown>|null;coverage:string};
const empty=()=>({viewId:"",views:[] as ContextView[],viewsCursor:null,status:null,items:[] as ContextSource[],next:null,after:0,pageStack:[] as number[],query:"",kind:"",sourceId:"",part:"result",detail:null,detailStack:[] as number[],pending:{},error:"",notice:"",exportResult:null,coverage:""});
const store=createModuleStore<State>({initialState:{connected:false,chatId:"",childId:"",children:[],childrenTruncated:false,...empty()}});
const requests=settingsRequests("external-context",command=>store.send(command),(key,value)=>store.setState(state=>({...state,pending:{...state.pending,[key]:value}})),(_key,error)=>store.setState({error,notice:""}));
export const useExternalContextSettings=store.useStore,getExternalContextSettings=store.getState;
export const setExternalContextSettingsContext=(context:RuntimeContext)=>store.setContext(context);
export function setExternalContextSettingsConnection(status:string){store.setConnected(status==="connected");if(status!=="connected")requests.cancel("Connection interrupted. Reconnect and retry reading; an export result may be unconfirmed.");}
function request(key:string,operation:string,payload:Record<string,unknown>={}){
  const state=store.getState();if(!state.connected || !state.chatId)return false;
  store.setState({error:"",notice:""});return requests.request(key,operation,{type:"external-context:request",operation,chat_id:state.chatId,child_id:state.childId,...payload},operation==="export"?120000:operation==="capture" || operation==="refresh"?60000:20000);
}
export function chooseContextChat(chatId:string){if(store.getState().pending.export)return;requests.cancel();store.setState({chatId,childId:"",children:[],childrenTruncated:false,...empty()});if(chatId){refreshContextViews();refreshContextChildren();}}
export function refreshContextChildren(){return request("children","children");}
export function chooseContextChild(childId:string){const state=store.getState();if(state.pending.export)return;requests.cancel();store.setState({childId,...empty()});refreshContextViews();refreshContextChildren();}
export function refreshContextViews(more=false){const state=store.getState();return request("views","views",{limit:40,after:more?state.viewsCursor:undefined});}
export function captureContext(refresh=false){if(!store.getState().connected)return false;requests.cancelKey("page");requests.cancelKey("detail");return request("view",refresh?"refresh":"capture",refresh?{view_id:store.getState().viewId}:{});}
export function chooseContextView(viewId:string){
  if(store.getState().pending.export)return;requests.cancelKey("page");requests.cancelKey("detail");requests.cancelKey("view");
  store.setState({viewId,status:null,items:[],next:null,after:0,pageStack:[],sourceId:"",detail:null,detailStack:[],query:"",kind:"",error:"",notice:"",exportResult:null});
  if(viewId){request("view","status",{view_id:viewId});readContextPage();}
}
export function readContextPage(after=0,query=store.getState().query,kind=store.getState().kind){
  const state=store.getState();if(!state.viewId || !state.connected)return false;requests.cancelKey("page");requests.cancelKey("detail");
  store.setState({query,kind,after,items:[],next:null,sourceId:"",detail:null,detailStack:[],coverage:""});
  return request("page",query?"search":"read",{view_id:state.viewId,after,limit:40,kind,...(query?{query}: {})});
}
export function changeContextFilter(query:string,kind:string){store.setState({pageStack:[]});return readContextPage(0,query.trim(),kind);}
export function turnContextPage(next:boolean){const state=store.getState();if(next && state.next!==null){store.setState({pageStack:[...state.pageStack,state.after].slice(-100)});readContextPage(state.next);}else if(!next && state.pageStack.length){const stack=[...state.pageStack],after=stack.pop()!;store.setState({pageStack:stack});readContextPage(after);}}
export function expandContextSource(sourceId:string,part="result",offset=0){
  const state=store.getState();if(!state.viewId || !state.connected)return false;requests.cancelKey("detail");
  if(sourceId!==state.sourceId || part!==state.part)store.setState({detailStack:[]});
  store.setState({sourceId,part,detail:null});return request("detail","expand",{view_id:state.viewId,source_id:sourceId,part,offset,max_chars:12000});
}
export function turnContextDetail(next:boolean){const state=store.getState();if(!state.detail)return;
  if(next && state.detail.next_offset!==null){store.setState({detailStack:[...state.detailStack,state.detail.offset].slice(-100)});expandContextSource(state.sourceId,state.part,state.detail.next_offset);}
  else if(!next && state.detailStack.length){const stack=[...state.detailStack],offset=stack.pop()!;store.setState({detailStack:stack});expandContextSource(state.sourceId,state.part,offset);}
}
export function exportContextView(path:string,format:"jsonl"|"markdown",overwrite:boolean){const state=store.getState();if(!state.viewId)return false;store.setState({exportResult:null});return request("export","export",{view_id:state.viewId,path,format,overwrite});}
export function contextExportError(error:string){store.setState({error,notice:""});}
export function ingestExternalContextSettings(message:Record<string,unknown>){
  if(message.type==="chat:session:deleted"){
    if([message.id,message.chat_id,message.session_id].includes(store.getState().chatId)){requests.cancel();store.setState({chatId:"",childId:"",children:[],childrenTruncated:false,...empty(),error:"This session was deleted."});}return;
  }
  if(message.type!=="external-context:result")return;const state=store.getState();if(message.chat_id!==state.chatId)return;
  const entry=requests.take(message);if(!entry)return;
  if(message.ok!==true){store.setState({error:String(asRecord(message.error).message || "Context could not be read."),notice:""});return;}
  const result=asRecord(message.result);
  if(["status","read","search","expand"].includes(entry.operation) && result.view_id!==undefined && result.view_id!==state.viewId){store.setState({error:"Response did not match the selected view. Read it again."});return;}
  if(entry.operation==="children"){
    const items=(Array.isArray(result.items)?result.items:[]).map(asRecord).filter(row=>typeof row.child_id==="string").slice(0,100) as ContextChild[];
    store.setState({children:items,childrenTruncated:result.truncated===true});
  }else if(entry.operation==="views"){
    const items=(Array.isArray(result.items)?result.items:[]).map(asRecord).filter(row=>typeof row.view_id==="string").slice(0,40) as ContextView[];
    // The selector stays bounded; a continuation replaces rather than accumulates pages.
    store.setState({views:items,viewsCursor:typeof result.next_cursor==="string"?result.next_cursor:null});
  }else if(entry.operation==="capture" || entry.operation==="refresh"){
    if(typeof result.view_id!=="string"){store.setState({error:"Context view was not returned."});return;}
    chooseContextView(result.view_id);store.setState({status:asRecord(result.status),notice:entry.operation==="refresh"?"Refreshed context view opened.":"Frozen context view opened."});refreshContextViews();
  }else if(entry.operation==="status")store.setState({status:result});
  else if(entry.operation==="read" || entry.operation==="search"){
    const items=(Array.isArray(result.items)?result.items:[]).map(asRecord).filter(row=>typeof row.source_id==="string").slice(0,40) as ContextSource[];
    store.setState({items,next:result.has_more===true && typeof result.next_cursor==="number"?result.next_cursor:null,coverage:typeof result.coverage==="string"?result.coverage:""});
  }else if(entry.operation==="expand"){
    const source=asRecord(result.source);if(source.source_id!==state.sourceId || typeof result.text!=="string" || (result.text.length>24000 || Array.from(result.text).length>12000) || typeof result.offset!=="number"){store.setState({error:"Source response did not match the selected detail."});return;}
    store.setState({detail:{...result,source,text:result.text,offset:result.offset,has_more:result.has_more===true,next_offset:typeof result.next_offset==="number"?result.next_offset:null,total_chars:Number(result.total_chars || 0)}});
  }else if(entry.operation==="export"){
    if(typeof result.path!=="string" || typeof result.bytes!=="number"){store.setState({error:"Export completion was not confirmed."});return;}
    store.setState({exportResult:result,notice:"Context export saved."});
  }
}
