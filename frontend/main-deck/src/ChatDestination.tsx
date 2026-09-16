import {useSessionState} from "./state/sessionStore";
import {Icon, type IconName} from "./ui/Icon";
/**
 * Main Deck Chat destination.
 *
 * The destination owns layout only. Transcript rendering and composer
 * behavior live in focused components under ./chat.
 */
import {ChatComposer} from "./chat/ChatComposer";
import {ChatBrowserStatus} from "./chat/ChatBrowserStatus";
import {ChatRecoveryStatus} from "./chat/ChatRecoveryStatus";
import {useQuestionChatIds} from "./state/clarificationStore";
import {ChatMessageList} from "./chat/ChatMessageList";
import {revealHistory} from "./state/appStore";
import {openBrowser, usePreviewState} from "./workbench/previewStore";
import {isWorkbenchPaneVisible, PANE, togglePane, useWorkbenchState} from "./workbench/workbenchStore";

type WorkbarTab = "terminal" | "review" | "browser";
const WORKBAR_TABS: ReadonlyArray<{id: WorkbarTab; label: string; icon: IconName}> = [
  {id: "terminal", label: "Terminal", icon: "terminal"},
  {id: "review", label: "Review", icon: "review"},
  {id: "browser", label: "Browser", icon: "browser"},
];

function ChatWorkbar() {
  const questionChats = useQuestionChatIds();
  const workbench = useWorkbenchState();
  const previews = usePreviewState();
  const chatId=useSessionState().displayedSessionId || "";

  function pressed(kind: WorkbarTab): boolean {
    if (kind === "browser") {
      return previews.tabs.some(tab => tab.target.kind === "url" && (tab.ownerChatId || "") === chatId
        && isWorkbenchPaneVisible(`preview:${tab.id}`, workbench));
    }
    return isWorkbenchPaneVisible(kind, workbench);
  }

  function toggle(kind: WorkbarTab): void {
    if (kind === "browser") {
      const tab = [...previews.tabs].reverse().find(item => item.target.kind === "url" && (item.ownerChatId || "") === chatId);
      if (!tab) openBrowser();
      else togglePane(`preview:${tab.id}`, "right");
      return;
    }
    togglePane(kind === "terminal" ? PANE.terminal : PANE.review, kind === "terminal" ? "bottom" : "right");
  }

  return <div className="chat-workbar deck-segmented" role="toolbar" aria-label="Thread tools">
    {questionChats.length ? <button type="button" className="chat-workbar__button" aria-label="Chats needing answers" title="Chats needing answers" onClick={revealHistory}>? <span>{questionChats.length}</span></button> : null}
    {WORKBAR_TABS.map(item => {
      const active = pressed(item.id);
      return <button
        key={item.id}
        type="button"
        className={`chat-workbar__button deck-segmented__button${active ? " is-active" : ""}`}
        aria-pressed={active}
        aria-label={item.label}
        title={item.label}
        onClick={() => toggle(item.id)}
      >
        <Icon name={item.icon}/>
      </button>;
    })}
  </div>;
}

export function ChatDestination() {
  // Session history is requested by runtime enter/connection hooks once the
  // WebSocket is open — do not fire refreshChat on mount while offline.

  return <>
    <ChatWorkbar/>
    <div className="chat-notices"><ChatBrowserStatus/><ChatRecoveryStatus/></div>
    <ChatMessageList />
    <ChatComposer />
  </>;
}

export default ChatDestination;
