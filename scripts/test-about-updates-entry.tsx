import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {AboutSettings} from "../frontend/main-deck/src/AboutSettings";
import {HeaderTools} from "../frontend/main-deck/src/shell/HeaderTools";
import {setAboutContext} from "../frontend/main-deck/src/aboutStore";
import {getAppState} from "../frontend/main-deck/src/state/appStore";
import {__resetTurnStoreForTests,turnController} from "../frontend/main-deck/src/state/turnStore";
import type {UpdateState} from "../frontend/main-deck/src/types";

/** Settings › About and the header pill mirror the main process; only clicks download or install. */
export async function run() {
  const calls:string[]=[];
  let emit:(state:UpdateState)=>void=()=>{};
  setAboutContext({send:()=>true,notify(){},api:{
    onUpdateState:callback=>{emit=callback;return ()=>{};},
    getUpdateState:async()=>null,
    downloadUpdate:async()=>{calls.push("download");return {ok:true};},
    cancelUpdateDownload:async()=>{calls.push("cancel");return {ok:true};},
    installUpdate:async()=>{calls.push("install");return {ok:true};},
    openUpdateRelease:async()=>{calls.push("release");return {ok:true};},
  }});
  const base:UpdateState={status:"available",reason:"",currentVersion:"0.1.1-preview.3",platform:"win32",version:"0.1.1-preview.4",releaseName:"",
    releaseUrl:"https://github.com/Nocturn3529/VARIANT-1/releases/tag/v0.1.1-preview.4",installMode:"in-app",percent:0,transferred:0,total:0,error:"",checkedAt:Date.now()};
  const host=document.createElement("div");document.body.append(host);const root=createRoot(host);
  const update=async(patch:Partial<UpdateState>)=>act(async()=>emit({...base,...patch}));
  const button=(label:string)=>[...host.querySelectorAll<HTMLButtonElement>("button")].find(b=>b.textContent===label);
  const click=async(label:string)=>{const target=button(label);assert.ok(target,label);await act(async()=>target.click());};
  const confirms:string[]=[];window.confirm=(text?:string)=>{confirms.push(String(text));return false;};
  try {
    await act(async()=>root.render(<><HeaderTools/><AboutSettings/></>));
    await update({});
    assert.ok((host.textContent || "").includes("VARIANT-1 0.1.1-preview.4 is available"));
    assert.match(host.textContent || "",/Nothing is downloaded until you choose to/);
    assert.equal(calls.length,0,"finding an update downloads nothing");
    await click("Update available");
    assert.deepEqual([getAppState().view,getAppState().settingsCategory],["settings","about"],"the header pill opens Settings › About");
    await click("Download update");assert.deepEqual(calls,["download"]);

    await update({status:"downloading",percent:40,transferred:41943040,total:104857600});
    assert.equal(host.querySelector<HTMLProgressElement>(".about-update__progress")?.value,40);
    assert.match(host.textContent || "",/40\.0 MB of 100 MB/);
    assert.ok(button("Update 40%"));
    await click("Cancel");assert.deepEqual(calls,["download","cancel"]);

    await update({status:"downloaded",percent:100});
    assert.ok(button("Update ready"));
    await click("Restart and install");
    assert.deepEqual(calls.at(-1),"install","nothing running: install without a prompt");assert.equal(confirms.length,0);
    // A running task makes the restart ask first.
    await act(async()=>turnController.begin({sessionId:"A",admissionId:"a1",runId:"r1"}));
    calls.length=0;await click("Restart and install");
    assert.match(confirms[0],/1 running task will stop/);assert.deepEqual(calls,[],"declining keeps the app running");

    await update({installMode:"release-page",platform:"darwin"});
    assert.match(host.textContent || "",/isn't signed by Apple/);
    assert.equal(button("Download update"),undefined);
    await click("Open release page");assert.deepEqual(calls,["release"]);

    await update({status:"error",error:"net::ERR_INTERNET_DISCONNECTED"});
    assert.match(host.textContent || "",/ERR_INTERNET_DISCONNECTED/);assert.ok(button("Try again"));
    await update({status:"unavailable",reason:"dev_mode",version:""});
    assert.equal(host.querySelector(".about-card--update"),null);
    assert.match(host.textContent || "",/Development build/);
    assert.equal(host.querySelector(".titlebar-thread-tools__update"),null);
  } finally {
    await act(async()=>root.unmount());host.remove();__resetTurnStoreForTests();
  }
  console.log("About updates: detect-only card, header pill, button-only download/cancel/install, running-work confirmation, unsigned macOS release page and dev builds passed");
}
