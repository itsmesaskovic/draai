/* ============================================================
   DRIVE — 80s pixel-art game visualizer.

   Rendering: everything is drawn into a low-res 320x180 (16:9) backing
   buffer with plain fillRect pixel-plotting, then blitted scaled onto the
   real display canvas with imageSmoothingEnabled=false for a crisp
   pixel-art look (ui/modes/drive/drive.css sets image-rendering:pixelated
   on the canvas too).

   House rule: never a fake signal. Beats and section boundaries are ALL
   derived from the real energy envelope (energyAt(posSec), sm.*) — never
   from a timer or Math.random(). Static layout "randomness" (star fields)
   uses a seeded, deterministic PRNG (driveRnd) so it never drives a
   musical event and never flickers on its own. Beat impact lives entirely
   in world elements (tail lights, sun core, road edges, speed lines) —
   nothing translates the whole frame. prefers-reduced-motion damps all of
   it to near-static.
============================================================ */

let driveC=null, driveBuf=null, driveBufCtx=null;

/* ---------------- low-res pixel toolkit (ported from the DRIVE art-direction board) ---------------- */
const DRIVE_W=320, DRIVE_H=180;
const DRIVE_TEAL="#2ee6c8";
const DRIVE_BAYER=[[0,8,2,10],[12,4,14,6],[3,11,1,9],[15,7,13,5]];
function driveRnd(seed){ let x=seed%2147483647; if(x<=0)x+=2147483646; return ()=>((x=x*16807%2147483647)-1)/2147483646; }
function driveHexToRgb(h){ h=h.replace("#",""); if(h.length===3)h=h.split("").map(c=>c+c).join(""); return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)]; }
function driveWithA(h,a){ const c=driveHexToRgb(h); return `rgba(${c[0]},${c[1]},${c[2]},${a})`; }
function drive255(v){ return v<0?0:v>255?255:v; }
function driveMixColor(c,t){ // lighten a color (hex string or [r,g,b] array) toward white by t (0..1) — used for beat flares
  const rgb=Array.isArray(c)?c:driveHexToRgb(c); t=t<0?0:t>1?1:t;
  const L=v=>drive255(v+(255-v)*t)|0;
  return `rgb(${L(rgb[0])},${L(rgb[1])},${L(rgb[2])})`;
}
function driveParseCol(c){ // accepts "#rrggbb" or "rgb(r,g,b)" — road colours come in both forms
  if(Array.isArray(c)) return c;
  if(c.charCodeAt(0)===35) return driveHexToRgb(c);
  const m=c.match(/-?\d+/g); return m?[+m[0],+m[1],+m[2]]:[0,0,0];
}
function driveBlend(a,b,t){ // a -> b by t (0..1)
  const x=driveParseCol(a), y=driveParseCol(b); t=t<0?0:t>1?1:t;
  return `rgb(${x[0]+(y[0]-x[0])*t|0},${x[1]+(y[1]-x[1])*t|0},${x[2]+(y[2]-x[2])*t|0})`;
}
function P(ctx,x,y,w,h,c){ ctx.fillStyle=c; ctx.fillRect(x,y,w,h); }
function driveDitherV(ctx,x,y,w,h,top,bot){ // Bayer 4x4 dithered vertical gradient
  const pt=driveHexToRgb(top), pb=driveHexToRgb(bot);
  for(let j=0;j<h;j++){ const tt=j/(h-1||1);
    const r=pt[0]+(pb[0]-pt[0])*tt, g=pt[1]+(pb[1]-pt[1])*tt, b=pt[2]+(pb[2]-pt[2])*tt;
    for(let i=0;i<w;i++){ const th=DRIVE_BAYER[((y+j)|0)&3][((x+i)|0)&3]/16;
      const rr=r+(th-.5)*22, gg=g+(th-.5)*22, bb=b+(th-.5)*22;
      ctx.fillStyle=`rgb(${drive255(rr)|0},${drive255(gg)|0},${drive255(bb)|0})`;
      ctx.fillRect((x+i)|0,(y+j)|0,1,1); } }
}
function driveStars(ctx,x,y,w,h,seed,n,color,accent){
  const r=driveRnd(seed);
  for(let k=0;k<n;k++){ const sx=x+(r()*w|0), sy=y+(r()*h|0), big=r()>.86;
    P(ctx,sx,sy,big?2:1,big?2:1, r()>.3?color:accent); }
}
/* ---------------- bitmap pixel font (5x7, hand-rolled — no web font, no antialiasing) ----------------
   Every glyph is a compact 5-wide x 7-tall bit pattern ('#'=on). Plotted with P() as whole buffer
   pixels, so it upscales perfectly crisp through the 4x nearest-neighbour blit. Covers A-Z, 0-9, the
   punctuation this UI actually uses, plus a couple of glyphs (em dash, right arrow) pulled in from the
   copy so nothing silently goes blank. Unknown characters render as blank space — never throw. */
const DRIVE_FONT_ROWS={
  " ":[".....",".....",".....",".....",".....",".....","....."],
  "A":[".###.","#...#","#...#","#####","#...#","#...#","#...#"],
  "B":["####.","#...#","#...#","####.","#...#","#...#","####."],
  "C":[".####","#....","#....","#....","#....","#....",".####"],
  "D":["####.","#...#","#...#","#...#","#...#","#...#","####."],
  "E":["#####","#....","#....","####.","#....","#....","#####"],
  "F":["#####","#....","#....","####.","#....","#....","#...."],
  "G":[".####","#....","#....","#.###","#...#","#...#",".####"],
  "H":["#...#","#...#","#...#","#####","#...#","#...#","#...#"],
  "I":["#####","..#..","..#..","..#..","..#..","..#..","#####"],
  "J":["..###","...#.","...#.","...#.","...#.","#..#.",".##.."],
  "K":["#...#","#..#.","#.#..","##...","#.#..","#..#.","#...#"],
  "L":["#....","#....","#....","#....","#....","#....","#####"],
  "M":["#...#","##.##","#.#.#","#...#","#...#","#...#","#...#"],
  "N":["#...#","##..#","#.#.#","#..##","#...#","#...#","#...#"],
  "O":[".###.","#...#","#...#","#...#","#...#","#...#",".###."],
  "P":["####.","#...#","#...#","####.","#....","#....","#...."],
  "Q":[".###.","#...#","#...#","#...#","#.#.#","#..#.",".##.#"],
  "R":["####.","#...#","#...#","####.","#.#..","#..#.","#...#"],
  "S":[".####","#....","#....",".###.","....#","....#","####."],
  "T":["#####","..#..","..#..","..#..","..#..","..#..","..#.."],
  "U":["#...#","#...#","#...#","#...#","#...#","#...#",".###."],
  "V":["#...#","#...#","#...#","#...#","#...#",".#.#.","..#.."],
  "W":["#...#","#...#","#...#","#.#.#","#.#.#","##.##","#...#"],
  "X":["#...#","#...#",".#.#.","..#..",".#.#.","#...#","#...#"],
  "Y":["#...#","#...#",".#.#.","..#..","..#..","..#..","..#.."],
  "Z":["#####","....#","...#.","..#..",".#...","#....","#####"],
  "0":[".###.","#...#","#..##","#.#.#","##..#","#...#",".###."],
  "1":["..#..",".##..","..#..","..#..","..#..","..#..","#####"],
  "2":[".###.","#...#","....#","...#.","..#..",".#...","#####"],
  "3":["####.","....#","....#","..##.","....#","....#","####."],
  "4":["...#.","..##.",".#.#.","#..#.","#####","...#.","...#."],
  "5":["#####","#....","#....","####.","....#","....#","####."],
  "6":[".###.","#....","#....","####.","#...#","#...#",".###."],
  "7":["#####","....#","...#.","..#..",".#...",".#...",".#..."],
  "8":[".###.","#...#","#...#",".###.","#...#","#...#",".###."],
  "9":[".###.","#...#","#...#",".####","....#","....#",".###."],
  ":":[".....","..#..",".....",".....",".....","..#..","....."],
  ".":[".....",".....",".....",".....",".....",".....","..#.."],
  ",":[".....",".....",".....",".....",".....","..#..",".#..."],
  "/":["....#","...#.","...#.","..#..",".#...",".#...","#...."],
  "-":[".....",".....",".....","#####",".....",".....","....."],
  "—":[".....",".....",".....","#####",".....",".....","....."],
  "·":[".....",".....","..#..",".....",".....",".....","....."],
  "(":["...#.","..#..",".#...",".#...",".#...","..#..","...#."],
  ")":[".#...","..#..","...#.","...#.","...#.","..#..",".#..."],
  "'":["..#..","..#..",".....",".....",".....",".....","....."],
  "?":[".###.","#...#","....#","..##.","..#..",".....","..#.."],
  "!":["..#..","..#..","..#..","..#..","..#..",".....","..#.."],
  "%":["#...#","....#","...#.","..#..",".#...","#....","#...#"],
  "…":[".....",".....",".....",".....",".....",".....","#.#.#"],
  "▶":["#....","##...","###..","####.","###..","##...","#...."],
  "★":["..#..","..#..",".#.#.","#####",".#.#.","..#..","..#.."],
  "×":[".....","#...#",".#.#.","..#..",".#.#.","#...#","....."],
  "→":[".....","..#..","...#.","#####","...#.","..#..","....."],
};
const DRIVE_FONT=(()=>{ const out={};
  for(const ch in DRIVE_FONT_ROWS){ const rows=DRIVE_FONT_ROWS[ch], pts=[];
    for(let ry=0;ry<rows.length;ry++) for(let rx=0;rx<rows[ry].length;rx++) if(rows[ry][rx]==="#") pts.push(rx,ry);
    out[ch]=pts; }
  return out; })();
const DRIVE_GLYPH_H=7, DRIVE_ADV=6; // 5px glyph + 1px spacing, per scale unit
function driveText(ctx,str,x,y,color,scale){ // plots whole buffer pixels via P() — perfectly crisp on upscale
  scale=scale||1; str=String(str); let cx=Math.round(x); const yy=Math.round(y);
  for(let i=0;i<str.length;i++){ const pts=DRIVE_FONT[str[i].toUpperCase()];
    if(pts) for(let p=0;p<pts.length;p+=2) P(ctx,cx+pts[p]*scale,yy+pts[p+1]*scale,scale,scale,color);
    cx+=DRIVE_ADV*scale; }
}
function driveTextWidth(str,scale){ return String(str).length*DRIVE_ADV*(scale||1); }
function driveTextC(ctx,str,cx,y,color,scale){ driveText(ctx,str,cx-driveTextWidth(str,scale)/2,y,color,scale); } // center-aligned
function driveTextR(ctx,str,rx,y,color,scale){ driveText(ctx,str,rx-driveTextWidth(str,scale),y,color,scale); }    // right-aligned
function driveFitText(text,maxW,baseScale){ // shrink then ellipsize (against driveTextWidth) to fit maxW
  let scale=Math.max(1,baseScale|0);
  while(scale>1 && driveTextWidth(text,scale)>maxW) scale--;
  if(driveTextWidth(text,scale)>maxW){ let t=text;
    while(t.length>1 && driveTextWidth(t+"…",scale)>maxW) t=t.slice(0,-1);
    text=t+"…"; }
  return {text,scale};
}
function driveArtistUC(){ const el=$("#driveArtist"); return (el&&el.textContent)?el.textContent.toUpperCase():""; }
function driveAccentStr(){ return `rgb(${pal.ac[0]|0},${pal.ac[1]|0},${pal.ac[2]|0})`; }

