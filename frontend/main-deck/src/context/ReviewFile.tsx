import {useEffect,useRef,useState,type MouseEvent} from "react";
import {Icon,type IconName} from "../ui/Icon";
import {DiffView} from "./DiffView";
import {reviewPath,reviewDeleted} from "../workbench/diffModel";
import {openFilePreview} from "../workbench/previewStore";
import type {WorkbenchGitFile,WorkbenchReviewOptions,WorkbenchReviewDiff,RuntimeApi} from "../types";

const IMAGE=/.(png|jpe?g|gif|webp|svg|ico|bmp)$/i,DOCUMENT=/.(md|mdx|txt|rst|pdf|docx?)$/i;
/** File-type glyph for a changed path: images, prose, or code. */
export function reviewFileIcon(path:string):IconName{return IMAGE.test(path)?"image":DOCUMENT.test(path)?"file":"code";}
/** Emphasize the file name; its folder stays quieter and truncates first. */
export function ReviewPath({path}:{path:string}){
  const cut=path.lastIndexOf("/");
  return <span className="workbench-review__path">{cut>=0?<span className="workbench-review__dir">{path.slice(0,cut+1)}</span>:null}<span className="workbench-review__name">{path.slice(cut+1)}</span></span>;
}

export function ReviewCounts({file}:{file:WorkbenchGitFile}){
  return <small>{file.binary?"Binary":file.added==null || file.removed==null ? file.untracked?"New":"Counts unavailable":<><span className="is-added">+{file.added}</span> <span className="is-removed">−{file.removed}</span></>}</small>;
}

/** Retain only nearby patches; offscreen cards keep their measured scroll geometry. */
export function ReviewFile({file,root,options,api,scrollParent,revision,collapsed,onToggle,onMenu,chatId,eager}: {
  file:WorkbenchGitFile;root:string;options:WorkbenchReviewOptions;api:RuntimeApi|undefined;scrollParent:HTMLElement|null;
  revision:number;collapsed:boolean;onToggle:()=>void;onMenu:(event:MouseEvent<HTMLButtonElement>)=>void;chatId:string;eager:boolean;
}) {
  const card=useRef<HTMLElement>(null),body=useRef<HTMLDivElement>(null);
  const [visible,setVisible]=useState(eager),[height,setHeight]=useState(120),[context,setContext]=useState(3);
  const [result,setResult]=useState<WorkbenchReviewDiff|null>(null),[loading,setLoading]=useState(false),[retry,setRetry]=useState(0);
  useEffect(()=>{
    const Observer=card.current?.ownerDocument.defaultView?.IntersectionObserver;
    if(!Observer){setVisible(true);return;}
    const observer=new Observer(entries=>setVisible(entries[0].isIntersecting),{root:scrollParent,rootMargin:"500px"});
    observer.observe(card.current!);return()=>observer.disconnect();
  },[scrollParent]);
  useEffect(()=>{
    let current=true;
    if(!visible || collapsed){setResult(null);return;}
    setLoading(true);
    const read=api?.getWorkbenchReviewDiff?.(root,file.path,{...options,context});
    if(!read){setResult({ok:false,error:"Review reader unavailable. Restart the application."});setLoading(false);return;}
    void read.then(value=>{if(current){setResult(value);setLoading(false);}},error=>{if(current){setResult({ok:false,error:String(error)});setLoading(false);}});
    return()=>{current=false;};
  },[visible,collapsed,root,file.path,options,api,revision,context,retry]);
  useEffect(()=>{
    if(!body.current || !result || collapsed)return;
    const measure=()=>{const next=body.current?.getBoundingClientRect().height || 120;setHeight(prior=>prior===next?prior:next);};
    const observer=new ResizeObserver(measure);observer.observe(body.current);measure();return()=>observer.disconnect();
  },[result,collapsed]);
  const patches=result?.sections?.length?result.sections:result?[{...result,oid:""}]:[];
  return <article ref={card} className="workbench-review__card" data-review-file={file.path}>
    <header><button className="workbench-review__card-title" aria-expanded={!collapsed} onClick={onToggle} title={file.originalPath?`${file.originalPath} → ${file.path}`:file.path}><Icon name="chevron" className={collapsed?"":"is-expanded"}/><Icon name={reviewFileIcon(file.path)}/><ReviewPath path={file.path}/><ReviewCounts file={file}/></button><button title={`Actions for ${file.path}`} aria-label={`Actions for ${file.path}`} aria-haspopup="menu" onClick={onMenu}><Icon name="more"/></button></header>
    {!collapsed?<div ref={body} className="workbench-review__card-body" style={!result?{minHeight:height}:undefined}>
      {result?.sections?.length && result.truncated?<p className="workbench-diff__notice" role="status">Selected commit previews are limited.{result.omittedSections?` ${result.omittedSections} commit patch sections omitted.`:""} Select fewer commits to inspect the remaining changes.</p>:null}
      {result?.ok===false?<div className="workbench-tool-empty" role="alert">{result.error}<button onClick={()=>setRetry(v=>v+1)}>Retry</button></div>:result?patches.map(patch=><section key={patch.oid || "live"}>
        {patches.length>1?<header className="workbench-review__commit-section" title={patch.oid}>Commit {patch.oid.slice(0,8)}</header>:null}
        <DiffView text={patch.diff || ""} language={file.path.split(".").pop() || ""} fullContents={patch.fullContents} binary={patch.binary} truncated={patch.truncated} scrollParent={scrollParent} context={context} onExpandContext={setContext} onOpenLine={["uncommitted","unstaged"].includes(options.scope) && !reviewDeleted(file)?line=>openFilePreview(reviewPath(root,file.path),undefined,chatId,line):undefined}/>
      </section>):<div className="workbench-tool-empty" role={visible?"status":undefined}>{visible?"Loading patch…":""}</div>}
      {loading && result?<span className="workbench-review__loading" role="status">Loading context…</span>:null}
    </div>:null}
  </article>;
}
