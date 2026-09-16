"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const esbuild = require("esbuild");
const root = path.resolve(__dirname, "..");
const output = fs.mkdtempSync(path.join(os.tmpdir(), "variant1-workbench-fixture-"));
const frontend = path.join(root, "frontend");
const mime = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"};

(async () => {
  const bundler = await esbuild.context({entryPoints: {fixture: path.join(__dirname, "workbench-fixture-entry.ts")}, bundle: true,
    format: "esm", splitting: true, platform: "browser", target: "chrome136", outdir: output,
    loader: {".woff2": "file"}, logLevel: "silent"});
  await bundler.rebuild();
  await bundler.watch();
  const server = http.createServer((request, response) => {
    try {
      const url = new URL(request.url, "http://127.0.0.1");
      const fixtureAsset = url.pathname.startsWith("/__fixture__/");
      const base = fixtureAsset ? output : frontend;
      const relative = fixtureAsset ? url.pathname.slice("/__fixture__/".length) : url.pathname.slice(1);
      const file = path.resolve(base, decodeURIComponent(relative));
      if (!file.startsWith(base + path.sep)) { response.writeHead(403).end(); return; }
      let body = fs.readFileSync(file);
      if (url.pathname === "/main-deck/index.html") {
        body = Buffer.from(body.toString().replace("./dist/platform.js", "/__fixture__/fixture.js").replace("./dist/platform.css", "/__fixture__/fixture.css"));
      }
      response.writeHead(200, {"Content-Type": mime[path.extname(file)] || "application/octet-stream"}); response.end(body);
    } catch { response.writeHead(404).end(); }
  });
  server.listen(8770, "127.0.0.1", () => console.log("Isolated workbench fixture: http://127.0.0.1:8770/main-deck/index.html?fixture=1"));
})().catch(error => { console.error(error); process.exitCode = 1; });
