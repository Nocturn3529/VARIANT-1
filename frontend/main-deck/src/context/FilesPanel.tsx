import {withCachedChatState} from "../chat/stateCore";
import {createRefreshQueue} from "../workbench/refreshQueue";
import {Icon} from "../ui/Icon";
import {watchPath} from "../workbench/watchPath";
import {focusMainComposer, useSurfaceDocument} from "../ui/SurfaceDocument";
import {useEffect, useMemo, useRef, useState, type KeyboardEvent} from "react";
import {PopupMenu} from "../ui/PopupMenu";
import ignore, {type Ignore} from "ignore";
import {addChatPathAttachment} from "../chatStore";
import type {WorkbenchGitFile} from "../types";
import {openFilePreview} from "../workbench/previewStore";

type FileEntry = {
  name: string;
  path: string;
  directory: boolean;
  symlink?: boolean;
  size?: number;
  mtimeMs?: number;
};

type MenuState = {entry: FileEntry; x: number; y: number} | null;

function relativePath(root: string, fullPath: string): string {
  const prefix = root.endsWith("\\") || root.endsWith("/") ? root : `${root}\\`;
  return fullPath.toLowerCase().startsWith(prefix.toLowerCase())
    ? fullPath.slice(prefix.length).replace(/\\/g, "/")
    : fullPath.replace(/\\/g, "/");
}

function statusLabel(value: string): string {
  if (value === "??") return "U";
  if (value.includes("A")) return "A";
  if (value.includes("D")) return "D";
  if (value.includes("R")) return "R";
  if (value.includes("U")) return "!";
  return "M";
}

