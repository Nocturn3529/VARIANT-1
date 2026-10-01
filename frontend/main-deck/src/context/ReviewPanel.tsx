import {useEffect,useRef,useState,type MouseEvent} from "react";
import {Icon} from "../ui/Icon";
import {PopupMenu} from "../ui/PopupMenu";
import {VirtualList} from "../ui/VirtualList";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {createRefreshQueue} from "../workbench/refreshQueue";
import {watchPath} from "../workbench/watchPath";
import {openFilePreview} from "../workbench/previewStore";
import {reviewPath,reviewDeleted} from "../workbench/diffModel";
import {ReviewFile,ReviewCounts} from "./ReviewFile";
import type {WorkbenchGitFile,WorkbenchGitBranch,WorkbenchGitCommit,WorkbenchReviewOptions} from "../types";

type Menu={kind:"scope"|"branch"|"options"|"file";x:number;y:number;file?:WorkbenchGitFile};
const labels={uncommitted:"Uncommitted",staged:"Staged",unstaged:"Unstaged",commit:"Selected commits"};
const reviewError=(value:unknown)=>({noncontiguous_commit_selection:"Select consecutive commits from the same branch.",invalid_commit_selection:"Select between 1 and 100 consecutive commits."}[String(value)] || String(value || "Unable to read changes."));

