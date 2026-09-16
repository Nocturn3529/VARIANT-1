/**
 * Chat WebSocket ingest router: typed event family → session/turn/speech modules.
 * Messages arrive already shaped by `parseChatWsMessage` at DeckRuntime.
 */
import type {
  ChatWsMessage,
  StreamErrorMessage,
} from "../protocol";
import {CHAT_STREAM_TYPES} from "../protocol";
import {
  beginSharedTurn,
  emit,
  endSharedTurn,
  getChatState,
  sendChat,
  getDisplayedChatState,
  cachedChatStates,
  withCachedChatState,
  notifyChat,
  patchChatState,
  replaceChatState,
  setSubtitle,
  sharedTurnActive,
  turnApi,
} from "./stateCore";
import {
  handleAutoSpeak,
  handleTtsPreview,
} from "./speech";
import {beginTurnReceipt} from "./receipt";
import {ingestPauseState,currentPause} from "./pause";
import {applyInputQueue,ingestInputQueueResult,refreshInputQueue,queueItem,markQueuedPromptDelivered,isQueueContinueStart,canAdoptQueueStart,startQueueContinuation,finishQueueContinuation} from "./inputQueue";
import {activeTurnSessionIds,isSettledTurnEvent} from "../state/turnStore";
import {ingestComposerGoal,invalidateComposerGoal} from "./goals";
import {recordRecoveryNotice,dismissRecoveryNotice} from "./recovery";
import {ingestAgentTeam,ingestAgentWorkEvent,ingestAgentChanged} from "./agentTeam";
import {requestSessionContext} from "../sessionContextStore";
import {applyRuntimeSnapshot, applySession} from "./session";
import {
  acknowledgeActiveInput,
  markActiveInputDelivered,
  rejectActiveInput,
  rejectOptimisticTurn,
  settleActiveInputTickets,
} from "./composer";
import {
  activeTurnClientIds,
  applySettledReceipt,
  finishStream,
  handleAppended,
  ingestActivityMessage,
  isForeignStream,
  pushTurnStep,
  settleRunningThinking,
} from "./turn";

function isTurnScopedError(message: StreamErrorMessage): boolean {
  if (!sharedTurnActive()) return false;
  if (isForeignStream(message)) return false;
  const client = message.client_id || "";
  const source = message.source || "";
  const turn = turnApi();
  if (client) {
    if (activeTurnClientIds().has(client)) return true;
    return false;
  }
  if (source) {
    // Explicit non-chat source never ends the chat turn.
    if (source !== "chat") return false;
    return !turn || turn.getSource() === "chat";
  }
  // Untagged generic errors (credential/config/handler_failed) leave the turn alone.
  return false;
}

function isConfigSurfaceError(err: string): boolean {
  return /credential_|fallbacks_|provider_options|handler_failed:|unknown_type:/i.test(err);
}

/**
 * Ingest a single shaped chat-family message. Safe for every chat module type;
 * unknown loose types are ignored after the switch.
 */
export function ingestChat(message: ChatWsMessage) {
  if(message.type==="children:changed"){ingestAgentChanged(message);return;}
  if(message.type==="work:event")ingestAgentWorkEvent(message);
  const routed = message as ChatWsMessage & {session_id?:string;id?:string};
  let id = routed.session_id || (["chat:runtime", "chat:runtime:mutation:set:done", "chat:runtime:mutation:set:rejected"].includes(message.type) ? routed.id : "");
  if (!id && routed.id && ["chat:queued", "chat:queue_rejected"].includes(message.type)) {
    id = [getDisplayedChatState(), ...cachedChatStates()].find(chat => chat.pendingActiveInputs.some(input => input.optimisticTurnId === routed.id))?.sessionId || "";
  }
  if (!id && !routed.id && ["chat:queued", "chat:queue_rejected"].includes(message.type)) {
    const owners = new Map([getDisplayedChatState(), ...cachedChatStates()].filter(chat => chat.pendingActiveInputs.length).map(chat => [chat.sessionId, chat]));
    if (owners.size !== 1) return;
    id = owners.values().next().value?.sessionId || "";
  }
  const scoped = CHAT_STREAM_TYPES.has(message.type) || ["chat:queued", "chat:queue_rejected", "chat:rejected", "chat:appended"].includes(message.type);
  if (!id && scoped && activeTurnSessionIds().some(id => id !== getDisplayedChatState().sessionId)) return;
  if (message.type !== "chat:session" && id && id !== getDisplayedChatState().sessionId) {
    if (message.type === "speak" || message.type === "tts:preview") return;
    withCachedChatState(id, () => ingestOwnedChat(message));return;
  }
  ingestOwnedChat(message);
}

