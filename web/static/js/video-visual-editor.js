/* Visual ffmpeg editor. Existing /run API remains compatible. */
const _ve={queue:[],selected:-1,duration:0,trimIn:0,trimOut:0,output:'',drag:null};
const veEl=id=>document.getElementById(id);
const veFmt=s=>`${String(Math.floor((s||0)/60)).padStart(2,'0')}:${String(Math.floor((s||0)%60)).padStart(2,'0')}`;
function veName(){return veEl('veVisualEditor')?.dataset.videoName||''}
async function veLoadSource(){
  const input=veEl('veInput')?.value.trim(); if(!input)return toast('入力ファイルを選んでください','w');
  const video=veEl('veVideo'); video.src=`/api/video-edit/preview?path=${encodeURIComponent(input)}`; video.load();
  veEl('veStrip').querySelectorAll(':scope > img').forEach(x=>x.remove());
  veEl('veProgressText').textContent='サムネイルと波形を生成中…';
  try{const r=await fetch(`/api/video-edit/assets?path=${encodeURIComponent(input)}&count=8`),d=await r.json();if(!r.ok)throw Error(d.detail||'素材生成失敗');
    _ve.duration=Number(d.duration||0);_ve.trimIn=0;_ve.trimOut=_ve.duration;
    for(const src of d.frames){const img=document.createElement('img');img.src=src;veEl('veStrip').insertBefore(img,veEl('veTrimRange'))}
    veEl('veWaveform').src=d.waveform;veEl('veProgressText').textContent=d.cached?'キャッシュ済み素材を読み込みました':'編集素材を生成しました';veUpdateTimeline();
  }catch(e){toast(e.message,'e');veEl('veProgressText').textContent=e.message}
}
function veTogglePlay(){const v=veEl('veVideo');if(!v.src)return;v.paused?v.play():v.pause()}
function veSeekTo(v){const video=veEl('veVideo');if(_ve.duration)video.currentTime=Number(v)/1000*_ve.duration}
function veUpdateTimeline(){
  const d=_ve.duration||1, left=_ve.trimIn/d*100, right=100-_ve.trimOut/d*100,range=veEl('veTrimRange');if(!range)return;
  range.style.left=left+'%';range.style.right=right+'%';
  const trim=_ve.queue.find(x=>x.operation==='trim');if(trim){trim.params.start=+_ve.trimIn.toFixed(3);trim.params.end=+_ve.trimOut.toFixed(3)}
  veRenderInspector();veRenderQueue();
}
function veDefault(op,kind){
  if(op==='trim')return {start:_ve.trimIn,end:_ve.trimOut||_ve.duration,reencode:true};
  if(op==='fade')return {video_in:1,video_out:1,audio_in:1,audio_out:1};
  if(op==='loop_to_duration')return {duration:Math.max(_ve.duration,10800),fade_out:3};
  if(op==='replace_audio')return {audio_path:'',volume_db:0};
  if(op==='burn_overlay')return {overlay_type:kind||'text',text:'TEXT',overlay_path:'',x:35,y:28,w:28,h:24,position:'(W-w)*0.35:(H-h)*0.28',size:kind==='image'?500:64,opacity:1};
  return {};
}
function veAddOperation(op,kind){_ve.queue.push({operation:op,params:veDefault(op,kind)});_ve.selected=_ve.queue.length-1;document.querySelectorAll('.ve-tool-rail button').forEach(b=>b.classList.toggle('active',b.dataset.op===op));veRenderQueue();veRenderInspector();if(op==='burn_overlay'){veEl('veOverlay').hidden=false;veEl('veOverlayLabel').textContent=kind==='text'?'TEXT':'IMAGE'}}
const veLabels={trim:'トリム',fade:'フェード',loop_to_duration:'ループ',replace_audio:'音声差し替え',burn_overlay:'焼き込み'};
function veRenderQueue(){const q=veEl('veQueue');if(!q)return;q.innerHTML=_ve.queue.length?_ve.queue.map((x,i)=>`<div class="ve-queue-item"><span>${i+1}</span><button style="color:inherit;text-align:left" onclick="_ve.selected=${i};veRenderInspector()">${veLabels[x.operation]||x.operation}</button><button onclick="veRemove(${i})">×</button></div>`).join(''):'<div class="ve-inspector-empty">操作はまだありません</div>'}
function veRemove(i){_ve.queue.splice(i,1);_ve.selected=Math.min(_ve.selected,_ve.queue.length-1);veRenderQueue();veRenderInspector()}
function veField(label,key,type='number',step='0.1'){const x=_ve.queue[_ve.selected],v=x?.params[key]??'';return `<label class="veInspectorLabel">${label}</label><input type="${type}" step="${step}" value="${String(v).replace(/"/g,'&quot;')}" oninput="veSetParam('${key}',this.value,'${type}')">`}
function veRenderInspector(){const el=veEl('veInspector'),x=_ve.queue[_ve.selected];if(!el)return;if(!x){el.className='ve-inspector-empty';el.innerHTML='左のツールを選んでください';return}el.className='ve-inspector-grid';
  if(x.operation==='trim')el.innerHTML=veField('イン点（秒）','start')+veField('アウト点（秒）','end');
  else if(x.operation==='fade')el.innerHTML=veField('映像フェードイン','video_in')+veField('映像フェードアウト','video_out')+veField('音声フェードイン','audio_in')+veField('音声フェードアウト','audio_out');
  else if(x.operation==='loop_to_duration')el.innerHTML=veField('目標尺（秒）','duration')+veField('終端フェード','fade_out');
  else if(x.operation==='replace_audio')el.innerHTML=veField('音声ファイル','audio_path','text')+veField('音量 dB','volume_db');
  else el.innerHTML=veField(x.params.overlay_type==='text'?'文字':'画像ファイル',x.params.overlay_type==='text'?'text':'overlay_path','text')+veField('サイズ','size','range','1')+veField('不透明度','opacity','range','0.01');
}
function veSetParam(key,val,type){const x=_ve.queue[_ve.selected];if(!x)return;x.params[key]=type==='text'?val:Number(val);if(x.operation==='trim'){_ve.trimIn=Math.max(0,Number(x.params.start));_ve.trimOut=Math.min(_ve.duration,Number(x.params.end));veUpdateTimeline()}if(x.operation==='burn_overlay'&&key==='text')veEl('veOverlayLabel').textContent=val;veRenderQueue()}
function veBindDrag(el,onmove){el.addEventListener('pointerdown',e=>{e.preventDefault();const timeline=veEl('veStrip').getBoundingClientRect();_ve.drag={el,timeline};el.setPointerCapture(e.pointerId)});el.addEventListener('pointermove',e=>{if(!_ve.drag||_ve.drag.el!==el)return;onmove(Math.max(0,Math.min(1,(e.clientX-_ve.drag.timeline.left)/_ve.drag.timeline.width)))});el.addEventListener('pointerup',e=>{_ve.drag=null;try{el.releasePointerCapture(e.pointerId)}catch(_){}})}
function veBindOverlay(){const box=veEl('veOverlay'),preview=veEl('vePreview'),resize=box?.querySelector('.ve-resize');if(!box||box.dataset.bound)return;box.dataset.bound='1';
  box.addEventListener('pointerdown',e=>{if(e.target===resize)return;const r=box.getBoundingClientRect();_ve.drag={type:'overlay',dx:e.clientX-r.left,dy:e.clientY-r.top};box.setPointerCapture(e.pointerId)});
  resize.addEventListener('pointerdown',e=>{e.stopPropagation();_ve.drag={type:'resize',x:e.clientX,y:e.clientY,w:box.offsetWidth,h:box.offsetHeight};resize.setPointerCapture(e.pointerId)});
  document.addEventListener('pointermove',e=>{if(!_ve.drag)return;const p=preview.getBoundingClientRect();if(_ve.drag.type==='overlay'){box.style.left=Math.max(0,Math.min(p.width-box.offsetWidth,e.clientX-p.left-_ve.drag.dx))/p.width*100+'%';box.style.top=Math.max(0,Math.min(p.height-box.offsetHeight,e.clientY-p.top-_ve.drag.dy))/p.height*100+'%'}else if(_ve.drag.type==='resize'){box.style.width=Math.max(30,_ve.drag.w+e.clientX-_ve.drag.x)/p.width*100+'%';box.style.height=Math.max(20,_ve.drag.h+e.clientY-_ve.drag.y)/p.height*100+'%'}veSyncOverlay()});document.addEventListener('pointerup',()=>{_ve.drag=null})}
