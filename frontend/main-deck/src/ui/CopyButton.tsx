import {useEffect, useState} from "react";
import {Icon} from "./Icon";

/**
 * Copy with in-place confirmation: the icon morphs to a check for a moment
 * instead of raising a toast, so feedback stays where the user is looking.
 */
export function CopyButton({text, label = "Copy", title, className = ""}: {
  text: string;
  label?: string;
  title?: string;
  className?: string;
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 1400);
    return () => window.clearTimeout(timer);
  }, [state]);
  return <button type="button" className={`copy-button ${className}`.trim()} data-state={state} title={title || label}
    onClick={() => {
      navigator.clipboard.writeText(text || "").then(() => setState("copied"), () => setState("failed"));
    }}>
    <span className="copy-button__icon" aria-hidden="true"><Icon name={state === "copied" ? "check" : "copy"}/></span>
    <span aria-live="polite">{state === "copied" ? "Copied" : state === "failed" ? "Couldn't copy" : label}</span>
  </button>;
}
