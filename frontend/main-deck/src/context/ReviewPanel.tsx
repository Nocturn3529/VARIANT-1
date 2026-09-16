import {VirtualList} from "../ui/VirtualList";
import {createRefreshQueue} from "../workbench/refreshQueue";
import {Icon} from "../ui/Icon";
import {watchPath} from "../workbench/watchPath";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {useEffect, useMemo, useRef, useState} from "react";
import type {WorkbenchGitFile, WorkbenchGitStatus} from "../types";
import {openFilePreview} from "../workbench/previewStore";

function statusName(value: string): string {
  if (value === "??") return "Untracked";
  if (value.includes("U")) return "Conflict";
  if (value.includes("A")) return "Added";
  if (value.includes("D")) return "Deleted";
  if (value.includes("R")) return "Renamed";
  return "Modified";
}

function statusGlyph(value: string): string {
  if (value === "??") return "U";
  if (value.includes("U")) return "!";
  if (value.includes("A")) return "A";
  if (value.includes("D")) return "D";
  if (value.includes("R")) return "R";
  return "M";
}

/** Join project-root + repo-relative file using the host path separator. */
function hostSep(root: string): "\\" | "/" {
  if (/^[A-Za-z]:/.test(root) || root.includes("\\")) return "\\";
  return "/";
}

function absolute(root: string, file: string): string {
  const sep = hostSep(root);
  const base = root.replace(/[\\\/]$/, "");
  const rel = String(file || "").replace(/[\\\/]+/g, sep);
  return `${base}${sep}${rel}`;
}
const unstaged = (file: WorkbenchGitFile) => file.status === "??" || !!file.status[1]?.trim();
const missing = (file: WorkbenchGitFile) => file.status[1] === "D" || file.status.trim() === "D";
type DiffSide = "staged" | "unstaged" | "untracked";

