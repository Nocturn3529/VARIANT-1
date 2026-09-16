import {ingestChatProjects} from "../frontend/main-deck/src/state/chatProjectStore";
import {openDirectoryPreview} from "../frontend/main-deck/src/workbench/previewStore";
import {noteDisplayedSession,ingestSessions} from "../frontend/main-deck/src/state/sessionStore";
/** Isolated native diagnostic. Never imported or packaged by the application. */
import {createRoot} from "react-dom/client";
import {Workbench} from "../frontend/main-deck/src/workbench/Workbench";
import {ingestBrowserHost, setBrowserHostConnection, setBrowserHostContext} from "../frontend/main-deck/src/workbench/browserHostBridge";
import {detachWorkbenchPane, getWorkbenchState, hidePane, moveWorkbenchPane, PANE} from "../frontend/main-deck/src/workbench/workbenchStore";
import {closeNativeWindow,dockAllNativeWindows,installNativeWindowBridge} from "../frontend/main-deck/src/workbench/nativeWindowStore";
import {findGroupOfPane} from "../frontend/main-deck/src/workbench/layoutModel";
import {installBrowserDownloads} from "../frontend/main-deck/src/workbench/browserDownloads";

document.getElementById("variant1-boot")?.remove();
const receipts: Record<string, unknown>[] = [];
const requests = new Map<string, (result: any) => void>();
let next = 0;
setBrowserHostContext({notify() {}, api: window.variant1Deck, send(message) {
  if (message.type === "browser:host:result") {
    receipts.push(message as unknown as Record<string, unknown>);
    requests.get(String(message.id))?.(message.result);
    requests.delete(String(message.id));
  }
  return true;
}});
setBrowserHostConnection("connected");
installNativeWindowBridge(window.variant1Deck || null);
const disposeDownloads = installBrowserDownloads(window.variant1Deck || null);
window.addEventListener("beforeunload", disposeDownloads, {once:true});
document.addEventListener("securitypolicyviolation", event => console.error("E01_CSP", event.violatedDirective, event.blockedURI));
createRoot(document.getElementById("variant1-react-root")!).render(<div className="app-shell chat-shell" style={{display: "block", height: "100vh"}}><Workbench api={window.variant1Deck || null}/></div>);
Object.assign(window, {e01: {
  command(command: Record<string, unknown>) {
    const id = `native-e01-${++next}`;
    return new Promise(resolve => {
      requests.set(id, resolve);
      void ingestBrowserHost({type: "browser:host:command", id, command});
    });
  },
  showProjects() {
    const message={type:"chat:sessions",items:[{id:"owner-a",title:"Refine chat composer",project:{root:"C:/Example/Variant",name:"Variant"}},{id:"owner-b",title:"Research notes",project:{root:"C:/Example/Research",name:"Research"}}]};
    ingestSessions(message);ingestChatProjects(message);noteDisplayedSession("owner-a");
    openDirectoryPreview("C:/Example/Assets","owner-a");
  },
  selectChat(id:string) {ingestSessions({type:"chat:sessions",active_id:id,items:[{id:"owner-a",title:"Owner A"},{id:"owner-b",title:"Owner B"}]});noteDisplayedSession(id);},
  stackWithChat(id: string) {
    const chat = findGroupOfPane(getWorkbenchState().layout, PANE.workspace)!;
    moveWorkbenchPane(`preview:${id}`, chat.id, "center");
  },
  hide(id: string) { hidePane(`preview:${id}`); },
  group(id: string) { return findGroupOfPane(getWorkbenchState().layout, `preview:${id}`)?.id; },
  detach(id: string) { detachWorkbenchPane(`preview:${id}`); return findGroupOfPane(getWorkbenchState().layout, `preview:${id}`)?.id; },
  dock() { dockAllNativeWindows(); },
  closeWindow(groupId:string){closeNativeWindow(`pane:${groupId}`);},
  receipts() { return receipts.map(receipt => { const result = {...receipt.result as Record<string, unknown>}; if (result.image) result.image = `<${String(result.image).length} base64 characters>`; return {...receipt, result}; }); },
}});
