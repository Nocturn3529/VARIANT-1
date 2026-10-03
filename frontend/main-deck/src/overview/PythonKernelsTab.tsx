import {useEffect, useState} from "react";
import {useChatState} from "../chatStore";
import {KernelGlyph} from "../motion/KernelGlyph";
import {Button} from "../ui/Button";
import {Icon} from "../ui/Icon";
import {
  canReleaseKernel, chatRuntimeAction, kernelCommand, loadKernelHistory, refreshKernelInventory, releaseKernel,
  useKernelInventory, type KernelInventoryRow,
} from "../kernelInventoryStore";

/**
 * Overview › Python: every live kernel at once. A concurrency strip (kernels,
 * running cells, subagent slots, CPU, memory) above one row per kernel with
 * its current cell; a row opens to its recent cells and actions.
 */
const POLL_MS = 2000;

/** The displayed chat's kernel in words; a stale "ready" never hides an offline backend. */
export function kernelStatusLabel(connected: boolean, kernelState?: string): string {
  if (!connected) return "Kernel offline";
  if (kernelState === "ready") return "Kernel ready";
  if (kernelState === "busy") return "Kernel working";
  if (!kernelState || kernelState === "absent") return "Kernel idle";
  return `Kernel ${kernelState.replace(/_/g, " ")}`;
}

const STATE: Record<string, {label: string; tone?: "live" | "positive" | "warning" | "danger"}> = {
  ready: {label: "Idle", tone: "positive"},
  starting: {label: "Starting", tone: "warning"},
  stopping: {label: "Stopping", tone: "warning"},
  unhealthy: {label: "Unhealthy", tone: "danger"},
  close_failed: {label: "Close failed", tone: "danger"},
  absent: {label: "Absent"},
};
const EXIT: Record<string, string> = {
  capacity_eviction: "Evicted for capacity", idle_or_absolute_eviction: "Retired after its lifetime",
  operator_release: "Released", operator_restart: "Restarted", boot_timeout: "Boot timed out",
  explicit_restart_kernel: "Restarted from chat", capsule_restore_unknown_effect: "Restore effect unknown", chat_closed: "Chat closed",
};

