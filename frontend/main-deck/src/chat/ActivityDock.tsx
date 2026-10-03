import {useEffect, useId, useState} from "react";
import {useChatSelection, shallowChatSelection} from "../chatStore";
import {useTerminalState, processFinished} from "../context/terminalStore";
import {Icon} from "../ui/Icon";
import {Overlay} from "../ui/Overlay";
import {ActivityMark, type MarkState} from "../motion/ActivityMark";
import {AgentTeamPanel} from "./AgentTeamPanel";
import {InputQueuePanel} from "./InputQueuePanel";
import {ComposerGoalPanel} from "./ComposerGoalPanel";
import {BackgroundProcesses, processMark, useProcessRefresh} from "./BackgroundProcesses";
import {queueAdmissionPending} from "./inputQueue";
import {closeTeamAgent} from "./agentTeam";

type Section = "agents" | "queue" | "goal" | "processes";
type Chip = {section: Section; title: string; mark: MarkState; text: string; label: string};

/* The open section per chat survives chat switches within this window. */
const openByChat = new Map<string, Section | null>();
const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;
const DONE_STEP = /succeed|complete|done|finish/;

function goalMark(status: string, pending: boolean): MarkState {
  if (pending) return "queued";
  if (["running", "queued"].includes(status)) return "live";
  if (["waiting_user", "blocked"].includes(status)) return "attention";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "stopped";
  if (["succeeded", "archived"].includes(status)) return "done";
  return "idle";
}

/**
 * Minimal live activity above the composer: one faded chip per kind of work.
 * A chip opens a centered overlay for that work; the overlay's sections stay
 * mounted (hidden) while closed so their state and controls persist.
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
  const setOpen = (next: Section | null) => {
    openByChat.set(chatId, next); setOpenState(next);
    // Leaving the agents view returns it to the roster for next time.
    if (next !== "agents" && team.selectedId) closeTeamAgent();
  };
  const titleId = useId();

  const chips: Chip[] = [];
  if (team.agents.length || team.error) {
    const working = team.active, blocked = team.blocked;
    chips.push({section: "agents", title: "Agents", mark: team.error ? "failed" : blocked ? "attention" : working ? "live" : "done",
      text: plural(working || team.agents.length, "agent"),
      label: `Subagents: ${working} working${blocked ? `, ${blocked} blocked` : ""}, ${team.agents.length} total`});
  }
  const items = queue.snapshot?.items || [];
  if (items.length || queue.action || queue.error || queueAdmissionPending()) {
    const parked = items.filter(item => item.state === "parked").length;
    chips.push({section: "queue", title: "Queued", mark: parked || queue.error ? "attention" : "queued",
      text: `${items.length} queued`, label: `Queued messages: ${items.length}${parked ? `, ${parked} waiting for you` : ""}`});
  }
  if (goal.snapshot || goal.pending || goal.error) {
    const record = goal.snapshot?.goal, steps = goal.snapshot?.steps || [];
    const done = steps.filter(step => DONE_STEP.test(step.status)).length, status = record?.status || "";
    const cleanupFailed = goal.snapshot?.cleanup.status === "failed";
    chips.push({section: "goal", title: "Goal", mark: goal.error || cleanupFailed ? "failed" : goalMark(status, !!goal.pending && !record),
      text: steps.length ? `Goal ${done}/${steps.length}` : "Goal",
      label: `Goal${record ? `: ${record.title || record.objective}` : ""}, ${record ? status.replaceAll("_", " ") : "awaiting confirmation"}${steps.length ? `, ${done} of ${steps.length} steps` : ""}`});
  }
  if (processes.length) {
    const running = processes.filter(process => !processFinished(process.state)).length;
    const failed = processes.some(process => processMark(process) === "failed");
    chips.push({section: "processes", title: "Processes", mark: running ? "live" : failed ? "failed" : "done",
      text: plural(running || processes.length, "process", "processes"),
      label: `Background processes: ${running} running of ${processes.length}`});
  }

  if (!chips.length) return null;
  const present = new Set(chips.map(chip => chip.section));
  const shown = open && present.has(open) ? open : null;
  return <div className="activity-dock">
    <div className="activity-dock__bar" role="toolbar" aria-label="Activity">
      {chips.map(chip => <button key={chip.section} type="button" className="activity-chip" data-section={chip.section} data-mark={chip.mark}
        aria-haspopup="dialog" aria-expanded={shown === chip.section} aria-label={chip.label}
        onClick={() => setOpen(shown === chip.section ? null : chip.section)}>
        <ActivityMark state={chip.mark}/>
        {/* Keyed by text so a changed count ticks in rather than swapping silently. */}
        <span key={chip.text} className="activity-chip__text">{chip.text}</span>
      </button>)}
    </div>
    <Overlay open={!!shown} onClose={() => setOpen(null)} labelledBy={titleId} className="activity-overlay">
      <header className="activity-overlay__header">
        <h1 id={titleId} className="activity-overlay__title">{chips.find(chip => chip.section === shown)?.title || "Activity"}</h1>
        {chips.length > 1 ? <nav className="activity-overlay__tabs" aria-label="Activity sections">
          {chips.map(chip => <button key={chip.section} type="button" aria-current={shown === chip.section ? "true" : undefined} onClick={() => setOpen(chip.section)}>
            <ActivityMark state={chip.mark}/>{chip.title}
          </button>)}
        </nav> : null}
        <button type="button" className="activity-overlay__close" aria-label="Close activity" onClick={() => setOpen(null)}><Icon name="close"/></button>
      </header>
      <div className="activity-overlay__body">
        <div hidden={shown !== "agents"}><AgentTeamPanel/></div>
        <div hidden={shown !== "queue"}><InputQueuePanel/></div>
        <div hidden={shown !== "goal"}><ComposerGoalPanel/></div>
        <div hidden={shown !== "processes"}>{shown === "processes" ? <BackgroundProcesses chatId={chatId} processes={processes}/> : null}</div>
      </div>
    </Overlay>
  </div>;
}
