import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {cancelChatTurn, sendUserMessage, setChatDraft} from "../frontend/main-deck/src/chat/composer";
import {getCachedChatState, getChatState, getDisplayedChatState, resetChatStateBag, setChatContext} from "../frontend/main-deck/src/chat/stateCore";
import {__resetSessionStoreForTests, getSessionState, ingestSessions, setSessionConnection, setSessionContext, switchSession} from "../frontend/main-deck/src/state/sessionStore";
import {__resetTurnStoreForTests, turnController} from "../frontend/main-deck/src/state/turnStore";
import {ClarificationCard} from "../frontend/main-deck/src/chat/ClarificationCard";
import * as questions from "../frontend/main-deck/src/state/clarificationStore";

const sent: Array<Record<string,unknown>>=[];
const context={send:(command:Record<string,unknown>)=>{sent.push(command);return true;},notify() {},isOpen:()=>true};
const incoming=(message:Record<string,unknown>)=>ingestChat(parseChatWsMessage(message));
const last=(type:string)=>[...sent].reverse().find(row=>row.type===type)!;
function snapshot(id:string,navigation?:Record<string,unknown>,busy=false,admission="") {
  incoming({type:"chat:session",session:{id,title:id,messages:[],runtime:{busy,active_admission_id:admission,active_run_id:admission ? "run-"+id : ""}},navigation});
}
function select(id:string,busy=false,admission="") {
  switchSession(id);const request=last("chat:session:switch");
  snapshot(id,{request_id:request.request_id,requested_id:id,effective_id:id,status:"switched"},busy,admission);
}
const question=(chatId:string,id:string)=>({type:"clarification:request",chat_id:chatId,id,run_id:"run-"+chatId,questions:[{id:"q",question:"Choose for "+chatId,header:chatId,multiSelect:false,options:[{label:"One",description:"First"},{label:"Two",description:"Second"}]}]});
function listReply(chatId:string,pending:unknown[]) {
  const request=[...sent].reverse().find(row=>row.type==="clarification:list"&&row.chat_id===chatId)!;
  questions.ingestClarification({type:"clarification:snapshot",chat_id:chatId,request_id:request.request_id,pending});
}

