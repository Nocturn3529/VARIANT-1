import {parseObjectiveOutcome} from "./goals";

export type AgentSummary=Readonly<{
  id:string;chatId:string;parentChatId:string;parentId:string|null;name:string;task:string;status:string;
  createdAt:number;startedAt:number;completedAt:number;updatedAt:number;generation:number;
  outcome:string;cleanupStatus:string;currentActivity:string;
}>;
export type AgentDetail=Readonly<{id:string;generation:number;activities:readonly {id:string;kind:string;status:string;createdAt:number;completedAt:number;runId:string}[];report:string;truncated:boolean}>;
export type AgentTeamState={agents:readonly AgentSummary[];revision:number;total:number;active:number;blocked:number;truncated:boolean;synced:boolean;
  changeRevision?:number;
  readId?:string;dirty?:boolean;error?:string;selectedId:string|null;detail:AgentDetail|null;detailRevision:number;activityRevision:number;detailReadId?:string;detailDirty?:boolean;detailError?:string};
export const initialAgentTeam=():AgentTeamState=>({agents:[],revision:-1,total:0,active:0,blocked:0,truncated:false,synced:false,selectedId:null,detail:null,detailRevision:-1,activityRevision:-1});
export type ChildrenCommand={type:"children:snapshot:get";session_id:string;request_id:string;limit?:number}
  | {type:"children:detail:get";session_id:string;request_id:string;child_id:string;limit?:number};
export type ChildrenMessage=Readonly<{type:"children:snapshot"|"children:detail"|"children:rejected";session_id:string;request_id:string;payload:Readonly<Record<string,unknown>>}>;
export type ChildrenChangedMessage=Readonly<{type:"children:changed";session_id:string;revision:number;childId?:string}>;
export function parseChildrenChanged(row:Record<string,unknown>):ChildrenChangedMessage|null {
  if(row.schema!=="variant1.children-changed.v1" || typeof row.session_id!=="string" || !row.session_id
    || !Number.isSafeInteger(row.revision) || Number(row.revision)<0
    || (row.child_id!==undefined && (typeof row.child_id!=="string" || !row.child_id)))return null;
  return {type:"children:changed",session_id:row.session_id,revision:Number(row.revision),
    ...(typeof row.child_id==="string"?{childId:row.child_id}:{})};
}
export function parseChildrenMessage(row:Record<string,unknown>):ChildrenMessage|null {
  if(!["children:snapshot","children:detail","children:rejected"].includes(String(row.type)) || typeof row.session_id!=="string" || !row.session_id || typeof row.request_id!=="string")return null;
  return {type:row.type as ChildrenMessage["type"],session_id:row.session_id,request_id:row.request_id,payload:row};
}
const string=(value:unknown)=>typeof value==="string"?value:"";
const number=(value:unknown)=>typeof value==="number" && Number.isFinite(value) && value>=0?value:0;
export function parseAgent(value:unknown):AgentSummary|null {
  if(!value || typeof value!=="object")return null;
  const row=value as Record<string,unknown>;
  if(!string(row.child_id) || !string(row.child_chat_id) || !string(row.parent_chat_id) || !string(row.status) || !Number.isSafeInteger(row.run_generation))return null;
  const cleanup=(row.cleanup && typeof row.cleanup==="object"?row.cleanup:{}) as Record<string,unknown>;
  return {id:string(row.child_id),chatId:string(row.child_chat_id),parentChatId:string(row.parent_chat_id),parentId:string(row.parent_child_id)||null,
    name:string(row.name)||string(row.child_id),task:string(row.task),status:string(row.status),createdAt:number(row.created_at),startedAt:number(row.started_at),completedAt:number(row.completed_at),updatedAt:number(row.updated_at),
    generation:number(row.run_generation),outcome:parseObjectiveOutcome(row.outcome).status,cleanupStatus:cleanup.status==="complete" && cleanup.complete!==true?"unconfirmed":string(cleanup.status)||"unknown",currentActivity:""};
}
export function parseAgentSnapshot(row:Readonly<Record<string,unknown>>):Pick<AgentTeamState,"agents"|"revision"|"total"|"active"|"blocked"|"truncated">|null {
  if(row.schema!=="variant1.children-snapshot.v1" || !Number.isSafeInteger(row.revision) || Number(row.revision)<0 || !Array.isArray(row.children) || row.children.length>500)return null;
  const agents=row.children.map(parseAgent);
  if(agents.some(agent=>!agent) || new Set(agents.map(agent=>agent!.id)).size!==agents.length)return null;
  return {agents:agents as AgentSummary[],revision:Number(row.revision),total:number(row.total),active:number(row.active),blocked:number(row.blocked),truncated:row.truncated===true};
}
export function parseAgentDetail(row:Readonly<Record<string,unknown>>):{agent:AgentSummary;detail:AgentDetail;revision:number;activityRevision:number}|null {
  const agent=parseAgent(row.child);
  if(row.schema!=="variant1.child-detail.v1" || !agent || !Number.isSafeInteger(row.revision) || Number(row.revision)<0
    || !Number.isSafeInteger(row.activity_revision) || Number(row.activity_revision)<0
    || row.activity_provenance!=="canonical_work_operations" || !Array.isArray(row.activity) || row.activity.length>100)return null;
  const child=row.child as Record<string,unknown>;
  const activities=row.activity.flatMap(value=>{
    if(!value || typeof value!=="object")return [];
    const entry=value as Record<string,unknown>,scope=(entry.scope || {}) as Record<string,unknown>;
    if(scope.chat_id!==agent.chatId || !string(entry.operation_id) || !string(entry.kind) || !string(entry.status))return [];
    return [{id:string(entry.operation_id),kind:string(entry.kind),status:string(entry.status),createdAt:number(entry.created_at),completedAt:number(entry.completed_at),runId:string(scope.run_id)}];
  });
  if(activities.length!==row.activity.length || new Set(activities.map(row=>row.id)).size!==activities.length)return null;
  return {agent,revision:Number(row.revision),activityRevision:Number(row.activity_revision),detail:{id:agent.id,generation:agent.generation,activities,report:string(child.report).slice(0,16000),truncated:row.truncated===true || child.report_truncated===true || string(child.report).length>16000}};
}
