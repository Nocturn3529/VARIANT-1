import assert from "node:assert/strict";
import {closeSettings, getAppState, navigateTo} from "../frontend/main-deck/src/state/appStore";
import {__resetWorkbenchForTests, detachWorkbenchPane, dockWorkbenchGroup, getWorkbenchState, setWorkbenchCompact} from "../frontend/main-deck/src/workbench/workbenchStore";
import {allPaneIds, findGroupOfPane} from "../frontend/main-deck/src/workbench/layoutModel";
import {allocateTracks, childTracks, trackFor} from "../frontend/main-deck/src/workbench/trackModel";
import {activityPresentation} from "../frontend/main-deck/src/chat/activityPresentation";
import {kernelStatusLabel} from "../frontend/main-deck/src/RuntimeOverlay";
import {canRevealPaneForStep} from "../frontend/main-deck/src/workbench/activityRouting";

navigateTo("memory");
assert.equal(getAppState().view, "settings", "legacy Memory links must open Settings");
assert.equal(getAppState().settingsCategory, "memory");
closeSettings();
assert.equal(getAppState().view, "chat");
for (const view of ["automations", "overview", "runtime"] as const) {
  navigateTo(view);
  assert.equal(getAppState().settingsReturnView, "chat", "utilities cannot become the underlying workspace");
  navigateTo("settings"); closeSettings();
  assert.equal(getAppState().view, "chat");
}

__resetWorkbenchForTests();
const initial = getWorkbenchState();
const dockLayout = JSON.stringify(initial.layout);
detachWorkbenchPane("files");
const detached = getWorkbenchState();
const group = findGroupOfPane(detached.layout, "files")!;
assert.ok(detached.floating[group.id]);
assert.equal(JSON.stringify(detached.layout), dockLayout, "detaching keeps the original dock location and split weights");
const context = {hidden: detached.hidden, known: new Set(allPaneIds(detached.layout)), compact: false, height: 720, floatingGroups: new Set([group.id])};
const floatTrack = trackFor(group, "row", context)!;
assert.equal(floatTrack.max, 0, "a detached group reserves no docked width");
assert.deepEqual(allocateTracks([floatTrack, {preferred: null, min: 360, max: Infinity}], 900, [1, 1]), [0, 900]);
if (detached.layout.type === "split") assert.ok(childTracks(detached.layout, "row", context).length,
  "zero tracks retain their React wrapper identity");
dockWorkbenchGroup(group.id);
assert.equal(Object.keys(getWorkbenchState().floating).length, 0);
assert.equal(JSON.stringify(getWorkbenchState().layout), dockLayout);
detachWorkbenchPane("workspace");
assert.equal(Object.keys(getWorkbenchState().floating).length, 0, "Chat cannot detach");
setWorkbenchCompact(true); detachWorkbenchPane("history");
assert.equal(Object.keys(getWorkbenchState().floating).length, 0, "compact mode uses the existing side drawer");

const step = {id: "cell-a", kind: "tool" as const, label: "Python", tool: "ipython", ts: 1,
  argsPreview: JSON.stringify({code: "samples = [12, 18, 11]"}),
  resultPreview: JSON.stringify({execution_count: 41, kernel_generation: 3, result: "3"})};
assert.equal(activityPresentation(step).input, "samples = [12, 18, 11]");
assert.equal(activityPresentation(step).executionCount, 41);
assert.equal(activityPresentation(step).generation, 3);
const truncated = activityPresentation({...step, resultPreview: '{"execution_count": 41, …'});
assert.equal(truncated.generation, null, "missing metadata must not borrow the current generation");
assert.equal(truncated.executionCount, null);
assert.equal(activityPresentation({...step, tool: "search"}).generation, null);
assert.equal(canRevealPaneForStep(step), false, "a pure Python cell must not advertise an inert panel action");
assert.equal(canRevealPaneForStep({...step, evidence: [{id: "output", kind: "file", label: "Output", value: "C:\\output.txt"}]}), true);
assert.equal(kernelStatusLabel(false, "ready"), "Kernel offline", "a stale ready snapshot cannot conceal a disconnected backend");
assert.equal(kernelStatusLabel(true, "busy"), "Kernel working");
console.log("chat overlays: navigation, docking, and trace metadata checks passed");
