/* ---------------- amplifier VU ---------------- */
let vuLc, vuRc, ampCv, vuL=0, vuR=0, peakLT=0, peakRT=0, ampLamp=true;
function mkCv(id){ const cv=document.getElementById(id); return {cv,ctx:cv.getContext("2d")}; }
function sizeCv(o){ if(!o||!o.cv) return; const r=o.cv.getBoundingClientRect(), dpr=Math.min(2,devicePixelRatio||1);
  o.cv.width=Math.max(1,r.width*dpr); o.cv.height=Math.max(1,r.height*dpr); o.ctx.setTransform(dpr,0,0,dpr,0,0); o.W=r.width; o.H=r.height; }
const FS_IDS=["np","amp","spec"];
function openMode(id, sizers){
  FS_IDS.forEach(x=>$("#"+x).classList.remove("open"));
  $("#"+id).classList.add("open");
  requestAnimationFrame(()=>{ (sizers||[]).forEach(s=>s()); });
}
function openNp(){ openMode("np",[()=>sizeCanvas(npCv)]); armIdle(); loadAccess(); }
function openAmp(){ $("#amp").classList.toggle("lampon",ampLamp); openMode("amp",[()=>sizeCv(vuLc),()=>sizeCv(vuRc),()=>sizeCanvas(ampCv)]); }
function closeAmp(){ $("#amp").classList.remove("open"); }
/* ---- amp meter tuning (A/B/C) ---- */
const VU_LATENCY = 0.0;    // s — meter energy read offset vs the clock (C2); room-align knob, 0 = off
const SYNC_NUDGE = 0.25;   // fraction of sub-threshold drift corrected per poll (C1)
const VU_DB_FLOOR = -38;   // dB — bottom of the perceptual window (B)
const VU_EPS = 1e-3;       // log floor (B)
function vuMap(v){ const db = 20*Math.log10(Math.max(v, VU_EPS));
  return clamp((db - VU_DB_FLOOR) / (0 - VU_DB_FLOOR), 0, 1.10); }   // 0..1.1, >1 keeps red zone / PEAK alive
