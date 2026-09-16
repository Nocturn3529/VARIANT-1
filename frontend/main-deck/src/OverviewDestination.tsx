/**
 * React owns VARIANT-1's four inference-observability widgets: Local LLM
 * inference, Live Performance, API Cost & Limits, and Model Usage. The
 * request-receipt inspector is their drill-down.
 */
import {useEffect, useState} from "react";
import {useChatState} from "./chatStore";
import {kernelStatusLabel} from "./RuntimeOverlay";
import {LocalInferenceWidget} from "./overview/LocalInferenceWidget";
import {LivePerformanceWidget} from "./overview/LivePerformanceWidget";
import {ApiCostLimitsWidget} from "./overview/ApiCostLimitsWidget";
import {ModelUsageWidget} from "./overview/ModelUsageWidget";
import {
  requestTelemetry,
  useOverviewTelemetry,
  type ModelRequestReceipt,
  type OverviewTelemetry,
} from "./overviewStore";
import {EmptyState} from "./ui/EmptyState";

const tokenFormatter = new Intl.NumberFormat(undefined, {maximumFractionDigits: 0});

function formatTokens(value: number | null): string {
  return value === null ? "—" : tokenFormatter.format(value);
}

function formatPair(source: number | null, rendered: number | null): string {
  if (source === null && rendered === null) return "—";
  if (source === null) return formatTokens(rendered);
  if (rendered === null) return formatTokens(source);
  return source === rendered ? formatTokens(rendered) : `${formatTokens(source)} → ${formatTokens(rendered)}`;
}

function RequestReceiptCard({receipt}: {receipt: ModelRequestReceipt}) {
  const date = receipt.capturedAt ? new Date(receipt.capturedAt > 1e10 ? receipt.capturedAt : receipt.capturedAt * 1000) : null;
  const validDate = date && !Number.isNaN(date.getTime()) ? date : null;
  const usage = receipt.usage;
  const unavailable = !usage?.linked || ["unknown","unavailable","missing"].includes(usage.measurement)
    || ["unknown","unavailable","missing"].includes(usage.status.toLowerCase())
    || (!usage.providerReported && !usage.estimated)
    || [usage.totalTokens,usage.promptTokens,usage.completionTokens].every(value=>value===null);
  const usageLabel = unavailable ? "Usage unavailable" : usage.estimated ? "Estimated usage" : "Provider-reported usage";
  const usageTokens = (value:number|null) => value===null ? "—" : `${usage?.estimated ? "≈ " : ""}${formatTokens(value)}`;
  const lineage = receipt.contextLineage;
  const transforms = [...new Set([...(lineage?.transformKinds || []), ...receipt.projectionTransformKinds])];
  const privacyLabel = receipt.privacy.status === "metadata_only" ? "Metadata only" : receipt.privacy.status === "retained_data" ? "Retained fields" : "Unknown";
  const privacyDetail = receipt.privacy.status === "metadata_only"
    ? "Prompt and tool values excluded"
    : receipt.privacy.status === "retained_data"
      ? "Source reports retained content; values are not loaded here"
      : "Retention policy was not reported; values are not loaded here";
  return <article className="overview-request-card deck-section" data-manifest-id={receipt.manifestId}>
    <header className="overview-request-card__header"><div><span>{[receipt.route.physicalMode, receipt.route.provider, receipt.route.apiStyle].filter(Boolean).join(" / ") || "route unavailable"}</span><strong>{receipt.route.model}</strong></div><div className="overview-request-card__attempt"><b>Attempt {receipt.attempt || 1}</b><time dateTime={validDate?.toISOString()}>{validDate ? validDate.toLocaleTimeString() : "time unavailable"}</time></div></header>
    <div className="overview-request-card__metrics deck-data-row">
      <div className="deck-data-cell"><span className="deck-data-cell__label">Messages</span><strong className="deck-data-cell__value">{formatPair(receipt.counts.sourceMessages, receipt.counts.renderedMessages)}</strong></div>
      <div className="deck-data-cell"><span className="deck-data-cell__label">Tools</span><strong className="deck-data-cell__value">{formatPair(receipt.counts.requestedTools, receipt.counts.renderedTools)}</strong></div>
      <div className="deck-data-cell"><span className="deck-data-cell__label">Images</span><strong className="deck-data-cell__value">{formatPair(receipt.counts.requestedImages, receipt.counts.renderedImages)}</strong></div>
      <div className={`overview-request-card__budget deck-data-cell${receipt.budget.overBudget ? " is-alert" : ""}`}><span className="deck-data-cell__label">Budget margin</span><strong className="deck-data-cell__value">{receipt.budget.remainingMarginTokens === null ? "Not available" : formatTokens(receipt.budget.remainingMarginTokens)}</strong></div>
    </div>
    <div className="overview-request-card__lineage deck-data-row">
      <div className="deck-data-cell"><span className="deck-data-cell__label">Context selection</span><strong className="deck-data-cell__value">{lineage ? `${formatTokens(lineage.selectedCount)} selected · ${formatTokens(lineage.droppedCount)} dropped` : "Not recorded"}</strong><small className="deck-data-cell__detail">{transforms.length ? transforms.join(" · ") : "No projection transforms recorded"}</small></div>
      <div className="deck-data-cell" data-usage-provenance={unavailable ? "unavailable" : usage.estimated ? "estimated" : "provider_reported"}><span className="deck-data-cell__label">{usageLabel}</span><strong className="deck-data-cell__value">{unavailable ? "—" : usage.totalTokens!==null ? `${usageTokens(usage.totalTokens)} tokens` : "Total unavailable"}</strong><small className="deck-data-cell__detail">{unavailable ? "Token counts not available" : `${usageTokens(usage.promptTokens)} in · ${usageTokens(usage.completionTokens)} out${usage.costUsd != null ? ` · ${usage.estimated ? "≈ " : ""}$${usage.costUsd.toFixed(4)}` : ""}`}</small></div>
      <div className={`overview-request-card__privacy deck-data-cell is-${receipt.privacy.status}`}><span className="deck-data-cell__label">Privacy</span><strong className="deck-data-cell__value">{privacyLabel}</strong><small className="deck-data-cell__detail">{privacyDetail}</small></div>
    </div>
  </article>;
}

