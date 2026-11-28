import {ingestTerminal, setTerminalConnection, setTerminalContext, disposeTerminalRuntime} from "../frontend/main-deck/src/context/terminalStore";
import {ingestChatProjects} from "../frontend/main-deck/src/state/chatProjectStore";
import {getSessionState, ingestSessions, noteDisplayedSession, openHistoryMatch, setSessionContext, setSessionSearchQuery} from "../frontend/main-deck/src/state/sessionStore";
import {getChatState, initialChatState, setChatState} from "../frontend/main-deck/src/chat/stateCore";
import {applySession} from "../frontend/main-deck/src/chat/session";
import {runWorkbenchBrowserCommand, waitForWorkbenchBrowser} from "../frontend/main-deck/src/workbench/browserBridge";
import {closePreview, getPreviewState, openBrowser, openFilePreview, openPreview, requestBrowserNavigation, setBrowserViewport} from "../frontend/main-deck/src/workbench/previewStore";
import {chatPaneId, detachWorkbenchPane, getWorkbenchState, revealPane, revealPreviewPane, setWorkbenchEditMode} from "../frontend/main-deck/src/workbench/workbenchStore";
import {dockAllNativeWindows} from "../frontend/main-deck/src/workbench/nativeWindowStore";

const pause = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
type FixtureControls = {hold():void; held():boolean; release():void; lastExternal():string; snapshot(name:string):Promise<void>};
async function until<T>(read: () => T, label: string): Promise<NonNullable<T>> {
  for (let i = 0; i < 200; i++) {
    const result = read();
    if (result) return result;
    await pause(25);
  }
  throw new Error("Timed out waiting for " + label);
}

export async function checkWorkbench(base: string) {
  const a = "workbench-A", b = "workbench-B", root = "C:/Example/Variant";
  const list = {type:"chat:sessions", items:[{id:a,title:a,project:{root,name:"Variant"}},{id:b,title:b}]};
  ingestSessions(list); ingestChatProjects(list); noteDisplayedSession(a);
  const controls = (window as Window & {browserFixture: FixtureControls}).browserFixture;
  const closes: Array<Record<string, unknown>> = [];
  setTerminalContext({notify(){}, send(command) {
    if (command.type === "terminal:close") closes.push(command);
    return true;
  }});
  setTerminalConnection("connected");
  for (const chat_id of [a,b]) ingestTerminal({type:"execution:snapshot", chat_id, terminals:[{id:"pty-"+chat_id,state:"running"}]});
  const filesId = chatPaneId("files",a);
  revealPane(filesId);
  const row = (name:string) => [...document.querySelectorAll(`[data-pane-id="${filesId}"] .workbench-file-row`)].find(el=>el.querySelector(".workbench-file-row__name")?.textContent === name);
  await until(()=>row("changed.ts")?.querySelector(".workbench-file-row__git")?.textContent === "A","subproject status");
  if (row("README.md")?.querySelector(".workbench-file-row__git")) throw new Error("The root README badge leaked onto the subproject copy");

  for (const name of ["backend/tests/test_pdf_jp_kr.py","htmlParser.ts","pdf/source.ts","document.pdf","page.htm"]) {
    const path = root + "/" + name, html = name.endsWith(".htm");
    const id = html ? openPreview({kind:"file",source:path,path,url:path,label:name,renderMode:"preview"},{ownerChatId:a}) : openFilePreview(path,undefined,a);
    await until(() => {
      const pane = [...document.querySelectorAll(".workbench-file-preview")].find(el=>el.querySelector("header span")?.getAttribute("title") === path);
      if (name.endsWith(".pdf")) return pane?.querySelector('iframe[aria-label^="PDF preview"]');
      if (html) return pane?.querySelector("iframe[srcdoc]");
      return [...pane?.querySelectorAll("button") || []].some(button=>button.textContent === "Edit");
    },name+" viewer");
    closePreview(id);
  }

  const id = openBrowser("about:blank",{ownerChatId:a,newTab:true});
  setBrowserViewport(id,{width:800,height:480});
  if (!await waitForWorkbenchBrowser(id,5000)) throw new Error("Blank browser did not open");
  const first = base + "/workbench-page", url = first + "#linked";
  requestBrowserNavigation(id,first);
  const tab = () => getPreviewState().tabs.find(item=>item.id === id);
  await until(()=>tab()?.committedUrl === first,"first committed URL");
  await runWorkbenchBrowserCommand({action:"evaluate",tab_id:id,expression:"document.querySelector('a[href=\"#linked\"]').click();true"});
  await until(()=>tab()?.committedUrl === url,"linked URL");
  const tabButton = await until(()=>document.querySelector<HTMLElement>(`[data-pane-tab="preview:${id}"]`),"browser tab menu target");
  tabButton.dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,cancelable:true,clientX:80,clientY:100}));
  const external = await until(()=>[...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(el=>el.textContent === "Open external" && !el.disabled),"Open external");
  external.click();
  await until(()=>controls.lastExternal() === url,"committed external URL");

  const key = (owner: Document) => owner.dispatchEvent(new KeyboardEvent("keydown",{key:"W",ctrlKey:true,shiftKey:true,bubbles:true,cancelable:true}));
  noteDisplayedSession(b);
  await pause(100);
  key(document);
  await until(()=>closes.some(c=>c.chat_id === b && c.terminal_id === "pty-"+b),"foreground B shortcut");
  if (closes.length !== 1) throw new Error("Shortcut closed an extra terminal");
  noteDisplayedSession(a);
  const terminal = chatPaneId("terminal",a);
  revealPane(terminal,"bottom");
  const mount = await until(()=>document.querySelector(`[data-pane-id="${terminal}"]`)?.closest(".native-surface-mount"),"terminal mount");
  detachWorkbenchPane(terminal);
  await until(()=>mount.ownerDocument !== document,"detached terminal");
  noteDisplayedSession(b);
  await pause(100);
  key(mount.ownerDocument);
  await until(()=>closes.length === 2 && closes[1].chat_id === a && closes[1].terminal_id === "pty-"+a,"detached A shortcut");
  dockAllNativeWindows();
  await pause(100);
  disposeTerminalRuntime();

  for (const detached of [false,true]) {
    noteDisplayedSession(a); revealPreviewPane(id);
    const pane = await until(()=>document.querySelector(`[data-pane-id="preview:${id}"]`)?.closest(".native-surface-mount"),"browser capture mount");
    if (detached) { detachWorkbenchPane("preview:"+id); await until(()=>pane.ownerDocument !== document,"detached browser"); }
    const capture = await until(()=>pane.querySelector<HTMLButtonElement>('.is-active [aria-label="Capture screenshot"]'),"capture button");
    controls.hold(); capture.click();
    await until(()=>controls.held(),"native capture response");
    noteDisplayedSession(b);
    await pause(100);
    controls.release();
    const output = await until(()=>getPreviewState().tabs.find(t=>t.target.source === "browser:"+id+":screenshot"),"screenshot output");
    if (output.ownerChatId !== a) throw new Error("Screenshot followed the displayed chat");
    closePreview(output.id);
    if (detached) dockAllNativeWindows();
    await pause(75);
  }
  noteDisplayedSession(a);
  await checkSearchAndPresets(a,controls);
  return {id,url};
}

