import {useEffect,useMemo,useState} from "react";
import {VirtualList} from "../ui/VirtualList";
import {parseDiff} from "../workbench/diffModel";

export function DiffView({text,fullContents=false,truncated=false,binary=false,onOpenLine}: {
  text:string;fullContents?:boolean;truncated?:boolean;binary?:boolean;onOpenLine?:(line:number)=>void;
}) {
  const model=useMemo(()=>parseDiff(text,fullContents),[text,fullContents]);
  const [closed,setClosed]=useState<ReadonlySet<number>>(new Set());
  useEffect(()=>setClosed(new Set()),[text,fullContents]);
  const rows=model.rows.filter(row=>row.type==="hunk" || row.hunk===undefined || !closed.has(row.hunk));
  return <div className="workbench-diff">
    {binary ? <p className="workbench-diff__notice" role="status">Binary file changed. Git cannot show a textual patch.</p> : null}
    {truncated || model.truncated ? <p className="workbench-diff__notice" role="status">Diff preview truncated. Use Git to inspect the complete patch.</p> : null}
    {!rows.length ? <p className="workbench-diff__notice">{text || "No textual diff."}</p> : <>
      <div className="workbench-diff__legend">Old · New <span>{truncated || model.truncated ? "Visible changes" : "Changes"}: +{model.added} −{model.removed}</span></div>
      <VirtualList items={rows} rowHeight={22} label="Diff lines" itemKey={row=>String(row.id)} render={row=>{
        if(row.type==="hunk")return <button className="workbench-diff__hunk" aria-expanded={!closed.has(row.id)} onClick={()=>setClosed(prior=>{const next=new Set(prior);next.has(row.id)?next.delete(row.id):next.add(row.id);return next;})}>{closed.has(row.id)?"▸":"▾"} {row.text}</button>;
        const line=row.newLine || (row.type==="remove" ? model.rows.find(next=>next.id>row.id && next.hunk===row.hunk && next.newLine)?.newLine : undefined);
        return <div className={`workbench-diff__line is-${row.type}`} role="group" aria-label={row.type==="add"?"Added line":row.type==="remove"?"Removed line":undefined}>
          <span className="workbench-diff__old" aria-label={row.oldLine ? `Old line ${row.oldLine}` : undefined}>{row.oldLine || ""}</span>
          {line && onOpenLine ? <button className="workbench-diff__new" aria-label={`Open current file at line ${line}`} onClick={()=>onOpenLine(line)}>{row.newLine || "↗"}</button> : <span className="workbench-diff__new">{row.newLine || ""}</span>}
          <span className="workbench-diff__sign" aria-hidden="true">{row.type==="add"?"+":row.type==="remove"?"−":" "}</span><code>{row.text}</code>
        </div>;
      }}/>
    </>}
  </div>;
}
