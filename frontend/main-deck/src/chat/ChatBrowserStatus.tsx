import {detachedChatId} from "../runtime/viewIdentity";
import {getPreviewState,openBrowser} from "../workbench/previewStore";
import {revealPreviewPane} from "../workbench/workbenchStore";
import {useEffect,useState} from "react";
import {useChatState} from "../chatStore";
import {observeBrowserChat, refreshBrowserChat, resolveBrowserOperation, useBrowserSettings, browserNoticeKey,dismissBrowserNotice,type BrowserReadiness} from "../browserSettingsStore";
import {navigateTo, selectSettingsCategory} from "../state/appStore";
import {Button} from "../ui/Button";
import {Icon} from "../ui/Icon";

export const BROWSER_STATE_LABELS: Record<BrowserReadiness, string> = {
  idle: "Browser not started", selection_required: "Choose a browser profile", login_required: "Browser sign-in needed",
  profile_locked: "Browser profile locked", connection_failed: "Browser connection failed", connecting: "Connecting browser…",
  ready: "Browser ready", cancelled: "Browser request cancelled", unknown: "Browser status unavailable",
};

export function BrowserReadinessActions({chatId, showSelection = true}: {chatId: string; showSelection?: boolean}) {
  const settings = useBrowserSettings();
  const chat = settings.chats[chatId];
  const pending = !!settings.pending[`resolve:${chatId}`] || !!settings.pending[`selection:${chatId}`];
  if (!chat) return null;
  return <div className="browser-state-actions">
    {chat.selection.mode === "embedded" && !detachedChatId() ? <Button tone="quiet" onClick={()=>{
      const tab=getPreviewState().tabs.find(t=>t.target.kind==="url" && t.ownerChatId===chatId);
      revealPreviewPane(tab?.id || openBrowser("about:blank",{newTab:true,ownerChatId:chatId}));
    }}>Open this chat's browser</Button>:null}
    {showSelection && chat.actions.includes("select_profile") ? <Button tone="quiet" disabled={!settings.connected || pending} onClick={() => {
      selectSettingsCategory("browser"); navigateTo("settings");
    }}>Browser settings</Button> : null}
    {chat.pending_operation_id && chat.actions.includes("retry") ? <Button tone="quiet" disabled={!settings.connected || pending} onClick={() => resolveBrowserOperation(chatId, "retry")}>
      {pending ? "Checking…" : "Retry connection"}
    </Button> : null}
    {chat.pending_operation_id && chat.actions.includes("cancel") ? <Button tone="quiet" disabled={!settings.connected || pending} onClick={() => resolveBrowserOperation(chatId, "cancel")}>Cancel browser request</Button> : null}
    {settings.errors[`resolve:${chatId}`] ? <span role="alert">{settings.errors[`resolve:${chatId}`]}</span> : null}
  </div>;
}

export function ChatBrowserStatus() {
  const chatId = useChatState().sessionId;
  const settings = useBrowserSettings();
  useEffect(() => { observeBrowserChat(chatId); return () => observeBrowserChat(null); }, [chatId]);
  const chat = chatId ? settings.chats[chatId] : undefined;
  const error = chatId ? settings.errors[`state:${chatId}`] : "";
  const [stale,setStale]=useState(false);
  useEffect(()=>{
    setStale(false);
    if(!chatId || chat?.state!=="connecting" || !settings.connected)return;
    const timer=setTimeout(()=>{setStale(true);refreshBrowserChat(chatId);},15000);
    return ()=>clearTimeout(timer);
  },[chatId,chat?.state,chat?.revision,settings.connected]);
  if (!chatId || (!error && (!chat || ["idle", "ready", "cancelled"].includes(chat.state)))) return null;
  if(settings.dismissed[chatId]===browserNoticeKey(chatId))return null;
  return <section className="chat-browser-status" aria-label="Browser activity">
    <Icon name="browser"/>
    <div>
      <div role="status"><strong>{!settings.connected ? "Browser disconnected" : stale ? "Browser connection not yet confirmed" : chat ? BROWSER_STATE_LABELS[chat.state] : "Browser status unavailable"}</strong>
        <p>{error || chat?.message}</p></div>
      <BrowserReadinessActions chatId={chatId}/>
      <Button tone="quiet" disabled={!settings.connected || !!settings.pending[`state:${chatId}`]} onClick={() => refreshBrowserChat(chatId)}>{settings.pending[`state:${chatId}`] ? "Checking…" : "Check connection"}</Button>
    </div>
    <Button tone="quiet" aria-label="Dismiss browser notice" onClick={()=>dismissBrowserNotice(chatId)}>Dismiss</Button>
  </section>;
}
