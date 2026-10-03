import {useEffect,useState} from "react";
import {Icon} from "../ui/Icon";
import {ActivityMark,type MarkState} from "../motion/ActivityMark";
import {clearProcessSelection,dismissExitedProcesses,dismissProcess,observeTerminal,processFinished,refreshExecution,selectProcess,useTerminalState,type ProcessSummary} from "../context/terminalStore";

const REFRESH_WHILE_RUNNING_MS=10_000;

export function processMark(process:ProcessSummary):MarkState {
  if(["starting","restarting"].includes(process.state))return "queued";
  if(!processFinished(process.state))return process.state==="running"?"live":"idle";
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

/** Streams one process's output only while its view is open. */
function ProcessOutput({chatId,process,onBack}:{chatId:string;process:ProcessSummary;onBack:()=>void}) {
  const output=useTerminalState(chatId).processOutput;
  useEffect(()=>{
    selectProcess(process.id,chatId);
    const release=observeTerminal(chatId);
    return ()=>{release();clearProcessSelection(chatId);};
  },[chatId,process.id]);
  return <div className="background-processes__detail">
    <button type="button" className="agent-team__back" onClick={onBack}><Icon name="back"/>All processes</button>
    <header><ActivityMark state={processMark(process)}/><code title={process.cwd}>{process.command || process.id}</code><small>{processStateLabel(process)}</small>
      {processFinished(process.state)?<button type="button" onClick={()=>{dismissProcess(process.id,chatId);onBack();}}>Dismiss</button>:null}</header>
    <pre aria-label={`Output of ${process.command || process.id}`}>{output || "No output yet."}</pre>
  </div>;
}

export function BackgroundProcesses({chatId,processes}:{chatId:string;processes:readonly ProcessSummary[]}) {
  const [openId,setOpenId]=useState<string|null>(null);
  if(!processes.length)return null;
  const open=processes.find(process=>process.id===openId);
  if(open)return <section className="background-processes" aria-label="Background processes"><ProcessOutput chatId={chatId} process={open} onBack={()=>setOpenId(null)}/></section>;
  const running=processes.filter(process=>!processFinished(process.state)).length;
  const finished=processes.length-running;
  return <section className="background-processes" aria-label="Background processes">
    <header><span>{running ? `${running} running` : "None running"}{finished?` · ${finished} finished`:""}</span>
      {finished?<button type="button" className="background-processes__clear" onClick={()=>dismissExitedProcesses(chatId)}>Clear finished</button>:null}
      <button type="button" aria-label="Refresh background processes" onClick={()=>refreshExecution(chatId)}><Icon name="refresh"/></button></header>
    <ul>{processes.map(process=><li key={process.id}>
      <button type="button" className="background-processes__row" data-process-id={process.id} data-process-state={processMark(process)} onClick={()=>setOpenId(process.id)}
        aria-label={`Show output of ${process.command || process.id}`} title={process.cwd ? `${process.command}\n${process.cwd}` : process.command}>
        <ActivityMark state={processMark(process)}/>
        <span className="background-processes__main"><code>{process.command || process.id}</code><small>{processStateLabel(process)}</small></span>
        <Icon name="chevron" className="background-processes__go"/>
      </button>
    </li>)}</ul>
  </section>;
}