function bytes(value: number | null): string {
  if (value === null) return "Unknown";
  const mib = value / 1024 / 1024;
  return mib >= 1024 ? `${(mib / 1024).toFixed(2)} GiB` : `${mib.toFixed(mib >= 100 ? 0 : 1)} MiB`;
}
function span(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}
function ms(value: number | null): string {
  if (value === null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}s` : `${Math.round(value)}ms`;
}
const cellTone = (status: string) => status === "completed" || status === "ok" || status === "succeeded" ? "positive" : status === "running" ? "live" : status ? "danger" : undefined;

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function KernelRow({row, now, current, agentName, open, onToggle}: {
  row: KernelInventoryRow; now: number; current: boolean; agentName: string; open: boolean; onToggle: () => void;
}) {
  const inventory = useKernelInventory();
  const chat = useChatState();
  const [confirm, setConfirm] = useState(false);
  const history = inventory.history[row.chatId];
  const action = inventory.actions[row.chatId];
  const closing = !!inventory.closing[row.chatId];
  // An open row reads its cells again once the backend reconnects.
  useEffect(() => { if (open) loadKernelHistory(row.chatId); }, [open, row.chatId, row.lastCell?.executionId, inventory.connected]);
  const state = row.busy ? {label: "Running", tone: "live" as const} : STATE[row.state] || {label: row.state.replace(/_/g, " ")};
  const cell = row.currentCell;
  const runtime = current ? chat.runtime : null;
  return <li className={`python-kernel${open ? " is-open" : ""}${row.busy ? " is-busy" : ""}`} data-chat-id={row.chatId}>
    <div className="python-kernel__row">
      <button type="button" className="python-kernel__toggle" aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} cells for ${row.title}`} onClick={onToggle}>
        <Icon name="chevron" className={open ? "is-expanded" : ""}/>
      </button>
      <div className="python-kernel__who">
        <strong title={row.title}>{agentName || row.title}</strong>
        <span>{current ? <em>This chat</em> : null}{agentName ? <em>Subagent</em> : null}<small>Gen {row.generation}</small></span>
      </div>
      <div className="python-kernel__now">
        <em className="deck-status" data-tone={state.tone}>{state.label}</em>
        {cell ? <code title={cell.label}>{cell.label || "Cell running"}</code>
          : row.lastCell ? <span>Last cell {row.lastCell.status || "done"} · {ms(row.lastCell.durationMs)}{row.lastCell.errorCode ? ` · ${row.lastCell.errorCode}` : ""}</span>
            : <span>No cells yet</span>}
        {cell?.startedAt ? <time>{span(now - cell.startedAt)}</time> : null}
        {row.queuedCells ? <b title="Cells waiting for this kernel">+{row.queuedCells} queued</b> : null}
      </div>
      <span className="python-kernel__metric" title="Interpreter CPU (one core = 100%)">{row.cpuPercent === null ? "—" : `${row.cpuPercent.toFixed(row.cpuPercent >= 10 ? 0 : 1)}%`}</span>
      <span className="python-kernel__metric" title="Owned process tree memory">{bytes(row.treeMemoryBytes ?? row.memoryBytes)}</span>
      <span className="python-kernel__metric" title="Kernel age · idle">{span(row.ageSeconds)}<small>{row.busy ? "busy" : `idle ${span(row.idleSeconds)}`}</small></span>
      <div className="python-kernel__actions">
        {row.busy ? <Button tone="quiet" disabled={!inventory.connected || !!action} onClick={() => kernelCommand(row.chatId, "interrupt")}>{action?.kind === "interrupt" ? "Interrupting…" : "Interrupt"}</Button> : null}
        <Button tone="quiet" disabled={!inventory.connected || !canReleaseKernel(row) || closing} onClick={() => setConfirm(true)}>{closing ? "Closing…" : "Release session"}</Button>
      </div>
    </div>
    {confirm ? <div className="kernel-release-confirm python-kernel__confirm" role="group" aria-label={`Close ${row.title}`}>
      <p>Closing ends this generation's live objects and background Python work. Saved chat history stays available; checkpoint restoration is not guaranteed.</p>
      <Button disabled={!inventory.connected || !canReleaseKernel(row) || closing} onClick={() => {if (releaseKernel(row.chatId, row.generation)) setConfirm(false);}}>Close kernel</Button>
      <Button tone="quiet" onClick={() => setConfirm(false)}>Cancel</Button>
    </div> : null}
    {open ? <div className="python-kernel__detail">
      <ol className="python-kernel__cells" aria-label={`Recent cells for ${row.title}`}>
        {history?.items.length ? [...history.items].reverse().map(item => <li key={item.sequence || item.executionId}>
          <em className="deck-status" data-tone={cellTone(item.status)}>{item.status || "unknown"}</em>
          <code title={item.label}>{item.label || "Cell"}</code>
          <span>{ms(item.durationMs)}</span>
          <time>{item.completedAt ? new Date(item.completedAt * 1000).toLocaleTimeString() : item.startedAt ? new Date(item.startedAt * 1000).toLocaleTimeString() : ""}</time>
          {item.errorCode ? <small>{item.errorCode}</small> : null}
        </li>) : <li className="is-empty">{history?.requestId ? "Reading cells…" : history?.error || "No cells recorded for this kernel yet."}</li>}
      </ol>
      <aside className="python-kernel__facts">
        <dl>
          <div><dt>Processes</dt><dd>{row.processes ?? "—"}{row.treeComplete ? "" : " · partial"}</dd></div>
          <div><dt>Interpreter</dt><dd>{bytes(row.memoryBytes)}</dd></div>
          <div><dt>Last exit</dt><dd>{row.lastExit ? `${EXIT[row.lastExit.reason] || row.lastExit.reason.replace(/_/g, " ")} · gen ${row.lastExit.generation}` : "None this session"}</dd></div>
          {runtime ? <>
            <div><dt>Session tools</dt><dd>{runtime.activeSlots} active · {runtime.probationSlots} probation</dd></div>
            <div><dt>Mutation</dt><dd className={`mutation-indicator${runtime.mutationEffectiveEnabled ? " is-enabled" : ""}`}>{runtime.mutationEffectiveEnabled ? "Enabled" : "Off"}</dd></div>
          </> : null}
        </dl>
        <div className="python-kernel__buttons">
          <Button tone="quiet" disabled={!inventory.connected || !!action || row.busy} onClick={() => kernelCommand(row.chatId, "restart")}><Icon name="refresh"/>{action?.kind === "restart" ? "Restarting…" : "Restart kernel"}</Button>
          {row.busy ? <Button tone="quiet" disabled={!inventory.connected} onClick={() => chatRuntimeAction(row.chatId, "stop_cell")}><Icon name="stop"/>Stop turn</Button> : null}
          <Button tone="quiet" disabled={!inventory.connected || row.busy} onClick={() => chatRuntimeAction(row.chatId, "reset_session_tools")}>Reset session tools</Button>
        </div>
      </aside>
    </div> : null}
  </li>;
}

