import assert from "node:assert/strict";
import {Session} from "node:inspector/promises";
import {pathToFileURL} from "node:url";
import {act,Profiler} from "react";
import {createRoot} from "react-dom/client";
import {DeckApp} from "../frontend/main-deck/src/DeckApp";
import {closeUpdatesModal, openUpdatesModal, setAboutContext} from "../frontend/main-deck/src/aboutStore";
import type {UpdateState} from "../frontend/main-deck/src/types";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {ComposerGoalStatus} from "../frontend/main-deck/src/chat/ComposerGoal";
import {InputQueuePanel} from "../frontend/main-deck/src/chat/InputQueuePanel";
import {AgentTeamPanel} from "../frontend/main-deck/src/chat/AgentTeamPanel";
import {TurnActivity} from "../frontend/main-deck/src/chat/TurnActivity";
import {ChatMessageList} from "../frontend/main-deck/src/chat/ChatMessageList";
import {initialChatState,setChatState,patchChatState,setChatContext,getChatState} from "../frontend/main-deck/src/chat/stateCore";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {noteDisplayedSession,__resetSessionStoreForTests} from "../frontend/main-deck/src/state/sessionStore";
import type {ChatTurnStep} from "../frontend/main-deck/src/chat/types";

// A Profiler surrounding the shell also sees legitimate transcript commits.
// V8's in-process function counts distinguish an actual rerender of each component.
async function checkFullDeck(root: ReturnType<typeof createRoot>, host: HTMLElement) {
  const inspector = new Session();
  async function renders() {
    const {result} = await inspector.post("Profiler.takePreciseCoverage");
    const bundle = result.find(script => script.url === pathToFileURL(__filename).href);
    assert.ok(bundle, "expected the assembled Deck fixture in the coverage sample");
    const counts = {DeckApp:0, Workbench:0, ChatComposer:0, ChatMessageList:0};
    for (const fn of bundle.functions) {
      if (Object.hasOwn(counts, fn.functionName)) counts[fn.functionName as keyof typeof counts] += fn.ranges[0].count;
    }
    return counts;
  }
  inspector.connect();
  try {
    await inspector.post("Profiler.enable");
    await inspector.post("Profiler.startPreciseCoverage", {callCount:true, detailed:false});
    await act(async () => root.render(<DeckApp api={null}/>));
    const mounted = await renders();
    for (const [name,count] of Object.entries(mounted)) assert.ok(count > 0, `missing render counter for ${name}`);
    let update!: (state:UpdateState)=>void;
    setAboutContext({send:()=>true,notify(){},api:{onUpdateState:callback=>{update=callback;return ()=>{};}}});
    await act(async () => openUpdatesModal());
    assert.ok(host.querySelector("dialog.updates-overlay[open]"), "the full Deck opens Updates during a live turn");
    await act(async()=>update({status:"downloading",reason:"",currentVersion:"1.0",platform:"win32",version:"1.1",
      releaseName:"Fixture",releaseUrl:"",installMode:"in-app",percent:40,transferred:40,total:100,error:"",checkedAt:1}));
    assert.equal(host.querySelector(".updates-overlay .about-update__percent")?.textContent,"40%");
    await renders();
    for (let i=1; i<=5; i++) await act(async () => patchChatState({streamText:`Visible stream ${i}`}));
    const transcriptWindow = host.querySelector(".chat-turn--live")!.ownerDocument.defaultView!;
    await act(async () => { await new Promise(resolve => transcriptWindow.requestAnimationFrame(resolve)); });
    const counts = await renders();
    console.log("Five full Deck stream updates", JSON.stringify(counts));
    assert.equal(counts.DeckApp, 0, "stream text must not rerender the shell");
    assert.equal(counts.Workbench, 0);
    assert.equal(counts.ChatComposer, 0);
    assert.ok(counts.ChatMessageList > 0);
    assert.match(host.querySelector(".chat-turn--live")?.textContent || "", /Visible stream 5/);
  } finally {
    await act(async () => closeUpdatesModal());
    setAboutContext({send:()=>false,notify(){},api:null});
    inspector.disconnect();
    await act(async () => root.render(null));
  }
}

export async function run() {
  __resetChatStoreForTests();__resetSessionStoreForTests();
  setChatContext({send:()=>true,isOpen:()=>true,notify(){}});
  const messages=Array.from({length:100},(_,index)=>({role:index%2?"assistant" as const:"user" as const,text:`Historical ${index}`,ts:index+1}));
  setChatState({...initialChatState(),sessionId:"performance",connected:true,messages,turnActive:true,streaming:true,activeTurnId:"current",streamText:"Current"});noteDisplayedSession("performance");
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  const commits:Record<string,number>={};
  const track=(id:string)=>{commits[id]=(commits[id]||0)+1;};
  const components=[['composer',<ChatComposer/>],['goal',<ComposerGoalStatus/>],['queue',<InputQueuePanel/>],['team',<AgentTeamPanel/>],['transcript',<ChatMessageList/>]] as const;
  try {
    await checkFullDeck(root,host);
    await act(async()=>root.render(<>{components.map(([id,element])=><Profiler key={id} id={id} onRender={()=>track(id)}>{element}</Profiler>)}</>));
    for(const id of Object.keys(commits))commits[id]=0;
    for(let index=0;index<20;index++)await act(async()=>patchChatState({streamText:`Current ${index}`}));
    const baseline={...commits};
    await act(async()=>patchChatState({draft:"Relevant composer update",connected:false}));
    assert.ok(commits.composer>baseline.composer && commits.goal>baseline.goal && commits.queue>baseline.queue && commits.team>baseline.team,'selectors still publish relevant state changes');
    assert.equal(host.querySelector<HTMLTextAreaElement>('.composer textarea')!.value,'Relevant composer update');
    await act(async()=>patchChatState({connected:true}));
    let clocks=0;
    const originalSet=window.setInterval,originalClear=window.clearInterval;
    window.setInterval=((...args:Parameters<typeof window.setInterval>)=>{clocks++;return originalSet(...args);}) as typeof window.setInterval;
    try {
      const steps:ChatTurnStep[]=Array.from({length:40},(_,index)=>({id:`step-${index}`,kind:"step",label:`Step ${index}`,status:"running",ts:Date.now()}));
      await act(async()=>root.render(<TurnActivity steps={steps} live streamText="" turnStartedAt={Date.now()} scope="clock"/>));
      await act(async()=>root.render(<div/>));
      console.log(JSON.stringify({streaming_commits:baseline,elapsed_intervals_for_40_rows:clocks}));
      assert.equal(baseline.composer,0);assert.equal(baseline.goal,0);assert.equal(baseline.queue,0);assert.equal(baseline.team,0);
      assert.equal(clocks,1);
    } finally {window.setInterval=originalSet;window.clearInterval=originalClear;}
    assert.equal(getChatState().messages.length,100);
    const revoke=URL.revokeObjectURL,removed:string[]=[];
    URL.revokeObjectURL=value=>removed.push(value);
    try {
      setChatState({...getChatState(),attachments:[{kind:"image",name:"fixture.png",previewUrl:"blob:performance-owned",size:1,mime:"image/png"}]});
      for(let index=0;index<20;index++)patchChatState({streamText:`More ${index}`});
      assert.equal(removed.length,0,'stream updates do not revoke retained attachments');
      patchChatState({attachments:[]});assert.deepEqual(removed,['blob:performance-owned']);
    } finally {URL.revokeObjectURL=revoke;}
  } finally {await act(async()=>root.unmount());host.remove();__resetChatStoreForTests();}
}
