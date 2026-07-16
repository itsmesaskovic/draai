/* ---------------- spectrum analyzer ---------------- */
let specC, specW2, SPEC_N=56, specVals=new Float32Array(SPEC_N), specPk=new Float32Array(SPEC_N);
function bandCurve(f,low,mid,high){ // f: 0..1 across the spectrum
  const L=Math.exp(-Math.pow((f-0.05)/0.16,2)), M=Math.exp(-Math.pow((f-0.42)/0.26,2)), H=Math.exp(-Math.pow((f-0.9)/0.3,2));
  const roll=1-0.28*f; // gentle high-end rolloff
  return clamp((low*L+mid*M*0.95+high*H*0.9)*roll,0,1.15);
}
function rrect(ctx,x,y,w,h,r){ if(h<=0){ctx.beginPath();return;} r=Math.min(r,w/2,h/2); ctx.beginPath(); ctx.moveTo(x+r,y); ctx.arcTo(x+w,y,x+w,y+h,r); ctx.arcTo(x+w,y+h,x,y+h,r); ctx.arcTo(x,y+h,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath(); }
function drawSpec(dt,t,prog){ if(!specC) return; if(!specC.W) sizeCanvas(specC); const {ctx,W,H}=specC; ctx.clearRect(0,0,W,H);
  const on=playing&&!REDUCE, low=playing?sm.low:0, mid=playing?sm.mid:0, high=playing?sm.high:0;
  const floor=H-26, gap=Math.max(2,W/SPEC_N*0.22), bw=W/SPEC_N-gap;
  const acc=[94,234,212];
  for(let i=0;i<SPEC_N;i++){ const f=i/(SPEC_N-1);
    const jitter=on?(0.5+0.5*Math.sin(t/150+i*0.7)+0.35*Math.sin(t/70+i*1.9))/1.35:0;
    const tgt=clamp(bandCurve(f,low,mid,high)*(0.55+0.6*jitter),0,1.1);
    const atk=1-Math.exp(-dt/0.045), dec=1-Math.exp(-dt/0.16);
    specVals[i]+= tgt>specVals[i]?(tgt-specVals[i])*atk:(tgt-specVals[i])*dec;
    specPk[i]=Math.max(specVals[i], specPk[i]-dt*0.6);
    const v=clamp(specVals[i],0,1.1), h=v*(floor-10), x=i*(bw+gap)+gap/2;
    const g=ctx.createLinearGradient(0,floor,0,floor-h);
    g.addColorStop(0,`rgb(${acc[0]} ${acc[1]} ${acc[2]} / .35)`); g.addColorStop(.55,`rgb(${acc[0]} ${acc[1]} ${acc[2]})`);
    g.addColorStop(1, v>0.86?"#ff6b5a":`rgb(180 250 235)`);
    ctx.fillStyle=g; rrect(ctx,x,floor-h,bw,h,Math.min(3,bw/2)); ctx.fill();
    ctx.globalAlpha=0.12; ctx.fillStyle=`rgb(${acc[0]} ${acc[1]} ${acc[2]})`; rrect(ctx,x,floor+3,bw,Math.min(h*0.4,26),Math.min(3,bw/2)); ctx.fill(); ctx.globalAlpha=1;
    const py=floor-clamp(specPk[i],0,1.1)*(floor-10); ctx.fillStyle=specPk[i]>0.86?"#ff6b5a":"#dffaf3"; ctx.fillRect(x,py-2,bw,2);
  }
  ctx.fillStyle="rgba(255,255,255,.07)"; ctx.fillRect(0,floor+1,W,1);
  if(specW2){ drawWave(specW2,prog); $("#specPos").textContent=fmt(posSec); $("#specDur").textContent=fmt(durSec); }
}
function openSpec(){ openMode("spec",[()=>sizeCanvas(specC),()=>sizeCanvas(specW2)]); }
