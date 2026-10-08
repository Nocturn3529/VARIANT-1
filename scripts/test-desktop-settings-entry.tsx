import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {DesktopSettings} from "../frontend/main-deck/src/DesktopSettings";
import {getDesktopStatus, ingestDesktop, setDesktopConnection, setDesktopContext} from "../frontend/main-deck/src/desktopStore";

/** Settings › Desktop control shows CuaDriver's state and each OS's limits; only the button asks macOS. */
export async function run() {
  const sent: Array<Record<string, unknown>> = [];
  setDesktopContext({send: (message: Record<string, unknown>) => { sent.push(message); return true; }, isOpen: () => true, notify() {}} as never);
  setDesktopConnection("connected");
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const reply = async (body: Record<string, unknown>) => act(async () => ingestDesktop({type: "desktop:status", request_id: sent.at(-1)!.request_id, ok: true, ...body}));
  const text = () => host.textContent || "";
  const button = (label: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find(node => node.textContent === label);
  const mac = {platform: "darwin", driver: {available: true, version: "0.34.0", state: "stopped"}, can_request_permissions: true, permissions: {accessibility: null, screen_recording: null}};
  try {
    await act(async () => root.render(<DesktopSettings/>));
    assert.equal(sent.at(-1)!.type, "desktop:status", "opening the page asks for status");
    await reply(mac);
    assert.match(text(), /Allow CuaDriver under System Settings › Privacy & Security › Accessibility and Screen Recording/);
    assert.match(text(), /stay in place when VARIANT-1 updates/);
    assert.match(text(), /Checked when CuaDriver first starts/);
    assert.equal(sent.filter(message => message.type === "desktop:permissions:request").length, 0, "status never asks macOS");

    await act(async () => button("Allow CuaDriver")!.click());
    assert.equal(sent.at(-1)!.type, "desktop:permissions:request");
    await reply({...mac, driver: {...mac.driver, state: "waiting_permissions"}, permission_request: {started: true}});
    assert.ok(button("Waiting for your answer in macOS…")?.disabled);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 2100)); });
    assert.equal(sent.at(-1)!.type, "desktop:status", "waiting for the user polls status");
    await reply({...mac, driver: {...mac.driver, state: "running"}, permissions: {accessibility: true, screen_recording: true}});
    assert.equal(button("Allow CuaDriver"), undefined, "granted permissions need no button");
    assert.equal([...host.querySelectorAll("em")].filter(node => node.textContent === "Allowed").length, 2);

    await act(async () => button("Refresh")!.click());
    await reply({platform: "linux", driver: {available: true, version: "0.34.0", state: "stopped"}, can_request_permissions: false, session_type: "wayland", desktop: "ubuntu:GNOME"});
    assert.match(text(), /Wayland session \(ubuntu:GNOME\)/); assert.match(text(), /GNOME restricts input and screen capture/);
    await act(async () => button("Refresh")!.click());
    await reply({platform: "win32", driver: {available: false, version: "", state: "unavailable", error: "cua-driver.exe is missing"}, can_request_permissions: false});
    assert.match(text(), /elevated apps can't be controlled/); assert.match(text(), /cua-driver\.exe is missing/);
    assert.equal(getDesktopStatus().status?.driver.state, "unavailable");

    await act(async () => button("Refresh")!.click());
    await act(async () => ingestDesktop({type: "desktop:status", request_id: "someone-else", ok: true, ...mac}));
    assert.equal(getDesktopStatus().status?.platform, "win32", "another request's reply is ignored");
    await act(async () => ingestDesktop({type: "desktop:status", request_id: sent.at(-1)!.request_id, ok: false, error: {code: "desktop_status_failed", message: "Status failed"}}));
    assert.match(text(), /Status failed/);
    console.log("Desktop control: status on open, macOS grant copy and button-only request, polling while waiting, Linux Wayland/GNOME and Windows elevation notes, stale replies and errors passed");
  } finally {
    await act(async () => root.unmount()); host.remove(); setDesktopConnection("offline");
  }
}
