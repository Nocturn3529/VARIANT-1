import {PeerTranscriptMessage} from "../peers/PeerTranscriptMessage";
import {KernelGlyph} from "../motion/KernelGlyph";
/**
 * Virtualized transcript and safe Markdown rendering for the Chat destination.
 */
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  notifyChat,
  speechKeyFor,
  toggleSpeakReply,
  useChatState,
  type ChatAttachment,
  type ChatMessage,
  type ChatTurnStep,
  type SpeechPhase,
} from "../chatStore";
import runtimeLib from "./runtimeLib";
import {RichText} from "./RichText";
import {TurnActivity} from "./TurnActivity";
import {
  ConversationTimeline,
  conversationTimelineEntry,
  type ConversationTimelineEntry,
} from "./ConversationTimeline";

const DEFAULT_TURN_HEIGHT = 180;
const OVERSCAN = 8;
const NEAR_BOTTOM_PX = 80;

type IndexedMessage = {
  message: ChatMessage;
  index: number;
};

type TranscriptTurn = {
  key: string;
  users: IndexedMessage[];
  assistants: IndexedMessage[];
  live: boolean;
  liveSteps: ChatTurnStep[];
  streamingMessage: ChatMessage | null;
};

/**
 * Build visual conversation turns without changing the durable message order.
 * Consecutive user inputs are one prompt group (initial prompt + steering or
 * follow-ups); the assistant rows that follow close that turn.
 */
function buildTranscriptTurns(messages: ChatMessage[]): TranscriptTurn[] {
  const turns: TranscriptTurn[] = [];
  let current: TranscriptTurn | null = null;

  messages.forEach((message, index) => {
    const indexed = {message, index};
    if (message.role === "user") {
      if (!current || current.assistants.length) {
        const seed = message.optimisticTurnId || message.localId || `${message.ts || 0}`;
        current = {
          key: `turn-${index}-${seed}`,
          users: [],
          assistants: [],
          live: false,
          liveSteps: [],
          streamingMessage: null,
        };
        turns.push(current);
      }
      current.users.push(indexed);
      return;
    }

    if (!current) {
      current = {
        key: `turn-${index}-assistant-${message.ts || 0}`,
        users: [],
        assistants: [],
        live: false,
        liveSteps: [],
        streamingMessage: null,
      };
      turns.push(current);
    }
    current.assistants.push(indexed);
  });

  return turns;
}

function addLiveTurn(
  turns: TranscriptTurn[],
  messages: ChatMessage[],
  activeTurnId: string | null,
  steps: ChatTurnStep[],
  streaming: boolean,
  streamText: string,
): TranscriptTurn[] {
  const next = [...turns];
  let targetIndex = activeTurnId
    ? next.findIndex(turn => turn.users.some(({message}) => message.optimisticTurnId === activeTurnId))
    : -1;
  if (targetIndex < 0 && next.length && !next[next.length - 1].assistants.length) {
    targetIndex = next.length - 1;
  }
  let target: TranscriptTurn;
  if (targetIndex < 0) {
    target = {
      key: `turn-live-${activeTurnId || messages.length}`,
      users: [],
      assistants: [],
      live: true,
      liveSteps: [],
      streamingMessage: null,
    };
    next.push(target);
  } else {
    target = {...next[targetIndex]};
    next[targetIndex] = target;
  }
  target.live = true;
  target.liveSteps = steps;
  target.streamingMessage = streaming && streamText
    ? {
        role: "assistant",
        text: streamText,
        ts: Date.now() / 1000,
        streaming: true,
      }
    : null;
  return next;
}

function stepsForTurn(turn: TranscriptTurn): ChatTurnStep[] {
  if (turn.live || (!turn.assistants.length && turn.liveSteps.length)) return turn.liveSteps;
  const seen = new Set<string>();
  const steps: ChatTurnStep[] = [];
  turn.assistants.forEach(({message}) => {
    (message.steps || []).forEach(step => {
      if (seen.has(step.id)) return;
      seen.add(step.id);
      steps.push(step);
    });
  });
  return steps;
}

function addFailedActivityTurn(turns: TranscriptTurn[], steps: ChatTurnStep[]): TranscriptTurn[] {
  if (!steps.length) return turns;
  return [...turns, {
    key: `turn-failed-${steps[0]?.callId || steps[0]?.id || "activity"}`,
    users: [],
    assistants: [],
    live: false,
    liveSteps: steps,
    streamingMessage: null,
  }];
}

