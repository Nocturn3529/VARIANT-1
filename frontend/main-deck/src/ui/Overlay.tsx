import {Icon} from "./Icon";
import {useLayoutEffect, useRef, type KeyboardEvent, type ReactNode} from "react";

/** Keep Tab within the app dialog instead of allowing Chromium chrome to take focus. */
export function trapDialogFocus(event: KeyboardEvent<HTMLDialogElement>) {
  if (event.key !== "Tab" || event.defaultPrevented) return;
  const dialog = event.currentTarget;
  if (!dialog.open) return;
  const owner = dialog.ownerDocument;
  const controls = [...dialog.querySelectorAll<HTMLElement>(
    "button, input, textarea, select, a[href], summary, [tabindex]",
  )].filter(node => node.tabIndex >= 0 && !node.matches(":disabled")
    && !node.closest("[hidden], [inert]") && node.getClientRects().length > 0
    && getComputedStyle(node).visibility !== "hidden");
  const target = event.shiftKey ? controls.at(-1) : controls[0];
  if (!target) { event.preventDefault(); dialog.focus(); return; }
  const boundary = event.shiftKey ? controls[0] : controls.at(-1);
  if (owner.activeElement === boundary || !dialog.contains(owner.activeElement)) {
    event.preventDefault(); target.focus({preventScroll: true});
  }
}

/** Native modal focus and stacking, including above detached chat panes. */
export function Overlay({children, onClose, labelledBy, className = "", open = true, closeOnSurfaceClick = false}: {
  children: ReactNode;
  onClose: () => void;
  labelledBy: string;
  className?: string;
  open?: boolean;
  closeOnSurfaceClick?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const dialog = ref.current;
    const previous = dialog?.ownerDocument.activeElement as HTMLElement | null;
    if (open) dialog?.showModal();
    else dialog?.close();
    return () => {
      dialog?.close();
      if (open && previous?.isConnected) previous.focus({preventScroll: true});
    };
  }, [open]);
  return <dialog ref={ref} className={`deck-overlay ${className}`} aria-labelledby={labelledBy}
    onKeyDown={trapDialogFocus}
    onCancel={event => { event.preventDefault(); event.stopPropagation(); onClose(); }}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      if (closeOnSurfaceClick) {onClose();return;}
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    }}>
    {children}
  </dialog>;
}

export function OverlayHeader({title, id, onClose, onPopout}: {title: string; id: string; onClose: () => void; onPopout?: () => void}) {
  return <header className="deck-overlay__header"><h1 id={id}>{title}</h1>
    {onPopout ? <button type="button" className="deck-overlay__popout" aria-label={`Pop out ${title.toLowerCase()}`} title="Open in a separate window" onClick={onPopout}><Icon name="popout"/></button> : null}
    <button type="button" aria-label={`Close ${title.toLowerCase()}`} onClick={onClose}><Icon name="close"/></button>
  </header>;
}
