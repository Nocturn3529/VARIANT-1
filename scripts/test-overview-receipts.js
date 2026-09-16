"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const esbuild = require("esbuild");

const root = path.resolve(__dirname, "..");
const source = path.join(root, "frontend", "main-deck", "src", "overviewStore.ts");
const overviewView = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "OverviewDestination.tsx"),
  "utf8",
);
const localInferenceView = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "overview", "LocalInferenceWidget.tsx"),
  "utf8",
);
const livePerformanceView = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "overview", "LivePerformanceWidget.tsx"),
  "utf8",
);
const apiCostView = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "overview", "ApiCostLimitsWidget.tsx"),
  "utf8",
);
const modelUsageView = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "overview", "ModelUsageWidget.tsx"),
  "utf8",
);
const overviewStyles = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "styles", "overview.css"),
  "utf8",
);
const fixtureView = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "fixture.ts"),
  "utf8",
);
const designSystem = fs.readFileSync(
  path.join(root, "frontend", "main-deck", "src", "styles", "design-system.css"),
  "utf8",
);
const inferenceStyles = overviewStyles;
const performanceStyles = overviewStyles;
const apiCostStyles = overviewStyles;
const modelUsageStyles = overviewStyles;
const built = esbuild.buildSync({
  entryPoints: [source],
  bundle: true,
  platform: "node",
  format: "cjs",
  write: false,
  external: ["react"],
}).outputFiles[0].text;

const loaded = new Module(source, module);
loaded.filename = source;
loaded.paths = Module._nodeModulePaths(root);
loaded._compile(built, source);

const {
  MODEL_REQUEST_RECEIPT_LIMIT,
  mergeModelRequestReceipts,
  normalizeModelRequestManifest,
  requestTelemetry,
  setOverviewContext,
} = loaded.exports;

const raw = {
  type: "model:request_manifest",
  schema: "variant1.model_request_manifest.v2",
  manifest_id: "mreq_1",
  logical_call_id: "mcall_1",
  attempt: 2,
  captured_at: 1_753_720_000,
  route: {
    provider: "openai",
    model: "gpt-test",
    physical_mode: "cloud",
    api_style: "responses",
  },
  messages: {
    source: {count: 6},
    rendered: {count: 5},
    ordered_rendered: [{content: "PROMPT_SECRET_MUST_NOT_SURVIVE"}],
  },
  tools: {
    requested_count: 3,
    rendered_count: 2,
    requested: [{name: "tool", value: "TOOL_SECRET_MUST_NOT_SURVIVE"}],
  },
  images: {requested_count: 1, rendered_count: 1},
  budget: {
    estimated_input_tokens_lower_bound: 1200,
    context_limit_tokens: 8000,
    output_reserve_tokens: 1000,
    remaining_margin_tokens: 5800,
    over_budget: false,
  },
  context_lineage: {
    selection: {
      considered: 12,
      kept: 8,
      dropped: 4,
    },
    selections: [
      {
        kind: "history",
        source: "durable_messages",
        trust: "conversation",
        considered: 9,
        kept: 6,
        dropped: 3,
        reason: "SELECTION_REASON_SECRET_MUST_NOT_SURVIVE",
      },
    ],
    items: [
      {
        kind: "memory",
        source: "retrieval",
        decision: "kept",
        reason: "ITEM_REASON_SECRET_MUST_NOT_SURVIVE",
      },
    ],
    transforms: [
      {
        kind: "supersession",
        affected_count: 3,
        removed_value: "LINEAGE_SECRET_MUST_NOT_SURVIVE",
      },
      {kind: "compression", input_count: 6, output_count: 2},
    ],
  },
  usage: {
    linked: true,
    provider_reported: true,
    estimated: false,
    measurement: "provider_reported",
    status: "reported",
    prompt_tokens: 1000,
    completion_tokens: 250,
    total_tokens: 1250,
    cached_tokens: 400,
  },
  provenance: {
    context_receipt_available: true,
    selection_available: true,
    observation_projection_available: true,
    supersession_available: true,
    compression_receipt_available: true,
    usage_available: true,
  },
  privacy: {
    policy: "metadata_only_v2",
    prompt_text_stored: false,
    tool_values_stored: false,
    image_data_stored: false,
    headers_stored: false,
    url_stored: false,
    exact_payload_ref: null,
  },
  unexpected_future_payload: "FUTURE_SECRET_MUST_NOT_SURVIVE",
};

