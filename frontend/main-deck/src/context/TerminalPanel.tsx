import {ConnectorControl} from "../peers/ConnectorControl";
import {Icon} from "../ui/Icon";
import {lazy,Suspense} from "react";
const PersistentTerminalSurface = lazy(() => import("../workbench/TerminalSurface").then(module=>({default:module.PersistentTerminalSurface})));
import {focusMainComposer, useSurfaceDocument} from "../ui/SurfaceDocument";
import {useEffect, useRef, useState} from "react";
import {retainChatDraft} from "../chat/stateCore";
import {getSessionState} from "../state/sessionStore";
import {notifyToast} from "../state/toastStore";
import {usePaneChatOwner} from "../workbench/usePaneChatOwner";
import type {TerminalSelection} from "../workbench/TerminalSurface";
import {bindTerminalSlot} from "../workbench/terminalSlot";
import {
  observeTerminal,
  clearTerminalOutput,
  interruptTerminal,
  killTerminal,
  openNewTerminal,
  refreshExecution,
  selectTerminal,
  useTerminalState,
} from "./terminalStore";

export function TerminalPanel({chatId = ""}: {chatId?:string}) {
  const ownerDocument = useSurfaceDocument();
  const owner = usePaneChatOwner(chatId);
  const state = useTerminalState(chatId);
  const slotRef = useRef<HTMLDivElement | null>(null);
  const active = state.terminals.find(item => item.id === state.activeId);
  // Agent-started processes live in the composer's activity overlay, not here.
  const visibleOutput = state.output;
  const [selection, setSelection] = useState<TerminalSelection | null>(null);
  const selected = selection?.chatId === chatId && selection.id === active?.id ? selection : null;
  const transferLabel = `Add ${selected ? "selection" : "output"} to ${owner.label || "chat"}`;
  const transfer = (text: string) => {
    if (!chatId || !text) return;
    retainChatDraft(chatId, text.slice(-12_000), owner.title);
    const sessions = getSessionState();
    if (sessions.displayedSessionId === chatId && !sessions.pendingAction) focusMainComposer();
    else {
      const surface = ownerDocument.querySelector<HTMLElement>("#native-popout-root > [data-native-surface]")?.dataset.nativeSurface;
      notifyToast(`Added to ${owner.label}`, surface);
    }
  };

  useEffect(() => bindTerminalSlot(slotRef.current, chatId), [ownerDocument, chatId]);

  useEffect(() => observeTerminal(chatId),[chatId]);

  return <section className="workbench-terminal-pane">
    <nav className="workbench-terminal-rail" aria-label="Terminal sessions">
      {state.terminals.map((item, index) => <button
        type="button"
        className={item.id === state.activeId ? "is-active" : ""}
        data-terminal-id={item.id} data-terminal-state={item.state}
        aria-label={item.profile || `Terminal ${index + 1}`}
        aria-pressed={item.id === state.activeId}
        title={`${item.profile || `Terminal ${index + 1}`}\n${item.cwd}`}
        onClick={() => selectTerminal(item.id,chatId)}
        onAuxClick={event => { if (event.button === 1) { selectTerminal(item.id,chatId); killTerminal(chatId); } }}
        key={item.id}
      ><span>{index + 1}</span><i className={item.state === "running" ? "is-running" : ""}/></button>)}
      <button type="button" title={state.opening ? "Opening terminal…" : "New terminal"} aria-label="New terminal" disabled={!state.connected || state.opening} onClick={() => void openNewTerminal(undefined,chatId)}><Icon name="plus"/></button>
    </nav>
    <div className="workbench-terminal-main">
      <header className="workbench-terminal-toolbar">
        <span title={active?.cwd}>{active?.cwd || (state.connected ? "Terminal ready" : "Terminal offline")}</span>
        {active ? <em data-terminal-state={active.state}>{active.state === "running" ? (active.transport || (active.truePty ? "pty" : "running")) : active.state.replaceAll("_"," ")}</em> : null}
        {/* Icon-only actions keep their names for assistive technology and never wrap. */}
        <div className="workbench-terminal-actions">
          <button title="Send Ctrl+C to the program; it may cancel input without exiting" disabled={!active} onClick={() => interruptTerminal(chatId)}><span className="workbench-terminal-kbd" aria-hidden="true">^C</span><span className="deck-sr-only">Interrupt</span></button>
          <button title="Clear scrollback without stopping the program or resetting its display modes" disabled={!active} onClick={() => clearTerminalOutput(chatId)}><Icon name="clear"/><span className="deck-sr-only">Clear scrollback</span></button>
          <button title={transferLabel} aria-label={transferLabel} aria-keyshortcuts={selected ? "Control+L Meta+L" : undefined}
            disabled={!chatId || (!selected && !visibleOutput)} onMouseDown={event => event.preventDefault()}
            onClick={() => transfer(selected ? selected.read() : visibleOutput)}><Icon name="toChat"/></button>
          <ConnectorControl chatId={chatId} terminalId={state.activeId}/>
          <button title="Refresh terminals" onClick={() => refreshExecution(chatId)}><Icon name="refresh"/><span className="deck-sr-only">Refresh terminals</span></button>
          <button title="Close terminal" aria-label="Close terminal" disabled={!active} onClick={() => killTerminal(chatId)}><Icon name="close"/></button>
        </div>
      </header>
      {state.notice ? <div className="workbench-terminal-notice" role="status">{state.notice}</div>:null}
      {state.error ? <div className="workbench-tool-error">{state.error}</div> : null}
      <div ref={slotRef} className="workbench-terminal-slot" aria-label="Interactive terminal"/>
    </div>
    <Suspense fallback={null}><PersistentTerminalSurface chatId={chatId} onSelection={setSelection} onTransfer={transfer}/></Suspense>
  </section>;
}