export function PythonKernelsTab() {
  const inventory = useKernelInventory();
  const chat = useChatState();
  const [openId, setOpenId] = useState<string | null>(null);
  useEffect(() => {
    if (!inventory.connected) return;
    refreshKernelInventory();
    const timer = setInterval(() => { if (!document.hidden) refreshKernelInventory(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [inventory.connected]);
  const rows = [...inventory.items].sort((left, right) => Number(right.busy) - Number(left.busy) || (left.idleSeconds ?? 0) - (right.idleSeconds ?? 0));
  const now = useNow(rows.some(row => row.busy));
  const running = rows.filter(row => row.busy).length;
  const queued = rows.reduce((sum, row) => sum + row.queuedCells, 0);
  const cpu = rows.reduce((sum, row) => sum + (row.cpuPercent || 0), 0);
  const memory = rows.reduce((sum, row) => sum + (row.treeMemoryBytes ?? row.memoryBytes ?? 0), 0);
  const capacity = inventory.capacity;
  const agents = new Map(chat.agentTeam.agents.map(agent => [agent.chatId, agent.name]));
  return <section className="python-kernels deck-instrument" aria-labelledby="python-kernels-title">
    <header className="python-kernels__header deck-instrument__header">
      <KernelGlyph seed={`${chat.sessionId}:${chat.runtime?.kernelGeneration}`} size={30} mutation={chat.runtime?.mutationEffectiveEnabled}
        phase={!chat.connected ? "offline" : running ? "running" : "idle"}/>
      <div className="deck-instrument__heading">
        <div className="deck-instrument__title-row">
          <h2 className="deck-instrument__title" id="python-kernels-title">Python kernels</h2>
          <span className="deck-instrument__status" data-state={!inventory.connected ? "waiting" : running ? "live" : undefined}><i/>{!inventory.connected ? "Backend offline" : running ? `${running} running` : rows.length ? "All idle" : "No live kernels"}</span>
        </div>
        <p className="python-kernels__self">This chat · {kernelStatusLabel(chat.connected, chat.runtime?.kernelState)}{chat.runtime?.kernelGeneration ? ` · generation ${chat.runtime.kernelGeneration}` : ""}</p>
      </div>
      <Button tone="quiet" disabled={!inventory.connected || !!inventory.requestId} onClick={refreshKernelInventory}>Refresh</Button>
    </header>
    <div className="python-kernels__strip deck-metric-rail" aria-label="Concurrency">
      <article className="deck-metric"><span className="deck-metric__label">Live kernels</span><strong className="deck-metric__value">{rows.length}</strong><small className="deck-metric__detail" title="No kernel limit; two kernels boot at a time">No limit</small></article>
      <article className="deck-metric"><span className="deck-metric__label">Running now</span><strong className="deck-metric__value">{running}</strong><small className="deck-metric__detail">{queued ? `${queued} cells queued` : "No cells queued"}</small></article>
      <article className="deck-metric"><span className="deck-metric__label">Subagent slots</span><strong className="deck-metric__value">{capacity ? `${capacity.active} / ${capacity.maxActive}` : "—"}</strong><small className="deck-metric__detail">{capacity ? `${capacity.admitted} / ${capacity.maxAdmitted} admitted · depth ${capacity.maxDepth} · ${capacity.mode}` : "Child runtime unavailable"}</small></article>
      <article className="deck-metric"><span className="deck-metric__label">CPU</span><strong className="deck-metric__value">{rows.some(row => row.cpuPercent !== null) ? `${cpu.toFixed(cpu >= 10 ? 0 : 1)}%` : "—"}</strong><small className="deck-metric__detail">Interpreters · one core = 100%</small></article>
      <article className="deck-metric"><span className="deck-metric__label">Memory</span><strong className="deck-metric__value">{rows.length ? bytes(memory) : "—"}</strong><small className="deck-metric__detail">Owned process trees</small></article>
    </div>
    {inventory.error ? <p className="python-kernels__error" role="alert">{inventory.error}</p> : null}
    {!inventory.connected && rows.length ? <p className="python-kernels__note" role="status">Backend offline. Showing the last observed kernels.</p> : null}
    {rows.length ? <ul className="python-kernels__list" aria-label="Live kernels">
      <li className="python-kernels__columns" aria-hidden="true"><span/><span>Session</span><span>Now</span><span>CPU</span><span>Memory</span><span>Age</span><span/></li>
      {rows.map(row => <KernelRow key={`${row.chatId}:${row.generation}`} row={row} now={now} current={row.chatId === chat.sessionId}
        agentName={agents.get(row.chatId) || ""} open={openId === row.chatId} onToggle={() => setOpenId(id => id === row.chatId ? null : row.chatId)}/>)}
    </ul> : <div className="python-kernels__empty" role="status">
      <strong>{inventory.requestId ? "Reading live kernels…" : "No live Python kernels"}</strong>
      <span>A kernel starts with the first Python cell in a chat and keeps its state until it is released.</span>
    </div>}
  </section>;
}
