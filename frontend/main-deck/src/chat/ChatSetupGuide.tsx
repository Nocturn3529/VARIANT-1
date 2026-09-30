import {useChatState, setChatDraft} from "../chatStore";
import {usePlatformState} from "../store";
import {useChatProjects, chooseChatProject} from "../state/chatProjectStore";
import {navigateTo, selectSettingsCategory} from "../state/appStore";
import {Button} from "../ui/Button";
import {detachedChatId} from "../runtime/viewIdentity";

/** Read setup readiness from existing backend projections; never infer a successful connection. */
export function ChatSetupGuide() {
  const chat = useChatState();
  const {config} = usePlatformState();
  const projects = useChatProjects();
  const id = chat.sessionId || "";
  const project = projects.projects[id];
  const ready = chat.connected && config.model_ready === true;
  const detached = !!detachedChatId();
  const configure = () => { selectSettingsCategory("providers"); navigateTo("settings"); };
  return <section className="chat-setup-guide" aria-label="Get started">
    <strong>What are we working on?</strong>
    <p>A conversation with a persistent Python workspace.</p>
    <ol>
      <li><div><strong>Connect a model</strong><small role="status">{!chat.connected ? "Waiting for the backend" : ready ? "Model ready" : config.model_ready === false ? "Choose an account, API key, or local model" : "Checking the selected model…"}</small></div>
        <Button tone="quiet" disabled={detached} title={detached ? "Configure your model in the main workspace" : undefined} onClick={configure}>{ready ? "Model settings" : "Connect model"}</Button></li>
      <li><div><strong>Choose a project <small>Optional</small></strong><small>{project?.name || "For work with local files"}</small></div>
        <Button tone="quiet" disabled={!chat.connected || !id || !!projects.pending[id] || !window.variant1Deck?.pickFolder} title={!window.variant1Deck?.pickFolder ? "Choose a project in the main workspace" : undefined} onClick={() => void chooseChatProject(id)}>{project ? "Change project" : "Choose folder"}</Button></li>
      <li><div><strong>Try a first task</strong><small>Inspect its Python steps and results as it runs.</small></div>
        <Button tone="quiet" disabled={!ready || !id} onClick={() => setChatDraft(project ? "Give me a concise overview of this project." : "Use Python to calculate the average of 12, 18, and 24, and explain the result.")}>Use an example</Button></li>
    </ol>
    {config.startup_error ? <p role="alert">{config.startup_error}</p> : null}
    {projects.errors[id] ? <p role="alert">{projects.errors[id]}</p> : null}
    <small>Enter to send · Ctrl K for actions</small>
  </section>;
}