async function checkSearchAndPresets(chatId: string, controls: FixtureControls) {
  setWorkbenchEditMode(true);
  const select = await until(()=>document.querySelector<HTMLSelectElement>('[aria-label="Layout preset"]'),"layout preset selector");
  const previous = JSON.stringify(getWorkbenchState().layout);
  for (const choice of ["focus","quad"]) {
    select.value = choice;
    select.dispatchEvent(new Event("change",{bubbles:true}));
    await until(()=>document.querySelector(".layout-preview")?.getAttribute("aria-label")?.toLowerCase().startsWith(choice),"labelled "+choice+" preview");
    if (JSON.stringify(getWorkbenchState().layout) !== previous) throw new Error("Preview moved the workspace");
    for (const name of ["Chat","Files","Review","Terminal"]) {
      if (!document.querySelector(".layout-preview")?.textContent?.includes(name)) throw new Error("Preview omitted "+name);
    }
  }
  await controls.snapshot("layout-quad");
  const apply = [...document.querySelectorAll<HTMLButtonElement>(".workbench-editbar button")].find(el=>el.textContent === "Apply layout")!;
  apply.click();
  await until(()=>JSON.stringify(getWorkbenchState().layout) !== previous,"explicit layout application");
  setWorkbenchEditMode(false);
  revealPane("history","left");
  const messages = Array.from({length:240}, (_, i)=>({nodeId:"node-"+i,role:i%2 ? "assistant" as const : "user" as const,
    text:i === 12 || i === 161 ? "Find this **exact** passage "+i : "Fixture message "+i,ts:1_700_000_000+i}));
  messages[10] = {...messages[12],nodeId:"node-10"}; // Same role, timestamp and passage; the node identity distinguishes them.
  const reads: Record<string,unknown>[] = [];
  setSessionContext({notify(){},send(command){if(command.type==="chat:history:around")reads.push(command);return true;}});
  setChatState({...initialChatState(),sessionId:chatId,messages,draft:"Unsent operator draft"});
  setSessionSearchQuery("exact passage");
  const owner = {session_id:chatId,branch_id:"branch-A",title:"Workbench",kind:"message"};
  ingestSessions({type:"chat:search:results",query:"exact passage",items:[
    ...[12,161].map(i=>({...owner,node_id:messages[i].nodeId,role:messages[i].role,ts:messages[i].ts,snippet:"Find this exact passage "+i})),
    ...["old-first","old-second"].map(node_id=>({...owner,node_id,role:"assistant",ts:2300,snippet:"Earlier exact passage"})),
    {...owner,kind:"title",role:"title",snippet:"Exact passage in conversation title"},
    {...owner,session_id:"workbench-B",branch_id:"branch-B",node_id:"node-B",role:"user",ts:169,snippet:"Exact passage in B"},
  ]});
  const hit = await until(()=>document.querySelector<HTMLButtonElement>('[data-group="Search results"] .history-item__select'),"search hit");
  hit.click();
  for (const index of [12,161]) {
    const article = await until(()=>document.querySelector<HTMLElement>('[data-message-index="'+index+'"].history-match-active'),"highlighted message "+index);
    const ranges = CSS.highlights.get("history-match");
    if (!ranges || [...ranges][0]?.toString() !== "exact passage") throw new Error("Matched passage was not highlighted across inline markup");
    const viewport = article.closest(".messages-scroll")!.getBoundingClientRect();
    const range = [...ranges][0].getBoundingClientRect();
    if (range.top < viewport.top || range.bottom > viewport.bottom) throw new Error("Matched passage is outside the viewport");
    const probe = document.createElement("span");
    probe.style.backgroundColor = "var(--deck-warning)";article.append(probe);
    const color = getComputedStyle(probe).backgroundColor;probe.remove();
    if (getComputedStyle(article,"::highlight(history-match)").backgroundColor !== color) throw new Error("Passage highlight did not use the active theme");
    const next = [...document.querySelectorAll<HTMLButtonElement>(".history-match-nav button")].find(button=>button.textContent === "Next match")!;
    if (index === 12) {await controls.snapshot("history-match");next.click();}
  }
  if (reads.length) throw new Error("Reading a loaded match contacted the history service");
  const button = (text:string) => [...document.querySelectorAll<HTMLButtonElement>(".history-match-nav button")].find(el=>el.textContent === text);
  const reply = (request:Record<string,unknown>, overrides:Record<string,unknown>={}) => ingestSessions({
    ...request,type:"chat:history:window",branch_id:owner.branch_id,has_before:true,has_after:true,
    messages:[{node_id:"goal-before",role:"user",display_kind:"goal",text:"Continuing goal",ts:2100},
      {node_id:"peer-before",role:"user",display_kind:"peer",text:"An earlier peer message",ts:2200},
      {node_id:request.node_id,role:"assistant",text:"Earlier exact passage in a long response",ts:2300,text_offset:60,text_total_chars:2000}],
    ...overrides,
  });
  openHistoryMatch(2);
  const first = await until(()=>reads[0],"first older request");
  openHistoryMatch(3);
  const second = await until(()=>reads[1],"second older request");
  reply(first);reply(second,{session_id:"workbench-B"});reply(second,{node_id:"wrong"});
  if (getSessionState().historyMatch?.requestId !== second.request_id) throw new Error("A stale, foreign-chat or wrong-node reply settled the current request");
  reply(second,{branch_id:"foreign-branch"});
  const retry = await until(()=>button("Retry"),"mismatched branch refusal");
  if (getSessionState().historyMatch?.window) throw new Error("Wrong branch opened");
  retry.click();
  const third = await until(()=>reads[2],"explicit history retry");
  button("Close search result")!.click();
  reply(third);
  if (getSessionState().historyMatch) throw new Error("Closed search was reopened by a late result");
  openHistoryMatch(2);
  const fourth = await until(()=>reads[3],"reopened older result");
  if (fourth.node_id !== first.node_id || fourth.request_id === first.request_id || fourth.query !== "exact passage") throw new Error("History retry did not use a fresh exact request");
  reply(fourth);
  await until(()=>document.querySelector('[data-message-index="2"].history-match-active .history-excerpt'),"older message and excerpt bounds");
  if (!document.querySelector('[data-message-index="0"].message--goal-turn') ||
      !document.querySelector('[data-message-index="1"].message--peer')) throw new Error("Historical Goal and peer inputs lost their origin");
  if (!document.querySelector(".history-match-nav")?.textContent?.includes("Earlier messages") || getChatState().messages !== messages ||
      getChatState().draft !== "Unsent operator draft" || getChatState().sessionId !== chatId) throw new Error("Historical view changed the live chat or composer");
  await controls.snapshot("history-older");
  button("Return to latest")!.click();
  await until(()=>document.querySelector('[data-message-index="239"]'),"latest visible message after historical view");
  openHistoryMatch(4);
  await until(()=>document.querySelector(".history-match-nav")?.textContent?.includes("Conversation title"),"title-only hit");
  if (reads.length !== 4) throw new Error("Title-only hit requested a message");
  const acceptSwitch = (id:string, rows: unknown[]) => {
    const pending = getSessionState().pendingAction;
    if (pending?.type !== "switch" || pending.id !== id) throw new Error("Search did not request the selected chat");
    applySession({id,title:id,messages:rows}, {request_id:pending.requestId,requested_id:id,effective_id:id,status:"switched"});
  };
  openHistoryMatch(5);
  if (getChatState().sessionId !== chatId || getChatState().draft !== "Unsent operator draft") throw new Error("Historical navigation changed the chat before admission");
  acceptSwitch("workbench-B",[{node_id:"node-B",role:"user",text:"Exact passage in B",ts:169}]);
  await until(()=>document.querySelector(".history-match-active")?.textContent?.includes("Exact passage in B"),"admitted cross-chat search");
  openHistoryMatch(0);
  acceptSwitch(chatId,messages.map(row=>({...row,node_id:row.nodeId})));
  await until(()=>document.querySelector('[data-message-index="12"].history-match-active'),"return to original chat hit");
  if (getChatState().draft !== "Unsent operator draft" || reads.length !== 4) throw new Error("Search lost the original draft or fetched a loaded match");
  setSessionSearchQuery("");
  const fence = String.fromCharCode(96).repeat(3);
  const block = (match: boolean) => [fence+"text", ...Array.from({length:55}, (_,i) =>
    match && i===44 ? "🗂 Caféine fixture" : "row "+(i+1)), fence].join("\n");
  const body = block(false)+"\n\n"+block(true);
  const codeMessages = (["user", "assistant"] as const).map((role,i)=>({role,nodeId:"code-"+i,text:body,ts:5000+i}));
  setChatState({...getChatState(),messages:codeMessages});
  setSessionSearchQuery("cafe");
  ingestSessions({type:"chat:search:results",query:"cafe",items:[
    ...codeMessages.map(row=>({...owner,node_id:row.nodeId,role:row.role,ts:row.ts,snippet:"…Caféine fixture…"})),
    {...owner,node_id:"code-earlier",role:"assistant",ts:4900,snippet:"…Caféine fixture…"},
  ]});
  const assertCodeMatch = async (index:number) => {
    let observed = "";
    await until(()=>{
      const article=document.querySelector<HTMLElement>('[data-message-index="'+index+'"].history-match-active');
      const range=[...(CSS.highlights.get("history-match") || [])][0];
      observed = JSON.stringify({index, article:!!article, text:range?.toString(), match:getSessionState().historyMatch?.hit.nodeId, scoped:!!article?.contains(range?.startContainer || null)});
      if (!article || !range || !article.contains(range.startContainer) || range.toString() !== "Café") return false;
      const blocks=article.querySelectorAll(".runtime-code-block__body");
      observed += " "+JSON.stringify([...blocks].map(b=>[b.className,b.getBoundingClientRect().top,b.getBoundingClientRect().bottom]));
      if (blocks.length!==2 || !blocks[0].classList.contains("is-collapsed") || blocks[1].classList.contains("is-collapsed")) return false;
      const rect=range.getBoundingClientRect(), viewport=article.closest(".messages-scroll")!.getBoundingClientRect();
      observed += " "+JSON.stringify({rect:[rect.top,rect.bottom],viewport:[viewport.top,viewport.bottom]});
      return rect.height>0 && rect.top>=viewport.top && rect.bottom<=viewport.bottom
        && rect.bottom<=blocks[1].getBoundingClientRect().bottom;
    },"visible accent-insensitive code match on line 45, with its sibling still collapsed").catch(error=>{throw new Error(String(error)+": "+observed);});
  };
  const codeHit = await until(()=>[...document.querySelectorAll<HTMLButtonElement>('[data-group="Search results"] .history-item__select')].find(item=>item.textContent?.includes("Caféine")), "current code search hit");
  codeHit.click();
  if (getSessionState().historyMatch?.hit.nodeId!=="code-0") throw new Error("Wrong code node after clicking: " + JSON.stringify(getSessionState().historyMatch?.hit));
  await assertCodeMatch(0);
  button("Next match")!.click();
  await assertCodeMatch(1);
  button("Next match")!.click();
  const older=await until(()=>reads.find(row=>row.node_id==="code-earlier"),"older code window request");
  reply(older,{messages:[{node_id:"code-earlier",role:"assistant",ts:4900,text:body,text_offset:6000,text_total_chars:10000}]});
  await assertCodeMatch(0);
  if (getChatState().messages!==codeMessages || getChatState().draft!=="Unsent operator draft") throw new Error("Opening earlier code changed the live transcript or draft");
  button("Return to latest")!.click();
  setSessionSearchQuery("");
}
