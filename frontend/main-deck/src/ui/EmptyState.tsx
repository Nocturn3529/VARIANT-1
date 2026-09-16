import type {ReactNode} from "react";

export type EmptyStateProps = {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  /** `panel` fills the remaining surface. `inline` sits inside a list or card. */
  tone?: "panel" | "inline";
  hidden?: boolean;
  id?: string;
  className?: string;
};

/** Shared blank for lists and panes. Chat welcome and chart overlays stay local. */
export function EmptyState({
  title,
  description,
  action,
  tone = "inline",
  hidden,
  id,
  className,
}: EmptyStateProps) {
  return <div
    className={["deck-empty", `deck-empty--${tone}`, className].filter(Boolean).join(" ")}
    id={id}
    hidden={hidden}
    role="status"
  >
    <strong className="deck-empty__title">{title}</strong>
    {description ? <p className="deck-empty__description">{description}</p> : null}
    {action ? <div className="deck-empty__action">{action}</div> : null}
  </div>;
}
