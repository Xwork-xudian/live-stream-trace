"""实时音画延迟监测页（/live）：播放直播流 + 0.5s 刷新信号延迟。"""

LIVE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>实时音画延迟监测 · live-stream-trace</title>
<script src="/assets/flv.min.js"></script>
<style>
 *{box-sizing:border-box}
 body{margin:0;background:#0b0e13;color:#e6edf3;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
 header{padding:14px 18px;border-bottom:1px solid #1d2430;display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:#0e131a;position:sticky;top:0;z-index:5}
 h1{font-size:16px;margin:0 12px 0 0;font-weight:650}
 input#q{flex:1;min-width:260px;background:#11161f;border:1px solid #253041;color:#e6edf3;border-radius:8px;padding:9px 12px;outline:none}
 input#q:focus{border-color:#3b82f6}
 button{background:#2563eb;border:0;color:#fff;border-radius:8px;padding:9px 14px;cursor:pointer;font-weight:600}
 button.ghost{background:#1b2431;color:#c8d3e0}
 button:disabled{opacity:.5;cursor:not-allowed}
 .wrap{display:grid;grid-template-columns:minmax(320px,1.15fr) minmax(320px,1fr);gap:16px;padding:16px}
 @media(max-width:900px){.wrap{grid-template-columns:1fr}}
 .card{background:#0f141c;border:1px solid #1d2430;border-radius:12px;padding:14px}
 .muted{color:#8b98a9;font-size:12px}
 video{width:100%;background:#000;border-radius:10px;aspect-ratio:16/9}
 .big{font-size:46px;font-weight:700;letter-spacing:-1px;line-height:1.05}
 .big.pos{color:#f0b429}.big.neg{color:#38bdf8}.big.na{color:#64748b}
 .row{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
 .badge{display:inline-block;border-radius:999px;padding:3px 10px;font-size:12px;font-weight:600;background:#1f2937}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:12px}
 .metric{background:#11161f;border:1px solid #1b2431;border-radius:8px;padding:8px 10px}
 .metric b{display:block;font-size:16px;font-weight:650;margin-top:2px}
 canvas{width:100%;height:120px;display:block;background:#0c1118;border:1px solid #1b2431;border-radius:8px}
 pre{white-space:pre-wrap;word-break:break-all;margin:6px 0 0;font-size:12px;color:#9fb0c5;max-height:150px;overflow:auto}
 .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:#8b98a9;margin-top:6px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
</style></head><body>
<header>
  <h1>⚡ 实时音画延迟监测</h1>
  <input id="q" placeholder="粘贴直播间链接 / 房间号 / 分享口令（例：https://live.bilibili.com/123456789）"/>
  <button id="go">开始监测</button>
  <button id="stop" class="ghost" disabled>停止</button>
  <span id="conn" class="muted">未连接</span>
</header>
<div class="wrap">
  <div class="card">
    <div class="muted">直播画面（经服务端代理补 Referer；可点视频解除静音）</div>
    <video id="v" controls muted playsinline></video>
    <pre id="log">等待开始…</pre>
  </div>
  <div class="card">
    <div class="row">
      <div>
        <div class="muted">信号延迟（流内 A/V 偏移：音频 − 视频）</div>
        <div id="off" class="big na">— ms</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div id="dir" class="muted">—</div>
        <div id="verdict" class="badge" style="margin-top:6px">等待数据</div>
      </div>
    </div>
    <div class="legend">
      <span><span class="dot" style="background:#f0b429"></span>音频超前（先听到后看到）</span>
      <span><span class="dot" style="background:#38bdf8"></span>音频滞后（先看到后听到）</span>
      <span>刷新 <b id="rate">0.5</b> s</span>
    </div>
    <canvas id="spark" width="900" height="240"></canvas>
    <div class="grid">
      <div class="metric"><span class="muted">状态</span><b id="st">—</b></div>
      <div class="metric"><span class="muted">视频帧率</span><b id="fps">—</b></div>
      <div class="metric"><span class="muted">码率</span><b id="kbps">—</b></div>
      <div class="metric"><span class="muted">时间戳跳变</span><b id="jump">—</b></div>
      <div class="metric"><span class="muted">窗口帧数 A/V</span><b id="fr">—</b></div>
      <div class="metric"><span class="muted">已监测</span><b id="up">—</b></div>
    </div>
    <pre id="err"></pre>
  </div>
</div>
<script>
var SID=null, TIMER=null, PL=null, HIST=[], PLAYED=false, T0=0;
var E=function(id){return document.getElementById(id)};
function log(s){E('log').textContent=s}
function destroy(){if(PL){try{PL.destroy()}catch(e){} PL=null}}

function startFlv(url){
  destroy();
  var v=E('v');
  if(!flvjs.isSupported()){log('浏览器不支持 flv.js（MSE）播放，请用 Chrome/Edge/Firefox');return}
  PL=flvjs.createPlayer({type:'flv',isLive:true,url:url},
    {enableWorker:false,enableStashBuffer:false,stashInitialSize:128,lazyLoad:false,autoCleanupSourceBuffer:true,fixAudioTimestampGap:false});
  PL.attachMediaElement(v); PL.load();
  PL.on(flvjs.Events.MEDIA_INFO,function(mi){
    log('✅ 解码中 '+(mi.width||'?')+'x'+(mi.height||'?')+' | '+(mi.videoCodec||'?')+'/'+(mi.audioCodec||'?')
        +'\n（若无声：点视频右下角音量解除静音）');
  });
  PL.on(flvjs.Events.STATISTICS_INFO,function(s){ if(s&&s.speed!=null) E('kbps').textContent=(s.speed.toFixed(0))+' KB/s' });
  PL.on(flvjs.Events.ERROR,function(t,d){ log('❌ ['+t+'/'+d+'] 播放失败：'+d+'\n（多数直播流需点击视频后播放；CDN 冷启动偶发失败，可重新开始）') });
  var p=v.play(); if(p&&p.catch){p.catch(function(){v.muted=true;v.play().catch(function(e){log('⚠️ 请手动点击视频播放：'+(e&&e.name||e))})})}
}

async function go(){
  var text=E('q').value.trim(); if(!text){log('请输入直播间链接 / 房间号');return}
  E('go').disabled=true; log('已提交，后台解析中…（解析完成后自动开始播放与测量）');
  try{
    var r=await fetch('/api/live/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})});
    var d=await r.json();
    if(d.error){ log('❌ '+d.error); E('go').disabled=false; return }
    SID=d.id; HIST=[]; PLAYED=false; T0=Date.now();
    E('stop').disabled=false;
    if(TIMER) clearInterval(TIMER);
    TIMER=setInterval(poll,500); poll();
  }catch(e){ log('❌ '+(e&&e.message||e)); E('go').disabled=false }
}
async function stop(){
  if(TIMER){clearInterval(TIMER);TIMER=null}
  if(SID){ try{ await fetch('/api/live/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:SID})}) }catch(e){} }
  SID=null; destroy(); E('stop').disabled=true; E('go').disabled=false;
  E('conn').textContent='已停止'; E('st').textContent='—';
}
async function poll(){
  if(!SID) return;
  try{
    var r=await fetch('/api/live/status?id='+encodeURIComponent(SID));
    if(!r.ok){ E('err').textContent='状态接口 HTTP '+r.status; return }
    render(await r.json());
  }catch(e){ E('err').textContent='轮询失败：'+(e&&e.message||e) }
}
function render(d){
  if(d.error && !d.alive){ E('err').textContent=d.error }
  if(d.play_url && !PLAYED){
    PLAYED=true;
    E('conn').textContent='已连接 '+(d.platform_name||'')+' · '+(d.room_id||'')+(d.title?(' · '+d.title):'');
    log('档位：'+(d.label||'')+'\n播放地址：'+d.play_url);
    startFlv(d.play_url);
  }
  var o=d.offset_ms;
  var el=E('off');
  if(o==null){ el.className='big na'; el.textContent='— ms' }
  else{ el.className='big '+(o>=0?'pos':'neg'); el.textContent=(o>0?'+':'')+o.toFixed(1)+' ms' }
  E('dir').textContent=d.direction||'—';
  var v=E('verdict'); v.textContent=d.verdict||'—';
  var c=d.verdict_level==='高'?'#16a34a':(d.verdict_level==='中'?'#d97706':'#dc2626');
  if(d.offset_ms==null) c='#475569';
  v.style.background=c;
  var st=d.status||'—';
  if(!PLAYED && SID){ st=st+'（'+((Date.now()-T0)/1000).toFixed(0)+'s）' }
  E('st').textContent=st+(d.alive?'':'（已断）');
  E('fps').textContent=(d.fps!=null?d.fps.toFixed(1)+' fps':'—');
  E('kbps').textContent=(d.kbps!=null?d.kbps.toFixed(0)+' kbps':'—');
  E('jump').textContent=(d.ts_jumps!=null?d.ts_jumps:'—');
  E('fr').textContent=(d.frames_a||0)+' / '+(d.frames_v||0);
  E('up').textContent=(d.uptime_s!=null?Math.floor(d.uptime_s)+' s':'—');
  if(d.error) E('err').textContent=d.error; else E('err').textContent='';
  if(o!=null){ HIST.push(o); if(HIST.length>240) HIST.shift(); }
  spark();
}
function spark(){
  var c=E('spark'), g=c.getContext('2d'), W=c.width, H=c.height;
  g.clearRect(0,0,W,H);
  var lim=200;  // ±200ms 可视范围
  g.strokeStyle='#1e293b'; g.lineWidth=1;
  [0.5,1.0].forEach(function(f){
    g.beginPath(); g.moveTo(0,H/2-f*H/2*0.9); g.lineTo(W,H/2-f*H/2*0.9); g.stroke();
    g.beginPath(); g.moveTo(0,H/2+f*H/2*0.9); g.lineTo(W,H/2+f*H/2*0.9); g.stroke();
  });
  g.strokeStyle='#334155'; g.beginPath(); g.moveTo(0,H/2); g.lineTo(W,H/2); g.stroke();
  // EBU R37 窗口 [-60, +40]
  g.fillStyle='rgba(34,197,94,.10)';
  var y1=H/2-(40/lim)*H/2*0.9, y2=H/2-(-60/lim)*H/2*0.9;
  g.fillRect(0,Math.min(y1,y2),W,Math.abs(y2-y1));
  g.fillStyle='#64748b'; g.font='11px sans-serif';
  g.fillText('+40ms (EBU 上界)',6,y1-3); g.fillText('-60ms (EBU 下界)',6,y2+12);
  if(!HIST.length) return;
  var n=HIST.length, step=W/Math.max(1,n-1);
  g.strokeStyle='#38bdf8'; g.lineWidth=1.6; g.beginPath();
  for(var i=0;i<n;i++){
    var v=Math.max(-lim,Math.min(lim,HIST[i]));
    var x=i*step, y=H/2-(v/lim)*H/2*0.9;
    if(i===0) g.moveTo(x,y); else g.lineTo(x,y);
  }
  g.stroke();
  var lv=HIST[n-1], lx=(n-1)*step, ly=H/2-(Math.max(-lim,Math.min(lim,lv))/lim)*H/2*0.9;
  g.fillStyle=lv>=0?'#f0b429':'#38bdf8';
  g.beginPath(); g.arc(lx,ly,3.5,0,Math.PI*2); g.fill();
}
E('go').onclick=go; E('stop').onclick=stop;
E('q').addEventListener('keydown',function(e){if(e.key==='Enter')go()});
spark();
</script></body></html>
"""
