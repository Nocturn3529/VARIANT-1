/**
 * Composer attachment encoding and list management.
 */
import type {ChatAttachment} from "./types";
import {
  MAX_ATTACHMENTS,
  getChatState,
  notifyChat,
  patchChatState,
  revokeAttachmentUrls,
  sharedTurnActive,
} from "./stateCore";

/** Large drag-only images are resized for transport; backend owns model preprocessing. */
const MAX_IMAGE_EDGE = 2048;
/** Per-image backend transport ceiling (~6 MB binary). */
const MAX_IMAGE_B64_CHARS = 8_000_000;
const MAX_IMAGE_INPUT_BYTES = 40 * 1024 * 1024;
const MAX_IMAGE_ATTACHMENTS = 4;
const MAX_TEXT_BYTES = 200 * 1024;
const IMAGE_MIME = new Set([
  "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
  "image/bmp", "image/x-png", "image/pjpeg", "image/tiff", "image/heic",
  "image/heif", "image/avif",
]);
const IMAGE_EXT = /\.(png|jpe?g|webp|gif|bmp|jfif|tiff?|heic|heif|avif)$/i;
const TEXT_MIME = new Set([
  "text/plain", "text/markdown", "text/csv", "text/tab-separated-values",
  "application/json", "application/xml", "text/xml", "text/html", "text/css",
  "application/javascript", "text/javascript", "text/x-python", "text/x-log",
]);
const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|xml|html?|css|js|ts|tsx|jsx|py|log|yml|yaml|toml|ini|cfg|env|sh|ps1|bat|c|cpp|h|rs|go|java|kt|rb|php|sql)$/i;

/** Resolve a disk-backed Web File through Electron's isolated preload bridge. */
function fileAbsPath(file: File): string {
  try {
    return String(window.variant1Deck?.getPathForFile?.(file) || "");
  } catch {
    return "";
  }
}

function imageMime(name: string, declared = ""): string {
  const mime = String(declared || "").toLowerCase();
  if (IMAGE_MIME.has(mime)) return mime === "image/jpg" ? "image/jpeg" : mime;
  const lower = String(name || "").toLowerCase();
  if (/\.jpe?g$|\.jfif$/.test(lower)) return "image/jpeg";
  if (/\.webp$/.test(lower)) return "image/webp";
  if (/\.gif$/.test(lower)) return "image/gif";
  if (/\.bmp$/.test(lower)) return "image/bmp";
  if (/\.tiff?$/.test(lower)) return "image/tiff";
  if (/\.heic$/.test(lower)) return "image/heic";
  if (/\.heif$/.test(lower)) return "image/heif";
  if (/\.avif$/.test(lower)) return "image/avif";
  return "image/png";
}

function isImageFile(file: File): boolean {
  const type = String(file.type || "").toLowerCase();
  if (type.startsWith("image/")) return true;
  if (IMAGE_MIME.has(type)) return true;
  return IMAGE_EXT.test(file.name || "");
}

function isTextFile(file: File): boolean {
  const type = String(file.type || "").toLowerCase();
  if (TEXT_MIME.has(type)) return true;
  if (type.startsWith("text/")) return true;
  return TEXT_EXT.test(file.name || "");
}

function readFileAsArrayBuffer(file: File): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsArrayBuffer(file);
  });
}

function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("read failed"));
    reader.readAsText(file);
  });
}

function bytesToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  const chunk = 0x8000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function canvasToBase64(
  canvas: HTMLCanvasElement,
  mime: "image/png" | "image/jpeg",
  quality?: number,
): string {
  const outUrl = canvas.toDataURL(mime, quality);
  const comma = outUrl.indexOf(",");
  return comma >= 0 ? outUrl.slice(comma + 1) : "";
}

/**
 * Encode a pathless Web File for transport. Small supported images stay in
 * their original format; oversized files are resized without creating an
 * attachment that contains neither bytes nor a usable path.
 */
