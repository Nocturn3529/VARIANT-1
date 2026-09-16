import {useToastState} from "../state/toastStore";
import {useNativeWindows} from "../workbench/nativeWindowStore";

/** Operation receipts retain their surface; ordinary background notices stay in Chat. */
export function ToastHost({surface = "main"}: {surface?: string}) {
  const toast = useToastState();
  const windows = useNativeWindows();
  const destination = toast.surface !== "main" && windows[toast.surface]?.phase === "ready" ? toast.surface : "main";
  const visible = toast.visible && destination === surface;
  return <div className={`toast${visible ? " visible" : ""}`} id={surface === "main" ? "toast" : undefined}
    role="status" aria-live="polite" aria-atomic="true">{visible ? toast.message : ""}</div>;
}