function lib(): typeof runtimeLib {
  return runtimeLib;
}

function formatTime(ts?: number): string {
  if (lib().formatTime) return lib().formatTime!(ts);
  const date = new Date((Number(ts) || Date.now() / 1000) * 1000);
  return date.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}

// ── Safe Markdown → React ───────────────────────────────────────────────────

function copyText(value: string, ok: string) {
  navigator.clipboard.writeText(value || "")
    .then(() => notifyChat(ok))
    .catch(() => notifyChat("Couldn't copy"));
}

function AttachmentStrip({items}: {items?: ChatAttachment[]}) {
  if (!items?.length) return null;
  return <div className="message__attachments" aria-label="Attachments">
    {items.map(item => (
      item.kind === "image" && item.previewUrl
        ? <img
            key={item.id}
            className="message__attachment-thumb"
            src={item.previewUrl}
            alt={item.name}
            title={item.name}
          />
        : <span key={item.id} className="message__attachment-file" title={item.name}>
            📄 {item.name}
          </span>
    ))}
  </div>;
}

function playLabel(phase: SpeechPhase, active: boolean): string {
  if (!active) return "Play";
  if (phase === "loading") return "…";
  if (phase === "playing") return "Stop";
  return "Play";
}

function MessageArticle({
  message,
  index,
  streaming,
  speechKey,
  speechPhase,
  latest,
}: {
  message: ChatMessage;
  index: number;
  streaming?: boolean;
  speechKey: string | null;
  speechPhase: SpeechPhase;
  latest?: boolean;
}) {
  if(message.origin)return <PeerTranscriptMessage message={{...message,origin:message.origin}}/>;
  const role = message.role === "user" ? "user" : "assistant";
  const isUser = role === "user";
  const text = streaming ? (message.text || "…") : (message.text || "");
  const key = speechKeyFor(message, index);
  const speechActive = !isUser && !streaming && speechKey === key;
  const phase = speechActive ? speechPhase : "idle";

  return <article
    className={`message message--${isUser ? "user" : "assistant"}${streaming ? " runtime-streaming" : ""}`}
    aria-label={isUser ? "You" : "VARIANT-1"}
  >
    <div className="message__body">
      {isUser ? <>
        <div className="message__prompt">
          <AttachmentStrip items={message.attachments} />
          {text ? <div className="message__content">
            <RichText value={text} />
          </div> : null}
        </div>
        <div className="message__meta">
          <time>{formatTime(message.ts)}</time>
          {message.delivery ? (
            <em className={`message__delivery message__delivery--${message.delivery}`}>
              {message.activeInputState === "delivered"
                ? (message.delivery === "steer" ? "Steered" : "Delivered")
                : (message.delivery === "steer" ? "Steer pending" : "Queued")}
            </em>
          ) : null}
        </div>
      </> : <>
        <div className="message__content" aria-busy={streaming || undefined}>
          <RichText value={text} streaming={streaming} />
        </div>
        {!streaming ? <div className={`message-actions runtime-message-actions${latest ? " is-latest" : ""}`}>
          {message.durability === "failed" ? (
            <span role="status" title="This reply was not committed to chat history">Not saved</span>
          ) : null}
          <button
            type="button"
            className={speechActive ? `message-action--speech-${phase}` : undefined}
            title={phase === "playing" ? "Stop speaking" : phase === "loading" ? "Synthesizing speech…" : "Play reply"}
            aria-label={phase === "playing" ? "Stop speaking" : "Play reply"}
            aria-pressed={phase === "playing" || phase === "loading"}
            onClick={() => toggleSpeakReply(message.text || "", key)}
          >
            {playLabel(phase, speechActive)}
          </button>
          <button
            type="button"
            title="Copy"
            onClick={() => copyText(message.text || "", "Response copied")}
          >
            Copy
          </button>
        </div> : null}
      </>}
    </div>
  </article>;
}

