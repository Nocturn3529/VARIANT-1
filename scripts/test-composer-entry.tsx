import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {activateChatState,getCachedChatState,getChatState,initialChatState,retainChatDraft,setChatContext,setChatState} from "../frontend/main-deck/src/chat/stateCore";
import {addChatFiles,invalidatePendingChatAttachments} from "../frontend/main-deck/src/chat/attachments";
import {setChatDelivery,setMutationWriteEnabled,submitUserInput} from "../frontend/main-deck/src/chat/composer";
import {__resetSessionStoreForTests, noteDisplayedSession, setSessionConnection, setSessionContext} from "../frontend/main-deck/src/state/sessionStore";
import {__resetTurnStoreForTests,turnController} from "../frontend/main-deck/src/state/turnStore";
import {__resetSessionContextStoreForTests,changeSessionSettings,getContextForSession,ingestSessionContext,setSessionContextConnection,setSessionContextContext} from "../frontend/main-deck/src/sessionContextStore";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {applyRuntimeSnapshot} from "../frontend/main-deck/src/chat/session";
import {setChatConnection} from "../frontend/main-deck/src/chat/connection";
import {requestChatPause} from "../frontend/main-deck/src/chat/pause";
import {resetWireStatus} from "../frontend/main-deck/src/connectionUi";

