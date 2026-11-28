/**
 * Shared in-app confirmation dialog inspired by Hermes Agent's ConfirmDialog.
 * Replaces blocking native window.confirm() with a themed native <dialog>
 * supporting native button Enter/Space activation, Escape/backdrop cancel,
 * async work state, a brief completion beat, and inline error feedback.
 */
import {useId, useLayoutEffect, useRef, useState} from "react";
import {createPortal} from "react-dom";
import {Icon} from "./Icon";
import {trapDialogFocus} from "./Overlay";
import {useSurfaceDocument} from "./SurfaceDocument";
import {useReducedMotion} from "../state/appearanceStore";

type ConfirmPhase = "idle" | "working" | "done";

export function ConfirmDialog({
  title,
  message,
  detail,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  workingLabel = "Working…",
  doneLabel = "Done",
  danger = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  workingLabel?: string;
  doneLabel?: string;
  danger?: boolean;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const ownerDocument = useSurfaceDocument();
  const reduced = useReducedMotion();
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const confirmButtonRef = useRef<HTMLButtonElement>(null);
  const doneTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const [phase, setPhase] = useState<ConfirmPhase>("idle");
  const [error, setError] = useState("");

  useLayoutEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    const previous = ownerDocument.activeElement as HTMLElement | null;
    if (dialog && !dialog.open) dialog.showModal();
    (danger ? cancelButtonRef : confirmButtonRef).current?.focus({preventScroll: true});
    return () => {
      mountedRef.current = false;
      if (doneTimerRef.current != null) window.clearTimeout(doneTimerRef.current);
      if (dialog?.open) dialog.close();
      if (previous?.isConnected) previous.focus({preventScroll: true});
    };
  }, [ownerDocument, danger]);

  async function submit() {
    if (phase !== "idle") return;
    setError("");
    setPhase("working");
    try {
      await onConfirm();
      if (!mountedRef.current) return;
      if (reduced) {
        onCancel();
        return;
      }
      setPhase("done");
      doneTimerRef.current = window.setTimeout(() => {
        doneTimerRef.current = null;
        if (mountedRef.current) onCancel();
      }, 320);
    } catch (err) {
      if (!mountedRef.current) return;
      setPhase("idle");
      setError(err instanceof Error ? err.message : String(err || "Action failed"));
    }
  }

  return createPortal(
    <dialog
      ref={dialogRef}
      className="deck-confirm-dialog"
      aria-labelledby={titleId}
      onKeyDown={trapDialogFocus}
      onCancel={event => {
        event.preventDefault();
        event.stopPropagation();
        if (phase === "idle") onCancel();
      }}
      onClick={event => {
        if (event.target !== event.currentTarget || phase !== "idle") return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) {
          onCancel();
        }
      }}
    >
      <header>
        <h2 id={titleId}>{title}</h2>
        <button
          type="button"
          className="deck-button deck-button--icon"
          aria-label="Close"
          disabled={phase !== "idle"}
          onClick={onCancel}
        ><Icon name="close"/></button>
      </header>
      <p className="deck-confirm-dialog__message">{message}</p>
      {detail ? <p className="deck-confirm-dialog__detail">{detail}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      <footer>
        {phase === "done" ? <span className="deck-confirm-dialog__done" role="status"><Icon name="check"/>{doneLabel}</span> : <>
          <button
            ref={cancelButtonRef}
            type="button"
            className="deck-button deck-button--quiet"
            data-confirm-cancel="true"
            disabled={phase !== "idle"}
            onClick={onCancel}
          >{cancelLabel}</button>
          <button
            ref={confirmButtonRef}
            type="button"
            className={`deck-button ${danger ? "deck-button--danger" : "deck-button--primary"}`}
            disabled={phase !== "idle"}
            onClick={() => void submit()}
          >
            {phase === "working" ? workingLabel : confirmLabel}
          </button>
        </>}
      </footer>
    </dialog>,
    ownerDocument.body,
  );
}