/* ---------------- BYTE the courier drone ---------------- */
function drawByte(ctx,cx,cy,s,accent,pose,pulse){
  pulse=pulse||0;
  const c=(x,y,w,h,col)=>P(ctx,Math.round(cx+x*s),Math.round(cy+y*s),Math.max(1,w*s|0),Math.max(1,h*s|0),col);
  const lean=pose==="move"?1:0;
  ctx.save(); ctx.shadowColor=accent; ctx.shadowBlur=7;
  c(-2+lean,7,4,2,accent);
  if(pose==="move"){ const boost=.6+pulse*.4; c(-4,8,2,1,driveWithA(accent,boost)); c(4,8,2,1,driveWithA(accent,boost)); }
  ctx.restore();
  c(-5,5,10,2,"#2b3350"); c(-4,6,8,1,"#171c2e");
  c(-4,-3,8,8,"#c9d2e6"); c(-4,-3,8,2,"#eef3ff"); c(-4,3,8,2,"#8b93ad"); c(4,-3,1,8,"#6a7290");
  ctx.save(); ctx.shadowColor=accent; ctx.shadowBlur=6;
  c(-3,-1,6,3,"#0a0e18"); c(-2,-1,4,2,accent); c(-2,-1,2,1,"#ffffff");
  ctx.restore();
  c(0,-6,1,3,"#8b93ad");
  ctx.save(); ctx.shadowColor=DRIVE_TEAL; ctx.shadowBlur=6+pulse*6;
  c(-1,-8,2,2, pulse>.35?"#eafff8":DRIVE_TEAL); ctx.restore();
  ctx.save(); ctx.globalAlpha=.5; c(-5,-4,10,1,"#ffffff"); ctx.restore();
}

/* ---------------- car sprite: DeLorean rear, drawn from reference ----------------
   36x42 LEFT HALF, mirrored at draw time to 72x42. 'A' = tail-light lens (kept red,
   authentic); 'W' = clear reversing lamp, which is what flares with the beat; 'P' =
   licence plate, never flares. */
const CAR_W=36, CAR_H=42;
const CAR_PAL={"0":"#0c0e12","1":"#e2e7ec","2":"#c6ccd3","4":"#3a4448","5":"#283034","6":"#1a1e22","7":"#8c949c","8":"#b0b7be","9":"#e8a020","A":"#c8324b","G":"rgba(236,244,252,0.34)","K":"rgba(0,0,0,0.55)","L":"#848c96","M":"#ced4da","P":"#fcfcfc","R":"#3a424c","S":"#464e56","T":"#191c22","W":"#fcfcfc"};
const CAR_ROWS=[
  "..................022222222222222222",
  "................02222222222222222222",
  "...............0GGGGGGGGGGGGGGGGGGGG",
  ".............02LLLLLLLLLLLLLLLLLLLLL",
  "............02GGGGGGGGGGGGGGGGGGGGGG",
  "...........02GGGGGGGGGGGGGGGGGGGGGGG",
  "..........02LLLLLLLLLLLLLLLLLLLLLLLL",
  ".......0222GGGGGGGGSSSSSSSGGGGGGGGGG",
  ".....02222GGGGGGGGGSSSSSSSGGGGGGGGGG",
  "..0MM0222LLLLLLLLLLLLLLLLLLLLLLLLLLL",
  "...0MMM11111111111111111111111111111",
  ".....0111111111111111111111111111111",
  "....06666666666666666666666666666666",
  "....069999AAA6AA6AA6AA6WWWW6PPPPPPPP",
  "...069999AAA6AA6AA6AA6AWWWW6PPPPPPPP",
  "...0666666666666666666666666PPPPPPPP",
  "..069999AAA6AA6AA6AA6AAWWWW6PPPPPPPP",
  ".069999AAA6AA6AA6AA6AA6WWWW6PPPPPPPP",
  ".06666666666666666666666666666666666",
  "022222222222222222222222222222222222",
  "022222222222222222222222222222222222",
  "022222222222222222222222222222222222",
  "077777777777777777777777777777777777",
  "044444444444444444444444444444444444",
  "044444444444444444444444444444444444",
  "044444444444444444444444444444444444",
  "044444444444444444444444444444444444",
  "044444444444444444444444444444444444",
  "0TTTTTTTTT44444444444444444444444444",
  "0TTTTTTTTT44444444444444444444444444",
  "0TTRRRRRRRRT444444444444444444444444",
  "0RRTTTTTTTTT444444444444444444444444",
  "0RRTTTTTTTTTT86686686686686686686688",
  "0RRRRRRRRRRTTKKKKKKKKKKKKKK888888888",
  "0RRTTTTTTTTTTKKKKKKKKKKKKKK885885885",
  "0RRTTTTTTTTTTKKKKKKKKKKKKKKKKKKKKKKK",
  "0RRRRRRRRRRTTKKKKKKKKKKKKKKKKKKKKKKK",
  "0RRTTTTTTTTTKKKKKKKKKKKKKKKKKKKKKKKK",
  "0RRTTTTTTTTTKKKKKKKKKKKKKKKKKKKKKKKK",
  "0RRRRRRRRRRTKKKKKKKKKKKKKKKKKKKKKKKK",
  "0TTTTTTTTTTTKKKKKKKKKKKKKKKKKKKKKKKK",
  "0TTTTTTTTTTTKKKKKKKKKKKKKKKKKKKKKKKK",
];

/* ---------------- car registry ----------------
   The DeLorean above is the BUILT-IN car, so the zero-file experience still has one.
   Anything dropped in <config>/cars/*.json is offered alongside it (see draai/cars.py
   for the format) — a new car needs no code change and no rebuild. Cycle with C.

   `flare` names the palette key that pulses on the beat. Everything else is baked
   into a canvas once; only those pixels are redrawn per frame, which is what keeps
   the glow on the indicators instead of haloing the whole car. */
const DRIVE_CAR_BUILTIN={id:"delorean",name:"DeLorean",w:CAR_W,h:CAR_H,
  flare:"9",pal:CAR_PAL,rows:CAR_ROWS};
let driveCars=[DRIVE_CAR_BUILTIN], driveCarIdx=0;
function driveCar(){ return driveCars[driveCarIdx] || DRIVE_CAR_BUILTIN; }
function driveSetCar(id){
  const i=driveCars.findIndex(c=>c.id===id);
  if(i>=0 && i!==driveCarIdx){ driveCarIdx=i; carBodyCv=null; }   // drop the baked body
}
function driveCycleCar(){
  if(driveCars.length<2) return;
  drivePickCar((driveCarIdx+1)%driveCars.length);
  toast("Car: "+driveCar().name,true);
}
/* Render the picker. Rebuilt on load and on every switch, which is cheap (a handful
   of buttons) and keeps the selected marker honest without extra state. */
function renderCarPick(){
  const lbl=$("#driveCarName"); if(lbl) lbl.textContent=driveCar().name;
  const el=$("#carPick"); if(!el) return;
  el.innerHTML="";
  driveCars.forEach((c,i)=>{
    const b=document.createElement("button");
    b.type="button"; b.textContent=c.name; b.setAttribute("role","option");
    b.setAttribute("aria-selected", i===driveCarIdx ? "true" : "false");
    b.addEventListener("click",()=>{ drivePickCar(i); el.classList.remove("open"); });
    el.appendChild(b);
  });
}
function drivePickCar(i){
  if(i<0 || i>=driveCars.length || i===driveCarIdx) return;
  driveCarIdx=i; carBodyCv=null;                       // drop the baked body, rebake on next frame
  const c=driveCar();
  A.driveCar=c.id; savePrefs();
  renderCarPick();
}
async function loadDriveCars(){
  try{
    const r=await api("/api/cars");
    const extra=(r&&r.cars)||[];
    driveCars=[DRIVE_CAR_BUILTIN].concat(extra);
    driveTraffic=[DRIVE_TRAFFIC_BUILTIN, DRIVE_TRAFFIC_VAN].concat((r&&r.traffic)||[]);
    const want=A.driveCar||DRIVE_CAR_BUILTIN.id;
    driveCarIdx=Math.max(0,driveCars.findIndex(c=>c.id===want));
    carBodyCv=null;
    renderCarPick();
  }catch(e){ /* no engine, or none installed — the built-in is enough */ }
}

/* Plot the car sprite: left half + mirror. The tail-light lens stays red (authentic);
   the indicators flare toward the album accent on the beat, and the cluster picks up
   an accent glow — the beat is felt without recolouring the car. */
function drawCarSprite(ctx,cx,baseY,accent,beatK){
  const car=driveCar(), W=car.w, H=car.h, FL=car.flare;
  const k=Math.max(0,Math.min(1,beatK));
  ctx.drawImage(carBodyCanvas(), cx-W, baseY-H);                   // baked body: 1 call, not ~3000
  const lit=car.pal[FL];
  if(!lit) return;                                                 // car has no flare colour: body only
  const amber = k>0.02 ? driveMixColor(lit, k*0.65) : lit;
  ctx.save();
  if(k>0.03){ ctx.shadowColor=accent; ctx.shadowBlur=3+k*11; }
  for(let y=0;y<H;y++){
    const row=car.rows[y], py=baseY-H+y;
    for(let x=0;x<W;x++){
      if(row[x]!==FL) continue;
      P(ctx,cx-W+x,py,1,1,amber); P(ctx,cx+W-1-x,py,1,1,amber);
    }
  }
  ctx.restore();
}

/* ---------------- oncoming traffic: KITT (built in) ----------------
   FRONT view, 50x56 left half -> 100x56 mirrored. Traced from pixel art, so the
   scanner is its own key rather than baked lit: 'X' marks the grille bar and
   drawTrafficSprite lights a moving segment of it. Front views live in a separate
   registry from player cars — you should not be able to drive a car facing you. */
