"use strict";
const assert = require("node:assert/strict"), path = require("node:path"), Module = require("node:module");
const {buildSync} = require("esbuild"), {JSDOM} = require("jsdom");
const root = path.resolve(__dirname, "..");
const dom = new JSDOM('<div id="root"></div>', {url: "http://localhost"});
global.window = dom.window; global.document = dom.window.document;
Object.defineProperty(global, "navigator", {value: dom.window.navigator, configurable: true});
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require("react"), {createRoot} = require("react-dom/client");
const output = buildSync({stdin: {contents: 'export * from "./localModelsStore"; export * from "./LocalModelLibrary";', resolveDir: path.join(root, "frontend/main-deck/src"), loader: "ts"},
  bundle: true, platform: "node", format: "cjs", jsx: "automatic", external: ["react", "react-dom"], loader: {".css": "empty"}, write: false, logLevel: "silent"}).outputFiles[0].text;
const compiled = new Module(__filename + ".bundle", module); compiled.filename = __filename; compiled.paths = module.paths; compiled._compile(output, __filename);
const api = compiled.exports, sent = [];
const actions = ["search", "files", "download", "cancel", "activate", "eject", "delete"];
const snapshot = {revision: 1, installed: [{id: "owned", name: "Owned.gguf", size_bytes: 1024 ** 3, active: false, managed_download: true}, {id: "user", name: "User.gguf", size_bytes: 1024 ** 3, active: true, managed_download: false}], jobs: [], hardware: {ram_total_mb: 32768}, supported_actions: actions};
const last = operation => sent.filter(item => item.type === `local-models:${operation}`).at(-1);
function reply(request, result, ok = true) {api.ingestLocalModels({type: "local-models:result", operation: request.type.split(":")[1], request_id: request.request_id, ok, result, error: ok ? undefined : {message: "Fixture failure"}});}
const click = text => {const button = [...document.querySelectorAll("button")].find(item => item.textContent === text); assert.ok(button, text); React.act(() => button.click());};
api.setLocalModelsContext({send: payload => {sent.push(payload); return true;}});
assert.equal(api.refreshLocalModels(), false, "offline requests are rejected");
api.setLocalModelsConnection("connected");
api.refreshLocalModels(); const oldGet = last("get");
api.setLocalModelsConnection("offline"); api.setLocalModelsConnection("connected"); api.refreshLocalModels();
reply(oldGet, snapshot); assert.equal(api.getLocalModels().snapshot, null, "disconnected reply is ignored");
reply(last("get"), snapshot);
assert.equal(api.requestLocalModels("download", {repo: "owner/model", paths: ["x"], revision: "fixed"}), true);
assert.equal(api.requestLocalModels("activate", {model_id: "owned"}), false, "mutations serialize");
api.refreshLocalModels(); const preMutation = last("get");
reply(last("download"), {id: "job"});
assert.notEqual(last("get").request_id, preMutation.request_id, "mutation refresh replaces an earlier snapshot request");
reply(preMutation, {...snapshot, revision: 0}); assert.equal(api.getLocalModels().snapshot.revision, 1);
reply(last("get"), snapshot);
assert.equal(sent.filter(item => item.type === "local-models:activate").length, 0, "download never activates automatically");
const mount = createRoot(document.getElementById("root"));
React.act(() => mount.render(React.createElement(api.LocalModelLibrary)));
React.act(() => reply(last("get"), snapshot));
assert.match(document.body.textContent, /1.0 GB/);
assert.equal([...document.querySelectorAll("button")].filter(item => item.textContent === "Delete").length, 1, "only owned downloads expose deletion");
React.act(() => {api.requestLocalModels("search", {query: "fixture"}); reply(last("search"), {items: [{repo: "owner/model", downloads: 12}]});});
click("Choose files");
React.act(() => reply(last("files"), {repo: "owner/model", revision: "immutable-fixture", variants: [
  {label: "split", paths: ["model-00001-of-00002.gguf", "model-00002-of-00002.gguf"], bytes: 2048, complete: true, projector: false},
  {label: "broken", paths: ["missing-part.gguf"], bytes: 1024, complete: false, projector: false},
  {label: "vision", paths: ["mmproj.gguf"], bytes: 1024, complete: true, projector: true},
  {label: "other-size", paths: ["other.gguf"], bytes: 2048, complete: true, projector: false},
  {label: "other-vision", paths: ["mmproj-other.gguf"], bytes: 1024, complete: true, projector: true}
]}));
const checks = document.querySelectorAll('.local-model-variants input');
assert.equal(checks[1].disabled, true, "incomplete split groups cannot be selected");
React.act(() => checks[2].click());
assert.equal([...document.querySelectorAll("button")].find(item => item.textContent.startsWith("Download ")).disabled, true, "projector alone is not a conversation model");
React.act(() => checks[0].click());
React.act(() => {checks[3].click(); checks[4].click();});
assert.equal(checks[0].checked,false,"one primary variant at a time");
assert.equal(checks[2].checked,false,"at most one optional projector");
assert.equal(checks[3].checked,true); assert.equal(checks[4].checked,true);
React.act(() => {checks[0].click(); checks[2].click();});
const download = [...document.querySelectorAll("button")].find(item => item.textContent.startsWith("Download "));
React.act(() => download.click());
assert.deepEqual(last("download").paths, ["model-00001-of-00002.gguf", "model-00002-of-00002.gguf", "mmproj.gguf"]);
assert.equal(last("download").revision, "immutable-fixture");
assert.ok(last("download").request_id);
React.act(() => {reply(last("download"), {id: "job2"}); reply(last("get"), snapshot);});
click("Activate"); assert.equal(last("activate").model_id, "owned");
React.act(() => reply(last("activate"), null, false)); assert.match(document.body.textContent, /Fixture failure/);
React.act(() => {api.refreshLocalModels(); reply(last("get"), {...snapshot, warning: "Download history could not be read.", jobs: ["failed", "interrupted"].map(status => ({id: status, repo: "fixture/" + status, status, phase: status, done_bytes: 0, total_bytes: 1000}))});});
assert.match(document.body.textContent,/Download history could not be read/);
assert.equal([...document.querySelectorAll("button")].some(button => button.textContent === "Cancel download"),false,"settled failed/interrupted jobs cannot be cancelled again");
for(const status of ["done","failed","interrupted","cancelled"]) assert.equal(api.modelJobFinished(status),true);
assert.equal(api.modelJobFinished("running"),false);
React.act(() => mount.unmount()); api.setLocalModelsConnection("offline"); dom.window.close();
console.log("Local models UI: correlated replies, reconnect fencing, mutation refresh, split downloads, explicit activation and owned deletion passed");
