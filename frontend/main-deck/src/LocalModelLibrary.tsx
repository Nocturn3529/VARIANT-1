import {useEffect, useId, useState} from "react";
import {useLocalModels, refreshLocalModels, requestLocalModels, modelMutationPending, modelJobFinished} from "./localModelsStore";
import {openUserModelFolder} from "./generalStore";
import {useSurfaceDocument} from "./ui/SurfaceDocument";
import {SettingsSection, SettingRow} from "./ui/Settings";
import {Button} from "./ui/Button";
import {asRecord} from "./state/storePrimitives";

export function modelBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "Size unavailable";
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GB` : `${Math.ceil(value / 1024 ** 2)} MB`;
}

export function LocalModelLibrary() {
  const primaryGroup = useId();
  const state = useLocalModels(), doc = useSurfaceDocument();
  const [query, setQuery] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const snapshot = state.snapshot, files = state.files;
  const jobs = snapshot?.jobs || [];
  const working = jobs.some(job => !modelJobFinished(job.status));
  const busy = modelMutationPending(state);
  const supports = (operation: string) => state.connected && !!snapshot?.supported_actions.includes(operation);
  useEffect(() => {if (state.connected) refreshLocalModels();}, [state.connected]);
  useEffect(() => {setChosen([]);}, [files?.repo, files?.revision]);
  useEffect(() => {
    if (!state.connected || !working) return;
    const timer = setInterval(() => {if (doc.visibilityState !== "hidden") refreshLocalModels();}, 5000);
    return () => clearInterval(timer);
  }, [state.connected, working, doc]);
  const variants = files?.variants || [];
  const selected = variants.filter(item => chosen.includes(item.label));
  const selectedPaths = [...new Set(selected.flatMap(item => item.paths))];
  const total = selected.reduce((bytes, item) => bytes + item.bytes, 0);
  const canDownload = selected.every(item => item.complete) && selected.filter(item => !item.projector).length === 1 && selected.filter(item => item.projector).length <= 1;
  const hardware = snapshot?.hardware || {};
  const gpus = Array.isArray(hardware.gpus) ? hardware.gpus.map(asRecord) : [];
  return <div className="local-model-library">
    {!state.connected ? <p role="status">Backend offline. Reconnect to manage local models.</p> : null}
    {snapshot?.warning ? <p role="status" className="platform-note">{snapshot.warning}</p> : null}
    {Object.entries(state.errors).filter(([, error]) => error).map(([operation, error]) => <p className="runtime-error" role="alert" key={operation}>{error}</p>)}
    <SettingsSection title="Installed models" description="Use your own GGUF files or download a model below. Activation is always a separate choice."
      action={<><Button tone="quiet" onClick={() => void openUserModelFolder()}>Open folder</Button><Button tone="quiet" disabled={!state.connected || !!state.pending.get} onClick={refreshLocalModels}>{state.pending.get ? "Refreshing…" : "Refresh"}</Button></>}>
      {snapshot ? <p className="platform-note">{Number(hardware.ram_total_mb) > 0 ? `${Math.round(Number(hardware.ram_total_mb) / 1024)} GB RAM` : "RAM unavailable"}{gpus.map(gpu => ` · ${String(gpu.name || "GPU")}`).join("")}</p> : null}
      {snapshot?.installed.map(model => <SettingRow key={model.id} title={model.name} description={`${modelBytes(Number(model.size_bytes))}${model.vision ? " · Vision" : ""}${model.active ? " · Active" : ""}`}
        control={<>
          {model.active ? <Button disabled={!supports("eject") || busy} onClick={() => requestLocalModels("eject")}>{state.pending.eject ? "Ejecting…" : "Eject"}</Button>
            : <Button disabled={!supports("activate") || busy} onClick={() => requestLocalModels("activate", {model_id: model.id})}>{state.pending.activate ? "Activating…" : "Activate"}</Button>}
          {model.managed_download ? <Button tone="quiet" disabled={!supports("delete") || busy || model.active} onClick={() => {
            if (doc.defaultView?.confirm(`Delete the downloaded files for ${model.name}? All files downloaded with this model, including split parts and projectors, will be removed.`)) requestLocalModels("delete", {model_id: model.id});
          }}>Delete</Button> : null}
        </>}/>)}
      {snapshot && !snapshot.installed.length ? <p className="platform-note">No local models installed. Add a GGUF file to the model folder or download one below.</p> : null}
    </SettingsSection>
    {jobs.length ? <SettingsSection title="Downloads">{[...jobs].reverse().map(job => <SettingRow key={job.id} title={job.repo} description={job.error || `${job.phase} · ${(job.done_bytes === 0 ? "0 MB" : modelBytes(job.done_bytes))} / ${modelBytes(job.total_bytes)}`}
      control={!modelJobFinished(job.status) ? <Button disabled={!supports("cancel") || busy} onClick={() => requestLocalModels("cancel", {job_id: job.id})}>Cancel download</Button> : <span>{job.status === "done" ? "Downloaded" : job.status}</span>}>
      {job.total_bytes > 0 ? <progress aria-label={`Download progress for ${job.repo}`} value={Math.max(0, Math.min(job.done_bytes, job.total_bytes))} max={job.total_bytes}/> : null}
    </SettingRow>)}</SettingsSection> : null}
    <SettingsSection title="Find a model" description="Browse GGUF models on Hugging Face. Choose the size and optional vision projector before downloading.">
      <form className="local-model-search" onSubmit={event => {event.preventDefault(); requestLocalModels("search", {query: query.trim(), limit: 20});}}>
        <input aria-label="Search Hugging Face models" value={query} onChange={event => setQuery(event.target.value)} placeholder="Model name or publisher"/>
        <Button type="submit" disabled={!supports("search") || !!state.pending.search}>{state.pending.search ? "Searching…" : "Search"}</Button>
      </form>
      {state.results.map(repo => <SettingRow key={repo.repo} title={repo.repo} description={`${Number(repo.downloads).toLocaleString()} downloads${repo.gated ? " · Access approval required" : ""}`}
        control={<Button disabled={!supports("files") || !!state.pending.files} onClick={() => requestLocalModels("files", {repo: repo.repo})}>Choose files</Button>}/>) }
      {state.pending.files ? <p role="status">Loading available files…</p> : null}
      {files ? <fieldset className="local-model-variants"><legend>{files.repo}</legend>
        {variants.map(variant => <label key={variant.label}><input type={variant.projector ? "checkbox" : "radio"} name={variant.projector ? undefined : primaryGroup} checked={chosen.includes(variant.label)} disabled={!variant.complete || busy}
          onChange={event => setChosen(values => event.target.checked ? [...values.filter(value => variants.find(item => item.label === value)?.projector !== variant.projector), variant.label] : values.filter(value => value !== variant.label))}/>
          <span>{variant.label}<small>{modelBytes(variant.bytes)}{variant.paths.length > 1 ? ` · ${variant.paths.length} parts included` : ""}{variant.projector ? " · Vision projector" : ""}{!variant.complete ? " · Missing parts" : ""}</small></span>
        </label>)}
        {!variants.length ? <p>No downloadable GGUF files found.</p> : null}
        <Button tone="primary" disabled={!supports("download") || busy || !canDownload} onClick={() => requestLocalModels("download", {repo: files.repo, revision: files.revision, paths: selectedPaths})}>
          {state.pending.download ? "Starting download…" : selected.length ? `Download ${modelBytes(total)}` : "Choose a model to download"}
        </Button>
      </fieldset> : null}
    </SettingsSection>
  </div>;
}
