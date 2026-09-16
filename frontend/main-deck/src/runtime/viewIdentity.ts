export function detachedChatId():string {
  if(typeof window === "undefined")return "";
  const id=new URLSearchParams(window.location.search).get("detached_chat") || "";
  return /^[\w.:-]{1,256}$/.test(id) ? id : "";
}

export function isDockedChat():boolean {
  return typeof window!=="undefined" && !!detachedChatId() && new URLSearchParams(window.location.search).get("docked")==="1";
}

/** A trusted same-origin chat iframe borrows only the main Deck's narrow bridge. */
export function installDockedChatBridge():void {
  if(!isDockedChat() || window.parent===window || window.variant1Deck)return;
  try {
    if(window.parent.location.origin!==window.location.origin || window.parent.location.pathname!=="/frontend/main-deck/index.html")return;
    const api=window.parent.variant1Deck;if(!api)return;
    window.variant1Deck={getBackendInfo:()=>api.getBackendInfo?.() || Promise.resolve(null),
      onBackendStatus:callback=>api.onBackendStatus?.(callback),
      getPathForFile:file=>api.getPathForFile?.(file) || "",
      openExternal:url=>api.openExternal?.(url)};
  } catch { /* A foreign frame never receives the desktop bridge. */ }
}
