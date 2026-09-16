/**
 * Right-edge prompt navigator adapted from Hermes Agent's ThreadTimeline.
 * Hermes Agent is MIT licensed, Copyright (c) 2025 Nous Research.
 */
import {useCallback, useEffect, useRef, useState, type RefObject} from "react";

export type ConversationTimelineEntry = Readonly<{
  id: string;
  preview: string;
  turnIndex: number;
}>;

const MIN_ENTRIES = 4;
const CLOSE_DELAY_MS = 140;

function boundedPreview(value: string, limit = 120): string {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length <= limit ? text : `${text.slice(0, limit - 1).trimEnd()}…`;
}

export function conversationTimelineEntry(id: string, text: string, turnIndex: number): ConversationTimelineEntry | null {
  const preview = boundedPreview(text);
  if (!preview || /^\[IMPORTANT: Background process[\s\S]*\]$/.test(preview)) return null;
  return {id, preview, turnIndex};
}

export function activeConversationIndex(
  entries: readonly ConversationTimelineEntry[],
  scrollTop: number,
  turnTop: (index: number) => number,
  slack = 8,
): number {
  let active = 0;
  entries.forEach((entry, index) => {
    if (turnTop(entry.turnIndex) <= scrollTop + slack) active = index;
  });
  return active;
}

export function ConversationTimeline({
  entries,
  viewportRef,
  turnTop,
  onManualNavigation,
}: {
  entries: readonly ConversationTimelineEntry[];
  viewportRef: RefObject<HTMLDivElement | null>;
  turnTop: (index: number) => number;
  onManualNavigation: () => void;
}) {
  const [active, setActive] = useState(0);
  const [open, setOpen] = useState(false);
  const [everOpened, setEverOpened] = useState(false);
  const closeTimer = useRef<number | null>(null);
  const jumpFrame = useRef<number | null>(null);

  const compute = useCallback(() => {
    const viewport = viewportRef.current;
    if (!viewport || !entries.length) return;
    const following = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight <= 80;
    const next = following
      ? entries.length - 1
      : activeConversationIndex(entries, viewport.scrollTop, turnTop);
    setActive(value => value === next ? value : next);
  }, [entries, turnTop, viewportRef]);

  useEffect(() => {
    if (entries.length < MIN_ENTRIES) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => { frame = 0; compute(); });
    };
    schedule();
    viewport.addEventListener("scroll", schedule, {passive: true});
    return () => {
      viewport.removeEventListener("scroll", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [compute, entries.length, viewportRef]);

  useEffect(() => () => {
    if (closeTimer.current != null) window.clearTimeout(closeTimer.current);
    if (jumpFrame.current != null) window.cancelAnimationFrame(jumpFrame.current);
  }, []);

  if (entries.length < MIN_ENTRIES) return null;

  const keepOpen = () => {
    if (closeTimer.current != null) window.clearTimeout(closeTimer.current);
    setEverOpened(true);
    setOpen(true);
  };
  const closeSoon = () => {
    if (closeTimer.current != null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY_MS);
  };
  const jump = (entry: ConversationTimelineEntry) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    onManualNavigation();
    if (jumpFrame.current != null) window.cancelAnimationFrame(jumpFrame.current);
    const from = viewport.scrollTop;
    const to = Math.max(0, turnTop(entry.turnIndex) - 8);
    const delta = to - from;
    const started = performance.now();
    const animate = (now: number) => {
      const progress = Math.min(1, (now - started) / 170);
      viewport.scrollTop = from + delta * (1 - (1 - progress) ** 3);
      if (progress < 1) jumpFrame.current = window.requestAnimationFrame(animate);
      else jumpFrame.current = null;
    };
    jumpFrame.current = window.requestAnimationFrame(animate);
  };

  return <nav
    className="conversation-timeline"
    aria-label="Conversation timeline"
    onMouseEnter={keepOpen}
    onMouseLeave={closeSoon}
  >
    <div className="conversation-timeline__ticks">
      {entries.map((entry, index) => <button
        type="button"
        aria-label={entry.preview}
        className={index === active ? "is-active" : ""}
        onClick={() => jump(entry)}
        key={entry.id}
      ><span/></button>)}
    </div>
    <div className={`conversation-timeline__popover${open ? " is-open" : ""}`}>
      {everOpened ? entries.map((entry, index) => <button
        type="button"
        className={index === active ? "is-active" : ""}
        onClick={() => jump(entry)}
        key={`row:${entry.id}`}
      >{entry.preview}</button>) : null}
    </div>
  </nav>;
}
