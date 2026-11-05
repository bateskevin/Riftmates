"""Privacy-conscious, persistent Riftmates usage metrics for local Prometheus."""
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from flask import Blueprint, Response, abort, current_app, request, session

bp = Blueprint('site_analytics', __name__)
_lock = threading.RLock()
DB_PATH = os.path.join(os.path.dirname(__file__), 'riftmates_analytics.sqlite3')
VIEW_LABELS = {'overview': 'Overview', 'hq': 'Squad HQ', 'players': 'Players', 'duos': 'Duos & synergy', 'teams': 'Team combos', 'ranked': 'Ranked progression', 'arena': '1v1 Arena', 'champs': 'Champions', 'tierlist': 'Champion tier list', 'runes': 'Rune tiers', 'items': 'Item rankings', 'benchmark': 'Champion benchmark', 'roles': 'Roles', 'role-rankings': 'Role rankings', 'combat': 'Kills & deaths', 'draft': 'Draft lab', 'matches': 'Match history', 'squad': 'Your squad', 'settings': 'Settings', 'tft': 'TFT Overview', 'tft-hq': 'TFT Squad HQ', 'tft-players': 'TFT Players', 'tft-duos': 'TFT Duos & synergies', 'tft-teams': 'TFT Team combos', 'tft-ranked': 'TFT Ranked progression', 'tft-tiers': 'TFT Tier lists', 'tft-matches': 'TFT Match history'}
PAGES = set(VIEW_LABELS.values())


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('CREATE TABLE IF NOT EXISTS visitors (uid TEXT PRIMARY KEY, first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, last_session INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(day,kind,label))')
    conn.execute('CREATE TABLE IF NOT EXISTS totals (kind TEXT NOT NULL, label TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind,label))')
    conn.execute('CREATE TABLE IF NOT EXISTS daily_visitors (day TEXT NOT NULL, uid TEXT NOT NULL, PRIMARY KEY(day,uid))')
    return conn

def increment(conn, kind, label, day=None):
    day = day or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn.execute('INSERT INTO daily(day,kind,label,count) VALUES(?,?,?,1) ON CONFLICT(day,kind,label) DO UPDATE SET count=count+1', (day,kind,label))
    conn.execute('INSERT INTO totals(kind,label,count) VALUES(?,?,1) ON CONFLICT(kind,label) DO UPDATE SET count=count+1', (kind,label))

def visit(uid):
    now = int(time.time())
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    with _lock, db() as conn:
        row = conn.execute('SELECT last_session FROM visitors WHERE uid=?', (uid,)).fetchone()
        if row is None:
            conn.execute('INSERT INTO visitors VALUES(?,?,?,?)', (uid,now,now,now))
            increment(conn,'visitor','new',day)
            increment(conn,'session','started',day)
        else:
            if now - row[0] >= 1800:
                increment(conn,'session','started',day)
                conn.execute('UPDATE visitors SET last_session=? WHERE uid=?', (now,uid))
            conn.execute('UPDATE visitors SET last_seen=? WHERE uid=?', (now,uid))
        conn.execute('INSERT OR IGNORE INTO daily_visitors VALUES(?,?)', (day,uid))

def track_sync(game, status):
    with _lock, db() as conn:
        increment(conn,'sync',f'{game}:{status}')

def track_page(page):
    if page not in PAGES: page = 'Other'
    with _lock, db() as conn:
        increment(conn,'page',page)

@bp.post('/api/analytics/page')
def page_event():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict): return ('', 400)
    page = VIEW_LABELS.get(str(payload.get('view', ''))[:40])
    if page is None: return ('', 400)
    track_page(page)
    return ('', 204)

@bp.post('/api/analytics/engagement')
def engagement_event():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict): return ('', 400)
    page = VIEW_LABELS.get(str(payload.get('view', ''))[:40])
    if page is None: return ('', 400)
    try:
        seconds = int(payload.get('seconds', 0))
    except (ValueError, TypeError):
        return ('', 400)
    if seconds < 1 or seconds > 30: return ('', 400)
    with _lock, db() as conn:
        increment_by(conn, 'engagement', page, seconds)
    return ('', 204)

def increment_by(conn, kind, label, amount):
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn.execute('INSERT INTO daily(day,kind,label,count) VALUES(?,?,?,?) ON CONFLICT(day,kind,label) DO UPDATE SET count=count+excluded.count', (day,kind,label,amount))
    conn.execute('INSERT INTO totals(kind,label,count) VALUES(?,?,?) ON CONFLICT(kind,label) DO UPDATE SET count=count+excluded.count', (kind,label,amount))

@bp.get('/analytics.js')
def tracker_script():
    return Response(TRACKER_JS, mimetype='application/javascript', headers={'Cache-Control':'no-store, max-age=0'})