const KITT_PAL={"0":"#181719", "1":"#434346", "2":"#050608", "3":"#111315", "4":"#1e0e0e", "5":"#0d0f13", "6":"#131316", "7":"#151518", "8":"#191e24", "9":"#505056", "A":"#303439", "B":"#686669", "C":"#380000", "D":"#510301", "X":"#3a0000", "g":"rgba(25,30,36,0.70)"};
const KITT_ROWS=[
  "...........................11111111111111111111111",
  ".......................A11103333333353333333333333",
  "......................1A2555555555555537gggggggggg",
  ".....................5123337gggggggggggAAAA1111111",
  "....................2153337gggggggggggggAAA1111111",
  "...................2123337gggggggggggggggAAA111111",
  "...................9553337ggggggggggggggggAA111111",
  "..................1A53337gggggggggggggggggAA111111",
  ".................g123333gggggggggggggggggggA111111",
  ".................1533337ggggA1AAAAAggggggggAA1111A",
  "................A053333ggggA1AAAAAAAggggggggAA111A",
  "........g99Ag22gA233337gggg1AAAAAAAAAgggggggAA11AA",
  ".......AB91A5231233333gggggAAAAAAAAAAAAggggggAAggg",
  ".......A9AA022105333337807777333377777670777700077",
  ".......087652A822222223336666666666666888888888811",
  "........2222206600000888888888888888888888888881BB",
  ".......8681BBBBBB18880888888888888888888AAAAAAABBB",
  "......A99BBBBBBBBBB91A8A111111111111111119BBBBBB99",
  ".....A9ABBBBBBBBBBBBB919BBBBBBBBBBBBBBBBBBBBBBB999",
  "....89A1999999999999999999999999999999999999999111",
  "....1981111111111111111111111111111999111199991A11",
  "...A9AAAAAAAAAAAA11111111111111111111111111111AAAA",
  "...110AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "..89888888888888AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA8888",
  "..A17888888888888888888888888888888888888888A88888",
  "..1A5000000000000000388888888888888888888888888000",
  "..180800000000000088588888888888888888888888880777",
  ".A160770007777777778508880000000000000000000007777",
  ".9A57777777777777778570000777777777777777777777777",
  ".9825555555555555553255555555555555555555555555555",
  "6A653333333333533336722222222222222C22422CCCDDXXXX",
  "A1352222222222222222222222555555524XCCXCDXXXXXXXXX",
  "A155222222222222222222222222222565222222222222CXXX",
  "A1733333333333333333333333333333386777777777777777",
  "A1866667777766333333335555555555588555555555555555",
  "A1866676777777763333333355555555578555555555555555",
  "A987677777777777663333333555555555A055555555555555",
  ".A888888800777777766333333355555558A55555555555555",
  ".A222253773888888888888733335555555A85555555555555",
  ".A222222222222222255535578888800736AA8888880088080",
  ".A2233222228AAAAA823800005222222555652222222222222",
  ".0226066655A11199A58AAA11A225555562222222222222222",
  ".05236063558A1111A28AAA11A220000882255222222222222",
  ".0322252222222222225555552220000802222222222222222",
  "..A82222222222222222222252222222252222222222222222",
  "..AAA835555308800000000032222222222222222222222222",
  "..8AA862222222222222222223555555556763555555555555",
  "...55553308065555555555555222222222222222222222222",
  "....2222222222222222222227880077776666336333333333",
  ".....30525255222222.222222222222222222222222222222",
  ".....58050255252525...............................",
  ".....3A023235232352...............................",
  ".....68626207252522...............................",
  "......8853253252522...............................",
  "......8025225252552...............................",
  ".......52222222222................................"
];

const DRIVE_TRAFFIC_BUILTIN={id:"kitt",name:"KITT",w:50,h:56,flare:"X",scanner:"X",
  pal:KITT_PAL,rows:KITT_ROWS};


/* ---------------- oncoming traffic: A-Team van (built in) ----------------
   GMC Vandura, front view, 88x80 left half. Flare = amber roof markers + indicators.
   No windscreen transparency: a panel van has no rear glass to see through. */
const VAN_PAL={"0":"#050506", "1":"#141317", "2":"#1c1c20", "3":"#1f2129", "4":"#95939a", "5":"#ccc9d0", "6":"#2f323c", "7":"#c45623", "8":"#3e3f46", "9":"#b1aeb5", "A":"#cdbdb9", "B":"#212c42", "C":"#676a75", "D":"#63544c", "Z":"#ffb043"};
const VAN_ROWS=[
  ".....................DCCCC......77Z776......",
  "....................D4A5ACC....37ZZZ7D......",
  "....................D45554C....27ZZZ78......",
  "....................6C444C84CCCD6DDD88444444",
  "................44444D444C499999499949999999",
  "..............99999999999999AAA9AAAAAAA99999",
  ".............499999AAAAAAAA55AAAAAAAAAAA9AAA",
  "............69999A555555555555555555555AAAA5",
  "............49995555555555555555555555555555",
  "............94C88888861111111111111111111111",
  "............C1000000000000000000000000000000",
  "............60000000000000000000000000000000",
  "...........880000000000000000000000000000000",
  "...........C62100000000000000000000000000001",
  "...........42B620000000000000000001111111111",
  "...........C38863332236666666666666666666632",
  "..........CD38C8BB333B6666666666666666666663",
  "..........466886BB333B6666666666666666666663",
  "..........438D86B333366666666666666666666663",
  "..........C68D8BB333366666666666666666666663",
  ".........486888BB333B66666666666666666666663",
  ".........436886BB3338CC866666666666666666666",
  "..49944..438DCCCCDDCD8C4C88C666666666666668C",
  "..49594.4C388611000000016DD86666666666666662",
  "..9955D.986888B3322366B33B88CCC8888888888886",
  "..9A548.9D68866336666666632666844C8866666666",
  "..5559C89949C444444444444444444CDCC494444444",
  "..4995AC999449999994444444C44444CC44C4444449",
  "...3684999A499995999999999999999999999999999",
  ".....C4A99A4AAA99999999999999999999999999999",
  ".....499AA99AAAAAAAAAAAAA9AAA99AA9AAAAAA99AA",
  ".....C5AAA49AA55555555555555555555555555555A",
  ".....9AA554555555555555555555555555555555555",
  ".....9A5554555555555555555555555555555555555",
  ".....555554555555555555555555555555555555555",
  "....49AAAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
  "....777ZZZ7777777777777777777777777777777777",
  "....3333331222222222222222222222222222222222",
  "....2233322666666666666666666666666666666663",
  "....3666666666666666388888888888888888888888",
  "...DD322221111111211686666666666666666666666",
  "..D81222222111116668601111111111111111111110",
  "..80201DCCCCC6101000110000000000000000000000",
  "..2021D5555559121000110000000000001000000000",
  "..202145A55AAA361000111110000000010000000000",
  "..2021C9A555AA8C1000111110010000000110001001",
  "..2021D9AAAAA9681000111001222111000006888081",
  "..202264AAA9AC221211311228945944211167DDD37D",
  "..20162222222231100011000D955599000037D47377",
  "..30110111111101100011000D95A54400001DDDD1D3",
  ".C3111DAAAZZZA631000110102CC44C8000000000000",
  "883011DAAAZAAA621000111100000000000000000000",
  "236112DAZZZZZ4611000188D88DDDDDDDDDD88DDDDDD",
  "2628116DD86DDD200100133333333333333333333333",
  "26616888688888888886610000000000000000000000",
  "6DD82011222222221121211232222233332233322223",
  "8CD63000000000000000110000000000000000000000",
  "6DD36010000000000000110000000000000000000100",
  "6DD26001222222111000110100000000001000000000",
  "6DD82102222222123333312222223222333232222222",
  "38386688888888888866616888888888888888888888",
  "38662111111111111111211111111111111103BBBBBB",
  "1666216211111111111121611111111111121BBB8CCC",
  "0336221111111111111121011111111111121B8B8888",
  "001C866666666666666631666666666666631CCCCCCC",
  "1221111111111111111111111111111111102CCC44CB",
  "16662000000000000000111000000000000018C8CCCC",
  "1668610000000000000011100000000000002CC888C8",
  "1888631001223110000011000000000000000BBBBBBB",
  "133333612DD777D20000000000000000000000000000",
  "02211166277ZZ7Z10000122222222222222222122222",
  "111111182ZZZZZZ10000388668888866688888888888",
  ".3211111277ZZ7711111210000000000000000000000",
  ".0333333201111000000000000000000000000000000",
  ".02121121110000.............................",
  "..111111111000..............................",
  "..111112111100..............................",
  "..111111111100..............................",
  "..011111111100..............................",
  "...1000000010..............................."
];
const DRIVE_TRAFFIC_VAN={id:"ateam-van",name:"A-Team van",w:44,h:80,flare:"Z",
  pal:VAN_PAL,rows:VAN_ROWS};


let driveTraffic=[DRIVE_TRAFFIC_BUILTIN, DRIVE_TRAFFIC_VAN];
/* Mip set per traffic sprite. An oncoming car crosses the whole scale range in about
   a second — from a speck at the horizon to filling a third of the frame — so the
   same trick the palms needed applies: pre-scale, then blit near 1:1. */
const TRAFFIC_MIP_SC=[1.0,0.5,0.22,0.09];
const trafficMipCv={};
function trafficMips(t){
  if(trafficMipCv[t.id]) return trafficMipCv[t.id];
  const base=document.createElement("canvas");
  base.width=t.w*2; base.height=t.h;
  const c=base.getContext("2d");
  for(let y=0;y<t.h;y++){ const row=t.rows[y];
    for(let x=0;x<t.w;x++){ const ch=row[x]; if(ch==="."||ch===t.scanner) continue;
      c.fillStyle=t.pal[ch]||"#000"; c.fillRect(x,y,1,1); c.fillRect(t.w*2-1-x,y,1,1); } }
  trafficMipCv[t.id]=TRAFFIC_MIP_SC.map(sc=>{
    if(sc===1.0) return base;
    const cv=document.createElement("canvas");
    cv.width=Math.max(2,Math.round(t.w*2*sc)); cv.height=Math.max(2,Math.round(t.h*sc));
    const cc=cv.getContext("2d"); cc.imageSmoothingEnabled=false;
    cc.drawImage(base,0,0,cv.width,cv.height); return cv;
  });
  return trafficMipCv[t.id];
}
/* The scanner sweeps rather than pulsing: `phase` (0..1) walks a lit window across
   the bar. Driven by beat phase, so it is tied to the music like everything else —
   a timer here would be exactly the fake signal the house rule forbids. */
function drawTrafficSprite(ctx,t,cx,baseY,scale,phase){
  if(scale<=0.05) return;
  const sw=Math.max(2,Math.round(t.w*2*scale)), sh=Math.max(2,Math.round(t.h*scale));
  const x0=Math.round(cx-sw/2), y0=Math.round(baseY-sh);
  if(x0>=DRIVE_W || x0+sw<=0 || y0>=DRIVE_H) return;
  const set=trafficMips(t);
  let m=0; while(m<TRAFFIC_MIP_SC.length-1 && TRAFFIC_MIP_SC[m+1]>=scale) m++;
  ctx.drawImage(set[m],x0,y0,sw,sh);
  if(!t.scanner || scale<0.35) return;             // too small to resolve the sweep
  // scanner pixels, in the mirrored sprite, ordered by x
  const lit=[]; const W2=t.w*2;
  for(let y=0;y<t.h;y++){ const row=t.rows[y];
    for(let x=0;x<t.w;x++) if(row[x]===t.scanner){ lit.push([x,y]); lit.push([W2-1-x,y]); } }
  if(!lit.length) return;
  const minX=Math.min(...lit.map(p=>p[0])), maxX=Math.max(...lit.map(p=>p[0]));
  const head=minX+(maxX-minX)*phase, halfWin=(maxX-minX)*0.16+1;
  const px=Math.max(1,Math.round(scale)), py=Math.max(1,Math.round(scale));
  ctx.save();
  ctx.shadowColor="#ff2a1a"; ctx.shadowBlur=2+scale*6;
  for(const [x,y] of lit){
    const d=Math.abs(x-head)/halfWin;
    if(d>1) continue;
    const k=1-d*d;                                  // bright core, quick falloff
    ctx.fillStyle=driveMixColor("#c81208",k*0.75);
    ctx.fillRect(x0+Math.round(x*scale),y0+Math.round(y*scale),px,py);
  }
  ctx.restore();
}

/* ---------------- palm sprite: warm-lit, drawn for this scene ----------------
   Authored at hero size and scaled by road depth at draw time (one sprite, many
   distances) exactly as the arcade original did. Lit from the right; the mirrored
   copy is used on the far side of the road so both catch the same sun. */
