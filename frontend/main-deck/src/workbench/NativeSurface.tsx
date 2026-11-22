import {useLayoutEffect, useRef, useState, type ReactNode} from "react";
import {createPortal} from "react-dom";
import {SurfaceDocumentContext} from "../ui/SurfaceDocument";
import {ToastHost} from "../ui/ToastHost";
import {Icon} from "../ui/Icon";
import {navigateTo} from "../state/appStore";
import {switchSession} from "../state/sessionStore";
import {usePaneChatOwner} from "./usePaneChatOwner";
import {notifyToast} from "../state/toastStore";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {closeNativeWindow, registerNativeSurface, updateNativeWindow, useNativeWindows} from "./nativeWindowStore";

function NativeIdentity({id, title, chatId}: {id: string; title: string; chatId: string}) {
  const surface = useSurfaceDocument();
  const owner = usePaneChatOwner(chatId);
  useLayoutEffect(() => {
    const name = owner.label && owner.title !== title ? `${title} · ${owner.label}` : owner.label || title;
    surface.title = name + " — VARIANT-1";
    updateNativeWindow(id, {title: name});
  }, [id, surface, title, owner.label, owner.title]);
  return <div className="native-window-identity"><strong>{title}</strong>{chatId && <button type="button"
    aria-label="Go to owning chat" title={`Go to owning chat · ${owner.label}${owner.root ? "\n" + owner.root : ""}`}
    onClick={() => {
      navigateTo("chat");
      switchSession(chatId);
      void window.variant1Deck?.controlNativeWindow?.("deck", "focus");
      window.focus();
    }}>{owner.label}</button>}</div>;
}

/** One portal container, physically adopted into the child window and back. */
export function NativeSurface({id, title, ownerChatId = "", children, onClosed,onBeforeClose}: {
  id: string; title: string; ownerChatId?: string; children: ReactNode; onClosed?: (dock: boolean) => void;onBeforeClose?:()=>boolean;
}) {
  const records = useNativeWindows();
  const record = records[id];
  const targetDocument = record?.document || document;
  const native = record?.phase === "ready";
  const dock = useRef<HTMLDivElement>(null);
  const callback = useRef(onClosed);
  callback.current = onClosed;
  const beforeClose=useRef(onBeforeClose);beforeClose.current=onBeforeClose;
  const [pinned, setPinned] = useState(false);
  const [mount] = useState(() => {
    const element = document.createElement("div");
    element.className = "native-surface-mount";
    element.dataset.nativeSurface = id;
    return element;
  });
  const moveTo = (target: HTMLElement) => {
    if (mount.parentNode === target) return;
    mount.ownerDocument.dispatchEvent(new CustomEvent("variant1:surface-will-move", {detail: {mount}}));
    target.appendChild(mount);
  };
  useLayoutEffect(() => registerNativeSurface(id, {
    restore: () => { if (dock.current) moveTo(dock.current); },
    closed: isDock => callback.current?.(isDock),
    canClose:()=>beforeClose.current?.() ?? true,
  }), [id, mount]);
  useLayoutEffect(() => {
    const target = native ? targetDocument.getElementById("native-popout-root") : dock.current;
    if (target) moveTo(target);
    if (native) {
      mount.querySelector<HTMLButtonElement>(".native-window-chrome button")?.focus({preventScroll: true});
      void window.variant1Deck?.controlNativeWindow?.(id, "ready").then(result => { if (result?.ok) { setPinned(!!result.pinned); updateNativeWindow(id, {pinned: !!result.pinned}); } });
    }
    // Consumers that overlay a terminal surface must remeasure after adoption.
    window.dispatchEvent(new Event("variant1:surface-document"));
  }, [native, targetDocument, mount, id]);
  const control = async (action: "minimize" | "maximize" | "pin") => {
    const result = await window.variant1Deck?.controlNativeWindow?.(id, action);
    if (action === "pin" && typeof result?.pinned === "boolean") {
      setPinned(result.pinned);
      updateNativeWindow(id, {pinned: result.pinned});
      if (!result.ok) notifyToast("The window’s pin setting could not be changed.", id);
    }
  };
  return <>
    <div ref={dock} className="native-surface-slot"/>
    {createPortal(<SurfaceDocumentContext.Provider value={targetDocument}>
      <div className={`native-surface${native ? " is-native" : ""}`}>
        {native ? <header className="native-window-chrome" onDoubleClick={event => { if (!(event.target as HTMLElement).closest("button")) void control("maximize"); }}>
          <NativeIdentity id={id} title={title} chatId={ownerChatId}/>
          <button type="button" aria-label={`Dock ${title} panel`} title="Return to VARIANT-1" onClick={() => closeNativeWindow(id, true)}>Dock</button>
          <button type="button" aria-label="Keep window on top" aria-pressed={pinned} title="Keep on top" onClick={() => void control("pin")}>Pin</button>
          <button type="button" aria-label="Minimize panel window" onClick={() => void control("minimize")}><Icon name="minimize"/></button>
          <button type="button" aria-label="Maximize or restore panel window" onClick={() => void control("maximize")}><Icon name="maximize"/></button>
          <button type="button" aria-label={`Close ${title} window`} onClick={() => closeNativeWindow(id)}><Icon name="close"/></button>
        </header> : null}
        <div className="native-surface-content">{children}</div>
        {native ? <ToastHost surface={id}/> : null}
      </div>
    </SurfaceDocumentContext.Provider>, mount)}
  </>;
}
