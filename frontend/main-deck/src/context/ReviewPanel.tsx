import {useEffect,useMemo,useRef,useState,type MouseEvent} from "react";
import {Icon} from "../ui/Icon";
import {PopupMenu} from "../ui/PopupMenu";
import {VirtualList} from "../ui/VirtualList";
import {useSurfaceDocument} from "../ui/SurfaceDocument";
import {createRefreshQueue} from "../workbench/refreshQueue";
import {watchPath} from "../workbench/watchPath";
import {openFilePreview} from "../workbench/previewStore";
import {reviewPath,reviewDeleted} from "../workbench/diffModel";
import {ReviewFile,ReviewCounts,reviewFileIcon} from "./ReviewFile";
import {ReviewSidebar} from "./ReviewSidebar";
import {reviewTree} from "../workbench/reviewTree";
import type {WorkbenchGitFile,WorkbenchGitBranch,WorkbenchGitCommit,WorkbenchReviewOptions,WorkbenchReviewAggregate} from "../types";

type Menu={kind:"scope"|"branch"|"options"|"file";x:number;y:number;file?:WorkbenchGitFile};
const labels={uncommitted:"Uncommitted",staged:"Staged",unstaged:"Unstaged",commit:"Selected commits",branch:"All commits"};
const reviewError=(value:unknown)=>({invalid_commit_selection:"Select between 1 and 100 commits.",comparison_base_unavailable_select_explicit_base:"Choose a comparison base in the branch menu.",ambiguous_default_base_select_explicit_base:"Multiple default branches are available. Choose a comparison base in the branch menu.",review_status_changed_retry:"Files changed while reading Git status. Refresh changes to retry."}[String(value)] || String(value || "Unable to read changes."));
function Counts({added,removed,complete=true}:{added:number;removed:number;complete?:boolean}){return <small title={complete?"Added and removed lines":"Partial counts"}>{!complete?"≥":""}<span className="is-added">+{added}</span> <span className="is-removed">−{removed}</span></small>;}