const ChatTurnGroup = memo(function ChatTurnGroup({
  turn,
  speechKey,
  speechPhase,
  latestAssistantIndex,
}: {
  turn: TranscriptTurn;
  speechKey: string | null;
  speechPhase: SpeechPhase;
  latestAssistantIndex: number;
}) {
  const steps = stepsForTurn(turn);
  const hasResponse = turn.assistants.length > 0 || Boolean(turn.streamingMessage);

  const turnStartedAt = Math.max(0, Number(
    turn.users[0]?.message.ts || turn.assistants[0]?.message.ts || Date.now() / 1000,
  ) * 1000);
  return <div
    className={`chat-turn${turn.live ? " chat-turn--live" : ""}`}
    role="group"
    aria-label={turn.live ? "Current conversation turn" : "Conversation turn"}
  >
    {turn.users.length ? <div className="chat-turn__prompts">
      {turn.users.map(({message, index}) => (
        <MessageArticle
          key={`${index}-${message.ts || 0}-user`}
          message={message}
          index={index}
          speechKey={speechKey}
          speechPhase={speechPhase}
        />
      ))}
    </div> : null}

    {turn.live || !hasResponse ? <TurnActivity
      steps={steps}
      live={turn.live}
      streamText={turn.streamingMessage?.text || ""}
      turnStartedAt={turnStartedAt}
    /> : null}

    {hasResponse ? <div className="chat-turn__responses">
      {turn.assistants.map(({message, index}) => (
        <div className="chat-response-segment" key={`${index}-${message.ts || 0}-response`}>
        {!turn.live ? <TurnActivity steps={message.steps || []} live={false} streamText="" turnStartedAt={turnStartedAt}/> : null}
        <MessageArticle
          key={`${index}-${message.ts || 0}-assistant`}
          message={message}
          index={index}
          speechKey={speechKey}
          speechPhase={speechPhase}
          latest={!turn.live && index === latestAssistantIndex}
        />
        </div>
      ))}
      {turn.streamingMessage ? <MessageArticle
        message={turn.streamingMessage}
        index={latestAssistantIndex + 1}
        streaming
        speechKey={speechKey}
        speechPhase={speechPhase}
      /> : null}
    </div> : null}
  </div>;
});

// ── Virtualized turn list ───────────────────────────────────────────────────

