import {Suspense, type ReactNode} from "react";

export function LazySurface({
  children,
  label,
}: {
  children: ReactNode;
  label: string;
}) {
  return <Suspense fallback={<div className="deck-lazy-surface" role="status" aria-live="polite">
    <span>Loading</span>
    <strong>{label}</strong>
  </div>}>
    {children}
  </Suspense>;
}
