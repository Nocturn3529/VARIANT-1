import {useLayoutEffect,useRef,useState,type ReactNode} from "react";

/** The file tree can grow, but always leaves room for the diff at narrow widths. */
export function ReviewSidebar({width,onResize,children}:{width:number|null;onResize:(width:number|null)=>void;children:ReactNode}){
  const host=useRef<HTMLElement>(null),drag=useRef<{x:number;width:number}|null>(null);
  const [available,setAvailable]=useState(900);
  useLayoutEffect(()=>{
    const body=host.current!.parentElement!;
    const measure=()=>{if(body.clientWidth)setAvailable(body.clientWidth);};
    const observer=new ResizeObserver(measure);observer.observe(body);measure();return()=>observer.disconnect();
  },[]);
  const maximum=Math.max(0,Math.min(480,Math.floor(available*.6)));
  const minimum=Math.min(160,Math.floor(available*.4),maximum);
  const clamp=(value:number)=>Math.round(Math.max(minimum,Math.min(maximum,value)));
  const actual=clamp(width ?? Math.max(160,Math.min(270,available*.32)));
  return <>
    <div className="workbench-review__resize" role="separator" aria-label="Resize changed files" aria-orientation="vertical" aria-valuemin={minimum} aria-valuemax={maximum} aria-valuenow={actual} tabIndex={0}
      onDoubleClick={()=>onResize(null)}
      onPointerDown={event=>{if(event.button!==0)return;event.preventDefault();drag.current={x:event.clientX,width:actual};event.currentTarget.setPointerCapture(event.pointerId);}}
      onPointerMove={event=>{if(drag.current)onResize(clamp(drag.current.width+drag.current.x-event.clientX));}}
      onPointerUp={event=>{drag.current=null;if(event.currentTarget.hasPointerCapture(event.pointerId))event.currentTarget.releasePointerCapture(event.pointerId);}}
      onPointerCancel={()=>{drag.current=null;}} onLostPointerCapture={()=>{drag.current=null;}}
      onKeyDown={event=>{const next=event.key==="ArrowLeft"?actual+16:event.key==="ArrowRight"?actual-16:event.key==="Home"?minimum:event.key==="End"?maximum:null;if(next!==null){event.preventDefault();event.stopPropagation();onResize(clamp(next));}}}/>
    <aside ref={host} className="workbench-review__files" style={{flexBasis:actual}}>{children}</aside>
  </>;
}
