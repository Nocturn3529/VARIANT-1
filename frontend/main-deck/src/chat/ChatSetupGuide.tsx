import {useChatState, setChatDraft} from "../chatStore";
import {useEffect, type ReactNode} from "react";
import {usePlatformState} from "../store";
import {useChatProjects, chooseChatProject} from "../state/chatProjectStore";
import {navigateTo, selectSettingsCategory} from "../state/appStore";
import {Button} from "../ui/Button";
import {Icon} from "../ui/Icon";
import {detachedChatId} from "../runtime/viewIdentity";
import {useSessionContextState, requestSessionContext} from "../sessionContextStore";

type SetupStep = {key: string; title: ReactNode; detail: string; done: boolean; action: ReactNode};

/** Read setup readiness from existing backend projections; never infer a successful connection. */
export function ChatSetupGuide() {
  const chat = useChatState();
  const {config} = usePlatformState();
  const projects = useChatProjects();
  const model = useSessionContextState();
  const id = chat.sessionId || "";
  const project = projects.projects[id];
  const connectionRevision = JSON.stringify([config.credential_revision_by_provider, config.oauth,
    config.providers?.map(provider => [provider.name, provider.api_key_configured, provider.credential_count])]);
  useEffect(() => {
    if (chat.connected && id) requestSessionContext(id);
  }, [chat.connected, id, connectionRevision, config.model_ready]);
  const configured = model.sessionId === id ? model.modelConfigured : null;
  const ready = chat.connected && model.connected && configured === true && !model.settingsPending;
  const detached = !!detachedChatId();
  const configure = () => { selectSettingsCategory("providers"); navigateTo("settings"); };
  // One primary action at a time: connect until a model is ready, then try a task.
  const steps: SetupStep[] = [
    {key: "model", title: "Connect a model", done: ready,
      detail: !chat.connected ? "Waiting for the backend" : ready ? "Model configured" : configured === false ? "Choose an account, API key, or local model" : model.route ? "Check the selected model's settings" : "Checking the selected model…",
      action: <Button tone={ready ? "quiet" : "primary"} disabled={detached} title={detached ? "Configure your model in the main workspace" : undefined} onClick={configure}>{ready ? "Model settings" : "Connect model"}</Button>},
    {key: "project", title: <>Choose a project <small>Optional</small></>, done: !!project,
      detail: project?.name || "For work with local files",
      action: <Button tone="quiet" disabled={!chat.connected || !id || !!projects.pending[id] || !window.variant1Deck?.pickFolder} title={!window.variant1Deck?.pickFolder ? "Choose a project in the main workspace" : undefined} onClick={() => void chooseChatProject(id)}>{project ? "Change project" : "Choose folder"}</Button>},
    {key: "task", title: "Try a first task", done: false,
      detail: ready ? "Inspect its Python steps and results as it runs." : "Available once a model is connected.",
      action: <Button tone={ready ? "primary" : "quiet"} disabled={!ready || !id} title={!ready ? "Connect a model first" : undefined} onClick={() => setChatDraft(project ? "Give me a concise overview of this project." : "Use Python to calculate the average of 12, 18, and 24, and explain the result.")}>Use an example</Button>},
  ];
  return <section className="chat-setup-guide" aria-label="Get started">
    <header>
      <strong>What are we working on?</strong>
      <p>A conversation with a persistent Python workspace.</p>
    </header>
    <ol>
      {steps.map((step, index) => <li key={step.key} className={step.done ? "is-done" : undefined}>
        <span className="chat-setup-guide__marker" aria-hidden="true">{step.done ? <Icon name="check"/> : index + 1}</span>
        <div>
          <strong>{step.title}{step.done ? <span className="chat-setup-guide__sr"> (done)</span> : null}</strong>
          <small role={step.key === "model" ? "status" : undefined}>{step.detail}</small>
        </div>
        {step.action}
      </li>)}
    </ol>
    {config.startup_error ? <p role="alert">{config.startup_error}</p> : null}
    {model.sessionId === id && model.settingsError ? <p role="alert">{model.settingsError}</p> : null}
    {projects.errors[id] ? <p role="alert">{projects.errors[id]}</p> : null}
    <small className="chat-setup-guide__hint">Enter to send · Ctrl K for actions</small>
  </section>;
}
