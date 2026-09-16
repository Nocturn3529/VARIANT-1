/**
 * Sticky online/offline UI for Deck islands.
 *
 * The WebSocket can briefly report connecting/offline during a healthy session
 * (reconnect probes, getBackendInfo races). Flipping badges every tick causes
 * visible flicker in Chat and Memory headers. Hold the last stable state for a
 * short window before painting offline.
 */

export type WireStatus = "connected" | "connecting" | "offline" | string;

export const OFFLINE_HOLD_MS = 2500;
const ONLINE_HOLD_MS = 200;

type Gate = {
  initialized: boolean;
  paintedOnline: boolean;
  pending: ReturnType<typeof setTimeout> | null;
  raw: WireStatus;
};

const gates = new Map<string, Gate>();

/** Data refresh follows transport lifetimes, independently of the painted badge. */
export function createReconnectRefresh(refresh: () => void): (status: WireStatus) => void {
  let connected = false;
  return status => {
    const next = status === "connected";
    const opened = next && !connected;
    connected = next;
    if (opened) refresh();
  };
}

function gateFor(id: string): Gate {
  let g = gates.get(id);
  if (!g) {
    g = {initialized: false, paintedOnline: false, pending: null, raw: "offline"};
    gates.set(id, g);
  }
  return g;
}

/**
 * Map a raw transport status to a stable boolean for UI.
 * @param id unique per island (chat, memory, …)
 * @param status transport status
 * @param onStable called only when the painted online/offline value changes
 */
export function pushWireStatus(
  id: string,
  status: WireStatus,
  onStable: (online: boolean) => void,
): void {
  const g = gateFor(id);
  g.raw = status;
  const wantOnline = status === "connected";

  if (g.pending) {
    clearTimeout(g.pending);
    g.pending = null;
  }

  if (wantOnline) {
    if (g.paintedOnline) return;
    g.pending = setTimeout(() => {
      g.pending = null;
      if (g.raw !== "connected") return;
      g.initialized = true;
      g.paintedOnline = true;
      onStable(true);
    }, ONLINE_HOLD_MS);
    return;
  }

  // connecting + offline → wait before painting offline
  if (!g.paintedOnline) {
    if (!g.initialized) {
      // Cold-start offline is meaningful state, not a transition to suppress.
      // Paint it once so Chat/History do not claim Ready/empty while the
      // backend is unavailable; repeated probes remain quiet after this.
      g.initialized = true;
      onStable(false);
    }
    // Already offline in UI — do not re-notify (avoids badge/toast thrash).
    return;
  }
  g.pending = setTimeout(() => {
    g.pending = null;
    if (g.raw === "connected") return;
    g.paintedOnline = false;
    onStable(false);
  }, OFFLINE_HOLD_MS);
}

export function resetWireStatus(id: string): void {
  const g = gates.get(id);
  if (!g) return;
  if (g.pending) clearTimeout(g.pending);
  gates.delete(id);
}
