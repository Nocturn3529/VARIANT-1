import {parseAgentSnapshot,parseAgentDetail,type ChildrenMessage,type ChildrenChangedMessage} from "../protocol/children";
import type {GoalWorkEvent} from "../protocol/goals";
import {getChatState,getChatContext,patchChatState,sendChat,cachedChatStates,getDisplayedChatState,withCachedChatState} from "./stateCore";

export function refreshAgentTeam(manual=false):boolean {
  const state=getChatState(),team=state.agentTeam;
  if(!state.sessionId || getChatContext()?.isOpen?.()===false || (team.readId && !manual))return false;
  const readId=`agents-${crypto.randomUUID()}`;
  patchChatState({agentTeam:{...team,readId,error:undefined}});
  if(sendChat({type:"children:snapshot:get",session_id:state.sessionId,request_id:readId,limit:100}))return true;
  patchChatState({agentTeam:{...getChatState().agentTeam,readId:undefined,error:"Agent status could not be refreshed.",synced:false}});return false;
}
export function selectTeamAgent(id:string):void {
  const state=getChatState(),team=state.agentTeam;
  const agent=team.agents.find(row=>row.id===id);
  if(!agent || !state.sessionId)return;
  patchChatState({agentTeam:{...team,selectedId:id,detail:team.detail?.id===id && team.detail.generation===agent.generation?team.detail:null,detailReadId:undefined,detailError:undefined,detailRevision:team.selectedId===id?team.detailRevision:-1,activityRevision:team.selectedId===id?team.activityRevision:-1}});
  refreshAgentDetail();
}
export function closeTeamAgent():void {
  patchChatState({agentTeam:{...getChatState().agentTeam,selectedId:null,detail:null,detailReadId:undefined,detailDirty:false,detailError:undefined,detailRevision:-1}});
}
function refreshAgentDetail():boolean {
  const state=getChatState(),team=state.agentTeam;
  if(!state.sessionId || !team.selectedId || team.detailReadId || getChatContext()?.isOpen?.()===false)return false;
  const detailReadId=`agent-detail-${crypto.randomUUID()}`;
  patchChatState({agentTeam:{...team,detailReadId,detailError:undefined}});
  if(sendChat({type:"children:detail:get",session_id:state.sessionId,request_id:detailReadId,child_id:team.selectedId,limit:100}))return true;
  patchChatState({agentTeam:{...getChatState().agentTeam,detailReadId:undefined,detailError:"This agent’s activity could not be read."}});return false;
}
export function ingestAgentTeam(message:ChildrenMessage):void {
  const state=getChatState(),team=state.agentTeam;
  if(message.session_id!==state.sessionId)return;
  const roster=!!team.readId && team.readId===message.request_id,detail=!!team.detailReadId && team.detailReadId===message.request_id;
  if(!roster && !detail)return;
  const error=typeof message.payload.error==="string"?message.payload.error:"Agent data could not be read.";
  if(message.type==="children:rejected") {
    patchChatState({agentTeam:{...team,...(roster?{readId:undefined,synced:false,error}:{detailReadId:undefined,detailError:error})}});return;
  }
  if(roster && message.type==="children:snapshot") {
    const snapshot=parseAgentSnapshot(message.payload);
    if(!snapshot || snapshot.agents.some(row=>!row.parentId && row.parentChatId!==state.sessionId)) {
      patchChatState({agentTeam:{...team,readId:undefined,synced:false,error:"The agent roster response was invalid. Refresh to try again."}});return;
    }
    const fresh=snapshot.revision>=team.revision;
    patchChatState({agentTeam:{...team,...(fresh?snapshot:{}),readId:undefined,dirty:false,synced:fresh || team.synced,error:undefined}});
    const current=getChatState().agentTeam,selected=current.agents.find(row=>row.id===current.selectedId);
    const priorSelected=team.agents.find(row=>row.id===current.selectedId);
    const selectedChanged=selected && priorSelected && (selected.updatedAt!==priorSelected.updatedAt || selected.status!==priorSelected.status || selected.outcome!==priorSelected.outcome);
    if(current.selectedId && !selected)closeTeamAgent();
    else if(selected && (!current.detail || current.detail.generation!==selected.generation || current.detailDirty || selectedChanged)) {
      patchChatState({agentTeam:{...getChatState().agentTeam,detail:current.detail?.generation===selected.generation?current.detail:null,detailDirty:!!current.detailReadId}});
      if(!current.detailReadId)refreshAgentDetail();
    }
    if(team.dirty)refreshAgentTeam();
    return;
  }
  if(detail && message.type==="children:detail") {
    const parsed=parseAgentDetail(message.payload),known=team.agents.find(row=>row.id===team.selectedId);
    if(!parsed || !known || parsed.agent.id!==known.id || parsed.agent.chatId!==known.chatId || parsed.agent.generation<known.generation) {
      patchChatState({agentTeam:{...team,detailReadId:undefined,detailError:"This agent’s activity response was invalid. Refresh to try again."}});return;
    }
    const fresh=parsed.revision>=Math.max(team.detailRevision,team.revision) && parsed.activityRevision>=team.activityRevision;
    patchChatState({agentTeam:{...team,detailReadId:undefined,detailDirty:false,detailError:undefined,
      ...(fresh?{detail:parsed.detail,detailRevision:parsed.revision,activityRevision:parsed.activityRevision,agents:team.agents.map(row=>row.id===known.id?parsed.agent:row)}:{})}});
    if(team.detailDirty)refreshAgentDetail();
  }
}
export function ingestAgentWorkEvent(message:GoalWorkEvent):void {
  if(!["job","operation"].includes(message.aggregateKind))return;
  // Supervisor/other job activity does not imply that a child record changed.
  if(message.aggregateKind==="job" && message.jobKind!=="child.execute.v1")return;
  const states=new Map([getDisplayedChatState(),...cachedChatStates()].map(state=>[state.sessionId,state]));
  for(const state of states.values()) {
    if(!state.sessionId)continue;
    const child=state.agentTeam.agents.find(agent=>agent.chatId===message.session_id);
    if(state.sessionId!==message.session_id && !child)continue;
    withCachedChatState(state.sessionId,()=>{
      const team=getChatState().agentTeam;
      if(message.aggregateKind==="job") {
        if(team.readId)patchChatState({agentTeam:{...team,dirty:true}});else refreshAgentTeam();
      }
      if(child && team.selectedId===child.id) {
        if(getChatState().agentTeam.detailReadId)patchChatState({agentTeam:{...getChatState().agentTeam,detailDirty:true}});else refreshAgentDetail();
      }
    });
  }
}

