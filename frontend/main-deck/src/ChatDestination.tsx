/**
 * Main Deck Chat destination.
 *
 * The destination owns layout only. Transcript rendering and composer
 * behavior live in focused components under ./chat. The thread's panel
 * buttons (Files, Terminal, Review, Browser) live in the window header.
 */
import {ChatComposer} from "./chat/ChatComposer";
import {ChatBrowserStatus} from "./chat/ChatBrowserStatus";
import {ChatRecoveryStatus} from "./chat/ChatRecoveryStatus";
import {ChatMessageList} from "./chat/ChatMessageList";

export function ChatDestination() {
  // Session history is requested by runtime enter/connection hooks once the
  // WebSocket is open — do not fire refreshChat on mount while offline.

  return <>
    <div className="chat-notices"><ChatBrowserStatus/><ChatRecoveryStatus/></div>
    <ChatMessageList />
    <ChatComposer />
  </>;
}

export default ChatDestination;
