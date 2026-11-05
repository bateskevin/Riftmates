/* Riftmates: optional, self-selected account (NOT ownership verification). */
(() => {
  'use strict';
  let selected = null, accounts = [], updating = false;
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const key = p => `${String(p.gameName||'').toLowerCase()}#${String(p.tagLine||'').toLowerCase()}`;
  const selectedKey = () => selected ? key(selected) : '';
  const css = document.createElement('style');
  css.textContent = `
  #rm-self-control{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0 14px;font-size:12px}
  #rm-self-control{position:relative;z-index:30}
  #rm-self-control #rm-self-picker{display:none}
  .rm-account-picker{position:relative;min-width:250px;max-width:360px;flex:1}
  .rm-account-trigger{width:100%;display:flex;align-items:center;gap:11px;text-align:left;padding:8px 12px!important;border:1px solid #527d69!important;border-radius:12px!important;background:linear-gradient(120deg,#1b3530,#142723)!important;color:#e8f9ee!important;box-shadow:0 3px 14px #0002;min-height:55px;transition:border-color .15s,box-shadow .15s}
  .rm-account-trigger:hover,.rm-account-trigger[aria-expanded="true"]{border-color:#80c99d!important;box-shadow:0 0 0 2px #79c89b20}
  .rm-account-avatar{width:38px;height:38px;object-fit:cover;border-radius:10px;border:1px solid #6b977c;background:#263b36;flex-shrink:0}
  .rm-account-avatar-fallback{display:grid;place-items:center;font-weight:800;color:#bfe9ce}
  .rm-account-trigger-text{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
  .rm-account-title{font-weight:800;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rm-account-subtitle{color:#9ebdae;font-size:11px}
  .rm-account-chevron{font-size:16px;color:#a5d9b7}
  .rm-account-menu{position:absolute;top:calc(100% + 8px);left:0;right:0;z-index:9999;background:#132823;border:1px solid #56816a;border-radius:14px;box-shadow:0 15px 35px #0009;padding:9px;max-height:350px;display:flex;flex-direction:column;gap:7px}
  .rm-account-menu[hidden]{display:none!important}
  .rm-account-search{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:9px;border:1px solid #3c6350;background:#0b1d19;color:#e5faed;outline:none}
  .rm-account-search:focus{border-color:#84cfa0}
  .rm-account-options{overflow-y:auto;min-height:0;display:flex;flex-direction:column;gap:3px}
  .rm-account-option{display:flex;align-items:center;gap:10px;width:100%;text-align:left;border:1px solid transparent!important;background:transparent!important;color:#e4f5e9!important;border-radius:9px!important;padding:8px!important}
  .rm-account-option:hover,.rm-account-option[aria-selected="true"]{background:#254b3a!important;border-color:#4b8664!important}
  .rm-account-option .rm-account-avatar{width:35px;height:35px}
  .rm-account-option-text{display:flex;flex-direction:column;min-width:0;gap:2px;flex:1}
  .rm-account-option-text strong{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rm-account-option-text small{color:#9dbbaa;font-size:11px}
  .rm-account-selected{color:#9af0b5;font-weight:900}
  .rm-account-empty{padding:14px;color:#a6bdb0;text-align:center}
  @media(max-width:550px){.rm-account-picker{min-width:190px;width:100%;max-width:none}}
  #rm-self-control button{background:#203b35;color:#c8efce;border:1px solid #487763;border-radius:8px;padding:8px;cursor:pointer}
  .rm-you-bubble{display:inline-flex!important;align-items:center;vertical-align:middle;padding:2px 7px;margin-left:6px;border:1px solid #69b68b;border-radius:999px;background:#244b39;color:#c8ffd9;font-weight:800;font-size:10px;letter-spacing:.04em}
  #rm-self-summary{margin:8px 0 18px;border:1px solid #446a56;border-radius:12px;background:#132a29;padding:14px;color:#e8f8ec}
  #rm-self-summary h3{margin:0 0 8px;font-size:16px}#rm-self-summary p{margin:5px 0;font-size:12px}
  #rm-self-summary .rm-self-stats{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;margin:10px 0}
  #rm-self-summary button{padding:7px 10px;margin:4px 6px 0 0;border:1px solid #5c9173;border-radius:8px;background:#1c4637;color:#e9ffe8;cursor:pointer}
  .rm-self-highlight{outline:1px solid #5dba83!important;outline-offset:1px;box-shadow:inset 0 0 0 1px #5dba8320}
  .rm-self-highlight-inline{border-radius:6px;background:rgba(72,172,111,.13)!important}
  .rm-you-bubble[data-rm-added]{white-space:nowrap;pointer-events:none}
  .rm-self-highlight .rm-you-bubble{box-shadow:0 0 8px #5dba8320}
  `;
  document.head.appendChild(css);
  const control = document.createElement('div'); control.id = 'rm-self-control';
  control.innerHTML = '<strong>My account</strong><select id="rm-self-picker" aria-label="Choose your account"><option value="">Not selected</option></select><button id="rm-self-clear" type="button">Clear</button><span id="rm-self-status" aria-live="polite"></span>';
  const header = document.querySelector('main .topbar') || document.querySelector('main');
  if (header) header.insertAdjacentElement('afterend', control);
  const picker = control.querySelector('#rm-self-picker');
  const fancy = document.createElement('div'); fancy.className = 'rm-account-picker';
  fancy.innerHTML = `<button type="button" class="rm-account-trigger" aria-label="Choose my Riot account" aria-haspopup="listbox" aria-expanded="false"><span class="rm-account-avatar rm-account-avatar-fallback">?</span><span class="rm-account-trigger-text"><span class="rm-account-title">Choose your account</span><span class="rm-account-subtitle">Personalize Riftmates</span></span><span class="rm-account-chevron">⌄</span></button><div class="rm-account-menu" hidden><input class="rm-account-search" type="search" placeholder="Search Riot IDs…" aria-label="Search accounts" autocomplete="off"><div class="rm-account-options" role="listbox" aria-label="Riot accounts"></div></div>`;
  picker.insertAdjacentElement('afterend', fancy);
  const trigger = fancy.querySelector('.rm-account-trigger');
  const menu = fancy.querySelector('.rm-account-menu');
  const search = fancy.querySelector('.rm-account-search');
  const options = fancy.querySelector('.rm-account-options');
  function iconId(p) {
    const player = (typeof state !== 'undefined' && state?.analytics?.players || []).find(x => x.puuid && x.puuid === p.puuid || key(x) === key(p));
    const id = Number(player?.profileIconId ?? p.profileIconId);
    return Number.isInteger(id) && id > 0 ? id : null;
  }
  function avatar(p) {
    const id = p && iconId(p);
    if (!id) return `<span class="rm-account-avatar rm-account-avatar-fallback">${escape((p?.gameName || '?').charAt(0).toUpperCase())}</span>`;
    const v = typeof version !== 'undefined' && /^[0-9]+\.[0-9]+\.[0-9]+$/.test(version) ? version : '15.10.1';
    return `<img class="rm-account-avatar" src="https://ddragon.leagueoflegends.com/cdn/${v}/img/profileicon/${id}.png" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'rm-account-avatar rm-account-avatar-fallback',textContent:'?' }))">`;
  }
  function renderPicker() {
    const chosen = accounts.find(a => selected && key(a) === selectedKey()) || selected;
    trigger.querySelector('.rm-account-avatar').outerHTML = avatar(chosen);
    trigger.querySelector('.rm-account-title').textContent = chosen ? chosen.gameName + '#' + chosen.tagLine : 'Choose your account';
    trigger.querySelector('.rm-account-subtitle').textContent = chosen ? 'My account · Not verified' : 'Personalize Riftmates';
    const q = search.value.trim().toLowerCase();
    const visible = accounts.map((a,i) => ({a,i})).filter(({a}) => key(a).includes(q));
    options.innerHTML = visible.length ? visible.map(({a,i}) => `<button type="button" class="rm-account-option" role="option" data-index="${i}" aria-selected="${!!selected && key(a) === selectedKey()}">${avatar(a)}<span class="rm-account-option-text"><strong>${escape(a.gameName)}</strong><small>#${escape(a.tagLine)}</small></span>${selected && key(a) === selectedKey() ? '<span class="rm-account-selected">✓ YOU</span>' : ''}</button>`).join('') : '<div class="rm-account-empty">No matching accounts</div>';
  }
  function closeMenu() {menu.hidden = true; trigger.setAttribute('aria-expanded','false');}
  trigger.addEventListener('click', () => {const open = menu.hidden;menu.hidden = !open;trigger.setAttribute('aria-expanded',String(open));if(open){search.value='';renderPicker();search.focus();}});
  search.addEventListener('input', renderPicker);
  search.addEventListener('keydown', e => {if(e.key === 'Escape'){closeMenu();trigger.focus();}if(e.key === 'Enter'){const first=options.querySelector('[data-index]');if(first)first.click();}});
  options.addEventListener('click', e => {const b=e.target.closest('[data-index]');if(!b)return;picker.value=b.dataset.index;closeMenu();save();});
  document.addEventListener('pointerdown', e => {if(!fancy.contains(e.target))closeMenu();});
  document.addEventListener('keydown', e => {if(e.key === 'Escape')closeMenu();});
  const status = control.querySelector('#rm-self-status');
  const summary = document.createElement('section'); summary.id = 'rm-self-summary'; summary.hidden = true;
  const overview = document.getElementById('overview'); if (overview) overview.prepend(summary);
  function findPlayer() { try { return (state?.analytics?.players || []).find(p => p.puuid === selected?.puuid || key(p) === selectedKey()); } catch (_) { return null; } }
  function renderSummary() {
    if (!summary.isConnected) return;
    if (!selected) { summary.hidden = true; return; }
    summary.hidden = false;
    const p = findPlayer();
    if (!p) { summary.innerHTML = `<h3>My account <span class="rm-you-bubble">YOU</span></h3><p>${escape(selected.gameName)}#${escape(selected.tagLine)}</p><p>Sync League data to see your personal statistics.</p>`; return; }
    let duos = [], champs = [];
    try {
      duos = (state.analytics.duos || []).filter(d => (d.playerIds||[]).includes(p.puuid)).sort((a,b) => b.winRate - a.winRate || b.games-a.games);
      const games = new Map();
      for (const m of state.matches || []) for (const part of m.players || []) if (part.puuid === p.puuid) {
        const s = games.get(part.champion) || {name:part.champion,games:0,wins:0}; s.games++; s.wins += Number(!!part.win); games.set(part.champion,s);
      }
      champs = [...games.values()].sort((a,b) => b.games-a.games);
    } catch (_) {}
    const partner = duos[0];
    const other = partner ? (partner.players || []).find(n => !n.toLowerCase().startsWith(p.gameName.toLowerCase()+'#')) : null;
    summary.innerHTML = `<h3>My Riftmates overview <span class="rm-you-bubble">YOU</span></h3><p>${escape(p.gameName)}#${escape(p.tagLine)}</p>
      <div class="rm-self-stats"><span><b>${Number(p.games||0)}</b> games</span><span><b>${Number(p.winRate||0).toFixed(1)}%</b> win rate</span><span><b>${Number(p.kda||0).toFixed(2)}</b> KDA</span></div>
      <p><b>Most played champions:</b> ${champs.slice(0,4).map(c=>`${escape(c.name)} (${c.games} games, ${Math.round(c.wins*100/c.games)}% WR)`).join(' · ') || 'No matches yet'}</p>
      <p><b>Top observed duo:</b> ${partner ? `${escape(other||'Teammate')} · ${partner.games} games · ${Number(partner.winRate||0).toFixed(1)}% WR` : 'No shared games yet'}</p>
      <p style="opacity:.7">Based on synced match samples, not predictions. Small samples can be misleading.</p>
      <button type="button" data-rm-self-nav="duos">My duos</button><button type="button" data-rm-self-nav="tierlist">Champion tier list</button><button type="button" data-rm-self-nav="draft">Draft lab</button>`;
  }
  // Match Riot IDs exactly where possible. A bare gameName is accepted only
  // when it uniquely identifies one tracked account (Riot names can repeat).
  const normalized = s => String(s ?? '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g,' ').trim();
  function isMine(value, allowEmbedded = false) {
    if (!selected) return false;
    const name = normalized(selected.gameName), tag = normalized(selected.tagLine);
    const raw = normalized(value).replace(/\s*#\s*/g,'#');
    if (!raw) return false;
    if (raw === name+'#'+tag || raw === name+' #'+tag) return true;
    const unique = accounts.filter(a=>normalized(a.gameName)===name).length===1;
    if (raw===name && unique) return true;
    if (!allowEmbedded) return false;
    // Duo labels join multiple accounts with a literal + separator.
    return raw.split(/\s+\+\s+/).some(part => part===name+'#'+tag || (unique && part===name));
  }
  const cardRules = [
    // League overview, Squad HQ, Your Squad and ranked profile cards
    ['[data-rank-profile]', '.profile-name', 'data-rank-profile'],
    ['.squad-row', '.squad-name'],
    ['.hq-person', 'h4'],
    // League duos, teams, roles, matches and combat views
    ['.duo-item', '.duo-name', null, true],
    ['.team-combo-player', '.team-combo-player-name'],
    ['.team-lab-player', 'strong'],
    ['.role-rank-player', '.role-rank-name'],
    ['.hq-match-player', 'span:last-child'],
    ['.rm-live-name-wrap', '.rm-live-player-name'],
    // TFT player profiles, duos, ranked, matches and match history
    ['.tft-participant', 'strong'],
    ['.tft-history-player', '.tft-history-player-body > strong'],
    ['.tft-history-head', '.tft-history-player'],
    ['.tft-profile', 'h3, h4, strong'],
    // Draft pickers, player options and selected player labels
    ['.draft-option[data-bench-player]', '.draft-option-text strong', 'data-bench-player'],
    ['.draft-option[data-player]', '.draft-option-text strong', 'data-player'],
    ['.role-pick-score', '.role-pick-name'],
  ];
  function markCards() {
    if (updating) return;
    updating = true;
    try {
      // Remove stale markers only when the selection changes; never tear down
      // and rebuild every marker on each render (would loop with observers).
      const id = selectedKey();
      if (markCards.lastSelection !== id) {
        document.querySelectorAll('.rm-you-bubble[data-rm-added]').forEach(el=>el.remove());
        document.querySelectorAll('.rm-self-highlight,.rm-self-highlight-inline').forEach(el=>el.classList.remove('rm-self-highlight','rm-self-highlight-inline'));
        markCards.lastSelection = id;
      }
      if (!selected) return;
      const player = findPlayer();
      const matches = (el, label, attr, embedded) => {
        if (attr && player?.puuid && el.getAttribute(attr) === player.puuid) return true;
        // Do not match another user's PUUID just because their display name matches.
        if (attr && el.hasAttribute(attr) && player?.puuid) return false;
        return isMine(label?.textContent, embedded);
      };
      for (const [selector, labelSelector, attr, embedded] of cardRules) {
        document.querySelectorAll(selector).forEach(el=>{
          if (el.closest('#rm-self-control,#rm-self-summary')) return;
          const label=el.querySelector(labelSelector);
          if (!label || !matches(el,label,attr,embedded)) return;
          if (!label.querySelector('.rm-you-bubble[data-rm-added]')) {
            const badge=document.createElement('span');
            badge.className='rm-you-bubble'; badge.dataset.rmAdded='1'; badge.textContent='YOU';
            label.appendChild(badge);
          }
          el.classList.add('rm-self-highlight');
        });
      }
      // Any other view: match individual account labels in known player UI
      // elements, rather than searching arbitrary text across the page.
      const otherLabels = [
        '.player-card h3','.player-card h4','.player-card .player-name',
        '.profile-name','.rank-profile-name','.ranked-player-name',
        '.match-player-name','.match-name','.combat-player-name',
        '.tft-player-name','.tft-profile-name','.tft-duo-name',
        '.tft-rank-name','.tft-history-player-body > strong',
        '.draft-option-text strong','.draft-selected strong',
        '.role-rank-name','.team-combo-player-name',
        '.team-lab-player strong','.hq-match-player > span:last-child',
        '.rm-live-player-name'
      ];
      document.querySelectorAll(otherLabels.join(',')).forEach(label=>{
        if (label.closest('#rm-self-control,#rm-self-summary')) return;
        // Badge text is ignored when checking a previously marked element.
        const raw=Array.from(label.childNodes).filter(n=>!(n.nodeType===1 && n.classList.contains('rm-you-bubble'))).map(n=>n.textContent).join('');
        if (!isMine(raw)) return;
        if (!label.querySelector('.rm-you-bubble[data-rm-added]')) {
          const badge=document.createElement('span');badge.className='rm-you-bubble';badge.dataset.rmAdded='1';badge.textContent='YOU';label.appendChild(badge);
        }
        label.classList.add('rm-self-highlight-inline');
      });
    } finally { updating = false; }
  }
  let scheduled = false;
  function refresh() {
    if (scheduled) return;
    scheduled=true;
    requestAnimationFrame(()=>{scheduled=false;renderSummary();markCards();});
  }
  async function load() {
    try {
      const [a,b] = await Promise.all([fetch('/api/accounts',{credentials:'same-origin'}),fetch('/api/my-account',{credentials:'same-origin'})]);
      if (!a.ok || !b.ok) throw Error('Could not load your account preferences');
      accounts = (await a.json()).players || []; selected = (await b.json()).account || null;
      picker.innerHTML = '<option value="">Not selected</option>' + accounts.map((p,i)=>`<option value="${i}">${escape(p.gameName)}#${escape(p.tagLine)}</option>`).join('');
      picker.value = selected ? String(accounts.findIndex(p=>key(p)===selectedKey())) : '';
      if (picker.value === '-1') picker.value='';
      renderPicker(); refresh();
    } catch(e) { status.textContent = e.message; }
  }
  async function save() {
    const chosen = picker.value === '' ? null : accounts[Number(picker.value)];
    status.textContent = 'Saving…';
    try {
      const r = await fetch('/api/my-account',{method:chosen?'PUT':'DELETE',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:chosen?JSON.stringify({gameName:chosen.gameName,tagLine:chosen.tagLine}):undefined});
      const d=await r.json(); if (!r.ok) throw Error(d.error||'Could not save');
      selected=d.account||null; status.textContent=selected?'Saved (not verified)':'Cleared'; renderPicker(); refresh();
    } catch(e) { status.textContent=e.message; }
  }
  picker.addEventListener('change',save);
  control.querySelector('#rm-self-clear').addEventListener('click',()=>{picker.value='';save();});
  summary.addEventListener('click',e=>{const btn=e.target.closest('[data-rm-self-nav]');if(btn&&typeof go==='function')go(btn.dataset.rmSelfNav);});
  // All views (League and TFT) render dynamically. Observe the SPA's main
  // content for newly inserted cards, and coalesce updates to one frame.
  const contentRoot=document.querySelector('main') || document.body;
  new MutationObserver(records=>{
    // Ignore changes that only add/remove our own YOU bubbles.
    if (records.some(r=>Array.from(r.addedNodes).concat(Array.from(r.removedNodes)).some(n=>
      n.nodeType===1 && !n.classList?.contains('rm-you-bubble') &&
      !n.closest?.('#rm-self-control,#rm-self-summary')))) refresh();
  }).observe(contentRoot,{childList:true,subtree:true});
  window.addEventListener('riftmates:navigate',refresh);
  load();
})();
