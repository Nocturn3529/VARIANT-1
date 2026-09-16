import {ConnectorControl} from "../peers/ConnectorControl";
import {lazy,Suspense} from "react";
const PersistentTerminalSurface = lazy(() => import("../workbench/TerminalSurface").then(module=>({default:module.PersistentTerminalSurface})));
import {focusMainComposer, useSurfaceDocument} from "../ui/SurfaceDocument";
import {useEffect, useRef} from "react";
import {retainChatDraft} from "../chat/stateCore";
import {bindTerminalSlot} from "../workbench/terminalSlot";
import {
  observeTerminal,
  clearTerminalOutput,
  clearProcessSelection,
  dismissProcess,dismissExitedProcesses,processFinished,
  interruptTerminal,
  killTerminal,
  openNewTerminal,
  refreshExecution,
  selectTerminal,
  selectProcess,
  useTerminalState,
} from "./terminalStore";

export function TerminalPanel({chatId = ""}: {chatId?:string}) {
  const ownerDocument = useSurfaceDocument();
  const state = useTerminalState(chatId);
  const slotRef = useRef<HTMLDivElement | null>(null);
  const active = state.terminals.find(item => item.id === state.activeId);
  const process = state.processes.find(item => item.id === state.selectedProcessId);
  const visibleOutput = process ? state.processOutput : state.output;

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
      {state.processes.map((item, index) => <button
        type="button"
        className={item.id === state.selectedProcessId ? "is-active is-process" : "is-process"}
        aria-label={item.command || `Process ${index + 1}`}
        aria-pressed={item.id === state.selectedProcessId}
        title={`${item.command || `Process ${index + 1}`}\n${item.cwd}`}
        onClick={() => selectProcess(item.id,chatId)}
        onAuxClick={event=>{if(event.button===1)dismissProcess(item.id,chatId);}}
        data-process-id={item.id}
        key={item.id}
      ><span>P{index + 1}</span><i className={["starting", "running", "restarting"].includes(item.state) ? "is-running" : ""}/></button>)}
      <button type="button" title={state.opening ? "Opening terminal…" : "New terminal"} aria-label="New terminal" disabled={!state.connected || state.opening} onClick={() => void openNewTerminal(undefined,chatId)}>+</button>
      {state.processes.some(item=>processFinished(item.state)) ? <button type="button" title="Remove exited processes from this list" aria-label="Remove exited processes" onClick={()=>dismissExitedProcesses(chatId)}>×</button>:null}
    </nav>
    <div className="workbench-terminal-main">
      <header className="workbench-terminal-toolbar">
        <span title={process?.command || active?.cwd}>{process?.command || active?.cwd || (state.connected ? "Terminal ready" : "Terminal offline")}</span>
        <em>{process ? `process · ${process.state}` : active ? (active.state === "running" ? (active.truePty ? "ConPTY" : active.transport) : active.state) : ""}</em>
        <ConnectorControl chatId={chatId} terminalId={state.activeId}/>
        <button title="Refresh terminals" onClick={() => refreshExecution(chatId)}>↻</button>
        <button title="Send Ctrl+C to the program; it may cancel input without exiting" disabled={!active || !!process} onClick={() => interruptTerminal(chatId)}>Interrupt</button>
        <button title="Clear scrollback without stopping the program or resetting its display modes" disabled={!active || !!process} onClick={() => clearTerminalOutput(chatId)}>Clear scrollback</button>
        <button title="Add visible output to chat" disabled={!visibleOutput} onClick={() => {
          retainChatDraft(chatId,visibleOutput.slice(-12_000));
          focusMainComposer();
        }}>Add to chat</button>
        <button title={process ? processFinished(process.state) ? "Remove exited process" : "Close process mirror" : "Close terminal"} aria-label={process ? processFinished(process.state) ? "Remove exited process" : "Close process mirror" : "Close terminal"} disabled={!active && !process} onClick={() => process ? processFinished(process.state) ? dismissProcess(process.id,chatId) : clearProcessSelection(chatId) : killTerminal(chatId)}>×</button>
      </header>
      {state.notice ? <div className="workbench-terminal-notice" role="status">{state.notice}</div>:null}
      {state.error ? <div className="workbench-tool-error">{state.error}</div> : null}
      {process ? <pre className="workbench-process-output">{state.processOutput || "No output yet."}</pre> : null}
      <div ref={slotRef} className="workbench-terminal-slot" aria-label="Interactive terminal" style={{display: process ? "none" : undefined}}/>
    </div>
    <Suspense fallback={null}><PersistentTerminalSurface chatId={chatId}/></Suspense>
  </section>;
}
