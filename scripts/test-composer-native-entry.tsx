import {createRoot} from "react-dom/client";
import {useState} from "react";
import {MicGlyph,MutationSwitch} from "../frontend/main-deck/src/chat/ComposerMotion";
import {DeckApp} from "../frontend/main-deck/src/DeckApp";
import {AppearanceBindings,setAppearance} from "../frontend/main-deck/src/state/appearanceStore";
import {__resetChatStoreForTests} from "../frontend/main-deck/src/chatStore";
import {getChatState,setChatContext,setChatState} from "../frontend/main-deck/src/chat/stateCore";
import {addChatFiles} from "../frontend/main-deck/src/chat/attachments";
import {ingestChat} from "../frontend/main-deck/src/chat/ingest";
import {parseChatWsMessage} from "../frontend/main-deck/src/protocol";
import {__resetSessionStoreForTests,ingestSessions,setSessionConnection,setSessionContext} from "../frontend/main-deck/src/state/sessionStore";
import {__resetTurnStoreForTests,turnController} from "../frontend/main-deck/src/state/turnStore";
import {__resetSessionContextStoreForTests,ingestSessionContext,setSessionContextConnection,setSessionContextContext} from "../frontend/main-deck/src/sessionContextStore";

const commands:Array<Record<string,unknown>>=[];
const routes={mode:"cloud",provider:"fixture",model:"gpt-5.3-codex-spark",reasoning_effort:"xhigh"};
const models=[{id:"fixture",name:"Connected models",mode:"cloud",models:[
  {id:"gpt-5.3-codex-spark",label:"gpt-5.3 codex spark",reasoning_efforts:["low","medium","high","xhigh"]},
  {id:"reasoning-fixture",label:"Reasoning model",reasoning_efforts:["low","medium","high","xhigh","max","ultra"]},
  {id:"unavailable-fixture",label:"Unavailable model",selectable:false,reasoning_efforts:["low","high"]},
  {id:"small-fixture",label:"Small model"},
]}];
let pendingSetting:Record<string,unknown>|null=null;
const context={isOpen:()=>true,notify() {},send(command:Record<string,unknown>){
  commands.push(command);
  queueMicrotask(()=>{
    if(command.type==="chat:context") ingestSessionContext({type:"chat:context",session_id:command.id,status:"ready",route:routes.mode,...routes,
      used_tokens:14200,context_limit_tokens:128000,available_tokens:113800,percent_used:11.1,
      categories:[{id:"messages",label:"Messages",tokens:9000,percent:7},{id:"tools",label:"Tools",tokens:3500,percent:2.7},{id:"memory",label:"Memory",tokens:1700,percent:1.4}]});
    if(command.type==="model:options") ingestSessionContext({type:"model:options",session_id:command.id,request_id:command.request_id,providers:models});
    if(command.type==="mode:set"||command.type==="reasoning:effort:set") pendingSetting=command;
    if(command.type==="chat") ingestChat(parseChatWsMessage({type:"start",session_id:command.session_id,client_id:command.client_id,source:"chat",admission_id:"native-admission",run_id:"native-run"}));
    if(command.type==="chat:pause") ingestChat(parseChatWsMessage({type:"chat:pause_state",session_id:command.session_id,admission_id:command.admission_id,run_id:command.run_id,request_id:command.request_id,pause_revision:1,state:"pausing",accepted:true}));
  });
  return true;
}};
const root=createRoot(document.getElementById("variant1-react-root")!);
let version=0;
function MotionStudy(){
  const [enabled,setEnabled]=useState(false);
  return <div className="app-shell" style={{display:"block",height:"auto",minHeight:0,minWidth:0,background:"#0e0e0e"}}>
    <div style={{padding:"16px 22px 0",color:"#919191",font:"12px Segoe UI"}}>CONTROL MOTION · LOCAL PREVIEW</div>
    <div className="composer" style={{display:"flex",alignItems:"center",gap:36,padding:"24px 22px",margin:0,border:0,background:"transparent"}}>
      <div style={{display:"grid",gap:10,color:"#919191",fontSize:12}}><span>Recording</span><span className="voice-button recording"><MicGlyph phase="recording"/></span></div>
      <div style={{display:"grid",gap:10,color:"#919191",fontSize:12}}><span>Transcribing</span><span className="voice-button"><MicGlyph phase="transcribing"/></span></div>
      <div style={{display:"grid",gap:10,color:"#919191",fontSize:12}}><span>Confirmed activation</span><MutationSwitch sessionId="motion-study" className="composer-mutation-control" checked={enabled} onChange={setEnabled} framed label="Mutation" caption={enabled ? "On" : "Off"}/></div>
    </div>
  </div>;
}
function seed(active=false) {
  __resetChatStoreForTests();__resetSessionStoreForTests();__resetTurnStoreForTests();__resetSessionContextStoreForTests();commands.length=0;
  setChatContext(context);setSessionContext(context);setSessionContextContext(context);setSessionConnection("connected");setSessionContextConnection("connected");
  ingestSessions({type:"chat:sessions",active_id:"A",items:[{id:"A",title:"Composer refinement"},{id:"B",title:"Workspace notes"}]});
  ingestChat(parseChatWsMessage({type:"chat:session",session:{id:"A",title:"Composer refinement",messages:[{role:"assistant",text:"The workspace is ready. Add your notes, choose a model, and continue from here."}],runtime:{busy:active,
    active_admission_id:active ? "native-admission" : "",active_run_id:active ? "native-run" : "",kernel:{state:"ready",generation:3},mutation_enabled:true,mutation_effective_enabled:true,mutation_toggle_available:true}}}));
  setChatState({...getChatState(),draft:active ? "Keep the interface monochrome and preserve the existing controls." : "Review the frontend changes and keep the chat interactions consistent."});
  if(active)turnController.begin({sessionId:"A",clientId:getChatState().clientId,admissionId:"native-admission",runId:"native-run"});
  root.render(<><AppearanceBindings/><DeckApp key={++version} api={null}/></>);
}
const fixture={commands,seed,
  setMutation(enabled:boolean){setChatState({...getChatState(),runtime:{...getChatState().runtime!,mutationEnabled:enabled,mutationEffectiveEnabled:enabled}});},
  acknowledgeMutation(){const command=[...commands].reverse().find(item=>item.type==="chat:runtime:mutation:set")!;
    ingestChat(parseChatWsMessage({type:"chat:runtime:mutation:set:done",id:command.id,request_id:command.request_id,enabled:command.enabled,effective_enabled:command.enabled,authority_revision:(getChatState().runtime?.mutationAuthorityRevision||0)+1}));
  },
  reduceMotion(reduced:boolean){setAppearance({motion:reduced ? "reduced" : "system"});},
  showMotionStudy(){const host=document.createElement("div");host.id="motion-study";Object.assign(host.style,{position:"fixed",top:"40px",left:"40px",width:"min(600px,calc(100vw - 80px))",zIndex:"500",border:"1px solid #303030",borderRadius:"8px",overflow:"hidden"});document.body.appendChild(host);createRoot(host).render(<MotionStudy/>);},
  acknowledge(){if(!pendingSetting)return;const command=pendingSetting;pendingSetting=null;
    if(command.type==="mode:set")Object.assign(routes,{mode:command.mode,provider:command.provider,model:command.model,reasoning_effort:command.reasoning_effort||""});
    else routes.reasoning_effort=String(command.effort);
    ingestSessionContext({type:"session:settings:ack",operation:command.type,session_id:command.id,request_id:command.request_id,status:"applied",route:routes});
  },
  pauseAtBoundary(){
    const command=commands.filter(command=>command.type==="chat:pause").at(-1)!;
    ingestChat(parseChatWsMessage({type:"chat:pause_state",session_id:command.session_id,admission_id:command.admission_id,run_id:command.run_id,pause_revision:2,state:"paused",accepted:true}));
  },
  async prepare(){
    const Reader=window.FileReader;
    window.FileReader=class extends Reader {override readAsText(file:Blob){setTimeout(()=>super.readAsText(file),700);}};
    try{await addChatFiles([new File(["Native attachment content"],"notes.txt",{type:"text/plain"})]);}
    finally{window.FileReader=Reader;}
  },
};
Object.assign(window,{composerFixture:fixture});seed();
