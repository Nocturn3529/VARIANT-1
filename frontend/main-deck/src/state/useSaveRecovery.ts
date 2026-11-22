import {useEffect, useRef, useState} from "react";

/** Keep the form editable when its write receipt is lost. Only its inventory read is repeated. */
export function useSaveRecovery<T>(
  pending: T | null, connected: boolean, inventory: unknown,
  actions: {release: () => void; refresh: () => void; describe: (operation: T) => string},
) {
  const [lost, setLost] = useState<{operation: T; previous: unknown} | null>(null);
  const latest = useRef({inventory, actions});
  latest.current = {inventory, actions};
  useEffect(() => {
    if (!pending) return;
    const interrupt = () => {
      setLost({operation: pending, previous: latest.current.inventory});
      latest.current.actions.release();
    };
    if (!connected) { interrupt(); return; }
    const timer = setTimeout(interrupt, 20_000);
    return () => clearTimeout(timer);
  }, [pending, connected]);
  useEffect(() => {
    if (lost && connected) latest.current.actions.refresh();
  }, [lost, connected]);
  const waiting = !!lost && (!connected || inventory == null || inventory === lost.previous);
  return {
    waiting,
    message: lost ? waiting ? "Save confirmation was lost. Waiting for refreshed settings."
      : actions.describe(lost.operation) : "",
    reset: () => setLost(null),
  };
}
