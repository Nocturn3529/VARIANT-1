import assert from "node:assert/strict";
import {parseGoalCommand} from "../frontend/main-deck/src/chat/goalCommand";
import {parseChatGoal,parseGoalMessage,goalStatusLabel,formatGoalDuration} from "../frontend/main-deck/src/protocol/goals";

import {act} from "react";
import {createRoot} from "react-dom/client";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {ChatMessageList} from "../frontend/main-deck/src/chat/ChatMessageList";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {activateChatState,getCachedChatState,getChatState,getComposerRevision,initialChatState,patchChatState,setChatContext,setChatState} from "../frontend/main-deck/src/chat/stateCore";
import {setChatDraft,submitUserInput} from "../frontend/main-deck/src/chat/composer";
import {refreshComposerGoal} from "../frontend/main-deck/src/chat/goals";
import {parseMessage} from "../frontend/main-deck/src/chat/messages";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {setChatConnection} from "../frontend/main-deck/src/chat/connection";
import {__resetSessionStoreForTests,noteDisplayedSession,setSessionConnection,setSessionContext} from "../frontend/main-deck/src/state/sessionStore";
import {__resetSessionContextStoreForTests} from "../frontend/main-deck/src/sessionContextStore";
import {__resetTurnStoreForTests} from "../frontend/main-deck/src/state/turnStore";
import {resetWireStatus} from "../frontend/main-deck/src/connectionUi";

const typeInto=(field:HTMLTextAreaElement,value:string)=>{
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,"value")!.set!.call(field,value);
  field.dispatchEvent(new Event("input",{bubbles:true}));
};