export function ChatMessageList() {
  const {
    messages, streaming, streamText, turnActive, speechKey, speechPhase, turnSteps,
    lastError, activeTurnId, runtime, sessionId,
  } = useChatState();
  const transcriptTurns = useMemo(() => buildTranscriptTurns(messages), [messages]);
  const turns = useMemo(() => (
    streaming || turnActive
      ? addLiveTurn(
          transcriptTurns,
          messages,
          activeTurnId,
          turnSteps,
          streaming,
          streamText,
        )
      : lastError && turnSteps.length
        ? addFailedActivityTurn(transcriptTurns, turnSteps)
        : transcriptTurns
  ), [
    activeTurnId,
    lastError,
    messages,
    streaming,
    streamText,
    transcriptTurns,
    turnActive,
    turnSteps,
  ]);
  const latestAssistantIndex = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "assistant") return index;
    }
    return -1;
  }, [messages]);
  const userTurnIndexes = useMemo(() => transcriptTurns
    .map((turn, index) => turn.users.length ? index : -1)
    .filter(index => index >= 0), [transcriptTurns]);
  const timelineEntries = useMemo(() => transcriptTurns.flatMap((turn, turnIndex) => (
    turn.users.flatMap(({message, index}) => {
      const id = message.localId || message.optimisticTurnId || `${turn.key}:${index}:${message.ts || 0}`;
      const entry = conversationTimelineEntry(id, message.text || "", turnIndex);
      return entry ? [entry] : [];
    })
  )) as ConversationTimelineEntry[], [transcriptTurns]);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const columnRef = useRef<HTMLDivElement | null>(null);
  const heightsRef = useRef<number[]>([]);
  const stickToBottomRef = useRef(true);
  const turnWasActiveRef = useRef(streaming || turnActive);
  const announceWasActiveRef = useRef(streaming || turnActive);
  const lastStreamingTextRef = useRef("");
  const assistantCountAtTurnStartRef = useRef(
    messages.filter(item => item.role === "assistant").length,
  );
  const [windowRange, setWindowRange] = useState({start: 0, end: 0, topPad: 0, bottomPad: 0});
  const [scrollVersion, setScrollVersion] = useState(0);
  const [completedAnnouncement, setCompletedAnnouncement] = useState("");

  const total = turns.length;

  // Keep heights array sized to the visual turn list.
  useEffect(() => {
    const heights = heightsRef.current;
    while (heights.length < total) heights.push(DEFAULT_TURN_HEIGHT);
    if (heights.length > total) heights.length = total;
  }, [total]);

  const computeWindow = useCallback((forceBottom = false) => {
    const scroll = scrollRef.current;
    const viewportHeight = scroll ? scroll.clientHeight : 600;
    let scrollTop = scroll ? scroll.scrollTop : 0;
    const heights = heightsRef.current;

    if (forceBottom || stickToBottomRef.current) {
      const sumFn = lib().sumMessageHeights;
      const totalH = sumFn
        ? sumFn(heights, 0, total, DEFAULT_TURN_HEIGHT)
        : total * DEFAULT_TURN_HEIGHT;
      scrollTop = Math.max(0, totalH - viewportHeight);
    }

    const vw = lib().virtualWindow;
    if (vw) {
      return vw({
        total,
        scrollTop,
        viewportHeight,
        heights,
        defaultHeight: DEFAULT_TURN_HEIGHT,
        overscan: OVERSCAN,
      });
    }
    // Fallback: render everything (short sessions / missing lib).
    return {start: 0, end: total, topPad: 0, bottomPad: 0, totalHeight: total * DEFAULT_TURN_HEIGHT};
  }, [total]);

  const recompute = useCallback((forceBottom = false) => {
    const next = computeWindow(forceBottom);
    setWindowRange({
      start: next.start,
      end: next.end,
      topPad: next.topPad,
      bottomPad: next.bottomPad,
    });
  }, [computeWindow]);

  const turnTop = useCallback((index: number) => {
    const sumFn = lib().sumMessageHeights;
    return sumFn
      ? sumFn(heightsRef.current, 0, index, DEFAULT_TURN_HEIGHT)
      : heightsRef.current.slice(0, index).reduce(
        (sum, height) => sum + (height || DEFAULT_TURN_HEIGHT), 0,
      );
  }, []);

  useLayoutEffect(() => {
    recompute(stickToBottomRef.current);
  }, [turns, scrollVersion, recompute]);

  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    if (stickToBottomRef.current) {
      scroll.scrollTop = scroll.scrollHeight;
    }
  }, [turns, windowRange]);

  useEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll) return;
    const onScroll = () => {
      const near = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight <= NEAR_BOTTOM_PX;
      stickToBottomRef.current = near;
      setScrollVersion(v => v + 1);
    };
    scroll.addEventListener("scroll", onScroll, {passive: true});
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => setScrollVersion(v => v + 1));
      ro.observe(scroll);
    }
    return () => {
      scroll.removeEventListener("scroll", onScroll);
      ro?.disconnect();
    };
  }, []);

  // Measure visible rows after paint.
  useLayoutEffect(() => {
    const column = columnRef.current;
    if (!column) return;
    let changed = false;
    column.querySelectorAll<HTMLElement>("[data-virt-index]").forEach(el => {
      const index = Number(el.dataset.virtIndex);
      if (!Number.isFinite(index) || index < 0) return;
      const style = window.getComputedStyle(el);
      const margin = (parseFloat(style.marginTop) || 0) + (parseFloat(style.marginBottom) || 0);
      const full = Math.ceil(el.getBoundingClientRect().height + margin);
      if (full > 0 && heightsRef.current[index] !== full) {
        heightsRef.current[index] = full;
        changed = true;
      }
    });
    if (changed) recompute(false);
  }, [windowRange, turns, recompute]);

  // A new turn starts at the tail. After that, scroll events own stickiness:
  // token/step churn must not drag a reader back down after they scroll up.
  useEffect(() => {
    const active = streaming || turnActive;
    if (active && !turnWasActiveRef.current) {
      stickToBottomRef.current = true;
      setScrollVersion(version => version + 1);
    }
    turnWasActiveRef.current = active;
  }, [streaming, turnActive]);

  // Announce one durable completion instead of sending every streaming token
  // through a live region. The ref preserves the final partial text when the
  // backend clears streamText in the same update that closes the turn.
  useEffect(() => {
    const active = streaming || turnActive;
    if (streaming && streamText.trim()) lastStreamingTextRef.current = streamText;
    if (active && !announceWasActiveRef.current) {
      assistantCountAtTurnStartRef.current = messages.filter(item => item.role === "assistant").length;
      setCompletedAnnouncement("");
    }
    if (!active && announceWasActiveRef.current) {
      const assistants = messages.filter(item => item.role === "assistant" && item.text);
      const latest = assistants.length > assistantCountAtTurnStartRef.current
        ? assistants[assistants.length - 1]
        : null;
      const text = String(latest?.text || lastStreamingTextRef.current || "").trim();
      setCompletedAnnouncement(lastError ? "" : (text ? `VARIANT-1 replied: ${text}` : "VARIANT-1 finished responding."));
      lastStreamingTextRef.current = "";
    }
    announceWasActiveRef.current = active;
  }, [messages, streaming, streamText, turnActive, lastError]);

  const slice = useMemo(
    () => turns.slice(windowRange.start, windowRange.end),
    [turns, windowRange.start, windowRange.end],
  );

  const empty = !turns.length;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (document.querySelector("dialog[open]")) return;
      const target = event.target as HTMLElement | null;
      const inField = Boolean(target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT"));
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "c") {
        if (inField || window.getSelection()?.toString()) return;
        const last = [...messages].reverse().find(item => item.role === "assistant" && item.text);
        if (!last) return;
        event.preventDefault();
        copyText(last.text, "Response copied");
        return;
      }
      if (!event.shiftKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
      if (inField) return;
      if (!userTurnIndexes.length) return;
      event.preventDefault();
      const scroll = scrollRef.current;
      if (!scroll) return;
      const heights = heightsRef.current;
      const sumFn = lib().sumMessageHeights;
      const topFor = (index: number) => sumFn
        ? sumFn(heights, 0, index, DEFAULT_TURN_HEIGHT)
        : heights.slice(0, index).reduce((sum, height) => sum + (height || DEFAULT_TURN_HEIGHT), 0);
      const positions = userTurnIndexes.map(index => ({index, top: topFor(index)}));
      const targetPosition = event.key === "ArrowUp"
        ? ([...positions].reverse().find(item => item.top < scroll.scrollTop - 1) || positions[0])
        : (positions.find(item => item.top > scroll.scrollTop + 1) || positions[positions.length - 1]);
      if (!targetPosition) return;
      stickToBottomRef.current = false;
      scroll.scrollTop = targetPosition.top;
      recompute(false);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        scroll.querySelector<HTMLElement>(`[data-virt-index="${targetPosition.index}"]`)?.focus({preventScroll: true});
      }));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [messages, recompute, userTurnIndexes]);

  return <div className="messages-frame">
  <div
    className="chat-completion-announcement"
    role="status"
    aria-live="polite"
    aria-atomic="true"
  >{completedAnnouncement}</div>
  {lastError ? <div className="chat-turn-error" role="alert">{lastError}</div> : null}
  <div className="messages-scroll" id="messages-scroll" ref={scrollRef}>
    <div className="message-column" id="message-column" ref={columnRef}>
      {empty ? <div className="runtime-chat-empty">
          <KernelGlyph seed={`${sessionId}:${runtime?.kernelGeneration}`} mutation={runtime?.mutationEffectiveEnabled} size={112} ascii phase="idle"/>
        <strong>What are we working on?</strong>
        <small>A conversation with a persistent Python workspace.</small><small className="empty-chat-hint">Enter to send · Ctrl K for actions</small>
      </div> : <>
        <div
          className="virt-pad virt-pad--top"
          aria-hidden="true"
          style={{height: Math.max(0, Math.round(windowRange.topPad))}}
        />
        {slice.map((turn, offset) => {
          const index = windowRange.start + offset;
          return <div
            className="virt-item"
            data-virt-index={index}
            data-turn-has-user={turn.users.length ? "true" : undefined}
            tabIndex={-1}
            key={turn.key}
          >
            <ChatTurnGroup
              turn={turn}
              speechKey={speechKey}
              speechPhase={speechPhase}
              latestAssistantIndex={latestAssistantIndex}
            />
          </div>;
        })}
        <div
          className="virt-pad virt-pad--bottom"
          aria-hidden="true"
          style={{height: Math.max(0, Math.round(windowRange.bottomPad))}}
        />
      </>}
    </div>
  </div>
  <ConversationTimeline
    entries={timelineEntries}
    viewportRef={scrollRef}
    turnTop={turnTop}
    onManualNavigation={() => { stickToBottomRef.current = false; }}
  />
  </div>;
}
