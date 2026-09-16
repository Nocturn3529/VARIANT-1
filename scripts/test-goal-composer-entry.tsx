import assert from "node:assert/strict";
import {parseGoalCommand} from "../frontend/main-deck/src/chat/goalCommand";
import {parseComposerGoal} from "../frontend/main-deck/src/protocol/goals";

import {act} from "react";
import {createRoot} from "react-dom/client";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {activateChatState,getCachedChatState,getChatState,getComposerRevision,initialChatState,patchChatState,setChatContext,setChatState} from "../frontend/main-deck/src/chat/stateCore";
import {setChatDraft,submitUserInput} from "../frontend/main-deck/src/chat/composer";
import {refreshComposerGoal} from "../frontend/main-deck/src/chat/goals";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {setChatConnection} from "../frontend/main-deck/src/chat/connection";
import {__resetSessionStoreForTests,noteDisplayedSession,setSessionConnection,setSessionContext} from "../frontend/main-deck/src/state/sessionStore";
import {__resetSessionContextStoreForTests} from "../frontend/main-deck/src/sessionContextStore";
import {__resetTurnStoreForTests} from "../frontend/main-deck/src/state/turnStore";
import {resetWireStatus} from "../frontend/main-deck/src/connectionUi";

export async function run() {
  assert.deepEqual(parseGoalCommand("/goal Build and verify the app"), {objective:"Build and verify the app"});
  assert.deepEqual(parseGoalCommand(" /goal\nKeep multiline\nrequirements "), {objective:"Keep multiline\nrequirements"});
  assert.deepEqual(parseGoalCommand("/goal"), {objective:""});
  for (const text of ["My goal is to build the app", "Explain /goal", "/goals list", "/goalkeeper", "`/goal build`", "/goal: build"]) {
    assert.equal(parseGoalCommand(text),null,`ordinary text must not enter goal mode: ${text}`);
  }
  const record={schema:"variant1.goal.v1",goal_id:"goal-1",owner_chat_id:"A",title:"Build",objective:"Build the app",status:"queued",version:1};
  assert.equal(parseComposerGoal(record)?.goal_id,"goal-1");
  for (const extra of [{version:-1},{version:1.5},{owner_chat_id:null},{goal_id:""},{status:"unknown"},{schema:"other"}]) {
    assert.equal(parseComposerGoal({...record,...extra}),null);
  }

  __resetChatStoreForTests();__resetSessionStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();resetWireStatus("chat");
  const commands:Array<Record<string,unknown>>=[];let online=true;
  const context={send:(command:Record<string,unknown>)=>{commands.push(command);return online;},isOpen:()=>online,notify(){}};
  setChatContext(context);setSessionContext(context);setSessionConnection("connected");
  setChatState({...initialChatState(),sessionId:"A",connected:true});noteDisplayedSession("A");
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  const ingest=async(row:Record<string,unknown>)=>act(async()=>ingestChat(parseChatWsMessage(row)));
  const last=(type:string)=>commands.filter(command=>command.type===type).at(-1)!;
  const click=async(label:string)=>act(async()=>{const button=Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(b=>b.textContent===label || b.getAttribute("aria-label")===label);assert.ok(button,label);button.click();});
  const makeSnapshot=(request:string,version:number,status="running",id="goal-1",session="A")=>({schema:"variant1.goal-snapshot.v1",goal:{...record,goal_id:id,owner_chat_id:session,status,version},submission_request_id:request,completion_basis:"agent_report",objective_outcome:{status:status==="succeeded"?"completed":"unreported",basis:"agent_report",independently_verified:false},cleanup:{status:status==="cancelled"?"complete":"not_requested",complete:status==="cancelled"},capabilities:{pause_scheduling:true,pause_active_work:false,resume:true,cancel:status!=="cancelled",continue:false,retry_cleanup:false,finish:false,archive:false},reports:status==="succeeded"?[{child_id:"child-1",step_id:"s1",status:"succeeded",text:"Tests passed. <script>do not execute</script>",truncated:true,completion_basis:"agent_report"}]:[],steps:[{step_id:"s1",title:"Implement and test",status:"running"}]});
  const reply=async(request:Record<string,unknown>,result:unknown,type="goal:accepted")=>ingest({type,session_id:request.session_id,request_id:request.request_id,operation:String(request.type).split(":").at(-1),result});
  try {
    await act(async()=>{root.render(<ChatComposer/>);});
    const textarea=host.querySelector<HTMLTextAreaElement>("#composer-input")!;
    await act(async()=>setChatDraft("/go"));
    await act(async()=>{textarea.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));});
    assert.equal(getChatState().draft,"/goal ");assert.equal(commands.filter(c=>c.type==="goal:submit").length,0,"menu selection does not submit");
    await click("Start goal");assert.equal(commands.filter(c=>c.type==="goal:submit").length,0,"empty objective stays local");
    await act(async()=>setChatDraft("/goal Build and test the app"));
    await act(async()=>{textarea.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));textarea.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));});
    const submit=last("goal:submit"),request=String(submit.request_id);
    assert.equal(commands.filter(c=>c.type==="goal:submit").length,1);assert.equal(commands.filter(c=>c.type==="chat").length,0);
    assert.equal(submit.objective,"Build and test the app");assert.equal(submit.session_id,"A");
    assert.equal(getChatState().messages.length,0);assert.equal(getChatState().turnActive,false);
    assert.equal(host.querySelector("[data-goal-id]"),null,"pending submit must not fabricate goal identity");
    assert.equal(getChatState().draft,"/goal Build and test the app");
    await reply({...submit,request_id:"wrong"},makeSnapshot(request,2));assert.equal(getChatState().goal.snapshot,null);
    await reply(submit,makeSnapshot(request,2));assert.equal(getChatState().draft,"");
    assert.equal(host.querySelector("[data-goal-id]")?.getAttribute("data-goal-id"),"goal-1");
    assert.ok(host.textContent?.includes("A running step may finish"));assert.equal(host.querySelector('[aria-label="Pause task"]'),null);
    await click("Pause scheduling");const pause=last("goal:pause");assert.equal(pause.expected_version,2);assert.equal(pause.goal_id,"goal-1");
    assert.equal(host.textContent?.includes("Scheduling paused"),false,"click alone is not paused authority");
    await reply(pause,makeSnapshot(request,3,"paused"));assert.ok(host.textContent?.includes("Scheduling paused"));
    await click("Resume goal");const resume=last("goal:resume");assert.equal(resume.expected_version,3);
    await reply(resume,makeSnapshot(request,4,"running"));assert.ok(host.textContent?.includes("Cancel goal"),"Cancel is available without pausing");
    await click("Pause scheduling");await reply(last("goal:pause"),makeSnapshot(request,5,"paused"));
    await click("Cancel goal");const cancel=last("goal:cancel");assert.equal(cancel.expected_version,5);
    await reply(cancel,makeSnapshot(request,6,"cancelled"));assert.ok(host.textContent?.includes("Stopped"));
    await reply(resume,makeSnapshot(request,4,"running"));assert.equal(getChatState().goal.snapshot?.goal.version,6,"late control ack cannot revive goal");

    // A read started before another submission cannot clear the newer identity.
    await act(async()=>refreshComposerGoal());const oldRead=last("goal:current:get");
    await act(async()=>setChatDraft("/goal Next objective"));await click("Start goal");const next=last("goal:submit"),nextId=String(next.request_id);
    await act(async()=>setChatDraft("new unsent draft"));
    await reply(next,makeSnapshot(nextId,1,"queued","goal-2"));assert.equal(getChatState().draft,"new unsent draft");
    await reply(oldRead,null,"goal:current");assert.equal(getChatState().goal.snapshot?.goal.goal_id,"goal-2");
    await ingest({type:"work:event",event:{aggregate:{kind:"goal",id:"goal-2",version:2},scope:{chat_id:"A"}}});
    const read=last("goal:current:get");
    await ingest({type:"work:event",event:{aggregate:{kind:"goal",id:"goal-2",version:3},scope:{chat_id:"A"}}});
    assert.equal(last("goal:current:get"),read,"event bursts coalesce while reading");
    await reply(read,makeSnapshot(nextId,2,"running","goal-2"),"goal:current");const freshRead=last("goal:current:get");assert.notEqual(freshRead.request_id,read.request_id);
    await reply(freshRead,makeSnapshot(nextId,3,"succeeded","goal-2"),"goal:current");assert.ok(host.textContent?.includes("Agent-reported completion"));assert.ok(host.textContent?.includes("Not independently verified"));assert.ok(host.textContent?.includes("Tests passed."));assert.equal(host.querySelector(".composer-goal script"),null);assert.ok(host.textContent?.includes("Report preview truncated"));
    await reply(read,makeSnapshot(nextId,2,"running","goal-2"),"goal:current");assert.equal(getChatState().goal.snapshot?.goal.version,3);

    // A lost submission acknowledgment is resolved only by exact persisted request identity.
    await act(async()=>setChatDraft("/goal Survive reconnect"));await click("Start goal");const ambiguous=last("goal:submit"),ambiguousId=String(ambiguous.request_id);
    await act(async()=>{online=false;setChatConnection("offline");});
    await act(async()=>{online=true;setChatConnection("connected");await new Promise(resolve=>setTimeout(resolve,230));});
    const recover=last("goal:current:get");assert.equal(recover.submission_request_id,ambiguousId);
    assert.equal(commands.filter(c=>c.type==="goal:submit").length,3,"reconnect never replays a submit");
    await reply(recover,makeSnapshot(ambiguousId,4,"running","goal-3"),"goal:current");assert.equal(getChatState().goal.pending,undefined);assert.equal(getChatState().draft,"");
    await act(async()=>refreshComposerGoal());const failedRead=last("goal:current:get");
    await ingest({type:"goal:rejected",session_id:"A",request_id:failedRead.request_id,operation:"current",error:"storage unavailable"});
    assert.equal(getChatState().goal.snapshot?.goal.goal_id,"goal-3");assert.ok(host.textContent?.includes("storage unavailable"));
    await act(async()=>refreshComposerGoal());const malformed=last("goal:current:get");await reply(malformed,{garbage:true},"goal:current");
    assert.equal(getChatState().goal.snapshot?.goal.goal_id,"goal-3");assert.equal(getChatState().goal.refreshRequestId,undefined);
    await click("Refresh goal");const manual=last("goal:current:get");await click("Refresh goal");const manualNew=last("goal:current:get");assert.notEqual(manual.request_id,manualNew.request_id);
    const noControls=makeSnapshot(ambiguousId,5,"running","goal-3");noControls.capabilities={pause_scheduling:false,pause_active_work:false,resume:false,cancel:false,continue:false,retry_cleanup:false,finish:false,archive:false};
    await reply(manualNew,noControls,"goal:current");assert.equal(host.textContent?.includes("Pause scheduling"),false);assert.equal(getChatState().goal.error,undefined);
    await reply(manual,null,"goal:current");assert.equal(getChatState().goal.snapshot?.goal.goal_id,"goal-3");

    // Offscreen events remain session-owned, including rejected actions and hydration.
    await act(async()=>{activateChatState("B");noteDisplayedSession("B");refreshComposerGoal();});const readB=last("goal:current:get");
    await reply(readB,null,"goal:current");await act(async()=>setChatDraft("/goal B objective"));await click("Start goal");const b=last("goal:submit");assert.equal(b.session_id,"B");
    await act(async()=>{activateChatState("A");noteDisplayedSession("A");});await reply(b,makeSnapshot(String(b.request_id),1,"running","goal-B","B"));
    assert.equal(getChatState().goal.snapshot?.goal.goal_id,"goal-3");assert.equal(getCachedChatState("B")?.goal.snapshot?.goal.goal_id,"goal-B");
    await act(async()=>{activateChatState("C");noteDisplayedSession("C");});await act(async()=>setChatDraft("/goal rejected objective"));await click("Start goal");const rejected=last("goal:submit");
    await ingest({type:"goal:rejected",session_id:"C",request_id:rejected.request_id,operation:"submit",error:"goal unavailable"});
    assert.equal(getChatState().draft,"/goal rejected objective");assert.equal(getChatState().goal.pending,undefined);assert.equal(getChatState().messages.length,0);
    await act(async()=>{patchChatState({attachments:[{id:"a",name:"file.txt",kind:"file",size:1,mime:"text/plain",text:"x"}]});});
    const count=commands.filter(c=>c.type==="goal:submit").length;await click("Start goal");assert.equal(commands.filter(c=>c.type==="goal:submit").length,count);assert.equal(getChatState().attachments.length,1);
    await act(async()=>{patchChatState({attachments:[]});setChatDraft("My goal is to improve tests");submitUserInput({source:"composer",sessionId:"C",revision:getComposerRevision(),text:getChatState().draft,attachments:[]});});
    assert.equal(last("chat").text,"My goal is to improve tests","ordinary text remains ordinary chat");
    await act(async()=>{activateChatState("D");noteDisplayedSession("D");refreshComposerGoal();});
    const structured={...makeSnapshot("durable-request",7,"succeeded","goal-D","D"),objective_outcome:{status:"blocked",summary:"Required input absent",basis:"agent_report",independently_verified:false},cleanup:{status:"pending",complete:false},capabilities:{pause_scheduling:false,pause_active_work:false,resume:false,cancel:true,continue:true,retry_cleanup:false,finish:true,archive:false}};
    await reply(last("goal:current:get"),structured,"goal:current");assert.ok(host.textContent?.includes("blocked"));assert.ok(host.textContent?.includes("Cleanup pending"));assert.equal(host.textContent?.includes("Agent-reported completion"),false,"execution succeeded cannot imply objective completion");
    await click("Continue goal");assert.equal(last("goal:continue").goal_id,"goal-D");assert.equal(last("goal:continue").expected_version,7);
    await reply(last("goal:continue"),{...structured,goal:{...structured.goal,status:"running",version:8}});
    await click("Cancel goal");assert.equal(last("goal:cancel").expected_version,8,"cancel does not require pause");
    const cleanupFailed={...structured,goal:{...structured.goal,status:"cancelled",version:9},cleanup:{status:"failed",complete:false},capabilities:{...structured.capabilities,continue:false,cancel:false,retry_cleanup:true}};
    await reply(last("goal:cancel"),cleanupFailed);assert.ok(host.textContent?.includes("Cleanup failed"));assert.equal(host.textContent?.includes("Stopped"),false);
    await click("Retry cleanup");assert.equal(last("goal:cancel").expected_version,9);
    const cleaned={...cleanupFailed,goal:{...cleanupFailed.goal,version:10},cleanup:{status:"complete",complete:true},capabilities:{...cleanupFailed.capabilities,retry_cleanup:false,archive:true}};
    await reply(last("goal:cancel"),cleaned);assert.ok(host.textContent?.includes("Stopped"));
    await click("End goal");assert.equal(last("goal:finish").expected_version,10);
    await reply(last("goal:finish"),{...cleaned,goal:{...cleaned.goal,version:11},termination:{kind:"user_finished"}});assert.ok(host.textContent?.includes("Ended by you"));assert.ok(host.textContent?.includes("blocked"),"ending goal preserves objective outcome");
    await click("Dismiss goal");assert.equal(last("goal:archive").expected_version,11);
    await reply(last("goal:archive"),{...cleaned,goal:{...cleaned.goal,status:"archived",version:12}});assert.equal(getChatState().goal.snapshot,null);assert.equal(host.querySelector('[aria-label="Durable goal"]'),null);

  } finally {
    await act(async()=>root.unmount());host.remove();resetWireStatus("chat");
  }
  console.log("Goal composer: literal command, durable identity, capability controls, completion disclosure, stale replies, reconnect without replay, event coalescing and session isolation passed");
}
