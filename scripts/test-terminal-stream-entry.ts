import assert from "node:assert/strict";
import {dismissProcess,disposeTerminalRuntime,getTerminalSnapshot,ingestTerminal,resizeTerminalFor,setTerminalConnection,setTerminalContext} from "../frontend/main-deck/src/context/terminalStore";
export function run(){
  const sent:Record<string,unknown>[]=[];
  setTerminalContext({notify(){},send:row=>{sent.push(row);return true;}});setTerminalConnection("connected");
  const terminal=(cols=80,rows=24)=>({id:"stream-terminal",state:"running",dimensions:{cols,rows}});
  const process={id:"finished-worker",state:"exited"};
  const snapshot=()=>ingestTerminal({type:"execution:snapshot",chat_id:"stream-chat",terminals:[terminal()],processes:[process]});snapshot();
  const page=(frame:Record<string,unknown>,kind="terminal",id="stream-terminal")=>ingestTerminal({type:kind==="process" ? "process:accepted" : "terminal:accepted",chat_id:"stream-chat",operation:kind==="process" ? "logs" : "read",result:{entity:{id,kind},frames:[frame],next_cursor:1}});
  page({data_base64:"4oI=",text:"�"});page({data_base64:"rA==",text:"�"});
  assert.equal(getTerminalSnapshot("stream-chat").output,"€","UTF-8 split between pages is decoded as a single glyph");
  page({text:"x".repeat(2_000_000)});const before=getTerminalSnapshot("stream-chat");page({text:"tail"});const after=getTerminalSnapshot("stream-chat");
  assert.equal(after.outputStart,before.outputStart+4);assert.ok(after.output.endsWith("tail"));assert.equal(after.output.length,2_000_000);
  resizeTerminalFor("stream-terminal",100,30,"stream-chat");resizeTerminalFor("stream-terminal",120,40,"stream-chat");resizeTerminalFor("stream-terminal",125,45,"stream-chat");
  const requests=()=>sent.filter(row=>row.type==="terminal:resize");assert.equal(requests().length,1,"resize flood is single-flight");
  const ack=(request:Record<string,unknown>,cols:number,rows:number)=>ingestTerminal({type:"terminal:accepted",chat_id:"stream-chat",operation:"resize",request_id:request.request_id,result:terminal(cols,rows)});
  const first=requests()[0];ack(first,100,30);assert.equal(requests().length,2);assert.equal(requests()[1].cols,125,"only the newest queued geometry is sent");
  ack(requests()[1],125,45);ack(first,100,30);assert.equal(getTerminalSnapshot("stream-chat").terminals[0].cols,125,"a stale resize ack cannot roll dimensions back");
  resizeTerminalFor("stream-terminal",125,45,"stream-chat");assert.equal(requests().length,2);
  assert.equal(dismissProcess("finished-worker","stream-chat"),true);snapshot();assert.equal(getTerminalSnapshot("stream-chat").processes.length,0,"dismissed exited records stay removed after snapshots");
  disposeTerminalRuntime();setTerminalContext({notify(){},send:()=>true});setTerminalConnection("connected");snapshot();
  assert.equal(getTerminalSnapshot("stream-chat").processes.length,0,"dismissal survives a runtime reload");
  disposeTerminalRuntime();console.log("Terminal stream: incremental UTF-8, bounded buffer offsets, single-flight/latest resize, stale ack fencing and persistent exited-process removal passed");
}
