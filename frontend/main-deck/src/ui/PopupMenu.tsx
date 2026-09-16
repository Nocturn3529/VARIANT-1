import {useEffect, useLayoutEffect, useRef, useState, type ReactNode} from "react";
import {createPortal} from "react-dom";
import {useSurfaceDocument} from "./SurfaceDocument";

/** Shared pointer dismissal, viewport bounds, and keyboard ownership for tool menus. */
export function PopupMenu({x, y, className, onClose, children}: {
  x: number; y: number; className: string; onClose: () => void; children: ReactNode;
}) {
  const ownerDocument = useSurfaceDocument();
  const ownerWindow = ownerDocument.defaultView || window;
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const [position, setPosition] = useState({left: x, top: y});
  const [portalHost] = useState(() => [...ownerDocument.querySelectorAll("dialog[open]")].at(-1) || ownerDocument.activeElement?.closest(".workbench-group:popover-open") || ownerDocument.body);
  useLayoutEffect(() => {
    ref.current?.showPopover();
    const rect = ref.current!.getBoundingClientRect();
    setPosition({left: Math.max(8, Math.min(x, ownerWindow.innerWidth - rect.width - 8)),
      top: Math.max(8, Math.min(y, ownerWindow.innerHeight - rect.height - 8))});
  }, [x, y]);
  useEffect(() => {
    const previous = ownerDocument.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
    const outside = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) closeRef.current();
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const dialog = ownerDocument.querySelector("dialog[open]");
      if (dialog && !dialog.contains(ref.current)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      closeRef.current();
    };
    ownerDocument.addEventListener("pointerdown", outside, true);
    ownerDocument.addEventListener("keydown", escape, true);
    return () => {
      ownerDocument.removeEventListener("pointerdown", outside, true);
      ownerDocument.removeEventListener("keydown", escape, true);
      requestAnimationFrame(() => {
        if (previous?.isConnected && ownerDocument.activeElement === ownerDocument.body) previous.focus();
      });
    };
  }, []);
  return createPortal(<div ref={ref} role="menu" popover="manual" data-deck-menu="" className={className} style={{position: "fixed", inset: "auto", margin: 0, ...position}}
    onPointerDown={event => event.stopPropagation()}
    onContextMenu={event => event.preventDefault()}
    onKeyDown={event => {
      const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
      const index = items.indexOf(ownerDocument.activeElement as HTMLButtonElement);
      let next: number;
      if (event.key === "ArrowDown") next = (index + 1) % items.length;
      else if (event.key === "ArrowUp") next = (index - 1 + items.length) % items.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = items.length - 1;
      else if (event.key === "Tab") { event.preventDefault(); onClose(); return; }
      else return;
      event.preventDefault(); event.stopPropagation(); items[next]?.focus();
    }}
  >{children}</div>, portalHost);
}