async function encodeImageAttachment(
  file: File,
): Promise<{data: string; previewUrl: string; mime: string}> {
  const previewUrl = URL.createObjectURL(file);
  const revoke = () => {
    try {
      URL.revokeObjectURL(previewUrl);
    } catch {
      /* ignore */
    }
  };

  try {
    const raw = await readFileAsArrayBuffer(file);
    if (!raw.byteLength) throw new Error("empty image");
    if (raw.byteLength > MAX_IMAGE_INPUT_BYTES) throw new Error("image too large");
    if (raw.byteLength <= Math.floor(MAX_IMAGE_B64_CHARS * 0.74)) {
      const rawB64 = bytesToBase64(raw);
      if (rawB64.length <= MAX_IMAGE_B64_CHARS) {
        return {data: rawB64, previewUrl, mime: imageMime(file.name, file.type)};
      }
    }

    let bitmap: ImageBitmap | null = null;
    if (typeof createImageBitmap === "function") {
      bitmap = await createImageBitmap(file);
    }
    if (bitmap) {
      try {
        const sourceMime = imageMime(file.name, file.type);
        const preferPng = sourceMime === "image/png" || sourceMime === "image/bmp";
        for (const edge of [MAX_IMAGE_EDGE, 1800, 1600, 1280]) {
          const scale = Math.min(1, edge / Math.max(bitmap.width, bitmap.height, 1));
          const width = Math.max(1, Math.round(bitmap.width * scale));
          const height = Math.max(1, Math.round(bitmap.height * scale));
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          const ctx = canvas.getContext("2d");
          if (!ctx) throw new Error("canvas unavailable");
          ctx.drawImage(bitmap, 0, 0, width, height);
          if (preferPng) {
            const png = canvasToBase64(canvas, "image/png");
            if (png && png.length <= MAX_IMAGE_B64_CHARS) {
              return {data: png, previewUrl, mime: "image/png"};
            }
          }
          ctx.globalCompositeOperation = "destination-over";
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, width, height);
          for (const quality of [0.92, 0.86, 0.78]) {
            const jpeg = canvasToBase64(canvas, "image/jpeg", quality);
            if (jpeg && jpeg.length <= MAX_IMAGE_B64_CHARS) {
              return {data: jpeg, previewUrl, mime: "image/jpeg"};
            }
          }
        }
      } finally {
        try {
          bitmap.close();
        } catch {
          /* ignore */
        }
      }
    }
    throw new Error("image remains too large after resize");
  } catch (err) {
    revoke();
    throw err;
  }
}

