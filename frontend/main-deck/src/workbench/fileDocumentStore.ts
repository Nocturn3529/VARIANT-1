import {createExternalStore} from "../state/createModuleStore";
import type {RuntimeApi} from "../types";

export type FileDocument = Readonly<{
  text: string;
  original: string;
  dataUrl: string;
  kind: "unknown" | "text" | "binary";
  editable: boolean;
  mtimeMs: number;
  loaded: boolean;
  loading: boolean;
  editing: boolean;
  saving: boolean;
  conflict: boolean;
  error: string;
}>;

function createDocument() {
  return {
    epoch: 0,
    store: createExternalStore<FileDocument>({
      text: "", original: "", dataUrl: "", kind: "unknown", editable: false, mtimeMs: 0,
      loaded: false, loading: false, editing: false, saving: false,
      conflict: false, error: "",
    }),
  };
}

// Buffers belong to open tabs, not to a particular mounted pane or destination.
// They deliberately stay in memory; localStorage only retains tab metadata.
const documents = new Map<string, ReturnType<typeof createDocument>>();
function documentFor(id: string) {
  let value = documents.get(id);
  if (!value) {
    value = createDocument();
    documents.set(id, value);
  }
  return value;
}

export function useFileDocument(id: string): FileDocument {
  return documentFor(id).store.useStore();
}

export function getFileDocument(id: string): FileDocument | undefined {
  return documents.get(id)?.store.getState();
}

export function isFileDocumentDirty(id: string): boolean {
  const state = getFileDocument(id);
  return !!state && state.text !== state.original;
}

export function forgetFileDocument(id: string): void {
  documents.delete(id);
}

export async function loadFileDocument(
  id: string, path: string, read: RuntimeApi["readWorkbenchFile"], discard = false,
): Promise<void> {
  const owner = documentFor(id);
  if (owner.store.getState().saving) return;
  const epoch = ++owner.epoch;
  owner.store.setState({loading: true, error: "", ...(discard ? {editing: false} : {})});
  try {
    const result = await read?.(path);
    if (documents.get(id) !== owner || epoch !== owner.epoch) return;
    if (!result?.ok) throw new Error(result?.error || "Unable to read file");
    const state = owner.store.getState();
    const text = String(result.text || "");
    const mtimeMs = Number(result.mtimeMs || 0);
    const editable = !result.binary && result.editable !== false && typeof result.text === "string";
    const kind = editable ? "text" : "binary";
    // A read that started before Edit, or while the pane was hidden, must not
    // replace the user's buffer. The original mtime remains the save fence.
    if (state.editing && !discard) {
      owner.store.setState({loading: false, kind, editable,
        conflict: !editable || mtimeMs !== state.mtimeMs || text !== state.original,
        error: editable ? "" : "The file is no longer editable text. Your draft has been kept."});
    } else {
      owner.store.setState({text, original: text, mtimeMs, kind, editable, dataUrl: String(result.dataUrl || ""),
        loaded: true, loading: false, editing: false, conflict: false});
    }
  } catch (error) {
    if (documents.get(id) === owner && epoch === owner.epoch) {
      owner.store.setState({loading: false, error: error instanceof Error ? error.message : String(error)});
    }
  }
}

export function beginFileEdit(id: string): void {
  const owner = documentFor(id);
  const state = owner.store.getState();
  if (state.loaded && state.editable) owner.store.setState({editing: true, error: ""});
}

export function updateFileDraft(id: string, text: string): void {
  const owner = documentFor(id);
  const state = owner.store.getState();
  if (state.editing && state.editable && !state.saving) owner.store.setState({text});
}

export function cancelFileEdit(id: string): void {
  const owner = documentFor(id);
  const state = owner.store.getState();
  if (!state.saving) owner.store.setState({text: state.original, editing: false, conflict: false, error: ""});
}

export async function saveFileDocument(
  id: string, path: string, write: RuntimeApi["writeWorkbenchFile"], force = false,
): Promise<boolean> {
  const owner = documentFor(id);
  const state = owner.store.getState();
  if (!state.editing || !state.editable || state.saving) return false;
  const epoch = ++owner.epoch;
  owner.store.setState({saving: true, loading: false, error: ""});
  try {
    const result = await write?.(path, state.text, force ? undefined : state.mtimeMs);
    if (documents.get(id) !== owner || epoch !== owner.epoch) return false;
    if (result?.conflict) {
      owner.store.setState({saving: false, conflict: true});
      return false;
    }
    if (!result?.ok) throw new Error(result?.error || "Unable to save file");
    owner.store.setState({original: state.text, mtimeMs: Number(result.mtimeMs || Date.now()),
      saving: false, editing: false, conflict: false});
    return true;
  } catch (error) {
    if (documents.get(id) === owner && epoch === owner.epoch) {
      owner.store.setState({saving: false, error: error instanceof Error ? error.message : String(error)});
    }
    return false;
  }
}