export function ReviewPanel({directory = "",chatId = ""}: {directory?: string;chatId?:string}) {
  const ownerDocument = useSurfaceDocument();
  const ownerWindow = ownerDocument.defaultView || window;
  const api = window.variant1Deck;
  const panel = useRef<HTMLElement>(null);
  const selectionEpoch = useRef(0);
  const selectedRef = useRef("");
  const sideRef = useRef<DiffSide>("unstaged");
  const [side, setSide] = useState<DiffSide>("unstaged");
  const shippingRef = useRef(false);
  const [root, setRoot] = useState("");
  const [status, setStatus] = useState<WorkbenchGitStatus>({files: []});
  const [selected, setSelected] = useState("");
  const [diff, setDiff] = useState("");
  const [error, setError] = useState("");
  const [treeMode, setTreeMode] = useState(false);
  const [message, setMessage] = useState("");
  const [shipping, setShipping] = useState(false);
  const files = status.files || [];

  async function refresh(path = root): Promise<void> {
    if (!path) return;
    const next = await api?.getWorkbenchGitStatus?.(path);
    if (!next?.ok) {
      setStatus(next || {files: []});
      setError(next?.error || "This folder is not a Git repository.");
      return;
    }
    setRoot(next.root || path);
    setStatus(next);
    setError("");
    const nextFiles = next.files || [];
    const candidate = nextFiles.some(file => file.path === selectedRef.current) ? selectedRef.current : nextFiles[0]?.path || "";
    if (candidate) await selectFile(candidate, next.root || path, next.files || []);
    else { setSelected(""); setDiff(""); }
  }

  async function selectFile(file: string, path = root, rows = files, requested?: DiffSide): Promise<void> {
    const epoch = ++selectionEpoch.current;
    const row = rows.find(item => item.path === file);
    const preferred = requested || (selectedRef.current === file ? sideRef.current : undefined);
    const selectedSide: DiffSide = row?.status === "??" ? "untracked" : preferred === "staged" && row?.staged ? "staged" : row && unstaged(row) ? "unstaged" : "staged";
    sideRef.current = selectedSide; setSide(selectedSide);
    selectedRef.current = file; setSelected(file);
    if (row?.status === "??") {
      const content = await api?.readWorkbenchFile?.(absolute(path, file));
      if (epoch !== selectionEpoch.current) return;
      setDiff(content?.ok ? content.binary ? "Binary file. Open the file preview to inspect it." : String(content.text || "") : String(content?.error || "Unable to read untracked file"));
      return;
    }
    const result = await api?.getWorkbenchGitDiff?.(path, file, selectedSide === "staged");
    if (epoch !== selectionEpoch.current) return;
    setDiff(result?.ok ? String(result.diff || "") : String(result?.error || "Unable to read diff"));
  }

  useEffect(() => {
    setRoot(directory); setStatus({files:[]}); setDiff(""); setSelected("");
    setError(directory ? "" : "Select a project for this chat to review changes.");
    if(directory)void refresh(directory);
  }, [directory, api]);

  const refreshCurrent = useRef(refresh); refreshCurrent.current = refresh;
  useEffect(() => {
    if (!root) return;
    const visible = () => ownerDocument.visibilityState === "visible" && !!panel.current?.getClientRects().length;
    const queue = createRefreshQueue(async () => { if (visible()) await refreshCurrent.current(); }, error => setError(String(error)));
    const focused = () => queue.request();
    ownerWindow.addEventListener("focus", focused);
    const interval = window.setInterval(focused, 30000);
    const stop = watchPath(api, root, focused, {scope: "workspace", delay: 50, onError: setError});
    return () => {queue.dispose();stop();selectionEpoch.current++;ownerWindow.removeEventListener("focus",focused);window.clearInterval(interval);};
  }, [root, api, ownerDocument]);

  async function mutate(action: string, file?: string): Promise<void> {
    const result = await api?.runWorkbenchGit?.(action, root, file ? {file} : {});
    if (!result?.ok) setError(String(result?.error || `${action} failed`));
    else await refresh();
  }

  async function ship(action: "commit" | "commit_push"): Promise<void> {
    if (!message.trim() || !files.some(file => file.staged) || shippingRef.current) return;
    shippingRef.current = true;
    setShipping(true);
    try {
      const result = await api?.runWorkbenchGit?.(action, root, {message: message.trim()});
      if (result?.ok || result?.committed) setMessage("");
      await refresh();
      if (!result?.ok) setError(`${result?.committed ? "Commit saved, but push failed. " : ""}${String(result?.error || "Commit failed")}`);
    } catch (error) {await refresh();setError(String(error));}
    finally {shippingRef.current = false;setShipping(false);}
  }

  async function discardUnstaged() {
    let failure = "";
    for (const file of files.filter(unstaged)) {
      const result = await api?.runWorkbenchGit?.("revert", root, {file:file.path});
      if (!result?.ok) {failure = String(result?.error || "Could not discard unstaged changes");break;}
    }
    await refresh(); if (failure) setError(failure);
  }

  const rows = useMemo(() => {
    const items: Array<{kind:"file"; file:WorkbenchGitFile} | {kind:"folder"; folder:string}> = [];
    if (!treeMode) return files.map(file => ({kind:"file" as const,file}));
    const groups = new Map<string,WorkbenchGitFile[]>();
    for (const file of files) {
      const index = file.path.lastIndexOf("/");
      const folder = index > 0 ? file.path.slice(0,index) : ".";
      const group = groups.get(folder) || []; group.push(file); groups.set(folder,group);
    }
    for (const [folder,group] of groups) {items.push({kind:"folder",folder});items.push(...group.map(file=>({kind:"file" as const,file})));}
    return items;
  }, [files,treeMode]);

  return <section ref={panel} className="workbench-review">
    <header className="workbench-tool-header">
      <strong>Review</strong>
      <span>{status.branch || (root ? "Working tree" : "No repository")}{status.ahead ? ` · ↑${status.ahead}` : ""}{status.behind ? ` · ↓${status.behind}` : ""}</span>
      <button title={treeMode ? "List view" : "Tree view"} onClick={() => setTreeMode(value => !value)}>{treeMode ? "☷" : "⑂"}</button>
      <button title="Stage all" disabled={!files.length} onClick={() => void mutate("stage")}>+</button>
      <button title="Discard all unstaged changes" disabled={!files.some(unstaged)} onClick={() => {
        if (ownerWindow.confirm("Discard every unstaged file change? Staged changes will be kept.")) void discardUnstaged();
      }}>↶</button>
      <button title="Refresh" onClick={() => void refresh()}><Icon name="refresh"/></button>
    </header>
    {error ? <div className="workbench-tool-error">{error}</div> : null}
    <div className="workbench-review__body">
      <aside className="workbench-review__files">
        <header><strong>Changes</strong><span>{files.length}</span></header>
        <VirtualList items={rows} rowHeight={31} label="Changed files"
          itemKey={row => row.kind === "folder" ? `folder:${row.folder}` : `${row.file.status}:${row.file.path}`}
          render={row => {
            if (row.kind === "folder") return <section><h4>{row.folder}</h4></section>;
            const file = row.file;
            return <article className={selected === file.path ? "is-selected" : ""}>
            <button className="workbench-review__select" onClick={() => void selectFile(file.path)} onDoubleClick={() => {if (!missing(file)) openFilePreview(absolute(status.root || root, file.path),undefined,chatId);}}>
              <i className={`status-${statusGlyph(file.status).toLowerCase()}`}>{statusGlyph(file.status)}</i>
              <span>{treeMode ? file.path.split("/").pop() : file.path}</span>
              <small>{statusName(file.status)}{file.added || file.removed ? ` · +${file.added || 0} −${file.removed || 0}${file.staged && unstaged(file) ? " total" : ""}` : ""}</small>
            </button>
            <div>
              <button title={missing(file) ? "Deleted file — inspect its diff" : "Open file"} disabled={missing(file)} onClick={() => openFilePreview(absolute(status.root || root, file.path),undefined,chatId)}><Icon name="popout"/></button>
              <button title={file.staged ? "Unstage" : "Stage"} onClick={() => void mutate(file.staged ? "unstage" : "stage", file.path)}>{file.staged ? "−" : "+"}</button>
              <button title="Discard unstaged changes" disabled={!unstaged(file)} onClick={() => { if (ownerWindow.confirm(`Discard unstaged changes in ${file.path}? Staged changes will be kept.`)) void mutate("revert", file.path); }}>↶</button>
            </div>
          </article>;
        }}/>
        {!files.length && !error ? <div className="workbench-tool-empty">No changes.</div> : null}
      </aside>
      <main className="workbench-review__diff">
        {selected ? <header><strong>{selected}</strong>{side === "untracked" ? <span>Untracked file · full contents</span> : <>
          <button type="button" aria-pressed={side === "unstaged"} disabled={!files.some(file=>file.path===selected && unstaged(file))} onClick={()=>void selectFile(selected,root,files,"unstaged")}>Unstaged</button>
          <button type="button" aria-pressed={side === "staged"} disabled={!files.some(file=>file.path===selected && file.staged)} onClick={()=>void selectFile(selected,root,files,"staged")}>Staged</button>
        </>}</header> : null}
        <pre>{diff || (selected ? "No textual diff." : "Select a changed file.")}</pre>
      </main>
    </div>
    <footer className="workbench-review__ship">
      <textarea rows={2} placeholder="Commit message" value={message} onChange={event => setMessage(event.target.value)}/>
      <button disabled={!files.length || shipping || !!message.trim()} title={message.trim() ? "Clear your draft to use a suggested title" : "Suggest a title from changed filenames"} onClick={() => {
        const names = files.slice(0, 2).map(file => file.path.split("/").pop()).join(", ");
        setMessage(files.length === 1 ? `Update ${names}` : `Update ${files.length} files`);
      }}>Suggest title</button>
      <button disabled={!message.trim() || shipping || !files.some(file=>file.staged)} onClick={() => void ship("commit")}>Commit</button>
      <button disabled={!message.trim() || shipping || !files.some(file=>file.staged)} onClick={() => void ship("commit_push")}>Commit & Push</button>
      <button disabled={shipping} onClick={() => void mutate("create_pr")}>Create PR</button>
    </footer>
  </section>;
}