const PALM_W=80, PALM_H=140;
const PALM_PAL={"B":"#36242c","C":"#7e5036","D":"#261928","F":"#182e24","G":"#708c4e","L":"#78584e","M":"#305236","R":"#d07c5a","T":"#503a3e"};
const PALM_ROWS=[
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  "................................................................................",
  ".............................FFFF....FFFFF......................................",
  ".........................FFFFFFFFFFFFFFFFFFFF...................................",
  "........................FFFFFFFFFFFFFFFFFFFFFF..................................",
  ".......................FFFFFFFMMFFFFFFFMMFFFFFFF................................",
  ".....................FFFFFFFMMFFFFFGMFFFGGFFFFFFF...............................",
  "....................FFFFMFFFMFFMMMMMMMFGGGMFFMMFFF..............................",
  "....................FFFMFFFMFFFMMMMMMMFFGGGGFFGMFFF.............................",
  "..................FFFMMFFFMMFFFMMMMMMMFFGGGGFFGGMMFF............................",
  ".................FFFMMFFFFMFFFFMMMMMMMFGGGGGGFGGGMGFFF..........................",
  "................FFFMMFFFFMMFFFFMMMMMMMFGGGGGGFGGGGMFFF..........................",
  "...............FFFFMFFFFFMFFFFFCCMFGGCCGGGGGGGFGGGGGFFFF........................",
  "..............FFFFMFFFFFFMFFFFFCCFFGGCCGGGGGGGFFFGGGGFFFF.......................",
  ".............FFFFMFFFFMFMFFFFF.FMFCCGGF..GGGGGFMFFGGGGFFFF......................",
  "............FFFFMMFFFMMFMFFF....MDCCBGR....GGGGFMFFGGGFGFFF.....................",
  "............FFMFMFFFMMFFMF......MDDTTGL.....GGGGGMFFGGGFGF......................",
  "...........FFMFMFFFMMFFMM.......MDDTTGL......GGGGGMFFGGFGGF.....................",
  "...........FMFFMFFFMFFFM........MDDBTTG.......GGGGGGFFGGFGGF....................",
  "..........FMMFMFFFMF.F.M.........DDTTTL........G.GGGGFGGGGGGF...................",
  ".........FFMFMMFFMFFF..M.........DDBBTR........G..GGGGFFG.GGFF..................",
  "........FFMF.MFFMFFF..............DDTTTL...........GGMFFGGGGGF..................",
  "........FMFF.MFMMFF...............DDTTTL............GGMFFG.GGGF.................",
  ".......FMMF.MFFMFF................DDBTTL.............GGMFGG.GGF.................",
  "......FFMF..MFMFFF................DDTTTL.............GGGMFG..GGF................",
  ".......MF...FMFFF.................DDBBTR.............GGGGFF..GGG................",
  "......MM....FMFF..................DDTTTL..............G.GGF....G................",
  "......M....FMFF...................DDTTTTL...............GGFF....G...............",
  ".....M....FFMF....................DDBTTTL................GGF....G...............",
  ".....M....FMF.....................DDTTTTL................GGGF....G..............",
  "....M....FMFF.....................DDBBBTR.................GGF....G..............",
  ".........FMF......................DDTTTTL..................GGF..................",
  "........FMFF......................DDTTTTL..................GGF..................",
  "........FMF.......................DDBTTTL...................GGF.................",
  ".......FMF........................DDTTTTL...................GGF.................",
  ".......MMF.........................DDBBBTR...................GG.................",
  ".......M...........................DDTTTTL....................G.................",
  "......MM...........................DDTTTTL.....................G................",
  "......M............................DDBTTTL.....................G................",
  "......M............................DDTTTTL......................G...............",
  ".....M.............................DDBBBTR......................G...............",
  ".....M.............................DDTTTTL.......................G..............",
  "...................................DDTTTTL......................................",
  "...................................DDBTTTL......................................",
  "...................................DDTTTTL......................................",
  "...................................DDBBBBTR.....................................",
  "...................................DDTTTTTL.....................................",
  "...................................DDTTTTTL.....................................",
  "...................................DDBTTTTL.....................................",
  "...................................DDTTTTTL.....................................",
  "...................................DDBBBBTR.....................................",
  "...................................DDTTTTTL.....................................",
  "...................................DDTTTTTL.....................................",
  "...................................DDBTTTTL.....................................",
  "...................................DDTTTTTL.....................................",
  "...................................DDBBBBTR.....................................",
  "....................................DDTTTTTL....................................",
  "....................................DDTTTTTL....................................",
  "....................................DDBTTTTL....................................",
  "....................................DDTTTTTL....................................",
  "....................................DDBBBBTR....................................",
  "....................................DDTTTTTL....................................",
  "....................................DDTTTTTL....................................",
  "...................................DDBTTTTTL....................................",
  "...................................DDTTTTTTL....................................",
  "...................................DDBBBBBTR....................................",
  "...................................DDTTTTTTL....................................",
  "...................................DDTTTTTTL....................................",
  "...................................DDBTTTTTL....................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDBBBBBTR...................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDBTTTTTL...................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDBBBBBTR...................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDBTTTTTL...................................",
  "....................................DDTTTTTTL...................................",
  "....................................DDBBBBBTR...................................",
  "...................................DDTTTTTTTL...................................",
  "...................................DDTTTTTTTL...................................",
  "...................................DDBTTTTTTL...................................",
  "...................................DDTTTTTTTL...................................",
  "...................................DDBBBBBBTR...................................",
  "...................................DDTTTTTTTL...................................",
  "...................................DDTTTTTTTL...................................",
  "...................................DDBTTTTTTL...................................",
  "...................................DDTTTTTTTL...................................",
  "...................................DDBBBBBBTR...................................",
];

/* Pre-rendered palm, drawn once at 1x then blitted scaled — one drawImage per palm
   instead of ~10k fillRects, which is what makes per-depth scaling affordable. */
let palmCvA=null, palmCvB=null, palmMips=[null,null];
function palmCanvas(flip){
  if(flip ? palmCvB : palmCvA) return flip?palmCvB:palmCvA;
  const cv=document.createElement("canvas"); cv.width=PALM_W; cv.height=PALM_H;
  const c=cv.getContext("2d");
  for(let y=0;y<PALM_H;y++){ const row=PALM_ROWS[y];
    for(let x=0;x<PALM_W;x++){ const ch=row[x]; if(ch===".") continue;
      c.fillStyle=PALM_PAL[ch]; c.fillRect(flip?PALM_W-1-x:x,y,1,1); } }
  if(flip) palmCvB=cv; else palmCvA=cv;
  return cv;
}
/* ---- curved trunks ----
   Real palms lean; a straight column reads as a telegraph pole. Rather than redraw
   the art, each lean is BAKED once by re-blitting the base sprite row by row with a
   quadratic x-offset: zero at the base (the tree is planted) rising to the crown, so
   the trunk bows and carries the canopy with it. Baking beats shearing at draw time —
   a sheared palm would cost ~140 drawImages every frame instead of one.
   The canvas is padded either side so a leaning crown cannot clip. */
const PALM_LEAN_STEPS=[-1,-0.55,0,0.55,1], PALM_LEAN_PX=13;
const PALM_PAD=PALM_LEAN_PX, PALM_CW=PALM_W+PALM_PAD*2;
const palmLeanCv={};
function palmLeanedCanvas(flip,li){
  const key=(flip?"b":"a")+li;
  if(palmLeanCv[key]) return palmLeanCv[key];
  const base=palmCanvas(flip), lean=PALM_LEAN_STEPS[li];
  const cv=document.createElement("canvas"); cv.width=PALM_CW; cv.height=PALM_H;
  const c=cv.getContext("2d"); c.imageSmoothingEnabled=false;
  for(let y=0;y<PALM_H;y++){
    const t=(PALM_H-1-y)/(PALM_H-1);            // 0 at the base, 1 at the crown
    const dx=Math.round(lean*PALM_LEAN_PX*t*t); // quadratic: the bow tightens toward the top
    c.drawImage(base,0,y,PALM_W,1, PALM_PAD+dx,y,PALM_W,1);
  }
  palmLeanCv[key]=cv; return cv;
}
/* Pre-scaled copies per (flip, lean). Most palms are distant, and shrinking the full
   source down to ~10px every frame is the costly direction for drawImage; picking a
   near-matching mip keeps every blit close to 1:1. */
const PALM_MIP_SC=[1.0,0.45,0.2,0.08];  // 0.08 for the deep far field
const palmMipCv={};
function palmMipSet(flip,li){
  const key=(flip?"b":"a")+li;
  if(palmMipCv[key]) return palmMipCv[key];
  const base=palmLeanedCanvas(flip,li);
  palmMipCv[key]=PALM_MIP_SC.map(sc=>{
    if(sc===1.0) return base;
    const cv=document.createElement("canvas");
    cv.width=Math.max(2,Math.round(PALM_CW*sc)); cv.height=Math.max(2,Math.round(PALM_H*sc));
    const c=cv.getContext("2d"); c.imageSmoothingEnabled=false;
    c.drawImage(base,0,0,cv.width,cv.height); return cv;
  });
  return palmMipCv[key];
}
function drawPalm(ctx,cx,baseY,scale,flip,li){
  if(scale<=0.06) return;
  li=li||0;
  const sw=Math.max(2,Math.round(PALM_CW*scale)), sh=Math.max(2,Math.round(PALM_H*scale));
  const x0=Math.round(cx-sw/2), y0=Math.round(baseY-sh);
  if(x0>=DRIVE_W || x0+sw<=0 || y0>=DRIVE_H) return;
  const set=palmMipSet(flip,li);
  let m=0; while(m<PALM_MIP_SC.length-1 && PALM_MIP_SC[m+1]>=scale) m++;
  ctx.drawImage(set[m], x0, y0, sw, sh);
}


/* The Bayer dither draws one fillRect PER PIXEL, so a full-width sky costs ~29k
   calls a frame. It only depends on the palette, so bake it once and blit. */
let skyCv=null, skyKey="";
function skyCanvas(W,h,top,bot,key){
  if(skyCv && skyKey===key+"|"+W+"x"+h) return skyCv;
  const cv=document.createElement("canvas"); cv.width=W; cv.height=h;
  driveDitherV(cv.getContext("2d"),0,0,W,h,top,bot);
  skyCv=cv; skyKey=key+"|"+W+"x"+h; return cv;
}
/* Same for the car: only the amber indicators change per frame, so bake the body
   (both halves) once and redraw just the indicator pixels on top. */
let carBodyCv=null;
function carBodyCanvas(){
  if(carBodyCv) return carBodyCv;
  const car=driveCar(), W=car.w, H=car.h, FL=car.flare;
  const cv=document.createElement("canvas"); cv.width=W*2; cv.height=H;
  const c=cv.getContext("2d");
  for(let y=0;y<H;y++){ const row=car.rows[y];
    for(let x=0;x<W;x++){ const ch=row[x]; if(ch==="."||ch===FL) continue;
      c.fillStyle=car.pal[ch]||"#000"; c.fillRect(x,y,1,1); c.fillRect(W*2-1-x,y,1,1); } }
  carBodyCv=cv; return cv;
}

/* ---------------- OutRun (pseudo-3D drive) — the whole game now ---------------- */
/* sunTop -> sunBot is a vertical gradient across the disc. A single flat yellow read
   as midday; an evening sun deepens toward the horizon, gold at the crown into ember
   where it meets the ground, which also ties it to the magenta sky below. */
const DRIVE_PAL_SUNSET={key:"sunset",skyTop:"#2a0a3e",skyBot:"#ff2e7e",
  sunTop:"#ffd066",sunBot:"#ff5a3c",sunGlow:"#ff9a3d",
  palmDark:"#160a2e",palmDark2:"#3a1560",roadTop:"#1a0630",roadBot:"#05010a",
  roadStripeA:"#43324f",roadStripeB:"#372743",laneMark:"#ffe14d",laneDash:"#cfc6dd",carBody:"#c81e5a",carBodyHi:"#ff5ec7"};