export function ReviewPanel({directory="",chatId=""}:{directory?:string;chatId?:string}) {
  const ownerDocument=useSurfaceDocument(),ownerWindow=ownerDocument.defaultView || window;
  const api=window.variant1Deck,panel=useRef<HTMLElement>(null);
  const epoch=useRef(0),historyEpoch=useRef(0);
  const catalogEpoch=useRef(0),totalsEpoch=useRef(0);
  const commitAnchor=useRef(""),mutating=useRef(false);
  const [root,setRoot]=useState(directory),[currentBranch,setCurrentBranch]=useState("");
  const [branches,setBranches]=useState<WorkbenchGitBranch[]>([]),[branch,setBranch]=useState("HEAD");
  const [commits,setCommits]=useState<WorkbenchGitCommit[]>([]),[historyOid,setHistoryOid]=useState("");
  const [nextOffset,setNextOffset]=useState<number|undefined>(),[historyError,setHistoryError]=useState("");
  const [historyLoading,setHistoryLoading]=useState(false),[options,setOptions]=useState<WorkbenchReviewOptions>({scope:"uncommitted"});
  const [allBranches,setAllBranches]=useState(false),[baseRef,setBaseRef]=useState(""),[defaultBase,setDefaultBase]=useState("");
  const [historyOids,setHistoryOids]=useState<string[]>([]),[excludeOid,setExcludeOid]=useState<string|undefined>();
  const [aggregate,setAggregate]=useState<WorkbenchReviewAggregate|null>(null),[branchAggregate,setBranchAggregate]=useState<WorkbenchReviewAggregate|null>(null);
  const [comparisonBase,setComparisonBase]=useState("");
  const [files,setFiles]=useState<WorkbenchGitFile[]>([]),[filesTruncated,setFilesTruncated]=useState(false);
  const [error,setError]=useState(""),[loading,setLoading]=useState(false),[revision,setRevision]=useState(0);
  const [sidebar,setSidebar]=useState(false),[query,setQuery]=useState(""),[branchQuery,setBranchQuery]=useState("");
  const [closed,setClosed]=useState<ReadonlySet<string>>(new Set()),[menu,setMenu]=useState<Menu|null>(null);
  const [scroller,setScroller]=useState<HTMLElement|null>(null),[selected,setSelected]=useState(""),[notice,setNotice]=useState("");
  const [closedFolders,setClosedFolders]=useState<ReadonlySet<string>>(new Set());
  const [sidebarWidth,setSidebarWidth]=useState<number|null>(null);
  async function loadBranches(){
    const token=++catalogEpoch.current;if(!directory)return;
    try{const value=await api?.getWorkbenchGitBranches?.(directory,{limit:200});if(token!==catalogEpoch.current)return;
      setBranches(value?.branches || []);setDefaultBase(value?.defaultBaseRef || "");
      const current=value?.branches?.find(row=>row.current);if(current)setCurrentBranch(current.name);
      if(!value?.ok)setHistoryError(reviewError(value?.error || "Could not list branches."));
    }catch(error){if(token===catalogEpoch.current)setHistoryError(String(error));}
  }
  async function loadBranchAggregate(){
    const token=++totalsEpoch.current;if(!directory)return;
    try{const value=await api?.getWorkbenchReviewFiles?.(directory,{scope:"branch",ref:branch,baseRef:baseRef || undefined});if(token===totalsEpoch.current)setBranchAggregate(value?.ok?value.aggregate || null:null);}
    catch{if(token===totalsEpoch.current)setBranchAggregate(null);}
  }

  async function refresh(){
    const token=++epoch.current;setAggregate(null);
    if(!directory || options.scope==="commit" && !options.commits?.length){setFiles([]);setError("");setLoading(false);return;}
    setLoading(true);setError("");
    try{
      const value=await api?.getWorkbenchReviewFiles?.(directory,options);
      if(token!==epoch.current)return;
      if(!value?.ok){setError(reviewError(value?.error || "Review reader unavailable. Restart the application."));setFiles([]);return;}
      setRoot(value.root || directory);if(value.branch!==undefined)setCurrentBranch(value.branch);setFiles(value.files || []);setFilesTruncated(!!value.truncated);setRevision(v=>v+1);
      setAggregate(value.aggregate || null);setComparisonBase(value.comparison?.baseRef || "");if(options.scope==="branch")setBranchAggregate(value.aggregate || null);
    }catch(error){if(token===epoch.current){setError(String(error));setFiles([]);}}
    finally{if(token===epoch.current)setLoading(false);}
  }
  async function loadHistory(more=false){
    if(!directory)return;const token=++historyEpoch.current;setHistoryLoading(true);setHistoryError("");
    try{
      const value=await api?.getWorkbenchGitHistory?.(directory,{ref:more?historyOid || branch:branch,baseRef:baseRef || undefined,allBranches,historyOids:more?historyOids:undefined,excludeOid:more?excludeOid:undefined,limit:50,offset:more?nextOffset:0});
      if(token!==historyEpoch.current)return;
      if(!value?.ok){setHistoryError(reviewError(value?.error || "Could not read commit history."));return;}
      setHistoryOid(value.resolvedOid || "");setCommits(prior=>more?[...prior,...value.commits || []]:value.commits || []);
      setHistoryOids(value.historyOids || []);setExcludeOid(value.excludeOid);
      setNextOffset(value.truncated?value.nextOffset ?? undefined:undefined);
    }catch(error){if(token===historyEpoch.current)setHistoryError(String(error));}
    finally{if(token===historyEpoch.current)setHistoryLoading(false);}
  }
  useEffect(()=>{
    setRoot(directory);setOptions({scope:"uncommitted"});setFiles([]);setFilesTruncated(false);setCurrentBranch("");setQuery("");setBranchQuery("");setBranch("HEAD");setBranches([]);setCommits([]);setClosed(new Set());setMenu(null);setNotice("");setSelected("");setHistoryError("");
    setAllBranches(false);setBaseRef("");setDefaultBase("");setComparisonBase("");setClosedFolders(new Set());setBranchAggregate(null);
    let current=true;
    if(directory){
      void api?.getWorkbenchGitStatus?.(directory).then(value=>{if(current)setCurrentBranch(value.branch || "");}).catch(()=>{});
      void loadBranches();
    }
    return()=>{current=false;epoch.current++;historyEpoch.current++;catalogEpoch.current++;};
  },[directory,api]);
  useEffect(()=>{setClosed(new Set());setFiles([]);setNotice("");void refresh();return()=>{epoch.current++;};},[directory,options,api]);
  useEffect(()=>{setCommits([]);setNextOffset(undefined);commitAnchor.current="";void loadHistory();return()=>{historyEpoch.current++;};},[directory,branch,baseRef,allBranches,api]);
  useEffect(()=>{
    setBranchAggregate(null);void loadBranchAggregate();return()=>{totalsEpoch.current++;};
  },[directory,branch,baseRef,api]);
  function chooseBranch(ref:string){setBranch(ref);setAllBranches(false);setOptions({scope:"branch",ref,baseRef:baseRef || undefined});setMenu(null);}
  const refreshRef=useRef(refresh);refreshRef.current=refresh;
  useEffect(()=>{
    if(!root)return;
    const queue=createRefreshQueue(async()=>{if(ownerDocument.visibilityState==="visible" && panel.current?.getClientRects().length)await refreshRef.current();},error=>setError(String(error)));
    const focused=()=>queue.request();ownerWindow.addEventListener("focus",focused);
    const stop=watchPath(api,root,focused,{scope:"workspace",delay:150,onError:setError}),interval=ownerWindow.setInterval(focused,30000);
    return()=>{queue.dispose();stop();ownerWindow.removeEventListener("focus",focused);ownerWindow.clearInterval(interval);};
  },[root,api,ownerDocument]);
  function disclose(event:MouseEvent<HTMLButtonElement>,kind:Menu["kind"],file?:WorkbenchGitFile){const rect=event.currentTarget.getBoundingClientRect();setMenu({kind,file,x:rect.left,y:rect.bottom+4});if(kind==="branch")void loadBranches();if(kind==="scope"){void loadHistory();void loadBranchAggregate();}}
  function selectCommit(oid:string,range=false){
    const anchor=commits.findIndex(row=>row.oid===commitAnchor.current),end=commits.findIndex(row=>row.oid===oid);
    if(range && anchor>=0 && end>=0){const ids=commits.slice(Math.min(anchor,end),Math.max(anchor,end)+1).map(row=>row.oid);if(ids.length>100){setError("Select up to 100 commits.");return;}setOptions({scope:"commit",commits:ids});}
    else setOptions(prior=>{const values=new Set(prior.scope==="commit"?prior.commits:[]);values.has(oid)?values.delete(oid):values.add(oid);return {scope:"commit",commits:[...values]};});
    commitAnchor.current=oid;
  }
  function navigate(file:string){setSelected(file);setClosed(prior=>{const next=new Set(prior);next.delete(file);return next;});requestAnimationFrame(()=>{[...panel.current?.querySelectorAll<HTMLElement>("[data-review-file]") || []].find(row=>row.dataset.reviewFile===file)?.scrollIntoView?.({block:"start"});});}
  async function mutate(action:string,file:WorkbenchGitFile){
    setMenu(null);if(["commit","branch"].includes(options.scope) || mutating.current)return;
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
      await ownerWindow.navigator.clipboard.writeText(patch?value?.sections?.length?value.sections.map(section=>`Commit ${section.oid}\n${section.diff}`).join("\n"):value?.diff || "":file.path);if(token===epoch.current)setNotice(patch?"Patch copied.":"Path copied.");
    }catch(error){if(token===epoch.current)setError(String(error));}
  }
  const scopeLabel=options.scope==="commit"?options.commits?.length?`${options.commits.length} commit${options.commits.length===1?"":"s"}`:"Select commits":labels[options.scope];
  const branchLabel=branch==="HEAD"?currentBranch || "Current branch":branches.find(item=>item.ref===branch)?.name || branch;
  const treeRows=useMemo(()=>reviewTree(files,closedFolders,query),[files,closedFolders,query]);
  return <section ref={panel} className="workbench-review">
    <header className="workbench-review__toolbar">
      <button className="workbench-review__scope" aria-haspopup="menu" title={options.scope==="commit"?"Counts sum the selected commits' edits":"Choose review scope"} onClick={event=>disclose(event,"scope")}><Icon name="review"/><span>{scopeLabel}</span>{aggregate?<Counts {...aggregate}/>:null}<Icon name="down"/></button>
      <button className="workbench-review__branch" aria-haspopup="menu" title={comparisonBase?`Compared against ${comparisonBase}; browsing does not switch the working branch`:"Choose branch history"} onClick={event=>disclose(event,"branch")}><span>{allBranches && options.scope==="commit"?"All branches":options.scope!=="commit" && options.scope!=="branch" && branch!=="HEAD"?`History · ${branchLabel}`:branchLabel}</span><Icon name="down"/></button>
      <div className="workbench-review__toolbar-actions"><button aria-label="Review options" title="Review options" aria-haspopup="menu" onClick={event=>disclose(event,"options")}><Icon name="more"/></button><button aria-label="Changed files" title="Changed files" aria-expanded={sidebar} onClick={()=>setSidebar(v=>!v)}><Icon name="panels"/></button></div>
    </header>
    {error?<div role="alert" className="workbench-tool-error">{error}</div>:null}
    {notice?<div role="status" className="workbench-review__notice">{notice}</div>:null}
    {filesTruncated?<div role="status" className="workbench-review__notice">File list limited. Use Git to inspect remaining changes.</div>:null}
    <div className="workbench-review__body">
      <main ref={setScroller} className="workbench-review__diff" aria-label="File diffs">
        {!directory?<div className="workbench-tool-empty">Select a project for this chat to review changes.</div>:loading && !files.length?<div className="workbench-tool-empty" role="status">Loading changes…</div>:!files.length && !error?<div className="workbench-tool-empty">{options.scope==="commit" && !options.commits?.length?"Select commits to review their changes.":"No changes in this scope."}</div>:null}
        {files.map((file,index)=><ReviewFile key={`${root}:${options.scope}:${options.ref}:${options.baseRef}:${options.commits?.join(",")}:${file.path}`} file={file} root={root} options={options} api={api} revision={revision} scrollParent={scroller} chatId={chatId} eager={index===0} collapsed={closed.has(file.path)} onToggle={()=>setClosed(prior=>{const next=new Set(prior);next.has(file.path)?next.delete(file.path):next.add(file.path);return next;})} onMenu={event=>disclose(event,"file",file)}/>)}
      </main>
      {sidebar?<ReviewSidebar width={sidebarWidth} onResize={setSidebarWidth}><header><strong>Changed files</strong><span>{files.length}</span></header><input aria-label="Search changed files" placeholder="Search files…" value={query} onChange={event=>setQuery(event.target.value)}/>
        <VirtualList items={treeRows} rowHeight={28} label="Changed files" itemKey={row=>row.kind==="folder"?`folder:${row.path}`:row.file.path} render={row=>row.kind==="folder"?<button className="workbench-review__select workbench-review__folder" style={{paddingLeft:9+row.depth*12}} title={row.path} aria-expanded={!!query || !closedFolders.has(row.path)} onClick={()=>setClosedFolders(prior=>{const next=new Set(prior);next.has(row.path)?next.delete(row.path):next.add(row.path);return next;})}><Icon name={query || !closedFolders.has(row.path)?"down":"chevron"}/><Icon name="folder"/><span>{row.name}</span><small>{row.count}</small></button>:<button title={row.file.path} style={{paddingLeft:21+row.depth*12}} className={`workbench-review__select${selected===row.file.path?" is-selected":""}`} onClick={()=>navigate(row.file.path)}><Icon name={reviewFileIcon(row.file.path)}/><span>{row.name}</span><ReviewCounts file={row.file}/></button>}/>
        {!treeRows.length?<div className="workbench-tool-empty">{query?"No matching files.":"No changed files."}</div>:null}
      </ReviewSidebar>:null}
    </div>
    {menu?<PopupMenu className="workbench-review__menu workbench-file-menu" x={menu.x} y={menu.y} onClose={()=>setMenu(null)}>
      {menu.kind==="scope"?<>
        {(["uncommitted","staged","unstaged"] as const).map(scope=><button key={scope} role="menuitemradio" aria-checked={options.scope===scope} onClick={()=>{setOptions({scope});setMenu(null);}}>{labels[scope]}{options.scope===scope?<Icon name="check"/>:null}</button>)}
        <hr/><div className="workbench-review__menu-label">Commits · {allBranches?"All branches":branchLabel}</div><p className="workbench-review__menu-hint">Select commits individually. Shift-click selects a range.</p>
        {historyError?<p role="alert">{historyError}</p>:null}{historyLoading?<p role="status">Loading history…</p>:null}
        <div className="workbench-review__commits">
          {!allBranches?<button role="menuitemcheckbox" aria-checked={options.scope==="branch"} onClick={()=>{setOptions({scope:"branch",ref:branch,baseRef:baseRef || undefined});setMenu(null);}}><span>All commits</span>{branchAggregate?<Counts {...branchAggregate}/>:null}{options.scope==="branch"?<Icon name="check"/>:null}</button>:null}
          {commits.map(commit=><button key={commit.oid} role="menuitemcheckbox" data-commit-oid={commit.oid} disabled={options.scope==="commit" && (options.commits?.length || 0)>=100 && !options.commits?.includes(commit.oid)} aria-checked={options.scope==="commit" && !!options.commits?.includes(commit.oid)} title={`${commit.subject} · ${commit.oid}`} onClick={event=>selectCommit(commit.oid,event.shiftKey)}><span>{commit.subject}</span>{typeof commit.added==="number" && typeof commit.removed==="number"?<Counts added={commit.added} removed={commit.removed} complete={commit.statsComplete!==false}/>:commit.statsComplete===false?<small>Counts unavailable</small>:null}{options.scope==="commit" && options.commits?.includes(commit.oid)?<Icon name="check"/>:null}</button>)}
        </div>
        {nextOffset!==undefined?<button disabled={historyLoading} role="menuitem" onClick={()=>void loadHistory(true)}>Load more commits</button>:null}
        {!commits.length && !historyLoading && !historyError?<p>No commits yet.</p>:null}
      </>:null}
      {menu.kind==="branch"?<><div className="workbench-review__menu-label">Browse branch history</div><input aria-label="Search branches" placeholder="Search branches…" value={branchQuery} onChange={event=>setBranchQuery(event.target.value)}/>
        <button role="menuitemradio" aria-checked={allBranches} onClick={()=>{setAllBranches(true);setOptions({scope:"commit",commits:[]});setMenu(prior=>prior?{...prior,kind:"scope"}:null);}}>All branches</button>
        <button role="menuitemradio" aria-checked={!allBranches && branch==="HEAD"} onClick={()=>chooseBranch("HEAD")}>Current branch · {currentBranch}</button>
        <div className="workbench-review__commits">{branches.filter(row=>row.name.toLowerCase().includes(branchQuery.toLowerCase())).map(row=><button key={row.ref} role="menuitemradio" aria-checked={!allBranches && branch===row.ref} title={row.ref} onClick={()=>chooseBranch(row.ref)}><span>{row.name}</span>{row.remote?<small>Remote</small>:null}</button>)}</div>
        <label className="workbench-review__menu-label">Comparison base<select aria-label="Comparison base" value={baseRef} onChange={event=>{const value=event.target.value;setBaseRef(value);if(options.scope==="branch")setOptions({scope:"branch",ref:branch,baseRef:value || undefined});}}><option value="">{defaultBase?`Default · ${branches.find(row=>row.ref===defaultBase)?.name || defaultBase}`:"Choose a base branch"}</option>{branches.map(row=><option key={row.ref} value={row.ref}>{row.name}</option>)}</select></label><p className="workbench-review__menu-hint">All commits compares a branch with its merge base. Browsing leaves your working branch unchanged.</p></>:null}
      {menu.kind==="options"?<><button role="menuitem" onClick={()=>{setClosed(new Set());setMenu(null);}}>Expand all files</button><button role="menuitem" onClick={()=>{setClosed(new Set(files.map(file=>file.path)));setMenu(null);}}>Collapse all files</button><hr/><button role="menuitem" onClick={()=>{setMenu(null);void refresh();void loadHistory();void loadBranches();void loadBranchAggregate();}}>Refresh changes</button></>:null}
      {menu.kind==="file" && menu.file?<>
        {!["commit","branch"].includes(options.scope)?<button role="menuitem" disabled={reviewDeleted(menu.file)} onClick={()=>{openFilePreview(reviewPath(root,menu.file!.path),undefined,chatId);setMenu(null);}}>Open preview</button>:null}
        <button role="menuitem" onClick={()=>void copy(menu.file!)}>Copy path</button><button role="menuitem" onClick={()=>void copy(menu.file!,true)}>Copy patch</button>
        {!["commit","branch"].includes(options.scope)?<><hr/><button role="menuitem" onClick={()=>void mutate("stage",menu.file!)}>Stage file</button><button role="menuitem" disabled={!menu.file.staged} onClick={()=>void mutate("unstage",menu.file!)}>Unstage file</button><button role="menuitem" disabled={menu.file.status[1]===" "} onClick={()=>void mutate("revert",menu.file!)}>Discard unstaged changes…</button></>:null}
      </>:null}
    </PopupMenu>:null}
  </section>;
}
