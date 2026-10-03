import {useEffect, useRef, useState, type CSSProperties, type ReactNode} from "react";
import {asRecord as record} from "../state/storePrimitives";
import type {OverviewTelemetry} from "../overviewStore";

/**
 * Overview at a glance: one tile per section, each opening its tab. Tiles
 * show the one number that matters plus a small graphic in that section's
 * graph hue; everything else stays monochrome.
 */
export type OverviewTab = "summary" | "inference" | "system" | "cloud" | "models" | "requests";

const HISTORY = 40;

function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function compact(value: number | null): string {
  if (value === null) return "—";
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(value >= 1e4 ? 0 : 1)}K`;
  return Math.round(value).toLocaleString();
}

function money(value: number | null, status = "exact"): string {
  if (value === null || status === "unavailable") return "—";
  return `$${value.toFixed(value > 0 && value < .01 ? 4 : 2)}${status === "estimated" || status === "partial" ? "*" : ""}`;
}

function modelName(value: unknown): string {
  const name = String(value || "").replaceAll("\\", "/").split("/").pop() || "";
  return name.replace(/\.gguf$/i, "");
}

/** Decode history sampled while the Summary is open (once a second). */
function useDecodeHistory(inference: Record<string, unknown> | null): number[] {
  const latest = useRef(inference);
  const [history, setHistory] = useState<number[]>(() => Array(HISTORY).fill(0));
  useEffect(() => { latest.current = inference; }, [inference]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      const state = String(latest.current?.state || "");
      const value = state === "decode" ? numeric(latest.current?.decode_tps) || 0 : 0;
      setHistory(current => [...current.slice(1), value]);
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);
  return history;
}

function Sparkline({values}: {values: number[]}) {
  const max = Math.max(1, ...values);
  const points = values.map((value, index) => `${(index / (values.length - 1) * 100).toFixed(1)},${(30 - value / max * 26).toFixed(1)}`).join(" ");
  return <svg className="overview-tile__spark" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true">
    <polyline points={`0,32 ${points} 100,32`} className="is-area"/>
    <polyline points={points} className="is-line"/>
  </svg>;
}

function Meter({label, value, hue}: {label: string; value: number | null; hue: string}) {
  const width = value === null ? 0 : Math.max(0, Math.min(100, value));
  return <div className="overview-meter" style={{"--overview-hue": hue} as CSSProperties}>
    <span>{label}</span>
    <i><b style={{width: `${width}%`}}/></i>
    <strong>{value === null ? "—" : `${Math.round(value)}%`}</strong>
  </div>;
}

function Tile({tab, title, status, tone, onOpen, children}: {
  tab: OverviewTab;
  title: string;
  status: string;
  tone?: "live" | "positive" | "warning";
  onOpen: (tab: OverviewTab) => void;
  children: ReactNode;
}) {
  return <button type="button" className="overview-tile" data-tab={tab} aria-label={`${title}: open details`} onClick={() => onOpen(tab)}>
    <header><span className="overview-tile__title">{title}</span><em className="deck-status" data-tone={tone}>{status}</em></header>
    {children}
  </button>;
}

export function OverviewSummary({telemetry, kernelLabel, connected, session, onOpen, onRefresh}: {
  telemetry: OverviewTelemetry;
  kernelLabel: string;
  connected: boolean;
  session: string;
  onOpen: (tab: OverviewTab) => void;
  onRefresh: () => void;
}) {
  const inference = telemetry.inference || {};
  const decodeHistory = useDecodeHistory(telemetry.inference);
  const state = String(inference.state || "idle");
  const engineReady = Boolean(inference.engine_ready);
  const inferenceStatus = state === "decode" ? "Generating" : state === "prefill" ? "Prefilling" : state === "error" ? "Request error" : engineReady ? "Idle" : "Engine offline";
  const decode = numeric(inference.decode_tps);
  const ttft = numeric(inference.ttft_ms);

  const hardware = record(telemetry.hardware);
  const gpu = record(Array.isArray(hardware.gpus) ? hardware.gpus[0] : null);
  const cpuPct = numeric(record(hardware.cpu).utilization_pct) ?? numeric(hardware.cpu_percent);
  const memoryPct = numeric(record(hardware.memory).utilization_pct) ?? numeric(hardware.memory_percent);
  const diskPct = numeric(record(hardware.disk).utilization_pct);
  const gpuPct = numeric(gpu.utilization_pct) ?? numeric(hardware.gpu_percent);

  const cloudEnvelope = record(telemetry.cloudUsage);
  const cloud = record(record(cloudEnvelope.cloud_usage ? cloudEnvelope.cloud_usage : cloudEnvelope).total);
  const costStatus = String(cloud.cost_status || "unavailable");
  const budget = numeric(cloud.budget_usd);
  const budgetUsed = numeric(cloud.budget_used_pct);
  const monthElapsed = numeric(cloud.month_elapsed_pct);
  const overPace = budgetUsed !== null && monthElapsed !== null && budgetUsed > monthElapsed;

  const usageEnvelope = record(telemetry.modelUsage);
  const totals = record(record(usageEnvelope.model_usage ? usageEnvelope.model_usage : usageEnvelope).totals);
  const local = numeric(totals.local_requests) || 0;
  const cloudRequests = numeric(totals.cloud_requests) || 0;
  const localShare = local + cloudRequests ? Math.round(local / (local + cloudRequests) * 100) : null;

  const latest = telemetry.modelRequests[0];
  const deliveryIssues = telemetry.modelRequestWindow.droppedEvents + telemetry.modelRequestWindow.publishFailures;
  const latestTime = latest?.capturedAt ? new Date(latest.capturedAt > 1e10 ? latest.capturedAt : latest.capturedAt * 1000) : null;

  return <div className="overview-summary" aria-label="Overview at a glance">
    <div className="overview-summary__status">
      <span className="deck-status" data-tone={connected ? "positive" : undefined}>{kernelLabel}</span>
      <span>{connected ? "Backend connected locally" : "Backend offline"}</span>
      <span>{session}</span>
      <button type="button" className="overview-summary__refresh" onClick={onRefresh}>Refresh</button>
    </div>
    <div className="overview-summary__grid">
      <Tile tab="inference" title="Inference" status={inferenceStatus} tone={state === "decode" || state === "prefill" ? "live" : state === "error" ? "warning" : engineReady ? "positive" : undefined} onOpen={onOpen}>
        <strong className="overview-tile__value">{decode ? decode.toFixed(1) : "—"}<small>tok/s decode</small></strong>
        <Sparkline values={decodeHistory}/>
        <p className="overview-tile__facts"><span>{modelName(inference.model) || "Local model"}</span><span>{ttft ? `${Math.round(ttft)} ms first token` : "No request yet"}</span></p>
      </Tile>
      <Tile tab="system" title="System" status={telemetry.hardware ? "Live" : "Waiting"} tone={telemetry.hardware ? "live" : undefined} onOpen={onOpen}>
        <div className="overview-tile__meters">
          <Meter label="CPU" value={cpuPct} hue="var(--deck-chart-blue)"/>
          <Meter label="Memory" value={memoryPct} hue="var(--deck-chart-violet)"/>
          <Meter label="Disk" value={diskPct} hue="var(--deck-chart-amber)"/>
          <Meter label="GPU" value={gpuPct} hue="var(--deck-chart-teal)"/>
        </div>
      </Tile>
      <Tile tab="cloud" title="Cloud spend" status={budget ? (overPace ? "Ahead of pace" : "On pace") : "No budget set"} tone={budget ? (overPace ? "warning" : "positive") : undefined} onOpen={onOpen}>
        <strong className="overview-tile__value">{money(numeric(cloud.today_cost_usd), costStatus)}<small>today</small></strong>
        <div className="overview-tile__budget" style={{"--overview-hue": "var(--deck-chart-amber)"} as CSSProperties}>
          <i><b style={{width: `${Math.min(100, budgetUsed || 0)}%`}}/>{monthElapsed !== null && budget ? <u style={{left: `${Math.min(100, monthElapsed)}%`}}/> : null}</i>
        </div>
        <p className="overview-tile__facts"><span>{money(numeric(cloud.cost_usd), costStatus)} this month</span><span>{budget ? `${(budgetUsed || 0).toFixed(0)}% of $${budget.toFixed(0)}` : "Set a budget in Settings"}</span></p>
      </Tile>
      <Tile tab="models" title="Models" status="Last 30 days" onOpen={onOpen}>
        <strong className="overview-tile__value">{compact(numeric(totals.tokens))}<small>tokens</small></strong>
        <div className="overview-tile__split" aria-label={localShare === null ? "No routed requests" : `${localShare}% local, ${100 - localShare}% cloud`}>
          <b style={{width: `${localShare ?? 0}%`}}/><b style={{width: `${localShare === null ? 0 : 100 - localShare}%`}}/>
        </div>
        <p className="overview-tile__facts"><span>{compact(numeric(totals.requests))} requests</span><span>{localShare === null ? "No routed requests" : `${localShare}% local · ${100 - localShare}% cloud`}</span></p>
      </Tile>
      <Tile tab="requests" title="Latest request" status={deliveryIssues ? `${deliveryIssues} delivery issues` : latest ? `${telemetry.modelRequests.length} retained` : "Waiting"} tone={deliveryIssues ? "warning" : latest ? "positive" : undefined} onOpen={onOpen}>
        <strong className="overview-tile__value is-text">{latest?.route.model || "No request yet"}</strong>
        <dl className="overview-tile__grid">
          <div><dt>Route</dt><dd>{latest ? [latest.route.physicalMode, latest.route.provider].filter(Boolean).join(" / ") || "—" : "—"}</dd></div>
          <div><dt>Tokens</dt><dd>{latest?.usage?.totalTokens != null ? compact(latest.usage.totalTokens) : "—"}</dd></div>
          <div><dt>Cost</dt><dd>{latest?.usage?.costUsd != null ? money(latest.usage.costUsd) : "—"}</dd></div>
          <div><dt>Captured</dt><dd>{latestTime && !Number.isNaN(latestTime.getTime()) ? latestTime.toLocaleTimeString() : "—"}</dd></div>
          <div><dt>Budget alerts</dt><dd>{telemetry.modelRequests.filter(receipt => receipt.budget.overBudget).length}</dd></div>
          <div><dt>Metadata only</dt><dd>{telemetry.modelRequests.filter(receipt => receipt.privacy.status === "metadata_only").length}</dd></div>
        </dl>
      </Tile>
    </div>
  </div>;
}
