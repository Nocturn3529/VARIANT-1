import {useEffect,useRef} from "react";
import {useReducedMotion} from "../state/appearanceStore";
import {renderStartupCells} from "./cellDivisionField";

const TILE=128, DURATION=2.4;
const smooth=(value:number)=>{const t=Math.max(0,Math.min(1,value));return t*t*(3-2*t);};
export function colonyStage(seconds:number,width:number,height:number) {
  const limit=Math.min(6,Math.max(0,Math.ceil(Math.log2((Math.max(width,height+184)+TILE)/TILE))));
  const generation=Math.min(limit,Math.floor(seconds/DURATION));
  return {generation,side:2**generation,phase:Math.min(DURATION,seconds-generation*DURATION),complete:seconds>=(limit+1)*DURATION};
}

export function StartupCells() {
  const canvas=useRef<HTMLCanvasElement>(null);
  const reduced=useReducedMotion(document);
  useEffect(()=>{
    const element=canvas.current,ctx=element?.getContext("2d");
    if(!element || !ctx)return;

    const sprite=document.createElement("canvas");sprite.width=sprite.height=192;
    const field=sprite.getContext("2d");if(!field)return;
    const accented=document.createElement("canvas");accented.width=accented.height=192;
    const accentField=accented.getContext("2d");if(!accentField)return;
    accentField.setTransform(192/640,0,0,192/640,0,0);
    field.setTransform(192/640,0,0,192/640,0,0);
    let frame=0,last=0,time=reduced ? 2.1 : 0;
    let width=window.innerWidth,height=window.innerHeight;
    const draw=()=>{
      const stage=colonyStage(time,width,height);
      const generation=reduced ? 0 : stage.generation,side=2**generation;
      const phase=reduced ? 2.1 : stage.phase/DURATION*4.2;
      field.clearRect(0,0,640,640);renderStartupCells(field,phase);
      accentField.clearRect(0,0,640,640);renderStartupCells(accentField,phase,true);
      ctx.clearRect(0,0,width,height);
      const spread=smooth(stage.phase/.9),size=TILE*(generation ? .52+.48*spread : 1);
      for(let row=0;row<side;row++)for(let col=0;col<side;col++){
        const previous=(index:number)=>(Math.floor(index/2)-(side/2-1)/2)*TILE+(index%2 ? 30 : -30);
        const offset=(index:number)=>generation ? previous(index)*(1-spread)+(index-(side-1)/2)*TILE*spread : 0;
        const seed=col*73.13+row*137.7+generation*11;
        const jitter=generation ? spread : 0;
        const x=width/2+offset(col)+Math.sin(seed)*9*jitter,y=height/2-92+offset(row)+Math.cos(seed*1.7)*9*jitter;
        if(x+size/2<0 || x-size/2>width || y+size/2<0 || y-size/2>height)continue;
        ctx.save();ctx.translate(x,y);ctx.rotate(Math.sin(seed*.73)*.32*jitter);
        const varied=size*(1+Math.sin(seed*.37)*.08*jitter);
        ctx.drawImage(generation===0 || Math.sin(seed*4.9)>0 ? accented : sprite,-varied/2,-varied/2,varied,varied);ctx.restore();
      }
      return stage.complete;
    };
    const tick=(now:number)=>{
      if(now-last>=1000/15){time+=last ? Math.min(100,now-last)/1000 : 0;last=now;if(draw())return;}
      frame=requestAnimationFrame(tick);
    };
    const sync=()=>{cancelAnimationFrame(frame);last=0;if(!document.hidden){draw();if(!reduced)frame=requestAnimationFrame(tick);}};
    const resize=()=>{width=window.innerWidth;height=window.innerHeight;const ratio=Math.min(1.5,window.devicePixelRatio || 1,1920/width,1080/height);element.width=Math.round(width*ratio);element.height=Math.round(height*ratio);ctx.setTransform(ratio,0,0,ratio,0,0);sync();};
    document.addEventListener("visibilitychange",sync);window.addEventListener("resize",resize);resize();
    return()=>{cancelAnimationFrame(frame);document.removeEventListener("visibilitychange",sync);window.removeEventListener("resize",resize);};
  },[reduced]);
  return <canvas ref={canvas} width={128} height={128} className="startup-cells" aria-hidden="true"/>;
}
