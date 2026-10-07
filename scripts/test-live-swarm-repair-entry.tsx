import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {HistoryRail} from "../frontend/main-deck/src/shell/HistoryRail";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {initialChatState,setChatState,setChatContext,getChatState,sharedTurnActive,withCachedChatState} from "../frontend/main-deck/src/chat/stateCore";
import {__resetTurnStoreForTests,turnController} from "../frontend/main-deck/src/state/turnStore";
import {__resetSessionStoreForTests,noteDisplayedSession,setSessionContext,switchSession,ingestSessions,getSessionState,retrySessionNavigation,returnToDisplayedSession,acceptIncomingSession} from "../frontend/main-deck/src/state/sessionStore";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {applySession} from "../frontend/main-deck/src/chat/session";
import {refreshExecutionStates,executionConnection} from "../frontend/main-deck/src/chat/executionRecovery";

export async function run() {
  const sent:Record<string,unknown>[]=[];
  const context={send:(row:Record<string,unknown>)=>{sent.push(row);return true;},notify(){},isOpen:()=>true};
  const reset=()=>{executionConnection(false);__resetChatStoreForTests();__resetTurnStoreForTests();__resetSessionStoreForTests();sent.length=0;
    setChatContext(context);setSessionContext(context);setChatState({...initialChatState(),sessionId:"A",clientId:"client",connected:true});noteDisplayedSession("A");};
  const incoming=(row:Record<string,unknown>)=>ingestChat(parseChatWsMessage(row));
  const route=(n="one")=>({session_id:"A",client_id:"client",source:"chat",admission_id:`admission-${n}`,run_id:`run-${n}`});
  reset();
  const oldSetTimeout=globalThis.setTimeout;
  let expire:()=>void=()=>{};
  globalThis.setTimeout=((callback:()=>void,ms:number,...args:unknown[])=>{
    if(ms===15_000){expire=callback;return oldSetTimeout(()=>{},60_000);}
    return oldSetTimeout(callback,ms,...args);
  }) as typeof setTimeout;
  try {
    switchSession("B");const request=getSessionState().pendingAction!;
    expire();assert.match(getSessionState().navigationError,/did not finish opening/);
    assert.ok(getSessionState().pendingAction,"uncertain navigation must still block chat input");
    retrySessionNavigation();assert.equal(sent.at(-1)?.request_id,request.requestId,"retry retains idempotency identity");
    ingestSessions({type:"error",request_id:request.requestId,error:"handler_failed:chat:session:switch"});
    assert.ok(getSessionState().pendingAction);assert.match(getSessionState().navigationError,/handler_failed/);
    returnToDisplayedSession();const rebound=getSessionState().pendingAction!;
    assert.equal(rebound.type,"switch");assert.notEqual(rebound.requestId,request.requestId);
    assert.equal(acceptIncomingSession("B","A",{request_id:request.requestId,requested_id:"B",effective_id:"B",status:"switched"}),false);
    assert.equal(acceptIncomingSession("A","A",{request_id:rebound.requestId,requested_id:"A",effective_id:"A",status:"switched"}),true);
    assert.equal(getSessionState().pendingAction,null);
  } finally {__resetSessionStoreForTests();globalThis.setTimeout=oldSetTimeout;}

  reset();incoming({type:"start",...route()});
  incoming({type:"tool:activity",...route(),event:"tool:start",tool:"ipython",call_id:"finished-cell",status:"running"});
  incoming({type:"tool:activity",...route(),event:"tool:start",tool:"ipython",call_id:"unobserved-cell",status:"running"});
  refreshExecutionStates();const read=sent.at(-1)!;
  incoming({type:"chat:execution",session_id:"A",request_id:read.request_id,observed_run_id:"run-one",observed_admission_id:"admission-one",
    busy:true,active_run_id:"run-one",active_admission_id:"admission-one",calls:[{call_id:"finished-cell",state:"succeeded",status:"ok",updated_at:Date.now()/1000}]});
  assert.equal(getChatState().turnSteps[0].status,"ok","actual durable results repair missed live result frames");
  assert.equal(getChatState().turnSteps[1].status,"running");
  incoming({type:"run:settled",...route(),status:"error",receipt:{tool_calls:2}});
  assert.equal(sharedTurnActive(),false,"settlement ends a turn even when done was lost");
  assert.deepEqual(getSessionState().workingSessionIds,[]);
  assert.equal(getChatState().turnSteps[1].status,"interrupted","settlement is not proof of tool success");
  applySession({id:"A",messages:[{role:"assistant",run_id:"run-one",text:"Actual durable failure"}],runtime:{busy:false}});
  assert.equal(getChatState().messages.at(-1)?.text,"Actual durable failure");
  assert.equal(getChatState().messages.at(-1)?.steps?.[0].status,"ok");
  assert.equal(getChatState().turnSteps.length,0);
  incoming({type:"start",...route("two")});refreshExecutionStates();const older=sent.at(-1)!;
  incoming({type:"run:settled",...route(),status:"ok",receipt:{}});assert.equal(sharedTurnActive(),true,"old terminal cannot settle a newer admission");
  turnController.end();incoming({type:"start",...route("three")});
  incoming({type:"chat:execution",session_id:"A",request_id:older.request_id,observed_run_id:"run-two",observed_admission_id:"admission-two",busy:false,calls:[]});
  assert.equal(sharedTurnActive(),true,"stale liveness response cannot settle the newer turn");

  reset();incoming({type:"start",...route()});refreshExecutionStates();const final=sent.at(-1)!;
  incoming({type:"chat:execution",session_id:"A",request_id:final.request_id,observed_run_id:"run-one",observed_admission_id:"admission-one",busy:false,calls:[]});
  assert.equal(sharedTurnActive(),false,"idle reconciliation recovers a completely missed terminal event");
  assert.equal(sent.at(-1)?.type,"chat:session:get");
  for(const status of ['ok','cancelled','error']) {
    reset();incoming({type:'start',...route()});refreshExecutionStates();const observed=sent.at(-1)!;
    incoming({type:'chat:execution',session_id:'A',request_id:observed.request_id,observed_run_id:'run-one',observed_admission_id:'admission-one',busy:false,calls:[],
      settlement:{chat_id:'A',run_id:'run-one',admission_id:'admission-one',receipt:{run_id:'run-one',settled:true,status}}});
    assert.equal(turnController.snapshot().lastEndStatus,status,'exact historical settlement supplies the terminal status');
  }
  reset();incoming({type:'start',...route()});refreshExecutionStates();const wrong=sent.at(-1)!;
  incoming({type:'chat:execution',session_id:'A',request_id:wrong.request_id,observed_run_id:'run-one',observed_admission_id:'admission-one',busy:false,calls:[],
    settlement:{chat_id:'A',run_id:'run-one',admission_id:'other-admission',receipt:{run_id:'run-one',settled:true,status:'ok'}}});
  assert.equal(turnController.snapshot().lastEndStatus,'interrupted','foreign settlement cannot imply successful completion');
  for(const source of ['goal','peer','peer:chat:lead']) {
    reset();
    incoming({type:'start',...route(),source,client_id:'native-client-one'});
    incoming({type:'token',...route(),source,client_id:'native-client-one',token:'Visible native work'});
    assert.equal(sharedTurnActive(),true);assert.equal(getChatState().streamText,'Visible native work');
    incoming({type:'done',...route(),source,client_id:'native-client-one',text:'Native result'});
    incoming({type:'start',...route('two'),source,client_id:'native-client-two'});
    assert.equal(sharedTurnActive(),true,'fresh native admissions may have a different ingress client');
    incoming({type:'token',...route(),source,client_id:'native-client-one',token:'stale'});
    assert.equal(getChatState().streamText,'','old admission cannot write into the fresh native turn');
  }
  reset();incoming({type:'start',...route(),source:'subagent',client_id:'child'});
  assert.equal(sharedTurnActive(),false,'child activity cannot claim the parent chat');
  reset();withCachedChatState("B",()=>turnController.begin({sessionId:"B",admissionId:"peer-admission",runId:"peer-run"}));
  incoming({type:"run:settled",session_id:"B",admission_id:"peer-admission",run_id:"peer-run",status:"error",receipt:{}});
  assert.equal(getChatState().sessionId,"A");assert.deepEqual(getSessionState().workingSessionIds,[],"background peers settle without switching the visible chat");

  reset();ingestSessions({type:"chat:sessions",items:[{id:"old-project-chat",title:"Archived project chat",archived:true,project:{root:"C:/fixture",name:"Fixture"}},
    {id:"current-project-chat",title:"Current project chat",project:{root:"C:/fixture",name:"Fixture"}}]});
  const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
  await act(async()=>root.render(<HistoryRail/>));
  const archived=host.querySelector('[data-group="Archived"]');
  assert.match(archived?.textContent || "",/Archived project chat/);
  assert.doesNotMatch(archived?.textContent || "",/Current project chat/);
  await act(async()=>root.unmount());host.remove();reset();
  console.log("Live swarm repairs: safe navigation recovery, exact-result/liveness reconciliation, stale admission fences, background settlement and archived projects passed");
}
