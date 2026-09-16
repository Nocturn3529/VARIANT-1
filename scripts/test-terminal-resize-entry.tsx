import {createRoot} from "react-dom/client";
import {createPortal} from "react-dom";
import {TerminalPanel} from "../frontend/main-deck/src/context/TerminalPanel";
import {SurfaceDocumentContext} from "../frontend/main-deck/src/ui/SurfaceDocument";
import {ingestTerminal,setTerminalConnection,setTerminalContext,disposeTerminalRuntime,getTerminalSnapshot,selectProcess} from "../frontend/main-deck/src/context/terminalStore";
const commands:Record<string,unknown>[]=[];
let size={cols:80,rows:24},inFlight=0,maxInFlight=0,storageWrites=0;
const oldSet=Storage.prototype.setItem;
Storage.prototype.setItem=function(key,value){if(key.includes("terminal.scrollback"))storageWrites++;return oldSet.call(this,key,value);};
const record=()=>({id:"tui",state:"running",profile:"Resize diagnostic",dimensions:size,capabilities:{true_pty:true},transport:"conpty"});
const processes=[{id:"old-helper",state:"exited",recipe:{argv:["powershell","old helper"]}},{id:"live-helper",state:"running",recipe:{argv:["powershell","live helper"]}}];
const paint=()=>{
  let text="\x1b[?1049h\x1b[2J";
  for(let row=1;row<=Math.min(size.rows-1,12);row++)text+=`\x1b[${row};1HL${"─".repeat(size.cols-2)}R`;
  text+="\x1b[2;12H";
  ingestTerminal({type:"terminal:accepted",operation:"read",result:{entity:{id:"tui",kind:"terminal"},frames:[{text}],next_cursor:1}});
};
setTerminalContext({notify(){},send(command){commands.push(command);
  if(command.type==="terminal:resize"){
    inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);
    setTimeout(()=>{inFlight--;size={cols:Number(command.cols),rows:Number(command.rows)};ingestTerminal({type:"terminal:accepted",operation:"resize",request_id:command.request_id,result:record()});paint();},70);
  }
  if(command.type==="terminal:read" || command.type==="process:logs")setTimeout(()=>ingestTerminal({type:command.type==="terminal:read" ? "terminal:accepted" : "process:accepted",operation:command.type==="terminal:read" ? "read" : "logs",request_id:command.request_id,result:{entity:{id:command.terminal_id || command.process_id,kind:command.type==="terminal:read" ? "terminal" : "process"},frames:[],next_cursor:1}}),0);
  return true;
}});
setTerminalConnection("connected");ingestTerminal({type:"execution:snapshot",terminals:[record()],processes});paint();
const root=createRoot(document.getElementById("variant1-react-root")!);
const mount=(owner:Document)=>root.render(<SurfaceDocumentContext.Provider value={owner}>{createPortal(<div className="app-shell" id="resize-shell" style={{height:480,width:760,display:"flex",flexDirection:"column"}}><TerminalPanel/></div>,owner.body)}</SurfaceDocumentContext.Provider>);
mount(document);
Object.assign(window,{runTerminalResize:async()=>{
  const pause=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));
  const check=(owner:Document)=>{
    const row=owner.querySelector(".xterm-rows")?.children[4];
    const text=row?.textContent || "";
    const screen=owner.querySelector(".xterm-screen")?.getBoundingClientRect();
    const host=owner.querySelector(".workbench-terminal-instance")?.getBoundingClientRect();
    return {cols:size.cols,textLength:text.length,aligned:text.startsWith("L") && text.endsWith("R") && text.length===size.cols,fits:!!screen && !!host && screen.width<=host.width+1,hidden:owner.hidden,rows:owner.querySelector(".xterm-rows")?.children.length,screen:screen && [screen.width,screen.height],host:host && [host.width,host.height],surface:owner.querySelector(".workbench-terminal-surface")?.getAttribute("class")};
  };
  await pause(450);
  const resizeBefore=commands.filter(row=>row.type==="terminal:resize").length;
  for(let i=0;i<60;i++){document.getElementById("resize-shell")!.style.width=`${440+(i%20)*12}px`;await pause(8);}
  document.getElementById("resize-shell")!.style.width="760px";await pause(400);
  const docked=check(document),resizeRequests=commands.filter(row=>row.type==="terminal:resize").length-resizeBefore;
  const popup=window.open(new URL("popout.html",document.baseURI).href,"terminal-diagnostic","width=800,height=600")!;
  if(!popup)throw new Error("Diagnostic popup unavailable");
  for(let i=0;i<200;i++){if(popup.document.readyState==="complete" && popup.document.getElementById("popout-ready"))break;await pause(20);}
  if(!popup.document.getElementById("popout-ready"))throw new Error("Diagnostic popup did not load");
  await popup.document.fonts.ready;
  mount(popup.document);await pause(450);
  const detached=check(popup.document);
  popup.document.getElementById("resize-shell")!.style.width="520px";await pause(350);
  const detachedResized=check(popup.document);
  mount(document);await pause(450);popup.close();
  const redocked=check(document);
  document.querySelector<HTMLButtonElement>('[aria-label="Remove exited processes"]')!.click();await pause(30);
  ingestTerminal({type:"execution:snapshot",terminals:[record()],processes});await pause(30);
  const endedRemoved=!document.querySelector('[data-process-id="old-helper"]'),livePreserved=!!document.querySelector('[data-process-id="live-helper"]');
  selectProcess("live-helper");const beforeLogs=commands.filter(row=>row.type==="process:logs").length;await pause(450);
  const logReads=commands.filter(row=>row.type==="process:logs").length-beforeLogs;
  const result={docked,detached,detachedResized,redocked,resizeRequests,maxInFlight,storageWrites,endedRemoved,livePreserved,logReads,openCommands:commands.filter(row=>row.type==="terminal:open").length,outputStart:getTerminalSnapshot().outputStart};
  disposeTerminalRuntime();root.unmount();Storage.prototype.setItem=oldSet;return result;
}});
