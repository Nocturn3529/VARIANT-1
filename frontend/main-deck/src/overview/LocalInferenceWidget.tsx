import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";
import {Button} from "../ui/Button";

type Telemetry = Record<string, unknown>;

type InferencePoint = {
  decode: number;
  prefill: number;
  rps: number;
  ttft: number;
  tpot: number;
  generation: number;
  last: number;
  request: boolean;
};

type ChartTooltip = {
  index: number;
  left: number;
  top: number;
  crosshair: number;
  point: InferencePoint;
};

const POINT_COUNT = 72;
const CHART_WIDTH = 1120;
const EMPTY_TELEMETRY: Telemetry = {};

function emptyPoint(): InferencePoint {
  return {
    decode: 0,
    prefill: 0,
    rps: 0,
    ttft: 0,
    tpot: 0,
    generation: 0,
    last: 0,
    request: false,
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function numeric(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function formatK(value: number): string {
  return value >= 1000
    ? `${(value / 1000).toFixed(2)}K`
    : Math.round(value).toLocaleString();
}

function valueOrDash(value: unknown, format: (number: number) => string): string {
  const parsed = numeric(value);
  return parsed > 0 ? format(parsed) : "—";
}

function formatMs(value: number): string {
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ms`;
}

function formatSeconds(value: number): string {
  return `${value.toFixed(2)} s`;
}

function compactModel(value: unknown): string {
  const model = String(value || "Local model").replaceAll("\\", "/").split("/").pop();
  return model?.replace(/\.gguf$/i, "") || "Local model";
}

function linePath(
  points: InferencePoint[],
  accessor: (point: InferencePoint) => number,
  top: number,
  height: number,
  max: number,
): string {
  return points.map((point, index) => {
    const x = index / (points.length - 1) * CHART_WIDTH;
    const y = top + height - clamp(accessor(point) / max, 0, 1) * height;
    return `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }).join(" ");
}

function stateCopy(state: string, engineReady: boolean): string {
  if (state === "prefill") return "Prefilling";
  if (state === "decode") return "Generating";
  if (state === "error") return "Request error";
  return engineReady ? "Idle" : "Engine offline";
}

function MetricCard({
  index,
  tone,
  title,
  value,
  unit,
  description,
  detail,
  positive = false,
}: {
  index: string;
  tone: "decode" | "prefill" | "request";
  title: string;
  value: string;
  unit: string;
  description: string;
  detail: string;
  positive?: boolean;
}) {
  return <article className={`local-inference-metric deck-metric is-${tone}`}>
    <div className="local-inference-metric__label deck-metric__label">
      <span>{index}</span><b>{title}</b>
    </div>
    <strong className="deck-metric__value">{value}<small>{unit}</small></strong>
    <p>{description}</p>
    <em className={`deck-metric__detail${positive ? " is-positive" : ""}`}>{detail}</em>
  </article>;
}

function LatencyMetric({label, value, detail}: {
  label: string;
  value: string;
  detail: string;
}) {
  return <article className="deck-data-cell">
    <span className="deck-data-cell__label">{label}</span>
    <strong className="deck-data-cell__value">{value}</strong>
    <small className="deck-data-cell__detail">{detail}</small>
  </article>;
}

export function LocalInferenceWidget({
  telemetry,
  onRefresh,
}: {
  telemetry: Record<string, unknown> | null;
  onRefresh: () => void;
}) {
  const latestTelemetry = useRef<Telemetry>(telemetry || EMPTY_TELEMETRY);
  const pausedRef = useRef(false);
  const lastTelemetryStamp = useRef(0);
  const lastMarkedRequest = useRef("");
  const chartShell = useRef<HTMLDivElement>(null);
  const [paused, setPaused] = useState(false);
  const [displayTelemetry, setDisplayTelemetry] = useState<Telemetry>(
    telemetry || EMPTY_TELEMETRY,
  );
  const [points, setPoints] = useState<InferencePoint[]>(() => (
    Array.from({length: POINT_COUNT}, emptyPoint)
  ));
  const [tooltip, setTooltip] = useState<ChartTooltip | null>(null);

  useEffect(() => {
    pausedRef.current = paused;
  }, [paused]);

  useEffect(() => {
    const next = telemetry || EMPTY_TELEMETRY;
    latestTelemetry.current = next;
    if (!paused) setDisplayTelemetry(next);
  }, [paused, telemetry]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (pausedRef.current) return;
      const latest = latestTelemetry.current;
      let point = emptyPoint();
      const stamp = numeric(latest.ts);
      if (stamp !== lastTelemetryStamp.current) {
        const requestId = text(latest.request_id);
        point = {
          decode: numeric(latest.decode_tps),
          prefill: numeric(latest.prompt_tps),
          rps: numeric(latest.requests_per_second),
          ttft: numeric(latest.ttft_ms),
          tpot: numeric(latest.tpot_ms),
          generation: numeric(latest.generation_time_s),
          last: numeric(latest.time_to_last_token_s),
          request: Boolean(requestId && requestId !== lastMarkedRequest.current),
        };
        if (point.request) lastMarkedRequest.current = requestId;
        lastTelemetryStamp.current = stamp;
      }
      setPoints(current => [...current.slice(1), point]);
    }, 900);
    return () => window.clearInterval(timer);
  }, []);

  const state = text(displayTelemetry.state, "idle");
  const engineReady = Boolean(displayTelemetry.engine_ready);
  const liveCopy = paused ? "Paused" : stateCopy(state, engineReady);
  const renderedState = state === "idle" && !engineReady ? "offline" : state;
  const runtimeName = text(displayTelemetry.runtime_name, "llama.cpp");
  const runtimeManaged = Boolean(displayTelemetry.runtime_managed);
  const status = text(displayTelemetry.status);

  const rollingDecode = numeric(displayTelemetry.rolling_avg_decode_tps);
  const decodeDetail = state === "decode"
    ? "Live stream"
    : rollingDecode > 0
      ? `${rollingDecode.toFixed(1)} tok/s · 5m avg`
      : status === "complete" ? "Last request" : stateCopy(state, engineReady);
  const cachedPromptTokens = numeric(displayTelemetry.cached_prompt_tokens);
  const processedPromptTokens = numeric(displayTelemetry.processed_prompt_tokens);
  const promptTokens = numeric(displayTelemetry.prompt_tokens);
  const outputTokens = numeric(displayTelemetry.output_tokens);
  const cacheHit = numeric(displayTelemetry.cache_hit_pct);
  const promptDetail = cachedPromptTokens > 0
    ? `${promptTokens.toLocaleString()} prompt · ${cacheHit.toFixed(0)}% cached`
    : `${promptTokens.toLocaleString()} prompt tokens`;
  const completed = numeric(displayTelemetry.rolling_completed);
  const failed = numeric(displayTelemetry.rolling_failed);
  const success = completed + failed > 0
    ? `${numeric(displayTelemetry.rolling_success_pct).toFixed(0)}% success · 5m`
    : "no completed requests";
  const requestDetail = `${numeric(displayTelemetry.active_requests)} active · ${numeric(displayTelemetry.queue_depth)} queued · ${success}`;
  const requestId = text(displayTelemetry.request_id);
  const tokenShape = !requestId
    ? "Waiting for a local inference request"
    : cachedPromptTokens > 0
      ? `${cachedPromptTokens.toLocaleString()} cached + ${processedPromptTokens.toLocaleString()} prefetched → ${outputTokens.toLocaleString()} output tokens`
      : `${promptTokens.toLocaleString()} prompt → ${outputTokens.toLocaleString()} output tokens`;

  const ttft = numeric(displayTelemetry.ttft_ms);
  const generationTime = numeric(displayTelemetry.generation_time_s);
  const timeToLastToken = numeric(displayTelemetry.time_to_last_token_s);
  const rollingP95 = numeric(displayTelemetry.rolling_p95_ttft_ms);
  const withinTarget = ttft > 0 && ttft < 500;
  const targetState = ttft <= 0
    ? "Awaiting request"
    : withinTarget ? "Within target" : "Above target";
  const targetCopy = ttft <= 0
    ? "No completed token yet"
    : rollingP95 > 0
      ? `${Math.round(rollingP95)} ms p95 · 5 minute window`
      : `${Math.abs(Math.round(500 - ttft))} ms ${withinTarget ? "below" : "above"} target`;

  const waterfall = useMemo(() => {
    const ttftSeconds = ttft / 1000;
    let prefill = 0;
    let decode = 0;
    let finish = 0;
    if (timeToLastToken > 0) {
      prefill = clamp(ttftSeconds / timeToLastToken * 100, 0, 100);
      decode = clamp(
        generationTime / timeToLastToken * 100,
        0,
        100 - prefill,
      );
      finish = Math.max(0, 100 - prefill - decode);
    } else if (state === "prefill") {
      prefill = 100;
    } else if (state === "decode") {
      decode = 100;
    }
    return {prefill, decode, finish};
  }, [generationTime, state, timeToLastToken, ttft]);

  const chart = useMemo(() => {
    const decodeMax = Math.max(
      10,
      Math.ceil(Math.max(...points.map(point => point.decode)) / 10) * 10,
    );
    const prefillMax = Math.max(
      100,
      Math.ceil(Math.max(...points.map(point => point.prefill)) / 100) * 100,
    );
    const decodeLine = linePath(points, point => point.decode, 20, 150, decodeMax);
    const prefillLine = linePath(points, point => point.prefill, 205, 135, prefillMax);
    return {
      decodeMax,
      prefillMax,
      decodeLine,
      prefillLine,
      decodeArea: `${decodeLine} L1120 170 L0 170 Z`,
      prefillArea: `${prefillLine} L1120 340 L0 340 Z`,
    };
  }, [points]);

  const showTooltip = (event: MouseEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const ratio = clamp((event.clientX - bounds.left) / bounds.width, 0, 1);
    const index = Math.round(ratio * (points.length - 1));
    const shell = chartShell.current;
    if (!shell) return;
    setTooltip({
      index,
      left: clamp(ratio * event.currentTarget.clientWidth + 14, 8, shell.clientWidth - 160),
      top: clamp(event.clientY - bounds.top - 45, 8, 245),
      crosshair: ratio * CHART_WIDTH,
      point: points[index] || emptyPoint(),
    });
  };

  return <section
    className={`overview-local-inference deck-instrument${paused ? " is-paused" : ""}`}
    data-inference-state={renderedState}
    aria-labelledby="local-inference-title"
  >
    <header className="local-inference-header deck-instrument__header">
      <div className="local-inference-heading deck-instrument__heading">
        <span className="local-inference-eyebrow deck-instrument__eyebrow">Overview / Widget 05</span>
        <div className="deck-instrument__title-row">
          <h2 className="deck-instrument__title" id="local-inference-title">Local LLM inference</h2>
          <span
            className="local-inference-state deck-instrument__status"
            data-state={renderedState === "error" ? "warning" : renderedState === "offline" || paused ? "waiting" : "live"}
          ><i />{liveCopy}</span>
        </div>
        <p className="deck-instrument__description">Real-time throughput and response latency for the selected local runtime</p>
      </div>
      <Button
        tone="quiet"
        className="local-inference-refresh"
        id="overview-refresh"
        onClick={onRefresh}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" />
        </svg>
        Refresh
      </Button>
      <div className="local-inference-runtime" aria-label="Active runtime">
        <span>Active runtime</span>
        <strong>{compactModel(displayTelemetry.model)}</strong>
        <small>{runtimeName} · {runtimeManaged ? "VARIANT-1-managed" : "Connected endpoint"}</small>
      </div>
      <div className="local-inference-controls deck-instrument__controls">
        <button
          type="button"
          className="local-inference-toggle"
          aria-pressed={!paused}
          onClick={() => setPaused(current => !current)}
        ><i /><span>{paused ? "Paused" : "Live"}</span></button>
        <span>Live · Local telemetry</span>
      </div>
    </header>

    <div className="local-inference-headlines deck-metric-rail" style={{"--deck-metric-columns": 3} as CSSProperties} aria-label="Throughput metrics">
      <MetricCard
        index="01"
        tone="decode"
        title="Generation / decode"
        value={valueOrDash(displayTelemetry.decode_tps, value => value.toFixed(1))}
        unit="tok/s"
        description="Output token throughput"
        detail={decodeDetail}
        positive={state === "decode" || status === "complete"}
      />
      <MetricCard
        index="02"
        tone="prefill"
        title="Prompt / prefill"
        value={valueOrDash(displayTelemetry.prompt_tps, formatK)}
        unit="tok/s"
        description="Input context processing"
        detail={promptDetail}
      />
      <MetricCard
        index="03"
        tone="request"
        title="Request rate"
        value={numeric(displayTelemetry.requests_per_second).toFixed(2)}
        unit="req/s"
        description="Rolling concurrent throughput"
        detail={requestDetail}
      />
    </div>

    <div className="local-inference-body">
      <section className="local-inference-throughput deck-section" aria-labelledby="inference-pulse-title">
        <header className="local-inference-section-heading deck-section__header">
          <div className="deck-section-band__heading">
            <span className="local-inference-eyebrow deck-section-band__eyebrow">Throughput metrics</span>
            <h3 className="deck-section-band__title" id="inference-pulse-title">Inference pulse</h3>
          </div>
          <div className="local-inference-trace-legend" aria-label="Chart legend">
            <span className="is-decode"><i />Decode TPS</span>
            <span className="is-prefill"><i />Prefill TPS</span>
            <span className="is-request"><i />Request</span>
          </div>
          <span className="local-inference-window">Window · 60 seconds</span>
        </header>

        <div className="local-inference-chart-shell" ref={chartShell}>
          <div className="local-inference-lane-label is-decode">
            <span>Decode</span><strong>{formatK(chart.decodeMax)} tok/s</strong>
          </div>
          <div className="local-inference-lane-label is-prefill">
            <span>Prefill</span><strong>{formatK(chart.prefillMax)} tok/s</strong>
          </div>
          <svg
            className="local-inference-chart"
            viewBox="0 0 1120 360"
            preserveAspectRatio="none"
            role="img"
            aria-label="Decode and prefill throughput over the last 60 seconds"
            onMouseMove={showTooltip}
            onMouseLeave={() => setTooltip(null)}
          >
            <defs>
              <linearGradient id="local-decode-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="var(--deck-signal-cyan)" stopOpacity=".28" />
                <stop offset="1" stopColor="var(--deck-signal-cyan)" stopOpacity="0" />
              </linearGradient>
              <linearGradient id="local-prefill-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="var(--deck-signal-indigo)" stopOpacity=".24" />
                <stop offset="1" stopColor="var(--deck-signal-indigo)" stopOpacity="0" />
              </linearGradient>
            </defs>
            <g className="local-inference-chart-grid" aria-hidden="true">
              <path d="M0 20H1120 M0 70H1120 M0 120H1120 M0 170H1120 M0 205H1120 M0 250H1120 M0 295H1120 M0 340H1120" />
              <path d="M0 0V350 M140 0V350 M280 0V350 M420 0V350 M560 0V350 M700 0V350 M840 0V350 M980 0V350 M1120 0V350" />
            </g>
            <path className="local-inference-trace-area is-decode" d={chart.decodeArea} />
            <path className="local-inference-trace-line is-decode" d={chart.decodeLine} />
            <path className="local-inference-trace-area is-prefill" d={chart.prefillArea} />
            <path className="local-inference-trace-line is-prefill" d={chart.prefillLine} />
            <g className="local-inference-request-markers">
              {points.map((point, index) => point.request ? <circle
                key={index}
                cx={(index / (points.length - 1) * CHART_WIDTH).toFixed(1)}
                cy="349"
                r="4"
              /> : null)}
            </g>
            {tooltip && <line
              className="local-inference-chart-crosshair"
              x1={tooltip.crosshair}
              x2={tooltip.crosshair}
              y1="0"
              y2="350"
            />}
          </svg>
          {tooltip && <div
            className="local-inference-chart-tooltip"
            style={{left: tooltip.left, top: tooltip.top}}
          >
            <span>{Math.round((tooltip.index - points.length + 1) * 60 / points.length)}s</span>
            <div>Decode <strong>{tooltip.point.decode.toFixed(1)} tok/s</strong></div>
            <div>Prefill <strong>{Math.round(tooltip.point.prefill)} tok/s</strong></div>
            <div>Requests <strong>{tooltip.point.rps.toFixed(2)} req/s</strong></div>
            <div>TTFT <strong>{Math.round(tooltip.point.ttft)} ms</strong></div>
          </div>}
          <div className="local-inference-chart-axis">
            <span>60s ago</span><span>45s</span><span>30s</span><span>15s</span><span>Now</span>
          </div>
        </div>
      </section>

      <section className="local-inference-latency deck-section" aria-labelledby="current-response-title">
        <header className="local-inference-section-heading local-inference-latency-heading deck-section__header">
          <div className="deck-section-band__heading">
            <span className="local-inference-eyebrow deck-section-band__eyebrow">Latency metrics</span>
            <h3 className="deck-section-band__title" id="current-response-title">Current response</h3>
          </div>
          <span className={`local-inference-target-state${ttft > 0 && !withinTarget ? " is-above" : ""}`}>
            <i />{targetState}
          </span>
        </header>

        <div className="local-inference-request-readout">
          <div><span>Request</span><strong>{requestId ? `#${requestId}` : "—"}</strong></div>
          <p>{tokenShape}</p>
        </div>

        <div className="local-inference-waterfall" aria-label="Request latency breakdown">
          <div className="local-inference-waterfall-track">
            <span className="is-queue" style={{width: "0%"}} />
            <span className="is-prefill" style={{width: `${waterfall.prefill}%`}} />
            <span className="is-decode" style={{width: `${waterfall.decode}%`}} />
            <span className="is-finish" style={{width: `${waterfall.finish}%`}} />
          </div>
          <div className="local-inference-waterfall-legend">
            <span className="is-queue"><i />Queue</span>
            <span className="is-prefill"><i />TTFT / prefill</span>
            <span className="is-decode"><i />Decode</span>
            <span className="is-finish"><i />Finalize</span>
          </div>
        </div>

        <div className="local-inference-latency-grid deck-data-row" style={{"--deck-data-columns": 4} as CSSProperties}>
          <LatencyMetric
            label="Time to first token"
            value={valueOrDash(displayTelemetry.ttft_ms, formatMs)}
            detail="Prompt to first output"
          />
          <LatencyMetric
            label="Time per output token"
            value={valueOrDash(displayTelemetry.tpot_ms, formatMs)}
            detail="Average inter-token gap"
          />
          <LatencyMetric
            label="Total generation time"
            value={valueOrDash(displayTelemetry.generation_time_s, formatSeconds)}
            detail="Decode phase only"
          />
          <LatencyMetric
            label="Time to last token"
            value={valueOrDash(displayTelemetry.time_to_last_token_s, formatSeconds)}
            detail="End-to-end completion"
          />
        </div>

        <footer className={`local-inference-target${ttft > 0 && !withinTarget ? " is-above" : ""}`}>
          <div><span>TTFT target</span><strong>&lt; 500 ms</strong></div>
          <div className="local-inference-target-track">
            <span style={{width: `${ttft > 0 ? clamp(ttft / 800 * 100, 0, 100) : 0}%`}} />
            <i />
          </div>
          <small>{targetCopy}</small>
        </footer>
      </section>
    </div>
  </section>;
}
