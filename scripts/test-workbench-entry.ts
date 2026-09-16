import assert from "node:assert/strict";
import {allPaneIds,findGroupOfPane,group,split,isLayoutNode,insertAtGroup} from "../frontend/main-deck/src/workbench/layoutModel";
import {allocateTracks, childTracks, COLLAPSED_TRACK, paneInGrid, paneSide, trackFor, type TrackContext} from "../frontend/main-deck/src/workbench/trackModel";
import {
  __resetWorkbenchForTests, applyWorkbenchPreset, closePane, getWorkbenchState,
  isWorkbenchPaneVisible, revealPane, setWorkbenchCompact,
  canCloseWorkbenchGroup,closeWorkbenchGroup,setWorkbenchSplitWeights,flushWorkbenchLayout,
  saveWorkbenchPreset,
  resetWorkbenchLayout,
} from "../frontend/main-deck/src/workbench/workbenchStore";
import {collapseHistory, navigateTo, revealHistory} from "../frontend/main-deck/src/state/appStore";
import {
  beginFileEdit, cancelFileEdit, forgetFileDocument, getFileDocument, isFileDocumentDirty,
  loadFileDocument, saveFileDocument, updateFileDraft,
} from "../frontend/main-deck/src/workbench/fileDocumentStore";
import {closeOtherPreviews, closePreview, getPreviewState, openFilePreview, reopenPreview,openBrowser} from "../frontend/main-deck/src/workbench/previewStore";
import {noteDisplayedSession,ingestSessions,setSessionContext,deleteSession} from "../frontend/main-deck/src/state/sessionStore";
import {openChatView,getChatViews} from "../frontend/main-deck/src/workbench/chatViewStore";
import {getTerminalSnapshot,ingestTerminal,disposeTerminalRuntime,setTerminalContext,setTerminalConnection} from "../frontend/main-deck/src/context/terminalStore";

function testSizingAndCompactNavigation() {
  __resetWorkbenchForTests();
  const state = getWorkbenchState();
  const context: TrackContext = {hidden: state.hidden, known: new Set(allPaneIds(state.layout)), compact: false, height: 720};
  assert.equal(state.layout.type, "split");
  if (state.layout.type !== "split") throw new Error("Expected default split");
  const tracks = childTracks(state.layout, "row", context);
  const initial = allocateTracks(tracks.map(item => item.track), 1278, tracks.map(item => state.layout.type === "split" ? state.layout.weights[item.index] : 1));
  const wide = allocateTracks(tracks.map(item => item.track), 1678, [1, 3.4, 1.25]);
  assert.equal(initial[0], wide[0], "widening the window must not widen the history rail");
  assert.equal(initial[2], wide[2], "widening the window must not widen the file rail");
  assert.equal(wide[1] - initial[1], 400, "the conversation absorbs additional space");

  const column = split("column", [group(["files"]), group(["terminal"], {minimized: true})]);
  const columnTracks = childTracks(column, "column", context);
  const heights = allocateTracks(columnTracks.map(item => item.track), 600, [1.6, 1]);
  assert.equal(heights[1], COLLAPSED_TRACK, "the minimized wrapper must be only a tab strip");
  assert.equal(heights[0] + heights[1], 600, "the neighbor receives the released height");
  const constrained = allocateTracks([360, 280, 280].map(min => ({min, max: Infinity, preferred: null})), 600, [1, 1, 1]);
  assert.deepEqual(constrained, [360, 280, 280], "constrained panes scroll rather than shrink below declared minima");

  const previewId = "preview:file:sample";
  context.known = new Set([...context.known, previewId]);
  const previewColumn = split("column", [group([previewId]), group(["terminal"], {minimized: true})]);
  assert.equal(trackFor(previewColumn, "row", context)?.preferred, null,
    "a minimized terminal cannot turn its flexible preview neighbor into a thin fixed column");

  setWorkbenchCompact(true);
  assert.equal(paneSide(getWorkbenchState().layout, "history"), "left");
  for (const preset of ["default", "focus", "terminal-deck", "quad"]) {
    applyWorkbenchPreset(preset);
    collapseHistory();
    revealHistory();
    const compact = getWorkbenchState();
    const compactContext = {...context, compact: true, hidden: compact.hidden};
    assert.equal(compact.overlayPaneId, "history");
    assert.equal(compact.hidden.history, false);
    assert.equal(paneInGrid("workspace", compactContext), true, "history toggles cannot remove Chat from the grid");
    assert.equal(paneInGrid("history", compactContext), false, "History is an overlay in compact mode");
    assert.equal(isWorkbenchPaneVisible("history"), true);
    revealPane("files");
    assert.equal(getWorkbenchState().overlayPaneId, "files", "only the requested side pane is revealed");
  }
  setWorkbenchCompact(false);
  assert.equal(getWorkbenchState().overlayPaneId, null);
}

