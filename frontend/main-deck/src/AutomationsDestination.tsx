import {useMemo, useState, type FormEvent} from "react";
import {
  automationKind,
  closeAutomationBuilder,
  openAutomationBuilder,
  relativeAutomationTime,
  removeAutomation,
  runAutomation,
  saveAutomation,
  setAutomationFilter,
  toggleAutomation,
  useAutomationState,
  type AutomationItem,
  type AutomationTrigger,
} from "./automationStore";
import {isActionSurface} from "./chat/runtimeProfile";
import {Button} from "./ui/Button";
import {EmptyState} from "./ui/EmptyState";
import {Switch} from "./ui/Switch";

function triggerValue(item: AutomationItem | null, key: string, fallback: string): string {
  const value = item?.trigger?.[key as keyof AutomationTrigger];
  return value == null ? fallback : String(value);
}

function AutomationBuilder({item}: {item: AutomationItem | null}) {
  const {pendingSave, saveError} = useAutomationState();
  const saving = !!pendingSave;
  const [name, setName] = useState(item?.name || "");
  const [prompt, setPrompt] = useState(item?.prompt || "");
  const [triggerType, setTriggerType] = useState(item?.trigger?.type || "daily");
  const retainedTrigger = !!item?.trigger && !["daily","weekdays","weekly","interval","cron"].includes(triggerType);
  const [time, setTime] = useState(triggerValue(item, "time", "08:00"));
  const [day, setDay] = useState(triggerValue(item, "day", "mon"));
  const [seconds, setSeconds] = useState(triggerValue(item, "seconds", "3600"));
  const [expr, setExpr] = useState(triggerValue(item, "expr", "0 9 * * *"));
  const [misfire, setMisfire] = useState<"latest" | "skip">(item?.misfire_policy || "latest");

  function submit(event: FormEvent) {
    event.preventDefault();
    let trigger: AutomationTrigger;
    if (retainedTrigger) trigger = {...item!.trigger};
    else if (triggerType === "weekly") trigger = {type: "weekly", day, time};
    else if (triggerType === "interval") {
      trigger = {type: "interval", seconds: Math.max(60, Number(seconds) || 3600)};
    } else if (triggerType === "cron") trigger = {type: "cron", expr: expr.trim()};
    else trigger = {type: triggerType, time};
    saveAutomation({name, prompt, trigger, misfire_policy: misfire});
  }

  return <form className="automation-builder deck-instrument" onSubmit={submit}>
    <div className="automation-builder__heading deck-instrument__header">
      <div className="deck-instrument__heading">
        <span className="deck-instrument__eyebrow">Automation definition</span>
        <h2 className="deck-instrument__title">{item ? "Edit automation" : "New automation"}</h2>
        <p className="deck-instrument__description">Pair one instruction with a predictable schedule or local event.</p>
      </div>
      <Button tone="icon" aria-label="Close automation builder" onClick={closeAutomationBuilder}>×</Button>
    </div>
    <fieldset disabled={saving} className="automation-builder__grid">
      <label><span>Name</span><input autoFocus maxLength={80} value={name} onChange={e => setName(e.target.value)} placeholder="Morning briefing"/></label>
      <label className="automation-builder__instruction"><span>Instruction</span><textarea rows={3} maxLength={2000} value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Summarize my priorities and send me the result."/></label>
      <label><span>Trigger</span><select value={triggerType} onChange={e => setTriggerType(e.target.value)}>
        {retainedTrigger ? <option value={triggerType}>Existing {triggerType} trigger</option> : null}
        <option value="daily">Daily</option><option value="weekdays">Weekdays</option>
        <option value="weekly">Weekly</option><option value="interval">Interval</option>
        <option value="cron">Cron expression</option>
      </select></label>
      {retainedTrigger ? <p>Event configuration is preserved when you save this automation.</p> : null}
      {(triggerType === "daily" || triggerType === "weekdays") && <label><span>At</span><input type="time" value={time} onChange={e => setTime(e.target.value)}/></label>}
      {triggerType === "weekly" && <><label><span>Day</span><select value={day} onChange={e => setDay(e.target.value)}>{["mon","tue","wed","thu","fri","sat","sun"].map(value => <option value={value} key={value}>{value.toUpperCase()}</option>)}</select></label><label><span>At</span><input type="time" value={time} onChange={e => setTime(e.target.value)}/></label></>}
      {triggerType === "interval" && <label><span>Every (seconds)</span><input type="number" min={60} value={seconds} onChange={e => setSeconds(e.target.value)}/></label>}
      {triggerType === "cron" && <label><span>Cron</span><input value={expr} onChange={e => setExpr(e.target.value)} placeholder="0 9 * * *" required/></label>}
      <label><span>Missed schedule</span><select value={misfire} onChange={e => setMisfire(e.target.value as "latest" | "skip")}><option value="latest">Run latest once</option><option value="skip">Skip</option></select></label>
    </fieldset>
    {saveError ? <p className="workbench-tool-error" role="alert">{saveError}</p> : null}
    <div className="automation-builder__actions"><Button onClick={closeAutomationBuilder}>Cancel</Button><Button tone="primary" type="submit" disabled={saving}>{saving ? "Saving…" : item ? "Save changes" : "Create automation"}</Button></div>
  </form>;
}

