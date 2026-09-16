'use strict';
/** Keep the current frontend graph free of abandoned parallel implementations. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const ts=require('typescript');
const root=path.resolve(__dirname,'..');
const source=path.join(root,'frontend/main-deck/src');
const files=fs.readdirSync(source,{recursive:true}).filter(file=>/\.tsx?$/.test(file)).map(file=>path.join(source,file));
const graph=new Map(files.map(file=>[file,[]]));
for(const file of files){
  const ast=ts.createSourceFile(file,fs.readFileSync(file,'utf8'),ts.ScriptTarget.Latest,true);
  function visit(node){
    let reference;
    if((ts.isImportDeclaration(node)||ts.isExportDeclaration(node))&&node.moduleSpecifier&&ts.isStringLiteral(node.moduleSpecifier))reference=node.moduleSpecifier.text;
    else if(ts.isCallExpression(node)&&node.expression.kind===ts.SyntaxKind.ImportKeyword&&node.arguments[0]&&ts.isStringLiteral(node.arguments[0]))reference=node.arguments[0].text;
    if(reference?.startsWith('.')){
      const base=path.resolve(path.dirname(file),reference);
      const target=[base,base+'.ts',base+'.tsx',path.join(base,'index.ts'),path.join(base,'index.tsx')].find(candidate=>graph.has(candidate));
      if(target)graph.get(file).push(target);
    }
    ts.forEachChild(node,visit);
  }
  visit(ast);
}
const reached=new Set();
function visit(file){if(reached.has(file))return;reached.add(file);for(const child of graph.get(file)||[])visit(child)}
visit(path.join(source,'main.tsx'));
const orphaned=files.filter(file=>!reached.has(file)&&!file.endsWith('.d.ts')&&path.basename(file)!=='fixture.ts');
assert.deepEqual(orphaned.map(file=>path.relative(source,file)),[], 'Production source must be reachable; keep test fixtures outside the production graph');
const corpus=files.map(file=>fs.readFileSync(file,'utf8')).join('\n');
assert.doesNotMatch(corpus,/BrowserPopoutApp|setPreviewPopped|bindBrowserHostSurface|ensureTerminalStarted|appendTerminalOutput|react-runtime-goals/,
  'retired views, subscriptions, and implicit terminal startup must not return');
for(const file of ['goalsStore.ts','ui/fabricPresentation.ts','workbench/BrowserPopoutApp.tsx','styles/knowledge-surfaces.css'])assert.equal(fs.existsSync(path.join(source,file)),false,file+' is retired');
const electron=['deck-preload.js','electron-deck-ipc.js','electron-app-boot.js'].map(file=>fs.readFileSync(path.join(root,file),'utf8')).join('\n');
assert.doesNotMatch(electron,/workbench:browser:(?:popout|location|closed)|__variant1BrowserPopout/,'Native panels are the sole browser detachment path');
// These remain live entry points, not legacy workspace copies.
for(const file of ['frontend/monitor.html','frontend/monitor.js','frontend/main-deck/popout.html'])assert.ok(fs.existsSync(path.join(root,file)));
console.log(`frontend reachability: ${reached.size} current source modules, no orphan implementations, no retired browser/startup path`);
