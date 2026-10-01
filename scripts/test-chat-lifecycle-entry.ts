import assert from "node:assert/strict";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {initialChatState,setChatState,setChatContext,getChatState,activateChatState,getCachedChatState,patchChatState,sharedTurnActive} from "../frontend/main-deck/src/chat/stateCore";
import {beginTurn,__resetTurnStoreForTests,activeTurnSessionIds} from "../frontend/main-deck/src/state/turnStore";
import {ingestSessionContext,getContextForSession,__resetSessionContextStoreForTests,setSessionContextContext,setSessionContextConnection,requestSessionContext,changeSessionSettings} from "../frontend/main-deck/src/sessionContextStore";
import {disclosureKey,disclosureChoice,rememberDisclosure} from "../frontend/main-deck/src/chat/disclosures";
import {beginTurnReceipt,noteReceiptTool,snapshotTurnReceipt} from "../frontend/main-deck/src/chat/receipt";
import {releaseDeletedChat} from "../frontend/main-deck/src/state/deletedChatCleanup";
import {enterOverview,stopOverview,setOverviewContext} from "../frontend/main-deck/src/overviewStore";

export async function run() {
  __resetChatStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();
  setChatContext({send:()=>true,isOpen:()=>true,notify(){}});
  setChatState({...initialChatState(),sessionId:"running",connected:true});beginTurn({sessionId:"running",runId:"run-running"});
  activateChatState("idle");beginTurnReceipt();noteReceiptTool("read_file");
  ingestSessionContext({type:"chat:context",session_id:"idle",route:"cloud",provider:"fixture",model:"fixture-model"});
  assert.equal(getContextForSession('idle').model,'fixture-model');
  const choice=disclosureKey("idle","run","call");rememberDisclosure(choice,true);
  activateChatState("draft");patchChatState({draft:"Keep this draft"});
  activateChatState("preparing");patchChatState({attachmentsPreparing:1});
  activateChatState("goal");patchChatState({goal:{synced:true,snapshot:{goal:{status:"paused"}} as any}});
  activateChatState("settings");setSessionContextContext({send:()=>true,isOpen:()=>true,notify(){}});setSessionContextConnection("connected");requestSessionContext("settings");
  assert.equal(changeSessionSettings("settings",{type:"reasoning:effort:set",effort:"low"},"Low effort"),true);
  for(let index=0;index<12;index++)activateChatState(`inactive-${index}`);
  assert.ok(getCachedChatState("running")?.turnActive,'active work survives cache trimming');
  assert.equal(getCachedChatState("draft")?.draft,"Keep this draft");assert.equal(getCachedChatState("preparing")?.attachmentsPreparing,1);
  assert.equal(getCachedChatState("goal")?.goal.snapshot?.goal.status,"paused");assert.ok(getContextForSession("settings").settingsPending && getCachedChatState("settings"),'unfinished model settings survive cache pressure');
  assert.equal(getCachedChatState("idle"),undefined);assert.equal(getContextForSession("idle").model,"");assert.equal(disclosureChoice(choice),undefined);
  activateChatState("idle");assert.equal(snapshotTurnReceipt(),undefined,'eviction releases the matching receipt projection');
  activateChatState("other-active");beginTurn({sessionId:"other-active",runId:"run-other"});
  releaseDeletedChat("running");
  assert.equal(getCachedChatState("running"),undefined);assert.ok(sharedTurnActive());assert.equal(getChatState().turnActive,true,'deleting another chat cannot stop the selected turn');
  assert.deepEqual(activeTurnSessionIds(),['other-active']);
  releaseDeletedChat("draft");assert.equal(getCachedChatState("draft"),undefined,'confirmed deletion releases an otherwise protected draft');
  const priorSet=globalThis.setInterval,priorClear=globalThis.clearInterval;
  let intervals=0,cleared=0;
  globalThis.setInterval=((...args:Parameters<typeof setInterval>)=>{intervals++;return priorSet(...args);}) as typeof setInterval;
  globalThis.clearInterval=((id:ReturnType<typeof setInterval>)=>{cleared++;priorClear(id);}) as typeof clearInterval;
  try {
    setOverviewContext({send:()=>true,isOpen:()=>true,notify(){}});enterOverview("overview");
    assert.equal(intervals,2);stopOverview();assert.equal(cleared,2);stopOverview();assert.equal(cleared,2,'overview disposal is idempotent');
  } finally {globalThis.setInterval=priorSet;globalThis.clearInterval=priorClear;__resetChatStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();}
  console.log('Chat lifecycle: coordinated idle/deleted projections, preserved live runs/drafts/preparation, scoped turn notifications and overview disposal passed');
}
