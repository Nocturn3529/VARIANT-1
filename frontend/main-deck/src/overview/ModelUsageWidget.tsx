import {
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";
import {asRecord as record} from "../state/storePrimitives";

type UnknownRecord = Record<string, unknown>;
type MetricKey = "tokens" | "requests" | "inference" | "cached" | "cost";
type MetricField = "tokens" | "requests" | "inferenceTimeS" | "cachedPromptTokens" | "costUsd";
type ViewMode = "timeline" | "activity";

type MetricValues = {
  tokens: number;
  requests: number;
  inferenceTimeS: number;
  timedCalls: number;
  cachedPromptTokens: number;
  costUsd: number;
};

type UsageModel = {
  key: string;
  name: string;
  provider: string;
  color: string;
};

type PerformanceModel = UsageModel & {
  requests: number;
  successful: number;
  failed: number;
  cancelled: number;
  successRate: number | null;
  avgLatencyMs: number | null;
  p50LatencyMs: number | null;
  p95LatencyMs: number | null;
  p99LatencyMs: number | null;
  avgTtftMs: number | null;
  p50TtftMs: number | null;
  p95TtftMs: number | null;
  p99TtftMs: number | null;
  prefillTps: number | null;
  generationTps: number | null;
  avgTokens: number;
  avgPromptTokens: number;
  avgCompletionTokens: number;
  p50Tokens: number;
  p95Tokens: number;
};

type UsageDay = {
  date: Date;
  dateKey: string;
  byModel: Record<string, MetricValues>;
};

type ActivityPoint = MetricValues & {
  date: Date;
  dateKey: string;
  hour: number;
  successful: number;
  failed: number;
  cancelled: number;
};

type ActivityDay = {
  date: Date;
  dateKey: string;
  hours: ActivityPoint[];
};

type NormalizedUsage = {
  usage: UnknownRecord;
  models: UsageModel[];
  performanceModels: PerformanceModel[];
  days: UsageDay[];
  activityDays: ActivityDay[];
};

type ChartTooltip = {
  dayIndex: number;
  left: number;
  top: number;
};

type ActivityTooltip = {
  point: ActivityPoint;
  left: number;
  top: number;
};

const MODEL_COLORS = [
  "var(--deck-signal-cyan)",
  "var(--deck-signal-indigo)",
  "var(--deck-signal-lime)",
  "var(--deck-signal-magenta)",
  "var(--deck-signal-orange)",
  "var(--deck-signal-warm)",
];

function numeric(value: unknown): number {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) ? parsed : 0;
}

function nullableNumeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function string(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function compact(value: unknown): string {
  const number = numeric(value);
  if (number >= 1e9) return `${(number / 1e9).toFixed(1)}B`;
  if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
  if (number >= 1e3) return `${(number / 1e3).toFixed(number >= 10000 ? 0 : 1)}K`;
  return Math.round(number).toString();
}

function duration(value: unknown): string {
  const seconds = numeric(value);
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)}h`;
  if (seconds >= 60) return `${Math.round(seconds / 60)}m`;
  if (seconds > 0 && seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  return `${Math.round(seconds)}s`;
}

function milliseconds(value: unknown): string {
  const number = nullableNumeric(value);
  if (number === null || number <= 0) return "—";
  if (number >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 0 : 1)}s`;
  return `${Math.round(number)}ms`;
}

function rate(value: unknown): string {
  const number = nullableNumeric(value);
  if (number === null || number <= 0) return "—";
  return `${number.toFixed(number >= 100 ? 0 : 1)} tok/s`;
}

function money(value: unknown): string {
  const amount = numeric(value);
  return amount > 0 && amount < .01 ? `$${amount.toFixed(4)}` : `$${amount.toFixed(2)}`;
}

function percentage(value: unknown): string {
  const number = nullableNumeric(value);
  return number === null ? "—" : `${number.toFixed(number >= 99 ? 1 : 0)}%`;
}

