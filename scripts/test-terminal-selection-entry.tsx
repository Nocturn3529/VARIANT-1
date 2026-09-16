import {createRoot} from "react-dom/client";
import {TerminalPanel} from "../frontend/main-deck/src/context/TerminalPanel";
import {ingestTerminal,setTerminalConnection,setTerminalContext,disposeTerminalRuntime} from "../frontend/main-deck/src/context/terminalStore";
const commands: Array<Record<string,unknown>>=[];
const record=(id:string,state="running")=>({id,state,cwd:"C:/diagnostic",profile:id,transport:"conpty",capabilities:{true_pty:true},dimensions:{cols:80,rows:24}});
const seen=new Set<string>();
setTerminalContext({notify(){},send(message){
  const value=message as Record<string,unknown>;commands.push(value);
  if(value.type==="terminal:read")setTimeout(()=>{
    const id=String(value.terminal_id);const text=seen.has(id)?"":`Retained ${id} output\r\n${id === "live" ? "\x1b[?1049h\x1b[2;12H" : ""}\x1b[6n`;
    seen.add(id);ingestTerminal({type:"terminal:accepted",operation:"read",request_id:value.request_id,result:{entity:{id,kind:"terminal"},frames:text?[{text}]:[],next_cursor:1}});
  },0);
  if(value.type==="terminal:close")setTimeout(()=>ingestTerminal({type:"terminal:accepted",operation:"close",request_id:value.request_id,result:record(String(value.terminal_id),"terminated")}),0);
  if(value.type==="terminal:open")setTimeout(()=>ingestTerminal({type:"terminal:accepted",operation:"open",request_id:value.request_id,result:record("unexpected-new")}),0);
  return true;
}});
setTerminalConnection("connected");
ingestTerminal({type:"execution:snapshot",terminals:[record("archived","exited"),record("live")]});
const root=createRoot(document.getElementById("variant1-react-root")!);
root.render(<div className="app-shell" style={{height:"100vh",display:"flex",flexDirection:"column"}}><TerminalPanel/></div>);
Object.assign(window,{runTerminalSelection:async()=>{
  const pause=(ms:number)=>new Promise(resolve=>setTimeout(resolve,ms));
  await pause(300);
  const writesBefore=commands.filter(c=>c.type==="terminal:write").length;
  const cursorBefore=commands.filter(c=>c.type==="terminal:write").at(-1)?.data;
  [...document.querySelectorAll<HTMLButtonElement>("button")].find(b=>b.textContent==="Clear scrollback")!.click();
  ingestTerminal({type:"terminal:accepted",operation:"read",result:{entity:{id:"live",kind:"terminal"},frames:[{text:"\x1b[6n"}],next_cursor:2}});
  await pause(300);
  const cursorAfter=commands.filter(c=>c.type==="terminal:write").at(-1)?.data;

  document.querySelector<HTMLButtonElement>('.workbench-terminal-rail button[title^="archived"]')!.click();
  await pause(500);
  const result={clearPreservedCursor:!!cursorBefore && cursorBefore===cursorAfter && commands.filter(c=>c.type==="terminal:write").length>writesBefore,clearSignals:commands.filter(c=>c.type==="terminal:signal").length,openCommands:commands.filter(value=>value.type==="terminal:open").length,
    writesToClosed:commands.filter(value=>value.type==="terminal:write"&&value.terminal_id==="archived").length,
    commands:commands.filter(value=>value.type==="terminal:open"||value.type==="terminal:write"),
    emulators:document.querySelectorAll('.xterm').length};
  document.querySelector<HTMLButtonElement>('button[title="Close terminal"]')!.click();
  await pause(100);
  Object.assign(result,{closedArchivedRemoved:!document.querySelector('[data-terminal-id="archived"]')});
  document.querySelector<HTMLButtonElement>('button[title="Close terminal"]')!.click();
  await pause(150);
  Object.assign(result,{remainingTerminals:document.querySelectorAll('[data-terminal-id]').length,remainingEmulators:document.querySelectorAll('.xterm').length});
  disposeTerminalRuntime();root.unmount();return result;
}});
