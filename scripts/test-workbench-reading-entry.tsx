import {createRoot} from "react-dom/client";
import "@xterm/xterm/css/xterm.css";
import "../frontend/main-deck/src/styles/index.css";
import {DeckApp} from "../frontend/main-deck/src/DeckApp";
import {AppearanceBindings, setAppearance} from "../frontend/main-deck/src/state/appearanceStore";
import {getSessionState, ingestSessions, noteDisplayedSession, setSessionConnection, setSessionContext} from "../frontend/main-deck/src/state/sessionStore";
import {activateChatState, getCachedChatState, initialChatState, setChatState} from "../frontend/main-deck/src/chat/stateCore";
import {ingestChatProjects} from "../frontend/main-deck/src/state/chatProjectStore";
import {ingestTerminal, setTerminalConnection, setTerminalContext} from "../frontend/main-deck/src/context/terminalStore";
import {chatPaneId, detachWorkbenchPane, getWorkbenchState, hidePane, revealPane} from "../frontend/main-deck/src/workbench/workbenchStore";

const a = "test-reader-a", b = "test-reader-b";
const sessions = {type:"chat:sessions", active_id:a, items:[
  {id:a,title:"Dashboard rebuild",project:{root:"C:/fixture/Observatory", name:"Observatory"}},
  {id:b,title:"Release notes",project:{root:"C:/fixture/Archive", name:"Archive"}},
]};
function display(id: string) { activateChatState(id); noteDisplayedSession(id); }
const switches: string[] = [];
setSessionContext({notify(){},send(command) {
  if (command.type === "chat:session:switch") {
    switches.push(command.id);
    queueMicrotask(() => display(command.id));
  }
  return true;
}});
setSessionConnection("connected"); ingestSessions(sessions); ingestChatProjects(sessions); display(a);
const now = Math.floor(Date.now()/1000);
setChatState({...initialChatState(),sessionId:a,title:"Dashboard rebuild",connected:true,
  turnActive:true,activeTurnId:"fixture-run",subtitle:"Working",messages:[
    {role:"user",text:"Review the dashboard’s export controls.",ts:now-30},
    {role:"assistant",text:"I checked the placement and the labels. The export menu stays beside the project name.",ts:now-22},
    {role:"user",origin:{kind:"peer",peer_id:"chat:reader",message_id:"fixture-peer"},text:"",ts:now-14,
      peerDisplay:{display_name:"Review assistant",content:"The exported rows match the selected project."}},
    {role:"user",text:"Please check the compact layout too.",ts:now-4},
  ]});
let read = false;
setTerminalContext({notify(){},send(command) {
  if (command.type === "terminal:read" && !read) {
    read = true;
    queueMicrotask(() => ingestTerminal({type:"terminal:accepted",chat_id:a,operation:"read",request_id:command.request_id,
      result:{entity:{id:"owner-terminal",kind:"terminal"},frames:[{text:"AlphaMarker full-output-marker\r\n"}],next_cursor:1}}));
  }
  return true;
}});
setTerminalConnection("connected");
ingestTerminal({type:"execution:snapshot", chat_id:a, terminals:[{id:"owner-terminal",state:"running",
  cwd:"C:/fixture/Observatory",profile:"Fixture shell",capabilities:{true_pty:true},dimensions:{cols:80,rows:24}}]});
setAppearance({density:"comfortable",motion:"reduced"});
createRoot(document.getElementById("variant1-react-root")!).render(<><AppearanceBindings/><DeckApp api={window.variant1Deck || null}/></>);
Object.assign(window, {readingFixture: {
  compact: (compact: boolean) => setAppearance({density:compact ? "compact" : "comfortable"}),
  hideFiles: () => hidePane(chatPaneId("files",a)),
  reveal: () => revealPane(chatPaneId("terminal",a),"bottom"),
  detach: () => detachWorkbenchPane(chatPaneId("terminal",a)),
  switchToB: () => display(b),
  state: () => ({a:getCachedChatState(a)?.draft || "", b:getCachedChatState(b)?.draft || "", current:getSessionState().displayedSessionId,
    drawer:getWorkbenchState().overlayPaneId, switches}),
}});
