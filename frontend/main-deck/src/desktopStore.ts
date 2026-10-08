/**
 * Desktop control status (CuaDriver) for Settings › Desktop control.
 * Request/reply only: the backend never pushes, so the page polls while
 * macOS is waiting on the user's permission choice.
 */
import type {RuntimeContext} from "./types";
import {createModuleStore} from "./state/createModuleStore";
import {asRecord, createRequestIdFactory} from "./state/storePrimitives";

export type DesktopDriverState = "running" | "stopped" | "error" | "unavailable" | "waiting_permissions";

export type DesktopStatus = Readonly<{
  platform: string;
  driver: Readonly<{available: boolean; version: string; state: DesktopDriverState; error: string}>;
  canRequestPermissions: boolean;
  /** macOS only; null until CuaDriver has started once. */
  permissions: Readonly<{accessibility: boolean | null; screenRecording: boolean | null}> | null;
  sessionType: "wayland" | "x11" | null;
  desktop: string | null;
}>;

type State = Readonly<{
  connected: boolean;
  status: DesktopStatus | null;
  pending: string;
  requesting: boolean;
  error: string;
}>;

const store = createModuleStore<State>({initialState: {connected: false, status: null, pending: "", requesting: false, error: ""}});
const nextId = createRequestIdFactory("desktop-ui");
const STATES: readonly DesktopDriverState[] = ["running", "stopped", "error", "unavailable", "waiting_permissions"];
let timer: ReturnType<typeof setTimeout> | undefined;

export const useDesktopStatus = store.useStore;
export const getDesktopStatus = store.getState;
export const setDesktopContext = (context: RuntimeContext) => store.setContext(context);

function finish(error = ""): void {
  clearTimeout(timer); timer = undefined;
  store.setState({pending: "", requesting: false, error});
}

export function setDesktopConnection(status: string): void {
  const connected = status === "connected";
  store.setConnected(connected);
  if (!connected && store.getState().pending) finish("Connection interrupted.");
}

function request(type: "desktop:status" | "desktop:permissions:request"): boolean {
  const state = store.getState();
  if (state.pending) return false;
  if (!state.connected) { store.setState({error: "Backend offline."}); return false; }
  const id = nextId(type === "desktop:status" ? "status" : "permissions");
  store.setState({pending: id, requesting: type !== "desktop:status", error: ""});
  timer = setTimeout(() => { if (store.getState().pending === id) finish("No response received. Refresh to try again."); }, 15000);
  if (!store.send({type, request_id: id})) { finish("Request could not be sent."); return false; }
  return true;
}

export const refreshDesktopStatus = () => request("desktop:status");
/** macOS shows CuaDriver's own permission prompts. */
export const requestDesktopPermissions = () => request("desktop:permissions:request");

function flag(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function parseDesktopStatus(message: Record<string, unknown>): DesktopStatus {
  const driver = asRecord(message.driver);
  const permissions = message.permissions && typeof message.permissions === "object" ? asRecord(message.permissions) : null;
  const state = STATES.find(item => item === driver.state) || "unavailable";
  return {
    platform: String(message.platform || ""),
    driver: {available: driver.available === true, version: String(driver.version || ""), state, error: String(driver.error || "")},
    canRequestPermissions: message.can_request_permissions === true,
    permissions: permissions ? {accessibility: flag(permissions.accessibility), screenRecording: flag(permissions.screen_recording)} : null,
    sessionType: message.session_type === "wayland" || message.session_type === "x11" ? message.session_type : null,
    desktop: typeof message.desktop === "string" && message.desktop ? message.desktop : null,
  };
}

export function ingestDesktop(message: Record<string, unknown>): void {
  if (message.type !== "desktop:status" || message.request_id !== store.getState().pending) return;
  if (message.ok === false) { finish(String(asRecord(message.error).message || "Desktop status is unavailable.")); return; }
  const started = asRecord(message.permission_request).started;
  store.setState({status: parseDesktopStatus(message)});
  finish(started === false ? "macOS is already waiting for your answer in CuaDriver's prompt." : "");
}
