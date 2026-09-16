import type {RuntimeApi} from "../types";
import {registerWorkbenchBrowser,type WorkbenchWebview} from "./browserBridge";
import {noteBrowserPage} from "./previewStore";

type Attachment={id:string;host:HTMLElement;windowId:string;signature:string;frame:number;timer:ReturnType<typeof setTimeout>|null;cleanup:()=>void;sync:()=>void};
type RetainedBrowser={guest:WorkbenchWebview;state:Record<string,unknown>;api:RuntimeApi;attachment:Attachment|null;unregister:()=>void;navigationRevision:number;update:Promise<unknown>;layoutError:string};
const browsers=new Map<string,RetainedBrowser>();
let eventApi:RuntimeApi|null=null,unsubscribe:(()=>void)|null=null;
function applyState(id:string,browser:RetainedBrowser,state:Record<string,unknown>={}) {
  browser.state={...browser.state,...state};
  noteBrowserPage(id,{url:String(browser.state.url || "about:blank"),title:String(browser.state.title || ""),loading:browser.state.loading===true,canGoBack:browser.state.canGoBack===true,canGoForward:browser.state.canGoForward===true});
}
function emit(browser:RetainedBrowser,name:string,details:Record<string,unknown>={}) {
  const event=new Event(name,{cancelable:true});
  for(const [key,value] of Object.entries(details))if(!["type","target","currentTarget","isTrusted"].includes(key))try{Object.defineProperty(event,key,{value});}catch{/* readonly browser event field */}
  browser.guest.dispatchEvent(event);
}
function listen(api:RuntimeApi) {
  if(eventApi===api)return;
  unsubscribe?.();eventApi=api;
  unsubscribe=api.onWorkbenchBrowserEvent?.(event=>{
    const browser=browsers.get(event.tabId);if(!browser)return;
    applyState(event.tabId,browser,{...event.state,...(event.guestId ? {guestId:event.guestId} : {})});
    emit(browser,event.event,event.details);
  }) || null;
}
export function retainedBrowser(id:string,url:string,api:RuntimeApi,navigationRevision=0):RetainedBrowser {
  listen(api);
  const existing=browsers.get(id);if(existing){existing.api=api;return existing;}
  const guest=document.createElement("div") as WorkbenchWebview;
  guest.className="workbench-browser__guest";guest.dataset.retainedBrowser=id;
  const browser:RetainedBrowser={guest,state:{url,loading:true,zoomFactor:1},api,attachment:null,unregister:()=>{},navigationRevision,update:Promise.resolve(),layoutError:""};
  const call=async(method:string,args:unknown[]=[])=>{
    const result=await browser.api.workbenchBrowser?.({action:"call",tabId:id,method,args});
    if(browsers.get(id)!==browser)throw new Error("Browser tab was closed");
    if(!result?.ok)throw new Error(result?.error || "Browser operation failed");
    if(result.state)applyState(id,browser,result.state);return result.value;
  };
  const quiet=(method:string,args:unknown[]=[])=>void call(method,args).catch(error=>emit(browser,"console-message",{level:3,message:String(error)}));
  guest.getURL=()=>String(browser.state.url || url);guest.getTitle=()=>String(browser.state.title || "");
  guest.canGoBack=()=>browser.state.canGoBack===true;guest.canGoForward=()=>browser.state.canGoForward===true;
  guest.getWebContentsId=()=>Number(browser.state.guestId)||0;guest.getZoomFactor=()=>Number(browser.state.zoomFactor)||1;
  guest.isDevToolsOpened=()=>browser.state.devToolsOpened===true;
  guest.flushLayout=async()=>{browser.attachment?.sync();await browser.update;if(browser.layoutError)throw new Error(browser.layoutError);};
  guest.loadURL=async value=>{await call("loadURL",[value]);};
  guest.executeJavaScript=(code,userGesture)=>call("executeJavaScript",[code,!!userGesture]);
  guest.sendInputEvent=async event=>{await call("sendInputEvent",[event]);};
  guest.goBack=async()=>{await call("goBack");};guest.goForward=async()=>{await call("goForward");};
  guest.reload=async()=>{await call("reload");};guest.reloadIgnoringCache=async()=>{await call("reloadIgnoringCache");};
  guest.openDevTools=()=>quiet("openDevTools");guest.closeDevTools=()=>quiet("closeDevTools");
  guest.focus=()=>quiet("focus");guest.inspectElement=(x,y)=>quiet("inspectElement",[x,y]);
  guest.findInPage=(text,options)=>{quiet("findInPage",[text,options || {}]);return 0;};guest.stopFindInPage=action=>quiet("stopFindInPage",[action]);
  browsers.set(id,browser);
  browser.unregister=registerWorkbenchBrowser(id,guest,()=>({url:guest.getURL!(),title:guest.getTitle!(),loading:browser.state.loading===true,canGoBack:guest.canGoBack!(),canGoForward:guest.canGoForward!()}));
  return browser;
}