function ModelRequestInspector({telemetry}: {telemetry: OverviewTelemetry}) {
  const receipts = telemetry.modelRequests.slice(0, 12);
  const deliveryIssues = telemetry.modelRequestWindow.droppedEvents + telemetry.modelRequestWindow.publishFailures;
  const metadataOnly = receipts.filter(receipt => receipt.privacy.status === "metadata_only").length;
  const budgetAlerts = receipts.filter(receipt => receipt.budget.overBudget).length;
  const statusState = deliveryIssues ? "warning" : receipts.length ? "live" : "waiting";
  const statusLabel = deliveryIssues ? "Delivery issues" : receipts.length ? "Receipt stream live" : "Awaiting requests";
  return <section className="overview-request-inspector deck-instrument" aria-labelledby="overview-request-title">
    <header className="overview-request-inspector__header deck-instrument__header">
      <div className="overview-request-inspector__heading deck-instrument__heading">
        <span className="deck-instrument__eyebrow">Overview / Request receipts</span>
        <div className="deck-instrument__title-row">
          <h2 className="deck-instrument__title" id="overview-request-title">Latest request attempts</h2>
          <span className="deck-instrument__status" data-state={statusState}><i />{statusLabel}</span>
        </div>
        <p className="deck-instrument__description">Provider-ready structure, context selection, token usage, and privacy metadata.</p>
      </div>
      <div className="overview-request-inspector__window deck-instrument__utility">
        <strong>{receipts.length}</strong>
        <span>shown · {telemetry.modelRequests.length} retained</span>
      </div>
    </header>
    {deliveryIssues > 0 && <p className="overview-request-inspector__warning">Receipt delivery reported {telemetry.modelRequestWindow.droppedEvents} dropped and {telemetry.modelRequestWindow.publishFailures} failed publications.</p>}
    <div className="overview-request-summary deck-metric-rail" aria-label="Request receipt summary">
      <article className="deck-metric"><span className="deck-metric__label">Visible attempts</span><strong className="deck-metric__value">{receipts.length}</strong><small className="deck-metric__detail">Newest of {telemetry.modelRequests.length} retained</small></article>
      <article className="deck-metric"><span className="deck-metric__label">Metadata only</span><strong className="deck-metric__value">{metadataOnly}</strong><small className="deck-metric__detail">Prompt and tool values excluded</small></article>
      <article className={`deck-metric${budgetAlerts ? " is-alert" : ""}`}><span className="deck-metric__label">Budget alerts</span><strong className="deck-metric__value">{budgetAlerts}</strong><small className="deck-metric__detail">Across the visible attempts</small></article>
      <article className={`deck-metric${deliveryIssues ? " is-alert" : ""}`}><span className="deck-metric__label">Delivery health</span><strong className="deck-metric__value">{deliveryIssues ? `${deliveryIssues} issues` : "Healthy"}</strong><small className="deck-metric__detail">{deliveryIssues ? `${telemetry.modelRequestWindow.droppedEvents} dropped · ${telemetry.modelRequestWindow.publishFailures} failed` : "No receipt loss reported"}</small></article>
    </div>
    <div className="overview-request-list deck-data-list" aria-live="polite">{receipts.length ? receipts.map(receipt => <RequestReceiptCard key={receipt.manifestId} receipt={receipt}/>) : <EmptyState tone="panel" title="No model requests captured yet" description="Local and cloud request receipts appear after inference begins." />}</div>
  </section>;
}