const receipt = normalizeModelRequestManifest(raw);
assert.ok(receipt);
assert.strictEqual(receipt.manifestId, "mreq_1");
assert.deepStrictEqual(receipt.counts, {
  sourceMessages: 6,
  renderedMessages: 5,
  requestedTools: 3,
  renderedTools: 2,
  requestedImages: 1,
  renderedImages: 1,
});
assert.deepStrictEqual(receipt.contextLineage, {
  consideredCount: 12,
  selectedCount: 8,
  droppedCount: 4,
  selections: [{
    kind: "history",
    source: "durable_messages",
    trust: "conversation",
    consideredCount: 9,
    selectedCount: 6,
    droppedCount: 3,
  }],
  items: [{
    kind: "memory",
    source: "retrieval",
    trust: "",
    decision: "kept",
  }],
  transforms: [{
    kind: "supersession",
    inputCount: null,
    outputCount: null,
    affectedCount: 3,
    charsBefore: null,
    charsAfter: null,
  }, {
    kind: "compression",
    inputCount: 6,
    outputCount: 2,
    affectedCount: null,
    charsBefore: null,
    charsAfter: null,
  }],
  transformKinds: ["supersession", "compression"],
});
assert.deepStrictEqual(receipt.provenance, {
  contextReceiptAvailable: true,
  selectionAvailable: true,
  observationProjectionAvailable: true,
  supersessionAvailable: true,
  compressionAvailable: true,
  usageAvailable: true,
});
assert.strictEqual(receipt.usage.totalTokens, 1250);
assert.strictEqual(receipt.usage.providerReported, true);
assert.strictEqual(receipt.privacy.status, "metadata_only");
assert.doesNotMatch(
  JSON.stringify(receipt),
  /PROMPT_SECRET|TOOL_SECRET|LINEAGE_SECRET|SELECTION_REASON_SECRET|ITEM_REASON_SECRET|FUTURE_SECRET/,
  "normalized Overview state must not retain content values",
);

const updated = {...receipt, usage: {...receipt.usage, totalTokens: 1300}};
const older = {...receipt, manifestId: "mreq_older", capturedAt: receipt.capturedAt - 1};
const merged = mergeModelRequestReceipts([receipt, older], [updated]);
assert.strictEqual(merged.length, 2);
assert.strictEqual(merged[0].manifestId, "mreq_1");
assert.strictEqual(merged[0].usage.totalTokens, 1300, "same manifest ID must upsert");

const oversized = Array.from({length: MODEL_REQUEST_RECEIPT_LIMIT + 5}, (_, index) => ({
  ...receipt,
  manifestId: `mreq_${index + 10}`,
  capturedAt: receipt.capturedAt + index,
}));
assert.strictEqual(
  mergeModelRequestReceipts([], oversized).length,
  MODEL_REQUEST_RECEIPT_LIMIT,
  "client receipt state must remain bounded",
);

const sent = [];
setOverviewContext({
  send(message) {
    sent.push(message.type);
    return true;
  },
  notify() {},
});
requestTelemetry();
assert.ok(
  sent.includes("model:request_manifests"),
  "Overview activation/reconnect must request the bounded receipt snapshot",
);
assert.ok(!sent.includes("inference:operations"),
  "Overview must rely on its four dedicated dashboards, not the aggregate operations projection");
assert.ok(
  !fs.existsSync(path.join(root, "assets", "unused-assets", "overview-widgets")),
  "retired standalone Overview sources must be deleted rather than archived beside the product",
);
assert.ok(!fs.existsSync(path.join(root, "frontend", "main-deck", "widgets")),
  "the active Main Deck must not retain the retired standalone widget tree");
