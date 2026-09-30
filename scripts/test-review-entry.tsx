import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {ReviewPanel} from "../frontend/main-deck/src/context/ReviewPanel";
import {DiffView} from "../frontend/main-deck/src/context/DiffView";
import {parseDiff,pullRequestUrl} from "../frontend/main-deck/src/workbench/diffModel";
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
  assert.equal(pullRequestUrl("warning\nhttps://github.com/example/project/pull/7\n"),"https://github.com/example/project/pull/7");
  assert.equal(pullRequestUrl("https://user:password@example.test/a/b/pull/1"),"");
  assert.equal(pullRequestUrl("javascript:alert(1)"),"");
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  let clipboard="",creations=0,resolvePr:((value:Record<string,unknown>)=>void)|undefined;
  const external:string[]=[],diffRequests:Array<{file:string;staged:boolean}>=[];
  const priorApi=window.variant1Deck;
  const clipboardDescriptor=Object.getOwnPropertyDescriptor(window.navigator,"clipboard");
  Object.defineProperty(window.navigator,"clipboard",{configurable:true,value:{writeText:async(value:string)=>{clipboard=value;}}});
  window.variant1Deck={
    getWorkbenchGitStatus:async()=>({ok:true,root:"C:\\fixture",branch:"review",files:[{path:"sample.ts",status:"MM",staged:true,added:2,removed:1}]}),
    getWorkbenchGitDiff:async(_root,file,staged)=>{diffRequests.push({file:file || "",staged:!!staged});return {ok:true,diff:patch};},
    runWorkbenchGit:async(action)=>{assert.equal(action,"create_pr");creations++;return new Promise(resolve=>{resolvePr=resolve;});},
    openExternal:async(url)=>{external.push(url);return {ok:true};},
  } as RuntimeApi;
  try {
    await act(async()=>{root.render(<ReviewPanel directory={"C:\\fixture"} chatId="review-chat"/>);await pause();});
    assert.ok(host.querySelector('.workbench-diff__line.is-add'));
    assert.equal(host.querySelector('textarea')!.getAttribute('aria-label'),'Commit message');
    assert.equal(diffRequests.at(-1)?.staged,false);
    const hunk=host.querySelector<HTMLButtonElement>('.workbench-diff__hunk')!;
    await act(async()=>hunk.click());assert.equal(host.querySelector('.workbench-diff__line.is-add'),null);
    await act(async()=>hunk.click());
    await act(async()=>host.querySelector<HTMLButtonElement>('[aria-label="Open current file at line 4"]')!.click());
    assert.equal(getPreviewState().tabs.at(-1)?.target.line,4);
    await act(async()=>[...host.querySelectorAll('button')].find(button=>button.textContent==='Copy preview')!.click());
    assert.equal(clipboard,patch,'copy retains the bounded patch outside visible/expanded rows');
    await act(async()=>[...host.querySelectorAll('button')].find(button=>button.textContent==='Staged')!.click());
    assert.equal(diffRequests.at(-1)?.staged,true);
    await act(async()=>{const create=[...host.querySelectorAll('button')].find(button=>button.textContent==='Create PR')!;create.click();create.click();});
    assert.equal(creations,1,'duplicate clicks cannot create a second PR');
    await act(async()=>{resolvePr!({ok:true,stdout:'https://github.com/example/project/pull/7\n'});await pause();});
    const result=host.querySelector('.workbench-review__result')!;assert.match(result.textContent!,/Pull request created/);
    await act(async()=>result.querySelector<HTMLButtonElement>('button')!.click());assert.equal(external[0],'https://github.com/example/project/pull/7');
    await act(async()=>root.render(<DiffView text="Binary files a/photo.png and b/photo.png differ\n" binary/>));
    assert.match(host.textContent!,/Binary file changed/);
    await act(async()=>root.render(<DiffView text={"line\n".repeat(8000)} fullContents/>));
    assert.match(host.textContent!,/preview truncated/);
    assert.ok(host.querySelectorAll('[role="listitem"]').length<40,'large text has bounded DOM');
    await act(async()=>root.render(<DiffView text="@@ -0,0 +1 @@\n+<img src=x onerror=alert(1)>\n"/>));
    assert.equal(host.querySelector('img'),null,'patch contents remain inert text');
    let resolveOld:((value:Record<string,unknown>)=>void)|undefined;
    const api=window.variant1Deck!;
    api.getWorkbenchGitStatus=async(directory)=>directory==='C:\\old-project'
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
    console.log('Review: unified parsing/gutters, hunk disclosure, bounded DOM, exact line navigation, copy, staged selection and observable PR creation passed');
  } finally {
    await act(async()=>root.unmount());host.remove();window.variant1Deck=priorApi;
    if(clipboardDescriptor)Object.defineProperty(window.navigator,'clipboard',clipboardDescriptor);else delete (window.navigator as unknown as Record<string,unknown>).clipboard;
    for(const tab of getPreviewState().tabs)closePreview(tab.id);
  }
}
