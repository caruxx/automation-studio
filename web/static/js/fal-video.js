/* Optional fal cloud generation. No automatic transfers, paid submits or retries. */
(function(root){
  'use strict';
  const API='/api/fal-video';
  const priceFallback={checked_at:'2026-10-03',valid_until:'2026-10-04T00:00:00+00:00',promo_per_second:{'480P':'0.015','768P':'0.024','1080P':'0.048'},normal_per_second:{'480P':'0.025','768P':'0.04','1080P':'0.08'},output_overrun_allowance:0.7};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=v=>Number(v).toFixed(4);
  const hex=buffer=>Array.from(new Uint8Array(buffer),b=>b.toString(16).padStart(2,'0')).join('');
  const get=id=>document.getElementById('fal-'+id);
  let session=0,state;
  async function request(path,body,extraHeaders={}){
    const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json',...extraHeaders},body:JSON.stringify(body)};
    const r=await fetch(API+path,options);
    let data;try{data=await r.json()}catch(_){throw Error('HTTP '+r.status)}
    if(!r.ok){if(r.status===404)throw Error('fal APIはサーバー反映待ちです。未保存内容を保存して通常サーバーを再起動すると使えます。');throw Error(typeof data.detail==='string'?data.detail:('HTTP '+r.status))}
    return data;
  }
  function message(text,error=false){if(get('message')){get('message').textContent=text;get('message').style.color=error?'var(--accent-danger)':'var(--text-secondary)'}}
  function readyPaid(){return state?.caps?.key_configured&&state?.caps?.paid_submit_enabled}
  function estimate(){
    const p=state?.caps?.price||priceFallback;
    const seconds=Number(get('duration').value),resolution=get('resolution').value,budget=Number(get('budget').value);
    if(Date.now()>=Date.parse(p.valid_until))throw Error('料金表の確認期限が過ぎました。公式単価の再確認が必要です。');
    if(!Number.isFinite(seconds)||seconds<0.92||seconds>15)throw Error('尺は0.92〜15秒で指定してください。');
    const estimated=Number(p.promo_per_second[resolution])*seconds;
    const reserved=Math.ceil(Number(p.normal_per_second[resolution])*(seconds+p.output_overrun_allowance)*10000)/10000;
    if(!Number.isFinite(budget)||budget<=0||budget>10||reserved>budget)throw Error('通常単価と尺の余裕を含む予約額が予算上限を超えています。');
    return {estimated,reserved,budget,seconds,resolution,p};
  }
  function showEstimate(){try{
    const q=estimate();get('estimate').textContent=`1本の概算 $${money(q.estimated)} ／予約額 $${money(q.reserved)} ／上限 $${money(q.budget)}。料金表有効期限 ${new Date(q.p.valid_until).toLocaleString()}。税・為替・手数料別。上限はfal側の請求額を強制制限するものではありません。`;
  }catch(e){get('estimate').textContent=e.message}}
  function status(){
    const c=state.caps;
    get('connection').textContent=c?`API接続済み ／キー ${c.key_configured?'設定あり':'未設定'} ／有料送信 ${c.paid_submit_enabled?'有効':'未有効化'} ／生成計算はfalクラウド、操作・履歴・保存はこのMac。残高は公式ダッシュボードで確認してください。`:'API反映待ち。概算の確認はできます。キー発行・保存・チャージはこの画面では行いません。';
    get('quote').disabled=!c;
    uploadButton();
  }
  function uploadButton(){
    const matches=!state?.plan||!get('preset')?.value||state.selected?.sha===state.plan.source.sha256;
    if(get('upload'))get('upload').disabled=!(matches&&state?.selected&&state?.caps?.key_configured&&get('transfer').checked&&!state.uploadBusy);
  }
  async function chooseFile(){
    const current=session, owned=state, file=get('file').files[0];state.selected=null;state.uploaded=null;get('image-url').value='';get('transfer').checked=false;uploadButton();
    if(!file)return;
    try{
      if(file.size>8*1024*1024)throw Error('PNG原本は8 MiB以下で選択してください。');
      const bytes=await file.arrayBuffer(),v=new DataView(bytes);
      if(bytes.byteLength<33||[137,80,78,71,13,10,26,10].some((n,i)=>v.getUint8(i)!==n))throw Error('PNG原本を選択してください。');
      const width=v.getUint32(16),height=v.getUint32(20),sha=hex(await crypto.subtle.digest('SHA-256',bytes));
      if(current!==session||owned!==state||!get('source'))return;state.selected={file,sha,width,height};
      get('source').textContent=`${file.name} ／${width}×${height} ／${file.size} bytes\nSHA-256: ${sha}\n選択しただけではfalへ送信されません。`;
      uploadButton();
      if(get('preset').value!==''&&state.plan&&sha!==state.plan.source.sha256)message('採用原本のハッシュと一致しません。Wobble Dayの3案には指定原本を選んでください。',true);
    }catch(e){if(current===session)message(e.message,true)}
  }
  async function upload(){
    if(!state.selected||!get('transfer').checked||!state.caps?.key_configured||state.uploadBusy)return;
    if(get('preset').value!==''&&state.plan&&state.selected.sha!==state.plan.source.sha256){message('採用原本のハッシュと一致しません。転送しません。',true);return;}
    const current=session, owned=state, selected=state.selected;state.uploadBusy=true;get('file').disabled=true;uploadButton();
    try{
      const form=new FormData();form.append('image',selected.file);form.append('approved_sha256',selected.sha);form.append('confirmation','UPLOAD_SELECTED_PNG');
      const r=await fetch(API+'/images/upload',{method:'POST',headers:{'X-Fal-Intent':'image-transfer-approved'},body:form});
      let data;try{data=await r.json()}catch(_){throw Error('転送結果を確認できません。自動再送しません。')}
      if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:('HTTP '+r.status));
      if(current!==session||owned!==state||!get('file'))return;state.uploaded={url:data.image_url,sha:data.sha256};get('image-url').value=data.image_url;get('transfer').checked=false;message('原本を転送しました。次に見積りを保存し、画像とプロンプトを確認してください。');
    }catch(e){if(current===session)message(e.message,true)}finally{owned.uploadBusy=false;if(current===session&&get('file')){get('file').disabled=false;uploadButton()}}
  }
  async function quote(){
    if(!state.caps||state.quoteBusy)return;
    const current=session,owned=state,button=get('quote');state.quoteBusy=true;button.disabled=true;
    try{
      estimate();
      if(get('preset').value!==''&&state.plan&&(state.uploaded?.sha!==state.plan.source.sha256||state.uploaded?.url!==get('image-url').value.trim()))throw Error('指定の採用原本を照合・転送したURLで見積りを保存してください。概算だけの確認では転送不要です。');
      const job=await request('/quotes',{spec:{prompt:get('prompt').value,duration:Number(get('duration').value),resolution:get('resolution').value,image_url:get('image-url').value.trim(),loop:get('loop').checked,project_tag:get('tag').value.trim()},max_usd:get('budget').value.trim()});
      if(current!==session||owned!==state||!get('quote'))return;try{localStorage.setItem('automationStudioFalJobId',job.id)}catch(_){}
      await history();message('見積りを保存しました。falへの生成依頼はまだ送信していません。');
    }catch(e){if(current===session)message(e.message,true)}finally{owned.quoteBusy=false;if(current===session&&button.isConnected)button.disabled=!state.caps}
  }
  const statuses={quoted:'見積り保存済み',submitting:'送信結果の照合が必要',submission_unknown:'送信結果不明・再送停止',submitted:'送信済み',queued:'待機中',running:'生成中',ready:'完成・取得待ち',completed:'取得・形式確認済みMP4（目視確認前）',failed:'生成失敗'};
  function jobsHtml(jobs){
    return jobs.map(job=>{
      const id=job.id;if(!/^[0-9a-f]{32}$/.test(id))return '';
      const q=job.quote||{},p=job.approval_preview||{};
      const valid=Date.now()/1000<job.expires;
      let actions='';
      if(job.status==='quoted')actions=`<label><input type="checkbox" data-approve="${id}"> 下の画像URL・プロンプトの送信と上限 $${esc(q.max_usd)} の有料生成1回を承認する</label><p><button class="btn btn-primary btn-sm" data-action="submit" data-id="${id}" ${!readyPaid()||!valid?'disabled':''}>承認して有料生成を依頼</button>${!valid?' 見積り期限切れ':''}</p>`;
      if(job.request_id&&job.status!=='completed'&&job.status!=='failed')actions+=`<button class="btn btn-secondary btn-sm" data-action="refresh" data-id="${id}">同じジョブの状況を確認</button> <button class="btn btn-ghost btn-sm" data-action="cancel" data-id="${id}">キャンセルを要求</button>`;
      if(job.status==='ready')actions+=` <button class="btn btn-secondary btn-sm" data-action="download" data-id="${id}">完成MP4を取得・検証</button>`;
      if(job.status==='completed')actions+=` <a class="btn btn-secondary btn-sm" href="${API}/jobs/${id}/file" download>元MP4を保存</a><p>${esc(job.media?.width)}×${esc(job.media?.height)} ／${esc(job.media?.duration)}秒 ／${esc(job.media?.bytes)} bytes</p>${job.normalized_media?`<a class="btn btn-secondary btn-sm" href="${API}/jobs/${id}/file?normalized=true" download>1280×720版を保存</a>`:`<button class="btn btn-secondary btn-sm" data-action="normalize" data-id="${id}">Macで1280×720に整形</button>`}`;
      return `<article class="card" style="margin:12px 0;padding:14px"><strong>${esc(job.project_tag||id.slice(0,12))} — ${esc(statuses[job.status]||job.status)}</strong><p>${esc(p.duration||job.summary?.duration)}秒 ／${esc(p.resolution||job.summary?.resolution)} ／概算 $${esc(q.estimated_usd)} ／予約額 $${esc(q.reserved_usd)}</p>${job.error||job.normalize_error?`<p style="color:var(--accent-warning)">${esc(job.error||job.normalize_error)}</p>`:''}<details><summary>送信対象とジョブIDを確認</summary><p style="overflow-wrap:anywhere">${esc(p.image_url||'画像URLは未取得')}<br>終了画像: ${esc(p.end_image_url||'指定なし')}<br>ジョブID: ${esc(id)}${job.request_id?'<br>fal ID: '+esc(job.request_id):''}</p><pre style="white-space:pre-wrap;max-height:180px;overflow:auto">${esc(p.prompt||'プロンプトは未取得')}</pre></details><p>${esc(job.loop_warning||'')}</p>${actions}</article>`;
    }).join('')||'<p class="fh">保存済みの見積り・生成履歴はありません。</p>';
  }
  async function history(){
    const current=session;
    try{const data=await request('/jobs');if(current!==session||!get('history'))return;state.jobs=data.jobs||[];get('history').innerHTML=jobsHtml(state.jobs)}
    catch(e){if(current===session&&get('history'))get('history').textContent=e.message}
  }
  async function jobAction(event){
    const button=event.target.closest('button[data-action]');if(!button)return;
    const id=button.dataset.id,action=button.dataset.action;
    if(!/^[0-9a-f]{32}$/.test(id)||state.busy.has(id))return;
    if(action==='submit'&&(!readyPaid()||!get('history').querySelector(`[data-approve="${id}"]`)?.checked)){message('キー・有料送信の有効化と、この見積りの明示承認が必要です。',true);return;}
    const job=state.jobs.find(j=>j.id===id);if(!job)return;
    const current=session,owned=state;state.busy.add(id);button.disabled=true;
    try{
      const body=action==='submit'?{confirmation:'GENERATE_PAID_VIDEO',approve_transfer:true,approved_max_usd:job.quote.max_usd}:{};
      await request('/jobs/'+id+'/'+action,body,action==='submit'?{'X-Fal-Intent':'user-approved'}:{});
      if(current!==session||owned!==state)return;await history();message(action==='submit'?'1回だけ送信しました。以後は同じジョブの状況を確認してください。':action==='cancel'?'キャンセルを要求しました。停止や無課金は保証されません。':'同じジョブを更新しました。');
    }catch(e){if(current===session)message(e.message,true)}finally{owned.busy.delete(id);if(current===session&&button.isConnected)button.disabled=false}
  }
  async function loadPresets(){
    const current=session,owned=state;try{
      const r=await fetch('/static/fal-wobble-plan.json');if(!r.ok)return;
      const plan=await r.json();if(current!==session||owned!==state||!get('preset'))return;state.plan=plan;
      get('preset').innerHTML='<option value="">自由入力</option>'+plan.loops.map((p,i)=>`<option value="${i}">Wobble Day ${esc(p.name)}</option>`).join('');
      get('preset').onchange=()=>{const key=get('preset').value;if(key===''){uploadButton();return;}const p=plan.loops[Number(key)];if(!p)return;get('prompt').value=p.prompt;get('duration').value=plan.duration;get('resolution').value=plan.resolution;get('loop').checked=plan.loop;get('budget').value=plan.max_usd_per_job;get('tag').value=(state.project?state.project+' / ':'')+p.name;get('expected-source').textContent=`採用原本: ${plan.source.file} ／${plan.source.width}×${plan.source.height}\nSHA-256: ${plan.source.sha256}\n3本は別々に生成。追加再生成は未承認。`;showEstimate();uploadButton()};
    }catch(_){}
  }
  async function open(project=''){
    session++;state={project,caps:null,jobs:[],busy:new Set(),selected:null,uploadBusy:false,quoteBusy:false};
    root.modal(`<div class="mo-head"><div class="mo-title">fal 動画 — H3 Max Turbo</div><button class="mo-x" onclick="closeModal()">×</button></div><div class="mo-body" style="max-height:80vh;overflow:auto">
      <p id="fal-connection" class="fh">接続状態を確認しています…</p><p class="fh">既存のレイヤーモーション・PNG制作とは別の任意機能です。自動生成・自動再送は行いません。キーはこの画面で発行・保存しません。</p>
      <div class="ff"><label class="fl" for="fal-preset">動作案</label><select class="fi" id="fal-preset"><option value="">自由入力</option></select><pre class="fh" id="fal-expected-source" style="white-space:pre-wrap"></pre></div>
      <details><summary>ローカルのPNG原本を選ぶ</summary><p class="fh">選択はこのMac内だけで照合します。転送ボタンを押すまでfalへ送りません。</p><input id="fal-file" type="file" accept="image/png"><pre id="fal-source" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre><label><input type="checkbox" id="fal-transfer"> このPNG原本のfal転送を承認する</label><p><button class="btn btn-secondary btn-sm" id="fal-upload" disabled>選択原本をfalへ転送</button></p></details>
      <div class="ff"><label class="fl" for="fal-image-url">開始画像の公開URL</label><input class="fi" id="fal-image-url" placeholder="https://…fal.media/… または storage.googleapis.com"><div class="fh">署名・認証情報を含まないURL。ローカル原本を転送した場合はここへ反映します。</div></div>
      <div class="ff"><label class="fl" for="fal-prompt">動作プロンプト</label><textarea class="fi" id="fal-prompt" rows="5" maxlength="50000"></textarea></div>
      <div style="display:flex;gap:12px;flex-wrap:wrap"><div class="ff"><label class="fl" for="fal-duration">尺（秒）</label><input class="fi" id="fal-duration" type="number" min="0.92" max="15" step="0.01" value="8"></div><div class="ff"><label class="fl" for="fal-resolution">解像度</label><select class="fi" id="fal-resolution"><option value="480P">480P</option><option value="768P" selected>768P</option><option value="1080P">1080P（768Pからの精細化）</option></select></div><div class="ff"><label class="fl" for="fal-budget">1回の上限（USD）</label><input class="fi" id="fal-budget" type="number" min="0.01" max="10" step="0.01" value="0.40"></div></div>
      <label><input id="fal-loop" type="checkbox" checked> 開始と終了に同じ画像を指定する（完全なループは保証されません）</label><div class="ff"><label class="fl" for="fal-tag">保存メモ／対象プロジェクト</label><input class="fi" id="fal-tag" maxlength="100" value="${esc(project)}"></div>
      <p id="fal-estimate" class="fh"></p><div class="btns"><button class="btn btn-secondary btn-sm" id="fal-estimate-button">概算を確認</button><button class="btn btn-primary btn-sm" id="fal-quote" disabled>見積りを保存（falへ送信しない）</button><button class="btn btn-ghost btn-sm" id="fal-refresh">履歴を更新</button></div><p id="fal-message" role="status" aria-live="polite"></p>
      <h3>見積り・生成履歴</h3><div id="fal-history">読み込み中…</div><p class="fh">完成MP4は検証してから明示的に保存・取り込みます。元画像や既存動画を上書きしません。<a href="https://fal.ai/models/minimax/h3-max-turbo/image-to-video" target="_blank" rel="noopener">公式仕様・単価</a></p></div>`);
    get('file').onchange=chooseFile;get('transfer').onchange=uploadButton;get('upload').onclick=upload;get('quote').onclick=quote;get('estimate-button').onclick=showEstimate;get('refresh').onclick=history;get('history').onclick=jobAction;
    ['duration','resolution','budget'].forEach(id=>get(id).onchange=showEstimate);showEstimate();loadPresets();
    const current=session;
    try{const caps=await request('/capabilities');if(current!==session)return;state.caps=caps}catch(e){message(e.message,true)}
    if(current!==session||!get('connection'))return;status();await history();
  }
  root.openFalVideo=open;
  root.FalVideoUI={open};
})(window);
