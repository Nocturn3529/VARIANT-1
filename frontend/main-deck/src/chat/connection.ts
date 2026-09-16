/**
 * WebSocket connection reflection + shared turn bridge for Chat.
 */
import {pushWireStatus,createReconnectRefresh} from "../connectionUi";
import {
  emit,
  cachedChatStates,
  getChatState,
  patchChatState,
  replaceChatState,
  setSubtitle,
  sendChat,
  turnApi,
  withCachedChatState,
} from "./stateCore";
import {refreshInputQueue} from "./inputQueue";
import {refreshComposerGoal} from "./goals";
import {refreshAgentTeam} from "./agentTeam";
import {invalidateRecoveryNotices} from "./recovery";

const refreshPauseOnReconnect=createReconnectRefresh(()=>{
  const chats=new Map([getChatState(),...cachedChatStates()].map(chat=>[chat.sessionId,chat]));
  for(const chat of chats.values()) if(chat.sessionId) {
    withCachedChatState(chat.sessionId,refreshInputQueue);
    withCachedChatState(chat.sessionId,refreshComposerGoal);
    withCachedChatState(chat.sessionId,refreshAgentTeam);
    if(chat.turnActive)sendChat({type:"chat:runtime:get",id:chat.sessionId});
    else if(chat.inputQueue.continuation && chat.sessionId===getChatState().sessionId)sendChat({type:"chat:session:get",id:chat.sessionId});
  }
});

/**
 * Reflect WebSocket up/down with sticky hold so brief reconnect probes do not
 * flicker the Chat header subtitle.
 */
export function setChatConnection(status: string) {
  // Authority cannot use the cosmetic offline delay: even a short disconnect
  // makes an in-flight control request uncertain until runtime reattachment.
  if(status!=="connected") {
    invalidateRecoveryNotices();
    const chats=new Map([getChatState(),...cachedChatStates()].map(chat=>[chat.sessionId,chat]));
    for(const chat of chats.values()) if(chat.sessionId) {
      withCachedChatState(chat.sessionId,()=>{const team=getChatState().agentTeam;patchChatState({agentTeam:{...team,synced:false,readId:undefined,detailReadId:undefined,detailDirty:!!team.selectedId}});});
      withCachedChatState(chat.sessionId,()=>{const goal=getChatState().goal;patchChatState({goal:{...goal,synced:false,refreshRequestId:undefined,refreshAgain:false,
        pending:goal.pending?{...goal.pending,uncertain:true}:undefined}});});
    }
    for(const chat of chats.values()) if(chat.sessionId && (chat.inputQueue.synced || chat.inputQueue.refreshRequestId)) {
      withCachedChatState(chat.sessionId,()=>{const queue=getChatState().inputQueue;patchChatState({inputQueue:{...queue,synced:false,refreshRequestId:undefined,
        action:queue.action?{...queue.action,uncertain:true}:undefined}});});
    }
    for(const chat of chats.values()) if(chat.sessionId && chat.turnActive && (!chat.pause || chat.pause.synced || chat.pause.pending)) {
      withCachedChatState(chat.sessionId,()=>{
        const current=getChatState(),turn=turnApi().snapshot();
        patchChatState({pause:{admissionId:turn.admissionId || current.runtime?.activeAdmissionId || "",runId:turn.runId || current.runtime?.activeRunId || "",
          state:current.pause?.state || "running",revision:current.pause?.revision ?? -1,synced:false}});
      });
    }
  }
  refreshPauseOnReconnect(status);
  pushWireStatus("chat", status, online => {
    const state = getChatState();
    if (online) {
      if (state.connected && !state.turnActive && state.subtitle === "Connected locally") {
        return;
      }
      if (state.turnActive) setSubtitle("VARIANT-1 is working", "working");
      else setSubtitle("Connected locally", "ready");
      patchChatState({connected: true});
      return;
    }
    const subtitle = "Backend offline — reconnecting";
    if (!state.connected && state.subtitle === subtitle) return;
    replaceChatState({
      ...getChatState(),
      connected: false,
      stopPending: false,
      pause: getChatState().pause,
      // Delivery becomes ambiguous once the socket drops. Reconnect
      // hydration supplies the durable state; do not leave the switch latched.
      mutationTogglePending: null,
      subtitle,
      subtitleState: "offline",
    });
    emit();
  });
}

/** Keep Chat state synchronized with the shared turn authority. */
export function attachTurnBridge() {
  const api = turnApi();
  if (!api || typeof api.subscribe !== "function") return;
  api.subscribe((snap, prev) => {
    const state = getChatState();
    const was = !!(prev && prev.active);
    const now = !!snap.active;
    if (was === now && state.turnActive === now) {
      return;
    }
    if (now && !state.turnActive) {
      patchChatState({turnActive: true});
    } else if (!now && state.turnActive) {
      // Stream completion usually clears local flags first; keep in sync when
      // another React owner ends the turn (for example on disconnect).
      patchChatState({turnActive: false, streaming: false, queuedFollowUps: 0, stopPending: false, pause:null});
    }
  });
}
