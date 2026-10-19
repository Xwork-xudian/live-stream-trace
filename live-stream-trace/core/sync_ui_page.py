#!/usr/bin/env python3
"""「音画同步检测（上传样本）」页面：单文件上传 → 两层检测 → 结果可视化 + 历史。

只放 HTML/JS 字符串，路由与后端在 core/web_ui.py。从 web_ui 引入以避免超长文件难维护。
"""

SYNC_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>音画同步检测 · 上传样本</title>
<script src="/assets/flv.min.js"></script>
<style>
:root{--bg:#0f1115;--card:#171a21;--card2:#11151d;--line:#2b2f3a;--fg:#e6e6e6;--muted:#8b97a8;
      --brand:#2c7be5;--ok:#4caf50;--warn:#ffb300;--err:#ef5350}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:Segoe UI,Arial,'Microsoft YaHei',sans-serif;line-height:1.6}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h3{font-size:15px;margin:0 0 12px;color:#9fb0c8}
a{color:var(--brand)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:16px 0}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.drop{border:2px dashed #38445c;border-radius:14px;padding:26px;text-align:center;cursor:pointer;transition:.15s}
.drop.hover{border-color:var(--brand);background:#141a26}
input[type=number],input[type=text],select{background:var(--card2);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
button{background:var(--brand);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-weight:700;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line);font-weight:500}
button:disabled{opacity:.5;cursor:not-allowed}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.metric{background:var(--card2);border:1px solid #232833;border-radius:10px;padding:10px}
.muted{color:var(--muted)} .small{font-size:12.5px} .val{font-weight:600;word-break:break-all}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;color:#0b0d10}
pre{background:var(--card2);border:1px solid #232833;border-radius:10px;padding:12px;max-height:300px;
    overflow:auto;font-size:12.5px;white-space:pre-wrap;word-break:break-word}
table{width:100%;border-collapse:collapse} th,td{border-bottom:1px solid #232833;padding:8px 6px;text-align:left;font-size:13.5px}
th{color:#9fb0c8;font-weight:600}
video{width:100%;max-height:420px;background:#000;border-radius:12px}
.bar{height:8px;background:#232833;border-radius:999px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--brand);transition:.2s}
</style></head><body><div class="wrap">
<h1>音画同步检测 · 上传样本</h1>
<div class="muted small">上传任意视频样本（flv / mp4 / mov / ts / mkv …），做两层音画同步检测：
<b>时间戳级</b>（tag PTS + 解码级 A-V）与 <b>内容级（人脸嘴型）</b>（不看时间戳，可发现推流前管线失同步）。
　<a href="/">← 返回直播流分析</a></div>

<div class="card">
  <h3>① 选择样本</h3>
  <div class="drop" id="drop">
    <div style="font-size:30px">⬆</div>
    <div>拖拽视频文件到此，或 <u>点击选择</u></div>
    <div class="muted small" id="fileInfo">未选择文件</div>
  </div>
  <input type="file" id="file" accept="video/*" style="display:none">

  <h3 style="margin-top:18px">② 参数</h3>
  <div class="row">
    <label class="small muted">分析时长 <input id="seconds" type="number" value="20" style="width:80px"> 秒</label>
    <label class="small muted">解码级时长 <input id="decode" type="number" value="10" style="width:70px"> 秒</label>
    <label class="small muted"><input id="fine" type="checkbox" checked> 高精度通道（5ms）</label>
    <label class="small muted"><input id="face" type="checkbox" checked> 人脸检测</label>
    <label class="small muted">手动 ROI <input id="roi" type="text" placeholder="x,y,w,h 相对坐标" style="width:180px"></label>
  </div>
  <div class="row" style="margin-top:12px">
    <label class="small muted"><input id="avsync" type="checkbox" checked> 时间戳级</label>
    <label class="small muted"><input id="lipsync" type="checkbox" checked> 内容级（嘴型）</label>
    <button id="btnRun" disabled>③ 上传并检测</button>
    <span id="hint" class="small muted"></span>
  </div>
  <div class="bar" style="margin-top:10px"><i id="upbar"></i></div>
</div>

<div class="card" id="jobCard" style="display:none">
  <h3>检测任务 <span id="jobBadge" class="badge" style="background:#8b97a8">排队中</span></h3>
  <div class="grid" id="jobMeta"></div>
  <pre id="log"></pre>
</div>

<div class="card" id="resultCard" style="display:none">
  <h3>检测结果</h3>
  <div id="results"></div>
  <div id="chartBox" style="display:none">
    <div class="muted small" style="margin-top:12px">内容级互相关曲线（横轴滞后 ms，纵轴相关峰 r）</div>
    <svg id="chart" viewBox="0 0 900 220" style="width:100%;height:220px;background:var(--card2);border-radius:10px;border:1px solid #232833"></svg>
  </div>
  <div class="row" style="margin-top:12px" id="reportRow"></div>
  <div id="previewBox" style="display:none;margin-top:12px">
    <div class="muted small">样本预览</div>
    <video id="player" controls playsinline></video>
  </div>
</div>

<div class="card">
  <h3>历史检测 <button class="ghost small" onclick="loadHistory()">刷新</button></h3>
  <table><thead><tr><th>时间</th><th>类型</th><th>样本/房间</th><th>标题</th><th>操作</th></tr></thead>
  <tbody id="history"></tbody></table>
</div>
<div class="muted small">仅供学习与技术研究使用。</div>
</div>
<script>
const $=(id)=>document.getElementById(id);
let uploadId=null, uploadName='', currentJob=null, timer=null, probe=null, flvPlayer=null;

async function api(p,b){const o=b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{};return (await fetch(p,o)).json();}
function esc(s){return (s===null||s===undefined)?'':String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function metric(l,v){return `<div class="metric"><div class="muted small">${esc(l)}</div><div class="val">${esc(v??'—')}</div></div>`;}

// —— 选择文件 ——
$('drop').onclick=()=>$('file').click();
$('file').onchange=(e)=>setFile(e.target.files[0]);
['dragenter','dragover'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.add('hover');}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('hover');}));
$('drop').addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(f)setFile(f);});
function setFile(f){
  if(!f) return;
  uploadId=null; uploadName=f.name;
  $('fileInfo').textContent=`${f.name}｜${(f.size/1e6).toFixed(1)} MB`;
  $('btnRun').disabled=false; $('hint').textContent='已选择，可开始检测';
  window._file=f;
}

// —— 上传并检测 ——
$('btnRun').onclick=async()=>{
  const f=window._file; if(!f){$('hint').textContent='请先选择文件';return;}
  $('btnRun').disabled=true; $('hint').textContent='上传中…'; $('upbar').style.width='0%';
  uploadId=await new Promise((resolve)=>{
    const xhr=new XMLHttpRequest();
    xhr.open('POST','/api/upload?name='+encodeURIComponent(f.name));
    xhr.upload.onprogress=(e)=>{ if(e.lengthComputable) $('upbar').style.width=(e.loaded/e.total*100).toFixed(0)+'%'; };
    xhr.onload=()=>{ try{ const j=JSON.parse(xhr.responseText); if(j.error){$('hint').textContent='上传失败：'+j.error; $('btnRun').disabled=false; resolve(null);} else resolve(j.upload_id);}catch(e){$('hint').textContent='上传响应异常'; resolve(null);} };
    xhr.onerror=()=>{ $('hint').textContent='上传失败（网络）'; $('btnRun').disabled=false; resolve(null); };
    const fd=new FormData(); fd.append('file',f,f.name);
    xhr.send(fd);
  });
  if(!uploadId){return;}
  $('hint').textContent='上传完成，开始检测…';
  const opts={seconds:Number($('seconds').value)||20, decode_seconds:Number($('decode').value)||10,
              fine: $('fine').checked, use_face: $('face').checked, roi: $('roi').value.trim(),
              av_sync: $('avsync').checked, lip_sync: $('lipsync').checked};
  const res=await api('/api/sync',{upload_id:uploadId, options:opts});
  if(res.error){ $('hint').textContent='提交失败：'+res.error; $('btnRun').disabled=false; return; }
  currentJob=res.job.id; $('jobCard').style.display=''; $('resultCard').style.display='none';
  $('log').textContent=''; pollJob();
};

function badge(s){const m={queued:['排队中','#8b97a8'],running:['检测中','#ffb300'],done:['完成','#4caf50'],failed:['失败','#ef5350']};
  const [t,c]=m[s]||['未知','#8b97a8']; $('jobBadge').textContent=t; $('jobBadge').style.background=c;}

async function pollJob(){
  if(!currentJob) return;
  const j=await api('/api/jobs/'+currentJob); if(j.error) return;
  badge(j.status);
  $('jobMeta').innerHTML=metric('样本',j.title||uploadName)+metric('开始',j.started||'—')
    +metric('耗时',j.elapsed_s!=null?j.elapsed_s+' s':'—')+metric('退出码',j.returncode??'—');
  $('log').textContent=j.log_tail||''; $('log').scrollTop=$('log').scrollHeight;
  if(j.status==='done'||j.status==='failed'){
    $('btnRun').disabled=false; $('hint').textContent='检测结束';
    await showResult(); loadHistory(); return;
  }
  timer=setTimeout(pollJob,1500);
}

async function showResult(){
  const s=await api('/api/sync/summary/'+currentJob);
  if(s.error){ $('hint').textContent=s.error; return; }
  const av=s.av||{}, lp=s.lip||{};
  let h='<div class="grid">';
  h+=metric('样本时长',(s.media&&s.media.duration!=null?s.media.duration+' s':'—'));
  h+=metric('分辨率',s.media&&s.media.width?s.media.width+'x'+s.media.height:'—');
  h+=metric('帧率',s.media&&s.media.fps?s.media.fps+' fps':'—');
  h+=metric('视频/音频编码',(s.media&&s.media.vcodec||'—')+' / '+(s.media&&s.media.acodec||'—'));
  h+='</div>';
  h+='<h3 style="margin-top:14px">时间戳级（tag PTS + 解码级）</h3><div class="grid">';
  h+=metric('判定',av.verdict||'未执行');
  h+=metric('流内偏移',av.stream_offset_ms!=null?av.stream_offset_ms+' ms':'—');
  h+=metric('播放器残差',av.player_residual_ms!=null?av.player_residual_ms+' ms':'—');
  h+=metric('方向',av.direction||'—');
  h+='</div>';
  h+='<h3 style="margin-top:14px">内容级（人脸嘴型）</h3><div class="grid">';
  h+=metric('判定',lp.verdict||'未执行');
  h+=metric('内容级偏移',lp.content_offset_ms!=null?lp.content_offset_ms+' ms':'—');
  h+=metric('方向',lp.direction||'—');
  h+=metric('测量分辨力',lp.lag_resolution_ms!=null?lp.lag_resolution_ms+' ms':'—');
  h+=metric('置信区间',lp.content_offset_ci_ms!=null?'±'+lp.content_offset_ci_ms+' ms':'—');
  h+=metric('滑窗一致率',lp.window_agreement!=null?(lp.window_agreement*100).toFixed(0)+'%':'—');
  h+=metric('置信度',lp.confidence||'—');
  h+=metric('人脸覆盖',lp.face_coverage!=null?(lp.face_coverage*100).toFixed(0)+'%':'—');
  h+='</div>';
  if(s.diagnosis) h+=`<div class="card" style="background:#111820;border-left:3px solid var(--brand)"><div class="small"><b>联合诊断：</b>${esc(s.diagnosis)}</div></div>`;
  if(lp.detail) h+=`<div class="card"><div class="small">${esc(lp.detail).replace(/\\n/g,'<br>')}</div></div>`;
  $('results').innerHTML=h;
  drawChart(s.lag_curve||[], lp.content_offset_ms, lp.lag_resolution_ms);
  $('reportRow').innerHTML = s.report_url ? `<a href="${esc(s.report_url)}" target="_blank">打开完整报告</a>` : '';
  if(s.preview_url){
    $('previewBox').style.display='';
    const v=$('player'); v.src=s.preview_url;
    if(s.preview_url.endsWith('.flv') && window.flvjs && flvjs.isSupported()){
      try{ flvPlayer && flvPlayer.destroy();
        flvPlayer=flvjs.createPlayer({type:'flv',url:s.preview_url}); flvPlayer.attachMediaElement(v); flvPlayer.load(); }catch(e){}
    }
  }
  $('resultCard').style.display='';
}

function drawChart(curve, off, res){
  const svg=$('chart'); svg.innerHTML='';
  const pts=curve.filter(c=>c[1]!==null); if(!pts.length){$('chartBox').style.display='none';return;}
  $('chartBox').style.display='';
  const W=900,H=220,pad=34;
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]);
  const x0=Math.min(...xs), x1=Math.max(...xs), y0=Math.min(...ys), y1=Math.max(...ys);
  const X=(v)=>pad+(v-x0)/(x1-x0||1)*(W-2*pad);
  const Y=(v)=>H-pad-(v-y0)/(y1-y0||1)*(H-2*pad);
  const ns='http://www.w3.org/2000/svg';
  const mk=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  svg.appendChild(mk('line',{x1:pad,y1:H-pad,x2:W-pad,y2:H-pad,stroke:'#3a4152'}));
  svg.appendChild(mk('line',{x1:X(0),y1:pad,x2:X(0),y2:H-pad,stroke:'#4a5568','stroke-dasharray':'4 4'}));
  const txt=mk('text',{x:X(0)+4,y:pad+12,fill:'#8b97a8','font-size':'12'}); txt.textContent='lag 0'; svg.appendChild(txt);
  if(off!=null&&isFinite(off)){ svg.appendChild(mk('line',{x1:X(off),y1:pad,x2:X(off),y2:H-pad,stroke:'#4caf50','stroke-width':2}));
    const t2=mk('text',{x:X(off)+4,y:H-pad-6,fill:'#4caf50','font-size':'12'}); t2.textContent='峰值 '+off.toFixed(1)+'ms'; svg.appendChild(t2); }
  const d=pts.map((p,i)=>(i?'L':'M')+X(p[0]).toFixed(1)+' '+Y(p[1]).toFixed(1)).join(' ');
  svg.appendChild(mk('path',{d,fill:'none',stroke:'#2c7be5','stroke-width':2}));
  const mid=mk('text',{x:pad,y:H-8,fill:'#8b97a8','font-size':'12'}); mid.textContent=x0.toFixed(0)+' ms'; svg.appendChild(mid);
  const mx=mk('text',{x:W-pad-60,y:H-8,fill:'#8b97a8','font-size':'12'}); mx.textContent=x1.toFixed(0)+' ms'; svg.appendChild(mx);
}

async function loadHistory(){
  const d=await api('/api/history');
  $('history').innerHTML=(d.reports||[]).map(r=>`<tr>
    <td class="small">${esc(r.time)}</td><td class="small">${esc(r.kind==='sync'?'上传样本':'直播流')}</td>
    <td class="small">${esc(r.room_id||'—')}</td><td class="small">${esc(r.title||'—')}</td>
    <td><a href="${esc(r.report_url)}" target="_blank">查看</a></td></tr>`).join('')
    ||'<tr><td colspan="5" class="muted small">暂无历史</td></tr>';
}
loadHistory();
</script></body></html>
"""