function delta(value: unknown): string {
  const number = nullableNumeric(value);
  if (number === null) return "No prior baseline";
  const prefix = number > 0 ? "+" : "";
  return `${prefix}${number.toFixed(Math.abs(number) >= 10 ? 0 : 1)}%`;
}

const METRICS: Record<MetricKey, {
  label: string;
  field: MetricField;
  format: (value: unknown) => string;
}> = {
  tokens: {label: "Tokens per day", field: "tokens", format: compact},
  requests: {label: "Requests per day", field: "requests", format: value => Math.round(numeric(value)).toLocaleString()},
  inference: {label: "Inference time per day", field: "inferenceTimeS", format: duration},
  cached: {label: "Cached prompt tokens per day", field: "cachedPromptTokens", format: compact},
  cost: {label: "Attributed API cost per day", field: "costUsd", format: money},
};

function parseDate(value: unknown): Date {
  const parsed = new Date(`${string(value)}T12:00:00`);
  return Number.isNaN(parsed.getTime()) ? new Date(0) : parsed;
}

function niceMax(value: number): number {
  const raw = Math.max(1, value);
  const power = 10 ** Math.floor(Math.log10(raw));
  const scaled = raw / power;
  const nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return nice * power;
}

function rowKey(row: UnknownRecord, index = 0): string {
  return string(row.key, `${string(row.provider, "unknown")}:${string(row.model, String(index))}`);
}

function providerLabel(row: UnknownRecord): string {
  return string(row.provider) === "local" && string(row.runtime_id)
    ? `Local · ${string(row.runtime_id).replace("openai_compatible", "OpenAI-compatible")}`
    : string(row.provider_name, string(row.provider, "Unknown"));
}

function emptyMetrics(): MetricValues {
  return {
    tokens: 0,
    requests: 0,
    inferenceTimeS: 0,
    timedCalls: 0,
    cachedPromptTokens: 0,
    costUsd: 0,
  };
}

function performanceModel(row: UnknownRecord, index: number): PerformanceModel {
  const successful = numeric(row.successful_requests ?? row.requests);
  const failed = numeric(row.failed_requests);
  const cancelled = numeric(row.cancelled_requests);
  const decided = successful + failed;
  return {
    key: rowKey(row, index),
    name: string(row.model, "Unknown model"),
    provider: providerLabel(row),
    color: MODEL_COLORS[index],
    requests: numeric(row.requests),
    successful,
    failed,
    cancelled,
    successRate: nullableNumeric(row.success_rate) ?? (decided ? successful / decided * 100 : null),
    avgLatencyMs: nullableNumeric(row.avg_latency_ms),
    p50LatencyMs: nullableNumeric(row.p50_latency_ms),
    p95LatencyMs: nullableNumeric(row.p95_latency_ms),
    p99LatencyMs: nullableNumeric(row.p99_latency_ms),
    avgTtftMs: nullableNumeric(row.avg_ttft_ms),
    p50TtftMs: nullableNumeric(row.p50_ttft_ms),
    p95TtftMs: nullableNumeric(row.p95_ttft_ms),
    p99TtftMs: nullableNumeric(row.p99_ttft_ms),
    prefillTps: nullableNumeric(row.prefill_tps),
    generationTps: nullableNumeric(row.generation_tps),
    avgTokens: numeric(row.avg_tokens),
    avgPromptTokens: numeric(row.avg_prompt_tokens),
    avgCompletionTokens: numeric(row.avg_completion_tokens),
    p50Tokens: numeric(row.p50_tokens),
    p95Tokens: numeric(row.p95_tokens),
  };
}

