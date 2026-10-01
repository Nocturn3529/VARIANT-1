import {useEffect,useMemo,useState} from "react";
import {VirtualList} from "../ui/VirtualList";
import {parseDiff} from "../workbench/diffModel";
import runtimeLib from "../chat/runtimeLib";

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
  return <div className="workbench-diff">
    {binary ? <p className="workbench-diff__notice" role="status">Binary file changed. Git cannot show a textual patch.</p> : null}
    {truncated || model.truncated ? <p className="workbench-diff__notice" role="status">Diff preview truncated. Use Git to inspect the complete patch.</p> : null}
    {!rows.length ? <p className="workbench-diff__notice">{text || "No textual diff."}</p> : <>
      <VirtualList items={rows} rowHeight={24} scrollParent={scrollParent} label="Diff lines" itemKey={row=>String(row.id)} render={row=>{
        if(row.type==="hunk")return <div className="workbench-diff__context">
          <button className="workbench-diff__hunk" title={row.text} aria-expanded={!closed.has(row.id)} onClick={()=>setClosed(prior=>{const next=new Set(prior);next.has(row.id)?next.delete(row.id):next.add(row.id);return next;})}>{closed.has(row.id)?"▸":"▾"} {omitted.get(row.id) ? `${omitted.get(row.id)} unchanged lines` : "Changed lines"}</button>
          {onExpandContext && !!omitted.get(row.id) && context<1000 ? <button title="Expand unchanged context" aria-label="Expand unchanged context" onClick={()=>onExpandContext(Math.min(1000,context+50))}>↕</button> : null}
        </div>;
        const line=row.newLine || (row.type==="remove" ? model.rows.find(next=>next.id>row.id && next.hunk===row.hunk && next.newLine)?.newLine : undefined);
        return <div className={`workbench-diff__line is-${row.type}`} role="group" aria-label={row.type==="add"?"Added line":row.type==="remove"?"Removed line":undefined}>
          <span className="workbench-diff__old" aria-label={row.oldLine ? `Old line ${row.oldLine}` : undefined}>{row.oldLine || ""}</span>
          {line && onOpenLine ? <button className="workbench-diff__new" aria-label={`Open current file at line ${line}`} onClick={()=>onOpenLine(line)}>{row.newLine || "↗"}</button> : <span className="workbench-diff__new">{row.newLine || ""}</span>}
          <span className="workbench-diff__sign" aria-hidden="true">{row.type==="add"?"+":row.type==="remove"?"−":" "}</span><code>{language && row.text.length<=4096 ? runtimeLib.highlightCode(row.text,language).map((token,index)=><span key={index} className={`token-${token.type}`}>{token.text}</span>) : row.text}</code>
        </div>;
      }}/>
      {onExpandContext && !binary && !fullContents ? <button className="workbench-diff__expand" disabled={context>=1000} onClick={()=>onExpandContext(Math.min(1000,context+50))}>{context>=1000 ? "Context limit reached (1,000 lines per hunk)" : "Expand unchanged context"}</button> : null}
    </>}
  </div>;
}