function ingestOwnedChat(message: ChatWsMessage) {
  const type = message.type;
  const turn = turnApi();
  const state = getChatState();

  // Stream isolation: voice surfaces never feed the Chat transcript.
  if (CHAT_STREAM_TYPES.has(type)) {
    const original = message as import("../protocol/chatEvents").StreamRouting;
    const routing = {...message,source:original.source==="queue_continue"?"chat":original.source};
    if (isForeignStream(routing)) return;
    if((message.type==="activity" || message.type==="tool:activity") && message.durable_replay) {
      if(!routing.source || routing.source==="chat")ingestActivityMessage(message);
      return;
    }
    if(isSettledTurnEvent(original))return;
    if(type!=="start" && type!=="error" && !turn.isActive())return;
    const queueStart=type==="start" && isQueueContinueStart(original);
    if(queueStart && !turn.isActive() && !canAdoptQueueStart(original)){refreshInputQueue();if(state.sessionId)sendChat({type:"chat:runtime:get",id:state.sessionId});return;}
    if (turn && !(queueStart && !turn.isActive()) && !turn.matchesEvent(routing)) return;
  }

  switch (message.type) {
    case "children:snapshot":
    case "children:detail":
    case "children:rejected":
      ingestAgentTeam(message);return;
    case "goal:accepted":
    case "goal:rejected":
    case "goal:current":
      ingestComposerGoal(message);return;
    case "work:event":
      if(message.aggregateKind==="goal" && (state.goal.snapshot?.goal.goal_id!==message.goal_id || state.goal.snapshot.goal.version<message.version))invalidateComposerGoal();
      return;
    case "chat:unknown":
      if(message.originalType==="chat:queue_snapshot" && message.request_id && message.session_id) {
        ingestInputQueueResult({type:"chat:queue_result",operation:"get",session_id:message.session_id,request_id:message.request_id,accepted:false,error:"Queue response was invalid. Refresh to try again.",queue:null});
      }
      return;
    case "chat:queue_snapshot":
      applyInputQueue(message);return;
    case "chat:queue_result":
      ingestInputQueueResult(message);return;
    case "chat:pause_state":
      ingestPauseState(message);
      return;
    case "hello":
      patchChatState({
        connected: true,
      });
      recordRecoveryNotice(message.orphaned_task);
      return;

    case "orphaned_task":
      recordRecoveryNotice(message.task);
      return;

    case "config":
    case "engine":
      patchChatState({connected: true});
      return;

    case "chat:session":
      if (message.session) applySession(message.session, message.navigation);
      return;

    case "chat:runtime":
      applyRuntimeSnapshot(message.id, message.runtime);
      return;

    case "chat:session:error":
      notifyChat(message.error || "Could not update session");
      return;

    case "chat:runtime:mutation:set:done": {
      const current = getChatState();
      if (current.sessionId && message.id && current.sessionId !== message.id) return;
      const pending = current.mutationTogglePending;
      const ownsRequest = !!pending && (
        !message.request_id || pending.requestId === message.request_id
      );
      // Do not let a late acknowledgement from another window/request paint
      // over the state targeted by our newer pending command. The following
      // authoritative session snapshot reconciles cross-window changes.
      if (pending && !ownsRequest) return;
      patchChatState({
        mutationTogglePending: ownsRequest ? null : pending,
        runtime: current.runtime ? {
          ...current.runtime,
          mutationEnabled: message.enabled,
          mutationEffectiveEnabled: message.effective_enabled,
          mutationAuthorityRevision: message.authority_revision,
        } : current.runtime,
      });
      if (ownsRequest) {
        notifyChat(
          message.enabled
            ? "Mutation authoring is on for this chat"
            : "Mutation authoring is off; activated session tools remain available",
        );
      }
      return;
    }

    case "chat:runtime:mutation:set:rejected": {
      const current = getChatState();
      if (current.sessionId && message.id && current.sessionId !== message.id) return;
      const pending = current.mutationTogglePending;
      const ownsRequest = !!pending && (
        !message.request_id || pending.requestId === message.request_id
      );
      if (ownsRequest) patchChatState({mutationTogglePending: null});
      if (ownsRequest || !pending) {
        notifyChat(message.error || "Mutation authority change was rejected");
      }
      return;
    }

    case "chat:queued": {
      if(message.queue){applyInputQueue(message.queue);return;}
      refreshInputQueue();
      if(getChatState().inputQueue.snapshot)return;
      const count = Math.max(0, message.queue_size);
      if (!acknowledgeActiveInput(message.id)) return;
      patchChatState({queuedFollowUps: count});
      notifyChat(
        message.delivery === "steer"
          ? "Instruction queued for the next model or tool boundary"
          : "Follow-up queued for after the active task",
      );
      return;
    }

    case "chat:queue_rejected": {
      refreshInputQueue();
      const rejected = rejectActiveInput(message.id);
      if (!rejected) return;
      const label = rejected?.delivery === "steer" ? "steering instruction" : "follow-up";
      notifyChat(`Could not queue the ${label}: ${message.error || "rejected"}`);
      return;
    }

    case "chat:queue_progress": {
      if (state.sessionId && message.session_id
          && state.sessionId !== message.session_id) return;
      if(message.queue){const item=queueItem(message.id);if(applyInputQueue(message.queue))markQueuedPromptDelivered(message.id,item);return;}
      refreshInputQueue();if(getChatState().inputQueue.snapshot)return;
      markActiveInputDelivered(message.id);
      patchChatState({queuedFollowUps: Math.max(0, message.queue_size)});
      return;
    }

    case "chat:queue_settled": {
      if (state.sessionId && message.session_id
          && state.sessionId !== message.session_id) return;
      refreshInputQueue();if(getChatState().inputQueue.snapshot)return;
      const settled = settleActiveInputTickets(message.ids);
      patchChatState({queuedFollowUps: Math.max(0, message.queue_size)});
      if (settled) {
        notifyChat(`Queued input was not delivered: ${message.reason}`);
      }
      return;
    }

    case "chat:rejected": {
      finishQueueContinuation(message);
      const err = message.text || message.error || "Could not start this turn";
      endSharedTurn("error");
      if (!rejectOptimisticTurn(err)) {
        replaceChatState({
          ...getChatState(),
          lastError: err,
          turnActive: false,
          stopPending: false,
          streaming: false,
          streamText: "",
          turnSteps: [],
          queuedFollowUps: 0,
          activeTurnId: null,
          pendingActiveInputs: [],
        });
      }
      setSubtitle("Connected locally", "ready");
      notifyChat(err);
      emit();
      return;
    }

    case "chat:appended":
      handleAppended(message);
      return;

    case "chat:transcript_failed": {
      const current = getChatState();
      if (
        current.sessionId
        && message.session_id
        && current.sessionId !== message.session_id
      ) return;
      const turn = turnApi().snapshot();
      const runId = message.run_id || "";
      const matchesActive = (!message.admission_id || message.admission_id === turn.admissionId)
        && (!runId || !turn.runId || runId === turn.runId);
      const messages = [...current.messages];
      let matched = false;
      for (let index = messages.length - 1; index >= 0; index -= 1) {
        if (messages[index].role !== "assistant" || !(runId ? messages[index].runId === runId
          : current.activeTurnId && messages[index].optimisticTurnId === current.activeTurnId)) continue;
        messages[index] = {...messages[index], durability: "failed"};
        matched = true;
        break;
      }
      const owner = current.activeTurnId || turn.runId;
      if (!matched && (!turn.active || !owner || !matchesActive)) return;
      replaceChatState({
        ...current,
        messages,
        ...(!matched ? {pendingTurnCommit: {owner, failed: true}} : {}),
        lastError: "This reply is visible but was not saved to chat history.",
      });
      notifyChat("Reply could not be saved. Resume the interrupted task before continuing.");
      emit();
      return;
    }

    case "tts:preview":
      handleTtsPreview(message);
      return;

    case "speak":
      handleAutoSpeak(message);
      return;

    case "start": {
      // Only adopt unowned streams for our own chat client (mic / multi-window).
      const eventClient = message.client_id || "";
      if (!sharedTurnActive()) {
        if (eventClient && eventClient !== state.clientId && !message.admission_id) return;
        beginSharedTurn({
          clientId: eventClient || state.clientId,
          source: "chat",
          sessionId: state.sessionId,
        });
      }
      turn.bind(message.admission_id, message.run_id);
      startQueueContinuation(message);
      dismissRecoveryNotice();
      replaceChatState({
        ...getChatState(),
        orphanedTask:null,
        pause: currentPause(),
        streamText: "",
        streaming: true,
        turnActive: true,
        lastError: "",
        turnSteps: [],
        pendingTurnCommit: undefined,
        runtime:getChatState().runtime ? {...getChatState().runtime!,busy:true,activeAdmissionId:message.admission_id || "",activeRunId:message.run_id || ""} : null,
      });
      beginTurnReceipt();
      requestSessionContext(getChatState().sessionId, getChatState().sessionId === getDisplayedChatState().sessionId);
      setSubtitle("VARIANT-1 is responding", "working");
      emit();
      return;
    }

    case "token": {
      const cur = getChatState();
      settleRunningThinking();
      patchChatState({
        streaming: true,
        turnActive: true,
        streamText: `${cur.streamText || ""}${message.token}`,
      });
      return;
    }

    case "thinking": {
      const text = message.text;
      const snapshot = message.summary_source === "provider_summary" && !!message.summary_id;
      if (snapshot && !text && !getChatState().turnSteps.some(step => step.id === message.summary_id)) return;
      if (text || snapshot) {
        const startedAt = message.ts ? (message.ts < 1e12 ? message.ts * 1000 : message.ts) : Date.now();
        pushTurnStep({
          id: snapshot ? message.summary_id : undefined,
          summaryState: snapshot ? message.status : undefined,
          summaryRevision: snapshot ? message.summary_revision : undefined,
          kind: "thinking",
          label: "Thinking",
          detail: snapshot ? text : text || undefined,
          appendDetail: !snapshot,
          status: snapshot && message.status !== "running" ? "done" : "running",
          key: snapshot ? message.summary_id : "thinking",
          startedAt,
          ...(snapshot && message.status !== "running" ? {completedAt: Date.now(), durationMs: Math.max(0, Date.now() - startedAt)} : {}),
        });
        if (!snapshot) setSubtitle("Thinking", "working");
      }
      return;
    }

    case "tool:activity":
    case "activity":
      // STEPS density for the transcript.
      ingestActivityMessage(message);
      return;

    case "done":
      finishStream(message);
      finishQueueContinuation(message);
      return;

    case "run:settled":
      applySettledReceipt(message.receipt, message.session_id, message.run_id);
      requestSessionContext(message.session_id || getChatState().sessionId, getChatState().sessionId === getDisplayedChatState().sessionId);
      if (!sharedTurnActive()) setSubtitle("Connected locally", "ready");
      emit();
      return;

    case "cancelling":
      if (message.accepted === false) {
        patchChatState({stopPending:false});
        notifyChat(message.error === "stale_run" ? "That task has changed. Refreshing its current status." : message.error || "Stop was not accepted.");
        const id = getChatState().sessionId;
        if (id) sendChat({type:"chat:runtime:get",id});
        return;
      }
      patchChatState({stopPending:true});
      setSubtitle("Stopping task…", "working");
      emit();
      return;

    case "error": {
      const err = message.error || "VARIANT-1 backend error";
      // Settings/cloud handlers return generic {type:"error"} without client_id.
      // Those must toast (if useful) but must NOT kill an in-flight chat turn.
      if (!isTurnScopedError(message)) {
        // Surface non-chat errors lightly only when Chat is idle (settings already
        // shows its own feedback). Mid-turn: leave generation alone.
        if (!sharedTurnActive()) {
          // Ignore routine config errors on the chat surface while idle.
          if (!isConfigSurfaceError(err)) notifyChat(err);
        } else if (!isConfigSurfaceError(err)) {
          notifyChat(err);
        }
        return;
      }
      const completedAt = Date.now();
      const failedSteps = getChatState().turnSteps.map(step => (
        step.status === "running" ? {
          ...step,
          status: "error" as const,
          rawStatus: step.rawStatus || "turn_failed",
          completedAt,
          durationMs: step.startedAt ? Math.max(0, completedAt - step.startedAt) : step.durationMs,
          ts: completedAt,
        } : step
      ));
      endSharedTurn("error");
      finishQueueContinuation(message);
      replaceChatState({
        ...getChatState(),
        lastError: err,
        turnActive: false,
        stopPending: false,
        streaming: false,
        turnSteps: failedSteps,
        queuedFollowUps: 0,
        activeTurnId: null,
        pendingActiveInputs: [],
      });
      notifyChat(err);
      setSubtitle("Connected locally", "ready");
      emit();
      return;
    }

    default:
      // Loose / unshaped types on the chat module map — ignore.
      return;
  }
}
