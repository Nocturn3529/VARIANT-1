import {sendChat, useChatState} from "./chatStore";
import {Button} from "./ui/Button";
import {KernelGlyph} from "./motion/KernelGlyph";
import {Icon} from "./ui/Icon";
import {currentStepLabel} from "./chat/activityModel";

export function kernelStatusLabel(connected: boolean, kernelState?: string): string {
  if (!connected) return "Kernel offline";
  if (kernelState === "ready") return "Kernel ready";
  if (kernelState === "busy") return "Kernel working";
  if (!kernelState || kernelState === "absent") return "Kernel idle";
  return `Kernel ${kernelState.replace(/_/g, " ")}`;
}

export function RuntimeDetails() {
  const {connected, runtime, sessionId, turnActive, title, turnSteps} = useChatState();
  const active = [...turnSteps].reverse().find(step => step.status === "running" && step.kind !== "thinking");
  const action = (action: "restart_kernel" | "stop_cell" | "reset_session_tools") => {
    if (connected && sessionId) sendChat({type: "chat:runtime:action", id: sessionId, action});
  };
  return <div className="runtime-details">
    <header className="runtime-summary">
      <KernelGlyph seed={`${sessionId}:${runtime?.kernelGeneration}`} size={72} mutation={runtime?.mutationEffectiveEnabled}
        phase={!connected ? "offline" : runtime?.kernelState === "busy" ? "running" : "idle"}/>
      <div><strong>{kernelStatusLabel(connected, runtime?.kernelState)}</strong><p>Python kernel{runtime?.kernelGeneration != null ? ` · generation ${runtime.kernelGeneration}` : ""}</p></div>
    </header>
    <dl className="runtime-details__rows">
      <div><dt>Current chat</dt><dd>{title || "New chat"}</dd></div>
      <div><dt>Backend</dt><dd>{connected ? "Connected locally" : "Offline"}</dd></div>
      <div><dt>Activity</dt><dd>{!connected ? "Waiting for the backend" : active ? currentStepLabel(active, true) : turnActive ? "Preparing a response" : "No active cell"}</dd></div>
      <div><dt>Mutation</dt><dd className={`mutation-indicator${runtime?.mutationEffectiveEnabled ? " is-enabled" : ""}`}>{runtime?.mutationEffectiveEnabled ? "Enabled" : "Off"}</dd></div>
    </dl>
    <details className="runtime-technical"><summary><Icon name="chevron"/>Runtime details</summary>
      <dl className="runtime-details__rows">
        <div><dt>Category</dt><dd>{runtime?.selectedCategoryId || "Not mounted"}</dd></div>
        <div><dt>Mount revision</dt><dd>{runtime?.mountRevision ?? "—"}</dd></div>
        <div><dt>Session tools</dt><dd>{runtime ? `${runtime.activeSlots} active · ${runtime.probationSlots} probation` : "—"}</dd></div>
        <div><dt>Execution</dt><dd>{runtime ? `${runtime.activeChildren} children · ${runtime.queuedInputs} queued inputs` : "—"}</dd></div>
        <div><dt>Continuation</dt><dd>{runtime?.continuationState || "—"}</dd></div>
      </dl>
      {runtime?.warning ? <p className="runtime-details__warning">{runtime.warning}</p> : null}
    </details>
    <div className="runtime-details__actions">
      {runtime?.kernelState === "busy" ? <Button onClick={() => action("stop_cell")} disabled={!connected || !sessionId}><Icon name="stop"/>Stop cell</Button> : null}
      <Button onClick={() => action("restart_kernel")} disabled={!connected || !sessionId || turnActive}><Icon name="refresh"/>Restart kernel</Button>
      <Button onClick={() => action("reset_session_tools")} disabled={!connected || !sessionId || turnActive}>Reset session tools</Button>
    </div>
  </div>;
}