function normalizeUsage(telemetry: Record<string, unknown> | null): NormalizedUsage {
  const envelope = record(telemetry);
  const usage = envelope.model_usage ? record(envelope.model_usage) : envelope;
  const sourceModels = Array.isArray(usage.models) ? usage.models.map(record) : [];
  const ranked = [...sourceModels].sort((left, right) => (
    numeric(right.tokens) - numeric(left.tokens)
  ));
  const performanceModels = ranked.slice(0, 5).map(performanceModel);
  const models = performanceModels.map(({key, name, provider, color}) => ({key, name, provider, color}));
  const hiddenKeys = new Set(ranked.slice(5).map((row, index) => rowKey(row, index + 5)));
  if (hiddenKeys.size) {
    models.push({key: "other", name: "Other", provider: "Mixed", color: MODEL_COLORS[5]});
  }
  const visibleKeys = new Set(models.map(model => model.key));
  const sourceDays = Array.isArray(usage.daily) ? usage.daily.map(record) : [];
  const days = sourceDays.map((day): UsageDay => {
    const byModel: Record<string, MetricValues> = {};
    const rows = Array.isArray(day.models) ? day.models.map(record) : [];
    rows.forEach((row, index) => {
      const originalKey = rowKey(row, index);
      const key = visibleKeys.has(originalKey) ? originalKey : hiddenKeys.has(originalKey) ? "other" : "";
      if (!key) return;
      const target = byModel[key] || (byModel[key] = emptyMetrics());
      target.tokens += numeric(row.tokens);
      target.requests += numeric(row.requests);
      target.inferenceTimeS += numeric(row.inference_time_s);
      target.timedCalls += numeric(row.timed_calls);
      target.cachedPromptTokens += numeric(row.cached_prompt_tokens);
      target.costUsd += numeric(row.cost_usd);
    });
    return {
      date: parseDate(day.date),
      dateKey: string(day.date),
      byModel,
    };
  });
  const sourceActivity = Array.isArray(usage.hourly_activity) ? usage.hourly_activity.map(record) : [];
  const activityDays = sourceActivity.map((day): ActivityDay => {
    const sourceHours = Array.isArray(day.hours) ? day.hours.map(record) : [];
    const byHour = new Map(sourceHours.map(hour => [Math.max(0, Math.min(23, numeric(hour.hour))), hour]));
    const date = parseDate(day.date);
    const dateKey = string(day.date);
    return {
      date,
      dateKey,
      hours: Array.from({length: 24}, (_, hour): ActivityPoint => {
        const source = byHour.get(hour) || {};
        return {
          date,
          dateKey,
          hour,
          tokens: numeric(source.tokens),
          requests: numeric(source.requests),
          successful: numeric(source.successful),
          failed: numeric(source.failed),
          cancelled: numeric(source.cancelled),
          inferenceTimeS: numeric(source.inference_time_s),
          timedCalls: numeric(source.successful),
          cachedPromptTokens: numeric(source.cached_prompt_tokens),
          costUsd: numeric(source.cost_usd),
        };
      }),
    };
  });
  return {usage, models, performanceModels, days, activityDays};
}

function SummaryMetric({label, value, detail}: {label: string; value: string; detail: string}) {
  return <article className="deck-metric" title={detail}>
    <span className="deck-metric__label">{label}</span>
    <strong className="deck-metric__value">{value}</strong>
    <small className="deck-metric__detail">{detail}</small>
  </article>;
}

function PerformanceValue({primary, detail}: {primary: string; detail: string}) {
  return <span className="model-usage-performance-value"><strong>{primary}</strong><small>{detail}</small></span>;
}

