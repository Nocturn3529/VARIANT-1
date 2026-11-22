import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {HistoryRail} from "../frontend/main-deck/src/shell/HistoryRail";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {activateChatState,getCachedChatState,initialChatState,setChatState,setChatContext,getChatState,sharedTurnActive,withCachedChatState} from "../frontend/main-deck/src/chat/stateCore";
import {__resetTurnStoreForTests,turnController} from "../frontend/main-deck/src/state/turnStore";
import {__resetSessionStoreForTests,noteDisplayedSession,setSessionContext,switchSession,ingestSessions,getSessionState,retrySessionNavigation,returnToDisplayedSession,acceptIncomingSession} from "../frontend/main-deck/src/state/sessionStore";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {applySession} from "../frontend/main-deck/src/chat/session";
import {refreshExecutionStates,executionConnection,checkInitialSubmission} from "../frontend/main-deck/src/chat/executionRecovery";
import {sendUserMessage,setChatDraft} from "../frontend/main-deck/src/chat/composer";
import {setChatConnection} from "../frontend/main-deck/src/chat/connection";
import {setGoalComposing} from "../frontend/main-deck/src/chat/goals";
import {resetTraceAnnotations} from "../frontend/main-deck/src/chat/annotations";

export async function run() {
  const sent:Record<string,unknown>[]=[];
  const context={send:(row:Record<string,unknown>)=>{sent.push(row);return true;},notify(){},isOpen:()=>true};
  const reset=()=>{executionConnection(false);__resetChatStoreForTests();__resetTurnStoreForTests();__resetSessionStoreForTests();sent.length=0;
    setChatContext(context);setSessionContext(context);setChatState({...initialChatState(),sessionId:"A",clientId:"client",connected:true});noteDisplayedSession("A");};
  const incoming=(row:Record<string,unknown>)=>ingestChat(parseChatWsMessage(row));
  const route=(n="one")=>({session_id:"A",client_id:"client",source:"chat",admission_id:`admission-${n}`,run_id:`run-${n}`});
  for(const status of ["absent","admitted","queued","settled","rejected","unknown"]) {
    reset();
    incoming({type:"hello",submission_origin:"first-ingress"});
    const draft="  recover this input  ";
    setChatDraft(draft);assert.equal(sendUserMessage(draft),true);
    const ticket=sent.at(-1)!.ticket_id;
    assert.match(String(ticket),/^first-ingress\.[a-f0-9-]{36}$/);
    setChatConnection("offline");setChatConnection("connected");
    incoming({type:"hello",submission_origin:"second-ingress"});
    incoming({type:"chat:runtime",id:"A",runtime:{busy:false}});
    assert.equal(sendUserMessage("new instruction"),false,"unconfirmed input cannot become steering");
    const read=sent.filter(row=>row.type==="chat:execution:get" && row.ticket_id===ticket).at(-1)!;
    assert.ok(read.request_id);assert.notEqual(read.request_id,ticket);
    const receipt={type:"chat:execution",session_id:"A",ticket_id:ticket,request_id:read.request_id,
      ...(status==="queued"?{busy:true,active_run_id:"other-run",active_admission_id:"other-admission"}:{}),
      submission:{ticket_id:ticket,status,run_id:"run-one",admission_id:"admission-one"}};
    for(const unrelated of [{session_id:"B"},{ticket_id:"different-ticket"},{request_id:"old-read"},
      {submission:{ticket_id:"another-ticket",status:"absent"}}]) {
      incoming({...receipt,...unrelated});assert.equal(getChatState().initialSubmission?.readId,read.request_id);
    }
    incoming(receipt);
    assert.equal(sent.filter(row=>row.type==="chat").length,1,"a recovery read never replays an input");
    if(status==="absent") {
      assert.equal(getChatState().draft,draft);assert.equal(sharedTurnActive(),false);
      assert.ok(sendUserMessage(draft));assert.equal(sent.at(-1)?.ticket_id,ticket);
      assert.notEqual(sent.at(-1)?.request_id,ticket,"retry has its own acknowledgement identity");
      assert.equal(sent.at(-1)?.delivery,undefined);
      const retry=sent.at(-1)!;
      incoming({...receipt,request_id:ticket,submission:{...receipt.submission,status:"rejected"}});
      assert.equal(getChatState().initialSubmission?.requestId,retry.request_id);
      incoming({...receipt,request_id:retry.request_id,submission:{...receipt.submission,status:"admitted"}});
      assert.equal(turnController.snapshot().admissionId,"admission-one","an idempotent replay has its own correlated receipt");
    } else if(status==="admitted") {
      assert.equal(turnController.snapshot().admissionId,"admission-one");
      assert.ok(sendUserMessage("steer confirmed run"));assert.equal(sent.at(-1)?.admission_id,"admission-one");
    } else if(status==="unknown") {
      assert.ok(getChatState().initialSubmission?.checkFailed);
      assert.equal(getChatState().initialSubmission?.user.optimisticDraft,draft);
      assert.equal(sendUserMessage(draft),false);
      checkInitialSubmission(true);
      const next=sent.at(-1)!;assert.equal(next.ticket_id,ticket);assert.notEqual(next.request_id,read.request_id);
      incoming({...receipt,submission:{...receipt.submission,status:"absent"}});
      assert.equal(getChatState().initialSubmission?.readId,next.request_id,"a delayed old lookup cannot undo the retry");
      incoming({...receipt,request_id:next.request_id,submission:{...receipt.submission,status:"absent"}});
      assert.equal(getChatState().draft,draft);
    } else if(status==="rejected") {
      assert.equal(getChatState().draft,draft);assert.equal(getChatState().turnActive,false);
      assert.ok(sendUserMessage(draft));assert.notEqual(sent.at(-1)?.ticket_id,ticket,"a terminal rejection consumes its ticket");
    } else {
      assert.equal(getChatState().initialSubmission,undefined);
      if(status==="queued"){assert.equal(getChatState().messages.length,0);assert.equal(turnController.snapshot().admissionId,"other-admission");}
      else {
        assert.equal(sharedTurnActive(),false);
        applySession({id:"A",runtime:{busy:false},messages:[{role:"user",text:draft,ticket_id:ticket},{role:"assistant",text:"Saved answer"}]});
        assert.equal(getChatState().messages.length,2);assert.equal(getChatState().messages.at(-1)?.text,"Saved answer");
      }
    }
  }
  reset();sendUserMessage("background submission");
  setChatConnection("offline");setChatConnection("connected");
  const background=sent.find(row=>row.type==="chat:execution:get" && row.ticket_id)!;
  activateChatState("B");noteDisplayedSession("B");setChatDraft("newer chat's draft");
  incoming({type:"chat:execution",session_id:"A",request_id:background.request_id,ticket_id:background.ticket_id,
    submission:{ticket_id:background.ticket_id,status:"absent"}});
  assert.equal(getChatState().draft,"newer chat's draft");assert.equal(getCachedChatState("A")?.draft,"background submission");
  assert.equal(sent.at(-1)?.type,"chat:runtime:get","background recovery does not navigate the viewer");
  reset();
  const image={id:"original-image",kind:"image" as const,name:"original.jpg",mime:"image/jpeg",size:3,data:"YWJj"};
  setChatState({...getChatState(),draft:"Original message",attachments:[image]});
  assert.ok(sendUserMessage("Original message"));
  setChatConnection("offline");setChatConnection("connected");
  const initialRead=sent.find(row=>row.type==="chat:execution:get" && row.ticket_id)!;
  setGoalComposing(true);setChatDraft("Independent objective");
  assert.ok(sendUserMessage("Independent objective"));const goal=sent.at(-1)!;
  assert.equal(goal.type,"goal:submit");assert.equal(getChatState().initialSubmission?.readId,initialRead.request_id);
  incoming({type:"chat:execution",session_id:"A",request_id:initialRead.request_id,ticket_id:initialRead.ticket_id,
    submission:{ticket_id:initialRead.ticket_id,status:"absent"}});
  assert.equal(getChatState().draft,"Independent objective");assert.deepEqual(getChatState().attachments,[]);
  assert.equal(getChatState().goal.pending?.requestId,goal.request_id);
  incoming({type:"goal:accepted",session_id:"A",operation:"submit",request_id:goal.request_id,result:{goal:{
    goal_id:"new-goal",session_id:"A",request_id:goal.request_id,objective:"Independent objective",status:"active",
    version:1,token_budget:null,tokens_used:0,time_used_s:0,created_at:1,updated_at:1}}});
  assert.equal(getChatState().goal.pending,undefined);assert.equal(getChatState().draft,"Original message");
  assert.deepEqual(getChatState().attachments,[image]);
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
    assert.ok(getSessionState().pendingAction);assert.match(getSessionState().navigationError,/Could not confirm the conversation/);
    assert.doesNotMatch(getSessionState().navigationError,/handler_failed/);
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
  const recoveredAnnotation=sent.findLast(row=>row.type==="chat:session:annotate")!;
  assert.equal(recoveredAnnotation.id,"A");assert.equal(recoveredAnnotation.run_id,"run-one");
  assert.equal((recoveredAnnotation.steps as {status:string}[])[0].status,"ok");
  assert.equal(getChatState().messages.at(-1)?.tracePersistence,"pending");
  incoming({type:"chat:session:annotated",id:"A",run_id:"run-one",request_id:recoveredAnnotation.request_id,ok:true});
  assert.equal(getChatState().messages.at(-1)?.tracePersistence,"saved");
  const annotationCount=sent.filter(row=>row.type==="chat:session:annotate").length;
  applySession({id:"A",messages:[{role:"assistant",run_id:"run-one",text:"Actual durable failure",steps:recoveredAnnotation.steps}],runtime:{busy:false}});
  assert.equal(getChatState().messages.at(-1)?.steps?.[0].status,"ok","reloading the persisted annotation retains the trace");
  assert.equal(sent.filter(row=>row.type==="chat:session:annotate").length,annotationCount,"saved trace is not annotated again");
  resetTraceAnnotations();
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
