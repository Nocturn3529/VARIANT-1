import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {ChatBrowserStatus} from "../frontend/main-deck/src/chat/ChatBrowserStatus";
import {ChatRecoveryStatus} from "../frontend/main-deck/src/chat/ChatRecoveryStatus";
import * as browser from "../frontend/main-deck/src/browserSettingsStore";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {initialChatState,setChatState,patchChatState,setChatContext,getChatState} from "../frontend/main-deck/src/chat/stateCore";
import {resumeOrphanedTask} from "../frontend/main-deck/src/chat/composer";

export async function run() {
  __resetChatStoreForTests();browser.__resetBrowserSettingsForTests();
  const sent:Array<Record<string,unknown>>=[];
  const context={send:(value:Record<string,unknown>)=>{sent.push(value);return true;},isOpen:()=>true,notify(){}};
  setChatContext(context);browser.setBrowserSettingsContext(context);browser.setBrowserSettingsConnection("connected");
  setChatState({...initialChatState(),sessionId:"A",connected:true});
  const host=document.createElement("div"),root=createRoot(host);document.body.appendChild(host);
  const snapshot=(revision:number,state="connecting",request_id?:string)=>browser.ingestBrowserSettings({type:"browser:state",chat_id:"A",revision,state,request_id,selection:{mode:"embedded"},actions:[]});
  const button=(name:string)=>host.querySelector<HTMLButtonElement>(`[aria-label="${name}"]`)!;
  try {
    await act(async()=>root.render(<><ChatBrowserStatus/><ChatRecoveryStatus/></>));
    await act(async()=>snapshot(1,"connecting",String(sent.at(-1)?.request_id)));
    assert.ok(host.textContent?.includes("Connecting browser"));assert.ok(host.textContent?.includes("Check connection"));
    await act(async()=>button("Dismiss browser notice").click());assert.equal(host.textContent,"");
    await act(async()=>snapshot(2,"connection_failed"));assert.ok(host.textContent?.includes("Browser connection failed"),"new failure resurfaces after dismissal");
    await act(async()=>browser.refreshBrowserChat("A"));
    await act(async()=>browser.ingestBrowserSettings({type:"browser:state",chat_id:"A",request_id:sent.at(-1)?.request_id,error:{message:"old read failed"}}));
    assert.ok(host.textContent?.includes("old read failed"));
    await act(async()=>snapshot(3,"ready"));assert.equal(host.textContent,"","new ready snapshot clears stale read error and banner");
    await act(async()=>snapshot(2,"connecting"));assert.equal(host.textContent,"","old state cannot revive banner");
    await act(async()=>patchChatState({orphanedTask:{task_id:"legacy",goal:"unscoped"}}));
    assert.equal(host.textContent,"");assert.equal(resumeOrphanedTask(),false,"unscoped checkpoint cannot resume current chat");
    await act(async()=>patchChatState({orphanedTask:{task_id:"old",session_id:"B",goal:"other chat"}}));assert.equal(host.textContent,"");
    await act(async()=>patchChatState({orphanedTask:{task_id:"owned",session_id:"A",goal:"Recover work",updated_at:1}}));assert.ok(host.textContent?.includes("Recover work"));
    await act(async()=>button("Dismiss recovery notice").click());assert.equal(host.textContent,"");
    await act(async()=>patchChatState({orphanedTask:{task_id:"new",session_id:"A",goal:"New checkpoint",updated_at:2}}));assert.ok(host.textContent?.includes("New checkpoint"));
    await act(async()=>patchChatState({turnActive:true}));assert.equal(host.textContent,"");
    await act(async()=>patchChatState({turnActive:false,connected:false}));
    assert.ok(Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(b=>b.textContent==="Resume task")?.disabled);
    assert.equal(getChatState().orphanedTask?.task_id,"new","dismiss is local visibility, not checkpoint deletion");
  } finally {
    await act(async()=>root.unmount());host.remove();browser.__resetBrowserSettingsForTests();
  }
  console.log("Chat notices: ready supersedes read error, stale revision, scoped dismissal, checkpoint ownership and resume eligibility passed");
}