const DRIVE_PAL_BOSS={key:"boss",skyTop:"#1a0208",skyBot:"#5e0a1a",
  sunTop:"#ffa83c",sunBot:"#e0231f",sunGlow:"#ff2e2e",
  palmDark:"#2a0505",palmDark2:"#5e0a1a",roadTop:"#3a0a12",roadBot:"#0a0204",
  roadStripeA:"#5e2430",roadStripeB:"#3a1620",laneMark:"#ff2e2e",laneDash:"#e0b4b4",carBody:"#7a0a1a",carBodyHi:"#ff2e2e"};
/* Road width at the bottom of the frame, as a fraction of it. This is the number
   that decides whether roadside scenery has anywhere to stand: at .9 the verge was
   16px a side, so a full-size palm was pushed out of shot before it could grow.
   The arcade sits nearer .72 and spends the rest on verge — which is why its near
   palms can be frame-height and still visible. The palm placement below reads the
   same constant, so trees always line up with the road edge. */
const ROAD_FRAC=0.72;
const ROAD_LANES=3;   // odd, so the player's lane is the centre one — see the divider loop
const SUN_R=42;   // was 22 — the disc now fills ~75% of the sky, as it does in the poster art
/* How fast the world comes at the camera, in z-units per scroll unit. ONE dial for
   the whole scene: the road texture and the roadside scenery both scroll by it, so
   they can never disagree about how fast you are moving. Raise it and everything
   speeds up together; parallax stays honest because near things still sweep faster
   than far ones, purely from the 1/z projection. */
const WORLD_RATE=0.45;
/* Length of one road band in z-units. At 1 the road texture repeated every single
   z-unit while trees sat 6-11 apart, so you passed 7-11 bands per tree: identical
   scroll speed, but a texture ~10x finer, which reads as the road streaking past
   stationary trees. Worse, at loud/fast passages that was 32 bands/sec — a band every
   1.9 frames at 60fps, under-sampled enough to buzz rather than move. At 3 the road
   passes ~3x per tree, which the eye reads as one coherent world. */
const ROAD_BAND_Z=3.0;
function drawOutrun(ctx,accent,ox,palette){
  const W=DRIVE_W,H=DRIVE_H;
  const oxRaw=ox;    // palms need the unrounded value: they advance ~0.45 z-units per scroll
                     // unit, so quantising ox first would step them in visible 10%-of-a-gap jerks
  ox=Math.round(ox); // snap the scroll offset to an integer buffer pixel before it drives any position below
  // beat punch lives entirely in world elements below — never a whole-frame translation.
  // REDUCE damps it to a near-static trickle rather than killing it outright.
  const beatK=REDUCE?drivePulse*0.15:drivePulse;
  ctx.drawImage(skyCanvas(W,H*.5|0,palette.skyTop,palette.skyBot,palette.key),0,0);
  const hz=H*.5|0; // road dither removed: the verge + road loop paint every pixel below the horizon anyway
  // Stars before the sun, so they never speckle the disc.
  driveStars(ctx,0,0,W,H*.22|0,hash(palette.key+"stars"),50,"#fff","#7de8ff");
  // ---- setting sun ----
  // Centre sits ABOVE the horizon by only part of the radius, so the disc runs past
  // hz and the road loop below paints over it — the sun is genuinely cut by the
  // horizon rather than floating above it.
  const sunR=SUN_R+beatK*3, sx=W/2, sy=hz-sunR*0.55;
  ctx.save(); ctx.shadowColor=palette.sunGlow; ctx.shadowBlur=18+beatK*14;
  // Ramp across the VISIBLE disc (crown -> horizon), not the full diameter. The sun
  // is cut by hz, so a full-diameter ramp spends its ember end below the horizon
  // where nothing can see it — the sun then reads flat gold, which is the midday
  // look we were trying to get away from.
  const sunG=ctx.createLinearGradient(0,sy-sunR,0,hz);
  // the beat lifts both stops toward white, so the flare keeps the gradient's shape
  sunG.addColorStop(0, beatK>0.04?driveMixColor(palette.sunTop,beatK*0.35):palette.sunTop);
  sunG.addColorStop(1, beatK>0.04?driveMixColor(palette.sunBot,beatK*0.35):palette.sunBot);
  ctx.fillStyle=sunG;
  ctx.beginPath(); ctx.arc(sx,sy,sunR,0,Math.PI*2); ctx.fill(); ctx.restore();
  drawSunBands(ctx,sx,sy,sunR,palette.skyTop,sm.high);
  // How far the world has travelled toward us, in the same z-units the palms use.
  // This is a CONTINUOUS offset added to depth BEFORE the floor, so the band pattern
  // slides through the bands. The old code added an integer phase inside the %2,
  // which inverted every band at once instead of moving it — the road strobed rather
  // than flowed, reading as much faster than the scenery it was supposed to match.
  const scrollZ=oxRaw*WORLD_RATE;
  const edgeCol=beatK>0.04?driveMixColor(pal.ac,beatK*0.4):accent; // road edges brighten toward the album accent on the beat
  for(let y=hz;y<H;y++){ const tt=(y-hz)/(H-hz);
    const roadW=Math.round(8+(W*ROAD_FRAC-8)*tt), bend=Math.sin(tt*3+ox*.03)*tt*(6+sm.low*10)+balSmooth*tt*26;
    // Road segments are spaced evenly in DEPTH, not in screen space. For a ground
    // plane, depth is proportional to 1/(y-horizon), so bands compress toward the
    // horizon like a real receding road. Banding on tt directly gave equal-height
    // stripes, which read as flat lines painted on a wall.
    // Depth constant sets how many road segments are visible. At 1.2 the reciprocal
    // flattens near the camera and floor(depth) stepped only once across the whole
    // bottom third, giving 20px+ dashes and dead road under the car. 6.0 keeps the
    // same 1/tt perspective but cycles ~13px bands near the viewer, ~3px mid-road.
    // The old floor of 0.12 pinned depth to a single value across the top ~11 rows,
    // so floor(depth+scrollZ)%2 flipped that whole slab as ONE block — the vanishing
    // point visibly blinked between the two stripe colours. The floor only existed to
    // hide aliasing where bands fall below a pixel, which is better solved with haze:
    // let depth keep growing, and fade the road's detail out before it can alias.
    const depth=6.0/Math.max(tt,0.02);
    const hazeK=Math.min(1,tt/0.20);   // 0 at the horizon, full detail ~20% down
    const cxr=W/2+bend, rl=Math.floor(cxr-roadW/2), stripe=Math.floor((depth+scrollZ)/ROAD_BAND_Z)%2===0;
    // ground verge either side — palms need a surface to stand on. Deliberately a
    // WARM tone against the road's cool purple, so the road reads as a ribbon
    // crossing ground rather than the two merging into one grey mass.
    const vgR=Math.round(18+34*tt), vgG=Math.round(12+22*tt), vgB=Math.round(16+26*tt);
    P(ctx,0,y,W,1,`rgb(${vgR},${vgG},${vgB})`);
    // Everything on the road surface fades into roadTop toward the horizon, so the
    // bands, the edge lines and the lane dashes all dissolve into haze instead of
    // converging into a flickering blob at the vanishing point.
    P(ctx,rl,y,roadW,1, driveBlend(palette.roadTop, stripe?palette.roadStripeA:palette.roadStripeB, hazeK));
    const eC=driveBlend(palette.roadTop,edgeCol,hazeK);
    P(ctx,rl,y,2,1,eC); P(ctx,rl+roadW-2,y,2,1,eC);
    // Lane dividers. THREE lanes, deliberately an odd number: it puts the player's
    // lane in the middle, so the car stays dead centre in frame. Four lanes would be
    // more realistic but would shove it ~29px off-centre and change the composition
    // the whole mode is built around. The outer lanes are the oncoming carriageway
    // and (later) same-direction traffic.
    if(stripe && hazeK>0.12){
      const dash=driveBlend(palette.roadTop,palette.laneDash,hazeK);
      for(let i=1;i<ROAD_LANES;i++){
        const f=i/ROAD_LANES-0.5;
        P(ctx,Math.floor(cxr+f*roadW),y,1,1,dash);
      }
    }
  }
  // ---- roadside palms ----
  // Palms are no longer a fixed evenly-spaced array. Each one was PLANTED by a real
  // beat (see plantPalm) at a world position, and is drawn wherever the camera has
  // got to since. Consequences that the old array could not give us:
  //   - spacing IS the tempo — dense music grows a dense avenue, sparse music leaves
  //     long empty stretches, and it is never the same twice
  //   - trees stand on ONE side, and which side follows the real stereo balance, so
  //     the avenue leans with the mix instead of being a mirrored tunnel
  //   - size, distance from the verge and trunk lean vary per palm, so an occasional
  //     one looms much larger than its neighbours — the big palm in the arcade shot
  // Projection is unchanged: p = 6/z, exactly as the road uses, unclamped so a palm
  // leaves by going wide and large rather than swelling in place.
  // Drawn far-to-near — the list is in plant order, so iterating backwards is
  // farthest-first and near palms correctly overlap the ones behind them.
  const palms=driveState.palms;
  for(let i=palms.length-1;i>=0;i--){
    const pm=palms[i];
    const z=pm.z-scrollZ;
    if(z<=1.0) continue;   // fully behind the camera
    const p=6.0/z;
    if(p>1.8) continue;  // provably outside the frame from p~1.3; skip the sprite maths
    // Same haze curve as the road surface, driven by the same value (p IS tt while
    // p<=1), so a palm and the road it stands on fade together rather than the trees
    // staying crisp against a hazed road.
    const palmHaze=Math.min(1,p/0.20);
    if(palmHaze<0.08) continue;          // too faint to see — skip before any sprite work
    const py=hz+p*(H-hz);
    // The bend still tracks the visible road, which only exists for p<=1, so the
    // curve term saturates there while position and scale keep going.
    const bt=Math.min(1,p);
    const bend=Math.sin(bt*3+ox*.03)*bt*(6+sm.low*10)+balSmooth*bt*26;
    const cxr=W/2+bend;
    // Margin kept tight (8*p, not 20*p): in the arcade the near palm HUGS the frame
    // edge at full size rather than being flung out of shot. pm.lat pushes individual
    // trees further back off the verge so the line is not perfectly straight.
    const off=((8+(W*ROAD_FRAC-8)*p)/2+8*p)*pm.lat;
    const sc=p*1.25*pm.sc;             // near palms read at ~175px in a 180px buffer, as in OutRun
    if(palmHaze<1) ctx.globalAlpha=palmHaze;
    drawPalm(ctx,cxr+pm.side*off,py,sc,pm.side>0,pm.li);
    if(palmHaze<1) ctx.globalAlpha=1;
  }
  // ---- oncoming traffic ----
  // Drawn after the palms and before the player car, so a passing vehicle is
  // occluded by our own bumper at the moment it goes by. Far-to-near within the list.
  for(let i=driveState.traffic.length-1;i>=0;i--){
    const v=driveState.traffic[i];
    const z=v.z-scrollZ;
    if(z<=0.6) continue;
    const p=6.0/z;
    if(p>2.4) continue;                       // past the camera
    const haze=Math.min(1,p/0.20);
    if(haze<0.08) continue;
    const py=hz+p*(H-hz);
    const bt=Math.min(1,p);
    const bend=Math.sin(bt*3+ox*.03)*bt*(6+sm.low*10)+balSmooth*bt*26;
    const roadW=8+(W*ROAD_FRAC-8)*p;
    const cx=W/2+bend+v.lane*roadW;           // lane offset scales with the road
    const sc=(TRAFFIC_ROAD_SHARE*roadW)/(v.sprite.w*2);
    // Scanner phase from BEAT phase, so the sweep is musical: one full pass per beat.
    const bpm=driveLocalBpm()||120;
    const phase=((posSec*bpm/60)%1+1)%1;
    if(haze<1) ctx.globalAlpha=haze;
    drawTrafficSprite(ctx,v.sprite,cx,py,sc,phase);
    if(haze<1) ctx.globalAlpha=1;
  }

  // speed lines — a steady base crawl plus a clear burst on the beat (length + brightness both track it)
  const burstLen=8+(playing?sm.amp*8:0)+beatK*16;
  ctx.globalAlpha=.35+beatK*.5;
  for(let k=0;k<9;k++){ const yy=hz+(k*4)+2; P(ctx,2,yy,burstLen,1,"#7de8ff"); P(ctx,W-2-burstLen,yy,burstLen,1,"#7de8ff"); }
  ctx.globalAlpha=1;
  const cx=Math.round(W/2+balSmooth*16), baseY=H-6;
  drawCarSprite(ctx,cx,baseY,accent,beatK);
}

