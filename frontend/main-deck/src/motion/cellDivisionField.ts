/** Startup adaptation of VARIANT-1 Website/src/landing-motion.ts (original project artwork). */
const WHITE = '#dedede';
const clamp = (n:number) => Math.max(0,Math.min(1,n));
const smooth = (n:number) => {const t=clamp(n);return t*t*(3-2*t);};
const mix = (a:number,b:number,t:number) => a+(b-a)*t;
const hash = (n:number) => {const v=Math.sin(n*127.1+311.7)*43758.5453123;return v-Math.floor(v);};
type Cell = {x:number;y:number;r:number;id:number};

const cellSamples=Array.from({length:64*64},(_,i)=>{
  const grain=hash(i+413);
  return {x:(i%64)*10+(grain-.5)*1.1,y:Math.floor(i/64)*10+(hash(i+71)-.5)*1.1,grain};
});

function drawCellField(ctx:CanvasRenderingContext2D,cells:Cell[],time:number,alpha:number,growth:number,accent=false) {
  if (alpha<.01) return;
  const paths=Array.from({length:6},()=>new Path2D());
  const tones=['#444','#666','#888','#aaa','#ccc',accent ? '#39ff14' : WHITE];
  for (const sample of cellSamples) {
    let field=0,nearest=Infinity,owner=cells[0];
    for (const cell of cells) {
      const dx=sample.x-cell.x,dy=sample.y-cell.y;
      const d=dx*dx*.93+dy*dy*1.08;
      const relative=d/(cell.r*cell.r);
      field+=1/(relative+.002);
      if (relative<nearest) {nearest=relative;owner=cell;}
    }
    if (field<.8) continue;
    const boundary=1+.065*Math.sin(sample.x*.034+time*.29)*Math.cos(sample.y*.028-time*.2);
    if (field<boundary) continue;
    const nucleus=nearest<.039;
    if (nucleus&&sample.grain>.07) continue;
    const rim=field-boundary<.12;
    const highlight=(rim&&(owner.id===0||owner.id===3))||(!nucleus&&nearest>.049&&nearest<.063&&owner.id===0);
    const light=clamp(.24+sample.grain*.35+(sample.x-owner.x)/owner.r*.14-(sample.y-owner.y)/owner.r*.21);
    const bucket=highlight?5:Math.min(4,Math.floor(light*5));
    const size=highlight?3.4:2+sample.grain*1.3+(rim?.7:0);
    paths[bucket].rect(sample.x,sample.y,size,size);
  }
  ctx.save();ctx.globalAlpha=alpha;
  ctx.translate(320,320);ctx.rotate(Math.sin(time*.19)*.09-.1);ctx.scale(growth,growth);ctx.translate(-320,-320);
  paths.forEach((path,i)=>{ctx.fillStyle=tones[i];ctx.fill(path);});
  // Follow the shared field so the membrane forms a neck before daughters separate.
  const step=16,side=41,values=new Float32Array(side*side);
  for (let row=0;row<side;row++) for (let col=0;col<side;col++) {
    const x=col*step,y=row*step;
    let field=0;
    for (const cell of cells) {const dx=x-cell.x,dy=y-cell.y;field+=cell.r*cell.r/(dx*dx*.93+dy*dy*1.08+.002*cell.r*cell.r);}
    values[row*side+col]=field-1-.065*Math.sin(x*.034+time*.29)*Math.cos(y*.028-time*.2);
  }
  const outlines=[new Path2D(),new Path2D()];
  for (let row=0;row<side-1;row++) for (let col=0;col<side-1;col++) {
    const x=col*step,y=row*step;
    const corners=[[x,y],[x+step,y],[x+step,y+step],[x,y+step]];
    const v=[values[row*side+col],values[row*side+col+1],values[(row+1)*side+col+1],values[(row+1)*side+col]];
    if (v.every(n=>n>=0)||v.every(n=>n<0)) continue;
    const hits:number[][]=[];
    for (let edge=0;edge<4;edge++) {
      const next=(edge+1)%4;
      if ((v[edge]>=0)===(v[next]>=0)) continue;
      const t=v[edge]/(v[edge]-v[next]);
      hits.push([mix(corners[edge][0],corners[next][0],t),mix(corners[edge][1],corners[next][1],t)]);
    }
    let nearest=Infinity,owner=0;
    for (const cell of cells) {const d=(x-cell.x)**2+(y-cell.y)**2;if(d<nearest){nearest=d;owner=cell.id;}}
    const outline=outlines[owner===0||owner===3?1:0];
    for (let i=0;i+1<hits.length;i+=2) {outline.moveTo(hits[i][0],hits[i][1]);outline.lineTo(hits[i+1][0],hits[i+1][1]);}
  }
  ctx.lineWidth=2.2;ctx.strokeStyle='#777';ctx.stroke(outlines[0]);ctx.strokeStyle=accent ? '#39ff14' : WHITE;ctx.stroke(outlines[1]);
  ctx.restore();
}

export function renderStartupCells(ctx:CanvasRenderingContext2D,seconds:number,accent=false) {
  const time=seconds*(22/6);
  const t=time%22;
  const split=smooth((t-2.6)/5.1),second=smooth((t-9.4)/5.4);
  const parentRadius=171+15*smooth(t/2.6);
  let cells:Cell[];
  if (t<2.6) cells=[{x:320,y:320,r:parentRadius,id:0}];
  else if (t<9.4) cells=[-1,1].map((sign,i)=>({x:320+sign*175*split,y:320+sign*15*split,r:mix(186/Math.sqrt(2),118,split),id:i}));
  else cells=[-1,1].flatMap((sign,i)=>[-1,1].map((vertical,j)=>({x:320+sign*mix(175,149,second),y:320+sign*15+vertical*150*second,r:mix(118/Math.sqrt(2),88,second),id:i*2+j})));
  const renewal=smooth((t-19)/3),growth=.88+.14*smooth(t/3)+Math.sin(time*.56)*.018;
  drawCellField(ctx,cells,time,1-renewal,growth,accent);
  if (renewal>0) drawCellField(ctx,[{x:320,y:320,r:mix(108,171,renewal),id:0}],time,renewal,.88,accent);
}