@bp.get('/metrics')
def prometheus_metrics():
    # Keep private visitor metrics on the Pi. Prometheus should scrape 127.0.0.1.
    if request.remote_addr not in ('127.0.0.1', '::1'):
        abort(403)
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    with _lock, db() as conn:
        visitors = conn.execute('SELECT COUNT(*) FROM visitors').fetchone()[0]
        today_visitors = conn.execute('SELECT COUNT(*) FROM daily_visitors WHERE day=?',(day,)).fetchone()[0]
        totals = conn.execute('SELECT kind,label,count FROM totals').fetchall()
        today = conn.execute('SELECT kind,label,count FROM daily WHERE day=?',(day,)).fetchall()
    lines = [
        '# HELP riftmates_unique_visitors_total Unique browsers seen (persistent cookie).',
        '# TYPE riftmates_unique_visitors_total gauge',
        f'riftmates_unique_visitors_total {visitors}',
        '# HELP riftmates_unique_visitors_today Unique browsers today (UTC).',
        '# TYPE riftmates_unique_visitors_today gauge',
        f'riftmates_unique_visitors_today {today_visitors}',
    ]
    specs = {'session':('riftmates_sessions_total', 'Sessions started (30-minute inactivity threshold).'), 'page':('riftmates_page_views_total','Client-reported page/tab views.'), 'sync':('riftmates_sync_requests_total','Sync requests by game and result.'), 'engagement':('riftmates_section_active_seconds_total','Approximate visible active time by section, sampled every 15 seconds.')}
    for kind,(name,helptext) in specs.items():
        lines += [f'# HELP {name} {helptext}',f'# TYPE {name} counter']
        for k,l,n in totals:
            if k!=kind: continue
            if kind=='sync':
                game,status=l.split(':',1)
                lines.append(f'{name}{{game="{game}",status="{status}"}} {n}')
            elif kind in ('page','engagement'):
                label=l.replace('\\','\\\\').replace('"','\\"')
                lines.append(f'{name}{{section="{label}"}} {n}' if kind=='engagement' else f'{name}{{page="{label}"}} {n}')
            else: lines.append(f'{name} {n}')
    for kind,name in [('session','riftmates_sessions_today'),('page','riftmates_page_views_today'),('sync','riftmates_sync_requests_today'),('engagement','riftmates_section_active_seconds_today')]:
        lines += [f'# HELP {name} Events recorded today UTC.',f'# TYPE {name} gauge']
        for k,l,n in today:
            if k!=kind: continue
            if kind=='sync':
                game,status=l.split(':',1)
                lines.append(f'{name}{{game="{game}",status="{status}"}} {n}')
            elif kind in ('page','engagement'):
                label=l.replace('\\','\\\\').replace('"','\\"')
                lines.append(f'{name}{{section="{label}"}} {n}' if kind=='engagement' else f'{name}{{page="{label}"}} {n}')
            else: lines.append(f'{name} {n}')
    return Response('\n'.join(lines)+'\n',mimetype='text/plain; version=0.0.4; charset=utf-8')

TRACKER_JS = r'''(() => {
  'use strict';
  const known = new Set('overview hq players duos teams ranked arena champs tierlist runes items benchmark roles role-rankings combat draft matches squad settings tft tft-hq tft-players tft-duos tft-teams tft-ranked tft-tiers tft-matches'.split(' '));
  let current = null;
  let lastTick = Date.now();
  function emit(endpoint, payload) {
    fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), credentials:'same-origin', keepalive:true}).catch(()=>{});
  }
  function creditTime() {
    const now = Date.now();
    const elapsed = Math.min(30, Math.floor((now-lastTick)/1000));
    if (current && !document.hidden && document.hasFocus() && elapsed >= 1)
      emit('/api/analytics/engagement', {view:current, seconds:elapsed});
    lastTick = now;
  }
  function setView(view) {
    if (!known.has(view) || view === current) return;
    creditTime();
    current = view;
    lastTick = Date.now();
    emit('/api/analytics/page', {view});
  }
  function activeView() {
    const active = document.querySelector('main .section.active') || document.querySelector('.section.active');
    return active && known.has(active.id) ? active.id : null;
  }
  function update() { const view = activeView(); if (view) setView(view); }
  // Main path: the actual go(id) function announces navigation explicitly.
  window.addEventListener('riftmates:navigate', event => {
    if (event.detail && typeof event.detail.view === 'string') setView(event.detail.view);
  });
  function start() {
    update();
    // Fallback for older frontends, and navigation not routed through go(id).
    const root = document.querySelector('main') || document.body;
    new MutationObserver(update).observe(root, {subtree:true, attributes:true, attributeFilter:['class']});
    document.addEventListener('click', () => queueMicrotask(update), true);
    window.addEventListener('pageshow', update);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) creditTime(); else {lastTick=Date.now(); update();}
    });
    window.addEventListener('focus', () => {lastTick=Date.now(); update();});
    window.addEventListener('blur', creditTime);
    window.addEventListener('pagehide', creditTime);
    setInterval(() => {creditTime(); update();}, 15000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',start,{once:true});
  else start();
})();'''