/* Horizontal bands cut INTO the sun. The old version drew every band at the disc's
   full diameter, so they overshot the circle top and bottom and read as bars laid
   over the sun. Each row is now clipped to the circle's true half-width at that row,
   sqrt(r^2 - dy^2), so the bands belong to the disc.
   Bands thicken and their gaps tighten toward the bottom, so the sun dissolves into
   the horizon instead of ending on a hard edge — the synthwave convention.
   `high` shifts the whole stack a couple of pixels: the layout is fixed artwork, but
   the shimmer on it stays tied to real treble rather than to a timer. */
function drawSunBands(ctx,sx,sy,r,col,high){
  let y=sy-r*0.10+(high||0)*2.5, th=1, gap=5.2;
  while(y<sy+r){
    const n=Math.round(th);
    for(let k=0;k<n;k++){
      const yy=Math.round(y)+k, dy=yy-sy;
      if(Math.abs(dy)>=r) continue;
      const hw=Math.sqrt(r*r-dy*dy);
      P(ctx,Math.round(sx-hw),yy,Math.max(1,Math.round(hw*2)),1,col);
    }
    y+=th+gap; th+=0.7; gap=Math.max(1.6,gap-0.45);
  }
}

/* ---------------- title screen ---------------- */
function drawTitle(ctx,t){
  const W=DRIVE_W,H=DRIVE_H;
  driveDitherV(ctx,0,0,W,H*.62|0,"#2a0a3e","#0a0420");
  driveStars(ctx,0,0,W,H*.4|0,hash("title-stars"),90,"#ffd0f0","#7de8ff");
  const sunc="#ff9b3d", sx=W/2, sy=H*.42;
  ctx.save(); ctx.shadowColor="#ff2e7e"; ctx.shadowBlur=16; ctx.fillStyle=sunc;
  ctx.beginPath(); ctx.arc(sx,sy,20,0,Math.PI*2); ctx.fill(); ctx.restore();
  for(let i=0;i<9;i++){ const yy=(sy-14+i*3.5)|0; if(yy>sy) P(ctx,(sx-20)|0,yy,40,i-2,"#0a0420"); }
  driveDitherV(ctx,0,sy|0,W,3,"#ffe14d","#ff2e7e");
  const base=H*.62|0; driveDitherV(ctx,0,base,W,H-base,"#2a0a3e","#08040c");
  ctx.strokeStyle=driveWithA("#ff5ec7",.7); ctx.lineWidth=1;
  for(let i=-10;i<=10;i++){ ctx.beginPath(); ctx.moveTo(W/2,base); ctx.lineTo(W/2+i*10,H); ctx.stroke(); }
  for(let y=base;y<H;y+=4){ ctx.globalAlpha=.6; ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(W,y); ctx.stroke(); ctx.globalAlpha=1; }
  ctx.save(); ctx.shadowColor=DRIVE_TEAL; ctx.shadowBlur=9; P(ctx,0,base,W,1,DRIVE_TEAL); ctx.restore();
  drawByte(ctx,W*.5,base-10,1.8,DRIVE_TEAL,"idle",0);
  const panelX=W/2-92, panelY=H*.12, panelW=184, panelH=40;
  ctx.fillStyle="#0a0420"; ctx.fillRect(panelX,panelY,panelW,panelH);
  ctx.strokeStyle=DRIVE_TEAL; ctx.lineWidth=1; ctx.strokeRect(panelX,panelY,panelW,panelH);
  const title=(A.status.title||"DRAAI").toUpperCase(), artist=driveArtistUC();
  const tt=driveFitText(title,panelW-16,2);
  driveTextC(ctx,tt.text,W/2,panelY+7,"#ffe14d",tt.scale);
  const sub=artist?("— "+artist+" —"):"— DRAAI —"; const st=driveFitText(sub,panelW-16,1);
  driveTextC(ctx,st.text,W/2,panelY+25,"#7de8ff",st.scale);
  ctx.globalAlpha= REDUCE ? .92 : (Math.sin(t/260)*.5+.5)*.5+.5;
  driveTextC(ctx,"▶ PRESS PLAY",W/2,H*.92-4,"#fff",1);
  ctx.globalAlpha=1;
}

/* ---------------- high-score end screen ---------------- */
function drawEnd(ctx){
  const W=DRIVE_W,H=DRIVE_H;
  driveDitherV(ctx,0,0,W,H,"#140a2e","#04040c");
  driveStars(ctx,0,0,W,H,hash("end-stars"),120,"#fff","#7de8ff");
  drawByte(ctx,W*.5,H*.14,1.5,DRIVE_TEAL,"idle",0);
  ctx.save(); ctx.shadowColor="#ff2e7e"; ctx.shadowBlur=10;
  const jc=driveFitText("JOURNEY COMPLETE",W*.86,2);
  driveTextC(ctx,jc.text,W/2,H*.26-6,"#ffe14d",jc.scale); ctx.restore();
  const meta=`${(A.status.title||"—").toUpperCase()} · ${driveArtistUC()||"DRAAI"} · ${fmt(durSec)}`;
  const mt=driveFitText(meta,W*.9,1);
  driveTextC(ctx,mt.text,W/2,H*.34-3,"#7de8ff",mt.scale);
  const tempo=(analysis && typeof analysis.bpm==="number") ? Math.round(analysis.bpm)+" BPM" : "—";  // whole-track median: correct for a summary
  const rows=[
    ["DISTANCE DRIVEN", Math.round(driveTelemetry.distanceM).toLocaleString()+" M"],
    ["PEAK LEVEL", Math.round(driveTelemetry.peakPct)+"%"],
    ["TEMPO", tempo],
    ["TOP SPEED", Math.round(driveTelemetry.topSpeed)+" KM/H"],
    ["BEATS MATCHED", driveTelemetry.beats+""],
  ];
  const tx=W*.24|0, tw=W*.52|0, ty=H*.42|0, rh=13;
  driveDitherV(ctx,tx,ty,tw,rows.length*rh+8,"#1a1040","#0a0620");
  ctx.strokeStyle="#3b2a6a"; ctx.lineWidth=1; ctx.strokeRect(tx,ty,tw,rows.length*rh+8);
  rows.forEach((row,i)=>{ const ry=ty+9+i*rh;
    if(i%2) P(ctx,tx+1,ry-6,tw-2,rh-1,"rgba(255,255,255,0.03)");
    driveText(ctx,row[0],tx+6,ry-5,"#8b93c7",1);
    driveTextR(ctx,row[1],tx+tw-6,ry-5,"#ffe14d",1); });
  ctx.globalAlpha=.9;
  driveTextC(ctx,driveTelemetry.bossCleared?"★ BOSS ARENA CLEARED ★":"▶ PLAY AGAIN",W/2,H*.9-3,DRIVE_TEAL,1);
  ctx.globalAlpha=1;
}

/* ---------------- persistent HUD ---------------- */
function drawHud(ctx,label,level){
  const x=6,y=6,pad=4,capW=200,rowGap=2,barGap=3,barH=3;
  const title=(A.status.title||"DRAAI").toUpperCase();
  // Tempo readout. These are three genuinely different states and the first version of
  // this label collapsed them all into "NO GRID", which pointed at the wrong culprit:
  //   ANALYSING — no analysis yet (still running, or ffmpeg missing). Nothing is
  //               music-driven at all; energyAt returns null and every band reads 0.
  //   n BPM     — a tempo was fitted; the pulse follows that grid.
  //   ONSETS    — analysis is ready but tempo confidence was too low, so the engine
  //               returned raw onsets instead of a grid. The pulse still tracks real
  //               hits, but there is no tempo, so nothing can scale with BPM.
  const bpm = !analysis ? "ANALYSING"
    : (driveLocalBpm()>0) ? Math.round(driveLocalBpm())+" BPM"
    : (Array.isArray(analysis.beats) && analysis.beats.length) ? "ONSETS" : "NO BEATS";
  const sub=fmt(posSec)+"/"+fmt(durSec)+"  ·  "+label+"  ·  "+bpm;
  const tt=driveFitText(title,capW-pad*2,1);
  const st=driveFitText(sub,capW-pad*2,1);
  const titleH=DRIVE_GLYPH_H*tt.scale, subH=DRIVE_GLYPH_H*st.scale;
  // panel hugs the actual fitted text width (capped so it can never overrun the buffer), not a fixed guess
  const innerW=Math.max(driveTextWidth(tt.text,tt.scale),driveTextWidth(st.text,st.scale))+pad*2;
  const panelH=pad+titleH+rowGap+subH+barGap+barH+pad; // sized to fit the bitmap-font metrics, not left overflowing
  ctx.fillStyle="rgba(6,10,18,.55)"; ctx.fillRect(x,y,innerW,panelH);
  let ty=y+pad;
  driveText(ctx,tt.text,x+pad,ty,driveWithA(DRIVE_TEAL,.9),tt.scale);
  ty+=titleH+rowGap;
  driveText(ctx,st.text,x+pad,ty,driveWithA(DRIVE_TEAL,.6),st.scale);
  ty+=subH+barGap;
  for(let i=0;i<5;i++){ const on=(i/5)<level;
    ctx.fillStyle= on?driveWithA(DRIVE_TEAL,.95):driveWithA(DRIVE_TEAL,.2);
    ctx.fillRect(x+pad+i*5,ty,4,barH); }
}

/* ---- planting palms on the beat ----
   A palm is planted far up the road on every detected beat, then simply recedes as
   the world scrolls past it. WHEN a tree exists is therefore a real musical event;
   only its dressing (which side, how big, how far off the verge, how much it leans)
   comes from a seeded PRNG keyed on the palm's own index. That keeps the house rule
   intact: the PRNG decides appearance, never timing.
   SPAWN_Z is far enough that a new palm enters as a speck (p=6/120 = 0.05) rather
   than popping in at a visible size. */
const PALM_SPAWN_Z=120, PALM_LIMIT=70;
/* Integer hash (splitmix-style finalizer) -> 0..1. A plain LCG seeded with the palm
   index gave visibly CORRELATED draws across consecutive palms — lateral offset
   drifted 1.30, 1.29, 1.27, 1.26 down the avenue instead of scattering, because
   Park-Miller's early outputs track nearby seeds. Hashing each field on its own key
   decorrelates both between palms and between a palm's own properties. */