assert.match(overviewView, /<LocalInferenceWidget[\s\S]*telemetry=\{telemetry\.inference\}[\s\S]*onRefresh=\{\(\) => requestTelemetry\(\{notify: true\}\)\}/,
  "Local LLM inference must render through the native React component");
assert.match(localInferenceView, /id="overview-refresh"[\s\S]*onClick=\{onRefresh\}/,
  "Overview refresh must live inside the Local LLM inference instrument");
assert.match(overviewView, /<LivePerformanceWidget\s+telemetry=\{telemetry\.hardware\}/,
  "Live Performance must render through the native React component");
assert.match(overviewView, /<ApiCostLimitsWidget\s+telemetry=\{telemetry\.cloudUsage\}/,
  "API Cost & Limits must render through the native React component");
assert.match(overviewView, /<ModelUsageWidget\s+telemetry=\{telemetry\.modelUsage\}/,
  "Model Usage must render through the native React component");
assert.doesNotMatch(overviewView, /<iframe|OverviewDashboardFrame|dashboardUrl|postDashboardMessage/,
  "the four completed React migrations must leave no Overview frame runtime");
for (const metric of [
  "decode_tps", "prompt_tps", "requests_per_second", "rolling_avg_decode_tps",
  "prompt_tokens", "cached_prompt_tokens", "processed_prompt_tokens", "cache_hit_pct",
  "active_requests", "queue_depth", "rolling_completed", "rolling_failed",
  "rolling_success_pct", "ttft_ms", "tpot_ms", "generation_time_s",
  "time_to_last_token_s", "rolling_p95_ttft_ms",
]) {
  assert.match(localInferenceView, new RegExp(`\\b${metric}\\b`),
    `native Local LLM inference must preserve the ${metric} display`);
}
assert.match(localInferenceView, /const POINT_COUNT = 72/,
  "the React throughput chart must preserve the 72-sample 60-second window");
assert.match(localInferenceView, /viewBox="0 0 1120 360"/,
  "the React throughput chart must preserve the original chart geometry");
assert.match(localInferenceView, /onMouseMove=\{showTooltip\}[\s\S]*onMouseLeave=/,
  "the React throughput chart must preserve hover crosshair and tooltip interaction");
assert.match(localInferenceView, /window\.setInterval\([\s\S]*900\)/,
  "the React throughput chart must preserve its live sampling cadence");
for (const metric of [
  "utilization_pct", "variant1_utilization_pct", "logical_processors", "speed_mhz",
  "temperature_c", "power_draw_w", "total_mb", "used_mb", "available_mb",
  "variant1_used_mb", "read_bytes_per_second", "write_bytes_per_second",
  "response_time_ms", "vram_used_mb", "vram_total_mb", "power_limit_w",
]) {
  assert.match(livePerformanceView, new RegExp(`\\b${metric}\\b`),
    `native Live Performance must preserve the ${metric} display`);
}
assert.match(livePerformanceView, /const SAMPLE_COUNT = 60/,
  "Live Performance must retain a one-minute window without oversampling it");
assert.match(livePerformanceView, /viewBox="0 0 960 250"/,
  "Live Performance must preserve the original bar-chart geometry");
assert.match(livePerformanceView, /\}, 1000\);/,
  "Live Performance must sample once per second for its 60-second window");
assert.match(livePerformanceView, /<path className="is-total" d=\{paths\.total\}/,
  "Live Performance must batch total-usage bars into one SVG path");
assert.match(livePerformanceView, /<path className="is-variant1" d=\{paths\.variant1\}/,
  "Live Performance must batch VARIANT-1-usage bars into one SVG path");
assert.match(livePerformanceView, /ownerDocument\.hidden/,
  "Live Performance must stop chart churn while the Deck is hidden");
assert.match(livePerformanceView, /matchMedia\("\(prefers-reduced-motion: reduce\)"\)/,
  "Live Performance must react to the operating system motion preference");
