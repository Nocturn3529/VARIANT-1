import {useEffect} from "react";
import {Icon} from "../ui/Icon";
import {StatusGlyph,type GlyphState} from "../motion/StatusGlyph";
import {dismissProcess,processFinished,refreshExecution,selectProcess,type ProcessSummary} from "../context/terminalStore";
import {chatPaneId,PANE,revealPane} from "../workbench/workbenchStore";

const REFRESH_WHILE_RUNNING_MS=10_000;

export function processGlyph(process:ProcessSummary):GlyphState {
  if(["starting","restarting"].includes(process.state))return "queued";
  if(!processFinished(process.state))return process.state==="running"?"running":"idle";
  if(process.state==="failed" || (process.exitCode!==null && process.exitCode!==0))return "failed";
  if(["terminated","stopped","cancelled"].includes(process.state))return "stopped";
  return "done";
}
export function processStateLabel(process:ProcessSummary):string {
  if(!processFinished(process.state))return process.pid?`${process.state} · pid ${process.pid}`:process.state;
  return process.exitCode===null?process.state:`exited ${process.exitCode}`;
}

/** Keeps this chat's agent-started process list fresh while something is live. */
export function useProcessRefresh(chatId:string,processes:readonly ProcessSummary[],connected:boolean):void {
  const live=processes.some(process=>!processFinished(process.state));
  useEffect(()=>{if(chatId && connected)refreshExecution(chatId);},[chatId,connected]);
  useEffect(()=>{
    if(!chatId || !connected || !live)return;
    const timer=window.setInterval(()=>{if(document.visibilityState!=="hidden")refreshExecution(chatId);},REFRESH_WHILE_RUNNING_MS);
    return ()=>window.clearInterval(timer);
  },[chatId,connected,live]);
}

export function BackgroundProcesses({chatId,processes}:{chatId:string;processes:readonly ProcessSummary[]}) {
  if(!processes.length)return null;
  const running=processes.filter(process=>!processFinished(process.state)).length;
  const show=(id:string)=>{revealPane(chatPaneId(PANE.terminal,chatId),"bottom");selectProcess(id,chatId);};
  return <section className="background-processes" aria-label="Background processes">
    <header><strong><Icon name="process"/>Background processes <span>{processes.length}</span></strong><span>{running ? `${running} running` : "None running"}</span>
      <button type="button" aria-label="Refresh background processes" onClick={()=>refreshExecution(chatId)}><Icon name="refresh"/></button></header>
    <ul>{processes.map(process=><li key={process.id} data-process-state={processGlyph(process)}>
      <StatusGlyph state={processGlyph(process)}/>
      <div className="background-processes__main"><code title={process.cwd ? `${process.command}\n${process.cwd}` : process.command}>{process.command || process.id}</code><small>{processStateLabel(process)}</small></div>
      <div className="background-processes__actions">
        <button type="button" onClick={()=>show(process.id)} aria-label={`Show output of ${process.command || process.id}`}><Icon name="terminal"/>Output</button>
        {processFinished(process.state)?<button type="button" className="is-icon" aria-label={`Dismiss ${process.command || process.id}`} title="Remove from this list" onClick={()=>dismissProcess(process.id,chatId)}><Icon name="close"/></button>:null}
      </div>
    </li>)}</ul>
  </section>;
}
