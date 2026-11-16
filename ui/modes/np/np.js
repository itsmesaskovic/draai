/* ---------------- full-screen set video ----------------
   The playing set's real video, muted, in #np's left half, kept in sync with the
   speaker through the UI clock (posSec/playing). Streams come from /api/video at
   runtime (the engine asks the user's own yt-dlp); nothing is fetched while #np is
   closed, in vinyl mode, or in the demo. Any failure quietly leaves the cover. */
const NPV={ id:null, gen:0, streams:[], cur:-1, errors:0, failed:null, shown:false,
  playT:0, winT:0, q:null, lastT:0, waiting:false, waits:0, bufT:0 };
function npVideoEl(){ return $("#npVideo"); }
function npVideoWanted(){
  if(DEMO || VINYL || A.curId==null || !$("#np").classList.contains("open")) return null;
  const t=A.tracks.find(x=>String(x.id)===String(A.curId));
  return (t && t.has_video) ? String(A.curId) : null;
}
function npCodecRank(vc){ vc=String(vc||"");
  return vc.startsWith("av01")?0 : (vc.startsWith("vp9")||vc.startsWith("vp09"))?1 : vc.startsWith("avc1")?2 : 3; }
function npCanPlay(s){ const v=npVideoEl();
  const type='video/'+(s.ext==='webm'?'webm':'mp4')+'; codecs="'+s.vcodec+'"';
  try{ if(window.MediaSource && MediaSource.isTypeSupported) return MediaSource.isTypeSupported(type); }catch(e){}
  try{ return v.canPlayType(type)!==""; }catch(e){ return false; } }
function npPlayable(list){
  return (list||[]).filter(s=>s && s.url && s.height>0 && npCanPlay(s))
    .sort((a,b)=>(b.height-a.height) || (npCodecRank(a.vcodec)-npCodecRank(b.vcodec))); }
function npVideoShow(on){ NPV.shown=on; $("#np").classList.toggle("np-video",on);
  if(on) requestAnimationFrame(()=>sizeCanvas(npCv)); }   // the waveform column changed width
function npVideoUnload(){ const v=npVideoEl();
  NPV.gen++; NPV.id=null; NPV.streams=[]; NPV.cur=-1; NPV.errors=0;
  if(document.fullscreenElement===v){ try{ document.exitFullscreen(); }catch(e){} }
  try{ v.pause(); }catch(e){}
  if(v.hasAttribute("src")){ v.removeAttribute("src"); try{ v.load(); }catch(e){} }
  if(NPV.shown) npVideoShow(false); }
function npVideoSetSrc(i, keepShown){ const v=npVideoEl(), s=NPV.streams[i]; NPV.cur=i;
  NPV.playT=0; NPV.winT=0; NPV.q=null; NPV.waiting=false; NPV.waits=0; NPV.bufT=0;
  if(!keepShown && NPV.shown) npVideoShow(false);
  v.playbackRate=1; v.src=s.url; try{ v.load(); }catch(e){} }
/* resolves to the playable streams (tallest first, maybe empty), null when the engine
   said no, or undefined when stale: the track or view changed while it was in flight */
async function npVideoFetch(id){ const g=NPV.gen;
  let d; try{ d=await api("/api/video?id="+enc(id)); }catch(e){ d=null; }
  if(g!==NPV.gen || NPV.id!==id) return undefined;
  return d ? npPlayable(d.streams) : null; }
async function npVideoStart(id){
  const list=await npVideoFetch(id); if(list===undefined) return;
  if(!list || !list.length){ NPV.failed=id; npVideoUnload(); return; }   // no video: the cover stays
  NPV.streams=list; npVideoSetSrc(0,false); }
function npVideoUpdate(){
  if(NPV.failed!=null && NPV.failed!==String(A.curId)) NPV.failed=null;   // a new track gets a fresh try
  const want=npVideoWanted();
  if(want===NPV.id) return;
  npVideoUnload();
  if(want==null || NPV.failed===want) return;
  NPV.id=want; npVideoStart(want); }
async function npVideoError(){
  const id=NPV.id; if(id==null || NPV.cur<0) return;
  NPV.errors++;
  if(NPV.errors>=2){ NPV.failed=id; npVideoUnload(); return; }   // second failure: cover for this set, no toast
  const h=NPV.streams[NPV.cur].height, vc=NPV.streams[NPV.cur].vcodec;
  const list=await npVideoFetch(id);                               // links may have expired: ask again once
  if(list===undefined) return;
  if(!list || !list.length){ NPV.failed=id; npVideoUnload(); return; }
  NPV.streams=list;
  let i=list.findIndex(s=>s.height===h && s.vcodec===vc); if(i<0) i=list.findIndex(s=>s.height===h); if(i<0) i=0;
  npVideoSetSrc(i,NPV.shown); }