export async function run() {
  assert.deepEqual(parseGoalCommand("/goal Build and verify the app"), {objective:"Build and verify the app"});
  assert.deepEqual(parseGoalCommand(" /goal\nKeep multiline\nrequirements "), {objective:"Keep multiline\nrequirements"});
  for (const text of ["My goal is to build the app", "Explain /goal", "/goals list", "/goalkeeper", "`/goal build`", "/goal: build"]) {
    assert.equal(parseGoalCommand(text),null,`ordinary text must not enter goal mode: ${text}`);
  }
  const goalRow=(over:Record<string,unknown>={})=>({goal_id:"goal-1",session_id:"A",request_id:"r",objective:"Build the app",status:"active",
    token_budget:null,tokens_used:0,time_used_s:0,status_reason:"",continuing:false,submission_pending:false,created_at:1,updated_at:1,
    completed_at:null,completed_run_id:null,version:1,...over});
  assert.equal(parseChatGoal(goalRow())?.goal_id,"goal-1");
  for (const extra of [{version:0},{version:1.5},{session_id:""},{goal_id:""},{status:"running"},{objective:null}]) {
    assert.equal(parseChatGoal(goalRow(extra)),null,`invalid goal: ${JSON.stringify(extra)}`);
  }
  assert.equal(parseChatGoal(goalRow({token_budget:-5}))?.token_budget,null,"a non-positive budget is no budget");
  assert.equal(parseGoalMessage({type:"goal:updated",session_id:"A",goal:{bad:true}}),null,"a malformed push is ignored, not read as a clear");
  assert.equal(parseGoalMessage({type:"goal:updated",session_id:"A",goal:goalRow({session_id:"B"})}),null);
  assert.deepEqual(parseGoalMessage({type:"goal:updated",session_id:"A",goal:null}),{type:"goal:updated",session_id:"A",goal:null});
  const label=(over:Record<string,unknown>)=>goalStatusLabel(parseChatGoal(goalRow(over))!);
  assert.deepEqual([label({}),label({continuing:true}),label({submission_pending:true}),label({status:"paused"}),label({status:"blocked"}),
    label({status:"budget_limited"}),label({status:"usage_limited"}),label({status:"complete"})],
    ["Pursuing goal","Continuing goal…","Goal set","Paused goal","Goal stalled","Goal limited","Goal usage limited","Goal achieved"]);
  assert.deepEqual([formatGoalDuration(42),formatGoalDuration(125),formatGoalDuration(7380)],["42s","2m 5s","2h 3m"]);

  // History marks goal turns by origin; the completing reply carries the achievement.
  const submitted=parseMessage({role:"user",text:"Build the app",origin:{kind:"goal",event:"submission",goal_id:"goal-1"}})!;
  assert.deepEqual([submitted.goalOrigin,submitted.origin],[{event:"submission",goal_id:"goal-1"},undefined],"a goal origin is not a peer origin");
  assert.deepEqual(parseMessage({role:"user",text:"Continue toward the goal",origin:{kind:"goal",event:"continuation"}})!.goalOrigin,{event:"continuation"});
  assert.equal(parseMessage({role:"user",text:"x",origin:{kind:"goal",event:"other"}})!.goalOrigin,undefined);
  const achieved=parseMessage({role:"assistant",text:"Done.",goal_event:{event:"achieved",goal_id:"goal-1",time_used_s:754,tokens_used:9000}})!;
  assert.deepEqual(achieved.goalEvent,{event:"achieved",goal_id:"goal-1",time_used_s:754,tokens_used:9000});

  __resetChatStoreForTests();__resetSessionStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();resetWireStatus("chat");
  const commands:Array<Record<string,unknown>>=[];let online=true;
  const context={send:(command:Record<string,unknown>)=>{commands.push(command);return online;},isOpen:()=>online,notify(){}};
  setChatContext(context);setSessionContext(context);setSessionConnection("connected");
  setChatState({...initialChatState(),sessionId:"A",connected:true});noteDisplayedSession("A");
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  const ingest=async(row:Record<string,unknown>)=>act(async()=>ingestChat(parseChatWsMessage(row)));
  const last=(type:string)=>commands.filter(command=>command.type===type).at(-1)!;
  const lastRead=(sessionId:string)=>commands.filter(command=>command.type==="goal:current:get" && command.session_id===sessionId).at(-1)!;
  const count=(type:string)=>commands.filter(command=>command.type===type).length;
  const buttons=()=>[...host.querySelectorAll<HTMLButtonElement>("button"),...document.body.querySelectorAll<HTMLButtonElement>("dialog button")];
  const find=(label:string)=>buttons().find(b=>b.textContent===label || b.getAttribute("aria-label")===label);
  const click=async(label:string)=>act(async()=>{const button=find(label);assert.ok(button,label);assert.equal(button.disabled,false,`${label} is enabled`);button.click();});
  const accept=async(request:Record<string,unknown>,goal:unknown,type="goal:accepted")=>ingest({type,schema:"variant1.goal-command.v2",session_id:request.session_id,
    request_id:request.request_id,operation:request.type==="goal:submit"?"submit":request.type==="goal:control"?"control":"current",result:{goal}});
  const push=async(goal:unknown,session="A")=>ingest({type:"goal:updated",session_id:session,goal});
  const enter=async(field:HTMLTextAreaElement)=>act(async()=>{field.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true,cancelable:true}));});
  const status=()=>host.querySelector(".composer-goal [role=status]")?.textContent;
  const goalMode=async(draft:string)=>act(async()=>{patchChatState({goal:{...getChatState().goal,composing:true}});setChatDraft(draft);});
  const reconnect=async()=>{
    await act(async()=>{online=false;setChatConnection("offline");});
    await act(async()=>{online=true;setChatConnection("connected");await new Promise(resolve=>setTimeout(resolve,230));});
  };
  try {
    await act(async()=>{root.render(<ChatComposer/>);});
    const textarea=host.querySelector<HTMLTextAreaElement>("#composer-input")!;

    // The /goal menu item enters goal mode; it never sends on its own.
    await act(async()=>setChatDraft("/go"));await enter(textarea);
    assert.deepEqual([getChatState().goal.composing,getChatState().draft],[true,""]);assert.ok(host.querySelector(".composer-goal-chip"));
    assert.equal(count("goal:submit"),0,"menu selection does not submit");
    assert.equal(find("Set goal")?.disabled,true,"an empty objective cannot be set");
    await act(async()=>{textarea.dispatchEvent(new KeyboardEvent("keydown",{key:"Backspace",bubbles:true,cancelable:true}));});
    assert.equal(getChatState().goal.composing,false,"Backspace on an empty goal draft leaves goal mode");

    // Typing "/goal ..." enters goal mode with the objective as the draft.
    await act(async()=>typeInto(textarea,"/goal Build and test the app"));
    assert.deepEqual([getChatState().goal.composing,getChatState().draft],[true,"Build and test the app"]);
    await act(async()=>refreshComposerGoal()); const earlierRead = last("goal:current:get");
    await enter(textarea);await enter(textarea);
    const submit=last("goal:submit"),request=String(submit.request_id);
    assert.equal(count("goal:submit"),1,"a pending submit blocks a second one");assert.equal(count("chat"),0,"no chat message is sent for a goal");
    assert.deepEqual([submit.objective,submit.session_id,submit.replace],["Build and test the app","A",undefined]);
    assert.equal(host.querySelector("[data-goal-id]"),null,"a pending submit has no goal yet");
    await accept({...submit,request_id:"wrong"},goalRow({request_id:request}));assert.equal(getChatState().goal.goal,null,"an unrelated reply is ignored");
    await accept(earlierRead,null,"goal:current");
    await act(async()=>refreshComposerGoal());
    await accept(last("goal:current:get"),null,"goal:current");
    await accept({...submit,type:"goal:control"},goalRow({request_id:request}));
    await accept(submit,goalRow({session_id:"B",request_id:request}));
    assert.equal(getChatState().goal.pending?.requestId,request,"ordinary reads, wrong operations and foreign targets cannot settle a write");
    await accept(submit,goalRow({request_id:request,submission_pending:true}));
    assert.deepEqual([getChatState().draft,getChatState().goal.composing],["",false],"a saved goal spends its draft and leaves goal mode");
    assert.equal(host.querySelector("[data-goal-id]")?.getAttribute("data-goal-id"),"goal-1");
    assert.equal(status(),"Goal set · starts after the current turn");
    await push(goalRow({request_id:request,continuing:true,version:2,tokens_used:1200,token_budget:50000}));
    assert.equal(status(),"Continuing goal…");assert.ok(host.textContent?.includes("1.2k / 50k tokens"));
    await push(goalRow({request_id:request,version:1}));assert.equal(getChatState().goal.goal?.version,2,"an older version never replaces a newer one");

    // Controls send one fenced control and wait for the backend.
    await click("Pause");const pause=last("goal:control");
    assert.deepEqual([pause.action,pause.goal_id,pause.expected_version],["pause","goal-1",2]);
    assert.equal(find("Pause")?.disabled,true,"controls wait for the reply");
    assert.equal(status(),"Continuing goal…","a click alone does not pause");
    await accept(pause,goalRow({request_id:request,status:"paused",submission_pending:true,version:3,tokens_used:752}));
    assert.equal(status(),"Paused goal");assert.equal(host.querySelector(".composer-goal__tokens")?.textContent,"752 tokens");
    await click("Resume");const resume=last("goal:control");assert.deepEqual([resume.action,resume.expected_version],["resume",3]);
    await accept(resume,goalRow({request_id:request,version:4}));assert.equal(status(),"Pursuing goal");
    await accept(resume,goalRow({request_id:request,status:"paused",version:3}));assert.equal(getChatState().goal.goal?.version,4,"a late reply cannot revert");

    // Edit opens a dialog; a stale version keeps the text and re-reads the goal.
    await click("Edit goal objective");
    const field=document.body.querySelector<HTMLTextAreaElement>("dialog.composer-goal-dialog textarea")!;assert.equal(field.value,"Build the app");
    await act(async()=>typeInto(field,"Build, test and ship the app"));
    await click("Save objective");const edit=last("goal:control");
    assert.deepEqual([edit.action,edit.objective,edit.expected_version],["edit","Build, test and ship the app",4]);
    await ingest({type:"goal:rejected",session_id:"A",request_id:edit.request_id,operation:"control",reason_code:"GoalVersionConflict",error:"The goal changed; try again."});
    assert.ok(document.body.querySelector("dialog.composer-goal-dialog [role=alert]")?.textContent?.includes("The goal changed"));
    assert.equal(document.body.querySelector<HTMLTextAreaElement>("dialog.composer-goal-dialog textarea")?.value,"Build, test and ship the app","the edit text is kept");
    const reread=last("goal:current:get");assert.equal(reread.session_id,"A","a version conflict re-reads the goal");
    await accept(reread,goalRow({request_id:request,version:5}),"goal:current");
    await click("Save objective");const edit2=last("goal:control");assert.equal(edit2.expected_version,5);
    await accept(edit2,goalRow({request_id:request,objective:"Build, test and ship the app",version:6}));
    assert.equal(document.body.querySelector("dialog.composer-goal-dialog"),null,"a saved edit closes the dialog");
    assert.equal(host.querySelector(".composer-goal__objective")?.textContent,"Build, test and ship the app");

    // Budget and usage stops: Resume only where the backend allows it.
    await push(goalRow({request_id:request,status:"budget_limited",status_reason:"Token budget reached",version:7,token_budget:50000,tokens_used:50000}));
    assert.equal(status(),"Goal limited");assert.ok(host.textContent?.includes("Token budget reached"));
    assert.equal(find("Resume"),undefined,"a spent budget cannot be resumed");assert.ok(find("Clear goal"));
    await push(goalRow({request_id:request,status:"usage_limited",version:8}));assert.ok(find("Resume"),"a usage limit can be resumed");

    // /goal over an unfinished goal asks first; Keep sends nothing.
    await goalMode("Second objective");
    await enter(textarea);assert.equal(count("goal:submit"),1,"replacing needs confirmation");
    assert.ok(document.body.querySelector("dialog.composer-goal-dialog")?.textContent?.includes("Replace current goal?"));
    await click("Keep current goal");assert.equal(getChatState().goal.confirmReplace,undefined);assert.equal(getChatState().draft,"Second objective");
    await enter(textarea);await click("Replace goal");const replace=last("goal:submit");
    assert.deepEqual([replace.objective,replace.replace,replace.expected_goal_id],["Second objective",true,"goal-1"]);
    await accept(replace,goalRow({goal_id:"goal-2",request_id:String(replace.request_id),objective:"Second objective"}));
    assert.deepEqual([getChatState().goal.goal?.goal_id,getChatState().draft],["goal-2",""]);

    // A complete goal is replaced without asking; a conflict the composer didn't see asks then.
    await push(goalRow({goal_id:"goal-2",request_id:String(replace.request_id),status:"complete",completed_at:9,time_used_s:754,version:2}));
    assert.equal(status(),"Goal achieved in 12m 34s");assert.equal(find("Edit goal objective"),undefined,"a complete goal cannot be edited");
    await goalMode("Third objective");
    await enter(textarea);const third=last("goal:submit");assert.equal(third.replace,undefined,"a complete goal is replaced without asking");
    await act(async()=>typeInto(textarea,"My next unsent draft"));
    await ingest({type:"goal:rejected",session_id:"A",request_id:third.request_id,operation:"submit",reason_code:"GoalConflict",error:"This chat already has an unfinished goal"});
    assert.equal(getChatState().goal.confirmReplace?.objective,"Third objective");assert.equal(getChatState().goal.error,undefined);
    assert.equal(find("Replace goal")?.disabled,true,"the conflict read must identify the goal before replacement");
    await accept(last("goal:current:get"),goalRow({goal_id:"goal-x",request_id:"other"}),"goal:current");
    await click("Replace goal");const thirdReplace=last("goal:submit");
    assert.deepEqual([thirdReplace.objective,thirdReplace.replace,thirdReplace.expected_goal_id],["Third objective",true,"goal-x"]);
    await accept(thirdReplace,goalRow({goal_id:"goal-3",request_id:String(thirdReplace.request_id),objective:"Third objective"}));
    assert.equal(getChatState().draft,"My next unsent draft","confirmation must not spend the revision typed after the original submission");

    // Clear removes the goal; a push of null clears it in every window.
    await click("Clear goal");const clear=last("goal:control");assert.equal(clear.action,"clear");
    await accept(clear,null);assert.equal(getChatState().goal.goal,null);assert.equal(host.querySelector("[data-goal-id]"),null);
    await push(goalRow({goal_id:"goal-4",request_id:"elsewhere"}));assert.equal(getChatState().goal.goal?.goal_id,"goal-4","another window's goal appears");
    await push(null);assert.equal(getChatState().goal.goal,null);

    // A goal can be set while a turn runs; its first turn waits for that turn.
    await act(async()=>patchChatState({turnActive:true}));await goalMode("Mid-turn goal");
    assert.equal(host.querySelector(".composer-delivery-choice"),null,"steer/queue choices don't apply to a goal");
    const sentBefore=commands.length;
    await click("Set goal");const midTurn=last("goal:submit");assert.equal(midTurn.objective,"Mid-turn goal");
    assert.deepEqual(commands.slice(sentBefore).map(c=>c.type),["goal:submit"],"the goal is not steered into the running turn");
    await act(async()=>patchChatState({turnActive:false}));

    // A lost reply is settled by the goal's request_id after reconnect; nothing is resent.
    const submits=count("goal:submit");
    await reconnect();
    assert.equal(count("goal:submit"),submits,"reconnect never replays a submit");
    await accept(last("goal:current:get"),goalRow({goal_id:"goal-5",request_id:String(midTurn.request_id),objective:"Mid-turn goal",submission_pending:true}),"goal:current");
    assert.deepEqual([getChatState().goal.pending,getChatState().draft,getChatState().goal.goal?.goal_id],[undefined,"","goal-5"]);
    // ...and a read that doesn't show the submit keeps the draft for a retry.
    await act(async()=>patchChatState({goal:{...getChatState().goal,goal:null}}));await goalMode("Lost goal");
    await click("Set goal");
    await reconnect();
    await accept(last("goal:current:get"),null,"goal:current");
    assert.deepEqual([getChatState().goal.pending,getChatState().draft],[undefined,"Lost goal"]);assert.ok(getChatState().goal.error?.includes("may not have been set"));

    // A read that started before a push can't undo it.
    await act(async()=>refreshComposerGoal());const oldRead=last("goal:current:get");
    await push(goalRow({goal_id:"goal-6",request_id:"x"}));
    await accept(oldRead,null,"goal:current");assert.equal(getChatState().goal.goal?.goal_id,"goal-6","a stale empty read keeps the pushed goal");

    // Goals are text only; ordinary text stays a chat message.
    await act(async()=>{patchChatState({attachments:[{id:"a",name:"file.txt",kind:"file",size:1,mime:"text/plain",text:"x"}],goal:{...getChatState().goal,goal:null,error:undefined}});});
    await goalMode("Goal with file");
    const before=count("goal:submit");await enter(textarea);assert.equal(count("goal:submit"),before,"a goal is text only");
    assert.equal(getChatState().attachments.length,1);
    await act(async()=>{patchChatState({attachments:[],goal:{...getChatState().goal,composing:false}});setChatDraft("My goal is to improve tests");
      submitUserInput({source:"composer",sessionId:"A",revision:getComposerRevision(),text:getChatState().draft,attachments:[]});});
    assert.equal(last("chat").text,"My goal is to improve tests","ordinary text remains ordinary chat");

    // Replies and pushes stay with their own chat.
    await act(async()=>{activateChatState("B");noteDisplayedSession("B");});
    await goalMode("B objective");await click("Set goal");const b=last("goal:submit");assert.equal(b.session_id,"B");
    await act(async()=>{activateChatState("A");noteDisplayedSession("A");});
    await accept(b,goalRow({goal_id:"goal-B",session_id:"B",request_id:String(b.request_id)}));
    assert.equal(getChatState().goal.goal,null,"B's reply leaves A's goal alone");assert.equal(getCachedChatState("B")?.goal.goal?.goal_id,"goal-B");

    // Confirmations and edits belong to the displayed goal. An unchanged objective
    // may use a newer usage version, but a conflicting objective needs a new decision.
    await push(goalRow({goal_id:"old"}));
    await goalMode("Proposed replacement"); await enter(textarea);
    const submitsBeforeSwitch = count("goal:submit");
    await push(goalRow({goal_id:"different"}));
    assert.equal(document.querySelector("dialog[open]"),null);
    assert.equal(count("goal:submit"),submitsBeforeSwitch);
    assert.equal(getChatState().draft,"Proposed replacement");
    const editField = () => document.querySelector<HTMLTextAreaElement>("dialog textarea")!;
    await click("Edit goal objective"); await act(async()=>typeInto(editField(),"Draft from A"));
    await act(async()=>{activateChatState("B");noteDisplayedSession("B");patchChatState({});});
    assert.equal(document.querySelector("dialog[open]"),null,"a draft from A is never attached to B's goal");
    await act(async()=>{activateChatState("A");noteDisplayedSession("A");patchChatState({});});
    await click("Edit goal objective"); await act(async()=>typeInto(editField(),"Draft for the old identity"));
    await push(goalRow({goal_id:"edited"})); assert.equal(document.querySelector("dialog[open]"),null);
    await click("Edit goal objective"); await act(async()=>typeInto(editField(),"My objective"));
    await push(goalRow({goal_id:"edited",objective:"A remote edit",version:2}));
    assert.equal(editField().value,"My objective"); assert.equal(find("Save objective")?.disabled,true);
    await click("Keep editing my draft");
    await push(goalRow({goal_id:"edited",objective:"A remote edit",tokens_used:1,version:3}));
    await click("Save objective"); const uncertainEdit = last("goal:control");
    assert.equal(uncertainEdit.expected_version,3,"usage changes do not invalidate the reviewed objective");
    await accept(uncertainEdit,goalRow({goal_id:"wrong-target",objective:"My objective",version:4}));
    assert.equal(getChatState().goal.goal?.goal_id,"edited");
    assert.equal(getChatState().goal.pending?.requestId,uncertainEdit.request_id);
    const controlsBeforeRead = count("goal:control");
    await reconnect();
    const retryRead = lastRead("A");
    await ingest({type:"goal:rejected",session_id:"A",request_id:retryRead.request_id,operation:"current",error:"Service unavailable"});
    assert.equal(getChatState().goal.readRequestId,undefined);
    assert.ok(document.querySelector("dialog[open]")?.textContent?.includes("Service unavailable"));
    assert.equal(document.querySelector<HTMLButtonElement>("dialog button[type=submit]")?.disabled,true); assert.equal(find("Close dialog")?.disabled,false);
    await click("Check saved goal");
    await accept(last("goal:current:get"),goalRow({goal_id:"edited",objective:"A remote edit",version:3}),"goal:current");
    assert.equal(editField().value,"My objective");
    assert.ok(document.querySelector("dialog[open]")?.textContent?.includes("Could not confirm"));
    assert.equal(count("goal:control"),controlsBeforeRead,"reconciliation never resends an edit");
    await click("Save objective"); const retriedEdit = last("goal:control");
    await act(async()=>{online=false;setChatConnection("offline");});
    await act(async()=>{document.querySelector("dialog[open]")!.dispatchEvent(new Event("cancel",{cancelable:true}));});
    assert.equal(document.querySelector("dialog[open]"),null,"Escape can dismiss an unconfirmed edit offline");
    await reconnect(); await accept(lastRead("A"),goalRow({goal_id:"edited",objective:"My objective",version:4}),"goal:current");
    assert.equal(getChatState().goal.confirmedRequestId,retriedEdit.request_id);

    await click("Clear goal"); const staleClear = last("goal:control");
    await push(goalRow({goal_id:"other-window"})); await accept(staleClear,null);
    assert.equal(getChatState().goal.goal?.goal_id,"other-window","a late clear receipt cannot erase another window's new goal");
    await accept(last("goal:current:get"),goalRow({goal_id:"other-window"}),"goal:current");
    await push(null);

    // A full, pasted slash command exceeds the native message field's 8K limit.
    // Replace a selection and retain its suffix, including non-BMP characters.
    await act(async()=>{patchChatState({goal:{...getChatState().goal,composing:false}});setChatDraft("replace me!");});
    const longObjective = "🌱".repeat(31999);
    await act(async()=>{
      textarea.setSelectionRange(0,10);
      const paste = new Event("paste",{bubbles:true,cancelable:true});
      Object.defineProperty(paste,"clipboardData",{value:{files:[],items:[],getData:()=>" /goal\n"+longObjective}});
      textarea.dispatchEvent(paste);
      assert.equal(paste.defaultPrevented,true);
    });
    assert.equal(getChatState().draft,longObjective+"!");
    await act(async()=>typeInto(textarea,longObjective+"!!")); assert.equal(find("Set goal")?.disabled,true);
    await act(async()=>typeInto(textarea,longObjective+"!")); await click("Set goal");
    const longSubmit=last("goal:submit"); assert.equal(longSubmit.objective,longObjective+"!");
    await click("Leave goal mode");
    await accept(longSubmit,goalRow({goal_id:"long",objective:longSubmit.objective,request_id:longSubmit.request_id}));
    assert.equal(getChatState().draft,longObjective+"!","a late submit cannot spend text the user switched back to a message");
  } finally {
    await act(async()=>root.unmount());host.remove();resetWireStatus("chat");
  }

  // Follow admitted turns through production event parsing. Active input
  // projections are optional and the append may arrive before or after done.
  __resetChatStoreForTests();__resetTurnStoreForTests();
  setChatContext(context);activateChatState("T");noteDisplayedSession("T");
  setChatState({...initialChatState(),sessionId:"T",connected:true,activeTurnId:"previous-prompt",messages:[
    {role:"user",text:"Build the app",optimisticTurnId:"previous-prompt"},
    {role:"assistant",text:"Started.",runId:"older-run",optimisticTurnId:"previous-prompt"},
  ]});
  const firstRun = {source:"goal:submit",session_id:"T",run_id:"r1",admission_id:"a1",client_id:"goal-engine"};
  const firstUser = {role:"user",text:"Build the app",origin:{kind:"goal",event:"submission",goal_id:"g"}};
  const firstAppend = {type:"chat:appended",...firstRun,messages:[firstUser,
    {role:"assistant",text:"Preparing.",run_id:"r1"},{role:"user",text:"Check both platforms",ticket_id:"steer-1",delivery:"steer"},
    {role:"assistant",text:"Started.",run_id:"r1"}]};
  await ingest({type:"start",...firstRun});
  await ingest({type:"activity",...firstRun,event:"tool:result",tool:"read_file",call_id:"read-source",status:"ok",text:"Read source"});
  await ingest({type:"token",...firstRun,token:"Starting"});
  await ingest({type:"done",...firstRun,text:"Starting"});
  await ingest(firstAppend);
  const rows = () => getChatState().messages;
  assert.deepEqual(rows().map(row=>row.text),["Build the app","Started.","Build the app","Preparing.","Check both platforms","Started."]);
  assert.equal(rows()[1].runId,"older-run","identical text from an earlier run survives");
  assert.equal(rows()[0].optimisticTurnId,"previous-prompt","an outstanding ordinary prompt retains its own append group");
  assert.equal(rows()[2].goalOrigin?.event,"submission");
  assert.equal(rows()[4].ticketId,"steer-1");
  assert.ok(rows()[5].steps?.some(step=>step.callId==="read-source"),"the saved reply keeps the live trace when its text differs");
  assert.ok(rows()[5].receipt,"the saved reply keeps the local run receipt");
  const secondRun = {source:"goal:continue",session_id:"T",run_id:"r2",admission_id:"a2",client_id:"goal-engine"};
  const secondUser = {role:"user",text:"Continue toward the goal",origin:{kind:"goal",event:"continuation",goal_id:"g"}};
  const secondAppend = {type:"chat:appended",...secondRun,messages:[secondUser,{role:"assistant",text:"All requirements met.",run_id:"r2",
    goal_event:{event:"achieved",goal_id:"g",time_used_s:3725,tokens_used:1}}]};
  await ingest({type:"start",...secondRun,active_inputs:{schema:"variant1.active-inputs.v1",...secondRun,root:secondUser,inputs:[]}});
  await ingest({type:"activity",...secondRun,event:"tool:result",tool:"read_file",call_id:"verify-source",status:"ok"});
  const activeId = getChatState().activeTurnId;
  await ingest(firstAppend); assert.equal(getChatState().activeTurnId,activeId);
  assert.equal(rows().filter(row=>row.runId==="r1").length,4,"a replay during the next goal turn is idempotent");
  await ingest({...secondAppend,admission_id:"wrong"});
  assert.equal(getChatState().pendingTurnCommit,undefined);
  await ingest(secondAppend);
  assert.equal(rows().some(row=>row.goalEvent),false,"a durable append waits for the live turn to finish");
  await ingest({type:"done",...secondRun,text:"All requirements met."});
  await ingest(secondAppend);
  assert.equal(rows().length,8);
  assert.equal(rows().at(-1)?.goalEvent?.event,"achieved");
  // History stores the run ID on assistants. Replaying the append after a
  // session reload must also recognize its adjacent durable goal prompt.
  patchChatState({messages:rows().map(row=>row.role==="user" ? {...row,runId:undefined,admissionId:undefined} : row)});
  await ingest(firstAppend); await ingest(secondAppend);
  assert.equal(rows().length,8,"replayed goal segments merge with restored history");
  const nextRun = {source:"goal:continue",session_id:"T",run_id:"r3",admission_id:"a3"};
  await ingest({type:"start",...nextRun});
  await ingest({type:"activity",...nextRun,event:"tool:result",tool:"read_file",call_id:"verify-last",status:"ok"});
  const beforeEarly = rows().length;
  const nextAppend = {type:"chat:appended",...nextRun,user:secondUser,assistant:{role:"assistant",text:"Checked."}};
  await ingest(nextAppend);
  assert.equal(rows().length,beforeEarly,"an early append without an active input root is held for done");
  await ingest({type:"done",...nextRun,text:"Checked."});
  await ingest(nextAppend);
  assert.equal(rows().length,beforeEarly+2);
  const annotations = commands.filter(command=>command.type==="chat:session:annotate" && command.id==="T");
  assert.deepEqual(annotations.map(command=>command.run_id),["r1","r2","r3"],"replayed appends do not enqueue duplicate annotations");
  for (const command of annotations) await ingest({type:"chat:session:annotated",id:"T",run_id:command.run_id,request_id:command.request_id,ok:true});
  const transcriptHost=document.createElement("div");document.body.appendChild(transcriptHost);const transcript=createRoot(transcriptHost);
  try {
    await act(async()=>{transcript.render(<ChatMessageList/>);});
    assert.ok(transcriptHost.querySelector(".message--user .message__goal-label")?.textContent?.includes("Sent as goal"));
    assert.ok(transcriptHost.querySelector(".message--goal-turn")?.textContent?.includes("Continuing goal"));
    assert.equal(transcriptHost.textContent?.includes("Continue toward the goal"),false,"the continuation prompt isn't shown as a bubble");
    assert.ok(transcriptHost.querySelector(".message__goal-achieved")?.textContent?.includes("Goal achieved in 1h 2m"));
  } finally {
    await act(async()=>transcript.unmount());transcriptHost.remove();
  }
  console.log("Goal composer: fenced controls and replacement, draft ownership, scoped receipts and reads, reconnect recovery, Unicode paste and durable goal transcripts passed");
}
