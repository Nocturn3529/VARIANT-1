import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {AboutSettings} from "../frontend/main-deck/src/AboutSettings";
import {DeckApp} from "../frontend/main-deck/src/DeckApp";
import {HeaderTools} from "../frontend/main-deck/src/shell/HeaderTools";
import {UpdatesOverlay} from "../frontend/main-deck/src/ui/UpdatesOverlay";
import {closeUpdatesModal, setAboutContext, useAboutState} from "../frontend/main-deck/src/aboutStore";
import {closeSettings, getAppState, navigateTo, selectSettingsCategory} from "../frontend/main-deck/src/state/appStore";
import {__resetTurnStoreForTests,turnController} from "../frontend/main-deck/src/state/turnStore";
import type {UpdateState} from "../frontend/main-deck/src/types";

function Probe() {
  const {updatesModalOpen, updateLogs} = useAboutState();
  return <span data-modal-open={String(updatesModalOpen)} data-log-count={String(updateLogs.length)} hidden/>;
}

/** Settings › About, the compact UpdatesOverlay modal and the header pill mirror the main process; only clicks download or install. */
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
    await act(async()=>root.render(<><HeaderTools/><UpdatesOverlay/><AboutSettings/><Probe/></>));
    await update({});
    assert.ok((host.textContent || "").includes("VARIANT-1 0.1.1-preview.4 is available"));
    assert.match(host.textContent || "",/Nothing is downloaded until you choose to/);
    assert.equal(calls.length,0,"finding an update downloads nothing");
    const viewBefore=getAppState().view;
    await click("Update available");
    assert.equal(getAppState().view,viewBefore,"the header pill opens the compact UpdatesOverlay instead of navigating Settings");
    const updatesModal=host.querySelector<HTMLDialogElement>("dialog.updates-overlay");
    assert.equal(updatesModal?.open,true,"UpdatesOverlay modal is open");
    assert.equal(updatesModal?.querySelector(".about-update__tag"),null,"available version is not repeated as a separate tag");
    assert.equal(updatesModal?.querySelector(".about-update__notes-link"),null,"Release notes action is not duplicated by a second GitHub release link");
    assert.doesNotMatch(updatesModal?.querySelector(".version-hero__channel")?.textContent || "",/0\.1\.1-preview\.4/,"compact hero badge shows build type without repeating available version");
    assert.equal(host.querySelector("[data-modal-open]")?.getAttribute("data-modal-open"),"true");
    assert.ok(Number(host.querySelector("[data-log-count]")?.getAttribute("data-log-count") || 0) >= 1,"status transition is recorded in updateLogs");
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

    // While installing, the UpdatesOverlay close button is locked and closeUpdatesModal() is a no-op.
    await update({status:"installing",percent:100});
    const closeBtn=host.querySelector<HTMLButtonElement>(".updates-overlay__close");
    assert.ok(closeBtn?.disabled,"close button is locked while installing");
    await act(async()=>closeUpdatesModal());
    assert.equal(host.querySelector<HTMLDialogElement>("dialog.updates-overlay")?.open,true,"modal stays open while installing");

    await update({installMode:"release-page",platform:"darwin"});
    assert.equal(closeBtn?.disabled,false,"close button unlocks once not installing");
    assert.match(host.textContent || "",/isn't signed by Apple/);
    assert.equal(button("Download update"),undefined);
    await click("Open release page");assert.deepEqual(calls,["release"]);

    await update({status:"error",error:"net::ERR_INTERNET_DISCONNECTED"});
    assert.match(host.textContent || "",/ERR_INTERNET_DISCONNECTED/);assert.ok(button("Try again"));
    await act(async()=>closeUpdatesModal());
    assert.equal(host.querySelector("dialog.updates-overlay"),null,"modal closes when not installing");
    await update({status:"unavailable",reason:"dev_mode",version:""});
    assert.equal(host.querySelector(".about-card--update"),null);
    assert.match(host.textContent || "",/Development build/);
    assert.equal(host.querySelector(".titlebar-thread-tools__update"),null);

    window.history.replaceState({},"","/?detached_chat=detached-updates-1");
    let handoffs=0;
    setAboutContext({send:()=>true,notify(){},api:{openDeckUpdates:async()=>{handoffs++;return {ok:true};}}});
    await act(async()=>{
      selectSettingsCategory("about"); navigateTo("settings"); root.render(<DeckApp api={null}/>);
    });
    await click("Open updates");
    assert.equal(handoffs,1,"the real About button requests the owner window");
    assert.equal(host.querySelector("dialog.updates-overlay"),null);
    assert.equal(button("Check for updates"),undefined);
  } finally {
    await act(async()=>closeSettings());
    window.history.replaceState({},"","/");
    await act(async()=>root.unmount());host.remove();__resetTurnStoreForTests();
  }
  console.log("About updates: detect-only card, header pill -> UpdatesOverlay modal, locked close while installing, button-only download/cancel/install, running-work confirmation, unsigned macOS release page and dev builds passed");
}