function npVideoStepDown(){ const h=NPV.streams[NPV.cur].height;
  const i=NPV.streams.findIndex(s=>s.height<h); if(i<0) return false;
  npVideoSetSrc(i,true); return true; }
function npVideoSync(){
  if(NPV.id==null) return;
  if(DEMO || VINYL || !$("#np").classList.contains("open")){ npVideoUpdate(); return; }  // safety net for any close path
  const v=npVideoEl(), now=performance.now(), dt=Math.min(.25,(now-(NPV.lastT||now))/1000); NPV.lastT=now;
  if(NPV.cur<0 || v.readyState<1 || v.seeking) return;
  const dur=isFinite(v.duration)?v.duration:Infinity, end=Math.max(0,dur-0.05);
  // the audio ran past the video's end: hold the last frame. Never play() an ended
  // video — that rewinds it to 0, and the drift seek would loop it forever.
  if(posSec>=end){ if(!v.paused) v.pause(); v.playbackRate=1;
    if(v.currentTime<end-1) v.currentTime=end;                   // e.g. the audio jumped past the end
    return; }
  const want=posSec, d=v.currentTime-want;
  if(v.ended){ v.currentTime=want; return; }                     // audio sought back below the end: seek, play next tick
  if(!playing){ if(!v.paused) v.pause(); v.playbackRate=1; if(Math.abs(d)>2) v.currentTime=want; return; }
  if(v.paused){ const p=v.play(); if(p&&p.catch) p.catch(()=>{}); }
  // While buffering, currentTime stands still: a drift seek would throw the buffer away
  // and start over. Wait; once it plays again one seek (if needed) catches up.
  const buffering=NPV.waiting || v.readyState<3;
  if(buffering) v.playbackRate=1;
  else if(Math.abs(d)>2){ v.currentTime=want; v.playbackRate=1; }
  else if(Math.abs(d)>0.15) v.playbackRate=d>0?0.95:1.05;
  else v.playbackRate=1;
  // step-down: after 15 s of playback, judge every 10 s window on dropped frames and stalls
  if(v.paused || !NPV.shown) return;
  NPV.playT+=dt; if(NPV.playT<15) return;
  const q=v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {droppedVideoFrames:0,totalVideoFrames:0};
  if(!NPV.q){ NPV.q={drop:q.droppedVideoFrames,tot:q.totalVideoFrames}; NPV.winT=NPV.playT; NPV.waits=0; NPV.bufT=0; return; }
  if(buffering) NPV.bufT+=dt;
  if(NPV.waits>=2 || NPV.bufT>3){ if(!npVideoStepDown()){ NPV.waits=0; NPV.bufT=0; } return; }  // lowest already: just keep going
  if(NPV.playT-NPV.winT<10) return;
  const drop=q.droppedVideoFrames-NPV.q.drop, tot=q.totalVideoFrames-NPV.q.tot;
  NPV.q={drop:q.droppedVideoFrames,tot:q.totalVideoFrames}; NPV.winT=NPV.playT; NPV.waits=0; NPV.bufT=0;
  if(tot>0 && drop/tot>0.10) npVideoStepDown();
}
function npVideoFullscreen(){ const v=npVideoEl();
  if(!NPV.shown || document.fullscreenElement) return;
  try{ const p=v.requestFullscreen(); if(p&&p.catch) p.catch(()=>{}); }catch(e){} }
(function npVideoWire(){ const v=npVideoEl();
  v.muted=true;
  v.addEventListener("loadedmetadata",()=>{ if(NPV.cur<0) return;
    const dur=isFinite(v.duration)?v.duration:Infinity; try{ v.currentTime=Math.min(posSec,Math.max(0,dur-0.05)); }catch(e){} });
  v.addEventListener("loadeddata",()=>{ if(NPV.id!=null && NPV.cur>=0 && !NPV.shown) npVideoShow(true); });
  // a stall (not a seek we asked for) counts toward step-down; playing/canplay ends it
  v.addEventListener("waiting",()=>{ NPV.waiting=true; if(!v.seeking) NPV.waits++; });
  v.addEventListener("playing",()=>{ NPV.waiting=false; });
  v.addEventListener("canplay",()=>{ NPV.waiting=false; });
  v.addEventListener("error",()=>{ if(v.hasAttribute("src")) npVideoError(); });
  v.addEventListener("click",npVideoFullscreen);
  $("#npVidFs").addEventListener("click",e=>{ e.stopPropagation(); npVideoFullscreen(); });
})();
