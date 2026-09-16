import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {useEffect, useMemo, useRef, useState} from "react";
import {asRecord as record} from "../state/storePrimitives";

type UnknownRecord = Record<string, unknown>;

type UsageSample = {
  total: number;
  variant1: number;
};

type PerformanceMetric = {
  label: string;
  value: string;
  detail: string;
};

type PerformanceResource = {
  key: string;
  index: string;
  name: string;
  hardware: string;
  color: string;
  scale: string;
  sample: UsageSample;
  metrics: PerformanceMetric[];
};

const SAMPLE_COUNT = 60;
const CHART_WIDTH = 960;
const ZERO_SAMPLES = Array.from({length: SAMPLE_COUNT}, () => ({total: 0, variant1: 0}));
const GPU_COLORS = [
  "var(--deck-signal-magenta)",
  "var(--deck-signal-orange)",
  "var(--deck-signal-cyan)",
  "var(--deck-signal-lime)",
];

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => (
    typeof window !== "undefined"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ));

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    setReduced(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function numeric(value: unknown): number {
  return finiteNumber(value) ?? 0;
}

function clamp(value: unknown, min = 0, max = 100): number {
  return Math.max(min, Math.min(max, numeric(value)));
}

function string(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function percent(value: unknown): string {
  const parsed = numeric(value);
  return `${clamp(parsed).toFixed(parsed < 10 && parsed > 0 ? 1 : 0)}%`;
}

function gb(value: unknown): string {
  return `${(numeric(value) / 1024).toFixed(1)} GB`;
}

function valueOrDash(value: unknown, formatter: (number: number) => string): string {
  const parsed = finiteNumber(value);
  return parsed === null ? "—" : formatter(parsed);
}

function bytesPerSecond(value: unknown): string {
  const bytes = Math.max(0, numeric(value));
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB/s`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB/s`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB/s`;
  return `${Math.round(bytes)} B/s`;
}

function metric(label: string, value: string, detail: string): PerformanceMetric {
  return {label, value, detail};
}

function gpuIndex(gpu: UnknownRecord, position: number): number {
  const parsed = finiteNumber(gpu.index);
  return parsed === null ? position : Math.max(0, Math.trunc(parsed));
}

function buildResources(
  telemetry: UnknownRecord,
  knownGpuIndexes: number[],
): PerformanceResource[] {
  const cpu = record(telemetry.cpu);
  const memory = record(telemetry.memory);
  const disk = record(telemetry.disk);
  const gpuRows = Array.isArray(telemetry.gpus)
    ? telemetry.gpus.map(record)
    : [];
  const totalMemory = memory.total_mb || telemetry.ram_total_mb || 0;
  const availableMemory = memory.available_mb || telemetry.ram_available_mb || 0;
  const variant1Memory = memory.variant1_used_mb || telemetry.variant1_rss_mb || 0;
  const resources: PerformanceResource[] = [
    {
      key: "cpu",
      index: "01",
      name: "CPU",
      hardware: string(cpu.name, "Processor"),
      color: "var(--deck-signal-cyan)",
      scale: "100%",
      sample: {
        total: clamp(cpu.utilization_pct),
        variant1: clamp(cpu.variant1_utilization_pct),
      },
      metrics: [
        metric("Utilization", percent(cpu.utilization_pct), `${cpu.logical_processors || "—"} logical processors`),
        metric("Speed", valueOrDash(cpu.speed_mhz, value => `${(value / 1000).toFixed(2)} GHz`), "Current clock"),
        metric("Temperature", valueOrDash(cpu.temperature_c, value => `${value.toFixed(0)} °C`), cpu.temperature_c == null ? "Sensor unavailable" : "Package sensor"),
        metric("Power", valueOrDash(cpu.power_draw_w, value => `${value.toFixed(1)} W`), cpu.power_draw_w == null ? "Sensor unavailable" : "Package draw"),
      ],
    },
    {
      key: "memory",
      index: "02",
      name: "Memory",
      hardware: `${gb(totalMemory)} usable`,
      color: "var(--deck-signal-indigo)",
      scale: gb(totalMemory),
      sample: {
        total: clamp(memory.utilization_pct),
        variant1: clamp(memory.variant1_utilization_pct),
      },
      metrics: [
        metric("In use", gb(memory.used_mb), `${percent(memory.utilization_pct)} of usable`),
        metric("Available", gb(availableMemory), "Standby + free"),
        metric("VARIANT-1", gb(variant1Memory), `${percent(memory.variant1_utilization_pct)} of total memory`),
        metric("Total", gb(totalMemory), "Usable memory"),
      ],
    },
    {
      key: "disk",
      index: "03",
      name: string(disk.name, "Disk"),
      hardware: "System storage · Live I/O",
      color: "var(--deck-signal-lime)",
      scale: "100%",
      sample: {
        total: clamp(disk.utilization_pct),
        variant1: clamp(disk.variant1_utilization_pct),
      },
      metrics: [
        metric("Active time", percent(disk.utilization_pct), "All processes"),
        metric("Read speed", bytesPerSecond(disk.read_bytes_per_second), "All processes"),
        metric("Write speed", bytesPerSecond(disk.write_bytes_per_second), "All processes"),
        metric("Response", valueOrDash(disk.response_time_ms, value => `${value.toFixed(value < 10 ? 2 : 1)} ms`), "Average latency"),
      ],
    },
  ];

  const activeByIndex = new Map<number, UnknownRecord>();
  gpuRows.forEach((gpu, position) => activeByIndex.set(gpuIndex(gpu, position), gpu));
  knownGpuIndexes.forEach((index, position) => {
    const gpu = activeByIndex.get(index);
    const active = Boolean(gpu);
    const row = gpu || {};
    const powerLimit = finiteNumber(row.power_limit_w);
    resources.push({
      key: `gpu-${index}`,
      index: String(position + 4).padStart(2, "0"),
      name: active ? `GPU ${index}` : index === 0 ? "GPU" : `GPU ${index}`,
      hardware: active
        ? string(row.name, "GPU")
        : gpuRows.length ? "Not reported in latest sample" : "No GPU telemetry detected",
      color: GPU_COLORS[position % GPU_COLORS.length],
      scale: "100%",
      sample: {
        total: active ? clamp(row.utilization_pct) : 0,
        variant1: active ? clamp(row.variant1_utilization_pct) : 0,
      },
      metrics: active ? [
        metric("Utilization", percent(row.utilization_pct), "All GPU engines"),
        metric("VRAM", `${gb(row.vram_used_mb)} / ${gb(row.vram_total_mb)}`, "Dedicated memory"),
        metric("Temperature", valueOrDash(row.temperature_c, value => `${value.toFixed(0)} °C`), "GPU sensor"),
        metric("Power", valueOrDash(row.power_draw_w, value => `${value.toFixed(1)} W`), powerLimit === null ? "Board draw" : `${powerLimit.toFixed(0)} W board limit`),
      ] : [
        metric("Utilization", "—", "Unavailable"),
        metric("VRAM", "—", "Unavailable"),
        metric("Temperature", "—", "Unavailable"),
        metric("Power", "—", "Unavailable"),
      ],
    });
  });
  return resources;
}

function PerformanceChart({
  resource,
  samples,
}: {
  resource: PerformanceResource;
  samples: UsageSample[];
}) {
  const slot = CHART_WIDTH / samples.length;
  const width = Math.max(1.5, slot - 1.75);
  const patternId = `performance-grid-${resource.key}`;
  const paths = useMemo(() => {
    const total: string[] = [];
    const variant1: string[] = [];
    samples.forEach((sample, index) => {
      const totalHeight = clamp(sample.total) * 2.5;
      const variant1Height = Math.min(clamp(sample.total), clamp(sample.variant1)) * 2.5;
      const x = index * slot + (slot - width) / 2;
      total.push(`M${x.toFixed(2)} 250v-${totalHeight.toFixed(2)}h${width.toFixed(2)}v${totalHeight.toFixed(2)}Z`);
      variant1.push(`M${x.toFixed(2)} 250v-${variant1Height.toFixed(2)}h${width.toFixed(2)}v${variant1Height.toFixed(2)}Z`);
    });
    return {total: total.join(""), variant1: variant1.join("")};
  }, [samples, slot, width]);
  return <div className="live-performance-chart-wrap">
    <div className="live-performance-chart-scale">
      <span>{resource.scale}</span><span>0</span>
    </div>
    <svg
      className="live-performance-chart"
      viewBox="0 0 960 250"
      preserveAspectRatio="none"
      role="img"
      aria-label={`${resource.name} usage over the last 60 seconds`}
    >
      <defs>
        <pattern id={patternId} width="30" height="25" patternUnits="userSpaceOnUse">
          <path
            d="M 30 0 L 0 0 0 25"
            fill="none"
            stroke="rgba(255,255,255,.08)"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        </pattern>
      </defs>
      <rect width="960" height="250" fill={`url(#${patternId})`} stroke="none" />
      <g className="live-performance-chart-bars">
        <path className="is-total" d={paths.total} />
        <path className="is-variant1" d={paths.variant1} />
      </g>
    </svg>
    <div className="live-performance-chart-axis"><span>60s ago</span><span>Now</span></div>
  </div>;
}

function PerformancePanel({
  resource,
  samples,
  live,
}: {
  resource: PerformanceResource;
  samples: UsageSample[];
  live: boolean;
}) {
  return <section
    className="live-performance-panel deck-section"
    style={{"--performance-signal": resource.color} as React.CSSProperties}
    aria-labelledby={`performance-${resource.key}-title`}
  >
    <header className="live-performance-toolbar deck-section__header">
      <div className="deck-section-band__heading">
        <span className="deck-section-band__eyebrow">{resource.index} / Hardware</span>
        <h3 className="deck-section-band__title" id={`performance-${resource.key}-title`}>{resource.name}</h3>
      </div>
      <div className="live-performance-legend" aria-label={`${resource.name} chart legend`}>
        <span className="is-total"><i />Total usage</span>
        <span className="is-variant1"><i />VARIANT-1 usage</span>
      </div>
      <strong className={`live-performance-hardware${live ? " is-live" : ""}`}>{resource.hardware}</strong>
    </header>
    <PerformanceChart resource={resource} samples={samples} />
    <div className="live-performance-metrics deck-metric-rail">
      {resource.metrics.map(item => <article className="deck-metric" key={item.label}>
        <span className="deck-metric__label">{item.label}</span>
        <strong className="deck-metric__value">{item.value}</strong>
        <small className="deck-metric__detail">{item.detail}</small>
      </article>)}
    </div>
  </section>;
}

export function LivePerformanceWidget({
  telemetry,
}: {
  telemetry: Record<string, unknown> | null;
}) {
  const ownerDocument = useSurfaceDocument();
  const payload = telemetry || {};
  const [knownGpuIndexes, setKnownGpuIndexes] = useState<number[]>([0]);
  const [samplesByKey, setSamplesByKey] = useState<Record<string, UsageSample[]>>({
    cpu: ZERO_SAMPLES,
    memory: ZERO_SAMPLES,
    disk: ZERO_SAMPLES,
    "gpu-0": ZERO_SAMPLES,
  });
  const currentSamples = useRef<Record<string, UsageSample>>({});
  const hasTelemetry = useRef(Boolean(telemetry));
  const reducedMotion = useReducedMotion();

  useEffect(() => {
    hasTelemetry.current = Boolean(telemetry);
    if (!Array.isArray(telemetry?.gpus)) return;
    const discovered = telemetry.gpus.map((value, position) => gpuIndex(record(value), position));
    setKnownGpuIndexes(current => {
      const next = [...new Set([...current, ...discovered])].sort((left, right) => left - right);
      return next.length === current.length && next.every((value, index) => value === current[index])
        ? current
        : next;
    });
  }, [telemetry]);

  const resources = useMemo(
    () => buildResources(payload, knownGpuIndexes),
    [knownGpuIndexes, payload],
  );

  useEffect(() => {
    currentSamples.current = Object.fromEntries(
      resources.map(resource => [resource.key, resource.sample]),
    );
  }, [resources]);

  useEffect(() => {
    if (reducedMotion) return;
    const timer = window.setInterval(() => {
      if (!hasTelemetry.current || ownerDocument.hidden) return;
      setSamplesByKey(current => {
        const next = {...current};
        Object.entries(currentSamples.current).forEach(([key, sample]) => {
          const samples = current[key] || ZERO_SAMPLES;
          next[key] = [...samples.slice(1), {...sample}];
        });
        return next;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [reducedMotion, ownerDocument]);

  return <section
    className={`overview-live-performance deck-instrument${telemetry ? " is-live" : " is-waiting"}`}
    aria-label="Live performance widget"
  >
    <div className="live-performance-grid">
      {resources.map(resource => <PerformancePanel
        key={resource.key}
        resource={resource}
        samples={samplesByKey[resource.key] || ZERO_SAMPLES}
        live={Boolean(telemetry)}
      />)}
    </div>
  </section>;
}