function driveHash01(n){
  n=(n^61)^(n>>>16); n=(n+(n<<3))|0; n^=n>>>4;
  n=Math.imul(n,0x27d4eb2d); n^=n>>>15;
  return (n>>>0)/4294967296;
}
/* How often you PASS a tree is (scroll speed / spacing), and the two bounds below
   have to be in the right units or they quietly take over from the music.
     GAP_MIN is a DISTANCE — correct, because its job is stopping sprites overlapping,
       which is a spatial problem.
     The density floor is a TIME. It used to be a distance (GAP_MAX, 6 z-units) and
       that was wrong: a distance floor divided by speed yields a rate that tracks
       SPEED — i.e. loudness — not tempo. Measured, a 90 BPM and a 200 BPM track both
       produced 3.58 trees/sec on loud passages, identical, because the floor was
       out-planting the beat in both. A time floor gives a fixed 1.33 trees/sec, so
       the beat wins for anything above ~80 BPM and the avenue really does thicken
       with tempo.
   The floor is still the one tree that is not a musical event; it only exists so a
   sparse passage does not leave an empty road. */
const PALM_GAP_MIN=2.4, PALM_FILL_S=0.75;
function plantPalm(atZ){
  if(driveState.palms.length>=PALM_LIMIT) return;   // safety valve for very dense material
  const z=(atZ==null)?driveState.scrollNear*WORLD_RATE+PALM_SPAWN_Z:atZ;
  if(z-driveState.lastPlantZ<PALM_GAP_MIN) return;  // too close behind the previous tree
  const n=driveState.palmSeq++, h=k=>driveHash01(n*4+k);
  // Which side a tree grows on follows the real stereo image: balSmooth is the
  // smoothed (R-L)/(R+L) energy balance, so a mix leaning right grows more trees on
  // the right. The hash supplies the dither, which matters because most music sits
  // near centre — without it a centred mix would have no way to alternate. Clamped
  // well short of 0/1 so even a hard-panned passage still plants on both sides and
  // the avenue never becomes a one-sided wall.
  const pRight=clamp(0.5+balSmooth*1.6,0.12,0.88);
  driveState.palms.push({
    z,
    side: h(0)<pRight?1:-1,
    sc:   0.78+h(1)*0.62,                           // not all trees are the same height
    lat:  0.92+h(2)*0.42,                           // nor all the same distance off the verge
    li:   Math.min(PALM_LEAN_STEPS.length-1,Math.floor(h(3)*PALM_LEAN_STEPS.length)),
  });
  driveState.lastPlantZ=z;
}
/* One oncoming vehicle, launched far up the road. Its own z DECREASES as it drives
   toward us while scrollZ increases as we drive at it, so camera depth closes at the
   sum of both speeds. `lane` is negative: the opposite carriageway, to our left. */
/* Vehicles are sized by the share of ROAD they occupy, not by a scale factor applied
   to the sprite. The player car is 72px against a 230px road = 31%, and every
   oncoming vehicle matches that share, so width is consistent while height follows
   each sprite's own aspect — a van is as wide as a car and much taller, which is
   correct. A fixed scale factor would have made the boxy van (aspect 1.09) come out
   the wrong size next to KITT (1.79). */
const TRAFFIC_ROAD_SHARE=0.31;
/* Pacing. A vehicle is only NOTICEABLE for ~1s of its approach — the 1/z projection
   means it spends most of its life as a speck and then arrives all at once. That is
   what oncoming traffic actually looks like, so the answer is frequency, not slowing
   it down. One per section boundary alone worked out at a sub-second event once a
   minute, which read as "no traffic".
   The interval is counted in BEATS, not seconds, so it stays musical: faster music
   puts more cars on the road. Section boundaries still force one, so you also get a
   car on the drop. */
const TRAFFIC_SPAWN_Z=150, TRAFFIC_SPEED=14, TRAFFIC_LIMIT=5;
const TRAFFIC_BEATS=8, TRAFFIC_FALLBACK_S=24;
function spawnTraffic(scrollZ){
  if(driveState.traffic.length>=TRAFFIC_LIMIT) return;
  const n=driveState.trafficSeq++, h=k=>driveHash01(n*8+k);
  const t=driveTraffic[Math.floor(h(0)*driveTraffic.length)%driveTraffic.length];
  driveState.traffic.push({
    z: scrollZ+TRAFFIC_SPAWN_Z,
    sprite: t,
    speed: TRAFFIC_SPEED*(0.85+h(1)*0.4),
    // Centre of the left-hand lane. With ROAD_LANES=3 that is exactly -1/3 of the
    // road's width from the middle, so a vehicle sits between the dividers rather
    // than straddling one.
    lane: -1/ROAD_LANES,
  });
}

/* Keep the avenue from thinning out when the music goes quiet — bounded in TIME, so
   the floor is a fixed trees-per-second and cannot scale with driving speed. */
function fillPalmGap(tSec){
  if(tSec-driveState.lastPlantT>=PALM_FILL_S){ plantPalm(); driveState.lastPlantT=tSec; }
}
/* An empty road for the first several seconds looks broken, and a palm planted now
   takes ~7s to arrive. So on reset we back-fill the avenue we would already have
   driven, spaced by the track's real tempo when the analysis knows it. */
function seedPalms(){
  driveState.palms=[]; driveState.palmSeq=0; driveState.lastPlantZ=-1e9; driveState.lastPlantT=-1e9;
  const bpm=(analysis && typeof analysis.bpm==="number" && analysis.bpm>0)?analysis.bpm:120;
  // one beat's worth of road at a typical cruising speed, held inside the same bounds
  // live planting uses so the seeded stretch matches what follows it
  const stepZ=Math.max(PALM_GAP_MIN,(60/bpm)*30*WORLD_RATE);
  const base=driveState.scrollNear*WORLD_RATE;
  for(let z=3; z<PALM_SPAWN_Z; z+=stepZ) plantPalm(base+z);
}

/* ---- tempo AT THE PLAYHEAD, not averaged over the file ----
   analysis.bpm is one number for the whole track: the median gap across every beat.
   That is fine for a single track and actively wrong for a mix. Measured on a
   101-minute hardcore set, the real tempo per 10-minute block ran 96.8, 166.7,
   176.5 x5, 187.5, 100.0, 120.0, 272.7 — the tracker followed all of it correctly,
   and collapsing 13,141 beats to one median reported "176 BPM" for a file that is
   near 176 for about half its length.
   The beat TIMES are good, so read the tempo back out of them locally: median gap
   over a window around the playhead. On a normal track this equals the global value;
   on a mix it follows the music. Recomputed at most a few times a second (binary
   search + a short scan), and cached in between. */
const BPM_WINDOW_S=15, BPM_MIN_BEATS=6;
let bpmCacheT=-1e9, bpmCacheV=null, bpmCacheKey=null;
function driveLocalBpm(){
  const beats=(analysis && Array.isArray(analysis.beats)) ? analysis.beats : null;
  if(!beats || beats.length<BPM_MIN_BEATS) return null;
  // reuse the cached answer while the playhead is still inside the same window
  if(bpmCacheKey===A.curId && Math.abs(posSec-bpmCacheT)<1.0) return bpmCacheV;
  const lo=posSec-BPM_WINDOW_S, hi=posSec+BPM_WINDOW_S;
  let a=0,b=beats.length;                       // first beat >= lo
  while(a<b){ const m=(a+b)>>1; if(beats[m]<lo) a=m+1; else b=m; }
  const gaps=[];
  for(let i=a;i+1<beats.length && beats[i]<=hi;i++) gaps.push(beats[i+1]-beats[i]);
  let v=null;
  if(gaps.length>=BPM_MIN_BEATS){
    gaps.sort((x,y)=>x-y);
    const med=gaps[gaps.length>>1];
    if(med>0) v=60/med;
  } else if(typeof analysis.bpm==="number" && analysis.bpm>0){
    v=analysis.bpm;                             // sparse patch (intro, breakdown) — fall back
  }
  bpmCacheT=posSec; bpmCacheV=v; bpmCacheKey=A.curId;
  return v;
}

/* ---------------- signal derivation — beats, sections, boss arena ---------------- */
let driveState={screen:"title",started:false,ampAvg:0,
  beatMean:0,beatLastT:-999,scrollNear:0,speed:24,palms:[],palmSeq:0,lastPlantZ:-1e9,lastPlantT:-1e9,lowRef:0,traffic:[],trafficSeq:0,lastCarSec:-1,lastCarBeat:0,bossFinalActive:false,bossSustained:true,
  beatIdx:0,lastBeatPos:null,sectionIndex:0};
let driveTelemetry={distanceM:0,topSpeed:0,peakPct:0,beats:0,bossCleared:false};
let drivePulse=0, balSmooth=0, driveTrackKey=undefined;

