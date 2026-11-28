import {useEffect, useLayoutEffect, useMemo, useRef, type RefObject} from "react";
import {closeHistoryMatch, loadHistoryWindow, openHistoryMatch, type HistoryMatch, type SessionSearchHit} from "../state/sessionStore";
import type {ChatMessage} from "./types";

function matchIndex(messages: readonly ChatMessage[], hit: SessionSearchHit): number {
  if (hit.kind === "title") return -1;
  if (hit.nodeId) return messages.findIndex(row=>row.nodeId === hit.nodeId);
  // Old search results lack node IDs. Only accept a unique legacy identity.
  if (hit.ts == null || !Number.isFinite(Number(hit.ts))) return -1;
  const candidates = messages.map((row,index)=>({row,index})).filter(({row})=>row.role === hit.role
    && Number.isFinite(row.ts) && row.ts === Number(hit.ts));
  if (candidates.length === 1) return candidates[0].index;
  const snippet = hit.snippet.replace(/(?:…|\.{3})$/,"").replace(/\s+/g," ").trim();
  const matching = candidates.filter(({row})=>snippet && row.text.replace(/\s+/g," ").includes(snippet));
  return matching.length === 1 ? matching[0].index : -1;
}

function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

function passage(element: HTMLElement, query: string, snippet: string): Range | undefined {
  const doc = element.ownerDocument;
  const walker = doc.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    if (!node.parentElement?.closest("button, figcaption, [aria-hidden=true]")) nodes.push(node);
  }
  const text = nodes.map(node => node.data).join("");
  // FTS can return an accent-insensitive prefix. Map folded UTF-16 offsets back
  // to their original spans, including astral characters and combining marks.
  let folded = "";
  const starts: number[] = [], ends: number[] = [];
  for (const cluster of text.matchAll(/\P{M}\p{M}*|\p{M}+/gu)) {
    const normalized = fold(cluster[0]);
    folded += normalized;
    for (let i=0; i<normalized.length; i++) {
      starts.push(cluster.index!);
      ends.push(cluster.index! + cluster[0].length);
    }
  }
  const excerpt = fold(snippet.replace(/^(?:…|\.\.\.)|(?:…|\.\.\.)$/g,"").trim());
  const anchor = excerpt ? folded.indexOf(excerpt) : -1;
  const terms = [fold(query.trim()), ...(fold(query).match(/[\p{L}\p{N}_]+/gu) || [])];
  for (const term of terms) {
    if (!term) continue;
    const inExcerpt = anchor >= 0 ? excerpt.indexOf(term) : -1;
    const at = inExcerpt >= 0 ? anchor + inExcerpt : folded.indexOf(term);
    if (at < 0) continue;
    const start = starts[at], end = ends[at + term.length - 1];
    const range = doc.createRange();
    let offset = 0;
    for (const node of nodes) {
      const next = offset + node.length;
      if (offset <= start && start < next) range.setStart(node,start-offset);
      if (offset < end && end <= next) { range.setEnd(node,end-offset); return range; }
      offset = next;
    }
  }
}

export function HistoryMatchNavigation({match,messages,viewport,reveal}: {
  match: HistoryMatch; messages: readonly ChatMessage[]; viewport: RefObject<HTMLDivElement | null>; reveal: (index:number)=>void;
}) {
  const index = useMemo(()=>matchIndex(messages, match.hit), [messages,match.hit]);
  const revealRef = useRef(reveal);
  revealRef.current = reveal;
  useEffect(()=>{
    if (index < 0 && match.hit.kind === "message" && match.hit.nodeId && !match.error && !match.requestId && !match.window) loadHistoryWindow();
  }, [index,match]);
  useLayoutEffect(()=>{
    const scroll = viewport.current;
    if (!scroll || index < 0) return;
    const view = scroll.ownerDocument.defaultView!;
    revealRef.current(index);
    let frame = 0, timer = 0, article: HTMLElement | null = null, attempts = 0;
    const clearHighlight = () => {
      article?.classList.remove("history-match-active");
      view.CSS?.highlights?.delete("history-match");
    };
    const locate = () => {
      article = scroll.querySelector<HTMLElement>('[data-message-index="' + index + '"]');
      if (!article) {
        if (++attempts < 20) frame = view.requestAnimationFrame(locate);
        return;
      }
      article.classList.add("history-match-active");
      const content = article.querySelector<HTMLElement>(".message__content,.peer-chat__bubble") || article;
      const range = passage(content,match.query,match.hit.snippet);
      const closed = range?.startContainer.parentElement?.closest(".runtime-code-block__body.is-collapsed");
      const expand = closed?.querySelector<HTMLButtonElement>('button[aria-expanded="false"]');
      if (expand && attempts++ < 20) {
        expand.click();
        frame = view.requestAnimationFrame(locate);
        return;
      }
      if (range && typeof view.Highlight !== "undefined") view.CSS?.highlights?.set("history-match",new view.Highlight(range));
      const bounds = range?.getBoundingClientRect() || article.getBoundingClientRect();
      scroll.scrollBy({top:bounds.top - scroll.getBoundingClientRect().top - scroll.clientHeight * .3, behavior:"instant"});
      timer = view.setTimeout(clearHighlight,2600);
    };
    frame = view.requestAnimationFrame(locate);
    return () => { view.cancelAnimationFrame(frame); view.clearTimeout(timer); clearHighlight(); };
  }, [index,match,viewport]);
  const position = "Match " + (match.index+1) + " of " + match.count;
  const context = match.window?.hasAfter ? "Earlier messages · " : match.window?.hasBefore ? "Recent messages · " : "";
  const label = match.hit.kind === "title" ? "Conversation title · " + position : match.error || (match.requestId ? "Loading earlier messages…"
    : index < 0 ? "Message outside the loaded conversation" : context + position);
  return <nav className="history-match-nav" aria-label="Conversation search matches">
    <span role="status">{label}</span>
    {match.error ? <button onClick={loadHistoryWindow}>Retry</button> : null}
    {match.count > 1 ? <button onClick={()=>openHistoryMatch((match.index+1)%match.count)}>Next match</button> : null}
    <button onClick={closeHistoryMatch}>{match.window ? "Return to latest" : "Close search result"}</button>
  </nav>;
}
