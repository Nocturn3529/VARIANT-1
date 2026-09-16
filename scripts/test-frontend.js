"use strict";

/**
 * Main Deck / Electron validation without invoking the Python test suite.
 *
 * Individual static tests may be updated when an ownership boundary
 * intentionally moves, but they must never be silently removed to make the
 * suite pass.
 */
const {execFileSync} = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");

function section(label, action) {
  console.log(`\n--- ${label} ---\n`);
  action();
}

section("Electron and renderer syntax", () => {
  const files = [
    ...fs.readdirSync(root).filter(name => /^electron-[\w-]+\.js$/.test(name)),
    "main.js", "deck-preload.js", "monitor-preload.js", "popout-preload.js",
    "frontend/monitor.js", "frontend/main-deck/public/mic-capture-processor.js",
    "frontend/main-deck/dev/fixture-loader.js",
  ];
  for (const file of files) execFileSync(process.execPath, ["--check", path.join(root, file)], {cwd: root, stdio: "inherit"});
});

section("Main Deck build", () => {
  execFileSync(process.execPath, [path.join(root, "scripts", "build-deck.js")], {
    cwd: root,
    stdio: "inherit",
  });
});

const tsc = path.join(root, "node_modules", "typescript", "bin", "tsc");
section("Main Deck TypeScript", () => {
  execFileSync(process.execPath, [
    tsc,
    "-p",
    path.join(root, "frontend", "main-deck", "tsconfig.json"),
    "--pretty",
    "false",
  ], {
    cwd: root,
    stdio: "inherit",
  });
});

const tests = [
  "test-deck-runtime.js",
  "test-peer-transport.js",
  "test-peer-ui.js",
  "test-terminal-stream.js",
  "test-mic-reconnect.js",
  "test-workbench.js",
  "test-workbench-files.js",
  "test-pane-resources.js",
  "test-bug-hunt-electron.js",
  "test-pane-ui.js",
  "test-test-selection.js",
  "test-settings-persistence.js",
  "test-frontend-maintainability.js",
  "test-frontend-design.js",
  "test-browser-host.js",
  "test-browser-settings.js",
  "test-chat-ownership.js",
  "test-chat-reliability.js",
  "test-composer.js",
  "test-input-queue.js",
  "test-goal-composer.js",
  "test-goal-guidance.js",
  "test-chat-notices.js",
  "test-agent-team.js",
  "test-browser-capture-ipc.js",
  "test-browser-downloads-ipc.js",
  "test-chat-command-types.js",
  "test-chat-overlays.js",
  "test-log-monitor-ipc.js",
  "test-native-popouts.js",
  "test-deck-runtime-lib.js",
  "test-deck-architecture.js",
  "test-frontend-reachability.js",
  "test-deck-design-system.js",
  "test-capability-surface-stores.js",
  "test-local-models-ui.js",
  "test-provider-auth-ui.js",
  "test-speech-config.js",
  "test-service-settings-ui.js",
  // Typed turn + session-gate behavior is exercised by test-deck-runtime.js.
  "test-main-deck-controls.js",
  "test-overview-receipts.js",
  "test-deck-fixture-hygiene.js",
  "test-overlay-removal.js",
  "test-deck-mic.js",
  "test-stream-isolation.js",
  "test-chat-attachments.js",
  "test-packaged-files.js",
  "test-electron-security.js",
  "test-grok-review-ipc.js",
  "test-electron-logging.js",
  "test-electron-backend.js",
];

for (const test of tests) {
  section(test, () => {
    execFileSync(process.execPath, [path.join(__dirname, test)], {
      cwd: root,
      stdio: "inherit",
    });
  });
}

if (!fs.existsSync(path.join(root, "frontend", "main-deck", "dist", "platform.js"))) {
  throw new Error("Main Deck bundle was not produced");
}

console.log("\nfrontend validation: all tests passed");