async function testDraftLifetimeAndWrites() {
  const id = openFilePreview("C:\\review-fixture\\draft.txt", "draft.txt");
  const read = async () => ({ok: true, text: "Disk version", mtimeMs: 10});
  await loadFileDocument(id, "sample", read);
  beginFileEdit(id);
  updateFileDraft(id, "Unsaved working draft");
  navigateTo("memory");
  navigateTo("chat");
  await loadFileDocument(id, "sample", read);
  assert.equal(getFileDocument(id)?.text, "Unsaved working draft");
  assert.equal(getFileDocument(id)?.editing, true);
  assert.equal(isFileDocumentDirty(id), true);

  await loadFileDocument(id, "sample", async () => ({ok: true, text: "External edit", mtimeMs: 20}));
  assert.equal(getFileDocument(id)?.text, "Unsaved working draft");
  assert.equal(getFileDocument(id)?.mtimeMs, 10, "the original version remains the write fence");
  assert.equal(getFileDocument(id)?.conflict, true);
  await saveFileDocument(id, "sample", async (_path, text, mtime) => {
    assert.equal(text, "Unsaved working draft"); assert.equal(mtime, 10);
    return {ok: false, error: "Disk unavailable"};
  });
  assert.equal(getFileDocument(id)?.editing, true);
  assert.equal(getFileDocument(id)?.saving, false);
  assert.equal(getFileDocument(id)?.error, "Disk unavailable");
  assert.equal(getFileDocument(id)?.text, "Unsaved working draft", "failed saves retain the retryable buffer");

  window.confirm = () => false;
  assert.equal(closePreview(id), false);
  revealPane(`preview:${id}`);
  closePane(`preview:${id}`);
  assert.ok(getPreviewState().tabs.some(tab => tab.id === id), "keyboard/middle-close must use the same dirty guard");
  revealPane(`preview:${id}`);
  const groupId=findGroupOfPane(getWorkbenchState().layout,`preview:${id}`)!.id;
  assert.equal(canCloseWorkbenchGroup(groupId),false,"detached-window close checks unsaved previews before closing");
  closeWorkbenchGroup(groupId);
  assert.ok(getPreviewState().tabs.some(tab=>tab.id===id),"cancelled group close preserves its unsaved tab");
  const clean = openFilePreview("C:\\review-fixture\\clean.txt", "clean.txt");
  assert.equal(closeOtherPreviews(clean), false, "bulk close cannot silently discard another tab's edit");
  assert.ok(getPreviewState().tabs.some(tab => tab.id === id));
  assert.ok(!window.localStorage.getItem("variant1.workbench.preview-tabs.v1")?.includes("Unsaved working draft"),
    "tab persistence must not write file buffers to localStorage");

  let settle!: (value: {ok: boolean; mtimeMs: number}) => void;
  const saving = saveFileDocument(id, "sample", (_path, _text, mtime) => {
    assert.equal(mtime, undefined, "explicit overwrite omits the version fence");
    return new Promise(resolve => { settle = resolve; });
  }, true);
  assert.equal(getFileDocument(id)?.saving, true);
  window.confirm = () => true;
  assert.equal(closePreview(id), false, "an in-flight save cannot lose its owner");
  settle({ok: true, mtimeMs: 21});
  assert.equal(await saving, true);
  assert.equal(isFileDocumentDirty(id), false);
  assert.equal(getFileDocument(id)?.editing, false);
  assert.equal(closePreview(id), true);
  assert.equal(getFileDocument(id), undefined, "closing releases the buffer");
  assert.equal(reopenPreview(id), id);
  assert.equal(getFileDocument(id), undefined, "reopening starts from disk, not a discarded draft");
  closePreview(id); closePreview(clean);
}