export function FilesPanel({directory = "",chatId = "",onChooseProject}: {directory?: string;chatId?:string;onChooseProject?:()=>void}) {
  const ownerDocument = useSurfaceDocument();
  const ownerWindow = ownerDocument.defaultView || window;
  const api = window.variant1Deck;
  const [root, setRoot] = useState("");
  const [children, setChildren] = useState<Record<string, FileEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [menu, setMenu] = useState<MenuState>(null);
  const [renaming, setRenaming] = useState("");
  const [renameValue, setRenameValue] = useState("");
  const [gitFiles, setGitFiles] = useState<WorkbenchGitFile[]>([]);
  const generation = useRef<Record<string, number>>({});
  const ignoreRules = useRef<Map<string, Ignore>>(new Map());
  const treeRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const renameStatus = useRef({pending: false, cancelled: false});
  const visibleEntries = useMemo(() => {
    const result: Array<{entry: FileEntry; parent: string}> = [];
    const visit = (path: string) => {
      for (const entry of children[path] || []) {
        result.push({entry, parent: path});
        if (entry.directory && expanded.has(entry.path)) visit(entry.path);
      }
    };
    if (root) visit(root);
    return result;
  }, [root, children, expanded]);
  const activePath = visibleEntries.some(item => item.entry.path === selected) ? selected : visibleEntries[0]?.entry.path || "";

  function focusRow(path: string) {
    setSelected(path);
    const row = rowRefs.current.get(path);
    row?.focus();
    row?.scrollIntoView({block: "nearest"});
  }

  function beginRename(entry: FileEntry) {
    renameStatus.current = {pending: false, cancelled: false};
    setRenaming(entry.path); setRenameValue(entry.name);
  }

  function navigateTree(event: KeyboardEvent<HTMLDivElement>, entry: FileEntry) {
    const index = visibleEntries.findIndex(item => item.entry.path === entry.path);
    let target = "";
    if (event.key === "ArrowDown") target = visibleEntries[Math.min(index + 1, visibleEntries.length - 1)]?.entry.path;
    else if (event.key === "ArrowUp") target = visibleEntries[Math.max(index - 1, 0)]?.entry.path;
    else if (event.key === "Home") target = visibleEntries[0]?.entry.path;
    else if (event.key === "End") target = visibleEntries[visibleEntries.length - 1]?.entry.path;
    else if (event.key === "ArrowRight") {
      if (entry.directory && !expanded.has(entry.path)) void toggle(entry);
      else if (entry.directory) target = children[entry.path]?.[0]?.path || "";
    } else if (event.key === "ArrowLeft") {
      if (entry.directory && expanded.has(entry.path)) void toggle(entry);
      else target = visibleEntries[index]?.parent || "";
    } else if (event.key === "Enter") void toggle(entry);
    else if (event.key === " ") setSelected(entry.path);
    else if (event.key === "F2") beginRename(entry);
    else if (event.key === "Delete") void trash(entry);
    else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      const rect = event.currentTarget.getBoundingClientRect();
      setMenu({entry, x: rect.left + 20, y: rect.bottom});
    } else return;
    event.preventDefault(); event.stopPropagation();
    if (target && rowRefs.current.has(target)) focusRow(target);
  }

  const gitByPath = useMemo(() => new Map(gitFiles.flatMap(file => [
    [file.path.replace(/\\/g, "/"), file],
    ...(file.originalPath ? [[file.originalPath.replace(/\\/g, "/"), file] as const] : []),
  ])), [gitFiles]);

  async function loadDirectory(path: string, replace = false): Promise<void> {
    const token = (generation.current[path] || 0) + 1;
    generation.current[path] = token;
    setLoading(current => new Set(current).add(path));
    const result = await api?.readWorkbenchDirectory?.(path);
    setLoading(current => { const next = new Set(current); next.delete(path); return next; });
    if (!result?.ok) {
      setError(result?.error || "Unable to read folder");
      return;
    }
    if (token !== generation.current[path]) return;
    const rawRows = Array.isArray(result.entries) ? result.entries as FileEntry[] : [];
    const gitignore = rawRows.find(entry => !entry.directory && entry.name === ".gitignore");
    if (gitignore) {
      const content = await api?.readWorkbenchFile?.(gitignore.path);
      const matcher = ignore();
      if (content?.ok && content.text) matcher.add(content.text);
      ignoreRules.current.set(path, matcher);
    } else {
      ignoreRules.current.delete(path);
    }
    const rows = rawRows.filter(entry => {
      if (entry.name === ".gitignore") return true;
      let cursor = path;
      while (cursor && cursor.toLowerCase().startsWith(root.toLowerCase())) {
        const matcher = ignoreRules.current.get(cursor);
        if (matcher) {
          const prefix = cursor.endsWith("\\") || cursor.endsWith("/") ? cursor : `${cursor}\\`;
          const relative = entry.path.toLowerCase().startsWith(prefix.toLowerCase())
            ? entry.path.slice(prefix.length).replace(/\\/g, "/")
            : entry.name;
          try { if (matcher.ignores(relative + (entry.directory ? "/" : ""))) return false; }
          catch { /* malformed ignore patterns do not hide the file tree */ }
        }
        if (cursor.toLowerCase() === root.toLowerCase()) break;
        cursor = parentPath(cursor);
      }
      return true;
    });
    setChildren(current => replace ? {[path]: rows} : {...current, [path]: rows});
    setError("");
  }

  async function refreshGit(path = root): Promise<void> {
    if (!path) return;
    const status = await api?.getWorkbenchGitStatus?.(path);
    setGitFiles(status?.ok && Array.isArray(status.files) ? status.files : []);
  }

  async function refreshAll(): Promise<void> {
    if (!root) return;
    const open = [...new Set([root, ...expanded])];
    await Promise.all(open.map(path => loadDirectory(path)));
    await refreshGit(root);
  }

  useEffect(() => {
    setRoot(directory); setChildren({}); setExpanded(new Set(directory ? [directory] : []));
    setSelected(""); setError(""); generation.current = {}; ignoreRules.current.clear();
    if(directory) { void loadDirectory(directory, true); void refreshGit(directory); }
  }, [directory, api]);

  const refreshCurrent = useRef(refreshAll); refreshCurrent.current = refreshAll;
  useEffect(() => {
    if (!root) return;
    const visible = () => ownerDocument.visibilityState === "visible" && !!treeRef.current?.getClientRects().length;
    const queue = createRefreshQueue(async () => { if (visible()) await refreshCurrent.current(); }, error => setError(String(error)));
    const focused = () => queue.request();
    ownerWindow.addEventListener("focus", focused);
    const interval = window.setInterval(focused, 30000);
    const stop = watchPath(api, root, focused, {scope: "workspace", delay: 50, onError: setError});
    return () => { queue.dispose(); stop(); ownerWindow.removeEventListener("focus", focused); window.clearInterval(interval); };
  }, [root, api, ownerDocument]);

  async function toggle(entry: FileEntry): Promise<void> {
    setSelected(entry.path);
    if (!entry.directory) {
      openFilePreview(entry.path, entry.name,chatId);
      return;
    }
    if (expanded.has(entry.path)) {
      setExpanded(current => { const next = new Set(current); next.delete(entry.path); return next; });
      return;
    }
    setExpanded(current => new Set(current).add(entry.path));
    if (!children[entry.path]) await loadDirectory(entry.path);
  }

  async function rename(entry: FileEntry): Promise<void> {
    if (renameStatus.current.pending || renameStatus.current.cancelled) return;
    renameStatus.current.pending = true;
    try {
      const result = await api?.renameWorkbenchPath?.(entry.path, renameValue);
      if (!result?.ok) throw new Error(result?.error || "Rename failed");
      setRenaming("");
      setSelected(result.path || "");
      await loadDirectory(parentPath(entry.path));
      await refreshGit();
      requestAnimationFrame(() => focusRow(result.path || entry.path));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { renameStatus.current.pending = false; }
  }

  async function trash(entry: FileEntry): Promise<void> {
    if (!ownerWindow.confirm(`Move ${entry.name} to the Recycle Bin?`)) return;
    const result = await api?.trashWorkbenchPath?.(entry.path);
    if (!result?.ok) {
      setError(result?.error || "Delete failed");
      return;
    }
    setMenu(null);
    setSelected("");
    await loadDirectory(parentPath(entry.path));
    await refreshGit();
  }

  function parentPath(value: string): string {
    const normalized = value.replace(/[\\/]+$/, "");
    const index = Math.max(normalized.lastIndexOf("\\"), normalized.lastIndexOf("/"));
    return index > 2 ? normalized.slice(0, index) : root;
  }

  function rows(path: string, depth: number): React.ReactNode {
    return (children[path] || []).map(entry => {
      const open = expanded.has(entry.path);
      const relative = relativePath(root, entry.path);
      const git = gitByPath.get(relative);
      return <div className="workbench-files__branch" key={entry.path}>
        <div
          className={`workbench-file-row${selected === entry.path ? " is-selected" : ""}`}
          style={{"--file-depth": depth} as React.CSSProperties}
          role="treeitem"
          ref={node => { if (node) rowRefs.current.set(entry.path, node); else rowRefs.current.delete(entry.path); }}
          aria-level={depth + 1}
          aria-expanded={entry.directory ? open : undefined}
          aria-selected={activePath === entry.path}
          tabIndex={activePath === entry.path ? 0 : -1}
          onFocus={event => { if (event.target === event.currentTarget) setSelected(entry.path); }}
          draggable
          onDragStart={event => {
            event.dataTransfer.effectAllowed = "copy";
            event.dataTransfer.setData("text/plain", entry.path);
            event.dataTransfer.setData("application/x-variant1-path", JSON.stringify({path: entry.path, directory: entry.directory}));
          }}
          onClick={event => {
            if (event.shiftKey) {
              event.preventDefault();
              withCachedChatState(chatId,()=>addChatPathAttachment(entry.path, entry.directory));
              focusMainComposer();
              return;
            }
            focusRow(entry.path);
            if (entry.directory) void toggle(entry);
          }}
          onDoubleClick={() => { if (!entry.directory) openFilePreview(entry.path, entry.name,chatId); }}
          onContextMenu={event => { event.preventDefault(); focusRow(entry.path); setMenu({entry, x: event.clientX, y: event.clientY}); }}
          onKeyDown={event => navigateTree(event, entry)}
        >
          <button type="button" className="workbench-file-row__twisty" tabIndex={-1} aria-hidden={!entry.directory} onClick={event => { event.stopPropagation(); if (entry.directory) void toggle(entry); }}>{entry.directory ? <Icon name={open ? "down" : "chevron"}/> : null}</button>
          <span className="workbench-file-row__icon" aria-hidden="true"><Icon name={entry.directory ? "folder" : "file"}/></span>
          {renaming === entry.path ? <input
            autoFocus
            aria-label={`Rename ${entry.name}`}
            value={renameValue}
            onChange={event => setRenameValue(event.target.value)}
            onClick={event => event.stopPropagation()}
            onFocus={event => {
              const dot = entry.directory ? -1 : renameValue.lastIndexOf(".");
              event.currentTarget.setSelectionRange(0, dot > 0 ? dot : renameValue.length);
            }}
            onBlur={() => { if (renameValue && renameValue !== entry.name) void rename(entry); else setRenaming(""); }}
            onKeyDown={event => {
              event.stopPropagation();
              if (event.key === "Enter") { event.preventDefault(); void rename(entry); }
              if (event.key === "Escape") { event.preventDefault(); renameStatus.current.cancelled = true; setRenaming(""); requestAnimationFrame(() => focusRow(entry.path)); }
            }}
          /> : <span className="workbench-file-row__name">{entry.name}</span>}
          {git ? <span className={`workbench-file-row__git status-${statusLabel(git.status).toLowerCase()}`}>{statusLabel(git.status)}</span> : null}
        </div>
        {entry.directory && open ? <div role="group">
          {loading.has(entry.path) && !children[entry.path] ? <div className="workbench-file-row is-loading" style={{"--file-depth": depth + 1} as React.CSSProperties}>Loading…</div> : null}
          {rows(entry.path, depth + 1)}
        </div> : null}
      </div>;
    });
  }

  return <section className="workbench-files">
    <header className="workbench-tool-header">
      <strong>Files</strong>
      {onChooseProject ? <button title="Change project folder" aria-label="Change project folder" onClick={onChooseProject}><Icon name="folder"/></button>:null}
      <span title={root}>{root ? root.split(/[\\/]/).pop() : "No project"}</span>
      <button title="Refresh" aria-label="Refresh files" onClick={() => void refreshAll()}><Icon name="refresh"/></button>
      <button title="Collapse All" aria-label="Collapse all folders" onClick={() => setExpanded(new Set(root ? [root] : []))}><Icon name="collapse"/></button>
    </header>
    {error ? <div className="workbench-tool-error">{error}</div> : null}
    <div ref={treeRef} className="workbench-files__tree" role="tree" aria-label="Project files">
      {root ? rows(root, 0) : <div className="workbench-tool-empty">No project folder.</div>}
    </div>
    <footer>Double-click to preview · Shift-click to attach · F2 to rename</footer>
    {menu ? <PopupMenu className="workbench-file-menu" x={menu.x} y={menu.y} onClose={() => setMenu(null)}>
      {!menu.entry.directory ? <button role="menuitem" onClick={() => { openFilePreview(menu.entry.path, menu.entry.name,chatId); setMenu(null); }}>Open preview</button> : null}
      <button role="menuitem" onClick={() => { withCachedChatState(chatId,()=>addChatPathAttachment(menu.entry.path, menu.entry.directory)); setMenu(null); }}>Attach to chat</button>
      <button role="menuitem" onClick={() => { void ownerWindow.navigator.clipboard.writeText(menu.entry.path); setMenu(null); }}>Copy absolute path</button>
      <button role="menuitem" onClick={() => { void ownerWindow.navigator.clipboard.writeText(relativePath(root, menu.entry.path)); setMenu(null); }}>Copy relative path</button>
      <button role="menuitem" onClick={() => { void api?.revealWorkbenchPath?.(menu.entry.path); setMenu(null); }}>Reveal in Explorer</button>
      <hr/>
      <button role="menuitem" onClick={() => { beginRename(menu.entry); setMenu(null); }}>Rename</button>
      <button role="menuitem" className="danger" onClick={() => void trash(menu.entry)}>Move to Recycle Bin</button>
    </PopupMenu> : null}
  </section>;
}
