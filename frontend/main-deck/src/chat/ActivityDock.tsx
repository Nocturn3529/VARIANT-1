import {useEffect, useId, useState, type KeyboardEvent} from "react";
import {useChatSelection, shallowChatSelection} from "../chatStore";
import {useTerminalState, processFinished} from "../context/terminalStore";
import {Icon} from "../ui/Icon";
import {StatusGlyph, type GlyphState} from "../motion/StatusGlyph";
import {AgentTeamPanel} from "./AgentTeamPanel";
import {InputQueuePanel} from "./InputQueuePanel";
import {ComposerGoalPanel} from "./ComposerGoalPanel";
import {BackgroundProcesses, processGlyph, useProcessRefresh} from "./BackgroundProcesses";
import {queueAdmissionPending} from "./inputQueue";

type Section = "agents" | "queue" | "goal" | "processes";
type Chip = {section: Section; glyph: GlyphState; text: string; detail: string; label: string; live: boolean; attention: string};

/* Open section per chat survives chat switches; attention keys open a section once. */
const openByChat = new Map<string, Section | null>();
const seenAttention = new Set<string>();
const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;
const DONE_STEP = /succeed|complete|done|finish/;

function goalGlyph(status: string, pending: boolean): GlyphState {
  if (pending) return "queued";
  if (["running", "queued"].includes(status)) return "running";
  if (status === "waiting_user") return "attention";
  if (status === "blocked") return "blocked";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "stopped";
  if (["succeeded", "archived"].includes(status)) return "done";
  return "idle";
}

/**
 * One compact activity surface above the composer. Collapsed, it is a row of
 * live chips; expanded, exactly one section opens with a single scroll area,
 * so subagents, queued input, goals and processes never push the transcript
 * off screen. Collapsed sections stay mounted (hidden) to keep their state.
 */
export function ActivityDock() {
  const {sessionId, connected, agentTeam: team, inputQueue: queue, goal} = useChatSelection(state => ({
    sessionId: state.sessionId, connected: state.connected, agentTeam: state.agentTeam, inputQueue: state.inputQueue, goal: state.goal,
  }), shallowChatSelection);
  const chatId = sessionId || "";
  const processes = useTerminalState(chatId || undefined).processes;
  useProcessRefresh(chatId, processes, connected);
  const [open, setOpenState] = useState<Section | null>(() => openByChat.get(chatId) ?? null);
  useEffect(() => { setOpenState(openByChat.get(chatId) ?? null); }, [chatId]);
  const setOpen = (next: Section | null) => { openByChat.set(chatId, next); setOpenState(next); };
  const panelId = useId();

  const chips: Chip[] = [];
  if (team.agents.length || team.error) {
    const blocked = team.blocked, working = team.active;
    chips.push({section: "agents", live: working > 0,
      glyph: team.error ? "failed" : blocked ? "blocked" : working ? "running" : "done",
      text: working ? plural(working, "agent") : plural(team.agents.length, "agent"),
      detail: blocked ? `${blocked} blocked` : working ? "working" : "finished",
      label: `Subagents: ${working} working${blocked ? `, ${blocked} blocked` : ""}, ${team.agents.length} total`,
      attention: blocked || team.error ? `agents:${chatId}:${blocked}:${team.error || ""}` : ""});
  }
  const items = queue.snapshot?.items || [];
  if (items.length || queue.action || queue.error || queueAdmissionPending()) {
    const parked = items.filter(item => item.state === "parked");
    chips.push({section: "queue", live: false, glyph: parked.length || queue.error ? "attention" : "queued",
      text: plural(items.length, "message"), detail: parked.length ? `${parked.length} parked` : "queued",
      label: `Queued messages: ${items.length}${parked.length ? `, ${parked.length} waiting for you` : ""}`,
      attention: parked.length || queue.error ? `queue:${chatId}:${parked.map(item => item.ticket_id).join(",")}:${queue.error || ""}` : ""});
  }
  if (goal.snapshot || goal.pending || goal.error) {
    const record = goal.snapshot?.goal, steps = goal.snapshot?.steps || [];
    const done = steps.filter(step => DONE_STEP.test(step.status)).length;
    const status = record?.status || "";
    const needsYou = status === "waiting_user" || status === "blocked" || status === "failed" || goal.snapshot?.cleanup.status === "failed" || !!goal.error;
    chips.push({section: "goal", live: ["running", "queued"].includes(status), glyph: goal.error ? "failed" : goalGlyph(status, !!goal.pending && !record),
      text: steps.length ? `Goal ${done}/${steps.length}` : "Goal", detail: record ? status.replaceAll("_", " ") : "pending",
      label: `Goal${record ? `: ${record.title || record.objective}` : ""}, ${record ? status.replaceAll("_", " ") : "awaiting confirmation"}${steps.length ? `, ${done} of ${steps.length} steps` : ""}`,
      attention: needsYou ? `goal:${chatId}:${record?.goal_id || ""}:${status}:${goal.error || ""}` : ""});
  }
  if (processes.length) {
    const running = processes.filter(process => !processFinished(process.state)).length;
    const failed = processes.filter(process => processGlyph(process) === "failed").length;
    chips.push({section: "processes", live: running > 0, glyph: running ? "running" : failed ? "failed" : "done",
      text: plural(running || processes.length, "process", "processes"), detail: running ? "running" : failed ? `${failed} failed` : "finished",
      label: `Background processes: ${running} running of ${processes.length}`,
      attention: failed ? `processes:${chatId}:${processes.filter(process => processGlyph(process) === "failed").map(process => process.id).join(",")}` : ""});
  }

  const present = new Set(chips.map(chip => chip.section));
  const attention = chips.find(chip => chip.attention && !seenAttention.has(chip.attention));
  useEffect(() => {
    if (!attention) return;
    seenAttention.add(attention.attention);
    setOpen(attention.section);
  });
  if (!chips.length) return null;
  const shown = open && present.has(open) ? open : null;
  const close = (event: KeyboardEvent) => {
    if (event.key !== "Escape" || !shown) return;
    event.preventDefault(); event.stopPropagation(); setOpen(null);
  };
  return <section className={`activity-dock${shown ? " is-open" : ""}`} aria-label="Activity" onKeyDown={close}>
    <div className="activity-dock__panel" id={panelId} hidden={!shown}>
      <div hidden={shown !== "agents"}><AgentTeamPanel/></div>
      <div hidden={shown !== "queue"}><InputQueuePanel/></div>
      <div hidden={shown !== "goal"}><ComposerGoalPanel/></div>
      <div hidden={shown !== "processes"}><BackgroundProcesses chatId={chatId} processes={processes}/></div>
    </div>
    <div className="activity-dock__bar" role="toolbar" aria-label="Activity summary">
      {chips.map(chip => <button key={chip.section} type="button" className={`activity-chip${chip.live ? " is-live" : ""}${chip.attention ? " needs-attention" : ""}`}
        data-section={chip.section} aria-expanded={shown === chip.section} aria-controls={panelId} aria-label={chip.label}
        onClick={() => setOpen(shown === chip.section ? null : chip.section)}>
        <StatusGlyph state={chip.glyph}/>
        <span className="activity-chip__text">{chip.text}</span>
        <span className="activity-chip__detail">{chip.detail}</span>
      </button>)}
      {shown ? <button type="button" className="activity-dock__collapse" aria-label="Collapse activity" onClick={() => setOpen(null)}><Icon name="down"/></button> : null}
    </div>
  </section>;
}