function veSyncOverlay(){const box=veEl('veOverlay'),p=veEl('vePreview')?.getBoundingClientRect(),x=_ve.queue[_ve.selected];if(!box||!p||x?.operation!=='burn_overlay')return;const r=box.getBoundingClientRect();x.params.x=(r.left-p.left)/p.width*100;x.params.y=(r.top-p.top)/p.height*100;x.params.w=r.width/p.width*100;x.params.h=r.height/p.height*100;x.params.position=`(W-w)*${(x.params.x/100).toFixed(4)}:(H-h)*${(x.params.y/100).toFixed(4)}`;x.params.size=Math.round((x.params.overlay_type==='image'?1920:1080)*x.params.w/100);veRenderInspector()}
async function veRunQueue(){const input=veEl('veInput').value.trim();if(!_ve.queue.length)return toast('操作をキューに追加してください','w');veEl('veRunBtn').disabled=true;const r=await fetch('/api/video-edit/run-queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_path:input,video_name:veName(),operations:_ve.queue})}),d=await r.json();if(!r.ok){veEl('veRunBtn').disabled=false;return toast(d.detail||'受付失敗','e')}vePoll(d.job_id)}
async function vePoll(id){const d=await (await fetch(`/api/video-edit/status?job_id=${encodeURIComponent(id)}`)).json(),pct=Number(d.progress||0);veEl('veProgressBar').style.width=pct+'%';veEl('veProgressText').textContent=`${d.status} ${pct.toFixed(1)}%`;if(d.status==='completed'){_ve.output=d.output_path;veEl('veRunBtn').disabled=false;veShowResult();return}if(d.status==='failed'){veEl('veRunBtn').disabled=false;return toast(d.error||'書き出し失敗','e')}setTimeout(()=>vePoll(id),1000)}
function veShowResult(){const el=veEl('veResult');el.hidden=false;el.innerHTML=`<video controls src="/api/video-edit/preview?path=${encodeURIComponent(_ve.output)}"></video><div class="fh">${_ve.output}</div><div class="ve-result-actions"><button class="btn btn-primary" onclick="veAdopt()">この動画で差し替える</button><button class="btn btn-secondary" onclick="veUseThumbnail()">現在位置をサムネに使う</button></div>`}
async function veAdopt(){if(!confirm('既存動画を退避リネームし、この出力で差し替えます。よろしいですか？'))return;const target=veEl('veInput').value.trim();const r=await fetch('/api/video-edit/adopt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_name:veName(),output_path:_ve.output,target_path:target,confirmed:true})}),d=await r.json();if(!r.ok)return toast(d.detail||'差し替え失敗','e');toast(`差し替えました（退避: ${d.backup_path}）`,'s')}
async function veUseThumbnail(){const time=veEl('veVideo').currentTime||0,input=_ve.output||veEl('veInput').value.trim();const r=await fetch('/api/video-edit/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation:'extract_frame',input_path:input,video_name:veName(),params:{time}})}),d=await r.json();if(!r.ok)return toast(d.detail||'抽出失敗','e');vePollThumb(d.job_id)}
async function vePollThumb(id){const d=await (await fetch(`/api/video-edit/status?job_id=${id}`)).json();if(d.status==='completed'){const r=await fetch('/api/video-edit/use-thumbnail',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({video_name:veName(),frame_path:d.output_path})}),x=await r.json();return r.ok?toast(`サムネ素材へ保存: ${x.path}`,'s'):toast(x.detail,'e')}if(d.status==='failed')return toast(d.error,'e');setTimeout(()=>vePollThumb(id),700)}
document.addEventListener('pointerdown',()=>{const root=veEl('veVisualEditor');if(!root||root.dataset.ready)return;root.dataset.ready='1';veBindDrag(veEl('veTrimInHandle'),p=>{_ve.trimIn=Math.min(_ve.trimOut-.01,p*_ve.duration);veUpdateTimeline()});veBindDrag(veEl('veTrimOutHandle'),p=>{_ve.trimOut=Math.max(_ve.trimIn+.01,p*_ve.duration);veUpdateTimeline()});veBindOverlay();const v=veEl('veVideo');v.addEventListener('loadedmetadata',()=>{if(!_ve.duration){_ve.duration=v.duration;_ve.trimOut=v.duration;veUpdateTimeline()}});v.addEventListener('timeupdate',()=>{const d=_ve.duration||v.duration||1,p=v.currentTime/d;veEl('veSeek').value=p*1000;veEl('vePlayhead').style.left=p*100+'%';veEl('veClock').textContent=`${veFmt(v.currentTime)} / ${veFmt(d)}`})});