export async function run() {
  questions.__resetClarificationForTests();__resetTurnStoreForTests();__resetSessionStoreForTests();resetChatStateBag();
  setSessionContext(context);setChatContext(context);setSessionConnection("connected");
  ingestSessions({type:"chat:sessions",active_id:"A",items:[{id:"A",title:"A"},{id:"B",title:"B"}]});snapshot("A");
  assert.equal(sendUserMessage("Alpha task"),true);assert.equal(last("chat").session_id,"A");
  const client=getChatState().clientId;
  incoming({type:"start",session_id:"A",client_id:client,source:"chat",admission_id:"admission-A",run_id:"run-A"});
  incoming({type:"token",session_id:"A",client_id:client,source:"chat",admission_id:"admission-A",token:"Alpha prefix "});
  select("B");
  assert.equal(getChatState().sessionId,"B");assert.equal(getChatState().turnActive,false);
  assert.deepEqual(getSessionState().workingSessionIds,["A"]);
  assert.equal(sendUserMessage("Beta task"),true);
  assert.equal(last("chat").session_id,"B");assert.equal(last("chat").admission_id,undefined,"a fresh B send cannot inherit A's run fence");
  incoming({type:"start",session_id:"B",client_id:client,source:"chat",admission_id:"admission-B",run_id:"run-B"});
  incoming({type:"token",session_id:"B",client_id:client,source:"chat",admission_id:"admission-B",token:"Beta prefix"});
  setChatDraft("Unsent Beta draft");
  incoming({type:"token",session_id:"A",client_id:client,source:"chat",admission_id:"admission-A",token:"continues"});
  assert.equal(getDisplayedChatState().streamText,"Beta prefix");assert.equal(getDisplayedChatState().draft,"Unsent Beta draft");
  assert.equal(getCachedChatState("A")?.streamText,"Alpha prefix continues");
  assert.deepEqual(getSessionState().workingSessionIds.sort(),["A","B"]);
  incoming({type:"token",client_id:client,source:"chat",token:"unscoped corruption"});
  assert.equal(getChatState().streamText,"Beta prefix","ambiguous unscoped events cannot paint the selected chat");
  assert.equal(sendUserMessage("Steer Beta","steer"),true);
  assert.equal(last("chat").session_id,"B");assert.equal(last("chat").admission_id,"admission-B");
  cancelChatTurn();assert.equal(last("cancel").session_id,"B");assert.equal(last("cancel").admission_id,"admission-B");
  incoming({type:"done",session_id:"A",client_id:client,source:"chat",admission_id:"admission-A",text:"Alpha complete"});
  assert.equal(getChatState().sessionId,"B");assert.equal(getChatState().streamText,"Beta prefix");
  assert.equal(turnController.isActive(),true);assert.deepEqual(getSessionState().workingSessionIds,["B"]);
  select("A");assert.equal(getChatState().turnActive,false);
  select("B",true,"admission-B");assert.equal(getChatState().streamText,"Beta prefix");
  incoming({type:"token",session_id:"B",source:"chat",client_id:client,admission_id:"older-B",token:"stale"});
  assert.equal(getChatState().streamText,"Beta prefix","old admissions cannot append to a newer run");

  questions.setClarificationContext(context);questions.setClarificationConnection("connected");listReply("B",[]);
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  await act(async()=>root.render(<ClarificationCard/>));
  await act(async()=>questions.ingestClarification(question("A","question-A")));
  assert.equal(host.querySelector("[data-question-id]"),null,"A's question cannot appear in B");
  await act(async()=>questions.ingestClarification(question("B","question-B")));
  assert.equal(host.querySelector<HTMLElement>("[data-question-chat]")?.dataset.questionChat,"B");
  const answer=host.querySelector<HTMLInputElement>('input[aria-label="Your answer"]')!;
  await act(async()=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value")!.set!.call(answer,"Beta answer");answer.dispatchEvent(new Event("input",{bubbles:true}));});
  await act(async()=>select("A"));assert.equal(host.querySelector<HTMLElement>("[data-question-chat]")?.dataset.questionChat,"A");
  await act(async()=>select("B",true,"admission-B"));
  assert.equal(host.querySelector<HTMLInputElement>('input[aria-label="Your answer"]')!.value,"Beta answer","draft survives switching chats");
  await act(async()=>listReply("B",[null, [], "invalid", question("A","foreign"), question("B","question-B")]));
  assert.equal(host.querySelector<HTMLInputElement>('input[aria-label="Your answer"]')!.value,"Beta answer","refresh does not erase a matching draft");
  const before=sent.filter(row=>row.type==="clarification:response").length;
  await act(async()=>{assert.equal(questions.submitClarification("question-B",{q:"Beta answer"}),true);assert.equal(questions.submitClarification("question-B",{q:"duplicate"}),false);});
  const reply=last("clarification:response");assert.equal(reply.chat_id,"B");assert.equal(sent.filter(row=>row.type==="clarification:response").length,before+1);
  await act(async()=>questions.ingestClarification({type:"clarification:response:ack",chat_id:"A",id:"question-B",request_id:reply.request_id,status:"resolved"}));
  assert.ok(questions.getClarificationState().submissions["question-B"],"wrong-chat ack cannot settle the answer");
  await act(async()=>select("A"));
  await act(async()=>questions.ingestClarification({type:"clarification:response:ack",chat_id:"B",id:"question-B",request_id:reply.request_id,status:"resolved"}));
  assert.equal(host.querySelector<HTMLElement>("[data-question-id]")?.dataset.questionId,"question-A","B's late ack cannot dismiss A's question");
  await act(async()=>{
    questions.refreshClarification("A");const oldRequest=last("clarification:list");
    questions.ingestClarification(question("A","question-A2"));
    questions.ingestClarification({type:"clarification:snapshot",chat_id:"A",request_id:oldRequest.request_id,pending:[]});
  });
  assert.equal(questions.getClarificationState().byChat.A.length,2,"an older snapshot cannot erase a newly arrived question");
  await act(async()=>root.unmount());host.remove();questions.__resetClarificationForTests();
  __resetTurnStoreForTests();__resetSessionStoreForTests();resetChatStateBag();
  console.log("E13/E14: per-chat streams/drafts, active switching, explicit Send/Steer/Stop fences, scoped questions, stale acknowledgements and snapshot races passed");
}
