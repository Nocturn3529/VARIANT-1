import {
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";
import {asRecord as record} from "../state/storePrimitives";

type UnknownRecord = Record<string, unknown>;

type ProviderRow = {
  key: string;
  color: string;
  name: string;
  calls: number;
  costUsd: number;
  costStatus: string;
  lastModel: string;
  cachedPromptTokens: number;
  limits: UnknownRecord;
};

type ArcRow = ProviderRow & {
  dashArray: string;
  dashOffset: number;
};

type RadialTooltip = {
  provider: ProviderRow;
  left: number;
  top: number;
};

const PROVIDER_COLORS: Record<string, string> = {
  openai: "var(--deck-signal-cyan)",
  anthropic: "var(--deck-signal-indigo)",
  gemini: "var(--deck-signal-lime)",
  xai: "var(--deck-signal-magenta)",
  nvidia: "var(--deck-signal-orange)",
};
const RING_RADIUS = 154;
const CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
const ARC_GAP = 5;

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function numeric(value: unknown): number {
  return finiteNumber(value) ?? 0;
}

function string(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function compact(value: unknown): string {
  const number = numeric(value);
  if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
  if (number >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 0 : 1)}K`;
  return String(Math.round(number));
}

function money(value: unknown, status = "exact"): string {
  if (value == null || status === "unavailable") return "—";
  const number = numeric(value);
  const decimals = number > 0 && number < .01 ? 4 : 2;
  const qualified = status === "estimated" || status === "partial";
  return `$${number.toFixed(decimals)}${qualified ? "*" : ""}`;
}

function costDetail(status: string): string {
  if (status === "exact") return "Provider-reported charge";
  if (status === "estimated") return "Verified list-price estimate";
  if (status === "partial") return "Known-price usage · some calls unpriced";
  return "Pricing unavailable for recorded model";
}

function providerRows(usage: UnknownRecord): ProviderRow[] {
  return Object.entries(record(usage.providers)).map(([key, raw]) => {
    const row = record(raw);
    return {
      key,
      color: PROVIDER_COLORS[key] || "var(--deck-signal-warm)",
      name: string(row.name, key),
      calls: numeric(row.calls),
      costUsd: numeric(row.cost_usd),
      costStatus: string(row.cost_status, "unavailable"),
      lastModel: string(row.last_model) || string(record(row.limits).model, "No request yet"),
      cachedPromptTokens: numeric(row.cached_prompt_tokens),
      limits: record(row.limits),
    };
  }).sort((left, right) => (
    right.costUsd - left.costUsd || right.calls - left.calls
  ));
}

function SummaryCard({label, value, detail, warning = false}: {
  label: string;
  value: string;
  detail: string;
  warning?: boolean;
}) {
  return <article className="deck-metric">
    <span className="deck-metric__label">{label}</span>
    <strong className="deck-metric__value">{value}</strong>
    <small className={`deck-metric__detail${warning ? " is-warning" : ""}`}>{detail}</small>
  </article>;
}

function LimitRow({label, limit, color}: {
  label: string;
  limit: UnknownRecord;
  color: string;
}) {
  const maximum = finiteNumber(limit.limit);
  const used = finiteNumber(limit.used);
  if (maximum === null || used === null) {
    return <div className="api-cost-limit-row is-unavailable">
      <span>{label}</span><div className="api-cost-limit-track" /><span>Unavailable</span>
    </div>;
  }
  const percentage = Math.min(100, used / Math.max(1, maximum) * 100);
  return <div className="api-cost-limit-row">
    <span>{label}</span>
    <div className="api-cost-limit-track"><span style={{width: `${percentage}%`, background: color}} /></div>
    <span>{compact(used)} / {compact(maximum)}</span>
  </div>;
}

export function ApiCostLimitsWidget({
  telemetry,
}: {
  telemetry: Record<string, unknown> | null;
}) {
  const envelope = record(telemetry);
  const usage = envelope.cloud_usage ? record(envelope.cloud_usage) : envelope;
  const total = record(usage.total);
  const providers = useMemo(() => providerRows(usage), [usage]);
  const radialWrap = useRef<HTMLDivElement>(null);
  const [focusedProvider, setFocusedProvider] = useState<string | null>(null);
  const [tooltip, setTooltip] = useState<RadialTooltip | null>(null);

  const calls = numeric(total.calls);
  const costStatus = string(total.cost_status, "unavailable");
  const trackedCost = numeric(total.cost_usd);
  const budget = finiteNumber(total.budget_usd);
  const budgetUsed = numeric(total.budget_used_pct);
  const monthElapsed = numeric(total.month_elapsed_pct);
  const projected = finiteNumber(total.projected_cost_usd);
  const projectedOver = budget !== null && projected !== null && projected > budget;
  const todayCost = total.today_cost_usd == null
    ? calls ? "—" : "$0.00"
    : money(total.today_cost_usd, costStatus);
  const monthCost = calls ? money(total.cost_usd, costStatus) : "$0.00";
  const remainingBudget = total.remaining_budget_usd == null
    ? "Not set"
    : `$${numeric(total.remaining_budget_usd).toFixed(2)}`;
  const cachedTokens = numeric(total.cached_prompt_tokens);
  const issueCount = providers.filter(provider => (
    ["throttled", "error"].includes(string(provider.limits.health))
  )).length;
  const headerState = issueCount
    ? `${issueCount} provider issue${issueCount === 1 ? "" : "s"}`
    : calls ? "Provider telemetry live" : "Waiting for cloud usage";

  const arcs = useMemo(() => {
    const denominator = budget && budget > 0 ? budget : trackedCost;
    let offset = 0;
    return providers.reduce<ArcRow[]>((rows, provider) => {
      const providerCost = provider.costStatus === "unavailable" ? 0 : provider.costUsd;
      if (denominator <= 0 || providerCost <= 0) return rows;
      const length = CIRCUMFERENCE * Math.min(providerCost / denominator, 1);
      const visible = Math.max(0, length - ARC_GAP);
      rows.push({
        ...provider,
        dashArray: `${visible} ${CIRCUMFERENCE - visible}`,
        dashOffset: -offset,
      });
      offset += length;
      return rows;
    }, []);
  }, [budget, providers, trackedCost]);

  const showTooltip = (event: MouseEvent<SVGCircleElement>, provider: ProviderRow) => {
    const bounds = radialWrap.current?.getBoundingClientRect();
    if (!bounds) return;
    setFocusedProvider(provider.key);
    setTooltip({
      provider,
      left: Math.min(bounds.width - 175, Math.max(8, event.clientX - bounds.left + 12)),
      top: Math.min(bounds.height - 78, Math.max(8, event.clientY - bounds.top - 30)),
    });
  };

  const hasBudget = budget !== null && budget > 0;
  const centerLabel = hasBudget ? "Used" : "Tracked";
  const centerValue = hasBudget
    ? `${budgetUsed.toFixed(1)}%`
    : calls ? money(trackedCost, costStatus) : "$0.00";
  const centerDetail = hasBudget
    ? `${money(trackedCost, costStatus)} / $${budget.toFixed(2)}`
    : calls ? costDetail(costStatus) : "No cloud calls this month";
  const resetProvider = providers.find(provider => {
    const requests = record(provider.limits.requests);
    const tokens = record(provider.limits.tokens);
    return provider.limits.retry_after || requests.reset || tokens.reset;
  });
  const resetCopy = resetProvider ? (() => {
    const requests = record(resetProvider.limits.requests);
    const tokens = record(resetProvider.limits.tokens);
    const retry = string(resetProvider.limits.retry_after);
    return `${resetProvider.name} · ${retry ? `retry in ${retry}s` : string(requests.reset) || string(tokens.reset)}`;
  })() : "No reset information reported";
  const throttled = providers.some(provider => string(provider.limits.health) === "throttled");

  return <section
    className={`overview-api-cost deck-instrument${telemetry ? " is-live" : " is-waiting"}${issueCount ? " has-provider-issue" : ""}`}
    aria-labelledby="api-cost-title"
  >
    <header className="api-cost-header deck-instrument__header">
      <div className="deck-instrument__heading">
        <span className="api-cost-eyebrow deck-instrument__eyebrow">Overview / Widget 04</span>
        <div className="deck-instrument__title-row">
          <h2 className="deck-instrument__title" id="api-cost-title">API cost &amp; limits</h2>
          <span
            className="api-cost-header-state deck-instrument__status"
            data-state={issueCount ? "warning" : telemetry ? "live" : "waiting"}
          ><i />{headerState}</span>
        </div>
        <p className="deck-instrument__description">Cloud providers · Current billing month</p>
      </div>
      <div className="deck-instrument__controls">
        <span className="api-cost-telemetry-label">{telemetry ? "Live" : "Waiting"} · Provider telemetry</span>
      </div>
    </header>

    <div className="api-cost-summary deck-metric-rail" aria-label="Cost summary">
      <SummaryCard label="Today" value={todayCost} detail={costDetail(costStatus)} />
      <SummaryCard
        label="This month"
        value={monthCost}
        detail={`${string(usage.period, "Current month")} · ${calls.toLocaleString()} calls${cachedTokens ? ` · ${compact(cachedTokens)} cached` : ""}`}
      />
      <SummaryCard
        label="Remaining"
        value={remainingBudget}
        detail={budget === null ? "Monthly budget not configured" : `Of $${budget.toFixed(2)} monthly budget`}
      />
      <SummaryCard
        label="Projected"
        value={projected === null ? "—" : money(projected, costStatus)}
        detail={projected === null ? "Insufficient priced usage" : projectedOver ? `$${(projected - (budget || 0)).toFixed(2)} above budget` : "At current monthly pace"}
        warning={projectedOver}
      />
    </div>

    <div className="api-cost-body">
      <section className="api-cost-budget deck-section" aria-labelledby="api-budget-heading">
        <header className="api-cost-section-heading deck-section-band">
          <div className="deck-section-band__heading"><span className="api-cost-eyebrow deck-section-band__eyebrow">Budget allocation</span><h3 className="deck-section-band__title" id="api-budget-heading">Monthly burn</h3></div>
          <span>Resets in {numeric(total.days_remaining)} days</span>
        </header>

        <div className="api-cost-radial-layout">
          <div className="api-cost-radial-wrap" ref={radialWrap}>
            <svg className="api-cost-radial" viewBox="0 0 420 420" role="img" aria-label="Monthly API budget used by provider">
              <circle className="api-cost-ring-track" cx="210" cy="210" r={RING_RADIUS} />
              <g
                className={`api-cost-budget-arcs${focusedProvider ? " is-filtering" : ""}`}
                transform="rotate(-90 210 210)"
              >
                {arcs.map(arc => <circle
                  key={arc.key}
                  className={`api-cost-budget-arc${focusedProvider === arc.key ? " is-focused" : ""}`}
                  cx="210"
                  cy="210"
                  r={RING_RADIUS}
                  pathLength={CIRCUMFERENCE}
                  strokeDasharray={arc.dashArray}
                  strokeDashoffset={arc.dashOffset}
                  style={{"--api-arc-color": arc.color} as CSSProperties}
                  onMouseEnter={event => showTooltip(event, arc)}
                  onMouseMove={event => showTooltip(event, arc)}
                  onMouseLeave={() => { setFocusedProvider(null); setTooltip(null); }}
                />)}
              </g>
              <circle className="api-cost-inner-ring" cx="210" cy="210" r="118" />
            </svg>
            <div className="api-cost-radial-center">
              <span>{centerLabel}</span><strong>{centerValue}</strong><small>{centerDetail}</small>
            </div>
            {tooltip && <div className="api-cost-radial-tooltip" style={{left: tooltip.left, top: tooltip.top}}>
              <span>{tooltip.provider.name}</span>
              <strong>{money(tooltip.provider.costUsd, tooltip.provider.costStatus)}</strong>
              <small>{costDetail(tooltip.provider.costStatus)} · {tooltip.provider.calls.toLocaleString()} calls</small>
            </div>}
          </div>

          <div className={`api-cost-provider-list${focusedProvider ? " is-filtering" : ""}`}>
            {providers.length ? providers.map(provider => <article
              key={provider.key}
              className={`api-cost-provider${focusedProvider === provider.key ? " is-focused" : ""}`}
              onMouseEnter={() => setFocusedProvider(provider.key)}
              onMouseLeave={() => setFocusedProvider(null)}
              title={costDetail(provider.costStatus)}
            >
              <i style={{background: provider.color}} />
              <div><span>{provider.name}</span><small>{provider.lastModel} · {provider.calls.toLocaleString()} calls{provider.cachedPromptTokens ? ` · ${compact(provider.cachedPromptTokens)} cached tokens` : ""}</small></div>
              <strong>{money(provider.costUsd, provider.costStatus)}</strong>
            </article>) : <div className="api-cost-empty">No cloud provider activity recorded this month.</div>}
          </div>
        </div>

        <div className="api-cost-pace">
          <div><span>Budget pace</span><strong>{hasBudget ? "Actual spend" : "No monthly limit"}</strong><em>{hasBudget ? `${budgetUsed.toFixed(1)}%` : "—"}</em></div>
          <div className="api-cost-pace-track">
            {hasBudget && <i style={{left: `${monthElapsed}%`}} />}
            <span style={{width: `${hasBudget ? Math.min(100, budgetUsed) : 0}%`}} />
          </div>
          <div className="api-cost-pace-labels">
            <span>0%</span><span>{hasBudget ? `Expected today · ${monthElapsed.toFixed(1)}%` : "Monthly budget not configured"}</span><span>100%</span>
          </div>
        </div>
      </section>

      <section className="api-cost-limits deck-section" aria-labelledby="api-limits-heading">
        <header className="api-cost-section-heading deck-section-band">
          <div className="deck-section-band__heading"><span className="api-cost-eyebrow deck-section-band__eyebrow">Provider status</span><h3 className="deck-section-band__title" id="api-limits-heading">Limits &amp; health</h3></div>
          <span className="api-cost-live-label"><i />Live</span>
        </header>
        <div className="api-cost-health-list">
          {providers.length ? providers.map(provider => {
            const health = string(provider.limits.health, provider.calls ? "operational" : "not used");
            const healthColor = health === "operational" ? "var(--deck-signal-lime)"
              : health === "throttled" ? "var(--deck-signal-orange)"
                : health === "error" ? "var(--deck-signal-red)" : "var(--deck-text-faint)";
            return <article className="api-cost-health-card" key={provider.key}>
              <div><span><i style={{background: provider.color}} />{provider.name}</span><em style={{color: healthColor}}>{health}</em></div>
              <LimitRow label="Requests" limit={record(provider.limits.requests)} color={provider.color} />
              <LimitRow label="Tokens" limit={record(provider.limits.tokens)} color={provider.color} />
            </article>;
          }) : <div className="api-cost-empty is-health">Limits appear after the first cloud response.</div>}
        </div>
        <div className="api-cost-limit-note">
          <span>Next limit reset</span><strong>{resetCopy}</strong><small>{throttled ? "At least one provider is currently throttled." : "No requests are currently throttled."}</small>
        </div>
      </section>
    </div>
  </section>;
}
