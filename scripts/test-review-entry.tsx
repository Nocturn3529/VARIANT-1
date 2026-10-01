import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {ReviewPanel} from "../frontend/main-deck/src/context/ReviewPanel";
import {DiffView} from "../frontend/main-deck/src/context/DiffView";
import {parseDiff} from "../frontend/main-deck/src/workbench/diffModel";
import {getPreviewState,closePreview} from "../frontend/main-deck/src/workbench/previewStore";
import type {RuntimeApi} from "../frontend/main-deck/src/types";
import {PreviewPane} from "../frontend/main-deck/src/workbench/PreviewPane";

const patch = "diff --git a/sample.ts b/sample.ts\n--- a/sample.ts\n+++ b/sample.ts\n@@ -2,2 +2,3 @@\n old\n-removed\n+added\n+++ content, not a header\n\\ No newline at end of file\n";
const pause=()=>new Promise(resolve=>setTimeout(resolve,0));

export async function run() {
  const parsed=parseDiff(patch);
  assert.equal(parsed.added,2);assert.equal(parsed.removed,1);
  assert.deepEqual(parsed.rows.filter(row=>row.type==="add").map(row=>row.newLine),[3,4]);
  assert.equal(parsed.rows.find(row=>row.type==="remove")?.oldLine,3);
  assert.equal(parseDiff(patch.replaceAll("\n","\r\n")).added,2);
  assert.equal(parseDiff("first\nsecond\n",true).rows.at(-1)?.newLine,2);
  assert.equal(parseDiff("@@ -0,0 +1 @@\n+one\n").rows.at(-1)?.newLine,1);
  assert.equal(parseDiff("diff --git a/old.ts b/new.ts\nsimilarity index 100%\nrename from old.ts\nrename to new.ts\n").added,0);
  assert.ok(parseDiff("x\n".repeat(8000),true).truncated);
  assert.ok(parseDiff("x".repeat(300000),true).truncated);
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  let clipboard="";
  const diffRequests:Array<{file:string;scope:string;context?:number;commits?:string[]}>=[];
  const priorApi=window.variant1Deck;
  const clipboardDescriptor=Object.getOwnPropertyDescriptor(window.navigator,"clipboard");
  Object.defineProperty(window.navigator,"clipboard",{configurable:true,value:{writeText:async(value:string)=>{clipboard=value;}}});
  HTMLElement.prototype.showPopover=()=>{};
  const fixtureFiles=[{path:"sample.ts",status:"MM",staged:true,added:2,removed:1}];
  window.variant1Deck={
    getWorkbenchGitStatus:async()=>({ok:true,root:"C:\\fixture",branch:"review",files:fixtureFiles}),
    getWorkbenchGitBranches:async()=>({ok:true,branches:[{ref:"refs/heads/other",name:"other",oid:"other-oid"}]}),
    getWorkbenchGitHistory:async(_root,options)=>({ok:true,resolvedOid:options.ref,commits:["a","b","c"].map((value,index)=>({oid:value.repeat(40),parents:[String.fromCharCode(value.charCodeAt(0)+1).repeat(40)],subject:`Fix review ${index+1}`,authorName:"Tester",committedAt:1}))}),
    getWorkbenchReviewFiles:async()=>({ok:true,root:"C:\\fixture",files:fixtureFiles}),
    getWorkbenchReviewDiff:async(_root,file,options)=>{diffRequests.push({file,scope:options.scope,context:options.context,commits:options.commits});return {ok:true,diff:patch};},
    runWorkbenchGit:async()=>{throw new Error("Inspection must not mutate Git");},
  } as RuntimeApi;
  const button=(label:string)=>[...document.querySelectorAll<HTMLButtonElement>('button')].find(row=>row.textContent===label || row.getAttribute('aria-label')===label)!;
  try {
    await act(async()=>{root.render(<ReviewPanel directory={"C:\\fixture"} chatId="review-chat"/>);await pause();});
    assert.ok(host.querySelector('.workbench-diff__line.is-add'));
    assert.equal(host.querySelector('textarea'),null,'commit footer removed');
    assert.equal(host.querySelector('.workbench-review__files'),null,'changed files are hidden initially');
    assert.equal(diffRequests.at(-1)?.scope,'uncommitted');
    await act(async()=>button('Changed files').click());
    assert.ok(host.querySelector('.workbench-review__files'),'sidebar toggle reveals files');
    assert.equal(host.querySelector('.workbench-review__body')?.lastElementChild?.tagName,'ASIDE','files are on the right');
    const hunk=host.querySelector<HTMLButtonElement>('.workbench-diff__hunk')!;
    await act(async()=>hunk.click());assert.equal(host.querySelector('.workbench-diff__line.is-add'),null);
    await act(async()=>hunk.click());
    await act(async()=>host.querySelector<HTMLButtonElement>('[aria-label="Open current file at line 4"]')!.click());
    assert.equal(getPreviewState().tabs.at(-1)?.target.line,4);
    await act(async()=>button('Actions for sample.ts').click());
    await act(async()=>button('Copy patch').click());
    assert.equal(clipboard,patch,'copy retains the bounded patch outside visible rows');
    await act(async()=>host.querySelector<HTMLButtonElement>('.workbench-review__scope')!.click());
    await act(async()=>button('Staged').click());
    assert.equal(diffRequests.at(-1)?.scope,'staged');
    assert.equal(host.querySelector('[aria-label="Open current file at line 4"]'),null,'index coordinates cannot be presented as worktree coordinates');
    await act(async()=>host.querySelector<HTMLButtonElement>('.workbench-diff__expand')!.click());
    assert.equal(diffRequests.at(-1)?.context,53,'context expansion reads real unchanged lines');
    await act(async()=>host.querySelector<HTMLButtonElement>('.workbench-review__scope')!.click());
    await act(async()=>document.querySelector<HTMLButtonElement>('[role="menuitemcheckbox"]')!.click());
    assert.equal(diffRequests.at(-1)?.scope,'commit');
    await act(async()=>document.querySelectorAll<HTMLButtonElement>('[role="menuitemcheckbox"]')[2].dispatchEvent(new MouseEvent('click',{bubbles:true,shiftKey:true})));
    assert.deepEqual(diffRequests.at(-1)?.commits,['a'.repeat(40),'b'.repeat(40),'c'.repeat(40)],'shift selection chooses a complete range');
    assert.equal(host.querySelector('[aria-label="Open current file at line 4"]'),null,'historical line numbers cannot open wrong current source');
    await act(async()=>button('Review options').click());
    await act(async()=>button('Collapse all files').click());
    assert.equal(host.querySelector('.workbench-diff'),null);
    await act(async()=>button('Review options').click());
    await act(async()=>button('Expand all files').click());
    assert.ok(host.querySelector('.workbench-diff'));
    await act(async()=>root.render(<DiffView text={"Binary files a/photo.png and b/photo.png differ\n"} binary/>));
    assert.match(host.textContent!,/Binary file changed/);
    await act(async()=>root.render(<DiffView text={"line\n".repeat(8000)} fullContents/>));
    assert.match(host.textContent!,/preview truncated/);
    assert.ok(host.querySelectorAll('[role="listitem"]').length<40,'large text has bounded DOM');
    await act(async()=>root.render(<DiffView text={"@@ -0,0 +1 @@\n+<img src=x onerror=alert(1)>\n"}/>));
    assert.equal(host.querySelector('img'),null,'patch contents remain inert text');
    await act(async()=>root.render(<DiffView text={"@@ -0,0 +1 @@\n+const value = 1;\n"} language="ts"/>));
    assert.ok(host.querySelector('.token-keyword'),'changed code reuses the safe tokenizer');
    let resolveOld:((value:Record<string,unknown>)=>void)|undefined;
    const api=window.variant1Deck!;
    api.getWorkbenchReviewFiles=async(directory)=>directory==='C:\\old-project'
      ? new Promise(resolve=>{resolveOld=resolve;})
      : {ok:true,root:directory,files:[{path:'current.ts',status:' M'}]};
    await act(async()=>{root.render(<ReviewPanel directory={"C:\\old-project"}/>);await pause();});
    await act(async()=>{root.render(<ReviewPanel directory={"C:\\new-project"}/>);await pause();});
    await act(async()=>{resolveOld!({ok:true,root:'C:\\old-project',files:[{path:'stale.ts',status:' M'}]});await pause();});
    assert.match(host.textContent!,/current\.ts/);assert.doesNotMatch(host.textContent!,/stale\.ts/,'a prior project cannot overwrite the selected repository');
    const tab=getPreviewState().tabs.at(-1)!;
    const sourceApi={readWorkbenchFile:async()=>({ok:true,text:Array.from({length:8000},(_,index)=>`Line ${index+1}`).join("\n"),editable:true,mtimeMs:1})} as RuntimeApi;
    await act(async()=>{root.render(<PreviewPane tabId={tab.id} api={sourceApi}/>);await pause();});
    assert.equal(host.querySelector('.workbench-file-preview__selected-line code')?.textContent,'Line 4','the related preview selects the requested source line');
    assert.ok(host.querySelectorAll('[data-line]').length<40,'line navigation keeps a large source preview virtualized');
    console.log('Review: unified parsing/gutters, hunk disclosure, bounded DOM, exact line navigation, copy, staged selection and scope/branch disclosure, real context expansion and footer removal passed');
  } finally {
    await act(async()=>root.unmount());host.remove();window.variant1Deck=priorApi;
    if(clipboardDescriptor)Object.defineProperty(window.navigator,'clipboard',clipboardDescriptor);else delete (window.navigator as unknown as Record<string,unknown>).clipboard;
    for(const tab of getPreviewState().tabs)closePreview(tab.id);
  }
}