/** An attachment is a presentation lease; replacing it never replaces the page. */
export function attachRetainedBrowser(id:string,host:HTMLElement,api:RuntimeApi):()=>void {
  const browser=browsers.get(id);if(!browser)return()=>{};
  browser.attachment?.cleanup();
  host.appendChild(browser.guest);
  const owner=host.ownerDocument,win=owner.defaultView || window;
  const windowId=owner===document ? "" : new URL(win.location.href).searchParams.get("surface") || "";
  const attachment:Attachment={id:crypto.randomUUID(),host,windowId,signature:"",frame:0,timer:null,cleanup:()=>{},sync:()=>{}};
  browser.attachment=attachment;
  const layout=()=>{
    attachment.frame=0;if(attachment.timer)clearTimeout(attachment.timer);attachment.timer=null;
    if(browser.attachment!==attachment)return;
    const surface=host.parentElement;if(!surface)return;
    const rect=surface.getBoundingClientRect(),guestRect=browser.guest.getBoundingClientRect();
    const left=Math.max(0,rect.left),top=Math.max(0,rect.top),right=Math.min(win.innerWidth,rect.left+surface.clientWidth),bottom=Math.min(win.innerHeight,rect.top+surface.clientHeight);
    const style=win.getComputedStyle(host);
    const visible=host.isConnected && !owner.hidden && style.visibility!=="hidden" && guestRect.width>0 && right>left && bottom>top && !owner.querySelector("dialog[open],.workbench-browser__menu,.popup-menu,.workbench-menu");
    const previous=browser.state.viewport as {width?:number;height?:number}|undefined;
    const payload={tabId:id,attachmentId:attachment.id,windowId,url:String(browser.state.url || "about:blank"),bounds:{x:Math.round(left),y:Math.round(top),width:Math.max(0,Math.round(right-left)),height:Math.max(0,Math.round(bottom-top))},viewport:{width:Math.round(guestRect.width || previous?.width || 800),height:Math.round(guestRect.height || previous?.height || 480)},scroll:{x:Math.max(0,Math.round(left-guestRect.left)),y:Math.max(0,Math.round(top-guestRect.top))},visible};
    const signature=JSON.stringify(payload);if(signature===attachment.signature)return;
    const action=attachment.signature ? "layout" : "attach";attachment.signature=signature;
    browser.update=Promise.resolve(api.workbenchBrowser?.({action,...payload})).then(result=>{
      if(browser.attachment!==attachment || !browsers.has(id))return;
      if(!result?.ok){attachment.signature="";browser.layoutError=result?.error || "Browser presentation failed";emit(browser,"did-fail-load",{errorDescription:browser.layoutError,isMainFrame:true});return;}
      browser.layoutError="";
      if(result.state)applyState(id,browser,result.state);
      if(action==="attach"){emit(browser,"did-attach");if(browser.state.ready===true)emit(browser,"dom-ready");}
    }).catch(error=>{if(browser.attachment===attachment){attachment.signature="";browser.layoutError=String(error);emit(browser,"did-fail-load",{errorDescription:String(error),isMainFrame:true});}});
  };
  const schedule=()=>{
    if(attachment.frame || attachment.timer)return;
    attachment.frame=win.requestAnimationFrame(layout);
    // Visibility events still hide native content when animation frames pause.
    attachment.timer=setTimeout(()=>{if(attachment.frame)win.cancelAnimationFrame(attachment.frame);layout();},100);
  };
  const resize=new ResizeObserver(schedule);resize.observe(host);if(host.parentElement)resize.observe(host.parentElement);
  const mutation=new MutationObserver(records=>{if(records.some(record=>!(record.target as Element).closest?.(".workbench-browser__guest")))schedule();});
  mutation.observe(owner.body,{attributes:true,childList:true,subtree:true,attributeFilter:["class","style","hidden","open"]});
  owner.addEventListener("visibilitychange",schedule);win.addEventListener("resize",schedule);win.addEventListener("scroll",schedule,true);
  attachment.cleanup=()=>{
    if(attachment.frame)win.cancelAnimationFrame(attachment.frame);if(attachment.timer)clearTimeout(attachment.timer);
    resize.disconnect();mutation.disconnect();owner.removeEventListener("visibilitychange",schedule);win.removeEventListener("resize",schedule);win.removeEventListener("scroll",schedule,true);
    if(browser.attachment===attachment){browser.attachment=null;void api.workbenchBrowser?.({action:"detach",tabId:id,attachmentId:attachment.id}).catch(()=>{});}
  };
  attachment.sync=layout;layout();return attachment.cleanup;
}
export function destroyRetainedBrowser(id:string):void {
  const browser=browsers.get(id);if(!browser)return;
  browsers.delete(id);browser.attachment?.cleanup();browser.unregister();browser.guest.remove();
  void browser.api.workbenchBrowser?.({action:"destroy",tabId:id}).catch(()=>{});
  if(!browsers.size){unsubscribe?.();unsubscribe=null;eventApi=null;}
}