assert.match(livePerformanceView, /if \(reducedMotion\) return/,
  "Live Performance must pause history updates when reduced motion is enabled");
assert.match(livePerformanceView, /fill=\{`url\(#\$\{patternId\}\)`\}\s+stroke="none"/,
  "the chart grid rectangle must not inherit the Main Deck's bright global SVG stroke");
assert.match(performanceStyles, /\.live-performance-chart\s*\{[^}]*border:\s*1px solid var\(--deck-line\)[^}]*border-radius:\s*0/s,
  "Live Performance chart housings must keep square edges with the standard dim outline");
for (const metric of [
  "calls", "cost_usd", "today_cost_usd", "cost_status", "cached_prompt_tokens",
  "remaining_budget_usd", "budget_usd", "projected_cost_usd", "budget_used_pct",
  "month_elapsed_pct", "days_remaining", "last_model", "limits", "health",
  "requests", "tokens", "retry_after", "limit", "used", "reset",
]) {
  assert.match(apiCostView, new RegExp(`\\b${metric}\\b`),
    `native API Cost & Limits must preserve the ${metric} display`);
}
assert.match(apiCostView, /viewBox="0 0 420 420"/,
  "API Cost & Limits must preserve the original provider-budget ring geometry");
assert.match(apiCostView, /onMouseEnter=\{event => showTooltip\(event, arc\)\}[\s\S]*onMouseMove=/,
  "API Cost & Limits must preserve provider-ring hover tooltips");
assert.match(apiCostView, /setFocusedProvider\(provider\.key\)/,
  "API Cost & Limits must preserve linked provider-list and ring focus");
for (const metric of [
  "tokens", "requests", "inference_time_s", "timed_calls",
  "cached_prompt_tokens", "cost_usd", "exact_calls", "estimated_calls",
  "local_requests", "cloud_requests", "runtime_id", "models", "daily",
  "successful_requests", "failed_requests", "cancelled_requests", "success_rate",
  "avg_latency_ms", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms",
  "avg_ttft_ms", "p50_ttft_ms", "p95_ttft_ms", "p99_ttft_ms",
  "prefill_tps", "generation_tps", "avg_prompt_tokens", "avg_completion_tokens",
  "p50_tokens", "p95_tokens", "week_over_week", "recent_activity",
  "peak_hours", "hourly_activity", "hour_timezone",
]) {
  assert.match(modelUsageView, new RegExp(`\\b${metric}\\b`),
    `native Model Usage must preserve the ${metric} display`);
}
for (const metric of ["tokens", "requests", "inference", "cached", "cost"]) {
  assert.match(modelUsageView, new RegExp(`${metric}:\\s*\\{label:`),
    `Model Usage must retain its ${metric} metric switch`);
}
assert.match(modelUsageView, /ranked\.slice\(0, 5\)/,
  "Model Usage must retain top-five model grouping");
assert.match(modelUsageView, /key:\s*"other"[\s\S]*name:\s*"Other"/,
  "Model Usage must retain the aggregated Other model series");
assert.match(modelUsageView, /onMouseMove=\{event => showTooltip\(event, dayIndex\)\}/,
  "Model Usage must preserve per-day pointer tooltips");
assert.match(modelUsageView, /onMouseEnter=\{\(\) => setFocusedModel\(model\.key\)\}/,
  "Model Usage must preserve linked legend and bar focus");
assert.match(modelUsageView, /type ViewMode = "timeline" \| "activity"/,
  "Model Usage must expose its original timeline and new hourly activity views");
assert.match(modelUsageView, /className="model-usage-heatmap"/,
  "Model Usage must render the seven-day hourly activity heatmap");
assert.match(modelUsageView, /Performance &amp; reliability/,
  "Model Usage must include performance and reliability in the existing widget");
assert.match(modelUsageView, /aria-label="Per-model performance and reliability"/,
  "Model Usage must retain the per-model performance comparison table");
assert.match(modelUsageStyles, /grid-template-columns:\s*repeat\(30, minmax\(0, 1fr\)\)/,
  "Model Usage must preserve its 30-day stacked chart geometry");
