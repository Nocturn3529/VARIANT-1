import {dismissRecoveryNotice} from "./recovery";
import {resumeOrphanedTask,useChatState} from "../chatStore";
import {useSessionState} from "../state/sessionStore";
import {Button} from "../ui/Button";

export function ChatRecoveryStatus() {
  const chat=useChatState(),task=chat.orphanedTask;
  const navigating=!!useSessionState().pendingAction;
  const owner=String(task?.session_id || task?.chat_id || "");
  if(!task || !owner || owner!==chat.sessionId || chat.turnActive)return null;
  return <section className="chat-recovery-banner" aria-label="Interrupted task">
    <div role="status"><strong>Interrupted task</strong><p>{String(task.goal || "A saved checkpoint is available for this chat.")}</p></div>
    <Button tone="quiet" disabled={!chat.connected || navigating} onClick={resumeOrphanedTask}>Resume task</Button>
    <Button tone="quiet" aria-label="Dismiss recovery notice" onClick={dismissRecoveryNotice}>Dismiss</Button>
  </section>;
}
