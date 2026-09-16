/**
 * Transcript message parse / merge helpers (session rehydrate + appended).
 */
import type {
  ChatAttachment,
  ChatMessage,
  ChatTurnStep,
} from "./types";
import {MAX_TURN_STEPS} from "./stateCore";
import {parseTurnReceipt} from "./receipt";
import {parseEvidence} from "./evidence";
import {parsePeerActivity} from "../protocol/peerActivity";

function newStepId(): string {
  return `st-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

/**
 * Collapse legacy user messages that stored full inlined file bodies after
 * "[Attached file: …]" (drag-and-drop used to explode the transcript).
 */
function collapseUserAttachmentBodies(text: string): {
  text: string;
  attachments: ChatAttachment[];
} {
  const raw = String(text || "");
  const marker = /\n\n\[Attached\s+(file|image|path):\s*([^\]\n—\-]+?)(?:\s*[—\-].*?)?\]/gi;
  const attachments: ChatAttachment[] = [];
  let firstIdx = -1;
  let match: RegExpExecArray | null;
  const re = new RegExp(marker.source, marker.flags);
  while ((match = re.exec(raw)) !== null) {
    if (firstIdx < 0) firstIdx = match.index;
    const kindRaw = (match[1] || "file").toLowerCase();
    const name = (match[2] || "file").trim() || "file";
    const kind: ChatAttachment["kind"] =
      kindRaw === "image" ? "image" : kindRaw === "path" ? "path" : "text";
    if (!attachments.some(a => a.name === name && a.kind === kind)) {
      attachments.push({
        id: `hist-${attachments.length}-${name}`,
        name,
        kind,
        mime: "",
        size: 0,
      });
    }
  }
  if (firstIdx < 0) {
    return {text: raw, attachments: []};
  }
  const head = raw.slice(0, firstIdx).trim();
  return {
    text: head || (attachments.length === 1
      ? `Attached ${attachments[0].name}`
      : `Attached ${attachments.length} files`),
    attachments,
  };
}

function parseStoredAttachments(raw: unknown): ChatAttachment[] {
  if (!Array.isArray(raw)) return [];
  const out: ChatAttachment[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const name = String(row.name || "").trim();
    if (!name) continue;
    const kindRaw = String(row.kind || "text").toLowerCase();
    const kind: ChatAttachment["kind"] =
      kindRaw === "image" ? "image"
        : kindRaw === "path" ? "path"
          : kindRaw === "folder" ? "folder"
            : "text";
    out.push({
      id: `att-${out.length}-${name}`,
      name,
      kind,
      mime: String(row.mime || ""),
      size: Number(row.size) || 0,
    });
  }
  return out;
}

export function parseTurnSteps(raw: unknown): ChatTurnStep[] | undefined {
  if (!Array.isArray(raw) || !raw.length) return undefined;
  const out: ChatTurnStep[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const label = String(row.label || "").trim();
    if (!label) continue;
    const kindRaw = String(row.kind || "note");
    const kind = (
      kindRaw === "tool" || kindRaw === "note" || kindRaw === "step"
      || kindRaw === "thinking"
    ) ? kindRaw as ChatTurnStep["kind"] : "note";
    let status = String(row.status || "done") as ChatTurnStep["status"];
    if (status !== "running" && status !== "ok" && status !== "error" && status !== "done") {
      status = "done";
    }
    // Reloaded steps should never stay "Live".
    if (status === "running") status = "done";
    out.push({
      id: String(row.id || newStepId()),
      kind,
      summaryState: kind === "thinking" && ["running","done","discarded","cancelled"].includes(String(row.status)) && (row.source === "provider_summary" || row.summary_source === "provider_summary")
        ? (row.status === "running" ? "cancelled" : row.status) as ChatTurnStep["summaryState"] : undefined,
      summaryRevision: typeof row.summary_revision === "number" && Number.isSafeInteger(row.summary_revision) ? row.summary_revision : undefined,
      label: label.slice(0, 160),
      detail: row.detail != null ? String(row.detail).slice(0, kind === "thinking" ? 16_000 : 400) : undefined,
      status,
      tool: row.tool != null ? String(row.tool) : undefined,
      key: row.key != null ? String(row.key) : undefined,
      callId: row.call_id != null ? String(row.call_id).slice(0, 128) : undefined,
      rawStatus: row.raw_status != null ? String(row.raw_status).slice(0, 64) : undefined,
      argsPreview: row.args_preview != null ? String(row.args_preview).slice(0, 600) : undefined,
      resultPreview: row.result_preview != null ? String(row.result_preview).slice(0, 800) : undefined,
      startedAt: row.started_at != null ? Number(row.started_at) || undefined : undefined,
      completedAt: row.completed_at != null ? Number(row.completed_at) || undefined : undefined,
      durationMs: row.duration_ms != null ? Math.max(0, Number(row.duration_ms) || 0) : undefined,
      admissionMs: row.admission_ms != null ? Math.max(0, Number(row.admission_ms) || 0) : undefined,
      evidence: parseEvidence(row.evidence),
      peerMessage:parsePeerActivity(row.peer_message),
      ts: row.ts != null ? Number(row.ts) || Date.now() : Date.now(),
    });
    if (out.length >= MAX_TURN_STEPS) break;
  }
  return out.length ? out : undefined;
}

function messageEnrichKey(m: ChatMessage): string {
  return `${m.role}:${Number(m.ts) || 0}:${String(m.text || "").slice(0, 120)}`;
}

function retainedSteps(remote:ChatTurnStep[]|undefined,local:ChatTurnStep[]|undefined):ChatTurnStep[]|undefined {
  if(!remote?.length)return local;
  if(remote.some(step=>!step.peerMessage))return remote;
  return [...(local || []).filter(step=>!remote.some(row=>row.peerMessage?.message_id===step.peerMessage?.message_id && row.peerMessage)),...remote];
}

/** Overlay client-cached steps onto rehydrated messages in the same session. */
export function mergeMessageEnrichment(
  remote: ChatMessage[],
  prior: ChatMessage[],
): ChatMessage[] {
  if (!prior.length) return remote;
  const priorByKey = new Map<string, ChatMessage>();
  for (const m of prior) {
    if (m.role !== "assistant") continue;
    if (!m.steps?.length) continue;
    priorByKey.set(messageEnrichKey(m), m);
  }
  if (!priorByKey.size) return remote;
  return remote.map(m => {
    if (m.role !== "assistant") return m;
    // Prefer durable fields from disk when present.
    if (m.steps?.some(step=>!step.peerMessage)) return m;
    const hit = priorByKey.get(messageEnrichKey(m));
    if (!hit) return m;
    return {
      ...m,
      steps: retainedSteps(m.steps,hit.steps),
      runId: m.runId || hit.runId,
      receipt: m.receipt || hit.receipt,
    };
  });
}

export function parseMessage(raw: unknown): ChatMessage | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  const role = row.role === "user" ? "user" : row.role === "assistant" ? "assistant" : "";
  if (!role) return null;
  const rawOrigin=row.origin && typeof row.origin==="object" ? row.origin as Record<string,unknown> : {};
  const origin:ChatMessage["origin"]=rawOrigin.kind==="peer" && typeof rawOrigin.peer_id==="string" && rawOrigin.peer_id && typeof rawOrigin.message_id==="string" && rawOrigin.message_id
    ? {kind:"peer",peer_id:rawOrigin.peer_id,message_id:rawOrigin.message_id} : undefined;
  const display=row.peer_display as {display_name?:unknown;content?:unknown}|undefined;
  const peerDisplay=origin && display && typeof display.display_name==="string" && typeof display.content==="string" ? {display_name:display.display_name,content:display.content} : undefined;
  let text = String(row.text || "");
  let attachments = parseStoredAttachments(row.attachments);
  // Legacy: whole file bodies were saved on the user message.
  if (role === "user" && !origin) {
    const collapsed = collapseUserAttachmentBodies(text);
    if (collapsed.attachments.length) {
      text = collapsed.text;
      if (!attachments.length) attachments = collapsed.attachments;
    }
  }
  let steps = role === "assistant" ? parseTurnSteps(row.steps) : undefined;
  if(role==="assistant" && Array.isArray(row.peer_sent))for(const value of row.peer_sent){
    const peer=parsePeerActivity(value);if(!peer || steps?.some(step=>step.peerMessage?.message_id===peer.message_id))continue;
    steps=[...(steps || []),{id:`peer:${peer.message_id}`,callId:`peer:${peer.message_id}`,kind:"step",label:`Message to ${peer.target_display_name}`,status:peer.state==="failed" ? "error" : "done",peerMessage:peer,ts:Number(row.ts)||0}];
  }
  if(role==="assistant" && row.peer_sent_more===true)steps=[...(steps || []),{id:`peer-more:${String(row.run_id || row.ts || "")}`,kind:"note",label:"Additional peer messages are available in Peers",status:"done",ts:Number(row.ts)||0}];
  const delivery = row.delivery === "steer" || row.delivery === "follow_up"
    ? row.delivery
    : undefined;
  return {
    role,
    text,
    runId: typeof row.run_id === "string" ? row.run_id : undefined,
    ...(origin ? {origin} : {}),
    ...(peerDisplay ? {peerDisplay} : {}),
    ts: row.ts != null ? Number(row.ts) : undefined,
    ticketId: row.ticket_id != null ? String(row.ticket_id) : undefined,
    activeInputAccepted: !!delivery || undefined,
    activeInputState: delivery ? "delivered" : undefined,
    attachments: attachments.length ? attachments : undefined,
    steps,
    delivery,
    receipt: role === "assistant" ? parseTurnReceipt(row.receipt) : undefined,
  };
}

export function parseMessages(raw: unknown): ChatMessage[] {
  if (!Array.isArray(raw)) return [];
  return raw.map(parseMessage).filter((item): item is ChatMessage => !!item);
}

function attachmentIdentity(message: ChatMessage): string {
  return (message.attachments || [])
    .map(item => {
      const rawName = String(item.name || "").trim().toLowerCase();
      const name = rawName.split(/[\\/]/).filter(Boolean).at(-1) || rawName;
      return `${item.kind}:${name}`;
    })
    .filter(Boolean)
    .sort()
    .join("\u0001");
}

/** Match durable rows to their optimistic counterpart without file payloads. */
export function chatMessagesMatch(local: ChatMessage, remote: ChatMessage): boolean {
  if (local.role !== remote.role) return false;
  if(local.origin || remote.origin)return !!local.origin && !!remote.origin && local.origin.peer_id===remote.origin.peer_id && local.origin.message_id===remote.origin.message_id;
  if (local.ticketId || remote.ticketId) {
    return !!local.ticketId && local.ticketId === remote.ticketId;
  }
  if (String(local.text || "") === String(remote.text || "")) return true;
  // Attachment-only display labels are generated independently in the Deck
  // and backend. Stable attachment metadata is the authoritative identity.
  if (local.role === "user") {
    const localAttachments = attachmentIdentity(local);
    if (localAttachments && localAttachments === attachmentIdentity(remote)) return true;
    // Some legacy/path backends persist a compact attachment marker but no
    // attachment array (notably folders). Names still provide a stable match.
    const names = (local.attachments || [])
      .map(item => {
        const rawName = item.name.trim().toLowerCase();
        return rawName.split(/[\\/]/).filter(Boolean).at(-1) || rawName;
      })
      .filter(Boolean);
    const remoteText = String(remote.text || "").toLowerCase();
    return !!names.length
      && /\[attached\s+(?:file|image|path|folder)\s*:/i.test(remoteText)
      && names.every(name => remoteText.includes(name));
  }
  return false;
}

function isOrderedSubset(
  optimistic: ChatMessage[],
  authoritative: ChatMessage[],
): boolean {
  let cursor = authoritative.length - 1;
  for (let index = optimistic.length - 1; index >= 0; index -= 1) {
    while (cursor >= 0 && !chatMessagesMatch(optimistic[index], authoritative[cursor])) {
      cursor -= 1;
    }
    if (cursor < 0) return false;
    cursor -= 1;
  }
  return true;
}

function mergeLocalEnrichment(
  authoritative: ChatMessage[],
  optimistic: ChatMessage[],
): ChatMessage[] {
  const localByAuthoritativeIndex = new Map<number, ChatMessage>();
  let cursor = authoritative.length - 1;
  for (let index = optimistic.length - 1; index >= 0; index -= 1) {
    const local = optimistic[index];
    while (cursor >= 0 && !chatMessagesMatch(local, authoritative[cursor])) {
      cursor -= 1;
    }
    if (cursor < 0) break;
    localByAuthoritativeIndex.set(cursor, local);
    cursor -= 1;
  }
  return authoritative.map((remote, index) => {
    const local = localByAuthoritativeIndex.get(index);
    if (!local) return remote;
    const localAttachments = local.attachments || [];
    const remoteAttachments = remote.attachments || [];
    const attachments = (remoteAttachments.length ? remoteAttachments : localAttachments).map(
      item => {
        const localItem = localAttachments.find(candidate => (
          candidate.kind === item.kind
          && (candidate.name.split(/[\\/]/).filter(Boolean).at(-1) || candidate.name).toLowerCase()
            === (item.name.split(/[\\/]/).filter(Boolean).at(-1) || item.name).toLowerCase()
        ));
        // Durable transcript rows keep only display metadata. Preserve a local
        // preview URL while the row is mounted, but never retain base64/text or
        // filesystem paths after backend reconciliation.
        return {
          id: item.id,
          name: item.name,
          kind: item.kind,
          mime: item.mime,
          size: item.size,
          previewUrl: localItem?.previewUrl,
        };
      },
    );
    return {
      ...remote,
      runId: remote.runId || local.runId,
      text: remote.role === "user"
        && localAttachments.length
        && (
          local.optimisticDraft === ""
          || /^\s*\[attached\s+(?:file|image|path|folder)\s*:/i.test(remote.text)
        )
          ? local.text
          : remote.text,
      ts: remote.ts ?? local.ts,
      attachments: attachments.length ? attachments : undefined,
      steps: retainedSteps(remote.steps,local.steps),
      receipt: remote.receipt || local.receipt,
      delivery: remote.delivery || local.delivery,
    };
  });
}

export type AuthoritativeTurnReconciliation = {
  messages: ChatMessage[];
  optimisticTurnId: string | null;
  optimisticTurnIds: string[];
  committedAssistant?: ChatMessage;
};

/** Merge a full durable prefix without consuming an unmatched in-flight tail. */
export function reconcileActiveTranscript(local:ChatMessage[],remote:ChatMessage[],activeTurnId:string|null):ChatMessage[] {
  if(!remote.length)return local;
  const matches=new Map<number,ChatMessage>(),tail:ChatMessage[]=[];
  let cursor=0,activeStart=-1;
  for(const message of local) {
    let index=-1;
    if(!message.streaming)for(let probe=cursor;probe<remote.length;probe++) {
      const candidate=remote[probe];
      if(chatMessagesMatch(message,candidate) && (message.optimisticTurnId || message.ts==null || candidate.ts==null || message.ts===candidate.ts)) {index=probe;break;}
    }
    if(index<0){tail.push(message);continue;}
    matches.set(index,message);cursor=index+1;
    if(activeTurnId && message.optimisticTurnId===activeTurnId && activeStart<0)activeStart=index;
  }
  const prefix=remote.map((message,index)=>{
    const prior=matches.get(index);
    const enriched=prior?mergeLocalEnrichment([message],[prior])[0]:message;
    // Keep the active group's boundary so its later chat:appended replaces the
    // whole exchange (including newly recovered intermediate rows) exactly once.
    return {...enriched,...(activeStart>=0 && index>=activeStart?{optimisticTurnId:prior?.optimisticTurnId || activeTurnId!}:{}),
      ...(message.ticketId?{activeInputAccepted:true,activeInputState:"delivered" as const}:{})};
  });
  return [...prefix,...tail];
}

/** Replace one optimistic turn group with the backend's ordered durable rows. */
export function reconcileAuthoritativeTurn(
  localMessages: ChatMessage[],
  authoritative: ChatMessage[],
): AuthoritativeTurnReconciliation {
  if (!authoritative.length) {
    return {messages: localMessages, optimisticTurnId: null, optimisticTurnIds: []};
  }
  const ids: string[] = [];
  for (const message of localMessages) {
    const id = message.optimisticTurnId;
    if (id && !ids.includes(id)) ids.push(id);
  }
  // A completed optimistic turn owns both its initial user bubble and final
  // assistant bubble. Requiring that complete group prevents an unrelated
  // later optimistic user message from being selected just because its text
  // happens to occur in this authoritative transcript.
  const selected = ids.find(id => {
    const group = localMessages.filter(message => message.optimisticTurnId === id);
    return group.some(message => message.role === "assistant")
      && isOrderedSubset(group, authoritative);
  }) || null;
  if (!selected) {
    return {messages: localMessages, optimisticTurnId: null, optimisticTurnIds: []};
  }
  const start = localMessages.findIndex(message => message.optimisticTurnId === selected);
  let end = start;
  for (let index = start; index < localMessages.length; index += 1) {
    if (localMessages[index].optimisticTurnId === selected) end = index;
  }
  // Steering/follow-up bubbles are inserted between the initial user and the
  // final assistant. Consume only tickets present in the durable transcript;
  // queue acceptance alone does not prove delivery.
  const authoritativeTickets = new Set(
    authoritative.map(message => message.ticketId).filter(
      (id): id is string => !!id,
    ),
  );
  const selectedIds = new Set<string>([selected]);
  for (let index = start; index <= end; index += 1) {
    const message = localMessages[index];
    const id = message.optimisticTurnId;
    if (
      id
      && (
        (message.ticketId && authoritativeTickets.has(message.ticketId))
        || authoritativeTickets.has(id)
      )
    ) {
      selectedIds.add(id);
    }
  }
  const optimistic = localMessages.filter(
    message => !!message.optimisticTurnId
      && selectedIds.has(message.optimisticTurnId),
  );
  const before = localMessages.slice(0, start);
  const after = localMessages
    .slice(start)
    .filter(message => !message.optimisticTurnId
      || !selectedIds.has(message.optimisticTurnId));
  const committed = mergeLocalEnrichment(authoritative, optimistic);
  return {
    messages: [
      ...before,
      ...committed,
      ...after,
    ],
    optimisticTurnId: selected,
    optimisticTurnIds: [...selectedIds],
    committedAssistant: [...committed].reverse().find(row => row.role === "assistant"),
  };
}

export {newStepId};