assert.doesNotMatch(overviewStyles, /^\s*--(?:inference|performance-black|api-black|model-usage-black)/m,
  "Overview widgets must not redeclare independent structural themes");
for (const view of [
  localInferenceView,
  livePerformanceView,
  apiCostView,
  modelUsageView,
]) {
  assert.match(view, /deck-instrument/,
    "every Overview widget must compose the shared instrument contract");
}
assert.match(overviewView, /overview-request-inspector deck-instrument/,
  "request receipts must render as a peer instrument");
assert.match(overviewView, /overview-request-summary deck-metric-rail/,
  "request receipts must expose their bounded state in a metric rail");
assert.match(overviewView, /overview-request-list deck-data-list/,
  "request receipts must use the flat shared data-list grammar");
for (const hook of [
  "deck-instrument",
  "deck-instrument__header",
  "deck-metric-rail",
  "deck-section-band",
  "deck-segmented",
  "deck-data-list",
  "deck-data-row",
]) {
  assert.match(designSystem, new RegExp(`\\.${hook.replaceAll("-", "\\-")}\\b`),
    `the shared design system must own ${hook}`);
}
assert.doesNotMatch(overviewStyles, /Canonical widget chrome/,
  "Overview must not depend on a late cascade override to look canonical");
assert.match(modelUsageStyles, /\.model-usage-chart-frame\s*\{[^}]*border:\s*1px solid var\(--deck-line\)[^}]*border-radius:\s*0/s,
  "Model Usage must preserve a square, dimly outlined graph housing");
assert.match(modelUsageStyles, /\.model-usage-activity-frame\s*\{[^}]*border:\s*1px solid var\(--deck-line\)[^}]*border-radius:\s*0/s,
  "Model Usage activity must use the same square, dimly outlined graph housing");
assert.match(modelUsageStyles, /\.model-usage-performance\s*\{[^}]*background:\s*var\(--deck-canvas\)/s,
  "Model Usage performance must retain the Overview structural black");
assert.match(modelUsageView, /className="model-usage-performance-header deck-section__header"/,
  "Model Usage performance must use the correctly named canonical header");
assert.doesNotMatch(modelUsageView, /model-usage-performance-heading/,
  "the retired performance-heading typo must not survive in markup");
assert.match(modelUsageStyles, /\.model-usage-performance-row\.is-focused\s*\{[^}]*background:\s*var\(--deck-canvas\)/s,
  "Model Usage linked row focus must not tint a structural surface");
assert.match(overviewStyles, /\.overview-widget-stack\s*\{[^}]*width:\s*100%/,
  "Overview widgets must fill the available section width");
assert.doesNotMatch(overviewStyles, /\.overview-widget-stack\s*\{[^}]*1660px/,
  "Overview must not restore the max-width that caused the large side gaps");
assert.doesNotMatch(overviewView, /InferenceOperations/,
  "Overview must not reintroduce the removed aggregate operations panel");
