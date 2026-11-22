import {useMemo, useState} from "react";
import type {LayoutNode} from "./layoutModel";
import {applyWorkbenchPreset, deleteWorkbenchPreset, getWorkbenchPreset, listWorkbenchPresets,
  resetWorkbenchLayout, saveWorkbenchPreset, setWorkbenchEditMode} from "./workbenchStore";

const labels: Record<string, string> = {history:"History", workspace:"Chat", files:"Files", review:"Review", terminal:"Terminal"};

function Diagram({node, hidden, detached}: {node: LayoutNode; hidden?: Record<string, boolean>; detached: ReadonlySet<string>}) {
  if (node.type === "group") {
    const floating = detached.has("pane:" + node.id);
    const panes = node.panes.map(id => id.startsWith("owned:") ? id.split(":")[1] : id).filter(id => labels[id]);
    return <div className={`layout-preview__group${floating ? " is-detached" : ""}`} title={floating ? "Detached window" : undefined}>
      {panes.map(id => <span key={id} className={hidden?.[id] ? "is-hidden" : undefined}>{labels[id]}</span>)}
      {floating ? <small>Detached</small> : null}
    </div>;
  }
  return <div className="layout-preview__split" style={{flexDirection:node.orientation}}>
    {node.children.map((child, i) => <div key={child.id} className="layout-preview__child" style={{flexGrow:node.weights[i] || 1}}>
      <Diagram node={child} hidden={hidden} detached={detached}/>
    </div>)}
  </div>;
}

export function LayoutPresetEditor({presetId,setPresetId}: {presetId:string;setPresetId:(id:string)=>void}) {
  const [presetRevision, setPresetRevision] = useState(0);
  const [presetName, setPresetName] = useState<string | null>(null);
  const [savingPreset, setSavingPreset] = useState(false);
  const [presetError, setPresetError] = useState("");
  const presets = useMemo(listWorkbenchPresets, [presetRevision]);
  const preview = useMemo(() => getWorkbenchPreset(presetId), [presetId, presetRevision]);
  const selected = presets.find(preset => preset.id === presetId);
  return <section className="workbench-editbar" aria-label="Layout editing">
    {preview ? <figure className="layout-preview" aria-label={`${selected?.name || "Workspace"} layout preview`}>
      <Diagram node={preview.layout} hidden={preview.hidden} detached={new Set(preview.windows.map(item=>item.id))}/>
    </figure> : null}
    <div className="workbench-editbar__actions">
      <label>Layout preset<select aria-label="Layout preset" value={presetId} onChange={event=>setPresetId(event.target.value)}>
        {presets.map(preset => <option value={preset.id} key={preset.id}>{preset.name}</option>)}
      </select></label>
      <button disabled={!preview} onClick={()=>applyWorkbenchPreset(presetId)}>Apply layout</button>
      <button onClick={()=>{resetWorkbenchLayout(); setPresetId("default");}}>Reset</button>
      {presetName === null ? <button title="Save the current workspace layout" onClick={() => { setPresetName(""); setPresetError(""); }}>Save as</button> : <form className="workbench-preset-form" onSubmit={async event => {
        event.preventDefault();
        if (!presetName.trim() || savingPreset) return;
        setSavingPreset(true);
        const id = await saveWorkbenchPreset(presetName);
        setSavingPreset(false);
        if (id) { setPresetId(id); setPresetRevision(value => value + 1); setPresetName(null); }
        else setPresetError("Could not save this layout. Try again.");
      }}>
        <input autoFocus aria-label="Layout name" disabled={savingPreset} value={presetName} maxLength={80} onChange={event => setPresetName(event.target.value)}
          onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setPresetName(null); } }}/>
        <button type="submit" disabled={!presetName.trim() || savingPreset}>{savingPreset ? "Saving…" : "Save"}</button><button type="button" disabled={savingPreset} onClick={() => setPresetName(null)}>Cancel</button>
        {presetError ? <span role="alert">{presetError}</span> : null}
      </form>}
      {selected?.user ? <button onClick={() => {
        deleteWorkbenchPreset(presetId);
        setPresetId("default");
        setPresetRevision(value => value + 1);
      }}>Delete</button> : null}
      <button onClick={() => setWorkbenchEditMode(false)}>Done</button>
    </div>
  </section>;
}
