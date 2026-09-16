"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const {spawn} = require("node:child_process");
const esbuild = require("esbuild");
const root = path.resolve(__dirname, "..");
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "variant1-pdf-preview-"));

function pdfData() {
  const stream = "BT /F1 18 Tf 50 740 Td (VARIANT-1 PDF preview check) Tj ET";
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let pdf = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((body, index) => { offsets.push(pdf.length); pdf += `${index + 1} 0 obj\n${body}\nendobj\n`; });
  const xref = pdf.length;
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.map(offset => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return `data:application/pdf;base64,${Buffer.from(pdf).toString("base64")}`;
}

(async () => {
  const component = path.join(root, "frontend/main-deck/src/workbench/PdfPreview.tsx").replaceAll("\\", "/");
  await esbuild.build({stdin: {contents: `
    import {createRoot} from 'react-dom/client';
    import {PdfPreview} from ${JSON.stringify(component)};
    document.addEventListener('securitypolicyviolation', event => console.error('PDF_CSP', event.violatedDirective, event.blockedURI));
    createRoot(document.getElementById('variant1-react-root')).render(<PdfPreview source={${JSON.stringify(pdfData())}} label="Sample PDF"/>);
  `, loader: "tsx", resolveDir: root}, jsx: "automatic", bundle: true, platform: "browser", format: "esm",
    outfile: path.join(temp, "renderer.js"), logLevel: "silent"});
  const html = fs.readFileSync(path.join(root, "frontend/main-deck/index.html"), "utf8")
    .replace('./dist/platform.css', './fixture.css').replace('./dist/platform.js', './renderer.js');
  fs.writeFileSync(path.join(temp, "index.html"), html);
  fs.writeFileSync(path.join(temp, "fixture.css"), 'html,body,#variant1-react-root,iframe {width:100%;height:100%;margin:0;border:0}');
  const screenshot = path.join(require('./native-test-artifacts')(root,'artifacts','pdf'), "frontend-pdf-preview.png");
  fs.writeFileSync(path.join(temp, "main.cjs"), `
    const {app, BrowserWindow, protocol} = require('electron');
    const fs = require('node:fs');
    const assert = require('node:assert/strict');
    const boot = require(${JSON.stringify(path.join(root, "electron-app-boot.js"))});
    boot.applyGpuFlags(app);
    boot.registerVariant1Scheme(protocol);
    app.setPath('userData', ${JSON.stringify(path.join(temp, "profile"))});
    app.whenReady().then(async () => {
      boot.registerVariant1Protocol(protocol, ${JSON.stringify(temp)});
      const win = new BrowserWindow({show:false,width:760,height:800,webPreferences:{sandbox:true,contextIsolation:true,nodeIntegration:false,backgroundThrottling:false}});
      const errors = [];
      win.webContents.on('console-message', event => { if (/PDF_CSP|Error/.test(event.message || '')) errors.push(event.message); });
      await win.loadURL('variant1://app/index.html');
      const findViewer = frame => frame.url.startsWith('chrome-extension://') ? frame : frame.frames.map(findViewer).find(Boolean);
      const deadline = Date.now() + 10000;
      let ready = false;
      while (Date.now() < deadline) {
        const viewer = findViewer(win.webContents.mainFrame);
        if (viewer) {
          ready = await viewer.executeJavaScript('Boolean(document.querySelector("pdf-viewer")?.shadowRoot?.querySelector("viewer-toolbar"))').catch(() => false);
          if (ready) break;
        }
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      assert.ok(ready, 'The built-in PDF viewer must load inside the Blob frame');
      assert.deepEqual(errors, [], 'PDF rendering must not violate the application CSP');
      // The extension toolbar mounts before PDFium presents the first page.
      // Allow its separate renderer to paint before capturing the QA image.
      await new Promise(resolve => setTimeout(resolve, 4000));
      fs.writeFileSync(${JSON.stringify(screenshot)}, (await win.webContents.capturePage()).toPNG());
      console.log('PDF preview: production protocol, Blob frame, native viewer, and CSP checks passed');
      app.quit();
    }).catch(error=>{console.error(error);app.exit(1)});
  `);
  const child = spawn(require("electron"), [path.join(temp, "main.cjs")], {cwd: root, windowsHide: true, stdio: "inherit"});
  const code = await new Promise(resolve => child.once("exit", resolve));
  if (code) process.exitCode = code;
  if (path.dirname(temp) === path.resolve(os.tmpdir()) && path.basename(temp).startsWith("variant1-pdf-preview-")) {
    fs.rmSync(temp, {recursive: true, force: true, maxRetries: 4, retryDelay: 150});
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