assert.doesNotMatch(overviewView, /postMessage\(message,\s*["']\*["']\)/,
  "parent telemetry delivery must use a declared local origin");
assert.match(fixtureView, /type:\s*"model:request_manifests"/,
  "development fixture mode must exercise the populated request inspector");
assert.match(fixtureView, /over_budget:\s*true/,
  "development receipts must preserve visual QA for budget alerts");
assert.match(fixtureView, /prompt_text_stored:\s*true/,
  "development receipts must preserve visual QA for retained-data privacy warnings");
assert.match(fixtureView, /dropped_events:\s*[1-9]/,
  "development receipts must preserve visual QA for delivery warnings");

// Render the actual private receipt card without introducing a test-only
// product export. No live app, effects, or backend requests are involved.
const viewPath=path.join(root,"frontend/main-deck/src/OverviewDestination.tsx");
const cardModule=new Module(viewPath,module);cardModule.filename=viewPath;cardModule.paths=Module._nodeModulePaths(root);
// Isolate the actual card and its formatters from sibling widgets' window
// initialization. Fail if the source boundary changes instead of testing a copy.
const cardStart=overviewView.indexOf('const tokenFormatter'),cardEnd=overviewView.indexOf('function ModelRequestInspector');
assert.ok(cardStart>=0&&cardEnd>cardStart);
cardModule._compile(esbuild.buildSync({stdin:{contents:overviewView.slice(cardStart,cardEnd)+"\nexport {RequestReceiptCard};",resolveDir:path.dirname(viewPath),sourcefile:viewPath,loader:"tsx"},bundle:true,platform:"node",format:"cjs",packages:"external",jsx:"automatic",write:false,logLevel:"silent"}).outputFiles[0].text,viewPath);
const React=require("react"),{renderToStaticMarkup}=require("react-dom/server"),{JSDOM}=require("jsdom");
function usageCard(usage) {
  const supplied=normalizeModelRequestManifest({...raw,usage});
  const markup=renderToStaticMarkup(React.createElement(cardModule.exports.RequestReceiptCard,{receipt:supplied}));
  const dom=new JSDOM(markup),cell=dom.window.document.querySelector('[data-usage-provenance]');
  const result={receipt:supplied,text:cell.textContent,kind:cell.getAttribute('data-usage-provenance')};dom.window.close();return result;
}
const estimate=usageCard({measurement:"estimated",provider_reported:false,estimated:false,input_tokens:0,output_tokens:114});
assert.strictEqual(estimate.receipt.usage.estimated,true,"canonical estimated metadata overrides an absent/false auxiliary flag");
assert.strictEqual(estimate.kind,"estimated");assert.match(estimate.text,/Estimated usage/);assert.match(estimate.text,/≈ 0 in/);assert.match(estimate.text,/≈ 114 tokens/);
assert.doesNotMatch(estimate.text,/Provider-reported/);
const nestedEstimate=usageCard({tokens:{measurement:"estimated",input_tokens:0,output_tokens:4}});
assert.strictEqual(nestedEstimate.kind,"estimated");assert.strictEqual(nestedEstimate.receipt.usage.estimated,true);
const reportedZero=usageCard({measurement:"provider_reported",provider_reported:true,input_tokens:0,output_tokens:0});
assert.strictEqual(reportedZero.kind,"provider_reported");assert.match(reportedZero.text,/Provider-reported usage0 tokens0 in · 0 out/);assert.doesNotMatch(reportedZero.text,/≈/);
const canonicalReported=usageCard({measurement:"provider_reported",input_tokens:12,output_tokens:3});
assert.strictEqual(canonicalReported.kind,"provider_reported");assert.match(canonicalReported.text,/15 tokens/);
for(const missing of [undefined,{}, {measurement:"provider_reported",provider_reported:true}, {measurement:"unavailable",provider_reported:false,input_tokens:0,output_tokens:0}, {linked:false,provider_reported:true,input_tokens:0}, {input_tokens:0,output_tokens:0}]) {
  const card=usageCard(missing);assert.strictEqual(card.kind,"unavailable");assert.match(card.text,/Usage unavailable—Token counts not available/);assert.doesNotMatch(card.text,/0 in|0 out|0 tokens/);
}
const partial=usageCard({measurement:"provider_reported",input_tokens:null,output_tokens:7});
assert.strictEqual(partial.receipt.usage.promptTokens,null);assert.strictEqual(partial.receipt.usage.totalTokens,null);
assert.match(partial.text,/Total unavailable— in · 7 out/);assert.doesNotMatch(partial.text,/0 in/);
const partialEstimate=usageCard({measurement:"estimated",output_tokens:7});
assert.match(partialEstimate.text,/Total unavailable— in · ≈ 7 out/);
const conflicting=usageCard({measurement:"estimated",provider_reported:true,total_tokens:20});
assert.strictEqual(conflicting.kind,"estimated","estimated evidence must not be presented as exact provider usage");
console.log("overview usage provenance: supplied estimated, nested, reported-zero, partial and unavailable receipts render distinctly");
console.log("overview request receipts: all tests passed");