function drawAmp(dt,t,prog){
  // A: own raw read at the clock (+C offset), NOT the 90ms `sm`. B: perceptual map. E: real per-channel.
  const em = playing ? energyAt(posSec + VU_LATENCY) : null;
  let tL = em ? vuMap(em.ampL) : 0;
  let tR = em ? vuMap(em.ampR) : 0;
  const atk=REDUCE?0.2:1-Math.exp(-dt/0.05), dec=1-Math.exp(-dt/0.24);   // ~50ms attack, ~240ms decay
  vuL+= tL>vuL?(tL-vuL)*atk:(tL-vuL)*dec;
  vuR+= tR>vuR?(tR-vuR)*atk:(tR-vuR)*dec;
  drawVU(vuLc,vuL); drawVU(vuRc,vuR);
  peakLT=vuL>0.98?0.9:Math.max(0,peakLT-dt); peakRT=vuR>0.98?0.9:Math.max(0,peakRT-dt);
  $("#peakL").classList.toggle("hot",peakLT>0); $("#peakR").classList.toggle("hot",peakRT>0);
  if(ampCv){ drawWave(ampCv,prog); $("#ampPos").textContent=fmt(posSec); $("#ampDur").textContent=fmt(durSec); }
}
const VU_MARKS=[[0,"20"],[0.26,"10"],[0.42,"7"],[0.54,"5"],[0.67,"3"],[0.82,"0"],[1,"+3"]];
function drawVU(o,val){ if(!o||!o.W){ if(o) sizeCv(o); if(!o||!o.W) return; } const {ctx,W,H}=o; ctx.clearRect(0,0,W,H);
  const face=ctx.createLinearGradient(0,0,0,H);
  if(ampLamp){ face.addColorStop(0,"#f6e8c6"); face.addColorStop(1,"#e7cc88"); } else { face.addColorStop(0,"#403c2d"); face.addColorStop(1,"#29271d"); }
  ctx.fillStyle=face; ctx.fillRect(0,0,W,H);
  const ink=ampLamp?"#2a2114":"#6b6250", red=ampLamp?"#c0341c":"#7a3226";
  const cx=W/2, cy=H*1.34, R=H*1.16, a0=-Math.PI/2-0.62, a1=-Math.PI/2+0.62;
  // red zone arc (past 0 dB)
  ctx.strokeStyle=red; ctx.lineWidth=3.5; ctx.beginPath(); ctx.arc(cx,cy,R-3,a0+(a1-a0)*0.82,a1); ctx.stroke();
  ctx.strokeStyle=ink; ctx.lineWidth=1.5; ctx.beginPath(); ctx.arc(cx,cy,R-3,a0,a0+(a1-a0)*0.82); ctx.stroke();
  // ticks + labels
  ctx.textAlign="center"; ctx.font="600 10px ui-monospace,Menlo,monospace";
  for(const [tk,lab] of VU_MARKS){ const ang=a0+(a1-a0)*tk;
    const x1=cx+Math.cos(ang)*(R-8), y1=cy+Math.sin(ang)*(R-8), x2=cx+Math.cos(ang)*(R-16), y2=cy+Math.sin(ang)*(R-16);
    ctx.strokeStyle=tk>=0.82?red:ink; ctx.lineWidth=1.6; ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
    const lx=cx+Math.cos(ang)*(R-27), ly=cy+Math.sin(ang)*(R-27)+3.5; ctx.fillStyle=tk>=0.82?red:ink; ctx.fillText(lab,lx,ly); }
  // minor ticks
  ctx.lineWidth=1;
  for(let k=0;k<=40;k++){ const tk=k/40; if(VU_MARKS.some(m=>Math.abs(m[0]-tk)<0.02))continue; const ang=a0+(a1-a0)*tk;
    ctx.strokeStyle=tk>=0.82?red:ink; ctx.globalAlpha=0.5; ctx.beginPath();
    ctx.moveTo(cx+Math.cos(ang)*(R-8),cy+Math.sin(ang)*(R-8)); ctx.lineTo(cx+Math.cos(ang)*(R-13),cy+Math.sin(ang)*(R-13)); ctx.stroke(); ctx.globalAlpha=1; }
  ctx.fillStyle=ink; ctx.font="700 11px system-ui"; ctx.fillText("VU",cx,H-14);
  // needle
  const ang=a0+(a1-a0)*clamp(val,0,1.06);
  ctx.strokeStyle="#1a1206"; ctx.lineWidth=2; ctx.lineCap="round"; ctx.beginPath();
  ctx.moveTo(cx,cy); ctx.lineTo(cx+Math.cos(ang)*(R+4),cy+Math.sin(ang)*(R+4)); ctx.stroke();
  ctx.fillStyle="#1a1206"; ctx.beginPath(); ctx.arc(cx,cy,5,0,Math.PI*2); ctx.fill();
}
let knobDrag=false;
function setKnob(el,v,min,max){ const f=(v-min)/(max-min||1); el.style.setProperty("--rot",(-140+f*280)+"deg"); }
function wireKnob(el,get,set){
  const sens=0.7;
  const start=e=>{ knobDrag=true; const y0=(e.touches?e.touches[0]:e).clientY, v0=get();
    const move=ev=>{ const y=(ev.touches?ev.touches[0]:ev).clientY; set(v0+(y0-y)*sens); ev.preventDefault(); };
    const up=()=>{ knobDrag=false; removeEventListener("pointermove",move); removeEventListener("pointerup",up); removeEventListener("pointercancel",up); };
    addEventListener("pointermove",move); addEventListener("pointerup",up); addEventListener("pointercancel",up); };
  el.addEventListener("pointerdown",start);
  el.addEventListener("wheel",e=>{ set(get()+(e.deltaY<0?2:-2)); e.preventDefault(); },{passive:false});
}

