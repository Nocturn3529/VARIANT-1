import {useEffect,useMemo,useState,type CSSProperties} from "react";
import {VirtualList} from "../ui/VirtualList";
import {Icon} from "../ui/Icon";
import {parseDiff} from "../workbench/diffModel";
import runtimeLib from "../chat/runtimeLib";

/** One diff row; CSS must keep `.workbench-diff__line` at this exact height. */
const ROW_HEIGHT=22;

export function DiffView({text,fullContents=false,truncated=false,binary=false,onOpenLine,language="",scrollParent,context=3,onExpandContext}: {
  text:string;fullContents?:boolean;truncated?:boolean;binary?:boolean;onOpenLine?:(line:number)=>void;
  language?:string;
  scrollParent?:HTMLElement|null;context?:number;onExpandContext?:(context:number)=>void;
}) {
  const model=useMemo(()=>parseDiff(text,fullContents),[text,fullContents]);
  const [closed,setClosed]=useState<ReadonlySet<number>>(new Set());
  useEffect(()=>setClosed(new Set()),[text,fullContents]);
  const hasHunks=model.rows.some(row=>row.type==="hunk");
  const rows=model.rows.filter(row=>(row.type!=="meta" || !hasHunks) && (row.type==="hunk" || row.hunk===undefined || !closed.has(row.hunk)));
  const omitted=new Map<number,number>();let priorEnd=0;
  for(const row of model.rows){
    if(row.type==="hunk"){
      const match=/^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(row.text);
      if(match){const start=Number(match[1]);omitted.set(row.id,Math.max(0,start-priorEnd-1));priorEnd=start+Number(match[2]??1)-1;}
    }
  }
  // Gutters are only as wide as the largest line number in this patch.
  const digits=useMemo(()=>Math.max(2,String(model.rows.reduce((max,row)=>Math.max(max,row.oldLine || 0,row.newLine || 0),0)).length),[model]);
  return <div className="workbench-diff" style={{"--diff-digits":digits} as CSSProperties}>
    {binary ? <p className="workbench-diff__notice" role="status">Binary file changed. Git cannot show a textual patch.</p> : null}
    {truncated || model.truncated ? <p className="workbench-diff__notice" role="status">Diff preview truncated. Use Git to inspect the complete patch.</p> : null}
    {!rows.length ? <p className="workbench-diff__notice">{text || "No textual diff."}</p> : <>
      <VirtualList items={rows} rowHeight={ROW_HEIGHT} scrollParent={scrollParent} label="Diff lines" itemKey={row=>String(row.id)} render={row=>{
        if(row.type==="hunk")return <div className="workbench-diff__context">
          <button className="workbench-diff__hunk" title={row.text} aria-expanded={!closed.has(row.id)} onClick={()=>setClosed(prior=>{const next=new Set(prior);next.has(row.id)?next.delete(row.id):next.add(row.id);return next;})}>
            <Icon name="chevron" className={closed.has(row.id)?"":"is-expanded"}/><span>{omitted.get(row.id) ? `${omitted.get(row.id)} unchanged lines` : "Changed lines"}</span>
          </button>
          {onExpandContext && !!omitted.get(row.id) && context<1000 ? <button className="workbench-diff__unfold" title="Expand unchanged context" aria-label="Expand unchanged context" onClick={()=>onExpandContext(Math.min(1000,context+50))}><Icon name="unfold"/></button> : null}
        </div>;
        const line=row.newLine || (row.type==="remove" ? model.rows.find(next=>next.id>row.id && next.hunk===row.hunk && next.newLine)?.newLine : undefined);
        return <div className={`workbench-diff__line is-${row.type}`} role="group" aria-label={row.type==="add"?"Added line":row.type==="remove"?"Removed line":undefined}>
          <span className="workbench-diff__old" aria-label={row.oldLine ? `Old line ${row.oldLine}` : undefined}>{row.oldLine || ""}</span>
          {line && onOpenLine ? <button className="workbench-diff__new" aria-label={`Open current file at line ${line}`} title={`Open current file at line ${line}`} onClick={()=>onOpenLine(line)}>{row.newLine || <Icon name="forward" className="workbench-diff__jump"/>}</button> : <span className="workbench-diff__new">{row.newLine || ""}</span>}
          <span className="workbench-diff__sign" aria-hidden="true">{row.type==="add"?"+":row.type==="remove"?"−":" "}</span><code>{language && row.text.length<=4096 ? runtimeLib.highlightCode(row.text,language).map((token,index)=><span key={index} className={`token-${token.type}`}>{token.text}</span>) : row.text}</code>
        </div>;
      }}/>
      {onExpandContext && !binary && !fullContents ? <button className="workbench-diff__expand" disabled={context>=1000} onClick={()=>onExpandContext(Math.min(1000,context+50))}><Icon name="unfold"/>{context>=1000 ? "Context limit reached (1,000 lines per hunk)" : "Show more context"}</button> : null}
    </>}
  </div>;
}