function newAttId(): string {
  return `att-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

let attachmentGeneration = 0;
let attachmentQueue: Promise<void> = Promise.resolve();

/**
 * Invalidate FileReader/image work that belongs to an older composer turn.
 * Reads cannot be synchronously cancelled on every supported path, so their
 * results are discarded (and preview URLs revoked) before they can commit.
 */
export function invalidatePendingChatAttachments() {
  attachmentGeneration += 1;
  attachmentQueue = Promise.resolve();
  if (getChatState().attachmentsPreparing) patchChatState({attachmentsPreparing: 0});
}

export function removeChatAttachment(id: string) {
  const state = getChatState();
  const next = state.attachments.filter(item => item.id !== id);
  if (next.length === state.attachments.length) return;
  patchChatState({attachments: next});
}

export function clearChatAttachments() {
  invalidatePendingChatAttachments();
  const state = getChatState();
  if (!state.attachments.length) return;
  patchChatState({attachments: []});
}

/** Attach an already-known same-user filesystem path from the workbench tree. */
export function addChatPathAttachment(path: string, directory = false): boolean {
  const value = String(path || "").trim();
  if (!value || sharedTurnActive()) return false;
  const state = getChatState();
  if (state.attachments.length >= MAX_ATTACHMENTS) {
    notifyChat(`At most ${MAX_ATTACHMENTS} attachments per message`);
    return false;
  }
  if (state.attachments.some(item => item.path === value)) return true;
  const name = value.split(/[\\/]/).filter(Boolean).pop() || value;
  patchChatState({attachments: [...state.attachments, {
    id: newAttId(),
    name,
    kind: "path",
    mime: directory ? "inode/directory" : "application/octet-stream",
    path: value,
    size: 0,
  }]});
  notifyChat(`Attached ${directory ? "folder" : "file"}: ${name}`);
  return true;
}

async function addChatFilesNow(files: File[], generation: number): Promise<void> {
  if (generation !== attachmentGeneration) return;
  if (sharedTurnActive()) {
    notifyChat("Wait for VARIANT-1 to finish before attaching files");
    return;
  }
  const state = getChatState();
  const room = MAX_ATTACHMENTS - state.attachments.length;
  if (room <= 0) {
    notifyChat(`At most ${MAX_ATTACHMENTS} attachments per message`);
    return;
  }
  const accepted: ChatAttachment[] = [];
  const skipped: string[] = [];
  const discardAccepted = () => revokeAttachmentUrls(accepted);
  const operationCurrent = () => (
    generation === attachmentGeneration && !sharedTurnActive()
  );
  let imageCount = state.attachments.filter(item => item.kind === "image").length;
  for (const file of files.slice(0, room + 4)) {
    if (!operationCurrent()) {
      discardAccepted();
      return;
    }
    if (accepted.length >= room) break;
    const name = file.name || "file";
    const absPath = fileAbsPath(file);
    try {
      if (isImageFile(file)) {
        if (imageCount >= MAX_IMAGE_ATTACHMENTS) {
          skipped.push(`${name} (maximum ${MAX_IMAGE_ATTACHMENTS} images)`);
          continue;
        }
        if (file.size > MAX_IMAGE_INPUT_BYTES) {
          skipped.push(`${name} (image over 40 MB)`);
          continue;
        }
        // Keep the real Electron path as the fast path, plus one bounded byte
        // fallback in case the file is moved or becomes unreadable before the
        // backend consumes the send. The fallback lives only through this
        // optimistic turn and is never persisted in transcript metadata.
        if (absPath) {
          let fallback: {data: string; previewUrl: string; mime: string} | null = null;
          try {
            fallback = await encodeImageAttachment(file);
          } catch {
            /* The absolute path remains usable even when fallback encoding fails. */
          }
          if (!operationCurrent()) {
            if (fallback) revokeAttachmentUrls([{
              id: "discarded", name, kind: "image", mime: fallback.mime,
              data: fallback.data, previewUrl: fallback.previewUrl, size: file.size,
            }]);
            discardAccepted();
            return;
          }
          accepted.push({
            id: newAttId(),
            name,
            kind: "image",
            mime: fallback?.mime || imageMime(name, file.type),
            path: absPath,
            data: fallback?.data,
            previewUrl: fallback?.previewUrl || URL.createObjectURL(file),
            size: file.size,
          });
          imageCount += 1;
          continue;
        }
        let encoded: {data: string; previewUrl: string; mime: string};
        try {
          encoded = await encodeImageAttachment(file);
        } catch (err) {
          const why = err instanceof Error ? err.message : "could not read";
          skipped.push(`${name} (${why})`);
          continue;
        }
        accepted.push({
          id: newAttId(),
          name,
          kind: "image",
          mime: encoded.mime,
          data: encoded.data,
          previewUrl: encoded.previewUrl,
          size: file.size,
        });
        if (!operationCurrent()) {
          discardAccepted();
          return;
        }
        imageCount += 1;
        continue;
      }
      if (isTextFile(file)) {
        if (file.size > MAX_TEXT_BYTES && !absPath) {
          skipped.push(`${name} (text file over 200 KB)`);
          continue;
        }
        let text: string | undefined;
        if (file.size <= MAX_TEXT_BYTES) {
          try {
            text = await readFileAsText(file);
          } catch {
            /* path fallback */
          }
        }
        if (!operationCurrent()) {
          discardAccepted();
          return;
        }
        if (text === undefined && !absPath) {
          skipped.push(`${name} (could not read this file)`);
          continue;
        }
        accepted.push({
          id: newAttId(),
          name,
          kind: "text",
          mime: file.type || "text/plain",
          text,
          path: absPath || undefined,
          size: file.size,
        });
        continue;
      }
      if (absPath) {
        accepted.push({
          id: newAttId(),
          name,
          kind: "path",
          mime: file.type || "application/octet-stream",
          path: absPath,
          size: file.size,
        });
        continue;
      }
      skipped.push(`${name} (use an image, text file, or local file with a readable path)`);
    } catch (err) {
      const why = err instanceof Error ? err.message : "could not read";
      skipped.push(`${name} (${why})`);
    }
  }
  if (!operationCurrent()) {
    discardAccepted();
    return;
  }
  const cur = getChatState();
  let remaining = Math.max(0, MAX_ATTACHMENTS - cur.attachments.length);
  let remainingImages = Math.max(
    0,
    MAX_IMAGE_ATTACHMENTS - cur.attachments.filter(item => item.kind === "image").length,
  );
  const committed: ChatAttachment[] = [];
  for (const item of accepted) {
    if (remaining <= 0 || (item.kind === "image" && remainingImages <= 0)) {
      revokeAttachmentUrls([item]);
      skipped.push(`${item.name} (attachment limit reached)`);
      continue;
    }
    committed.push(item);
    remaining -= 1;
    if (item.kind === "image") remainingImages -= 1;
  }
  if (committed.length) {
    patchChatState({attachments: [...cur.attachments, ...committed]});
    const nImg = committed.filter(a => a.kind === "image").length;
    if (nImg) {
      notifyChat(`Attached ${committed.length} item${committed.length > 1 ? "s" : ""}`);
    }
  }
  if (skipped.length) {
    notifyChat(
      `Skipped: ${skipped.slice(0, 3).join(", ")}${skipped.length > 3 ? "…" : ""}`,
    );
  } else if (files.length > room && room > 0) {
    notifyChat(`Only ${MAX_ATTACHMENTS} attachments allowed — extra files ignored`);
  }
}

/**
 * Add files from the composer file picker or drag-drop. Operations are
 * serialized so two concurrent drops cannot both reserve the same slots.
 */
export function addChatFiles(fileList: FileList | File[] | null | undefined, expectedSessionId = getChatState().sessionId): Promise<void> {
  const files = fileList ? Array.from(fileList) : [];
  if (!files.length) return Promise.resolve();
  const state = getChatState();
  if (expectedSessionId !== state.sessionId) {
    notifyChat("The chat changed while choosing files. Attach them to the intended chat again.");
    return Promise.resolve();
  }
  if (sharedTurnActive()) {
    notifyChat("Wait for VARIANT-1 to finish before attaching files");
    return Promise.resolve();
  }
  const generation = attachmentGeneration;
  patchChatState({attachmentsPreparing: state.attachmentsPreparing + files.length});
  const task = attachmentQueue.then(() => addChatFilesNow(files, generation));
  attachmentQueue = task.catch(error => {
    if (generation !== attachmentGeneration) return;
    const why = error instanceof Error ? error.message : "could not read attachments";
    notifyChat(why);
  }).finally(() => {
    if (generation === attachmentGeneration) patchChatState({attachmentsPreparing: Math.max(0, getChatState().attachmentsPreparing - files.length)});
  });
  return attachmentQueue;
}