function driveResetForTrack(){
  driveState={screen:"title",started:false,ampAvg:0,
    beatMean:0,beatLastT:-999,scrollNear:0,speed:24,palms:[],palmSeq:0,lastPlantZ:-1e9,lastPlantT:-1e9,lowRef:0,traffic:[],trafficSeq:0,lastCarSec:-1,lastCarBeat:0,
    bossFinalActive:false,bossSustained:true,beatIdx:0,lastBeatPos:null,sectionIndex:0};
  driveTelemetry={distanceM:0,topSpeed:0,peakPct:0,beats:0,bossCleared:false};
  drivePulse=0; balSmooth=0;
  seedPalms();   // back-fill the avenue so the road is not bare for the first ~7s
}
function updateDriveSignals(dt,t){
  if(driveTrackKey!==A.curId){ driveTrackKey=A.curId; driveResetForTrack(); }
  const tSec=t/1000;
  drivePulse=Math.max(0,drivePulse-dt/0.18);

  if(driveState.screen==="title"){ if(playing){ driveState.screen="scene"; driveState.started=true; } return; }
  if(driveState.screen==="end") return;

  // real beats/sections from the analysis payload, when the detector has produced them — code
  // defensively, `analysis` may be null (no ffmpeg) or mid-poll ("pending"); fall back to the
  // existing envelope-derived heuristics unchanged whenever the real data isn't there yet.
  const beats=(analysis && Array.isArray(analysis.beats) && analysis.beats.length) ? analysis.beats : null;
  const sections=(analysis && Array.isArray(analysis.sections) && analysis.sections.length) ? analysis.sections : null;

  if(playing){
    const raw=energyAt(posSec)||{amp:0,low:0,mid:0,high:0,ampL:0,ampR:0};

    // Slow reference level for the low band (tau ~6s) = "how hard does this track
    // normally hit". Deliberately slower than the 0.6s detector mean: a fast mean
    // re-normalises to a quiet passage within a second and every soft beat scores as
    // a hard one again, which is the whole problem being fixed here.
    driveState.lowRef += (raw.low-driveState.lowRef)*(1-Math.exp(-dt/6));
    // analysis.beats is a METRONOME — the engine fits a phase and period, then emits
    // a beat every period to the end of the track without ever checking whether there
    // is energy there. So the grid says WHEN a beat falls; the audio has to say how
    // hard it lands, or a breakdown pulses just as violently as a drop.
    const beatHit=clamp(raw.low/(driveState.lowRef*1.25+0.04),0,1);

    // ---- beats: fire when posSec crosses the next real beat time; fall back to onset detection ----
    if(beats){
      // resync (no counting) on: first tick after a track change/reset, a backward seek, or a big forward jump —
      // only steady forward playback should fire pulses, never a backlog burst for beats already skipped past
      // The backward tolerance is 0.4s, not 0.05s. posSec is now derived from the
      // fitted clock, and each poll eases that fit — which can step position slightly
      // BACKWARD. At 0.05s those routine corrections were being misread as seeks, and
      // a "seek" resyncs beatIdx PAST the current position without firing, silently
      // swallowing beats. A real seek moves far more than 0.4s.
      const seeked=driveState.lastBeatPos==null || posSec<driveState.lastBeatPos-0.4 || posSec>driveState.lastBeatPos+1.5;
      if(seeked){ let i=0; while(i<beats.length && beats[i]<=posSec) i++; driveState.beatIdx=i; }
      else while(driveState.beatIdx<beats.length && beats[driveState.beatIdx]<=posSec){
        driveTelemetry.beats++; drivePulse=Math.max(drivePulse,beatHit); driveState.beatIdx++; plantPalm(); driveState.lastPlantT=tSec;
      }
      driveState.lastBeatPos=posSec;
    } else {
      // onset/beat detector — running mean of RAW low, tau~0.6s, 220ms refractory
      const bk=1-Math.exp(-dt/0.6);
      driveState.beatMean += (raw.low-driveState.beatMean)*bk;
      if(raw.low>driveState.beatMean*1.35+0.04 && (tSec-driveState.beatLastT)>0.22){
        driveState.beatLastT=tSec; driveTelemetry.beats++; drivePulse=Math.max(drivePulse,beatHit); plantPalm(); driveState.lastPlantT=tSec;
      }
    }

    // ---- current section (real boundaries when available) — feeds the boss-arena check below ----
    if(sections){
      let idx=0; for(let i=0;i<sections.length;i++){ if(sections[i].t<=posSec) idx=i; else break; }
      driveState.sectionIndex=idx;
    }
    // smoothed amp — tau~1.2s, used by the boss-arena fallback when sections aren't available
    const dk=1-Math.exp(-dt/1.2);
    driveState.ampAvg += (raw.amp-driveState.ampAvg)*dk;
    // stereo lane drift — real L/R balance, smoothed
    const targetBal=(raw.ampR-raw.ampL)/(raw.ampR+raw.ampL+1e-6);
    const balK=1-Math.exp(-dt/0.35); balSmooth += (targetBal-balSmooth)*balK;
    // Scroll speed still comes from real loudness, but a car has mass. Two changes
    // over the old `10 + amp*46`:
    //   range  — 5.6:1 meant a quiet passage crawled to a near-halt and a loud one
    //            sprinted. 2.2:1 still reads as "the music drives it" without the
    //            world ever stopping.
    //   inertia — sm.amp is smoothed at tau 0.09s, so speed tracked every transient
    //            and lurched. A ~1.1s throttle response gives the car weight.
    // Both filter a real signal rather than inventing one, so the speed still means
    // something. This was always here; WORLD_RATE 0.09 -> 0.45 just made it visible.
    // Loudness sets the throttle; tempo scales it. Without the tempo term a 200 BPM
    // track and a 90 BPM one drove at the same speed and only differed in how densely
    // the trees were spaced — the avenue thickened but nothing felt faster.
    // Tempo at the PLAYHEAD (driveLocalBpm), not the file average — on a DJ set the
    // road should speed up and slow down with the mix. Normalised around 120 and
    // clamped so an outlier (or a halved/doubled reading) cannot make it unreadable.
    const lbpm=driveLocalBpm();
    const tempoK=(lbpm>0) ? clamp(lbpm/120,0.8,1.5) : 1;
    const spdTarget=REDUCE?6:(24+clamp(sm.amp,0,1)*28)*tempoK;
    const spdK=1-Math.exp(-dt/1.1);
    driveState.speed += (spdTarget-driveState.speed)*spdK;
    const advanced=driveState.scrollNear+dt*driveState.speed;
    driveState.scrollNear=advanced%100000;
    // Palm z values are absolute world positions, so the scroll wrap has to be
    // applied to them too or every planted tree is stranded 45000 units away.
    if(advanced>=100000){ const d=100000*WORLD_RATE; for(const pm of driveState.palms) pm.z-=d; }
    // Retire palms the camera has passed. They are planted in order, so the oldest
    // are the nearest — one shift per pass is enough, no full-array scan.
    fillPalmGap(tSec);
    const passedZ=driveState.scrollNear*WORLD_RATE;
    while(driveState.palms.length && driveState.palms[0].z-passedZ<=0.5) driveState.palms.shift();

    // ---- oncoming traffic ----
    // Deliberately an EVENT, not scenery: one vehicle per section boundary, so a car
    // arrives on the drop rather than streaming past constantly. Sections come from
    // the real analysis; without them a slow timer stands in, so the feature is not
    // simply absent on tracks the section detector gave up on.
    const secIdx = sections ? driveState.sectionIndex : Math.floor(posSec/TRAFFIC_FALLBACK_S);
    const onSection = secIdx!==driveState.lastCarSec;
    const onCadence = driveTelemetry.beats-driveState.lastCarBeat >= TRAFFIC_BEATS;
    if((onSection || onCadence) && driveTraffic.length){
      driveState.lastCarSec=secIdx;
      driveState.lastCarBeat=driveTelemetry.beats;
      spawnTraffic(passedZ);
    }
    // They close on us at OUR speed plus THEIRS — relative closing speed, which is
    // what makes an approach read as fast.
    for(const v of driveState.traffic) v.z -= dt*v.speed;
    while(driveState.traffic.length && driveState.traffic[0].z-passedZ<=0.6) driveState.traffic.shift();
    // telemetry — distance/top speed from smoothed amp, peak level from smoothed amp
    const speedKmh=40+clamp(sm.amp,0,1.2)*150;
    driveTelemetry.distanceM += speedKmh*dt/3.6;
    driveTelemetry.topSpeed=Math.max(driveTelemetry.topSpeed,speedKmh);
    driveTelemetry.peakPct=Math.max(driveTelemetry.peakPct,sm.amp*100);
    // boss arena eligibility: real final-section energy when we have sections; otherwise the old
    // "sustained loud through the final ~15% of the track" heuristic on RAW amp
    if(sections){
      if(driveState.sectionIndex===sections.length-1){
        driveState.bossFinalActive=true; driveState.bossSustained=sections[sections.length-1].energy>=0.62;
      }
    } else if(durSec>0 && posSec>=durSec*0.85){
      if(!driveState.bossFinalActive){ driveState.bossFinalActive=true; driveState.bossSustained=true; }
      if(driveState.ampAvg<=0.62) driveState.bossSustained=false;
    }
  }

  if(durSec>0 && posSec>=durSec-0.15 && driveState.started){
    driveState.screen="end";
    driveTelemetry.bossCleared=driveState.bossFinalActive && driveState.bossSustained;
  }
}

/* ---------------- render dispatch + display blit ---------------- */
function renderDriveBuffer(t){
  const ctx=driveBufCtx, accent=driveAccentStr();
  ctx.clearRect(0,0,DRIVE_W,DRIVE_H);
  if(driveState.screen==="title"){ drawTitle(ctx,t); return; }
  if(driveState.screen==="end"){ drawEnd(ctx); return; }
  const boss=driveState.bossFinalActive && driveState.bossSustained;
  drawOutrun(ctx,accent,driveState.scrollNear, boss?DRIVE_PAL_BOSS:DRIVE_PAL_SUNSET);
  drawHud(ctx, boss?"BOSS ARENA":"OUTRUN", sm.amp);
}
/* Size the display canvas to an exact integer multiple of the 320x180 buffer. Two
   things fall out of that: the nearest-neighbour upscale lands on whole pixels, and
   the element gains a true 16:9 intrinsic ratio so CSS can "contain" it correctly
   instead of the JS and the stylesheet each doing half the letterboxing. */
function sizeDriveCanvas(){
  if(!driveC) return;
  const dpr=Math.min(2,devicePixelRatio||1), wrap=driveC.wrap, cs=getComputedStyle(wrap);
  // CONTENT box, not getBoundingClientRect(): the stage carries 40px side padding, and
  // sizing against the border box made the canvas wider than the space it had, so
  // max-width clamped it back and reintroduced a fractional 0.95x scale.
  const availW=wrap.clientWidth-parseFloat(cs.paddingLeft)-parseFloat(cs.paddingRight);
  const availH=wrap.clientHeight-parseFloat(cs.paddingTop)-parseFloat(cs.paddingBottom);
  // Largest whole-number buffer multiple that fits, in DEVICE pixels.
  const k=Math.max(1,Math.min(12,Math.floor(Math.min(availW*dpr/DRIVE_W, availH*dpr/DRIVE_H))));
  const w=DRIVE_W*k, h=DRIVE_H*k;
  if(driveC.cv.width!==w || driveC.cv.height!==h){ driveC.cv.width=w; driveC.cv.height=h; }
  // Pin the CSS size to exactly the backing store over dpr. Without this the browser
  // was free to clamp the element via max-width/max-height and scale it — at one
  // window size that meant a 2560px store painted into 3862 device px, a 1.5x
  // NON-INTEGER upscale, which makes some pixel-art pixels 1px wider than others.
  // Choosing k to fit means the max-* rules never have to fire.
  driveC.cv.style.width=(w/dpr)+"px";
  driveC.cv.style.height=(h/dpr)+"px";
  driveC.ctx.setTransform(1,0,0,1,0,0);   // buffer coords are the canvas coords now — no dpr transform
  driveC.W=w; driveC.H=h;
}
function ensureDriveBuf(){ if(driveBuf) return;
  driveBuf=document.createElement("canvas"); driveBuf.width=DRIVE_W; driveBuf.height=DRIVE_H; driveBufCtx=driveBuf.getContext("2d");
  // Sprites are scaled on the way INTO this buffer, so smoothing has to be off here
  // too — not just on the display blit. Left on, every palm was bilinear-filtered:
  // soft edges on pixel art, and the slow scaling path.
  driveBufCtx.imageSmoothingEnabled=false; }
function drawDrive(dt,t){
  if(!driveC) return; if(!driveC.W) sizeDriveCanvas();
  ensureDriveBuf();
  updateDriveSignals(dt,t);
  renderDriveBuffer(t);
  const {ctx,W,H}=driveC;
  // The canvas is an exact multiple of the buffer, so the blit fills it edge to edge:
  // no letterbox maths, no rounding, no clear. CSS handles fitting the element into
  // whatever shape the stage is.
  // NO whole-frame shake, deliberately. At a 320x180 buffer upscaled ~4x the
  // smallest grid-aligned offset is already ~4 screen pixels, so there is no
  // subtle version — it reads as a jolt. Beat impact belongs in world elements
  // (tail lights, sun core, road edges, speed lines) which can punch without
  // translating the pixel grid.
  ctx.imageSmoothingEnabled=false;
  ctx.drawImage(driveBuf,0,0,W,H);
}
function openDrive(){
  // Warm the caches here: building them lazily put ~54k fillRects into the first
  // rendered frame, a ~15ms hitch right as the mode appears.
  // Every lean x flip x mip, or the first frame that happens to show an unseen lean
  // pays for building it. The leans are cheap (row blits off one baked base sprite).
  loadDriveCars();
  try{ carBodyCanvas();
    for(let li=0;li<PALM_LEAN_STEPS.length;li++){ palmMipSet(false,li); palmMipSet(true,li); }
  }catch(e){}
  openMode("drive",[()=>sizeDriveCanvas()]);
}
