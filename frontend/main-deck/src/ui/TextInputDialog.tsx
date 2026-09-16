import {useEffect, useId, useRef, useState} from "react";
import {createPortal} from "react-dom";
import {Button} from "./Button";
import {trapDialogFocus} from "./Overlay";
import {useSurfaceDocument} from "./SurfaceDocument";

export function TextInputDialog({
  title, label, initialValue, maxLength = 200, submitLabel = "Save", returnFocus,
  onSubmit, onClose,
}: {
  title: string;
  label: string;
  initialValue: string;
  maxLength?: number;
  submitLabel?: string;
  returnFocus?: HTMLElement | null;
  onSubmit: (value: string) => boolean | void | Promise<boolean | void>;
  onClose: () => void;
}) {
  const ownerDocument = useSurfaceDocument();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState(initialValue);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const titleId = useId();
  const errorId = useId();
  useEffect(() => {
    const previous = returnFocus || ownerDocument.activeElement as HTMLElement | null;
    const dialog = dialogRef.current!;
    dialog.showModal();
    inputRef.current?.focus();
    inputRef.current?.select();
    return () => {
      dialog.close();
      requestAnimationFrame(() => { if (previous?.isConnected) previous.focus(); });
    };
  }, [returnFocus, ownerDocument]);

  return createPortal(<dialog
    ref={dialogRef}
    className="deck-input-dialog"
    aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); event.stopPropagation(); if (!pending) onClose(); }}
    onKeyDown={event => { trapDialogFocus(event); event.stopPropagation(); }}
  >
    <form onSubmit={async event => {
      event.preventDefault();
      if (!value.trim() || pending) return;
      setPending(true);
      setError("");
      try {
        const accepted = await onSubmit(value.trim());
        if (accepted === false) setError("Could not save. Check the connection and try again.");
        else onClose();
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
      } finally { setPending(false); }
    }}>
      <header><h2 id={titleId}>{title}</h2><Button tone="icon" aria-label="Close dialog" disabled={pending} onClick={onClose}>×</Button></header>
      <label><span>{label}</span><input ref={inputRef} value={value} maxLength={maxLength} disabled={pending}
        aria-describedby={error ? errorId : undefined} onChange={event => setValue(event.target.value)}/></label>
      {error ? <p role="alert" id={errorId}>{error}</p> : null}
      <footer><Button disabled={pending} onClick={onClose}>Cancel</Button><Button tone="primary" type="submit" disabled={pending || !value.trim()}>{pending ? "Saving…" : submitLabel}</Button></footer>
    </form>
  </dialog>, ownerDocument.body);
}