export function ReviewPanel({directory="",chatId=""}:{directory?:string;chatId?:string}) {
  const ownerDocument=useSurfaceDocument(),ownerWindow=ownerDocument.defaultView || window;
  const api=window.variant1Deck,panel=useRef<HTMLElement>(null);
  const epoch=useRef(0),historyEpoch=useRef(0);
  const commitAnchor=useRef(""),mutating=useRef(false);
  const [root,setRoot]=useState(directory),[currentBranch,setCurrentBranch]=useState("");
  const [branches,setBranches]=useState<WorkbenchGitBranch[]>([]),[branch,setBranch]=useState("HEAD");
  const [commits,setCommits]=useState<WorkbenchGitCommit[]>([]),[historyOid,setHistoryOid]=useState("");
  const [nextOffset,setNextOffset]=useState<number|undefined>(),[historyError,setHistoryError]=useState("");
  const [historyLoading,setHistoryLoading]=useState(false),[options,setOptions]=useState<WorkbenchReviewOptions>({scope:"uncommitted"});
  const [files,setFiles]=useState<WorkbenchGitFile[]>([]),[filesTruncated,setFilesTruncated]=useState(false);
  const [error,setError]=useState(""),[loading,setLoading]=useState(false),[revision,setRevision]=useState(0);
  const [sidebar,setSidebar]=useState(false),[query,setQuery]=useState(""),[branchQuery,setBranchQuery]=useState("");
  const [closed,setClosed]=useState<ReadonlySet<string>>(new Set()),[menu,setMenu]=useState<Menu|null>(null);
  const [scroller,setScroller]=useState<HTMLElement|null>(null),[selected,setSelected]=useState(""),[notice,setNotice]=useState("");

  async function refresh(){
    if(!directory)return;const token=++epoch.current;setLoading(true);setError("");
    try{
      const value=await api?.getWorkbenchReviewFiles?.(directory,options);
      if(token!==epoch.current)return;
      if(!value?.ok){setError(reviewError(value?.error || "Review reader unavailable. Restart the application."));setFiles([]);return;}
      setRoot(value.root || directory);if(value.branch!==undefined)setCurrentBranch(value.branch);setFiles(value.files || []);setFilesTruncated(!!value.truncated);setRevision(v=>v+1);
    }catch(error){if(token===epoch.current){setError(String(error));setFiles([]);}}
    finally{if(token===epoch.current)setLoading(false);}
  }
  async function loadHistory(more=false){
    if(!directory)return;const token=++historyEpoch.current;setHistoryLoading(true);setHistoryError("");
    try{
      const value=await api?.getWorkbenchGitHistory?.(directory,{ref:more?historyOid:branch,limit:50,offset:more?nextOffset:0});
      if(token!==historyEpoch.current)return;
      if(!value?.ok){setHistoryError(value?.error || "Could not read commit history.");return;}
      setHistoryOid(value.resolvedOid || "");setCommits(prior=>more?[...prior,...value.commits || []]:value.commits || []);
      setNextOffset(value.truncated?value.nextOffset ?? undefined:undefined);
    }catch(error){if(token===historyEpoch.current)setHistoryError(String(error));}
    finally{if(token===historyEpoch.current)setHistoryLoading(false);}
  }
  useEffect(()=>{
    setRoot(directory);setOptions({scope:"uncommitted"});setFiles([]);setFilesTruncated(false);setCurrentBranch("");setQuery("");setBranchQuery("");setBranch("HEAD");setBranches([]);setCommits([]);setClosed(new Set());setMenu(null);setNotice("");setSelected("");setHistoryError("");
    let current=true;
    if(directory){
      void api?.getWorkbenchGitStatus?.(directory).then(value=>{if(current)setCurrentBranch(value.branch || "");}).catch(()=>{});
      void api?.getWorkbenchGitBranches?.(directory,{limit:200}).then(value=>{if(current){setBranches(value.branches || []);if(!value.ok)setHistoryError(value.error || "Could not list branches.");}}).catch(error=>{if(current)setHistoryError(String(error));});
    }
    return()=>{current=false;epoch.current++;historyEpoch.current++;};
  },[directory,api]);
  useEffect(()=>{setClosed(new Set());setFiles([]);setNotice("");void refresh();return()=>{epoch.current++;};},[directory,options,api]);
  useEffect(()=>{setCommits([]);setNextOffset(undefined);void loadHistory();return()=>{historyEpoch.current++;};},[directory,branch,api]);
  const refreshRef=useRef(refresh);refreshRef.current=refresh;
  useEffect(()=>{
    if(!root)return;
    const queue=createRefreshQueue(async()=>{if(ownerDocument.visibilityState==="visible" && panel.current?.getClientRects().length)await refreshRef.current();},error=>setError(String(error)));
    const focused=()=>queue.request();ownerWindow.addEventListener("focus",focused);
    const stop=watchPath(api,root,focused,{scope:"workspace",delay:150,onError:setError}),interval=ownerWindow.setInterval(focused,30000);
    return()=>{queue.dispose();stop();ownerWindow.removeEventListener("focus",focused);ownerWindow.clearInterval(interval);};
  },[root,api,ownerDocument]);
  function disclose(event:MouseEvent<HTMLButtonElement>,kind:Menu["kind"],file?:WorkbenchGitFile){const rect=event.currentTarget.getBoundingClientRect();setMenu({kind,file,x:rect.left,y:rect.bottom+4});}
  function selectCommit(oid:string,range=false){
    const anchor=commits.findIndex(row=>row.oid===commitAnchor.current),end=commits.findIndex(row=>row.oid===oid);
    if(range && anchor>=0 && end>=0){const ids=commits.slice(Math.min(anchor,end),Math.max(anchor,end)+1).map(row=>row.oid);if(ids.length>100){setError("Select up to 100 consecutive commits.");return;}setOptions({scope:"commit",commits:ids});}
    else setOptions(prior=>{const values=new Set(prior.scope==="commit"?prior.commits:[]);values.has(oid)?values.delete(oid):values.add(oid);return values.size?{scope:"commit",commits:[...values]}:{scope:"uncommitted"};});
    commitAnchor.current=oid;
  }
  function navigate(file:string){setSelected(file);setClosed(prior=>{const next=new Set(prior);next.delete(file);return next;});requestAnimationFrame(()=>{[...panel.current?.querySelectorAll<HTMLElement>("[data-review-file]") || []].find(row=>row.dataset.reviewFile===file)?.scrollIntoView?.({block:"start"});});}
  async function mutate(action:string,file:WorkbenchGitFile){
    setMenu(null);if(options.scope==="commit" || mutating.current)return;
    if(action==="revert" && !ownerWindow.confirm(`Discard unstaged changes in ${file.path}? Staged changes will be kept.`))return;
    const token=epoch.current;mutating.current=true;
    try{const value=await api?.runWorkbenchGit?.(action,root,{file:file.path});if(token!==epoch.current)return;if(!value?.ok)setError(String(value?.error || "Git action failed"));else await refresh();}
    catch(error){if(token===epoch.current)setError(String(error));}
    finally{mutating.current=false;}
  }
  async function copy(file:WorkbenchGitFile,patch=false){
    setMenu(null);const token=epoch.current;
    try{const value=patch?await api?.getWorkbenchReviewDiff?.(root,file.path,{...options,context:3}):null;
      if(token!==epoch.current)return;if(patch && !value?.ok)throw new Error(value?.error || "Could not read patch.");
      await ownerWindow.navigator.clipboard.writeText(patch?value?.diff || "":file.path);if(token===epoch.current)setNotice(patch?"Patch copied.":"Path copied.");
    }catch(error){if(token===epoch.current)setError(String(error));}
  }
  const scopeLabel=options.scope==="commit"?`${options.commits?.length || 0} commit${options.commits?.length===1?"":"s"}`:labels[options.scope];
  const branchLabel=branch==="HEAD"?currentBranch || "Current branch":branches.find(item=>item.ref===branch)?.name || branch;
  const visibleFiles=files.filter(file=>file.path.toLowerCase().includes(query.toLowerCase()));
  return <section ref={panel} className="workbench-review">
    <header className="workbench-review__toolbar">
      <button className="workbench-review__scope" aria-haspopup="menu" title="Choose review scope" onClick={event=>disclose(event,"scope")}><Icon name="review"/><span>{scopeLabel}</span><Icon name="down"/></button>
      <button className="workbench-review__branch" aria-haspopup="menu" title="Browse branch history (does not switch the working branch)" onClick={event=>disclose(event,"branch")}><span>{options.scope!=="commit" && branch!=="HEAD"?`History · ${branchLabel}`:branchLabel}</span><Icon name="down"/></button>
      <div className="workbench-review__toolbar-actions"><button aria-label="Review options" title="Review options" aria-haspopup="menu" onClick={event=>disclose(event,"options")}>⋯</button><button aria-label="Changed files" title="Changed files" aria-expanded={sidebar} onClick={()=>setSidebar(v=>!v)}><Icon name="panels"/></button></div>
    </header>
    {error?<div role="alert" className="workbench-tool-error">{error}</div>:null}
    {notice?<div role="status" className="workbench-review__notice">{notice}</div>:null}
    {filesTruncated?<div role="status" className="workbench-review__notice">File list limited. Use Git to inspect remaining changes.</div>:null}
    <div className="workbench-review__body">
      <main ref={setScroller} className="workbench-review__diff" aria-label="File diffs">
        {!directory?<div className="workbench-tool-empty">Select a project for this chat to review changes.</div>:loading && !files.length?<div className="workbench-tool-empty" role="status">Loading changes…</div>:!files.length && !error?<div className="workbench-tool-empty">No changes in this scope.</div>:null}
        {files.map((file,index)=><ReviewFile key={`${root}:${options.scope}:${options.commits?.join(",")}:${file.path}`} file={file} root={root} options={options} api={api} revision={revision} scrollParent={scroller} chatId={chatId} eager={index===0} collapsed={closed.has(file.path)} onToggle={()=>setClosed(prior=>{const next=new Set(prior);next.has(file.path)?next.delete(file.path):next.add(file.path);return next;})} onMenu={event=>disclose(event,"file",file)}/>)}
      </main>
      {sidebar?<aside className="workbench-review__files"><header><strong>Changed files</strong><span>{files.length}</span></header><input aria-label="Search changed files" placeholder="Search files…" value={query} onChange={event=>setQuery(event.target.value)}/>
        <VirtualList items={visibleFiles} rowHeight={34} label="Changed files" itemKey={file=>file.path} render={file=><button title={file.path} className={`workbench-review__select${selected===file.path?" is-selected":""}`} onClick={()=>navigate(file.path)}><Icon name="file"/><span>{file.path}</span><ReviewCounts file={file}/></button>}/>
        {!visibleFiles.length?<div className="workbench-tool-empty">{query?"No matching files.":"No changed files."}</div>:null}
      </aside>:null}
    </div>
    {menu?<PopupMenu className="workbench-review__menu workbench-file-menu" x={menu.x} y={menu.y} onClose={()=>setMenu(null)}>
      {menu.kind==="scope"?<>
        {(["uncommitted","staged","unstaged"] as const).map(scope=><button key={scope} role="menuitemradio" aria-checked={options.scope===scope} onClick={()=>{setOptions({scope});setMenu(null);}}>{labels[scope]}{options.scope===scope?<Icon name="check"/>:null}</button>)}
        <hr/><div className="workbench-review__menu-label">Commits · {branchLabel}</div><p className="workbench-review__menu-hint">Select consecutive commits. Shift-click selects a range.</p>
        {historyError?<p role="alert">{historyError}</p>:null}{historyLoading?<p role="status">Loading history…</p>:null}
        <div className="workbench-review__commits">
          {commits.length?<button role="menuitem" onClick={()=>setOptions({scope:"commit",commits:commits.slice(0,100).map(row=>row.oid)})}>Recent commits ({Math.min(100,commits.length)})</button>:null}
          {commits.map(commit=><button key={commit.oid} role="menuitemcheckbox" disabled={options.scope==="commit" && (options.commits?.length || 0)>=100 && !options.commits?.includes(commit.oid)} aria-checked={options.scope==="commit" && !!options.commits?.includes(commit.oid)} title={`${commit.subject} · ${commit.oid.slice(0,8)}`} onClick={event=>selectCommit(commit.oid,event.shiftKey)}><span>{commit.subject}</span><small>{commit.oid.slice(0,7)}</small>{options.scope==="commit" && options.commits?.includes(commit.oid)?<Icon name="check"/>:null}</button>)}
        </div>
        {nextOffset!==undefined?<button disabled={historyLoading} role="menuitem" onClick={()=>void loadHistory(true)}>Load more commits</button>:null}
        {!commits.length && !historyLoading && !historyError?<p>No commits yet.</p>:null}
      </>:null}
      {menu.kind==="branch"?<><div className="workbench-review__menu-label">Browse branch history</div><input aria-label="Search branches" placeholder="Search branches…" value={branchQuery} onChange={event=>setBranchQuery(event.target.value)}/><button role="menuitemradio" aria-checked={branch==="HEAD"} onClick={()=>{setBranch("HEAD");setOptions({scope:"uncommitted"});setMenu(null);}}>Current branch · {currentBranch}</button>
        <div className="workbench-review__commits">{branches.filter(row=>row.name.toLowerCase().includes(branchQuery.toLowerCase())).map(row=><button key={row.ref} role="menuitemradio" aria-checked={branch===row.ref} title={row.ref} onClick={()=>{setBranch(row.ref);setOptions({scope:"uncommitted"});setMenu(null);}}><span>{row.name}</span>{row.remote?<small>Remote</small>:null}</button>)}</div><p className="workbench-review__menu-hint">Browsing history leaves your working branch unchanged. Live scopes always show the current working tree.</p></>:null}
      {menu.kind==="options"?<><button role="menuitem" onClick={()=>{setClosed(new Set());setMenu(null);}}>Expand all files</button><button role="menuitem" onClick={()=>{setClosed(new Set(files.map(file=>file.path)));setMenu(null);}}>Collapse all files</button><hr/><button role="menuitem" onClick={()=>{setMenu(null);void refresh();void loadHistory();}}>Refresh changes</button></>:null}
      {menu.kind==="file" && menu.file?<>
        {options.scope!=="commit"?<button role="menuitem" disabled={reviewDeleted(menu.file)} onClick={()=>{openFilePreview(reviewPath(root,menu.file!.path),undefined,chatId);setMenu(null);}}>Open preview</button>:null}
        <button role="menuitem" onClick={()=>void copy(menu.file!)}>Copy path</button><button role="menuitem" onClick={()=>void copy(menu.file!,true)}>Copy patch</button>
        {options.scope!=="commit"?<><hr/><button role="menuitem" onClick={()=>void mutate("stage",menu.file!)}>Stage file</button><button role="menuitem" disabled={!menu.file.staged} onClick={()=>void mutate("unstage",menu.file!)}>Unstage file</button><button role="menuitem" disabled={menu.file.status[1]===" "} onClick={()=>void mutate("revert",menu.file!)}>Discard unstaged changes…</button></>:null}
      </>:null}
    </PopupMenu>:null}
  </section>;
}