function runtimeLabel(item: AutomationItem): string {
  const runtime = item.runtime;
  if (!runtime) return "Runtime status unavailable";
  if (runtime.error) return `Runtime blocked · ${runtime.error}`;
  if (runtime.assignment_state !== "pinned") return "Runtime not assigned";
  const surface = isActionSurface(runtime.action_surface)
    ? "VARIANT-1 environment"
    : "Retired runtime";
  const kernelState = runtime.busy ? "busy" : runtime.kernel.state || "absent";
  const mutation = isActionSurface(runtime.action_surface)
    ? " · mutation off"
    : "";
  return `${surface}${mutation} · kernel ${kernelState}`;
}

function AutomationCard({item}: {item: AutomationItem}) {
  const kind = automationKind(item);
  return <article className="automation-card deck-data-row" data-kind={kind}>
    <div className="automation-card__icon" aria-hidden="true">{kind === "event" ? <svg viewBox="0 0 24 24"><path d="M4 12h4l2-7 4 14 2-7h4"/></svg> : <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 8v5l3 2"/></svg>}</div>
    <div className="automation-card__copy">
      <div><strong>{item.name}</strong><span>{kind === "event" ? "Event" : "Scheduled"}</span></div>
      <p>{item.prompt || "No instruction saved."}</p>
      <small>{item.schedule} · Last {relativeAutomationTime(item.last_run)}</small>
      {item.schedule_error && <small className="automation-card__runtime">Schedule blocked · {item.schedule_error}</small>}
      <small className="automation-card__runtime" title={item.runtime?.warning || item.runtime?.graph_revision || ""}>{runtimeLabel(item)}</small>
    </div>
    <div className="automation-card__actions">
      <Button tone="quiet" onClick={() => runAutomation(item.id)}>Run</Button>
      <Button tone="quiet" onClick={() => openAutomationBuilder(item)}>Edit</Button>
      <Button tone="quiet" className="automation-card__delete" onClick={() => removeAutomation(item.id, item.name)}>Delete</Button>
    </div>
    <div className="automation-toggle">
      <Switch
        checked={item.enabled}
        caption={item.enabled ? "On" : "Off"}
        aria-label={`${item.name} ${item.enabled ? "on" : "off"}`}
        onChange={next => toggleAutomation(item.id, next)}
      />
    </div>
  </article>;
}

export function AutomationsDestination() {
  const state = useAutomationState();
  const visible = useMemo(() => state.items.filter(item => {
    if (state.filter === "all") return true;
    if (state.filter === "active") return item.enabled;
    return automationKind(item) === state.filter;
  }), [state.items, state.filter]);
  const active = state.items.filter(item => item.enabled).length;
  const recent = state.history.filter(run => Number(run.finished_at || 0) >= Date.now() / 1000 - 7 * 86400);
  const healthy = recent.length
    ? Math.round(recent.filter(run => run.status === "ok").length / recent.length * 100)
    : null;

  return <div className="automations-scroll deck-destination-scroll">
    <div className="automations-page deck-destination-page">
    {state.builderOpen && <AutomationBuilder key={state.editorGeneration} item={state.editing}/>}
    <section className="automations-console deck-instrument" aria-labelledby="automations-library-title">
      <header className="automations-console__header deck-instrument__header">
        <div className="deck-instrument__heading">
          <span className="deck-instrument__eyebrow">Task orchestration</span>
          <h2 className="deck-instrument__title" id="automations-library-title">Automation library</h2>
          <p className="deck-instrument__description">Schedules, event triggers, runtime assignment, and recent outcomes.</p>
        </div>
        <div className="automations-console__actions">
          <div className="automations-console__signals">
            <span className="automations-console__connection" data-state={state.connected ? "online" : "offline"}>
              {state.connected ? "Connected" : "Offline"}
            </span>
            <span className="automations-console__state deck-status" data-state={active ? "live" : "idle"}>
              {active ? `${active} active` : "No active tasks"}
            </span>
          </div>
          <Button
            className="automations-create"
            aria-label="New automation — schedule or trigger"
            onClick={() => openAutomationBuilder()}
          >
            <span className="automations-create__mark" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>
            </span>
            <span className="automations-create__copy">
              <strong>New automation</strong>
              <small>Schedule or trigger</small>
            </span>
          </Button>
        </div>
      </header>
      <section className="automations-metrics deck-metric-rail" aria-label="Automation metrics">
        <article className="deck-metric">
          <span className="deck-metric__label">Active</span>
          <strong className="deck-metric__value">{active}</strong>
          <small className="deck-metric__detail">of {state.items.length} definitions</small>
        </article>
        <article className="deck-metric">
          <span className="deck-metric__label">Runs retained</span>
          <strong className="deck-metric__value">{state.historyCount}</strong>
          <small className="deck-metric__detail">local history</small>
        </article>
        <article className="deck-metric">
          <span className="deck-metric__label">7-day health</span>
          <strong className="deck-metric__value">{healthy === null ? "—" : `${healthy}%`}</strong>
          <small className="deck-metric__detail">{recent.length} recent runs</small>
        </article>
      </section>
      <div className="automations-grid">
        <section className="automations-list-panel deck-section">
          <div className="automations-toolbar deck-section__header">
            <div className="deck-segmented">{(["all", "active", "scheduled", "event"] as const).map(filter => <button type="button" key={filter} className={state.filter === filter ? "active" : ""} aria-pressed={state.filter === filter} onClick={() => setAutomationFilter(filter)}>{filter[0].toUpperCase() + filter.slice(1)}</button>)}</div>
            <span>{visible.length} shown</span>
          </div>
          <div className="automations-list deck-data-list">{visible.map(item => <AutomationCard key={item.id} item={item}/>)}{!visible.length && <EmptyState tone="panel" title={state.items.length ? "No matching automations" : "No automations yet"} description="Create one to schedule a prompt or respond to a local event." />}</div>
        </section>
        <aside className="automations-history deck-section">
          <div className="automations-history__heading deck-section__header"><span>Recent runs</span><strong>Activity</strong></div>
          <ol className="deck-data-list">{state.history.slice(0, 12).map((run, index) => {const item = state.items.find(candidate => candidate.id === run.automation_id); return <li className="deck-data-row" data-status={run.status} key={`${run.automation_id}-${run.finished_at || index}`}><i/><div><strong>{item?.name || "Removed automation"}</strong><p>{run.summary || run.status}</p><small>{relativeAutomationTime(run.finished_at)}</small></div></li>;})}</ol>
          {!state.history.length && <EmptyState title="No runs recorded yet" />}
        </aside>
      </div>
    </section>
    </div>
  </div>;
}
