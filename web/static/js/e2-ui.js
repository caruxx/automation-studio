(function(){
  const $ = (id)=>document.getElementById(id);
  const esc = window.esc || ((s)=>String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
  const SETTINGS_TABS = ['channel','posting','production','integration','advanced'];
  const SETTINGS_TAB_LABELS = {channel:'チャンネル', posting:'投稿', production:'制作', integration:'連携・通知', advanced:'詳細'};
  const SECRET_RE = /api_key|secret|token|webhook/i;
  const state = {catalog:null, channels:[], activeChannelId:'', activeSettingsTab:'channel'};

  function card(id, html){
    const el=$(id)?.querySelector('.e2-card-body');
    if(el) el.innerHTML = html;
  }
  function link(page, label, extra=''){
    return `<div class="e2-card-link"><a href="#${esc(page)}" onclick="go('${esc(page)}');${extra}return false">${esc(label)}</a></div>`;
  }
  function dateText(v){
    if(!v) return '';
    const d = new Date(v);
    if(Number.isNaN(d.getTime())) return String(v);
    return d.toLocaleString('ja-JP', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  }
  async function fetchJson(url, opts){
    const r = await fetch(url, opts);
    if(!r.ok) throw new Error(`${url} HTTP ${r.status}`);
    return await r.json();
  }

  async function loadDashboardCards(){
    const targets=['e2CardTodaySchedule','e2CardInProgress','e2CardErrors'];
    targets.forEach(id=>card(id, '読み込み中...'));
    const [runs, process, rq, workers, health, stock] = await Promise.allSettled([
      fetchJson('/api/runs/active'),
      fetchJson('/api/process/status'),
      fetchJson('/api/render-queue?limit=20'),
      fetchJson('/api/workers/status'),
      fetchJson('/api/health'),
      fetchJson('/api/stock'),
    ]);
    renderHomeSummaries(runs.value, process.value, rq.value, workers.value, health.value);
    if(workers.value?.update_guard) mirrorUpdateGuard(workers.value.update_guard);
    mirrorUpdatePanel();
  }

  function renderHomeSummaries(runs, process, rq, workers, health){
    const today = new Date().toLocaleDateString('sv-SE', {timeZone:'Asia/Tokyo'});
    const scheduled=[];
    (runs?.channels||[]).forEach(ch=>(ch.vols||[]).forEach(v=>{
      if(v.publish_date && String(v.publish_date).slice(0,10)===today) scheduled.push({ch,v});
    }));
    const next=scheduled[0];
    card('e2CardTodaySchedule', `<div class="e2-card-kpi">${scheduled.length}</div><div>${next?`${esc(next.ch.name||next.ch.channel_name||'')} vol.${esc(next.v.vol||'')}`:'今日の予約はありません'}</div>${link('videos','予約を確認')}`);
    const active=(runs?.active_jobs||[]).filter(j=>['','running','in_progress'].includes(String(j.status||'').toLowerCase()));
    const rqRunning=Number(rq?.counts?.running||0);
    const n=active.length+(process?.running?1:0)+rqRunning;
    const healthText=health?.status==='ok'?'サーバー正常':`ヘルス ${esc(health?.status||'unknown')}`;
    card('e2CardInProgress', `<div class="e2-card-kpi">${n}</div><div>実行中 ${active.length+(process?.running?1:0)} / 書き出し ${rqRunning}</div><div class="e2-card-sub">${healthText}</div>${link('videos','制作を確認')}`);
    const errors=[];
    (runs?.history_recent||[]).forEach(x=>{if(String(x.status||'').includes('fail')||x.error_message)errors.push(x)});
    (rq?.jobs||[]).forEach(x=>{if(x.status==='error'||x.error_message)errors.push(x)});
    const tripped=(workers?.channels||[]).filter(c=>c.tripped).length;
    const total=errors.length+tripped;
    card('e2CardErrors', `<div class="e2-card-kpi ${total?'e2-health-bad':'e2-health-ok'}">${total}</div><div>${tripped?`ブレーカー停止 ${tripped}件`:total?'確認が必要なエラーがあります':'問題は見つかりません'}</div>${link('ledger','詳細を確認')}`);
  }

  function renderActiveRuns(runs, process){
    // 「進行中」は実際に走っているものだけに限定する。
    // 定義: runs.db 由来の /api/runs/active.active_jobs + /api/process/status.running の process task。
    // 未完了volの current stage は「次にやる工程」であり、停止中/過去の候補も含むためここでは数えない。
    const activeJobs = (runs?.active_jobs || []).filter(j => {
      const st = String(j.status || '').toLowerCase();
      return !st || st === 'running' || st === 'in_progress';
    });
    const runningProcess = !!process?.running;
    const n = activeJobs.length + (runningProcess ? 1 : 0);
    const first = activeJobs[0];
    const detail = first
      ? `${esc(first.channel_name || first.channel_id || '')} / vol.${esc(first.vol || '')} / ${esc(first.kind || first.stage || first.from_stage || '実行中')}`
      : (runningProcess ? '楽曲後処理が実行中です' : '進行中の工程はありません');
    card('e2CardActiveRuns', `<div class="e2-card-kpi">${n}</div><div>${detail}</div>${link('videos','動画を確認')}`);
  }

  function renderSchedule(runs){
    const items = [];
    (runs?.channels||[]).forEach(ch => (ch.vols||[]).forEach(v=>{
      if(v.publish_date && !v.is_uploaded) items.push({ch, v, t:new Date(v.publish_date).getTime()});
    }));
    items.sort((a,b)=>a.t-b.t);
    const next = items.find(x=>!Number.isNaN(x.t) && x.t >= Date.now()) || items[0];
    if(!next){ card('e2CardNextSchedule', `予約済みvolは見つかりません。${link('videos','動画一覧へ')}`); return; }
    card('e2CardNextSchedule', `<div class="e2-card-kpi">vol.${esc(next.v.vol)}</div><div>${dateText(next.v.publish_date)}</div><div class="e2-card-sub">${esc(next.ch.name || next.ch.channel_name || '')}</div>${link('videos','予約を確認')}`);
  }

  function renderRecentError(runs, process, rq){
    const errors = [];
    (runs?.history_recent||[]).forEach(x=>{ if(String(x.status||'').includes('fail') || x.error_message) errors.push(x); });
    (rq?.jobs||[]).forEach(x=>{ if(x.status==='error' || x.error_message) errors.push(x); });
    const pLogs = process?.logs || [];
    const logErr = [...pLogs].reverse().find(l=>/error|traceback|失敗|エラー/i.test(l));
    if(logErr) errors.unshift({summary:logErr});
    const e = errors[0];
    if(!e){ card('e2CardRecentError', `直近エラーは見つかりません。${link('ledger','実行履歴へ')}`); return; }
    const raw = e.summary || e.error_message || e.failed_stage || e.status || 'エラーあり';
    const h = humanizeLocalError(e.exit_code, raw, e.failed_stage);
    const tech = String(raw || '').slice(0,220);
    card('e2CardRecentError', `<div class="e2-health-bad">${esc(h.plain_ja).slice(0,120)}</div><div class="e2-card-sub">${esc(h.next_action)}</div><details class="e2-card-sub"><summary>詳細ログ</summary>${esc(tech)}</details>${link('ledger','ログを見る')}`);
  }

  function renderQueue(rq){
    const c = rq?.counts || {};
    card('e2CardRenderQueue', `<div class="e2-card-kpi">${Number(c.pending||0)}/${Number(c.running||0)}</div><div>待機 / 実行中</div>${link('automation','書き出しキューを見る',"setTimeout(()=>document.getElementById('renderQueueCard')?.scrollIntoView({behavior:'smooth'}),200);")}`);
  }

  function renderAutopilotCard(ws, stock){
    const chs = ws?.channels || [];
    const on = chs.filter(c=>c.autopilot_enabled).length;
    const suspended = chs.filter(c=>c.autopilot_suspended_by_update).length;
    const warn = suspended ? `<div class="e2-health-warn">更新後の一時停止 ${suspended} 件</div>` : '<div>一時停止なし</div>';
    const stockRows = (stock?.channels || []).slice(0,7).map(c=>{
      const cls = c.warning ? 'e2-health-warn' : 'e2-card-sub';
      return `<div class="${cls}">${esc(c.channel_name || c.channel_id || '-')}: ${esc(String(c.stock_days ?? 0))}日分</div>`;
    }).join('');
    const stockWarn = Number((stock?.warnings || []).length || 0);
    const stockHead = stock ? `<div class="${stockWarn ? 'e2-health-warn' : 'e2-health-ok'}">素材在庫 ${stockWarn ? '警告 '+stockWarn+'件' : 'OK'}</div>${stockRows}` : '';
    card('e2CardAutopilot', `<div class="e2-card-kpi">${on}</div><div>自動運転中のチャンネル</div>${warn}${stockHead}${link('automation','自動運転へ')}`);
  }

  function humanizeLocalError(exitCode, message, stage){
    const text = String(message || '');
    const code = Number(exitCode || (text.match(/\b(75|76|77|78)\b/) || [])[1] || 0);
    const map = {
      75:['SUNOのログインが切れました','SUNOを開いて再ログインし、止まった工程から再開してください。'],
      76:['一時的な通信エラーです','しばらく待ってから再実行してください。'],
      77:['YouTube APIの1日上限に達しました','約24時間待ってから再開してください。'],
      78:['Premiere Proの準備ができていません','Premiere ProとPremiere Linkを起動してから再実行してください。']
    };
    let m = map[code];
    if(!m && /invalid_grant|OAuth|再認証/i.test(text)) m=['YouTubeの再認証が必要です','YouTube設定で対象チャンネルを再認証してください。'];
    if(!m && /Premiere|CEP|preflight/i.test(text)) m=map[78];
    if(!m && /SUNO|ログイン|cookie/i.test(text)) m=map[75];
    if(!m) m=['処理が途中で止まりました','実行履歴で詳細ログを確認し、止まった工程から再実行してください。'];
    return {plain_ja: (stage ? stage + ': ' : '') + m[0], next_action: m[1]};
  }

  function renderHealthCard(h){
    const disk = h?.disk || {};
    const rq = h?.render_queue || {};
    const freeGb = Number.isFinite(Number(disk.free_bytes)) ? (Number(disk.free_bytes) / 1024 / 1024 / 1024).toFixed(1) : null;
    const cls = h?.status === 'ok' ? 'e2-health-ok' : 'e2-health-warn';
    card('e2CardHealth', `<div class="${cls}">${h?.status==='ok'?'正常':'要確認'}</div><div>定期実行: ${h?.scheduler?.orchestrator_registered?'有効':'停止中'}</div><div>書き出しキュー 待機 ${esc(String(rq.pending ?? '-'))} / 実行中 ${esc(String(rq.running ?? '-'))}</div><div class="e2-card-sub">空き ${freeGb ?? '-'} GB</div>${link('ledger','接続状態へ')}`);
  }

  function mirrorUpdateGuard(g){
    const src = $('updateGuardBanner');
    ['e2UpdateBannerSlot','e2UpdatesBannerSlot'].forEach(id=>{
      const slot=$(id); if(!slot) return;
      const n = Number(g?.suspended_count || 0);
      slot.innerHTML = n ? `<div class="ledger-hint" style="margin-bottom:var(--space-3);border-left:3px solid var(--accent-warning)"><strong>更新完了。自動運転を再開してください</strong>（一時停止中 ${n} 件） <button class="btn btn-primary btn-sm" onclick="resumeAutopilotAfterUpdate()">自動運転を再開</button></div>` : '';
    });
    if(src && typeof renderUpdateGuardBanner === 'function') renderUpdateGuardBanner(g);
  }

  function mirrorUpdatePanel(){
    const st = $('versionStatus')?.textContent || '確認中...';
    const rs = $('updateResult')?.innerHTML || '';
    if($('e2VersionStatus')) $('e2VersionStatus').textContent = st;
    if($('e2UpdateResult')) $('e2UpdateResult').innerHTML = rs;
  }

  async function resolveActiveChannel(){
    try{
      const [ch, cfg] = await Promise.all([fetchJson('/api/channels'), fetchJson('/api/config')]);
      state.channels = ch.channels || [];
      const folder = cfg.dashboard?.channel_folder || '';
      const active = (state.channels.find(c=>c.folder===folder) || state.channels[0] || {}).id || '';
      const headerChannel = window.AutomationStudioContext?.channelId || '';
      state.activeChannelId = headerChannel && headerChannel !== 'all' ? headerChannel : active;
    }catch(_){ state.activeChannelId = ''; }
  }

  async function loadSettingsCatalog(){
    const panels = Object.fromEntries(SETTINGS_TABS.map(tab=>[tab, $(`settingsCatalogTab_${tab}`)]));
    if(!panels.advanced || !panels.channel) return;
    const preserved = {
      legacy: $('settingsDetailToggle'),
      update: $('updatePanel'),
      shortcuts: $('settingsChannelShortcuts'),
    };
    SETTINGS_TABS.forEach(tab=>{
      const el = panels[tab];
      if(el) el.innerHTML = tab === state.activeSettingsTab ? '<div class="fh">読み込み中...</div>' : '';
    });
    preserved.legacy?.remove();
    await resolveActiveChannel();
    state.catalog = await fetchJson('/api/settings-catalog');
    const settings = state.catalog.settings || [];
    const rows = await Promise.all(settings.map(renderSettingRow));
    const grouped = Object.fromEntries(SETTINGS_TABS.map(tab=>[tab, []]));
    rows.forEach(row=>{
      const tab = SETTINGS_TABS.includes(row.category) ? row.category : 'advanced';
      grouped[tab].push(row.html);
    });
    SETTINGS_TABS.forEach(tab=>{
      const target = panels[tab];
      if(target) target.innerHTML = grouped[tab].join('') || `<div class="fh">${SETTINGS_TAB_LABELS[tab]}の設定はありません</div>`;
      const count = grouped[tab].length;
      const btn = document.querySelector(`[data-settings-tab="${tab}"]`);
      if(btn) btn.innerHTML = `${SETTINGS_TAB_LABELS[tab]} <span class="e2-tab-count">${count}</span>`;
    });
    const search=$('settingsCatalogSearch');
    if(search && !search.dataset.e2Wired){
      search.addEventListener('input', filterSettingsCatalog);
      search.dataset.e2Wired='1';
    }
    ensureSettingsTabWiring();
    relocateSettingsLegacyPanels(preserved);
    ensureSettingsTools();
    wireSettingsTemplateTool();
    switchSettingsTab(state.activeSettingsTab);
    filterSettingsCatalog();
    loadPostingStrategyRecommendation().catch(()=>{});
    loadImageModulesPanel().catch(()=>{});
    renderReferencePreviewTool().catch(()=>{});
    loadToolTemplateOptions().catch(()=>{});
    loadConfigBackups().catch(()=>{});
    checkAppVersion?.();
    checkUpdateGuard?.();
  }

  function ensureSettingsTabWiring(){
    document.querySelectorAll('[data-settings-tab]').forEach(btn=>{
      const tab = btn.dataset.settingsTab;
      if(btn.dataset.e2Wired) return;
      if(!btn.onclick) btn.addEventListener('click', ()=>switchSettingsTab(tab));
      btn.dataset.e2Wired = '1';
    });
  }

  function switchSettingsTab(tab, fromRouter=false){
    if(!SETTINGS_TABS.includes(tab)) tab = 'channel';
    if(!fromRouter && window.AutomationStudioContext?.navigate){
      window.AutomationStudioContext.navigate({page:'settings',settingsTab:tab});return;
    }
    state.activeSettingsTab = tab;
    document.querySelectorAll('[data-settings-tab]').forEach(btn=>{
      const on = btn.dataset.settingsTab === tab;
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('[data-settings-panel]').forEach(panel=>{
      panel.hidden = panel.dataset.settingsPanel !== tab;
    });
    filterSettingsCatalog();
  }

  function relocateSettingsLegacyPanels(preserved = {}){
    const production = $('settingsCatalogTab_production');
    const defaults = $('automationDefaultsDetails');
    if(production && defaults && defaults.parentElement !== production){
      defaults.hidden = false;
      production.appendChild(defaults);
    }
    const integration = $('settingsCatalogTab_integration');
    const update = preserved.update || $('updatePanel');
    if(integration && update && update.parentElement !== integration){
      integration.appendChild(update);
      update.classList.add('e2-update-card');
    }
    const shortcuts = preserved.shortcuts || $('settingsChannelShortcuts');
    const tools = $('settingsToolsGrid');
    if(shortcuts && tools){
      shortcuts.remove();
    }
  }

  function ensurePostingStrategyPanel(){
    if($('settingsPostingStrategyPanel')) return $('settingsPostingStrategyPanel');
    const anchor = $('settingsCatalogField_channel_publish_time_jst') || $('settingsCatalogTab_posting');
    if(!anchor) return null;
    const div = document.createElement('div');
    div.className = 'e2-setting-row';
    div.id = 'settingsPostingStrategyPanel';
    div.dataset.search = 'posting strategy publish_time_jst weekly_publish_count 投稿戦略 投稿頻度 時刻';
    div.innerHTML = `<div>
      <div class="e2-setting-label">投稿頻度・時刻の推奨</div>
      <div class="e2-setting-key">posting-strategy</div>
      <div class="e2-setting-desc" id="settingsPostingStrategyReason">蓄積データから読み込み中...</div>
    </div>
    <div class="e2-setting-control" id="settingsPostingStrategyValue">読み込み中...</div>
    <div class="e2-setting-actions" id="settingsPostingStrategyActions"></div>`;
    anchor.insertAdjacentElement('afterend', div);
    return div;
  }

  async function loadPostingStrategyRecommendation(){
    if(!state.activeChannelId) return;
    const panel = ensurePostingStrategyPanel();
    if(!panel) return;
    const val = $('settingsPostingStrategyValue'), reason = $('settingsPostingStrategyReason'), actions = $('settingsPostingStrategyActions');
    try{
      const d = await fetchJson(`/api/posting-strategy/${encodeURIComponent(state.activeChannelId)}`);
      if(d.status !== 'ok'){
        if(val) val.innerHTML = '<div class="fh">蓄積中</div>';
        if(reason) reason.textContent = `サンプル ${d.sample_count || 0}/${d.minimum_samples || 3}。エラーではありません。`;
        if(actions) actions.innerHTML = '';
        return;
      }
      const rec = d.recommendation || {};
      const src = d.sources || {};
      const reasons = (d.reasons || []).join(' / ');
      if(val) val.innerHTML = `<div><strong>推奨: ${esc(rec.publish_time_jst || '-')} JST</strong></div><div class="e2-card-sub">週 ${esc(String(rec.weekly_publish_count || '-'))} 本 / ベンチ ${esc(String(src.benchmark_cache || 0))}件・自ch ${esc(String(src.own_48h_review || 0))}件</div>`;
      if(reason) reason.textContent = reasons || '蓄積済みデータから算出しています';
      if(actions) actions.innerHTML = `<button class="btn btn-primary btn-sm" onclick="E2UI.applyPostingStrategy('time','${esc(rec.publish_time_jst || '')}')">時刻を適用</button><button class="btn btn-secondary btn-sm" onclick="E2UI.applyPostingStrategy('weekly','${esc(String(rec.weekly_publish_count || ''))}')">週次本数を適用</button><div class="e2-setting-status" id="settingsPostingStrategyStatus"></div>`;
    }catch(e){
      if(val) val.innerHTML = '<div class="fh">蓄積中</div>';
      if(reason) reason.textContent = `投稿戦略を取得できません: ${e.message}`;
      if(actions) actions.innerHTML = '';
    }
  }

  async function applyPostingStrategy(kind, value){
    const st = $('settingsPostingStrategyStatus');
    if(!state.activeChannelId || !value) return;
    const key = kind === 'weekly' ? 'channel.weekly_publish_count' : 'channel.publish_time_jst';
    if(st) st.textContent = '保存中...';
    try{
      await fetchJson('/api/settings-catalog/value', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key, value, channel_id: state.activeChannelId})});
      const input = $(settingInputId(key));
      if(input) input.value = value;
      if(st) st.textContent = '保存済み';
      if(typeof toast === 'function') toast('投稿戦略の推奨を保存しました: '+key, 's');
    }catch(e){
      if(st) st.textContent = '保存失敗';
      if(typeof toast === 'function') toast('保存失敗: '+e.message, 'e');
    }
  }

  function ensureImageModulesPanel(){
    let panel = $('settingsImageModulesPanel');
    if(panel) return panel;
    const anchor = $('settingsCatalogTab_production');
    if(!anchor) return null;
    panel = document.createElement('div');
    panel.className = 'e2-setting-row';
    panel.id = 'settingsImageModulesPanel';
    panel.dataset.category = 'production';
    panel.dataset.search = 'image modules prompt composition subject background color tone mood text overlay style 画像 プロンプト モジュール';
    panel.innerHTML = `<div>
      <div class="e2-setting-label">画像プロンプトモジュール</div>
      <div class="e2-setting-key">image-modules</div>
      <div class="e2-setting-desc">構図/主題/背景/色調/雰囲気/文字/画風を分けて保存します。</div>
    </div>
    <div class="e2-setting-control" id="settingsImageModulesBody">読み込み中...</div>
    <div class="e2-setting-actions">
      <button class="btn btn-primary btn-sm" onclick="E2UI.saveImageModules()">保存</button>
      <button class="btn btn-secondary btn-sm" onclick="E2UI.loadImageModulesPanel()">再読込</button>
      <div class="e2-setting-status" id="settingsImageModulesStatus"></div>
    </div>`;
    anchor.insertAdjacentElement('afterbegin', panel);
    return panel;
  }

  async function loadImageModulesPanel(){
    if(!state.activeChannelId) return;
    const panel = ensureImageModulesPanel();
    if(!panel) return;
    const body = $('settingsImageModulesBody');
    if(body) body.innerHTML = '読み込み中...';
    try{
      const d = await fetchJson(`/api/image-modules/${encodeURIComponent(state.activeChannelId)}`);
      state.imageModules = d;
      renderImageModules(d);
    }catch(e){
      if(body) body.innerHTML = `<div class="e2-health-warn">取得失敗: ${esc(e.message || e)}</div>`;
    }
  }

  function renderImageModules(d){
    const body = $('settingsImageModulesBody');
    if(!body) return;
    const sections = d.schema || ['composition','subject','background','color_tone','mood','text_overlay','style'];
    const labels = {composition:'構図', subject:'主題・被写体', background:'背景・場所', color_tone:'色調・光', mood:'雰囲気', text_overlay:'文字入れ指示', style:'画風・品質'};
    body.innerHTML = `<div style="display:flex;flex-direction:column;gap:8px">
      ${sections.map(sec=>{
        const mods = (d.modules?.[sec] || []);
        const sel = d.selection?.[sec] || '';
        const ov = d.overrides?.[sec] || '';
        return `<details class="settings-group" style="margin:0" ${sec==='composition'?'open':''}>
          <summary class="settings-group-summary" style="padding:8px 10px;min-height:34px"><span class="settings-group-caret">▶</span><span class="settings-group-title" style="font-size:var(--text-sm)">${esc(labels[sec] || sec)}</span><span class="settings-group-desc">${esc(sel || '未選択')}</span></summary>
          <div class="settings-group-body" style="padding:10px">
            <select class="fs" id="imageModuleSelect_${esc(sec)}">${mods.map(m=>`<option value="${esc(m.id || '')}" ${String(m.id||'')===String(sel)?'selected':''}>${esc(m.name || m.id || '')}</option>`).join('')}</select>
            <textarea class="ft" rows="2" id="imageModuleOverride_${esc(sec)}" placeholder="自由上書き（空なら選択モジュールを使用）">${esc(ov)}</textarea>
            <div class="fh" style="white-space:pre-wrap">${esc((mods.find(m=>String(m.id||'')===String(sel)) || mods[0] || {}).text || '')}</div>
          </div>
        </details>`;
      }).join('')}
      <details><summary style="cursor:pointer;font-size:var(--text-xs);color:var(--text-tertiary)">結合プレビュー</summary><pre style="white-space:pre-wrap;font-size:var(--text-xs);max-height:220px;overflow:auto">${esc(d.composed_prompt || '')}</pre></details>
    </div>`;
  }

  async function saveImageModules(){
    const st = $('settingsImageModulesStatus');
    const d = state.imageModules || {};
    const sections = d.schema || [];
    const selection = {}, overrides = {};
    sections.forEach(sec=>{
      const sel = $(`imageModuleSelect_${sec}`)?.value || '';
      const ov = $(`imageModuleOverride_${sec}`)?.value || '';
      if(sel) selection[sec] = sel;
      if(ov.trim()) overrides[sec] = ov.trim();
    });
    if(st) st.textContent = '保存中...';
    try{
      const saved = await fetchJson(`/api/image-modules/${encodeURIComponent(state.activeChannelId)}`, {
        method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({selection, overrides})
      });
      state.imageModules = saved;
      renderImageModules(saved);
      if(st) st.textContent = '保存済み';
      if(typeof toast === 'function') toast('画像プロンプトモジュールを保存しました', 's');
    }catch(e){
      if(st) st.textContent = '保存失敗';
      if(typeof toast === 'function') toast('保存失敗: '+(e.message || e), 'e');
    }
  }

  function ensureConfigBackupPanel(){
    if($('settingsConfigBackupPanel')) return;
    const anchor = $('settingsCatalogAdvanced');
    if(!anchor) return;
    const div = document.createElement('div');
    div.className = 'card';
    div.id = 'settingsConfigBackupPanel';
    div.style.marginTop = 'var(--space-3)';
    div.innerHTML = `<div class="card-header" style="margin-bottom:var(--space-3)">
      <div><div class="section-heading"><div class="sec-title">設定バックアップ / 復元</div></div>
      <div class="fh">config/ と全チャンネルの .app_channel_config.json を Drive 上に保存します。</div></div>
      <div class="btns" style="margin:0"><button class="btn btn-secondary btn-sm" onclick="E2UI.runConfigBackup()">今すぐ作成</button><button class="btn btn-secondary btn-sm" onclick="E2UI.loadConfigBackups()">再読み込み</button></div>
    </div><div id="settingsConfigBackupList" class="fh">読み込み中...</div>`;
    anchor.insertAdjacentElement('afterend', div);
  }

  function ensureSettingsTools(){
    const grid = $('settingsToolsGrid');
    if(!grid || grid.dataset.rendered) return;
    grid.dataset.rendered = '1';
    const toolCard = (id, title, body, actions) => `<article class="e2-tool-card" id="${id}" data-tool-card="${id}">
      <div class="e2-tool-title">${title}</div>
      <div class="e2-tool-body">${body}</div>
      <div class="e2-tool-actions">${actions}</div>
    </article>`;
    grid.innerHTML = [
      toolCard('settingsToolChannels','チャンネル管理','チャンネル一覧と新規作成を開きます。','<button class="btn btn-secondary btn-sm" onclick="openChannelMgmt()">管理</button><button class="btn btn-primary btn-sm" onclick="openAddChannelFlow()">新規作成</button>'),
      toolCard('settingsToolTemplates','テンプレ選択','prproj / psd 候補をスキャンしてカタログ値へ保存します。','<div class="e2-template-tool"><select class="fs" id="toolTemplatePrproj"></select><select class="fs" id="toolTemplatePsd"></select><button class="btn btn-primary btn-sm" onclick="E2UI.saveTemplateTool()">保存</button><button class="btn btn-secondary btn-sm" onclick="E2UI.loadToolTemplateOptions()">再スキャン</button></div><div class="e2-setting-status" id="toolTemplateStatus"></div>'),
      toolCard('settingsToolReferencePreview','参照画像プレビュー','背景画像生成で使う参照画像フォルダを確認します。','<button class="btn btn-secondary btn-sm" onclick="E2UI.renderReferencePreviewTool()">更新</button><div id="settingsToolReferencePreviewBody" class="e2-ref-preview"></div>'),
      toolCard('settingsToolDiscord','Discordテスト','保存済みWebhookへテスト通知を送ります。','<button class="btn btn-secondary btn-sm" onclick="E2UI.sendDiscordTest()">テスト送信</button><div class="e2-setting-status" id="settingsToolDiscordStatus"></div>'),
      toolCard('settingsToolDiagnostics','接続診断','認証・依存関係・サーバー状態を確認します。','<button class="btn btn-secondary btn-sm" onclick="checkCredentials()">認証確認</button><button class="btn btn-secondary btn-sm" onclick="checkSetup()">セットアップ確認</button><button class="btn btn-secondary btn-sm" onclick="E2UI.runConnectionDiagnostics()">API診断</button><div class="e2-setting-status" id="settingsToolDiagnosticsStatus"></div>'),
      toolCard('settingsToolSchedule','自動実行スケジュール','定期実行ジョブを追加・実行・削除します。',scheduleToolHtml()),
      toolCard('settingsToolSetup','環境セットアップ','不足依存の確認とセットアップ実行へ進みます。','<button class="btn btn-secondary btn-sm" onclick="checkSetup()">状態確認</button><button class="btn btn-primary btn-sm" onclick="runSetup()">実行</button>'),
      toolCard('settingsToolBackup','バックアップ復元','config とチャンネル設定をバックアップ・復元します。','<button class="btn btn-secondary btn-sm" onclick="E2UI.runConfigBackup()">作成</button><button class="btn btn-secondary btn-sm" onclick="E2UI.loadConfigBackups()">一覧更新</button><div id="settingsConfigBackupList" class="e2-tool-list">読み込み中...</div>'),
      toolCard('settingsToolPrompts','プロンプトライブラリ','共有プロンプト管理を設定内で絞り込みます。','<button class="btn btn-secondary btn-sm" onclick="E2UI.showPromptSettings()">開く</button>'),
      toolCard('settingsToolAnalysisCache','競合分析キャッシュ操作','競合分析キャッシュの状態確認と削除を行います。','<button class="btn btn-secondary btn-sm" onclick="loadCacheInfo()">状態更新</button><button class="btn btn-secondary btn-sm" onclick="clearAnalysisCache()">削除</button><div id="analysisCacheInfo" class="e2-tool-list">読み込み中...</div>')
    ].join('');
    setTimeout(()=>{
      if(typeof onSchTypeChange === 'function') onSchTypeChange();
      if(typeof loadMasterSchedule === 'function') loadMasterSchedule();
    }, 0);
  }

  function scheduleToolHtml(){
    return `<div class="e2-schedule-tool">
      <div style="display:flex;gap:var(--space-2);align-items:center;flex-wrap:wrap;margin-bottom:var(--space-2)">
        <select class="fs" id="schFilterChannel" onchange="loadMasterSchedule()" style="min-width:180px">
          <option value="">すべて</option>
          <option value="__none__">未指定（全チャンネル共通）</option>
        </select>
        <button class="btn btn-secondary btn-sm" onclick="loadMasterSchedule()">再読込</button>
        <span id="schFilterCount" class="fh" style="font-size:var(--text-xs)"></span>
      </div>
      <div id="masterScheduleList" class="e2-tool-list" style="margin-bottom:var(--space-3)">読み込み中...</div>
      <details>
        <summary style="cursor:pointer;font-weight:var(--font-semibold)">ジョブを追加</summary>
        <div style="margin-top:var(--space-3)">
          <div class="fr">
            <div class="ff"><label class="fl">ジョブ名</label><input type="text" class="fi" id="schNewName" placeholder="例: 朝7時の自動生成"></div>
            <div class="ff"><label class="fl">実行する内容</label><select class="fs" id="schNewType" onchange="onSchTypeChange()"><option value="vol_create">vol を定期生成</option><option value="benchmark_refresh">ベンチマーク分析を再取得</option><option value="export_window">書き出し ON / OFF</option><option value="spot_create">単発動画を作成</option></select></div>
          </div>
          <div class="ff" id="schChannelRow"><label class="fl">対象チャンネル</label><select class="fs" id="schNewChannel"><option value="">いま選んでいるチャンネル</option></select></div>
          <div class="ff" id="schExportActionRow" style="display:none"><label class="fl">動作</label><select class="fs" id="schNewAction"><option value="on">書き出しを ON</option><option value="off">書き出しを OFF</option></select></div>
          <div class="ff" id="schVolRow" style="display:none"><label class="fl">vol 番号</label><input type="number" class="fi" id="schNewVol" min="1" placeholder="例: 78"></div>
          <div class="fr" id="schCronRow">
            <div class="ff"><label class="fl">曜日</label><input type="text" class="fi" id="schNewDow" placeholder="mon,fri / *" value="*"></div>
            <div class="ff"><label class="fl">時</label><input type="number" class="fi" id="schNewHour" min="0" max="23" value="9"></div>
            <div class="ff"><label class="fl">分</label><input type="number" class="fi" id="schNewMin" min="0" max="59" value="0"></div>
          </div>
          <div class="ff" id="schDateRow" style="display:none"><label class="fl">実行日時</label><input type="datetime-local" class="fi" id="schNewDate"></div>
          <div class="fr" id="schAutoResumeRow">
            <label class="ff" style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="schNewAutoResume"> 失敗時に自動再投入</label>
            <div class="ff"><label class="fl">遅延（分）</label><input type="number" class="fi" id="schNewResumeDelay" min="1" max="1440" value="30"></div>
            <div class="ff"><label class="fl">最大試行回数</label><input type="number" class="fi" id="schNewResumeMax" min="1" max="10" value="3"></div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="saveScheduleJob()">追加</button>
        </div>
      </details>
      <details style="margin-top:var(--space-3)">
        <summary style="cursor:pointer;font-size:var(--text-sm);color:var(--text-tertiary)">実行履歴</summary>
        <div id="masterScheduleHistory" class="e2-tool-list" style="margin-top:var(--space-2)"></div>
      </details>
    </div>`;
  }

  async function loadConfigBackups(){
    const el = $('settingsConfigBackupList');
    if(!el) return;
    const d = await fetchJson('/api/config-backup');
    const rows = (d.backups || []).map(b=>`<tr><td>${esc(b.date)}</td><td>${esc(String(b.channel_config_count || 0))}</td><td>${esc(b.created_at || '')}</td><td><button class="btn btn-secondary btn-sm" onclick="E2UI.restoreConfigBackup('${esc(b.date)}')">復元</button></td></tr>`).join('');
    el.innerHTML = `<div class="ledger-table-wrap"><table class="ledger-table"><thead><tr><th>日付</th><th>ch設定</th><th>作成</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="fh">バックアップなし</td></tr>'}</tbody></table></div><div class="e2-card-sub">${esc(d.backup_root || '')}</div>`;
  }

  async function runConfigBackup(){
    try{
      const d = await fetchJson('/api/config-backup/run', {method:'POST'});
      if(typeof toast === 'function') toast('設定バックアップを作成しました: '+(d.date || ''), 's');
      await loadConfigBackups();
    }catch(e){
      const h = humanizeLocalError(null, e.message || e, '');
      if(typeof toast === 'function') toast('バックアップ失敗: '+h.next_action, 'e');
    }
  }

  async function restoreConfigBackup(date){
    if(!confirm(`${date} の設定へ復元します。現状は復元前に自動退避されます。続行しますか？`)) return;
    try{
      await fetchJson('/api/config-backup/restore', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({date})});
      if(typeof toast === 'function') toast('設定を復元しました: '+date, 's');
      await loadSettingsCatalog();
    }catch(e){
      const h = humanizeLocalError(null, e.message || e, '');
      if(typeof toast === 'function') toast('復元失敗: '+h.next_action, 'e');
    }
  }

  async function renderSettingRow(item){
    let value = '';
    let status = '';
    const needsChannel = item.scope === 'channel';
    if(needsChannel && !state.activeChannelId){
      status = 'チャンネル未選択';
    }else{
      try{
        const qs = new URLSearchParams({key:item.key});
        if(needsChannel) qs.set('channel_id', state.activeChannelId);
        const d = await fetchJson('/api/settings-catalog/value?' + qs.toString());
        value = d.current?.value;
        item._configured = !!d.current?.configured;
        item._masked = d.current?.masked_value || '';
      }catch(e){ status = '読込失敗'; }
    }
    const id = settingInputId(item.key);
    const control = renderControl(id, item, value);
    const extraAction = item.key === 'discord.webhook_url' ? `<button class="btn btn-secondary btn-sm" onclick="E2UI.sendDiscordTest()">テスト送信</button>` : '';
    return {tier:item.tier || 'advanced', category:settingCategory(item), html:`<div class="e2-setting-row" id="settingsCatalogField_${esc(slug(item.key))}" data-key="${esc(item.key)}" data-category="${esc(settingCategory(item))}" data-search="${esc([item.key,item.label_ja,item.description_ja].join(' ').toLowerCase())}">
      <div>
        <div class="e2-setting-label">${esc(item.label_ja || item.key)}</div>
        <div class="e2-setting-key">${esc(item.key)}</div>
        <div class="e2-setting-desc">${esc(item.description_ja || '')}</div>
      </div>
      <div class="e2-setting-control">${control}</div>
      <div class="e2-setting-actions">
        <button class="btn btn-primary btn-sm" onclick="E2UI.saveCatalogSetting('${esc(item.key)}')">保存</button>
        ${extraAction}
        <div class="e2-setting-status" id="${esc(id)}_status">${esc(status)}</div>
      </div>
    </div>`};
  }

  function renderControl(id, item, value){
    const type = item.type || 'string';
    const choices = item.choices || [];
    if(SECRET_RE.test(item.key) || item.secret){
      const configured = !!item._configured;
      const masked = item._masked || (configured ? '●●●●●●●●' : '');
      return `<div class="e2-secret-control" data-configured="${configured?'1':'0'}">
        <div class="e2-secret-mask" id="${esc(id)}_mask">${configured ? esc(masked || '●●●●●●●●') : '未設定'}</div>
        <input class="fi" type="password" id="${esc(id)}" value="" autocomplete="off" placeholder="${configured ? '変更する場合だけ入力' : '新しい値を入力'}">
        <button type="button" class="btn btn-secondary btn-sm" onclick="E2UI.toggleSecretInput('${esc(id)}')">表示切替</button>
      </div>`;
    }
    if(type === 'boolean'){
      return `<label style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="${esc(id)}" ${value ? 'checked' : ''}> 有効</label>`;
    }
    if(type === 'multiselect'){
      const selected = new Set(Array.isArray(value) ? value.map(String) : String(value || '').split(/\s*,\s*|\n/).filter(Boolean));
      return `<div class="e2-multiselect" id="${esc(id)}">${choices.map(c=>`<label><input type="checkbox" value="${esc(c)}" ${selected.has(String(c))?'checked':''}> ${esc(choiceLabel(c))}</label>`).join('')}</div>`;
    }
    if(choices.length){
      return `<select class="fs" id="${esc(id)}">${choices.map(c=>`<option value="${esc(c)}"${String(value ?? '')===String(c)?' selected':''}>${esc(choiceLabel(c))}</option>`).join('')}</select>`;
    }
    if(type === 'array'){
      const text = Array.isArray(value) ? value.join('\n') : (value || '');
      return `<textarea class="ft" id="${esc(id)}" rows="4">${esc(text)}</textarea>`;
    }
    if(type === 'integer' || type === 'number'){
      const step = type === 'number' ? 'any' : '1';
      return `<input class="fi" type="number" step="${step}" id="${esc(id)}" value="${esc(value ?? '')}">`;
    }
    const long = String(value ?? '').length > 60 || /prompt|persona|description|rival/.test(item.key);
    return long ? `<textarea class="ft" id="${esc(id)}" rows="3">${esc(value ?? '')}</textarea>` : `<input class="fi" type="text" id="${esc(id)}" value="${esc(value ?? '')}">`;
  }

  function choiceLabel(v){
    return ({unlisted:'限定公開', public:'公開', delayed:'予約公開', ame:'Premiere/AME', ffmpeg:'ffmpeg', claude:'Claude', codex:'Codex', gemini:'Gemini'})[v] || v;
  }
  function slug(key){ return String(key).replace(/[^A-Za-z0-9_-]+/g,'_'); }
  function settingInputId(key){ return 'settingsCatalogInput_' + slug(key); }
  function settingCategory(item){
    const explicit = String(item.category || '').trim();
    if(SETTINGS_TABS.includes(explicit)) return explicit;
    const key = String(item.key || '');
    if(/^(dashboard\.(channel_name|channel_folder|file_prefix)|channel\.(persona|reference_image|reference_image_dir))$/.test(key)) return 'channel';
    if(/^channel\.(publish_time_jst|weekly_publish_count|publish_mode|publish_delay_hours|youtube\.)/.test(key)) return 'posting';
    if(/^(channel\.(suno\.|rival_channels|scene_text_|export_|default_duration_sec|template_|psd_)|benchmark\.)/.test(key)) return 'production';
    if(/^(youtube\.|digest\.|review\.|mining\.|research\.|alerts\.|update\.|suno\.(api_key|claude_cli|codex_cli))/.test(key)) return 'integration';
    return 'advanced';
  }

  async function saveCatalogSetting(key){
    const item = (state.catalog?.settings || []).find(x=>x.key===key);
    if(!item) return;
    const id = settingInputId(key), el=$(id), st=$(id+'_status');
    if(!el) return;
    if(item.scope === 'channel' && !state.activeChannelId){
      if(st) st.textContent = 'チャンネル未選択';
      return;
    }
    let value;
    if(item.type === 'boolean') value = !!el.checked;
    else if(item.type === 'array') value = String(el.value || '').split('\n').map(x=>x.trim()).filter(Boolean);
    else if(item.type === 'multiselect') value = Array.from(document.querySelectorAll(`#${CSS.escape(id)} input:checked`)).map(x=>x.value);
    else value = el.value;
    if((SECRET_RE.test(item.key) || item.secret) && !String(value || '').trim()){
      if(st) st.textContent = '未入力のため維持';
      if(typeof toast === 'function') toast('機密値は未入力のため変更していません: '+key, 'i');
      return;
    }
    if(st) st.textContent = '保存中...';
    try{
      const body = {key, value};
      if(item.scope === 'channel'){
        body.channel_id = $('settingsCatalogApplyAll')?.checked ? 'all' : state.activeChannelId;
      }
      await fetchJson('/api/settings-catalog/value', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      if(st) st.textContent = '保存済み';
      if((SECRET_RE.test(item.key) || item.secret) && el) el.value = '';
      if(typeof toast === 'function') toast('設定を保存しました: '+key, 's');
      if(typeof loadConfig === 'function') loadConfig().catch(()=>{});
    }catch(e){
      if(st) st.textContent = '保存失敗';
      const h = humanizeLocalError(null, e.message || e, '');
      if(typeof toast === 'function') toast('保存失敗: '+h.next_action, 'e');
    }
  }

  function filterSettingsCatalog(){
    const q = ($('settingsCatalogSearch')?.value || '').trim().toLowerCase();
    document.querySelectorAll('.e2-setting-row[data-search]').forEach(row=>{
      const inActiveTab = !row.dataset.category || row.dataset.category === state.activeSettingsTab;
      row.hidden = !inActiveTab || (!!q && !row.dataset.search.includes(q));
    });
  }

  function showPromptSettings(){
    switchSettingsTab('production');
    const search = $('settingsCatalogSearch');
    if(search){
      search.value = 'master_prompts';
      filterSettingsCatalog();
      search.scrollIntoView({behavior:'smooth', block:'center'});
      search.focus();
    }
  }

  async function sendDiscordTest(){
    const st = $('settingsToolDiscordStatus') || $('settingsCatalogInput_discord_webhook_url_status');
    if(st) st.textContent = '送信中...';
    try{
      await fetchJson('/api/notify/discord', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:'Automation Studio: 通知テスト'})});
      if(st) st.textContent = '送信済み';
      if(typeof toast === 'function') toast('Discordテストを送信しました', 's');
    }catch(e){
      if(st) st.textContent = '送信失敗';
      if(typeof toast === 'function') toast('Discordテスト失敗: '+e.message, 'e');
    }
  }

  function toggleSecretInput(id){
    const el = $(id);
    if(!el) return;
    el.type = el.type === 'password' ? 'text' : 'password';
  }

  async function loadToolTemplateOptions(){
    const pr = $('toolTemplatePrproj'), ps = $('toolTemplatePsd'), st = $('toolTemplateStatus');
    if(!pr || !ps) return;
    if(st) st.textContent = 'スキャン中...';
    try{
      const d = await fetchJson('/api/templates/list');
      const prCurrent = await settingValue('channel.template_prproj');
      const psCurrent = await settingValue('channel.template_psd');
      pr.innerHTML = '<option value="">未選択</option>' + (d.prproj || []).map(x=>`<option value="${esc(x.filename)}">${esc(x.filename)}</option>`).join('');
      ps.innerHTML = '<option value="">未選択</option>' + (d.psd || []).map(x=>`<option value="${esc(x.filename)}">${esc(x.filename)}</option>`).join('');
      if(prCurrent && !Array.from(pr.options).some(o=>o.value===prCurrent)) pr.insertAdjacentHTML('beforeend', `<option value="${esc(prCurrent)}">${esc(prCurrent)}</option>`);
      if(psCurrent && !Array.from(ps.options).some(o=>o.value===psCurrent)) ps.insertAdjacentHTML('beforeend', `<option value="${esc(psCurrent)}">${esc(psCurrent)}</option>`);
      pr.value = prCurrent || '';
      ps.value = psCurrent || '';
      if(st) st.textContent = `候補 prproj ${(d.prproj||[]).length} / psd ${(d.psd||[]).length}`;
    }catch(e){
      if(st) st.textContent = 'スキャン失敗';
    }
  }

  function wireSettingsTemplateTool(){}

  async function settingValue(key){
    const item = (state.catalog?.settings || []).find(x=>x.key===key);
    const qs = new URLSearchParams({key});
    if(item?.scope === 'channel' && state.activeChannelId) qs.set('channel_id', state.activeChannelId);
    const d = await fetchJson('/api/settings-catalog/value?' + qs.toString());
    return d.current?.value || '';
  }

  async function saveTemplateTool(){
    const st = $('toolTemplateStatus');
    if(!state.activeChannelId){ if(st) st.textContent='チャンネル未選択'; return; }
    try{
      if(st) st.textContent='保存中...';
      await fetchJson('/api/settings-catalog/value', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:'channel.template_prproj', value:$('toolTemplatePrproj')?.value || '', channel_id:state.activeChannelId})});
      await fetchJson('/api/settings-catalog/value', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:'channel.template_psd', value:$('toolTemplatePsd')?.value || '', channel_id:state.activeChannelId})});
      if(st) st.textContent='保存済み';
      await loadSettingsCatalog();
    }catch(e){
      if(st) st.textContent='保存失敗';
    }
  }

  async function renderReferencePreviewTool(){
    const body = $('settingsToolReferencePreviewBody');
    if(!body) return;
    body.innerHTML = '読み込み中...';
    try{
      const d = await fetchJson('/api/bgimage/reference-dir/list?limit=6');
      if(!d.configured){ body.innerHTML = '<div class="fh">参照画像フォルダ未設定</div>'; return; }
      if(!d.exists){ body.innerHTML = `<div class="e2-health-warn">フォルダ不在: ${esc(d.path || '')}</div>`; return; }
      body.innerHTML = `<div class="fh">画像 ${esc(String(d.count || 0))} 枚</div><div class="e2-ref-thumbs">${(d.files || []).map(name=>`<img src="/api/bgimage/reference-dir/thumb/${encodeURIComponent(name)}" alt="${esc(name)}" title="${esc(name)}">`).join('')}</div>`;
    }catch(e){
      body.innerHTML = '<div class="e2-health-warn">取得失敗</div>';
    }
  }

  async function runConnectionDiagnostics(){
    const st = $('settingsToolDiagnosticsStatus');
    if(st) st.textContent = '確認中...';
    try{
      const [h, v, w] = await Promise.all([fetchJson('/api/health'), fetchJson('/api/version'), fetchJson('/api/workers/status')]);
      if(st) st.textContent = `health ${h.status || '-'} / v${v.version || '-'} / workers ${(w.channels || []).length}`;
    }catch(e){
      if(st) st.textContent = '診断失敗';
    }
  }

  function wrapExisting(){
    const oldGo = window.go;
    if(typeof oldGo === 'function' && !oldGo._e2Wrapped){
      window.go = function(page, pushHistory){
        oldGo(page, pushHistory);
        if(page === 'dashboard') loadDashboardCards().catch(()=>{});
        if(page === 'settings' && window.AutomationStudioContext?.channelId !== 'all') loadSettingsCatalog().catch(()=>{});
        if(page === 'updates'){
          checkAppVersion?.();
          checkUpdateGuard?.();
          setTimeout(mirrorUpdatePanel, 300);
        }
      };
      window.go._e2Wrapped = true;
    }
    const oldLoadDashboard = window.loadDashboard;
    if(typeof oldLoadDashboard === 'function' && !oldLoadDashboard._e2Wrapped){
      window.loadDashboard = async function(){
        const out = await oldLoadDashboard.apply(this, arguments);
        loadDashboardCards().catch(()=>{});
        return out;
      };
      window.loadDashboard._e2Wrapped = true;
    }
    const oldCheckAppVersion = window.checkAppVersion;
    if(typeof oldCheckAppVersion === 'function' && !oldCheckAppVersion._e2Wrapped){
      window.checkAppVersion = async function(){
        const out = await oldCheckAppVersion.apply(this, arguments);
        mirrorUpdatePanel();
        return out;
      };
      window.checkAppVersion._e2Wrapped = true;
    }
  }

  window.E2UI = {loadDashboardCards, loadSettingsCatalog, saveCatalogSetting, mirrorUpdatePanel, loadConfigBackups, runConfigBackup, restoreConfigBackup, applyPostingStrategy, switchSettingsTab, showPromptSettings, sendDiscordTest, toggleSecretInput, loadToolTemplateOptions, saveTemplateTool, renderReferencePreviewTool, runConnectionDiagnostics, loadImageModulesPanel, saveImageModules};
  wrapExisting();
  document.addEventListener('DOMContentLoaded', ()=>{
    wrapExisting();
    if(location.hash.startsWith('#settings') && window.AutomationStudioContext?.channelId !== 'all') loadSettingsCatalog().catch(()=>{});
    loadDashboardCards().catch(()=>{});
  });
})();
