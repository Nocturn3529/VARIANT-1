import {useEffect, useState} from "react";
import {useKernelInventory, refreshKernelInventory, releaseKernel, canReleaseKernel} from "./kernelInventoryStore";
import {Button} from "./ui/Button";

const memory = (bytes: number | null) => bytes === null ? "Unknown" : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
const age = (seconds: number | null) => seconds === null ? "Unknown" : seconds < 60 ? `${Math.floor(seconds)}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${(seconds / 3600).toFixed(1)}h`;

export function KernelInventory() {
  const state = useKernelInventory();
  const [confirm, setConfirm] = useState<string | null>(null);
  useEffect(() => {
    if (!state.connected) return;
    refreshKernelInventory();
    const timer = setInterval(refreshKernelInventory, 5000);
    return () => clearInterval(timer);
  }, [state.connected]);
  return <section className="kernel-inventory" aria-label="Retained Python sessions">
    <header><h3>Retained Python sessions</h3><Button tone="quiet" disabled={!state.connected || !!state.requestId} onClick={refreshKernelInventory}>Refresh</Button></header>
    <p>Sessions retain their live Python state until explicitly closed or interrupted by a lifecycle event.</p>
    {!state.connected ? <p role="status">Backend offline. Showing the last observed sessions.</p> : null}
    {state.error ? <p role="alert">{state.error}</p> : null}
    {!state.items.length ? <p role="status">{state.requestId ? "Reading live sessions…" : "No live sessions observed."}</p> : null}
    <ul>{state.items.map(row => <li key={`${row.chatId}:${row.generation}`}>
      <strong>{row.title}</strong><small>Generation {row.generation} · {row.busy ? "Run active" : row.state} · Age {age(row.ageSeconds)} · Idle {age(row.idleSeconds)}</small>
      <dl><div><dt>Interpreter memory</dt><dd>{memory(row.memoryBytes)}</dd></div>
        <div><dt>Owned process tree</dt><dd>{memory(row.treeMemoryBytes)}{row.processes === null ? "" : ` · ${row.processes} processes`}{row.treeComplete ? "" : " · Partial or unavailable"}</dd></div></dl>
      <small>{row.measurementSource.startsWith("live_") ? "Live sample" : row.sampledAt ? `Last worker snapshot: ${new Date(row.sampledAt * 1000).toLocaleTimeString()}` : "Measurement unavailable"}. Tree RSS sums may count shared memory more than once.</small>
      {confirm === `${row.chatId}:${row.generation}` ? <div className="kernel-release-confirm" role="group" aria-label={`Close ${row.title}`}>
        <p>Closing ends this generation's live objects and background Python work. Saved chat history stays available; checkpoint restoration is not guaranteed.</p>
        <Button disabled={!state.connected || !canReleaseKernel(row) || !!state.closing[row.chatId]} onClick={() => {if (releaseKernel(row.chatId, row.generation)) setConfirm(null);}}>Close kernel</Button>
        <Button tone="quiet" onClick={() => setConfirm(null)}>Cancel</Button>
      </div> : <Button tone="quiet" disabled={!state.connected || !canReleaseKernel(row) || !!state.closing[row.chatId]} onClick={() => setConfirm(`${row.chatId}:${row.generation}`)}>{state.closing[row.chatId] ? "Closing…" : "Release session"}</Button>}
    </li>)}</ul>
  </section>;
}
