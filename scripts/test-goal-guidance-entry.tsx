import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {ComposerGoalPanel} from "../frontend/main-deck/src/chat/ComposerGoalPanel";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {setChatContext,setChatState,initialChatState,getChatState,activateChatState,emit} from "../frontend/main-deck/src/chat/stateCore";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {refreshComposerGoal,requestGoalControl,setGoalGuidance} from "../frontend/main-deck/src/chat/goals";
import {__resetSessionStoreForTests} from "../frontend/main-deck/src/state/sessionStore";
import {setChatConnection} from "../frontend/main-deck/src/chat/connection";
import {resetWireStatus} from "../frontend/main-deck/src/connectionUi";

export async function run() {
  __resetChatStoreForTests();__resetSessionStoreForTests();resetWireStatus("chat");
  const sent:Array<Record<string,unknown>>=[];let transport=true;
  setChatContext({send:value=>{sent.push(value);return transport;},isOpen:()=>true,notify(){}});
  setChatState({...initialChatState(),sessionId:"A",connected:true,draft:"ordinary composer draft"});
  const host=document.createElement("div"),root=createRoot(host);document.body.appendChild(host);
  const snapshot=(version:number,id="goal-A",session="A",continuation="")=>({schema:"variant1.goal-snapshot.v1",goal:{schema:"variant1.goal.v1",goal_id:id,owner_chat_id:session,title:"Goal",objective:"Original objective",status:"blocked",version},submission_request_id:`submit-${id}`,capabilities:{continue:true},state:continuation?{continuation_request:{request_id:continuation}}:{}});
  const ingest=async(row:Record<string,unknown>)=>act(async()=>ingestChat(parseChatWsMessage(row)));
  const last=(type:string)=>sent.filter(row=>row.type===type).at(-1)!;
  const read=async(value:Record<string,unknown>)=>{await act(async()=>refreshComposerGoal(true));await ingest({type:"goal:current",session_id:getChatState().sessionId,request_id:last("goal:current:get").request_id,result:value});};
  const field=()=>host.querySelector<HTMLTextAreaElement>('#goal-continuation-guidance')!;
  const write=async(text:string)=>act(async()=>{Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,"value")!.set!.call(field(),text);field().dispatchEvent(new Event("input",{bubbles:true}));});
  const click=async()=>act(async()=>host.querySelector<HTMLButtonElement>('[data-goal-action="continue"]')!.click());
  try {
    await act(async()=>root.render(<ComposerGoalPanel/>));await read(snapshot(3));
    await write("Use the revised requirement.\nReturn 42 instead of 7.");
    assert.equal(getChatState().draft,"ordinary composer draft");
    await act(async()=>{const button=host.querySelector<HTMLButtonElement>('[data-goal-action="continue"]')!;button.click();button.click();});
    const first=last("goal:continue");assert.equal(sent.filter(row=>row.type==="goal:continue").length,1);
    assert.equal(first.message,"Use the revised requirement.\nReturn 42 instead of 7.");assert.equal(first.goal_id,"goal-A");assert.equal(first.session_id,"A");assert.equal(first.expected_version,3);assert.ok(field().disabled);
    await ingest({type:"goal:rejected",session_id:"A",request_id:first.request_id,operation:"continue",error:"Goal version changed"});
    assert.equal(field().value,first.message);assert.equal(getChatState().goal.pending,undefined);
    await read(snapshot(4));assert.equal(field().value,first.message,"refresh preserves rejected guidance on the same goal");
    await act(async()=>{assert.equal(requestGoalControl("continue","A","goal-A",3),false);setGoalGuidance("stale edit","A","goal-A",3);});assert.equal(field().value,first.message);
    await click();const retry=last("goal:continue");assert.equal(retry.expected_version,4);assert.equal(retry.message,first.message);assert.notEqual(retry.request_id,first.request_id);
    await ingest({type:"goal:accepted",session_id:"A",request_id:retry.request_id,operation:"continue",result:snapshot(5)});
    assert.equal(field().value,"");assert.equal(getChatState().draft,"ordinary composer draft");
    await ingest({type:"goal:accepted",session_id:"A",request_id:first.request_id,operation:"continue",result:snapshot(4)});assert.equal(getChatState().goal.snapshot?.goal.version,5);
    await click();const empty=last("goal:continue");assert.equal("message" in empty,false,"empty guidance keeps existing no-message behavior");
    await ingest({type:"goal:accepted",session_id:"A",request_id:empty.request_id,operation:"continue",result:snapshot(6)});
    await write("Keep on transport failure");transport=false;await click();transport=true;assert.equal(field().value,"Keep on transport failure");assert.equal(getChatState().goal.pending,undefined);
    await read(snapshot(7));await click();const uncertain=last("goal:continue");
    await read(snapshot(8));assert.equal(field().value,"Keep on transport failure");assert.ok(getChatState().goal.pending,"unrelated refresh cannot retire ambiguous guidance or permit replay");
    await read(snapshot(8,"goal-A","A",String(uncertain.request_id)));assert.ok(getChatState().goal.pending,"stored intent before admission is not delivery confirmation");
    const admitted=snapshot(9,"goal-A","A",String(uncertain.request_id));admitted.goal.status="running";
    await read(admitted);assert.ok(getChatState().goal.pending,"running state is not a guidance admission receipt");
    assert.equal(field().value,"Keep on transport failure");
    await ingest({type:"goal:accepted",session_id:"A",request_id:uncertain.request_id,operation:"continue",result:admitted});
    assert.equal(field().value,"");assert.equal(getChatState().goal.pending,undefined);
    await write("  Revised guidance\nReturn 42.  ");await click();const lostAck=last("goal:continue");
    await act(async()=>setChatConnection("offline"));
    await act(async()=>{setChatConnection("connected");await new Promise(resolve=>setTimeout(resolve,230));});
    assert.equal(getChatState().goal.pending?.requestId,lostAck.request_id,"raw reconnect retains the original pending continuation");
    assert.equal(lostAck.message,"Revised guidance\nReturn 42.","receipt comparison uses the submitted trimmed message");
    const receipt=()=>({...snapshot(100),continuation_admission:{request_id:lostAck.request_id,step_id:"step-main",previous_attempt:2,status:"admitted",basis:"work_job",job_id:"supervisor-job-exact",job_status:"queued"},
      state:{continuation_request:{request_id:lostAck.request_id,message:lostAck.message,step_id:"step-main",previous_attempt:2}}});
    const invalidCases:Array<[string,(row:any)=>void]>=[
      ["staged",row=>row.continuation_admission.status="staged"],
      ["unknown",row=>row.continuation_admission.status="unknown"],
      ["wrong basis",row=>row.continuation_admission.basis="step_status"],
      ["empty job",row=>row.continuation_admission.job_id=""],
      ["whitespace job",row=>row.continuation_admission.job_id="  "],
      ["wrong admission request",row=>row.continuation_admission.request_id="other"],
      ["other self-consistent request",row=>{row.continuation_admission.request_id="other";row.state.continuation_request.request_id="other";}],
      ["wrong message",row=>row.state.continuation_request.message="Old requirement"],
      ["missing message",row=>delete row.state.continuation_request.message],
      ["wrong step",row=>row.state.continuation_request.step_id="other-step"],
      ["empty step",row=>{row.state.continuation_request.step_id="";row.continuation_admission.step_id="";}],
      ["wrong attempt",row=>row.state.continuation_request.previous_attempt=3],
      ["string attempt",row=>{row.state.continuation_request.previous_attempt="2";row.continuation_admission.previous_attempt="2";}],
      ["negative attempt",row=>{row.state.continuation_request.previous_attempt=-1;row.continuation_admission.previous_attempt=-1;}],
      ["missing stored request",row=>row.state={}],
      ["null receipt",row=>row.continuation_admission=null],
    ];
    const countBeforeRecovery=sent.filter(row=>row.type==="goal:continue").length;
    for(const [label,change] of invalidCases) {
      const row=receipt();change(row);await read(row);
      assert.equal(getChatState().goal.pending?.requestId,lostAck.request_id,label);
      assert.equal(field().value,"  Revised guidance\nReturn 42.  ",label);
    }
    const committed=receipt();committed.continuation_admission.job_status="failed";
    await read(committed);assert.equal(field().value,"");assert.equal(getChatState().goal.pending,undefined,"exact admitted receipt recovers a lost ACK even after the admitted job settles");
    assert.equal(sent.filter(row=>row.type==="goal:continue").length,countBeforeRecovery,"reads never replay Continue");
    await write("Only for A");await act(async()=>{activateChatState("B");emit();});await read(snapshot(1,"goal-B","B"));assert.equal(field().value,"");await write("Only for B");
    await act(async()=>{activateChatState("A");emit();});assert.equal(field().value,"Only for A");await read(snapshot(1,"replacement-A"));assert.equal(field().value,"");assert.equal(getChatState().goal.guidance,undefined,"goal replacement retires old guidance");
    await act(async()=>setGoalGuidance("wrong goal","A","goal-A",1));assert.equal(field().value,"");
    await write("x".repeat(9000));assert.equal(getChatState().goal.guidance?.text.length,8000,"store enforces the UI bound");
    await click();const replacedRequest=last("goal:continue");await read(snapshot(1,"replacement-A-2"));
    assert.equal(field().value,"");assert.equal(getChatState().goal.pending,undefined,"replacement retires old pending guidance");
    await ingest({type:"goal:accepted",session_id:"A",request_id:replacedRequest.request_id,operation:"continue",result:snapshot(2,"replacement-A")});
    assert.equal(getChatState().goal.snapshot?.goal.goal_id,"replacement-A-2","old goal acknowledgment cannot restore retired guidance");
  } finally {await act(async()=>root.unmount());host.remove();resetWireStatus("chat");}
  console.log("Goal guidance: actual input, exact scope/version, duplicate guard, reject/transport retention, confirmation retirement, no-message compatibility and session/replacement isolation passed");
}