async function testReadRaces() {
  const id = "read-race";
  await loadFileDocument(id, "sample", async () => ({ok: true, text: "Original", mtimeMs: 1}));
  let resolveRead!: (value: {ok: boolean; text: string; mtimeMs: number}) => void;
  const pending = loadFileDocument(id, "sample", () => new Promise(resolve => { resolveRead = resolve; }));
  beginFileEdit(id); updateFileDraft(id, "Typed after read began");
  resolveRead({ok: true, text: "Read result", mtimeMs: 2});
  await pending;
  assert.equal(getFileDocument(id)?.text, "Typed after read began");
  cancelFileEdit(id);
  assert.equal(getFileDocument(id)?.text, "Original");
  const stale = loadFileDocument(id, "sample", () => new Promise(resolve => { resolveRead = resolve; }));
  forgetFileDocument(id);
  await loadFileDocument(id, "sample", async () => ({ok: true, text: "Reopened", mtimeMs: 3}));
  resolveRead({ok: true, text: "Old tab result", mtimeMs: 2});
  await stale;
  assert.equal(getFileDocument(id)?.text, "Reopened", "a closed tab's late read cannot update its replacement");
  forgetFileDocument(id);
}

export async function run() {
  testSizingAndCompactNavigation();
  await testDraftLifetimeAndWrites();
  await testReadRaces();
  let writes = 0;
  const write = async () => { writes++; return {ok: true}; };
  await loadFileDocument("binary", "sample.db", async () => ({ok: true, binary: true, text: "", mtimeMs: 10}));
  beginFileEdit("binary"); updateFileDraft("binary", "replacement");
  assert.equal(getFileDocument("binary")?.kind, "binary");
  assert.equal(getFileDocument("binary")?.editing, false);
  assert.equal(await saveFileDocument("binary", "sample.db", write, true), false);
  assert.equal(writes, 0, "binary reads cannot call the text writer, including forced saves");
  await loadFileDocument("empty", "empty.txt", async () => ({ok: true, binary: false, text: "", mtimeMs: 10}));
  beginFileEdit("empty"); updateFileDraft("empty", "A real empty text file");
  assert.equal(await saveFileDocument("empty", "empty.txt", write), true);
  assert.equal(writes, 1);
  beginFileEdit("empty"); updateFileDraft("empty", "Keep this draft");
  await loadFileDocument("empty", "empty.txt", async () => ({ok: true, binary: true, text: "", mtimeMs: 11}));
  assert.equal(getFileDocument("empty")?.text, "Keep this draft");
  assert.equal(await saveFileDocument("empty", "empty.txt", write, true), false);
  assert.equal(writes, 1, "replacement with binary bytes revokes text save eligibility");
  forgetFileDocument("binary"); forgetFileDocument("empty");
  __resetWorkbenchForTests();
  const layout=getWorkbenchState().layout;
  if(layout.type!=="split")throw new Error("Expected a resizeable root");
  const originalSet=window.localStorage.setItem;let layoutWrites=0;
  window.localStorage.setItem=(key,value)=>{layoutWrites++;originalSet(key,value);};
  for(let i=0;i<100;i++)setWorkbenchSplitWeights(layout.id,[1,3,1+i/100],undefined,true);
  assert.equal(layoutWrites,0,"drag updates must not synchronously write storage for every movement");
  flushWorkbenchLayout();assert.equal(layoutWrites,3,"settled resize persists the three layout records once");
  window.localStorage.setItem=originalSet;
  noteDisplayedSession("preset-A");applyWorkbenchPreset("default");revealPane("files");
  const saved=await saveWorkbenchPreset("Reusable layout");assert.ok(saved);
  const presets=JSON.parse(window.localStorage.getItem("variant1.workbench.presets.v1")!);
  assert.ok(!JSON.stringify(presets[saved!]).includes("owned:"),"new presets contain resource kinds, not chat ownership");
  noteDisplayedSession("preset-B");assert.equal(applyWorkbenchPreset(saved!),true);
  assert.ok(allPaneIds(getWorkbenchState().layout).includes("owned:files:preset-B"));
  assert.ok(!allPaneIds(getWorkbenchState().layout).some(id=>id.includes("preset-A")));
  assert.equal(getWorkbenchState().hidden["owned:files:preset-B"],false);
  const browser=openBrowser("https://example.com",{ownerChatId:"preset-B"});revealPane(`preview:${browser}`);
  openChatView("view-C","C","right");
  const legacy=split("row",[group(["workspace"],{id:"legacy-chat"}),group(["owned:files:preset-A"],{id:"legacy-files"}),group([`preview:${browser}`,"chatview:view-C"],{id:"legacy-previews"})]);
  window.localStorage.setItem("variant1.workbench.presets.v1",JSON.stringify({legacy:{name:"Legacy",layout:legacy,hidden:{"owned:files:preset-A":false,"right:preset-A":true},windows:[]}}));
  assert.equal(applyWorkbenchPreset("legacy"),true);
  const ids=allPaneIds(getWorkbenchState().layout);
  assert.equal(ids.length,new Set(ids).size,"legacy live tabs are never inserted twice");
  assert.ok(ids.includes(`preview:${browser}`));assert.ok(ids.includes("chatview:view-C"));
  assert.equal(getWorkbenchState().hidden["right:preset-B"],true);
  resetWorkbenchLayout();
  const resetIds=allPaneIds(getWorkbenchState().layout);
  for(const kind of ["files","review","terminal"])assert.ok(resetIds.includes(`owned:${kind}:preset-B`),"reset keeps every resource assigned to the selected chat");
  assert.ok(!resetIds.some(id=>["files","review","terminal"].includes(id)),"reset does not leave orphan unowned resources");
  assert.ok(resetIds.includes(`preview:${browser}`) && resetIds.includes("chatview:view-C"),"reset preserves live browser and chat views");
  assert.deepEqual(getWorkbenchState().hidden,{files:true,review:true,terminal:true});
  assert.equal(isLayoutNode(split("row",[group(["duplicate"]),group(["duplicate"])])),false);
  const unique=split("row",[group(["one"],{id:"one"}),group(["two"],{id:"two"})]);
  assert.equal(allPaneIds(insertAtGroup(unique,"two","one","center")!).filter(id=>id==="one").length,1);
  const chatGroup=findGroupOfPane(getWorkbenchState().layout,"chatview:view-C")!;
  closeWorkbenchGroup(chatGroup.id,true);
  assert.ok(!allPaneIds(getWorkbenchState().layout).includes("chatview:view-C"),"group close unmounts the persistent iframe");
  assert.ok(!getChatViews().some(row=>row.id==="view-C"),"closed chat descriptors are removed");

  const survivor=openBrowser("https://example.org",{ownerChatId:"survivor"});
  openChatView("preset-B","Deleted chat","right");revealPane("terminal");
  setTerminalContext({send:()=>true,notify(){}});setTerminalConnection("connected");
  ingestTerminal({type:"execution:snapshot",chat_id:"preset-B",terminals:[{id:"deleted-terminal",state:"running"}],processes:[]});
  ingestTerminal({type:"terminal:accepted",chat_id:"preset-B",operation:"read",result:{entity:{id:"deleted-terminal",kind:"terminal"},frames:[{text:"retained output"}],next_cursor:1}});
  assert.equal(getTerminalSnapshot("preset-B").output,"retained output");
  setSessionContext({send:()=>true,notify(){}});
  assert.equal(deleteSession("preset-B"),true);
  assert.ok(getPreviewState().tabs.some(tab=>tab.id===browser),"delete dispatch does not close surfaces before success");
  ingestSessions({type:"chat:session:deleted",session_id:"preset-B"});
  assert.ok(!getPreviewState().tabs.some(tab=>tab.ownerChatId==="preset-B"));
  assert.ok(getPreviewState().tabs.some(tab=>tab.id===survivor));
  assert.equal(reopenPreview(browser),null,"deleted browser cannot return from reopen history");
  assert.ok(!allPaneIds(getWorkbenchState().layout).some(id=>id.includes("preset-B") || id===`preview:${browser}`));
  assert.equal(getTerminalSnapshot("preset-B").output,"");
  ingestTerminal({type:"execution:snapshot",chat_id:"preset-B",terminals:[{id:"late-terminal",state:"running"}]});
  assert.equal(getTerminalSnapshot("preset-B").terminals.length,0,"late events cannot recreate deleted runtimes");
  disposeTerminalRuntime();
  console.log("workbench behavior: fixed/minimized tracks, compact pane identity, draft lifetime, guarded close, write failures, and read races passed");
}