export function ingestAgentChanged(message:ChildrenChangedMessage):void {
  const states=new Map([getDisplayedChatState(),...cachedChatStates()].map(state=>[state.sessionId,state]));
  for(const state of states.values()) {
    if(!state.sessionId || (state.sessionId!==message.session_id && !state.agentTeam.agents.some(child=>child.chatId===message.session_id)))continue;
    withCachedChatState(state.sessionId,()=>{
      const team=getChatState().agentTeam;
      if(message.revision<=(team.changeRevision ?? -1))return;
      const needsRoster=message.revision>team.revision;
      const needsDetail=!!team.selectedId && message.revision>team.detailRevision;
      // This is a global table clock. A later notification for another child
      // can arrive before an earlier message-only change to the selected child.
      // Refresh open detail through the same frontier instead of trusting childId order.
      // Roster coverage alone also does not prove that detail/report data was read.
      patchChatState({agentTeam:{...team,changeRevision:message.revision,dirty:team.dirty || (needsRoster && !!team.readId),
        detailDirty:team.detailDirty || (needsDetail && (needsRoster || !!team.detailReadId))}});
      if(needsRoster && !team.readId)refreshAgentTeam();
      else if(!needsRoster && needsDetail && !team.detailReadId)refreshAgentDetail();
    });
  }
}
