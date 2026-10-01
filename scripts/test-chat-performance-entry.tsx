import assert from "node:assert/strict";
import {act,Profiler} from "react";
import {createRoot} from "react-dom/client";
import {ChatComposer} from "../frontend/main-deck/src/chat/ChatComposer";
import {ComposerGoalPanel} from "../frontend/main-deck/src/chat/ComposerGoalPanel";
import {InputQueuePanel} from "../frontend/main-deck/src/chat/InputQueuePanel";
import {AgentTeamPanel} from "../frontend/main-deck/src/chat/AgentTeamPanel";
import {TurnActivity} from "../frontend/main-deck/src/chat/TurnActivity";
import {ChatMessageList} from "../frontend/main-deck/src/chat/ChatMessageList";
import {initialChatState,setChatState,patchChatState,setChatContext,getChatState} from "../frontend/main-deck/src/chat/stateCore";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {noteDisplayedSession,__resetSessionStoreForTests} from "../frontend/main-deck/src/state/sessionStore";
import type {ChatTurnStep} from "../frontend/main-deck/src/chat/types";

export async function run() {
  __resetChatStoreForTests();__resetSessionStoreForTests();
  setChatContext({send:()=>true,isOpen:()=>true,notify(){}});
  const messages=Array.from({length:100},(_,index)=>({role:index%2?"assistant" as const:"user" as const,text:`Historical ${index}`,ts:index+1}));
  setChatState({...initialChatState(),sessionId:"performance",connected:true,messages,turnActive:true,streaming:true,activeTurnId:"current",streamText:"Current"});noteDisplayedSession("performance");
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  const commits:Record<string,number>={};
  const track=(id:string)=>{commits[id]=(commits[id]||0)+1;};
  const components=[['composer',<ChatComposer/>],['goal',<ComposerGoalPanel/>],['queue',<InputQueuePanel/>],['team',<AgentTeamPanel/>],['transcript',<ChatMessageList/>]] as const;
  try {
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