const pause=(ms=0)=>new Promise(resolve=>setTimeout(resolve,ms));
const key=(node:Element,key:string,extra:KeyboardEventInit={})=>node.dispatchEvent(new KeyboardEvent("keydown",{key,bubbles:true,cancelable:true,...extra}));
async function input(node:HTMLInputElement|HTMLTextAreaElement,value:string) {
  await act(async()=>{Object.getOwnPropertyDescriptor(node.tagName==="TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype,"value")!.set!.call(node,value);node.dispatchEvent(new Event("input",{bubbles:true}));});
}

export async function run() {
  const commands:Array<Record<string,unknown>>=[];
  let online=true;
  const context={send:(command:Record<string,unknown>)=>{commands.push(command);return online;},isOpen:()=>online,notify() {}};
  __resetChatStoreForTests();__resetSessionStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();
  setSessionContext(context);setChatContext(context);setSessionContextContext(context);setSessionConnection("connected");
  setSessionContextConnection("connected");
  setChatState({...initialChatState(),sessionId:"A",connected:true});noteDisplayedSession("A");
  const host=document.createElement("div");host.className="app-shell";document.body.appendChild(host);const root=createRoot(host);
  try {
    await act(async()=>{root.render(<ChatComposer/>);await pause();});
    const textarea=host.querySelector<HTMLTextAreaElement>("#composer-input")!;
    await input(textarea,"こんにちは");
    await act(async()=>key(textarea,"Enter",{isComposing:true}));
    assert.equal(commands.filter(command=>command.type==="chat").length,0,"IME confirmation must never send a message");
    assert.equal(getChatState().draft,"こんにちは");
    await act(async()=>key(textarea,"Enter",{shiftKey:true}));
    assert.equal(commands.filter(command=>command.type==="chat").length,0,"Shift+Enter is reserved for newlines");

    await input(textarea,"/");await act(async()=>key(textarea,"ArrowDown"));await act(async()=>key(textarea,"Enter"));
    assert.equal(getChatState().draft,"/system-status","Enter chooses the highlighted command without sending it");
    assert.equal(commands.filter(command=>command.type==="chat").length,0);
    await act(async()=>key(textarea,"Escape"));
    assert.equal(host.querySelector("#composer-command-menu"),null);

    online=false;
    await act(async()=>setChatState({...getChatState(),connected:false,draft:"Offline draft"}));
    assert.equal(textarea.disabled,false,"drafts remain editable offline");
    assert.equal(host.querySelector<HTMLButtonElement>('[aria-label="Send message"]')!.disabled,true);
    await input(textarea,"Saved while offline");await act(async()=>key(textarea,"Enter"));
    assert.equal(getChatState().draft,"Saved while offline");
    assert.equal(commands.filter(command=>command.type==="chat").length,0);
    online=true;await act(async()=>setChatState({...getChatState(),connected:true}));
    const send=()=>host.querySelector<HTMLButtonElement>('[aria-label="Send message"]')!;
    const originalReader=globalThis.FileReader;
    let finishRead:()=>void=()=>{};
    class Reader {
      result:string|null=null;onload:(()=>void)|null=null;onerror:(()=>void)|null=null;
      readAsText(){finishRead=()=>{this.result="prepared contents";this.onload?.();};}
    }
    let preparation:Promise<void>;
    try{
      Object.assign(globalThis,{FileReader:Reader});
      await act(async()=>{preparation=addChatFiles([{name:"notes.txt",type:"text/plain",size:17} as File]);await pause();});
      assert.equal(send().disabled,true);assert.match(host.querySelector('.composer-preparation')!.textContent!,/Preparing 1 attachment/);
      assert.equal(submitUserInput({source:"voice",sessionId:"A",text:"voice while preparing"}),false,"automatic voice cannot invalidate a pending file read");
      await act(async()=>key(textarea,"Enter"));assert.equal(commands.filter(command=>command.type==="chat").length,0);
      await act(async()=>{finishRead();await preparation;});assert.equal(getChatState().attachments[0]?.text,"prepared contents");assert.equal(send().disabled,false);
      await act(async()=>host.querySelector<HTMLButtonElement>('[aria-label="Remove attachment notes.txt"]')!.click());
      assert.equal(getChatState().attachments.length,0);
      await act(async()=>{preparation=addChatFiles([{name:"cancelled.txt",type:"text/plain",size:17} as File]);await pause();invalidatePendingChatAttachments();finishRead();await preparation;});
      assert.equal(getChatState().attachments.length,0);assert.equal(getChatState().attachmentsPreparing,0);
      await act(async()=>host.querySelector<HTMLButtonElement>('#attach-button')!.click());
      await act(async()=>{activateChatState("B");setChatState({...getChatState(),sessionId:"B",connected:true});noteDisplayedSession("B");});
      const picker=host.querySelector<HTMLInputElement>('input[type="file"]')!;
      Object.defineProperty(picker,"files",{configurable:true,value:[{name:"belongs-to-A.txt",type:"text/plain",size:17}]});
      await act(async()=>{picker.dispatchEvent(new Event("change",{bubbles:true}));await pause();});
      assert.equal(getChatState().attachments.length,0,"a picker opened in A cannot attach into B");
    }finally{Object.assign(globalThis,{FileReader:originalReader});}
    const textDrag=new Event("dragover",{bubbles:true,cancelable:true});
    Object.defineProperty(textDrag,"dataTransfer",{value:{types:["text/plain"]}});
    await act(async()=>textarea.dispatchEvent(textDrag));assert.equal(textDrag.defaultPrevented,false,"ordinary text dragging is not file upload");
    await act(async()=>{activateChatState("A");setChatState({...getChatState(),sessionId:"A",connected:true});noteDisplayedSession("A");});

    await act(async()=>ingestSessionContext({type:"chat:context",session_id:"A",route:"local",provider:"local",model:"C:/models/A/shared.gguf",status:"ready"}));
    await act(async()=>{host.querySelector<HTMLButtonElement>('#model-button')!.click();await pause();});
    const catalog=commands.filter(command=>command.type==="model:options").at(-1)!;
    await act(async()=>{ingestSessionContext({type:"model:options",session_id:"A",request_id:catalog.request_id,providers:[{id:"local",mode:"local",name:"Local models",models:[
      {id:"C:/models/A/shared.gguf",label:"First model",reasoning_efforts:["low","high"]},
      {id:"C:/models/B/shared.gguf",label:"Second model",reasoning_efforts:["low","high"]},
      {id:"unavailable",label:"Unavailable",selectable:false,reasoning_efforts:["low"]},
    ]}]});await pause();});
    assert.equal(document.querySelectorAll('[data-model-option][aria-pressed="true"]').length,1,"same basename cannot select two distinct local models");
    await act(async()=>document.querySelector<HTMLButtonElement>('.model-picker__provider')!.click());
    assert.equal(document.querySelectorAll('[data-model-option]').length,0,"provider headers actually collapse their models");
    await act(async()=>document.querySelector<HTMLButtonElement>('.model-picker__provider')!.click());
    assert.equal(document.querySelector<HTMLButtonElement>('[aria-label="Unavailable options"]')!.disabled,true);
    await act(async()=>document.querySelectorAll<HTMLButtonElement>('[data-model-option]')[1].dispatchEvent(new MouseEvent("mouseover",{bubbles:true})));
    assert.equal(document.querySelector('.model-picker__effort-menu'),null,"hover does not activate reasoning menus");
    await act(async()=>document.querySelectorAll<HTMLButtonElement>('[data-model-option]')[1].click());
    const setting=commands.filter(command=>command.type==="mode:set").at(-1)!;
    assert.ok(setting.request_id);assert.equal(setting.id,"A");assert.equal(send().disabled,true);
    assert.equal(setMutationWriteEnabled(true,"A"),false,"an already-rendered Mutation handler cannot race a pending model change");
    await act(async()=>ingestSessionContext({type:"session:settings:ack",session_id:"B",request_id:setting.request_id,operation:"mode:set",status:"applied"}));
    assert.equal(send().disabled,true,"a different chat cannot settle the model change");
    await act(async()=>ingestSessionContext({type:"session:settings:ack",session_id:"A",request_id:setting.request_id,operation:"mode:set",status:"applied",route:{mode:"local",provider:"local",model:"C:/models/B/shared.gguf"}}));
    assert.equal(send().disabled,false);assert.equal(getContextForSession("A").model,"C:/models/B/shared.gguf");
    await act(async()=>ingestSessionContext({type:"model:options",session_id:"A",request_id:catalog.request_id,providers:[]}));
    assert.equal(getContextForSession("A").modelProviders.length,1,"a late old catalog cannot overwrite the accepted list");

    await act(async()=>changeSessionSettings("A",{type:"reasoning:effort:set",effort:"high"},"High reasoning"));
    await act(async()=>{setSessionContextConnection("offline");setSessionContextConnection("connected");});
    const check=commands.filter(command=>command.type==="session:settings:get").at(-1)!;
    assert.ok(check.request_id);assert.equal(send().disabled,true);
    await act(async()=>ingestSessionContext({type:"session:settings:snapshot",session_id:"A",request_id:check.request_id,pending:true}));
    assert.equal(send().disabled,true,"pending backend application keeps the composer gated after reconnect");
    await act(async()=>pause(1050));const settledCheck=commands.filter(command=>command.type==="session:settings:get").at(-1)!;
    assert.notEqual(settledCheck.request_id,check.request_id);
    await act(async()=>ingestSessionContext({type:"session:settings:snapshot",session_id:"A",request_id:settledCheck.request_id,pending:false,route:{mode:"local",provider:"local",model:"C:/models/B/shared.gguf",reasoning_effort:"high"}}));
    assert.equal(send().disabled,false);assert.equal(getContextForSession("A").reasoningEffort,"high");

    await act(async()=>{setChatDelivery("follow_up");activateChatState("B");setChatState({...getChatState(),connected:true});noteDisplayedSession("B");});
    assert.equal(getChatState().deliveryMode,"steer","delivery choice does not leak between chats");
    retainChatDraft("A","Retained voice text","A");
    assert.equal(getChatState().draft,"","retaining speech in A cannot mutate B");assert.match(getCachedChatState("A")!.draft,/Saved while offline\n\nRetained voice text/);
    await act(async()=>{activateChatState("A");setChatState({...getChatState(),connected:true,turnActive:true});noteDisplayedSession("A");turnController.begin({sessionId:"A",clientId:getChatState().clientId,admissionId:"run-A",runId:"task-A"});});
    const lastChat=()=>commands.filter(command=>command.type==="chat").at(-1)!;
    await act(async()=>setChatDelivery("steer","A"));
    await input(textarea,"ordinary enter");await act(async()=>key(textarea,"Enter"));
    assert.equal(lastChat().delivery,"steer");
    await input(textarea,"ordinary steer button");await act(async()=>host.querySelector<HTMLButtonElement>('[aria-label="Send steering message"]')!.click());
    await act(async()=>submitUserInput({source:"voice",sessionId:"A",text:"ordinary voice steer"}));
    await act(async()=>setChatDelivery("follow_up","A"));
    await input(textarea,"queue normally");await act(async()=>key(textarea,"Enter"));
    assert.equal(lastChat().delivery,"follow_up");
    assert.equal(commands.filter(command=>command.type==="cancel").length,0,"Enter, Steer, voice and Queue never invoke cancellation");
    const pauseButton=()=>host.querySelector<HTMLButtonElement>('[aria-label="Pause task"]');
    const resumeButton=()=>host.querySelector<HTMLButtonElement>('[aria-label="Resume task"]');
    const stopButton=()=>host.querySelector<HTMLButtonElement>('[aria-label="Stop response"]');
    const pauseEvent=(values:Record<string,unknown>)=>ingestChat(parseChatWsMessage({type:"chat:pause_state",session_id:"A",admission_id:"run-A",run_id:"task-A",accepted:true,...values}));
    assert.ok(pauseButton());assert.equal(stopButton(),null,"running task exposes Pause, not Stop");
    await input(textarea,"draft survives pause");const queueBefore=getChatState().pendingActiveInputs.length;
    await act(async()=>{pauseButton()!.click();pauseButton()!.click();});
    const sentPause=commands.filter(command=>command.type==="chat:pause").at(-1)!;
    assert.equal(commands.filter(command=>command.type==="chat:pause").length,1);
    assert.equal(sentPause.session_id,"A");assert.equal(sentPause.admission_id,"run-A");assert.equal(sentPause.run_id,"task-A");assert.ok(sentPause.request_id);
    assert.equal(getChatState().draft,"draft survives pause");assert.equal(getChatState().pendingActiveInputs.length,queueBefore);
    assert.equal(resumeButton(),null);assert.equal(stopButton(),null);assert.equal(pauseButton()!.disabled,true);
    await act(async()=>pauseEvent({pause_revision:1,state:"pausing",request_id:sentPause.request_id}));
    assert.match(host.textContent!,/Pausing after current step/);assert.equal(stopButton(),null);
    for(const wrong of [{session_id:"B"},{admission_id:"old"},{run_id:"old"}]) {
      await act(async()=>pauseEvent({pause_revision:99,state:"paused",...wrong}));assert.equal(resumeButton(),null);
    }
    await act(async()=>pauseEvent({pause_revision:1,state:"paused"}));assert.equal(resumeButton(),null,"equal revision cannot change state");
    for(const invalid of [{pause_revision:-1},{pause_revision:"2"},{pause_revision:NaN},{state:"unknown"}]) {
      await act(async()=>pauseEvent({pause_revision:2,state:"paused",...invalid}));assert.equal(resumeButton(),null,"malformed authority cannot claim paused");
    }
    await act(async()=>pauseEvent({pause_revision:2,state:"paused"}));assert.ok(resumeButton());assert.ok(stopButton());
    await act(async()=>setChatDelivery("steer","A"));await act(async()=>key(textarea,"Enter"));
    assert.equal(lastChat().text,"draft survives pause");assert.equal(lastChat().delivery,"steer");
    assert.equal(commands.filter(command=>command.type==="chat:resume").length,0,"sending while paused cannot resume the run");
    assert.equal(getChatState().pendingActiveInputs.length,queueBefore+1);
    assert.equal(commands.filter(command=>command.type==="cancel").length,0);
    await act(async()=>{resumeButton()!.click();resumeButton()!.click();});
    const firstResume=commands.filter(command=>command.type==="chat:resume").at(-1)!;
    assert.equal(commands.filter(command=>command.type==="chat:resume").length,1);assert.equal(resumeButton()!.disabled,true);
    assert.equal(firstResume.admission_id,sentPause.admission_id);assert.equal(firstResume.run_id,sentPause.run_id);
    await act(async()=>pauseEvent({pause_revision:2,state:"paused",accepted:false,request_id:"unrelated",error:"no"}));assert.equal(resumeButton()!.disabled,true);
    await act(async()=>pauseEvent({pause_revision:2,state:"paused",accepted:false,request_id:firstResume.request_id,error:"try again"}));assert.equal(resumeButton()!.disabled,false);
    await act(async()=>resumeButton()!.click());const secondResume=commands.filter(command=>command.type==="chat:resume").at(-1)!;
    await act(async()=>pauseEvent({pause_revision:2,state:"paused",accepted:false,request_id:firstResume.request_id}));assert.equal(resumeButton()!.disabled,true,"old rejection cannot settle a newer request");
    await act(async()=>pauseEvent({pause_revision:3,state:"running",request_id:secondResume.request_id}));assert.ok(pauseButton());assert.equal(stopButton(),null);
    await act(async()=>pauseEvent({pause_revision:2,state:"paused"}));assert.equal(resumeButton(),null,"stale paused broadcast ignored");
    await act(async()=>applyRuntimeSnapshot("A",{busy:true,active_admission_id:"run-A",active_run_id:"task-A",pause_state:"paused",pause_revision:2}));assert.equal(resumeButton(),null,"runtime snapshot cannot regress revision");
    await act(async()=>applyRuntimeSnapshot("A",{busy:true,active_admission_id:"old",active_run_id:"old",pause_state:"paused",pause_revision:99}));assert.equal(resumeButton(),null,"foreign runtime owner cannot rebind active task");
    await act(async()=>applyRuntimeSnapshot("A",{busy:true,active_admission_id:"run-A",active_run_id:"task-A",pause_state:"paused",pause_revision:4}));assert.ok(resumeButton());
    await act(async()=>applyRuntimeSnapshot("A",{busy:false,pause_state:"idle",pause_revision:99}));assert.ok(resumeButton(),"unscoped idle snapshot cannot end an acknowledged paused run");
    resetWireStatus("chat");online=false;await act(async()=>setChatConnection("offline"));assert.equal(getChatState().pause?.synced,false);assert.equal(requestChatPause("resume","A"),false);
    online=true;await act(async()=>{setChatConnection("connected");await pause(230);});
    assert.equal(commands.filter(command=>command.type==="chat:runtime:get").at(-1)!.id,"A");assert.equal(pauseButton()!.disabled,true,"reconnect waits for authority");
    await act(async()=>applyRuntimeSnapshot("A",{busy:true,active_admission_id:"run-A",active_run_id:"task-A",pause_state:"paused",pause_revision:4}));assert.equal(resumeButton()!.disabled,false);
    await act(async()=>{activateChatState("B");setChatState({...getChatState(),connected:true});noteDisplayedSession("B");});
    await act(async()=>pauseEvent({pause_revision:5,state:"running"}));assert.equal(getChatState().pause,null,"offscreen A state cannot affect B");assert.equal(getCachedChatState("A")!.pause?.state,"running");
    await act(async()=>{activateChatState("A");noteDisplayedSession("A");pauseEvent({pause_revision:6,state:"paused"});});
    assert.ok(resumeButton());assert.ok(stopButton());
    await act(async()=>setChatState({...getChatState(),runtime:{...getChatState().runtime!,activeAdmissionId:"stale-runtime-admission",activeRunId:"stale-runtime-run"}}));
    assert.equal(turnController.snapshot().admissionId,"run-A");
    assert.equal(getChatState().pause?.admissionId,"run-A");
    assert.ok(stopButton(),"acknowledged paused authority still exposes Stop with a stale runtime cache");
    await act(async()=>{host.querySelector<HTMLButtonElement>('[aria-label="Stop response"]')!.click();host.querySelector<HTMLButtonElement>('[aria-label="Stop response"]')!.click();});
    assert.equal(commands.filter(command=>command.type==="cancel").length,1);
    assert.equal(commands.filter(command=>command.type==="cancel")[0].admission_id,"run-A");
    assert.equal(commands.filter(command=>command.type==="cancel")[0].run_id,"task-A","Stop uses current turn fences, not stale runtime metadata");
    assert.equal(submitUserInput({source:"voice",text:"blocked",sessionId:"A"}),false);
    await act(async()=>ingestChat(parseChatWsMessage({type:"cancelling",session_id:"A",admission_id:"run-A",accepted:false,error:"stale_run"})));
    assert.equal(host.querySelector<HTMLButtonElement>('[aria-label="Stop response"]')!.disabled,false,"rejected cancellation permits a fresh Stop");
    await act(async()=>ingestChat(parseChatWsMessage({type:"done",session_id:"A",admission_id:"run-A",run_id:"task-A",text:"Complete"})));
    assert.equal(getChatState().pause,null);assert.equal(pauseButton(),null);assert.equal(resumeButton(),null);
    await act(async()=>pauseEvent({pause_revision:999,state:"paused"}));assert.equal(getChatState().pause,null,"late paused event cannot revive a completed run");
    await act(async()=>{turnController.begin({sessionId:"A",clientId:getChatState().clientId,admissionId:"missed-old",runId:"missed-old-run"});setChatState({...getChatState(),turnActive:true});setChatConnection("offline");});
    await act(async()=>applyRuntimeSnapshot("A",{busy:true,active_admission_id:"reattached-new",active_run_id:"reattached-run",pause_state:"paused",pause_revision:1}));
    assert.equal(getChatState().pause?.admissionId,"reattached-new","reconnect snapshot can discover a newer admission after missed lifecycle events");
    await act(async()=>resumeButton()!.click());
    assert.equal(commands.filter(command=>command.type==="chat:resume").at(-1)!.admission_id,"reattached-new");
    await act(async()=>pauseEvent({pause_revision:1000,state:"running"}));assert.equal(getChatState().pause?.state,"paused","old admission cannot overwrite reattached authority");
    console.log("Composer: IME confirmation, newline semantics, keyboard command selection, and editable offline drafts passed");
    console.log("Composer: attachment preparation/cancellation, picker ownership, model identity/ack/reconnect fences, per-chat delivery, retained speech and Stop deduplication passed");
    console.log("Composer steering: ordinary Enter, Steer, voice and Queue never cancel; existing scoped Stop remains separate and deduplicated");
    console.log("Composer pause: boundary-only acknowledgement, Pause/Resume/paused-only Stop, queue/draft preservation, revision/scope/request guards, same-run resume, reconnect snapshots and session isolation passed");
  } finally {
    await act(async()=>root.unmount());host.remove();resetWireStatus("chat");__resetChatStoreForTests();__resetSessionStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();
  }
}