export function OverviewDestination() {
  const telemetry = useOverviewTelemetry();
  const chat = useChatState();
  const [tab, setTab] = useState("summary");
  useEffect(() => {
    if (telemetry.active) requestTelemetry({notify: true});
  }, [telemetry.active]);
  const latest = telemetry.modelRequests[0];
  const metric = (value: unknown, unit = "") => typeof value === "number" && Number.isFinite(value) ? `${value.toLocaleString(undefined, {maximumFractionDigits: 1})}${unit}` : "—";
  return <div className="overview-scroll overview-shell overview-widget-shell deck-destination-scroll">
    <nav className="utility-tabs" aria-label="Overview details">
      {["summary", "inference", "system", "cloud", "models", "requests"].map(id => <button type="button" key={id} aria-pressed={tab === id} onClick={() => setTab(id)}>{id[0].toUpperCase() + id.slice(1)}</button>)}
    </nav>
    <div className="overview-page deck-destination-page">
      {tab === "summary" ? <div className="overview-compact">
        <div className="overview-compact__status"><strong>{kernelStatusLabel(chat.connected, chat.runtime?.kernelState)}</strong><span>{chat.connected ? "Backend connected locally" : "Backend offline"}</span></div>
        <dl className="overview-compact__metrics">
          <div><dt>Decode</dt><dd>{metric(telemetry.inference?.decode_tps, " tok/s")}</dd></div>
          <div><dt>First token</dt><dd>{metric(telemetry.inference?.ttft_ms, " ms")}</dd></div>
          <div><dt>Request receipts</dt><dd>{telemetry.modelRequests.length}</dd></div>
        </dl>
        <dl className="runtime-details__rows">
          <div><dt>Python session</dt><dd>{chat.runtime ? `Generation ${chat.runtime.kernelGeneration} · ${chat.runtime.selectedCategoryId || "no category"}` : "Not started"}</dd></div>
          <div><dt>Latest model</dt><dd>{latest?.route.model || "No request captured"}</dd></div>
          <div><dt>Route</dt><dd>{latest ? [latest.route.physicalMode, latest.route.provider].filter(Boolean).join(" / ") : "—"}</dd></div>
          <div><dt>Latest token usage</dt><dd>{metric(latest?.usage?.totalTokens)}</dd></div>
          <div><dt>Latest cost</dt><dd>{latest?.usage?.costUsd == null ? "Not reported" : `$${latest.usage.costUsd.toFixed(4)}`}</dd></div>
        </dl>
        <button type="button" className="overview-refresh" onClick={() => requestTelemetry({notify: true})}>Refresh telemetry</button>
      </div> : null}
      <div className="overview-widget-stack">
        {tab === "inference" ? <LocalInferenceWidget telemetry={telemetry.inference} onRefresh={() => requestTelemetry({notify: true})}/> : null}
        {tab === "system" ? <LivePerformanceWidget telemetry={telemetry.hardware}/> : null}
        {tab === "cloud" ? <ApiCostLimitsWidget telemetry={telemetry.cloudUsage}/> : null}
        {tab === "models" ? <ModelUsageWidget telemetry={telemetry.modelUsage}/> : null}
        {tab === "requests" ? <ModelRequestInspector telemetry={telemetry}/> : null}
      </div>
    </div>
  </div>;
}
