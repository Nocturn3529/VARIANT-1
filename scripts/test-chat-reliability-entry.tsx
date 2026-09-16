import assert from "node:assert/strict";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {initialChatState,setChatState,setChatContext,getChatState,sharedTurnActive,activateChatState,getCachedChatState,patchChatState} from "../frontend/main-deck/src/chat/stateCore";
import {__resetTurnStoreForTests} from "../frontend/main-deck/src/state/turnStore";
import {__resetSessionStoreForTests,noteDisplayedSession} from "../frontend/main-deck/src/state/sessionStore";
import {sendUserMessage,setChatDraft} from "../frontend/main-deck/src/chat/composer";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {applySession} from "../frontend/main-deck/src/chat/session";
import {parseTurnSteps} from "../frontend/main-deck/src/chat/messages";
import {getPreviewState,closePreview} from "../frontend/main-deck/src/workbench/previewStore";
import {setChatConnection} from "../frontend/main-deck/src/chat/connection";
import {resetWireStatus} from "../frontend/main-deck/src/connectionUi";

export async function run() {
  const sent:Array<Record<string,unknown>>=[];
  function reset() {
    __resetChatStoreForTests();__resetTurnStoreForTests();__resetSessionStoreForTests();
    resetWireStatus("chat");
    setChatContext({send:row=>{sent.push(row);return true;},isOpen:()=>true,notify(){}});
    setChatState({...initialChatState(),sessionId:"A",clientId:"client",connected:true});noteDisplayedSession("A");
    for(const tab of getPreviewState().tabs)closePreview(tab.id);
  }
  const route=(id="one",session="A")=>({session_id:session,client_id:"client",source:"chat",admission_id:`admission-${id}`,run_id:`run-${id}`});
  const incoming=(row:Record<string,unknown>)=>ingestChat(parseChatWsMessage(row));
  const start=(id="one")=>incoming({type:"start",...route(id)});
  const done=(id="one",text="Answer")=>incoming({type:"done",...route(id),text});
  const append=(id:string,messages:Record<string,unknown>[])=>incoming({type:"chat:appended",...route(id),messages,user:messages[0],assistant:messages.at(-1)});
  const activity=(id:string,event:string,call_id:string,replay:boolean)=>incoming({type:"activity",...route(id),event,tool:"browser_navigate",call_id,status:event==="tool:result"?"ok":"running",text:replay?"Replayed result":"Live call",durable_replay:replay});
  reset();sendUserMessage("Question");start();activity("one","tool:start","call-one",false);
  assert.equal(getPreviewState().tabs.length,1,"normal live activity still routes panes");
  done();const completed=getChatState().messages.length;done();
  assert.equal(getChatState().messages.length,completed,"duplicate done is idempotent");
  incoming({type:"token",...route(),token:"late"});incoming({type:"thinking",...route(),text:"late thought"});
  start();assert.equal(sharedTurnActive(),false,"duplicate settled start cannot revive the same admission");
  assert.equal(getChatState().turnActive,false);assert.equal(getChatState().streamText,"");
  for(const tab of getPreviewState().tabs)closePreview(tab.id);
  const receiptBefore=JSON.stringify(getChatState().messages.at(-1)?.receipt);
  activity("one","tool:start","unknown-replay",true);activity("one","tool:result","call-one",true);
  activity("one","tool:result","call-one",true);
  assert.equal(JSON.stringify(getChatState().messages.at(-1)?.receipt),receiptBefore,"replay does not recount tool usage");
  assert.equal(getPreviewState().tabs.length,0,"settled replay cannot reopen a closed browser");
  assert.equal(getChatState().turnSteps.length,0);
  assert.equal(getChatState().messages.at(-1)?.steps?.[0].resultPreview,"Replayed result","late replay may enrich an exact existing call");
  const committed=[{role:"user",text:"Question"},{role:"assistant",text:"Answer"}];
  append("one",committed);append("one",committed);done();
  assert.equal(getChatState().messages.length,2,"late durable commit and duplicate terminal do not duplicate rows");
  assert.equal(getChatState().messages[1].steps?.[0].resultPreview,"Replayed result");
  sendUserMessage("Second question");
  incoming({type:"token",...route(),token:"old admission"});done();start();
  assert.equal(sharedTurnActive(),true);assert.equal(getChatState().streamText,"");assert.equal(getChatState().messages.length,3,"old terminal cannot finish a new unbound turn");
  start("two");activity("two","tool:start","unknown-replay",true);
  assert.equal(getChatState().turnSteps.length,0);assert.equal(getPreviewState().tabs.length,0);
  incoming({type:"token",...route("two"),token:"Current"});assert.equal(getChatState().streamText,"Current");
  incoming({type:"done",...route("two"),text:"Stopped answer",cancelled:true});
  activity("two","tool:start","late-live",false);incoming({type:"token",...route("two"),token:"ghost"});
  assert.equal(getChatState().turnActive,false);assert.equal(getPreviewState().tabs.length,0);
  // A real resumed run can retain a run ID but must have a fresh admission.
  incoming({type:"start",...route("three"),run_id:"run-two"});
  assert.equal(sharedTurnActive(),true);
  incoming({type:"token",session_id:"A",client_id:"client",source:"chat",run_id:"run-two",token:"Resumed token"});
  assert.equal(getChatState().streamText,"Resumed token","fresh admission accepts run-only frames of its resumed logical run");
  incoming({type:"done",...route("three"),run_id:"run-two",text:"Resumed"});

  reset();setChatState({...getChatState(),messages:[{role:"user",text:"repeat"},{role:"assistant",text:"Earlier answer"}]});
  sendUserMessage("repeat");start("active");incoming({type:"token",...route("active"),token:"in-flight token"});
  sendUserMessage("delivered steer");const delivered=getChatState().pendingActiveInputs.at(-1)!.optimisticTurnId;
  sendUserMessage("undelivered tail");const tail=getChatState().pendingActiveInputs.at(-1)!.optimisticTurnId;
  setChatDraft("keep draft");patchChatState({attachments:[{id:"draft-file",kind:"text",name:"draft.txt",mime:"text/plain",size:1,text:"x"}]});
  const history=[{role:"user",text:"repeat"},{role:"assistant",text:"Earlier answer"},{role:"user",text:"repeat"},
    {role:"assistant",text:"Missed intermediate reply"},{role:"user",text:"delivered steer",ticket_id:delivered,delivery:"steer"},
    {role:"assistant",text:"Second intermediate reply"}];
  const runtime={busy:true,active_admission_id:"admission-active",active_run_id:"run-active",pause_state:"running",pause_revision:1};
  const pendingBeforeReconnect=getChatState().pendingActiveInputs.length;
  setChatConnection("offline");setChatConnection("connected");await new Promise(resolve=>setTimeout(resolve,230));
  assert.equal(getChatState().pendingActiveInputs.length,pendingBeforeReconnect,"disconnect does not discard or replay optimistic input");
  applySession({id:"A",messages:history,runtime});applySession({id:"A",messages:history,runtime});
  assert.deepEqual(getChatState().messages.map(row=>row.text),[...history.map(row=>row.text),"undelivered tail"]);
  assert.deepEqual(getChatState().pendingActiveInputs.map(row=>row.optimisticTurnId),[tail]);
  assert.equal(getChatState().streamText,"in-flight token");assert.equal(getChatState().draft,"keep draft");assert.equal(getChatState().attachments.length,1);
  const beforeConflict=getChatState().messages;
  applySession({id:"A",messages:[{role:"user",text:"Different admission prefix"}],runtime:{busy:true,active_admission_id:"other",active_run_id:"other"}});
  assert.deepEqual(getChatState().messages,beforeConflict,"conflicting runtime cannot replace the live transcript");
  done("active","Final active reply");
  append("active",[...history.slice(2),{role:"assistant",text:"Final active reply"}]);
  done("active","Final active reply");
  assert.deepEqual(getChatState().messages.map(row=>row.text),[...history.map(row=>row.text),"Final active reply","undelivered tail"],"durable final append consumes recovered intermediate rows once, preserving undelivered input");
  activateChatState("B");noteDisplayedSession("B");setChatDraft("B draft");sendUserMessage("B work");
  incoming({type:"start",...route("B","B")});const bCount=getChatState().messages.length;
  done("active");append("active",[...history.slice(2),{role:"assistant",text:"Final active reply"}]);
  assert.equal(getChatState().messages.length,bCount);assert.equal(getChatState().sessionId,"B");assert.equal(sharedTurnActive(),true);
  assert.equal(getCachedChatState("A")?.messages.filter(row=>row.text==="Final active reply").length,1);
  reset();sendUserMessage("Send a peer message");start("peer-send");
  const peer={message_id:"canonical-send",sender_peer_id:"chat:A",target_peer_id:"grok:native",target_display_name:"Grok Build",content:"Check the boundary conditions",state:"queued",sender_invocation:{chat_id:"A",run_id:"run-peer-send",outer_tool_call_id:"outer-cell",nested_call_id:"nested-send",cell_execution_id:"cell-1"}};
  const event={type:"activity",...route("peer-send"),event:"peer:sent",call_id:"nested-send",peer_message:peer};incoming(event);incoming(event);
  assert.equal(getChatState().turnSteps.filter(step=>step.peerMessage).length,1,"duplicate send events create only one outgoing trace row");
  assert.equal(getChatState().turnSteps[0].peerMessage?.content,peer.content);
  done("peer-send","Message queued");append("peer-send",[{role:"user",text:"Send a peer message"},{role:"assistant",text:"Message queued",peer_sent:[peer],run_id:"run-peer-send"}]);
  assert.equal(getChatState().messages.at(-1)?.steps?.filter(step=>step.peerMessage).length,1,"canonical historical sends merge without duplicate trace rows");

  reset();sendUserMessage("Keep this live");start("snapshot");
  incoming({type:"token",...route("snapshot"),token:"still streaming"});
  for(const runtime of [{busy:false},{busy:false,active_admission_id:"old",active_run_id:"old"}]) {
    incoming({type:"chat:runtime",id:"A",runtime});
    applySession({id:"A",messages:[{role:"assistant",text:"Old snapshot"}],runtime});
    assert.equal(sharedTurnActive(),true,"unowned idle snapshots cannot settle a bound admission");
    assert.equal(getChatState().streamText,"still streaming");
    assert.equal(getChatState().messages[0].text,"Keep this live");
  }
  incoming({type:"token",...route("snapshot"),token:" and accepting tokens"});
  done("snapshot","Completed normally");assert.equal(getChatState().messages.at(-1)?.text,"Completed normally");

  reset();sendUserMessage("Initial request");start("early");
  patchChatState({messages:getChatState().messages.map(row=>({...row,optimisticAttachmentRetry:[{id:"retry",kind:"image",name:"image.png",mime:"image/png",size:4,data:"data-to-release"}]}))});
  sendUserMessage("Also check this","steer");const ticket=getChatState().pendingActiveInputs.at(-1)!.optimisticTurnId;
  const durable=[{role:"user",text:"Initial request"},{role:"assistant",text:"Intermediate",run_id:"run-early"},
    {role:"user",text:"Also check this",ticket_id:ticket,delivery:"steer"},{role:"assistant",text:"Stopped",run_id:"run-early"}];
  append("early",durable);assert.ok(getChatState().pendingTurnCommit?.appended,"persist-before-done is retained");
  incoming({type:"done",...route("early"),text:"Stopped",cancelled:true});
  assert.deepEqual(getChatState().messages.map(row=>row.text),durable.map(row=>row.text));
  assert.equal(getChatState().activeTurnId,null);assert.equal(getChatState().pendingActiveInputs.length,0);
  assert.ok(getChatState().messages.every(row=>!row.optimisticAttachmentRetry && !row.optimisticTurnId));
  append("early",durable);assert.equal(getChatState().messages.length,4,"duplicate durable terminal cannot append again");

  reset();sendUserMessage("Previous");start("previous");done("previous","Previous reply");
  sendUserMessage("Fail this commit");start("failed");
  incoming({type:"chat:transcript_failed",...route("failed"),error:"persistence_failed"});
  assert.equal(getChatState().messages[1].durability,undefined,"early failure cannot mark previous reply");
  done("failed","Unsaved reply");assert.equal(getChatState().messages.at(-1)?.durability,"failed");
  sendUserMessage("Newer");start("newer");done("newer","Newer reply");
  const latestReceipt=getChatState().messages.at(-1)?.receipt;
  const receiptEvent={type:"run:settled",...route("previous"),receipt:{model:"first-model",duration_ms:1234,prompt_tokens:77}};
  incoming(receiptEvent);
  assert.equal(getChatState().messages[1].receipt?.promptTokens,77);
  assert.deepEqual(getChatState().messages.at(-1)?.receipt,latestReceipt,"late receipt belongs to its exact run");
  assert.equal(sent.at(-2)?.run_id || sent.at(-1)?.run_id,"run-previous");
  const annotationCount=sent.filter(row=>row.type==="chat:session:annotate").length;
  incoming(receiptEvent);incoming({...receiptEvent,run_id:"unknown"});
  assert.equal(sent.filter(row=>row.type==="chat:session:annotate").length,annotationCount,"duplicate or unknown receipt cannot annotate the latest row");
  resetWireStatus("chat");
  reset();sendUserMessage("Inspect the project");start("live-thoughts");
  const summaryId="summary_0123456789abcdef0123456789abcdef";
  const thinking=(status:string,revision:number,text:string,id=summaryId)=>incoming({type:"thinking",...route("live-thoughts"),summary_source:"provider_summary",summary_id:id,status,summary_revision:revision,text,ts:Date.now()});
  thinking("running",0,"");assert.equal(getChatState().turnSteps.length,0,"empty public summary creates no placeholder row");
  thinking("running",1,"**Inspecting**");thinking("running",2,"**Inspecting files**\nChecking project settings.");
  assert.equal(getChatState().turnSteps.length,1);assert.equal(getChatState().turnSteps[0].detail,"**Inspecting files**\nChecking project settings.","public text snapshots replace rather than concatenate");
  thinking("running",1,"old snapshot");assert.equal(getChatState().turnSteps[0].summaryRevision,2);
  thinking("running",3,"");assert.equal(getChatState().turnSteps[0].detail,"","a newer empty snapshot clears supplied text");
  thinking("running",4,"Current summary");thinking("discarded",5,"Current summary");thinking("running",6,"cannot reopen");
  assert.equal(getChatState().turnSteps[0].summaryState,"discarded");
  const retryId="summary_fedcba9876543210fedcba9876543210";
  thinking("running",1,"Independent retry",retryId);thinking("done",2,"Accepted summary",retryId);
  assert.equal(getChatState().turnSteps.length,2);assert.equal(getChatState().turnSteps[1].detail,"Accepted summary");
  for(const status of ["discarded","cancelled","running"]) {
    const [step]=parseTurnSteps([{id:summaryId,kind:"thinking",label:"Thought",source:"provider_summary",status,summary_revision:5,detail:"Public text"}])!;
    assert.equal(step.summaryState,status==="running"?"cancelled":status,"reload retains interrupted/discarded provenance");
    assert.equal(step.status,"done");
  }
  done("live-thoughts","Finished");
  console.log("Chat reliability: terminal idempotence, late-event fences, durable enrichment, active-prefix/optimistic-tail reconciliation and replay side-effect isolation passed");
}