export function ModelUsageWidget({
  telemetry,
}: {
  telemetry: Record<string, unknown> | null;
}) {
  const {usage, models, performanceModels, days, activityDays} = useMemo(
    () => normalizeUsage(telemetry),
    [telemetry],
  );
  const totals = record(usage.totals);
  const weekOverWeek = record(usage.week_over_week);
  const weekChange = record(weekOverWeek.change_pct);
  const recentActivity = record(usage.recent_activity);
  const peakHours = Array.isArray(usage.peak_hours) ? usage.peak_hours.map(record) : [];
  const chartFrame = useRef<HTMLDivElement>(null);
  const activityFrame = useRef<HTMLDivElement>(null);
  const [activeMetric, setActiveMetric] = useState<MetricKey>("tokens");
  const [viewMode, setViewMode] = useState<ViewMode>("timeline");
  const [focusedModel, setFocusedModel] = useState<string | null>(null);
  const [focusedDay, setFocusedDay] = useState<number | null>(null);
  const [tooltip, setTooltip] = useState<ChartTooltip | null>(null);
  const [activityTooltip, setActivityTooltip] = useState<ActivityTooltip | null>(null);

  const config = METRICS[activeMetric];
  const totalRequests = numeric(totals.requests);
  const successfulRequests = numeric(totals.successful_requests ?? totals.requests);
  const failedRequests = numeric(totals.failed_requests);
  const cancelledRequests = numeric(totals.cancelled_requests);
  const decidedRequests = successfulRequests + failedRequests;
  const successRate = nullableNumeric(totals.success_rate)
    ?? (decidedRequests ? successfulRequests / decidedRequests * 100 : null);
  const localRequests = numeric(totals.local_requests);
  const cloudRequests = numeric(totals.cloud_requests);
  const routedRequests = localRequests + cloudRequests;
  const localPercent = routedRequests ? Math.round(localRequests / routedRequests * 100) : 0;
  const cloudPercent = routedRequests ? 100 - localPercent : 0;
  const pricedCalls = numeric(totals.exact_calls) + numeric(totals.estimated_calls);
  const timedCalls = numeric(totals.timed_calls);
  const dailyTotals = days.map(day => models.reduce((sum, model) => (
    sum + day.byModel[model.key]?.[config.field] || sum
  ), 0));
  const chartMax = niceMax(Math.max(0, ...dailyTotals));
  const tooltipDay = tooltip ? days[tooltip.dayIndex] : null;
  const tooltipValues = tooltipDay ? models.map(model => ({
    model,
    value: tooltipDay.byModel[model.key]?.[config.field] || 0,
    timedCalls: tooltipDay.byModel[model.key]?.timedCalls || 0,
  })) : [];
  const tooltipTotal = tooltipValues.reduce((sum, row) => sum + row.value, 0);
  const activityPoints = activityDays.flatMap(day => day.hours);
  const activityValues = activityPoints.map(point => point[config.field]);
  const activityMax = Math.max(1, ...activityValues);
  const activityAvailable = Boolean(usage.hourly_available) && activityValues.some(value => value > 0);
  const noUsage = !days.length || !models.length;
  const noRuntime = activeMetric === "inference" && !timedCalls;
  const noCost = activeMetric === "cost" && !pricedCalls;

  const clearChartFocus = () => {
    setFocusedModel(null);
    setFocusedDay(null);
    setTooltip(null);
  };

  const showTooltip = (event: MouseEvent<HTMLDivElement>, dayIndex: number) => {
    const bounds = chartFrame.current?.getBoundingClientRect();
    if (!bounds) return;
    setFocusedDay(dayIndex);
    setTooltip({
      dayIndex,
      left: Math.min(bounds.width - 228, Math.max(58, event.clientX - bounds.left + 12)),
      top: Math.max(8, event.clientY - bounds.top - 115),
    });
  };

  const showActivityTooltip = (event: {clientX: number; clientY: number}, point: ActivityPoint) => {
    const bounds = activityFrame.current?.getBoundingClientRect();
    if (!bounds) return;
    setActivityTooltip({
      point,
      left: Math.min(bounds.width - 206, Math.max(82, event.clientX - bounds.left + 10)),
      top: Math.max(58, event.clientY - bounds.top - 68),
    });
  };

  const emptyCopy = noUsage
    ? ["No model usage recorded yet", "Completed local and cloud requests will appear here automatically."]
    : noRuntime
      ? ["No measured runtime in this window", "Inference time is captured for new requests."]
      : noCost
        ? ["No attributable API cost in this window", "Local calls stay unpriced; supported cloud calls appear as exact or estimated cost."]
        : null;
  const requestDetail = [
    decidedRequests ? `${percentage(successRate)} success` : "Awaiting outcomes",
    failedRequests ? `${failedRequests.toLocaleString()} failed` : "No failures",
    pricedCalls ? `${money(totals.cost_usd)} API` : "",
    nullableNumeric(weekChange.requests) !== null ? `${delta(weekChange.requests)} WoW` : "",
  ].filter(Boolean).join(" · ");
  const cachedInput = numeric(totals.cached_prompt_tokens);
  const uncachedInput = numeric(totals.uncached_prompt_tokens);
  const cacheShare = numeric(totals.cache_share) * 100;
  const tokenDetail = [
    cachedInput
      ? `${compact(cachedInput)} cached (${percentage(cacheShare)})`
      : "Input + output",
    uncachedInput ? `${compact(uncachedInput)} uncached` : "",
    nullableNumeric(weekChange.tokens) !== null ? `${delta(weekChange.tokens)} WoW` : "",
  ].filter(Boolean).join(" · ");
  const peakHour = peakHours[0] ? numeric(peakHours[0].hour) : null;

  return <section
    className={`overview-model-usage deck-instrument${telemetry ? " is-live" : " is-waiting"}`}
    aria-labelledby="model-usage-title"
  >
    <header className="model-usage-header deck-instrument__header">
      <div className="model-usage-heading deck-instrument__heading">
        <span className="model-usage-eyebrow deck-instrument__eyebrow">Overview / Widget 02</span>
        <div className="deck-instrument__title-row"><h2 className="deck-instrument__title" id="model-usage-title">Model usage</h2></div>
        <p className="deck-instrument__description">Local and cloud activity · Last 30 days</p>
      </div>
      <div className="deck-instrument__controls">
        <div className="model-usage-switcher deck-segmented" role="group" aria-label="Usage metric">
          {(Object.keys(METRICS) as MetricKey[]).map(metric => <button
            key={metric}
            type="button"
            className={`deck-segmented__button${activeMetric === metric ? " is-active" : ""}`}
            aria-pressed={activeMetric === metric}
            onClick={() => {
              setActiveMetric(metric);
              clearChartFocus();
              setActivityTooltip(null);
            }}
          >{metric === "inference" ? "Inference time" : metric === "cached" ? "Cached" : metric[0].toUpperCase() + metric.slice(1)}</button>)}
        </div>
        <span
          className="model-usage-state deck-instrument__status"
          data-state={telemetry ? "live" : "waiting"}
        ><i />{telemetry ? "Live telemetry" : "Waiting for model usage"}</span>
      </div>
    </header>

    <div className="model-usage-summary deck-metric-rail" aria-label="Thirty day totals">
      <SummaryMetric label="Total tokens" value={compact(totals.tokens)} detail={tokenDetail} />
      <SummaryMetric label="Requests" value={totalRequests.toLocaleString()} detail={requestDetail} />
      <SummaryMetric
        label="Inference time"
        value={timedCalls ? duration(totals.inference_time_s) : "—"}
        detail={timedCalls
          ? `${timedCalls.toLocaleString()} measured · ${milliseconds(totals.avg_latency_ms)} average`
          : "Runtime available for new requests"}
      />
      <article className="model-usage-route deck-metric">
        <span className="deck-metric__label">Route split</span>
        <strong className="deck-metric__value"><i className="is-local" /><b>{localPercent}%</b><em>Local</em><i className="is-cloud" /><b>{cloudPercent}%</b><em>Cloud</em></strong>
        <small className="deck-metric__detail" aria-label={`${localPercent}% local and ${cloudPercent}% cloud`}><b style={{width: `${localPercent}%`}} /><b style={{width: `${cloudPercent}%`}} /></small>
      </article>
    </div>

    <section className="model-usage-chart-section deck-section" aria-labelledby="model-usage-chart-heading">
      <div className="model-usage-chart-toolbar deck-section__header">
        <div className="deck-section-band__heading">
          <span className="model-usage-eyebrow deck-section-band__eyebrow">{viewMode === "timeline" ? "Daily breakdown" : "Usage rhythm"}</span>
          <h3 className="deck-section-band__title" id="model-usage-chart-heading">{viewMode === "timeline" ? config.label : `${config.label.replace(" per day", "")} by hour`}</h3>
        </div>
        <div className="model-usage-chart-controls">
          <div className="model-usage-view-switcher deck-segmented" role="group" aria-label="Usage chart view">
            {(["timeline", "activity"] as ViewMode[]).map(mode => <button
              key={mode}
              type="button"
              className={`deck-segmented__button${viewMode === mode ? " is-active" : ""}`}
              aria-pressed={viewMode === mode}
              onClick={() => {
                setViewMode(mode);
                clearChartFocus();
                setActivityTooltip(null);
              }}
            >{mode === "timeline" ? "30 days" : "Activity"}</button>)}
          </div>
          {viewMode === "timeline" ? <div className={`model-usage-legend${focusedModel ? " is-filtering" : ""}`} aria-label="Models">
            {models.map(model => <button
              type="button"
              key={model.key}
              className={focusedModel === model.key ? "is-focused" : ""}
              aria-label={`Highlight ${model.name}`}
              onMouseEnter={() => setFocusedModel(model.key)}
              onMouseLeave={() => setFocusedModel(null)}
              onFocus={() => setFocusedModel(model.key)}
              onBlur={() => setFocusedModel(null)}
            >
              <i style={{"--model-usage-color": model.color} as CSSProperties} />
              <span>{model.name}</span><em>{model.provider}</em>
            </button>)}
          </div> : <span className="model-usage-activity-zone">7 days · {string(usage.hour_timezone, "UTC")}</span>}
        </div>
      </div>

      {viewMode === "timeline" ? <>
        <div className="model-usage-chart-frame" ref={chartFrame}>
          <div className="model-usage-y-axis" aria-hidden="true">
            {[1, .75, .5, .25, 0].map(ratio => <span key={ratio}>{config.format(chartMax * ratio)}</span>)}
          </div>
          <div
            className={`model-usage-chart${focusedModel ? " is-model-focus" : ""}${focusedDay !== null && !focusedModel ? " is-day-focus" : ""}`}
            role="img"
            aria-label="Stacked model usage by day for the last 30 days"
          >
            {days.map((day, dayIndex) => <div
              key={`${day.dateKey}-${dayIndex}`}
              className={`model-usage-day${focusedDay !== null && focusedDay !== dayIndex && !focusedModel ? " is-dimmed" : ""}${focusedDay === dayIndex && !focusedModel ? " is-focused" : ""}`}
              onMouseEnter={() => setFocusedDay(dayIndex)}
              onMouseMove={event => showTooltip(event, dayIndex)}
              onMouseLeave={clearChartFocus}
            >
              {models.map(model => {
                const value = day.byModel[model.key]?.[config.field] || 0;
                if (value <= 0) return null;
                return <div
                  key={model.key}
                  className={`model-usage-segment${focusedModel && focusedModel !== model.key ? " is-dimmed" : ""}${focusedModel === model.key ? " is-focused" : ""}`}
                  data-model={model.key}
                  style={{height: `${value / chartMax * 100}%`, "--model-usage-color": model.color} as CSSProperties}
                  onMouseEnter={() => setFocusedModel(model.key)}
                  onMouseLeave={() => setFocusedModel(null)}
                />;
              })}
            </div>)}
            {emptyCopy && <div className="model-usage-chart-empty"><strong>{emptyCopy[0]}</strong><span>{emptyCopy[1]}</span></div>}
          </div>
          {tooltip && tooltipDay && <div className="model-usage-tooltip" style={{left: tooltip.left, top: tooltip.top}}>
            <strong>{tooltipDay.date.toLocaleDateString([], {month: "short", day: "numeric"})}</strong>
            <span>{config.format(tooltipTotal)} total</span>
            {tooltipValues.map(({model, value, timedCalls: rowTimedCalls}) => <div
              key={model.key}
              className={`model-usage-tooltip-row${focusedModel && focusedModel !== model.key ? " is-dimmed" : ""}${focusedModel === model.key ? " is-focused" : ""}`}
            >
              <i style={{"--model-usage-color": model.color} as CSSProperties} />
              <span>{model.name}</span><b>{activeMetric === "inference" && !rowTimedCalls ? "—" : config.format(value)}</b>
            </div>)}
          </div>}
        </div>

        <div className={`model-usage-date-axis${focusedDay !== null && !focusedModel ? " is-filtering" : ""}`} aria-hidden="true">
          {days.map((day, index) => <span
            key={`${day.dateKey}-axis`}
            className={`${index % 5 === 0 || index === days.length - 1 ? "is-date-label" : ""}${focusedDay === index && !focusedModel ? " is-focused" : ""}`}
          >{day.date.toLocaleDateString([], {month: "short", day: "numeric"})}</span>)}
        </div>
      </> : <div className="model-usage-activity-frame" ref={activityFrame}>
        <div className="model-usage-activity-summary deck-metric-rail">
          <SummaryMetric label="Last 24 hours" value={numeric(recentActivity.last_24h_requests).toLocaleString()} detail={`${compact(recentActivity.last_24h_tokens)} tokens`} />
          <SummaryMetric label="Previous 24 hours" value={numeric(recentActivity.prev_24h_requests).toLocaleString()} detail="Completed and failed attempts" />
          <SummaryMetric label="24 hour change" value={delta(recentActivity.change_24h_pct)} detail="Request volume" />
          <SummaryMetric label="Peak hour" value={peakHour === null ? "—" : `${String(peakHour).padStart(2, "0")}:00`} detail={`${string(usage.hour_timezone, "UTC")} · ${peakHours[0] ? numeric(peakHours[0].requests).toLocaleString() : 0} requests`} />
        </div>
        <div className="model-usage-heatmap-scroll">
          <div className="model-usage-hour-axis" aria-hidden="true">
            <span />
            {Array.from({length: 24}, (_, hour) => <span key={hour} className={[0, 6, 12, 18, 23].includes(hour) ? "is-hour-label" : ""}>{String(hour).padStart(2, "0")}</span>)}
          </div>
          <div className="model-usage-heatmap" role="img" aria-label={`${config.label} by UTC hour for the last seven days`}>
            {activityDays.map(day => <div className="model-usage-heatmap-row" key={day.dateKey}>
              <time dateTime={day.dateKey}>{day.date.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"})}</time>
              {day.hours.map(point => {
                const value = point[config.field];
                const strength = value > 0 ? .16 + value / activityMax * .84 : 0;
                return <button
                  key={`${day.dateKey}-${point.hour}`}
                  type="button"
                  className={value > 0 ? "has-activity" : ""}
                  style={{"--activity-strength": strength} as CSSProperties}
                  aria-label={`${day.dateKey} ${String(point.hour).padStart(2, "0")}:00: ${config.format(value)}`}
                  onMouseEnter={event => showActivityTooltip(event, point)}
                  onMouseMove={event => showActivityTooltip(event, point)}
                  onMouseLeave={() => setActivityTooltip(null)}
                  onFocus={event => {
                    const bounds = event.currentTarget.getBoundingClientRect();
                    showActivityTooltip({
                      clientX: bounds.left + bounds.width / 2,
                      clientY: bounds.top + bounds.height / 2,
                    }, point);
                  }}
                  onBlur={() => setActivityTooltip(null)}
                />;
              })}
            </div>)}
            {!activityAvailable && <div className="model-usage-chart-empty"><strong>Hourly activity starts now</strong><span>Existing daily history is preserved; the heatmap fills as new requests arrive.</span></div>}
          </div>
        </div>
        {activityTooltip && <div className="model-usage-activity-tooltip" style={{left: activityTooltip.left, top: activityTooltip.top}}>
          <strong>{activityTooltip.point.date.toLocaleDateString([], {weekday: "short", month: "short", day: "numeric"})} · {String(activityTooltip.point.hour).padStart(2, "0")}:00</strong>
          <span>{config.format(activityTooltip.point[config.field])} · {string(usage.hour_timezone, "UTC")}</span>
          <small>{activityTooltip.point.successful.toLocaleString()} successful · {activityTooltip.point.failed.toLocaleString()} failed · {activityTooltip.point.cancelled.toLocaleString()} cancelled</small>
        </div>}
      </div>}
    </section>

    <section className="model-usage-performance deck-section" aria-labelledby="model-performance-heading">
      <header className="model-usage-performance-header deck-section__header">
        <div className="deck-section-band__heading"><span className="model-usage-eyebrow deck-section-band__eyebrow">Observed requests</span><h3 className="deck-section-band__title" id="model-performance-heading">Performance &amp; reliability</h3></div>
        <span>{performanceModels.length ? `${performanceModels.length} leading models` : "Awaiting model traffic"}</span>
      </header>
      <div className="model-usage-performance-summary deck-metric-rail">
        <SummaryMetric
          label="Success rate"
          value={decidedRequests ? percentage(successRate) : "—"}
          detail={`${successfulRequests.toLocaleString()} successful · ${failedRequests.toLocaleString()} failed · ${cancelledRequests.toLocaleString()} cancelled`}
        />
        <SummaryMetric
          label="Response latency"
          value={milliseconds(totals.p50_latency_ms || totals.avg_latency_ms)}
          detail={`P50 · ${milliseconds(totals.p95_latency_ms)} p95 · ${milliseconds(totals.p99_latency_ms)} p99`}
        />
        <SummaryMetric
          label="Time to first token"
          value={milliseconds(totals.p50_ttft_ms || totals.avg_ttft_ms)}
          detail={`P50 · ${milliseconds(totals.p95_ttft_ms)} p95 · ${milliseconds(totals.p99_ttft_ms)} p99`}
        />
        <SummaryMetric
          label="Tokens per request"
          value={compact(totals.avg_tokens)}
          detail={`${compact(totals.avg_prompt_tokens)} prompt · ${compact(totals.avg_completion_tokens)} output · ${compact(totals.p50_tokens)} p50 · ${compact(totals.p95_tokens)} p95`}
        />
      </div>
      <div className="model-usage-performance-scroll">
        <div className="model-usage-performance-table deck-data-list" role="table" aria-label="Per-model performance and reliability">
          <div className="model-usage-performance-row deck-data-row is-header" role="row">
            <span role="columnheader">Model</span><span role="columnheader">Reliability</span><span role="columnheader">Latency</span><span role="columnheader">TTFT</span><span role="columnheader">Throughput</span><span role="columnheader">Token mix</span>
          </div>
          {performanceModels.map(model => <div
            className={`model-usage-performance-row deck-data-row${focusedModel === model.key ? " is-focused" : ""}`}
            role="row"
            key={model.key}
            onMouseEnter={() => setFocusedModel(model.key)}
            onMouseLeave={() => setFocusedModel(null)}
          >
            <span className="model-usage-performance-model" role="cell"><i style={{"--model-usage-color": model.color} as CSSProperties} /><strong>{model.name}</strong><small>{model.provider}</small></span>
            <PerformanceValue primary={model.successful + model.failed ? percentage(model.successRate) : "—"} detail={`${model.successful.toLocaleString()} ok · ${model.failed.toLocaleString()} failed`} />
            <PerformanceValue primary={milliseconds(model.avgLatencyMs)} detail={`${milliseconds(model.p95LatencyMs)} p95 · ${milliseconds(model.p99LatencyMs)} p99`} />
            <PerformanceValue primary={milliseconds(model.avgTtftMs)} detail={`${milliseconds(model.p95TtftMs)} p95 · ${milliseconds(model.p99TtftMs)} p99`} />
            <PerformanceValue primary={rate(model.generationTps)} detail={`${rate(model.prefillTps)} prefill`} />
            <PerformanceValue primary={`${compact(model.avgPromptTokens)} / ${compact(model.avgCompletionTokens)}`} detail={`Prompt / output · ${compact(model.p50Tokens)} p50 · ${compact(model.p95Tokens)} p95`} />
          </div>)}
          {!performanceModels.length && <div className="model-usage-performance-empty">Performance rows appear after the first completed model request.</div>}
        </div>
      </div>
    </section>
  </section>;
}
