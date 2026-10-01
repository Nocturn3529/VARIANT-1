import {disposeTerminalChat} from "../context/terminalStore";
import {forgetChatPreviews} from "../workbench/previewStore";
import {forgetChatWorkbench} from "../workbench/workbenchStore";
import {detachedChatId} from "../runtime/viewIdentity";
import {forgetDeletedChatState} from "../chat/stateCore";

/** Run only after the backend confirms the chat was deleted. */
export function releaseDeletedChat(chatId:string):void {
  forgetDeletedChatState(chatId);
  const detached=detachedChatId();
  if(detached){if(detached===chatId){disposeTerminalChat(chatId);window.variant1Deck?.close?.();}return;}
  const previews=forgetChatPreviews(chatId);
  forgetChatWorkbench(chatId,previews);
  disposeTerminalChat(chatId);
  void window.variant1Deck?.manageChatWindow?.(chatId,"close").catch(()=>{/* The deleted chat window also receives its own close event. */});
}
