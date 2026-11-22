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

function passage(element: HTMLElement, query: string): Range | undefined {
  const doc = element.ownerDocument, walker = doc.createTreeWalker(element,NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  const text = nodes.map(node=>node.data).join("");
  const terms = [query, ...(query.match(/[\p{L}\p{N}_-]+/gu) || [])];
  for (const term of terms) {
    const found = new RegExp(term.replace(/[.*+?^{}()|[\]\\$]/g,"\\$&"),"iu").exec(text);
    if (!found?.[0]) continue;
    const start = found.index, end = start + found[0].length;
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
    revealRef.current(index);
    let frame = 0, timer = 0, article: HTMLElement | null = null, attempts = 0;
    const clearHighlight = () => {
      article?.classList.remove("history-match-active");
      globalThis.CSS?.highlights?.delete("history-match");
    };
    const locate = () => {
      article = scroll.querySelector<HTMLElement>('[data-message-index="' + index + '"]');
      if (!article) {
        if (++attempts < 20) frame = requestAnimationFrame(locate);
        return;
      }
      article.classList.add("history-match-active");
      const content = article.querySelector<HTMLElement>(".message__content,.peer-chat__bubble") || article;
      const range = passage(content,match.query);
      if (range && typeof Highlight !== "undefined") globalThis.CSS?.highlights?.set("history-match",new Highlight(range));
      const bounds = range?.getBoundingClientRect() || article.getBoundingClientRect();
      scroll.scrollBy({top:bounds.top - scroll.getBoundingClientRect().top - scroll.clientHeight * .3, behavior:"instant"});
      timer = window.setTimeout(clearHighlight,2600);
    };
    frame = requestAnimationFrame(locate);
    return () => { cancelAnimationFrame(frame); clearTimeout(timer); clearHighlight(); };
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
