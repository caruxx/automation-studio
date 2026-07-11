(function(){
  const TTP_TABS = ["overview", "title", "thumbnail", "demand", "posting"];
  const TTP_TAB_LABELS = {overview:"概要", title:"タイトルの型", thumbnail:"サムネ要素", demand:"視聴者の需要", posting:"投稿戦略"};
  const state = {channels: [], profiles: [], demands: {}, genreRadar: null, selectedId: "", selectedProfile: null, poll: null, activeTab: "overview", productionPrompt: ""};
  const $ = (id) => document.getElementById(id);
  const htmlEsc = (v) => (typeof esc === "function" ? esc(v) : String(v ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
  const notify = (msg, type) => (typeof toast === "function" ? toast(msg, type) : console.log(msg));
  const fmtNum = (n) => {
    const v = Number(n || 0);
    if(!v) return "非公開";
    if(v >= 1000000) return `${(v / 1000000).toFixed(1)}M`;
    if(v >= 10000) return `${(v / 10000).toFixed(1)}万`;
    return v.toLocaleString();
  };

  async function jsonFetch(url, opts){
    const r = await fetch(url, opts);
    const d = await r.json().catch(() => ({}));
    if(!r.ok) throw new Error(d.detail || d.error || `${r.status}`);
    return d;
  }

  async function load(){
    await Promise.all([loadChannels(), loadProfiles(), loadGenreRadar()]);
    const preferred = state.selectedId || state.channels[0]?.channel_id || "";
    if(preferred) selectChannel(preferred);
  }

  async function loadChannels(){
    const strip = $("analysisBenchmarkChannelStrip");
    if(strip) strip.innerHTML = '<div class="analysis-loading">登録チャンネルを読み込み中...</div>';
    try{
      const d = await jsonFetch("/api/benchmark/channels");
      state.channels = d.channels || [];
      renderChannels();
    }catch(e){
      if(strip) strip.innerHTML = `<div class="analysis-empty">登録チャンネルを取得できません: ${htmlEsc(e.message)}</div>`;
    }
  }

  async function loadProfiles(){
    try{
      const d = await jsonFetch("/api/ttp/profiles");
      state.profiles = d.profiles || [];
    }catch(e){
      state.profiles = [];
    }
  }

  async function loadGenreRadar(){
    try{
      state.genreRadar = await jsonFetch("/api/genre-radar/top");
    }catch(e){
      state.genreRadar = null;
    }
    renderGenreRadar();
  }

  function renderGenreRadar(){
    const el = $("genreRadarTop5");
    if(!el) return;
    const rows = state.genreRadar?.items || [];
    if(!rows.length){
      el.innerHTML = `<div class="genre-placeholder-card" id="genreRadarPlaceholder">
        <div class="genre-placeholder-title">データ蓄積中</div>
        <div class="genre-placeholder-text">週次で候補が表示されます</div>
      </div>`;
      return;
    }
    el.innerHTML = `<div class="genre-radar-list">${rows.slice(0,5).map((r, i) => {
      const ch = r.representative_channel || {};
      const growth = (r.growth_rate_pct === null || r.growth_rate_pct === undefined) ? "初回計測" : `${Number(r.growth_rate_pct).toFixed(1)}%`;
      const spark = (r.sparkline || []).map(v => Number(v || 0)).filter(v => v > 0);
      return `<article class="genre-radar-card">
        <div class="genre-radar-rank">${i + 1}</div>
        <div class="genre-radar-main">
          <div class="genre-radar-name">${htmlEsc(r.genre || "BGM")}</div>
          <div class="genre-radar-meta">${htmlEsc(growth)} / ${htmlEsc(ch.name || "代表チャンネル未取得")}</div>
          ${spark.length ? `<div class="genre-sparkline" aria-label="登録者推移">${spark.map(v => `<span style="height:${Math.max(10, Math.min(100, v / Math.max(...spark) * 100))}%"></span>`).join("")}</div>` : `<div class="fh">推移は次回以降に表示</div>`}
        </div>
        <button class="btn btn-secondary btn-sm" onclick="AnalysisJ2.generateTtpFromRadar('${htmlEsc(ch.url || "")}', '${htmlEsc(ch.channel_id || "")}', '${htmlEsc(ch.name || "")}')">TTPプロファイル生成</button>
      </article>`;
    }).join("")}</div>`;
  }

  function renderChannels(){
    const strip = $("analysisBenchmarkChannelStrip");
    if(!strip) return;
    if(!state.channels.length){
      strip.innerHTML = '<div class="analysis-empty">ベンチ先が未登録です。上のURL欄から追加してください。</div>';
      return;
    }
    strip.innerHTML = state.channels.map(ch => {
      const id = ch.channel_id || "";
      const name = ch.name || "名称未取得";
      const icon = ch.icon_url ? `<img src="${htmlEsc(ch.icon_url)}" alt="" referrerpolicy="no-referrer">` : htmlEsc(name.slice(0,1) || "?");
      return `<button class="analysis-channel-card ${id === state.selectedId ? "is-selected" : ""}" id="analysisChannelCard_${htmlEsc(id)}" data-channel-id="${htmlEsc(id)}" onclick="AnalysisJ2.selectChannel('${htmlEsc(id)}')">
        <span class="analysis-channel-avatar">${icon}</span>
        <span class="analysis-channel-name" title="${htmlEsc(name)}">${htmlEsc(name)}</span>
        <span class="analysis-channel-badges">
          <span class="badge badge-info">${fmtNum(ch.subscribers)} 登録</span>
        </span>
        <span class="analysis-card-menu" title="登録解除" onclick="event.stopPropagation();AnalysisJ2.deleteChannel('${htmlEsc(id)}')">⋮</span>
      </button>`;
    }).join("");
  }

  function selectChannel(id){
    state.selectedId = id;
    state.selectedProfile = findProfileForChannel(id);
    renderChannels();
    renderAttribution();
    renderTtp();
    loadDemand(id);
  }

  async function loadDemand(id){
    if(!id) return;
    try{
      const d = await jsonFetch(`/api/comment-mining/${encodeURIComponent(id)}`);
      state.demands[id] = d.status === "ok" ? (d.memo || null) : null;
    }catch(e){
      state.demands[id] = null;
    }
    if(state.selectedId === id) renderTtp();
  }

  function findChannel(id){
    return state.channels.find(c => c.channel_id === id) || null;
  }

  function findProfileForChannel(id){
    return state.profiles.find(p => {
      const ids = p.input_channel_ids || [];
      const aggCh = ((p.aggregate || {}).channels || []).map(c => c.channel_id || c.channelId || c.name || "");
      return ids.includes(id) || aggCh.includes(id);
    }) || null;
  }

  function renderAttribution(){
    const span = $("analysisSelectedChannelLink");
    const ch = findChannel(state.selectedId);
    if(!span) return;
    if(!ch){ span.innerHTML = ""; return; }
    const url = ch.url || ch.source_url || (ch.channel_id ? `https://www.youtube.com/channel/${ch.channel_id}` : "");
    span.innerHTML = url ? ` / 選択中: <a href="${htmlEsc(url)}" target="_blank" rel="noopener">${htmlEsc(ch.name || "YouTubeチャンネル")}</a>` : "";
  }

  function renderTtp(){
    const ch = findChannel(state.selectedId);
    const meta = $("analysisTtpChannelMeta");
    const spec = $("analysisTtpSpec");
    const createBtn = $("analysisCreateChannelFromTtpBtn");
    if(meta) meta.textContent = ch ? `${ch.name || ""} / ${fmtNum(ch.subscribers)} 登録 / ${ch.video_count || 0}本` : "チャンネルを選択してください";
    if(!spec) return;
    if(!ch){
      spec.innerHTML = '<div class="analysis-empty">カードを選択すると仕様書が表示されます。</div>';
      if(createBtn) createBtn.disabled = true;
      return;
    }
    const profile = state.selectedProfile;
    if(!profile){
      spec.innerHTML = `<div class="analysis-create-ttp-note" id="analysisTtpEmptyState">
        <div>
          <div style="font-weight:var(--font-semibold);color:var(--text-primary);margin-bottom:4px">このチャンネルのTTP仕様書は未生成です</div>
          <div class="fh">生成はバックグラウンドで走ります。完了後に自動で表示します。</div>
        </div>
        <div style="display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap;justify-content:flex-end">
          <label class="fh" style="display:inline-flex;align-items:center;gap:6px;margin:0"><input type="checkbox" id="analysisRunCommentMining" checked>コメント需要も分析</label>
          <button class="btn btn-primary btn-sm" id="analysisRunTtpBtn" onclick="AnalysisJ2.generateTtp()">分析を実行</button>
        </div>
      </div>`;
      if(createBtn) createBtn.disabled = true;
      return;
    }
    if(createBtn) createBtn.disabled = false;
    spec.innerHTML = renderProfile(profile);
    activateTtpTab(state.activeTab || "overview");
    updatePostingStrategySection(ch.channel_id || "").catch(()=>{});
  }

  function renderProfile(p){
    const wf = p.winning_format_spec || {};
    const agg = p.aggregate || {};
    const im = p.imitate_evolve || {};
    const demand = state.demands[state.selectedId] || null;
    return `<div class="analysis-ttp-tabs" role="tablist" aria-label="TTP仕様書タブ">
      ${TTP_TABS.map(k => `<button type="button" class="analysis-ttp-tab" id="analysisTtpTab_${k}" role="tab" aria-controls="analysisTtpPane_${k}" onclick="AnalysisJ2.activateTtpTab('${k}')">${TTP_TAB_LABELS[k]}</button>`).join("")}
    </div>
    <div class="analysis-ttp-tabpanes">
      <section class="analysis-ttp-pane" id="analysisTtpPane_overview" role="tabpanel">
        ${renderOverviewTab(wf, agg, im)}
      </section>
      <section class="analysis-ttp-pane" id="analysisTtpPane_title" role="tabpanel" hidden>
        ${renderTitleTab(wf, agg)}
      </section>
      <section class="analysis-ttp-pane" id="analysisTtpPane_thumbnail" role="tabpanel" hidden>
        ${renderThumbnailTab(wf, agg)}
      </section>
      <section class="analysis-ttp-pane" id="analysisTtpPane_demand" role="tabpanel" hidden>
        ${section("視聴者の需要", renderDemand(demand))}
      </section>
      <section class="analysis-ttp-pane" id="analysisTtpPane_posting" role="tabpanel" hidden>
        ${renderPostingTab(wf, agg)}
      </section>
    </div>`;
  }

  function renderOverviewTab(wf, agg, im){
    const summary = [
      wf.summary || wf.one_line || "",
      wf.title_formula ? `タイトル: ${wf.title_formula}` : "",
      (wf.thumbnail_elements || wf.visual_elements) ? `サムネ: ${wf.thumbnail_elements || wf.visual_elements}` : "",
      wf.posting_schedule ? `投稿: ${wf.posting_schedule}` : "",
    ].filter(Boolean);
    return `<div class="analysis-overview-grid">
      <div class="analysis-overview-main">
        ${section("サマリ", textOrList(summary.length ? summary : ["勝ちフォーマットの要点を集約中です。"]))}
        ${section("コンセプト材料", `<div class="analysis-axis-actions"><button class="btn btn-secondary btn-sm" id="bcRunBtn" onclick="bcRun()">コンセプト分析</button><button class="btn btn-ghost btn-sm" onclick="bcLoad()">再読込</button><span id="bcStatus" class="fh"></span></div><div id="bcAggregate"></div><div id="bcChannels" class="analysis-axis-detail"></div>`)}
      </div>
      <aside class="analysis-overview-side">
        <div class="analysis-chip-group">${imitateChips(im)}</div>
        <div class="analysis-action-stack">
          <button class="btn btn-primary btn-sm" id="analysisCreateChannelFromTtpInlineBtn" onclick="AnalysisJ2.openCreateWizard()">この仕様書で新チャンネル作成</button>
          <button class="btn btn-secondary btn-sm" id="analysisUseForProductionBtn" onclick="AnalysisJ2.useForProduction()">制作に使う</button>
        </div>
        <div id="analysisProductionResult" class="analysis-production-result"></div>
      </aside>
    </div>`;
  }

  function renderTitleTab(wf, agg){
    return `${section("タイトルの型", textOrList(wf.title_formula || topTitlePatterns(agg)))}
      <div class="analysis-axis-actions">
        <button class="btn btn-primary btn-sm" id="btitleRunBtn" onclick="btitleRun()">タイトル分析</button>
        <button class="btn btn-ghost btn-sm" onclick="btitleLoad()">再読込</button>
        <span id="btitleStatus" class="fh"></span>
      </div>
      <div id="btitleAggregate"></div>
      <div id="btitleChannels" class="analysis-axis-detail"></div>`;
  }

  function renderThumbnailTab(wf, agg){
    return `${section("サムネ要素", textOrList((wf.thumbnail_elements || wf.visual_elements || "").toString() || thumbFallback(agg)))}
      <div class="analysis-axis-actions">
        <button class="btn btn-primary btn-sm" id="btRunBtn" onclick="btRun(false)">DL + Vision分析</button>
        <button class="btn btn-secondary btn-sm" onclick="btRun(true)">DLのみ</button>
        <button class="btn btn-ghost btn-sm" onclick="btLoad()">再読込</button>
        <button class="btn btn-secondary btn-sm" onclick="btSavePicked()">Picked保存</button>
        <span id="btStatus" class="fh"></span>
        <span id="btPickedCount" class="badge badge-neutral">Picked 0 件</span>
      </div>
      <div id="btAggregate"></div>
      <div id="btChannels" class="analysis-axis-detail"></div>`;
  }

  function renderPostingTab(wf, agg){
    return `${section("投稿戦略", renderHeatmap(agg.posting_cadence || {}, wf.posting_schedule))}
      ${section("投稿頻度・時刻分析", renderDurationBars(agg.duration_distribution || {}, wf.duration))}
      ${section("シリーズ構造", renderSeries(agg.series_structure || [], wf.series_structure))}`;
  }

  function section(label, body){
    return `<section class="ttp-section" id="analysisTtpSection_${slug(label)}">
      <div class="ttp-section-label">${htmlEsc(label)}</div>
      <div class="ttp-section-body">${body}</div>
    </section>`;
  }

  function slug(s){
    return ({ "タイトルの型":"title", "サムネ要素":"thumbnail", "動画の尺":"duration", "投稿頻度・時刻":"cadence", "シリーズ構造":"series", "視聴者の需要":"viewer-demand", "差別化ポイント":"differentiation" })[s] || "item";
  }

  function textOrList(value){
    if(Array.isArray(value)) return `<ul class="ttp-list">${value.map(v => `<li>${htmlEsc(v)}</li>`).join("")}</ul>`;
    return `<div style="font-size:var(--text-sm);color:var(--text-secondary);line-height:1.7">${htmlEsc(value || "データ蓄積中")}</div>`;
  }

  function topTitlePatterns(agg){
    const rows = agg.title_syntax_patterns || [];
    return rows.length ? rows.slice(0,4).map(r => `${r.pattern} (${r.count})`) : "データ蓄積中";
  }

  function thumbFallback(agg){
    const tags = (agg.frequent_tags || []).slice(0,8).map(t => t.tag);
    return tags.length ? `頻出タグ: ${tags.join(", ")}` : "サムネ要素は今後のVision分析と連動予定";
  }

  function renderDurationBars(dist, fallback){
    const buckets = dist.buckets || {};
    const entries = Object.entries(buckets);
    const max = Math.max(1, ...entries.map(([,v]) => Number(v || 0)));
    const bars = entries.length ? entries.map(([k,v]) => `<div class="ttp-bar-row">
      <span>${htmlEsc(k)}</span><div class="ttp-bar-track"><span class="ttp-bar-fill" style="width:${Math.max(4, Number(v || 0) / max * 100)}%"></span></div><strong>${Number(v || 0)}</strong>
    </div>`).join("") : `<div class="fh">${htmlEsc(fallback || "データ蓄積中")}</div>`;
    return `<div class="ttp-bars">${bars}</div><div class="fh" style="margin-top:var(--space-2)">平均 ${Math.round((dist.avg_sec || 0) / 60)} 分</div>`;
  }

  function renderHeatmap(cadence, fallback){
    const weekdays = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
    const hours = cadence.hour_counts_jst || {};
    const max = Math.max(1, ...Object.values(hours).map(Number));
    const rows = weekdays.map((w, wi) => {
      const cells = Array.from({length:24}, (_, h) => {
        const raw = Number(hours[String(h)] || 0);
        const level = raw ? Math.min(3, Math.ceil(raw / max * 3)) : 0;
        return `<span class="ttp-heat-cell" data-level="${level}" title="${w} ${h}:00 / ${raw}件"></span>`;
      }).join("");
      return `<span class="ttp-heat-label">${["月","火","水","木","金","土","日"][wi]}</span>${cells}`;
    }).join("");
    return `<div class="ttp-heatmap">${rows}</div><div class="fh" style="margin-top:var(--space-2)">${htmlEsc(fallback || `上位: ${cadence.top_weekday || "-"} ${cadence.top_hour_jst ?? "-"}時 JST`)}</div><div id="analysisPostingStrategyRecommendation" class="fh" style="margin-top:var(--space-2)">投稿戦略の推奨を読み込み中...</div>`;
  }

  async function updatePostingStrategySection(channelId){
    const el = $("analysisPostingStrategyRecommendation");
    if(!el || !channelId) return;
    try{
      const d = await jsonFetch(`/api/posting-strategy/${encodeURIComponent(channelId)}`);
      if(d.status !== "ok"){
        el.innerHTML = `蓄積中（${htmlEsc(String(d.sample_count || 0))}/${htmlEsc(String(d.minimum_samples || 3))}件）`;
        return;
      }
      const rec = d.recommendation || {};
      const reasons = (d.reasons || []).slice(0,2).join(" / ");
      el.innerHTML = `<div style="display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap">
        <strong>推奨: ${htmlEsc(rec.publish_time_jst || "-")} JST</strong>
        <span>週 ${htmlEsc(String(rec.weekly_publish_count || "-"))} 本</span>
        <button class="btn btn-primary btn-sm" onclick="AnalysisJ2.applyPostingStrategy('${htmlEsc(rec.publish_time_jst || "")}', '${htmlEsc(String(rec.weekly_publish_count || ""))}')">ワンクリック適用</button>
      </div><div class="fh">${htmlEsc(reasons)}</div>`;
    }catch(e){
      el.innerHTML = `蓄積中（投稿戦略を取得できません: ${htmlEsc(e.message)}）`;
    }
  }

  async function applyPostingStrategy(time, weekly){
    const channelId = await activeStudioChannelId();
    if(!channelId || !time) return;
    try{
      await jsonFetch("/api/settings-catalog/value", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({key:"channel.publish_time_jst", value:time, channel_id:channelId})});
      if(weekly) await jsonFetch("/api/settings-catalog/value", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({key:"channel.weekly_publish_count", value:weekly, channel_id:channelId})});
      notify("投稿戦略の推奨を保存しました", "s");
      updatePostingStrategySection(channelId).catch(()=>{});
    }catch(e){
      notify(`保存に失敗: ${e.message}`, "e");
    }
  }

  async function activeStudioChannelId(){
    try{
      const [chs, cfg] = await Promise.all([jsonFetch("/api/channels"), jsonFetch("/api/config")]);
      const folder = cfg.dashboard?.channel_folder || "";
      return ((chs.channels || []).find(c => c.folder === folder) || (chs.channels || [])[0] || {}).id || "";
    }catch(e){
      notify(`適用先チャンネルを取得できません: ${e.message}`, "e");
      return "";
    }
  }

  function renderSeries(rows, fallback){
    if(rows.length){
      return `<div class="ttp-flow">${rows.slice(0,5).map(r => `<span class="ttp-flow-node">${htmlEsc(r.pattern)}<br><small>${Number(r.count || 0)}件</small></span>`).join("")}</div>`;
    }
    return textOrList(fallback || "シリーズ構造は明確な連番が少ないため、独自の定番枠を設計してください");
  }

  function renderImitate(im){
    const groups = [["adopt","採用"],["avoid","避ける"],["evolve","進化"]];
    return `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:var(--space-3)">${groups.map(([key,label]) => `
      <div><div style="font-weight:var(--font-semibold);color:var(--text-primary);margin-bottom:6px">${label}</div>${textOrList(im[key] || [])}</div>
    `).join("")}</div>`;
  }

  function imitateChips(im){
    const groups = [["adopt","採用","success"],["avoid","避ける","danger"],["evolve","進化","warning"]];
    return groups.map(([key,label,tone]) => {
      const items = Array.isArray(im[key]) ? im[key] : (im[key] ? [im[key]] : []);
      return `<div class="analysis-chip-column analysis-chip-${tone}">
        <div class="analysis-chip-title">${label}</div>
        ${items.slice(0,4).map(x => `<span class="analysis-ttp-chip">${htmlEsc(x)}</span>`).join("") || `<span class="fh">蓄積中</span>`}
      </div>`;
    }).join("");
  }

  function activateTtpTab(key){
    if(!TTP_TABS.includes(key)) key = "overview";
    state.activeTab = key;
    TTP_TABS.forEach(k => {
      const tab = $(`analysisTtpTab_${k}`);
      const pane = $(`analysisTtpPane_${k}`);
      if(tab){
        tab.classList.toggle("is-active", k === key);
        tab.setAttribute("aria-selected", k === key ? "true" : "false");
      }
      if(pane) pane.hidden = k !== key;
    });
    if(key === "overview" && typeof bcLoad === "function") bcLoad();
    if(key === "title" && typeof btitleLoad === "function") btitleLoad();
    if(key === "thumbnail" && typeof btLoad === "function") btLoad();
  }

  async function useForProduction(){
    const ch = findChannel(state.selectedId);
    if(!ch){ notify("チャンネルを選択してください", "w"); return; }
    const btn = $("analysisUseForProductionBtn");
    const out = $("analysisProductionResult");
    if(btn) btn.disabled = true;
    if(out) out.innerHTML = '<div class="fh">SUNOプロンプトを生成中...</div>';
    try{
      const r = await jsonFetch("/api/benchmark/fuse", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({channel_names:[ch.name || ch.channel_id].filter(Boolean)})
      });
      const fusion = r.fusion || {};
      renderProductionPrompt(fusion.suno_prompt || "", "SUNOプロンプト生成完了");
      notify("SUNOプロンプトを生成しました", "s");
    }catch(e){
      const prompt = buildTtpSunoPrompt(state.selectedProfile, ch);
      renderProductionPrompt(prompt, "TTP仕様書からSUNOプロンプト生成完了");
      notify("TTP仕様書からSUNOプロンプトを生成しました", "s");
    }finally{
      if(btn) btn.disabled = false;
    }
  }

  function renderProductionPrompt(prompt, title){
    const out = $("analysisProductionResult");
    state.productionPrompt = prompt || "";
    if(!out) return;
    out.innerHTML = `<div class="analysis-production-card">
      <div class="analysis-production-title">${htmlEsc(title || "SUNOプロンプト生成完了")}</div>
      <pre>${htmlEsc(state.productionPrompt || "プロンプト候補は生成されましたが、SUNO用テキストが空です。")}</pre>
      <div class="analysis-axis-actions"><button class="btn btn-secondary btn-sm" onclick="AnalysisJ2.copyProductionPrompt()">コピー</button><button class="btn btn-primary btn-sm" onclick="AnalysisJ2.applyProductionPrompt()">SUNO設定へ反映</button></div>
    </div>`;
  }

  function buildTtpSunoPrompt(profile, ch){
    const wf = profile?.winning_format_spec || {};
    const agg = profile?.aggregate || {};
    const tags = (agg.frequent_tags || []).slice(0,8).map(t => t.tag).filter(Boolean);
    const titleHint = wf.title_formula || "";
    const durationHint = wf.duration || "";
    const scene = wf.series_structure || wf.posting_schedule || "";
    return [
      "Instrumental BGM for YouTube long-form listening.",
      tags.length ? `Style references: ${tags.join(", ")}.` : "",
      titleHint ? `Emotional direction from winning title format: ${titleHint}.` : "",
      wf.thumbnail_elements || wf.visual_elements ? `Visual mood to match: ${wf.thumbnail_elements || wf.visual_elements}.` : "",
      scene ? `Scene and listener context: ${scene}.` : "",
      durationHint ? `Arrangement note: ${durationHint}.` : "",
      `Create a polished, loop-friendly prompt for ${ch?.name || "this channel"}; avoid copying any existing melody, title, or exact brand phrase.`,
    ].filter(Boolean).join("\n");
  }

  function copyProductionPrompt(){
    const txt = state.productionPrompt || "";
    if(!txt){ notify("コピー対象がありません", "w"); return; }
    navigator.clipboard.writeText(txt).then(() => notify("コピーしました", "s"), () => notify("コピー失敗", "e"));
  }

  function applyProductionPrompt(){
    const txt = state.productionPrompt || "";
    if(!txt){ notify("反映するSUNOプロンプトがありません", "w"); return; }
    if(typeof go === "function") go("settings");
    setTimeout(() => {
      const ta = $("cfgSunoPrompt");
      if(ta){
        ta.value = txt;
        ta.scrollIntoView({behavior:"smooth", block:"center"});
        notify("SUNO設定へ反映しました。保存で確定します", "s");
      }else{
        notify("SUNO設定欄が見つかりません", "e");
      }
    }, 200);
  }

  function renderDemand(memo){
    if(!memo) return `<div class="fh">コメント需要メモは未生成です。次回の「分析を実行」で作成できます。</div>`;
    const chips = (memo.use_scenes || []).concat(memo.repeated_keywords || []).slice(0,12);
    const notes = memo.underserved_demands?.length ? memo.underserved_demands : (memo.demand_notes || []);
    return `<div style="display:grid;gap:var(--space-3)">
      <div style="font-size:var(--text-sm);color:var(--text-secondary);line-height:1.7">${htmlEsc(memo.japanese_summary || "需要メモを生成済みです。")}</div>
      ${chips.length ? `<div class="analysis-channel-badges">${chips.map(x => `<span class="badge badge-info">${htmlEsc(x)}</span>`).join("")}</div>` : ""}
      ${notes.length ? textOrList(notes.slice(0,5)) : ""}
      <div class="fh">コメント ${Number(memo.comment_count || 0).toLocaleString()} 件 / 動画 ${Number(memo.video_count || 0)} 本</div>
    </div>`;
  }

  async function addChannel(){
    const input = $("analysisChannelUrlInput");
    const btn = $("analysisAddChannelBtn");
    const url = (input?.value || "").trim();
    if(!url){ notify("URLを入力してください", "w"); return; }
    if(btn) btn.disabled = true;
    try{
      const d = await jsonFetch("/api/benchmark/channels", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({url, limit:30})
      });
      if(input) input.value = "";
      notify(`登録しました: ${d.channel?.name || ""}`, "s");
      await loadChannels();
      await loadProfiles();
      selectChannel(d.channel?.channel_id || state.channels[0]?.channel_id || "");
    }catch(e){
      notify(`登録失敗: ${e.message}`, "e");
    }finally{
      if(btn) btn.disabled = false;
    }
  }

  async function deleteChannel(id){
    const ch = findChannel(id);
    if(!ch || !confirm(`「${ch.name}」をベンチ先から登録解除しますか？`)) return;
    try{
      await jsonFetch(`/api/benchmark/channels/${encodeURIComponent(id)}`, {method:"DELETE"});
      notify("登録解除しました", "s");
      if(state.selectedId === id) state.selectedId = "";
      await load();
    }catch(e){
      notify(`登録解除失敗: ${e.message}`, "e");
    }
  }

  async function generateTtp(){
    const ch = findChannel(state.selectedId);
    if(!ch) return;
    const btn = $("analysisRunTtpBtn");
    if(btn) btn.disabled = true;
    showProgress(["TTP生成を開始します"]);
    const mining = $("analysisRunCommentMining");
    try{
      await jsonFetch("/api/ttp/generate", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({channel_ids:[ch.channel_id], name: slugify(ch.name || ch.channel_id), background:true, run_comment_mining: mining ? !!mining.checked : true})
      });
      pollTtp();
    }catch(e){
      hideProgress();
      notify(`TTP生成の開始に失敗: ${e.message}`, "e");
      if(btn) btn.disabled = false;
    }
  }

  async function generateTtpFromRadar(url, channelId, name){
    const target = url || (channelId ? `https://www.youtube.com/channel/${channelId}` : "");
    if(!target){ notify("代表チャンネルが見つかりません", "w"); return; }
    showProgress([`ジャンルレーダー候補をベンチ登録します: ${name || channelId}`]);
    try{
      const registered = await jsonFetch("/api/benchmark/channels", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({url: target, limit:30, force:false})
      });
      await loadChannels();
      const id = registered.channel?.channel_id || channelId;
      if(id) selectChannel(id);
      await jsonFetch("/api/ttp/generate", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({channel_ids:id ? [id] : [], name: slugify(name || id || "genre-radar"), background:true, run_comment_mining:true})
      });
      pollTtp();
    }catch(e){
      hideProgress();
      notify(`TTP生成の開始に失敗: ${e.message}`, "e");
    }
  }

  function slugify(s){
    return String(s || "ttp").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0,80) || "ttp";
  }

  function showProgress(lines){
    const box = $("analysisTtpProgress");
    const log = $("analysisTtpProgressLog");
    if(box) box.hidden = false;
    if(log) log.textContent = (lines || []).join("\n");
  }

  function hideProgress(){
    const box = $("analysisTtpProgress");
    if(box) box.hidden = true;
  }

  function pollTtp(){
    clearInterval(state.poll);
    const tick = async () => {
      try{
        const d = await jsonFetch("/api/ttp/status");
        showProgress(d.logs || []);
        if(d.running) return;
        clearInterval(state.poll);
        state.poll = null;
        await loadProfiles();
        await loadDemand(state.selectedId);
        state.selectedProfile = findProfileForChannel(state.selectedId);
        hideProgress();
        renderTtp();
        if((d.meta || {}).status === "failed") notify(`TTP生成失敗: ${(d.meta || {}).error || ""}`, "e");
        else notify("TTP仕様書を生成しました", "s");
      }catch(e){}
    };
    tick();
    state.poll = setInterval(tick, 2500);
  }

  function openCreateWizard(){
    const p = state.selectedProfile;
    if(!p){ notify("先に仕様書を生成してください", "w"); return; }
    if(typeof openChannelOnboardingWizard !== "function"){
      notify("かんたん作成ウィザードが見つかりません", "e");
      return;
    }
    openChannelOnboardingWizard();
    setTimeout(() => {
      const status = $("channelOnboardingStatus");
      const bench = $("onboardBenchmarkUrls");
      if(bench && state.selectedId){
        const ch = findChannel(state.selectedId);
        bench.value = ch?.url || ch?.source_url || `https://www.youtube.com/channel/${state.selectedId}`;
      }
      if(status){
        status.insertAdjacentHTML("beforebegin", `<div class="ff" id="onboardTtpProfileWrap">
          <label class="fl">引き継ぎTTPプロファイルID</label>
          <input type="text" class="fi" id="onboardTtpProfileId" value="${htmlEsc(p.id || "")}" readonly data-ttp-profile-id="${htmlEsc(p.id || "")}">
          <div class="fh">Phase C かんたん作成ウィザードへTTP仕様書を引き継いでいます。</div>
        </div>`);
      }
    }, 50);
  }

  window.AnalysisJ2 = {load, addChannel, deleteChannel, selectChannel, generateTtp, generateTtpFromRadar, openCreateWizard, applyPostingStrategy, activateTtpTab, useForProduction, copyProductionPrompt, applyProductionPrompt};
  if((location.hash === '#analysis' || location.hash === '#benchmark') && window.AutomationStudioContext?.channelId !== 'all') setTimeout(()=>load(),0);
})();
