import assert from "node:assert/strict";
import {act} from "react";
import {createRoot} from "react-dom/client";
import {VirtualList} from "../frontend/main-deck/src/ui/VirtualList";
import {createRefreshQueue} from "../frontend/main-deck/src/workbench/refreshQueue";
import {watchPath} from "../frontend/main-deck/src/workbench/watchPath";
import type {RuntimeApi} from "../frontend/main-deck/src/types";
const pause=(ms=0)=>new Promise(resolve=>setTimeout(resolve,ms));
export async function run(){
  const host=document.createElement("div");document.body.appendChild(host);const root=createRoot(host);
  const rows=Array.from({length:5000},(_,index)=>`file-${index}.ts`);
  await act(async()=>root.render(<VirtualList items={rows} rowHeight={31} label="Changed files" itemKey={value=>value} render={value=><button>{value}</button>}/>));
  assert.ok(host.querySelectorAll('button').length<50,'thousands of changes cannot become thousands of DOM rows');
  const list=host.querySelector<HTMLElement>('[role="list"]')!;
  await act(async()=>{list.scrollTop=4990*31;list.dispatchEvent(new Event('scroll'))});
  assert.ok(host.textContent?.includes('file-4999.ts'),'the end of the list remains reachable');
  assert.ok(host.querySelectorAll('button').length<50);
  await act(async()=>root.unmount());host.remove();
  let active=0,peak=0,calls=0,release!:()=>void;
  const queue=createRefreshQueue(async()=>{calls++;active++;peak=Math.max(peak,active);await new Promise<void>(resolve=>{release=resolve});active--},error=>{throw error},1);
  for(let i=0;i<40;i++)queue.request();await pause(8);assert.equal(calls,1);
  for(let i=0;i<40;i++)queue.request();release();await pause(8);assert.equal(calls,2);assert.equal(peak,1);
  queue.request();queue.dispose();release();await pause(8);assert.equal(calls,2,'unmounted panes cannot schedule more work');
  let event!:Parameters<NonNullable<RuntimeApi['onWorkbenchPathChanged']>>[0],registered!:(value:{ok:boolean;id:string})=>void;
  let changes=0;const errors:string[]=[],stopped:string[]=[];
  const api:RuntimeApi={onWorkbenchPathChanged:callback=>{event=callback;return()=>{};},
    watchWorkbenchPath:()=>new Promise(resolve=>{registered=resolve;}),stopWorkbenchWatch:async id=>{stopped.push(id);return {ok:true};}};
  let dispose=watchPath(api,'C:/workspace',()=>changes++,{delay:1,onError:value=>errors.push(value)});
  event({id:'unrelated',path:'C:/other',event:'change'});event({id:'early-watch',path:'C:/workspace',event:'change'});
  registered({ok:true,id:'early-watch'});await pause(5);assert.equal(changes,1,'matching filesystem events survive delayed registration acknowledgement');dispose();
  dispose=watchPath(api,'C:/workspace',()=>changes++,{delay:1,onError:value=>errors.push(value)});
  event({id:'failed-watch',path:'C:/workspace',event:'error',error:'watch closed'});registered({ok:true,id:'failed-watch'});
  await pause(5);assert.deepEqual(errors,['watch closed']);dispose();
  dispose=watchPath(api,'C:/workspace',()=>changes++,{delay:1});dispose();registered({ok:true,id:'late-watch'});await pause(5);
  assert.ok(stopped.includes('late-watch'));assert.equal(changes,1,'disposed watch neither loses unwatch nor emits a refresh');
  console.log('Pane UI: bounded Review DOM, scrolling through all changes, refresh backpressure, and disposal passed');
}
