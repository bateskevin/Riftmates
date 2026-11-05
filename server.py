import os

import time

import threading

import sqlite3

import json

from contextlib import closing
from collections import defaultdict

from itertools import combinations

from concurrent.futures import ThreadPoolExecutor, as_completed

from urllib.parse import quote, urljoin
from html.parser import HTMLParser
from html import unescape as html_unescape
import re

import requests

from dotenv import load_dotenv

from flask import Flask, jsonify, request, send_file, send_from_directory, session, Response

load_dotenv()

API_KEY = os.getenv("RIOT_API_KEY", "").strip()
TFT_API_KEY = os.getenv("RIOT_TFT_API_KEY", "").strip() or API_KEY  # Separate TFT key; legacy fallback

REGION = os.getenv("RIOT_REGION", "europe").lower()  # Legacy default, used only to choose initial platform.
RIOT_PLATFORMS = {
    "EUW1":"europe","EUN1":"europe","TR1":"europe","RU":"europe",
    "NA1":"americas","BR1":"americas","LA1":"americas","LA2":"americas",
    "KR":"asia","JP1":"asia",
    "OC1":"sea","PH2":"sea","SG2":"sea","TH2":"sea","TW2":"sea","VN2":"sea",
}
DEFAULT_PLATFORM = {"europe":"EUW1","americas":"NA1","asia":"KR","sea":"OC1"}.get(REGION,"EUW1")
def routing_for(settings):
    return RIOT_PLATFORMS[settings["platform"]]


PORT = int(os.getenv("PORT", "8765"))

if not API_KEY:

    raise RuntimeError("RIOT_API_KEY is missing. Add it to your .env file.")

app = Flask(__name__)

from site_analytics import bp as analytics_bp, visit as analytics_visit, track_sync as analytics_sync
app.register_blueprint(analytics_bp)
# A stable key keeps anonymous profiles accessible across server restarts.
# Set FLASK_SECRET_KEY explicitly for any public deployment.
_secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".riftmates_session_secret")
if os.getenv("FLASK_SECRET_KEY"):
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
else:
    import secrets
    try:
        with open(_secret_path, "x", encoding="utf-8") as _secret_file:
            _secret_file.write(secrets.token_hex(32))
        os.chmod(_secret_path, 0o600)
    except FileExistsError:
        pass
    with open(_secret_path, encoding="utf-8") as _secret_file:
        app.secret_key = _secret_file.read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1", PERMANENT_SESSION_LIFETIME=60*60*24*30)


@app.get("/rank_emblems/<tier>.svg")
def serve_rank_emblem(tier):
    # Flask does not serve files next to server.py automatically.
    # Restrict this route to the bundled emblem names.
    allowed = {"iron", "bronze", "silver", "gold", "platinum", "emerald",
               "diamond", "master", "grandmaster", "challenger", "unranked"}
    if tier not in allowed:
        from flask import abort
        abort(404)
    from flask import send_from_directory
    return send_from_directory(os.path.join(BASE_DIR, "rank_emblems"),
                               tier + ".svg", mimetype="image/svg+xml")

@app.get("/tft_penguin_search.gif")
def serve_tft_penguin_gif():
    return send_from_directory(BASE_DIR, "tft_penguin_search.gif", mimetype="image/gif")

@app.get("/heimer_feeding_poro.gif")
def serve_heimer_feeding_gif():
    from flask import send_from_directory
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "heimer_feeding_poro.gif", mimetype="image/gif")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DPM_BASE = "https://dpm.lol"


def _clean_player_list(items):
    """Validate the editable list of Riot IDs; no account is immutable."""
    cleaned, seen = [], set()

    if not isinstance(items, list):
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue

        game_name = str(item.get("gameName", "")).strip()
        tag_line = str(item.get("tagLine", "")).strip()
        key = (game_name.casefold(), tag_line.casefold())

        if (
            not game_name
            or not tag_line
            or len(game_name) > 64
            or len(tag_line) > 16
            or key in seen
        ):
            continue

        cleaned.append({
            "gameName": game_name,
            "tagLine": tag_line,
            "games": [g for g in ("lol", "tft") if g in (item.get("games") if isinstance(item.get("games"), list) else ["lol", "tft"])] or ["lol", "tft"]
        })
        seen.add(key)

    return cleaned

USER_DB = os.path.join(BASE_DIR, "riftmates_users.sqlite3")
DEFAULT_SETTINGS = {"queues": {"normals": True, "flex": True, "rankedSolo": False}, "platform": DEFAULT_PLATFORM, "defaultGameCount": 100}
DEFAULT_PLAYERS = [
    {"gameName": "INT Budget Faker", "tagLine": "xpp", "games": ["lol", "tft"]},
    {"gameName": "INT Afkaler", "tagLine": "555", "games": ["lol", "tft"]},
    {"gameName": "INT Robin Hook", "tagLine": "EUW", "games": ["lol", "tft"]},
    {"gameName": "INT Carry Potter", "tagLine": "Int", "games": ["lol", "tft"]},
    {"gameName": "INT RunitDauniJR", "tagLine": "EUW", "games": ["lol"]},
    {"gameName": "padret", "tagLine": "euw", "games": ["lol"]},
    {"gameName": "morgana freeman", "tagLine": "int", "games": ["lol"]},
    {"gameName": "maneyy", "tagLine": "euw", "games": ["lol"]},
    {"gameName": "Int Jax Sparrow", "tagLine": "euw", "games": ["lol"]},
]
SYNC_PROGRESS = {}
SYNC_PROGRESS_LOCK = threading.Lock()
USER_SYNC_LOCKS = {}
USER_SYNC_LOCKS_LOCK = threading.Lock()
_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False

def current_user_id():
    if "uid" not in session:
        import secrets
        session["uid"] = secrets.token_urlsafe(24)
        session.permanent = True
    return session["uid"]

@app.before_request
def ensure_user_session():
    uid = current_user_id()
    if request.path == "/":
        analytics_visit(uid)

def user_sync_lock(uid):
    with USER_SYNC_LOCKS_LOCK:
        return USER_SYNC_LOCKS.setdefault(uid, threading.Lock())

def _init_user_db():
    """Initialize the schema once, rather than changing WAL mode per HTTP request."""
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _DB_INIT_LOCK:
        if _DB_INITIALIZED:
            return
        with sqlite3.connect(USER_DB, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS user_data (user_id TEXT PRIMARY KEY, players TEXT NOT NULL, settings TEXT NOT NULL, pro_config TEXT NOT NULL)")
        _DB_INITIALIZED = True

def _user_db():
    _init_user_db()
    conn = sqlite3.connect(USER_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def _user_record(uid=None):
    uid = uid or current_user_id()
    with closing(_user_db()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_data (user_id, players, settings, pro_config) VALUES (?, ?, ?, ?)",
            (uid, json.dumps(DEFAULT_PLAYERS), json.dumps(DEFAULT_SETTINGS), json.dumps({"teams": []})),
        )
        row = conn.execute("SELECT * FROM user_data WHERE user_id=?", (uid,)).fetchone()
        # Recover empty profiles created by the broken multi-user release.
        # The existing UI never allowed intentionally saving an empty roster.
        if row is not None and row["players"] in ("[]", "null", ""):
            conn.execute("UPDATE user_data SET players=? WHERE user_id=? AND players=?", (json.dumps(DEFAULT_PLAYERS), uid, row["players"]))
            row = conn.execute("SELECT * FROM user_data WHERE user_id=?", (uid,)).fetchone()
        return dict(row)

def _get_user_players(uid=None):
    try:
        raw = json.loads(_user_record(uid)["players"])
        # Migrate legacy roster entries that predate per-game selections.
        # Never overwrite an explicit games selection, including TFT enabled manually.
        defaults = {(p["gameName"].casefold(), p["tagLine"].casefold()): p["games"] for p in DEFAULT_PLAYERS}
        changed = False
        for entry in raw:
            if isinstance(entry, dict) and not isinstance(entry.get("games"), list):
                key = (str(entry.get("gameName", "")).strip().casefold(), str(entry.get("tagLine", "")).strip().casefold())
                entry["games"] = list(defaults.get(key, ["lol"]))
                changed = True
        if changed:
            # Persist the migration so later reads and sync use the same selections.
            with closing(_user_db()) as conn, conn:
                conn.execute("UPDATE user_data SET players=? WHERE user_id=?", (json.dumps(raw), uid or current_user_id()))
        return _clean_player_list(raw)
    except (ValueError, TypeError):
        return []

def _get_user_settings(uid=None):
    try:
        raw = json.loads(_user_record(uid)["settings"])
        queues = raw.get("queues", {})
        platform = str(raw.get("platform", DEFAULT_PLATFORM)).upper()
        if platform not in RIOT_PLATFORMS: platform = DEFAULT_PLATFORM
        return {"queues": {k: bool(queues.get(k, DEFAULT_SETTINGS["queues"][k])) for k in DEFAULT_SETTINGS["queues"]}, "platform": platform, "tftQueues": {k: bool((raw.get("tftQueues") or {}).get(k, True)) for k in ("ranked", "doubleup")}, "defaultGameCount": int(raw.get("defaultGameCount", 100)) if str(raw.get("defaultGameCount", 100)).isdigit() and int(raw.get("defaultGameCount", 100)) in range(25, 1001, 25) else 100}
    except (ValueError, TypeError):
        return json.loads(json.dumps(DEFAULT_SETTINGS))

def _get_pro_config(uid=None):
    try:
        return json.loads(_user_record(uid)["pro_config"])
    except (ValueError, TypeError):
        return {"teams": []}

def _set_user_field(field, value, uid=None):
    if field not in {"players", "settings", "pro_config"}:
        raise ValueError("Invalid user field")
    uid = uid or current_user_id()
    _user_record(uid)
    with closing(_user_db()) as conn, conn:
        conn.execute(f"UPDATE user_data SET {field}=? WHERE user_id=?", (json.dumps(value, ensure_ascii=False), uid))

def set_sync_progress(percent, message, done=0, total=0, active=True):
    with SYNC_PROGRESS_LOCK:
        SYNC_PROGRESS[current_user_id()] = {"active": active, "percent": max(0, min(100, int(percent))), "message": message, "done": int(done), "total": int(total)}

class DPMPageParser(HTMLParser):
    """Extract visible page text and links, excluding script/style source."""
    def __init__(self):
        super().__init__()
        self.links = []
        self.text = []
        self._anchor = None
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and attrs.get("href"):
            # Keep nested anchors from accidentally inheriting previous text.
            self._anchor = {"href": attrs["href"], "text": []}

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_data(self, data):
        if self._ignored_depth:
            return
        value = data.strip()
        if value:
            self.text.append(value)
            if self._anchor is not None:
                self._anchor["text"].append(value)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._anchor is not None:
            self.links.append({"href": self._anchor["href"], "text": " ".join(self._anchor["text"]).strip()})
            self._anchor = None

def dpm_get(path):
    response=requests.get(urljoin(DPM_BASE, path), timeout=20, headers={"User-Agent":"Riftmates local analytics app"})
    response.raise_for_status()
    return response.text

def dpm_parse(html):
    parser=DPMPageParser(); parser.feed(html); return parser

def get_dpm_teams():
    html=dpm_get("/pro/teams")
    page=dpm_parse(html); teams=[]; seen=set()
    for link in page.links:
        m=re.match(r"^/pro/teams/([A-Za-z0-9_-]+)$", link["href"])
        slug=m.group(1) if m else None
        label=link["text"]
        if slug and slug.casefold() not in {"teams","leaderboard"} and slug.casefold() not in seen:
            seen.add(slug.casefold()); teams.append({"slug":slug,"name":label or slug,"url":f"{DPM_BASE}/pro/teams/{slug}"})
    return sorted(teams,key=lambda x:x["name"].casefold())

def get_dpm_team_players(slug):
    page=dpm_parse(dpm_get(f"/pro/teams/{quote(slug,safe='')}"))
    found=[]; seen=set()
    for link in page.links:
        m=re.match(r"^/pro/([A-Za-z0-9_-]+)$", link["href"])
        if m and m.group(1).casefold() not in {"teams","about","links"} and m.group(1).casefold() not in seen:
            seen.add(m.group(1).casefold()); found.append({"slug":m.group(1),"name":link["text"] or m.group(1)})
    return found


RIFTVANTA_BASE = "https://riftvanta.com"

def riftvanta_get(path):
    response = requests.get(urljoin(RIFTVANTA_BASE, path), timeout=20,
                            headers={"User-Agent": "Mozilla/5.0 Riftmates local analytics"})
    response.raise_for_status()
    return response.text

def _slugify_team_name(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").casefold()).strip("-")

def _riftvanta_team_players(team_name, selected_slug=""):
    """Resolve the selected dropdown team to a RIFTVANTA team page, then read its roster.

    Team names and DPM slugs differ for many organizations (for example FNC/Fnatic,
    GX/GIANTX, and GEN/Gen.G), so try known aliases, the selected DPM slug, and a
    live RIFTVANTA team directory rather than relying on one guessed URL.
    """
    aliases = {
        "giantx": ["giantx", "gx"], "gx": ["giantx", "gx"],
        "g2": ["g2-esports", "g2"], "g2 esports": ["g2-esports", "g2"],
        "fnatic": ["fnatic", "fnc"], "karmine corp": ["karmine-corp", "karmine-corp-blue"],
        "movistar koi": ["movistar-koi", "koi"], "natus vincere": ["natus-vincere", "navi"],
        "shifters": ["shifters", "team-shifters"], "sk gaming": ["sk-gaming", "sk"],
        "team heretics": ["team-heretics", "heretics"], "team vitality": ["team-vitality", "vitality"],
        "los ratones": ["los-ratones"], "cloud9 kia": ["cloud9", "cloud9-kia"],
        "cloud9": ["cloud9", "cloud9-kia"], "team liquid alienware": ["team-liquid", "team-liquid-alienware"],
        "team liquid": ["team-liquid", "team-liquid-alienware"], "tl": ["team-liquid"],
        "gen.g esports": ["gen-g", "gen-g-esports"], "gen.g": ["gen-g", "gen-g-esports"],
        "gen g": ["gen-g", "gen-g-esports"], "hanwha life esports": ["hanwha-life-esports", "hle"],
        "dplus kia": ["dplus-kia", "dplus"], "bnk fearx": ["bnk-fearx", "fearx"],
        "kt rolster": ["kt-rolster", "kt"], "nongshim redforce": ["nongshim-redforce"],
        "dn soopers": ["dn-soopers", "dn-soopers"], "anyone's legend": ["anyones-legend", "anyone-s-legend"],
        "bilibili gaming": ["bilibili-gaming", "blg"], "edward gaming": ["edward-gaming", "edg"],
        "jd gaming": ["jd-gaming", "jdg"], "lgd gaming": ["lgd-gaming", "lgd"],
        "lng esports": ["lng-esports", "lng"], "ninjas in pyjamas": ["ninjas-in-pyjamas", "nip"],
        "oh my god": ["oh-my-god", "omg"], "thundertalk gaming": ["thundertalk-gaming", "tt"],
        "top esports": ["top-esports", "tes"], "ultra prime": ["ultra-prime", "up"],
        "team we": ["team-we", "we"], "weibo gaming": ["weibo-gaming", "wbg"],
        "dignitas": ["dignitas", "dig"], "disguised": ["disguised", "dsG"],
        "flyquest": ["flyquest", "fly-quest"], "lyon": ["lyon"], "sentinels": ["sentinels"],
        "shopify rebellion": ["shopify-rebellion", "sr"], "brion": ["brion", "ok-brion"],
        "drx": ["drx"], "t1": ["t1"],
    }
    name_key = re.sub(r"\s+", " ", str(team_name or "").casefold()).strip()
    candidates = list(aliases.get(name_key, []))
    for value in (selected_slug, _slugify_team_name(team_name)):
        value = str(value or "").strip().casefold().replace("_", "-")
        if value and value not in candidates:
            candidates.append(value)

    # A live directory is more reliable than assumptions about how a team is slugged.
    try:
        directory = dpm_parse(riftvanta_get("/en/teams"))
        wanted = re.sub(r"[^a-z0-9]", "", name_key)
        for link in directory.links:
            href = link.get("href", "")
            m = re.fullmatch(r"/en/teams/([a-z0-9-]+)", href, re.I)
            label_key = re.sub(r"[^a-z0-9]", "", link.get("text", "").casefold())
            if m and (label_key == wanted or (wanted and (wanted in label_key or label_key in wanted))):
                if m.group(1).casefold() not in candidates:
                    candidates.insert(0, m.group(1).casefold())
    except requests.RequestException:
        pass

    seen_candidates = set()
    last_error = None
    for team_slug in candidates:
        if team_slug in seen_candidates:
            continue
        seen_candidates.add(team_slug)
        try:
            page = dpm_parse(riftvanta_get(f"/en/teams/{quote(team_slug, safe='-')}"))
        except requests.RequestException as exc:
            last_error = exc
            continue
        players, seen = [], set()
        for link in page.links:
            m = re.fullmatch(r"/en/pros/([a-z0-9-]+)", link.get("href", ""), re.I)
            if not m or m.group(1).casefold() in seen:
                continue
            seen.add(m.group(1).casefold())
            player_slug = m.group(1)
            label = re.sub(r"\s+", " ", link.get("text", "")).strip()
            label = re.sub(r"^(?:Top|Jungle|Mid|Bot|ADC|Support)\s*", "", label, flags=re.I)
            label = re.sub(r"(?:No account|Solo queue profile|Account)$", "", label, flags=re.I).strip()
            players.append({"slug": player_slug, "name": label or player_slug.replace("-", " ").title(), "source": "RIFTVANTA"})
        if players:
            return players
    if last_error and not candidates:
        raise last_error
    return []

def _riftvanta_player_accounts(player_slug):
    """Read Riot IDs only from the dedicated Solo queue accounts section."""
    html = riftvanta_get(f"/en/pros/{quote(player_slug, safe='-')}")
    page = dpm_parse(html)
    # Prefer the raw SSR text because account labels can be split across nested spans.
    text = _visible_html_text(html) or re.sub(r"\s+", " ", " ".join(page.text)).strip()
    match = re.search(r"Solo\s*queue\s*accounts?(.*?)(?=Coming soon|Last 2 Weeks|Recent Games|Champion Stats|$)", text, re.I)
    if not match:
        return []
    section = match.group(1)
    # Prefer account anchors because their text usually contains the exact Riot ID.
    found = []
    for link in page.links:
        label = re.sub(r"\s+", " ", link.get("text", "")).strip()
        if "#" in label and ("/account" in link.get("href", "").casefold() or
                             re.search(r"/pros/[^/]+", link.get("href", ""), re.I)):
            found.extend(_riot_ids_from_text(label))
    # Fallback to the section text. RIFTVANTA's tags are compact, so stop at whitespace
    # after the tag rather than accidentally swallowing the next account's label.
    region_prefix = r"(?:EUW|EUNE|NA|KR|BR|LAN|LAS|OCE|TR|RU|JP|SG|PH|TH|TW|VN)"
    for m in re.finditer(r"(?:^|\s)" + region_prefix + r"\s+([^#<>]{1,64}?)#([A-Za-z0-9_.-]{1,16})(?=\s|$|[·|,;])", section, re.I):
        game = re.sub(r"\s+", " ", m.group(1)).strip(" ·|:-")
        if _is_plausible_riot_id(game, m.group(2)):
            found.append({"gameName": game, "tagLine": m.group(2)})
    result, seen = [], set()
    for item in found:
        game, tag = item["gameName"].strip(), item["tagLine"].strip()
        game = re.sub(r"^(?:EUW|EUNE|NA|KR|BR|LAN|LAS|OCE|TR|RU|JP|SG|PH|TH|TW|VN)\s+", "", game, flags=re.I)
        key = (game.casefold(), tag.casefold())
        if _is_plausible_riot_id(game, tag) and key not in seen:
            seen.add(key); result.append({"gameName": game, "tagLine": tag})
    return result

def _riot_ids_from_text(value):
    """Extract Riot IDs from page text, including DPM links where timestamps touch tags.

    DPM currently renders profile links like ``Razørk Activoo#razzz2d ago``
    (no space between the tag and timestamp). Older parser versions only accepted
    a space before rank/stat metadata, so they silently returned no accounts.
    """
    value = html_unescape(str(value or ""))
    found = []
    # Riot IDs are parsed up to a clear suffix boundary. The suffix can be a rank,
    # W/L statistics, a region, a timestamp joined directly to the tag, or end of label.
    pattern = re.compile(
        r"(?P<game>[^#<>\n]{1,80}?)\s*#(?P<tag>[\w .’'\-]{1,24}?)"
        r"(?=(?:\s*\d{1,3}\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|[smhdw])\s+ago)"
        r"|\s*(?:EUW|EUNE|KR|NA|BR|LAN|LAS|OCE|TR|JP|RU|SG|VN|TW|ME)(?=\s|Level\b|$)"
        r"|\s+(?:\d{2,4}\s*LP|[IVX]{1,3}\s*[-+]?\s*\d+\s*LP|Unranked\b|\d+\s*[WL]\b)"
        r"|[·|,;\n]|$)",
        flags=re.I | re.UNICODE,
    )
    for match in pattern.finditer(value):
        game = re.sub(r"\s+", " ", match.group("game")).strip(" ·|:-")
        tag = re.sub(r"\s+", " ", match.group("tag")).strip(" ·|,:-")
        game = re.sub(r"^(?:(?:\d+\s*)?(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|[smhdw])\s+ago\s+)+", "", game, flags=re.I)
        game = re.sub(r"^\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+", "", game, flags=re.I)
        if not game or not tag or len(game) > 64 or len(tag) > 16:
            continue
        if re.search(r"[{}<>;=/\\]|pointer-events|nprogress|javascript|function\s*\(", game + " " + tag, re.I):
            continue
        if game.casefold() in {"solo q accounts", "search summoner", "account", "accounts"}:
            continue
        if not re.fullmatch(r"[\w .’'\-]+", game, flags=re.UNICODE):
            continue
        if not re.fullmatch(r"[\w .\-]+", tag, flags=re.UNICODE):
            continue
        found.append({"gameName": game, "tagLine": tag})
    return found

def _extract_soloq_section(page_text):
    """Return only the SoloQ Accounts portion of a DPM profile, excluding page chrome."""
    text = re.sub(r"\s+", " ", html_unescape(str(page_text or ""))).strip()
    match = re.search(r"Solo\s*Q\s*Accounts?(.*?)(?=Last\s+2\s+Weeks|Recent\s+Games|Champion\s+Stats|$)", text, re.I)
    return match.group(1).strip() if match else ""

def _visible_html_text(source):
    """Extract readable text from SSR HTML without depending on anchor nesting."""
    source = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1\s*>", " ", str(source or ""))
    source = re.sub(r"(?s)<[^>]+>", " ", source)
    source = html_unescape(source)
    return re.sub(r"\s+", " ", source).strip()

def _accounts_from_profile_page(html):
    page = dpm_parse(html)
    # Parse the raw server-rendered text too: some page layouts split account names,
    # tags and rank metadata across nested spans that HTMLParser's text list separates.
    raw_text = _visible_html_text(html)
    parser_text = " ".join(page.text)
    section = _extract_soloq_section(raw_text) or _extract_soloq_section(parser_text)
    candidates = []
    # Parse the dedicated SoloQ section first; this avoids unrelated Riot IDs in page chrome.
    if section:
        candidates.extend(_riot_ids_from_text(section))
    # Some DPM layouts put account IDs in profile links rather than visible section text.
    for link in page.links:
        label = link.get("text", "")
        if "#" in label:
            candidates.extend(_riot_ids_from_text(label))
    # Also parse raw anchor labels. Some pages use nested markup that causes a basic
    # HTMLParser to close an anchor early or merge the following timestamp into its text.
    for anchor in re.findall(r"(?is)<a\b[^>]*>(.*?)</a\s*>", html):
        label = _visible_html_text(anchor)
        if "#" in label:
            candidates.extend(_riot_ids_from_text(label))
    result, seen = [], set()
    for account in candidates:
        game = account["gameName"].strip()
        tag = account["tagLine"].strip()
        key = (game.casefold(), tag.casefold())
        if key not in seen and _is_plausible_riot_id(game, tag):
            seen.add(key)
            result.append({"gameName": game, "tagLine": tag})
    return result

def _is_plausible_riot_id(game, tag):
    if not game or not tag or len(game) > 64 or len(tag) > 16:
        return False
    if re.search(r"[{}<>;=/\\]|pointer-events|nprogress|javascript|function\s*\(", game + " " + tag, re.I):
        return False
    if not re.fullmatch(r"[\w .’'\-]+", game, flags=re.UNICODE):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_.\-\u0080-\uffff]+", tag):
        return False
    return True

def _lolprofile_player_accounts(player_name):
    """Fallback to LoLProfile's public pro account page when DPM has no usable IDs."""
    slug = re.sub(r"[^a-z0-9]+", "-", player_name.casefold()).strip("-")
    if not slug:
        return []
    try:
        response = requests.get(f"https://lolprofile.net/pros/{quote(slug, safe='-')}", timeout=15,
                                headers={"User-Agent": "Mozilla/5.0 Riftmates local analytics"})
        if response.status_code != 200:
            return []
        page = dpm_parse(response.text)
        # LoLProfile account tables usually include rows such as "G2 Hans Sama #12838 EUW".
        text = " ".join(page.text)
        found = _riot_ids_from_text(text)
        result, seen = [], set()
        for item in found:
            game, tag = item["gameName"].strip(), item["tagLine"].strip()
            key = (game.casefold(), tag.casefold())
            if key not in seen and _is_plausible_riot_id(game, tag):
                seen.add(key); result.append({"gameName": game, "tagLine": tag})
        return result
    except requests.RequestException:
        return []

def _player_image_from_html(html):
    """Extract a public profile image when the page publishes one."""
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I)
        if match:
            url = html_unescape(match.group(1)).strip()
            if url.startswith('//'):
                url = 'https:' + url
            if url.startswith('https://'):
                return url
    return ''

def get_dpm_player_accounts(player_slug, player_name=""):
    html = dpm_get(f"/pro/{quote(player_slug, safe='')}")
    result = _accounts_from_profile_page(html)
    image = _player_image_from_html(html)
    if not result and player_name:
        result = _lolprofile_player_accounts(player_name)
    return [{**account, **({'playerImage': image} if image else {})} for account in result]

def load_selected_pro_accounts():
    config = _get_pro_config()
    selected = config.get("teams", [])
    if not selected: return [], []
    accounts=[]; report=[]; seen=set()
    for team in selected:
        slug=str(team.get("slug","") if isinstance(team,dict) else team).strip()
        if not slug: continue
        try:
            players=get_dpm_team_players(slug); team_count=0
            for player in players:
                try:
                    for account in get_dpm_player_accounts(player["slug"], player.get("name", "")):
                        key=(account["gameName"].casefold(),account["tagLine"].casefold())
                        if key not in seen:
                            seen.add(key); accounts.append({**account,"source":f"DPM.LOL · {player['name']} · {slug}"}); team_count+=1
                except requests.RequestException:
                    continue
            report.append({"team":slug,"playersFound":len(players),"accountsFound":team_count})
        except requests.RequestException:
            report.append({"team":slug,"error":"Could not load team from DPM.LOL"})
    return accounts,report


def _save_players(items):
    players = _clean_player_list(items)
    if not players:
        raise ValueError("Add at least one account before saving.")
    _set_user_field("players", players)
    return players

def _save_settings(payload):
    queues = payload.get("queues", {}) if isinstance(payload, dict) else {}
    platform = str(payload.get("platform", DEFAULT_PLATFORM)).upper()
    if platform not in RIOT_PLATFORMS:
        raise ValueError("Unsupported Riot server.")
    default_count = payload.get("defaultGameCount", 100)
    try:
        default_count = int(default_count)
    except (TypeError, ValueError):
        raise ValueError("Default games per player must be a number.")
    if default_count < 25 or default_count > 1000 or default_count % 25:
        raise ValueError("Default games per player must be 25–1000 in steps of 25.")
    cleaned = {"queues": {k: bool(queues.get(k, False)) for k in DEFAULT_SETTINGS["queues"]}, "platform": platform, "defaultGameCount": default_count, "tftQueues": {k: bool((payload.get("tftQueues") or {}).get(k, True)) for k in ("ranked", "doubleup")}}
    if not any(cleaned["tftQueues"].values()):
        raise ValueError("Select at least one TFT queue.")
    if not any(cleaned["queues"].values()):
        raise ValueError("Select at least one queue type.")
    _set_user_field("settings", cleaned)
    return cleaned

HEADERS = {"X-Riot-Token": API_KEY}  # League and shared Riot account endpoints

CACHE_DB = os.path.join(BASE_DIR, "riftmates_cache.sqlite3")

CACHE_LOCK = threading.Lock()

REQUEST_LOCK, LAST_REQUEST = threading.Lock(), 0.0

MIN_REQUEST_INTERVAL = 1.22  # Keep below Riot personal-key limit of 100 requests / 120 seconds.

MAX_RATE_LIMIT_RETRIES = 4

class RiotRateLimitError(RuntimeError):

    pass

def _cache_connection():

    conn = sqlite3.connect(CACHE_DB, timeout=15)

    conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL)")

    return conn

def cached(key, fetch, ttl=86400):

    """Persistent cache. ttl=None means retain until the cache is manually cleared."""

    now = time.time()

    with CACHE_LOCK:

        conn = _cache_connection()

        try:

            row = conn.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()

            if row and (row[1] is None or row[1] > now):

                return json.loads(row[0])

            if row:

                conn.execute("DELETE FROM cache WHERE key=?", (key,))

                conn.commit()

        finally:

            conn.close()

    value = fetch()

    with CACHE_LOCK:

        conn = _cache_connection()

        try:

            expires = None if ttl is None else time.time() + ttl

            conn.execute("INSERT OR REPLACE INTO cache(key, value, expires) VALUES (?, ?, ?)",

                         (key, json.dumps(value, separators=(",", ":")), expires))

            conn.commit()

        finally:

            conn.close()

    return value

ROLE_NAMES = {"TOP":"Top", "JUNGLE":"Jungle", "MIDDLE":"Mid", "BOTTOM":"ADC", "UTILITY":"Support", "SUPPORT":"Support"}

def riot_get(url, params=None, use_tft_key=False):

    """Serialize Riot requests, retry 429s, and honor Riot's Retry-After header."""

    global LAST_REQUEST

    last_error = None

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):

        with REQUEST_LOCK:

            wait = MIN_REQUEST_INTERVAL - (time.time() - LAST_REQUEST)

            if wait > 0:

                time.sleep(wait)

            LAST_REQUEST = time.time()

        try:

            request_headers = {"X-Riot-Token": TFT_API_KEY} if (use_tft_key or "/tft/" in url) else HEADERS
            response = requests.get(url, headers=request_headers, params=params, timeout=30)

        except requests.RequestException as exc:

            last_error = exc

            if attempt >= MAX_RATE_LIMIT_RETRIES:

                raise

            time.sleep(min(2 ** attempt, 15))

            continue

        if response.status_code == 429:

            retry_after = response.headers.get("Retry-After")

            try:

                delay = max(float(retry_after), 1.0) if retry_after else min(10 * (attempt + 1), 60)

            except ValueError:

                delay = min(10 * (attempt + 1), 60)

            if attempt >= MAX_RATE_LIMIT_RETRIES:

                raise RiotRateLimitError(

                    f"Riot API is still rate-limiting after {MAX_RATE_LIMIT_RETRIES} retries. "

                    f"Wait at least {int(delay)} seconds before trying again."

                )

            time.sleep(delay)

            continue

        if response.status_code == 400 and (use_tft_key or "/tft/" in url):
            try:
                reason = response.json().get("status", {}).get("message", "")
            except (ValueError, AttributeError):
                reason = ""
            raise RuntimeError(f"TFT API HTTP 400 ({reason or 'Bad Request'}). Verify the TFT key, account PUUID, and routing region. Endpoint: {url.split('?')[0].rsplit('/', 1)[-1][:24]}...")

        if response.status_code == 401:
            key_name = "RIOT_TFT_API_KEY" if (use_tft_key or "/tft/" in url) else "RIOT_API_KEY"
            raise RuntimeError(f"Riot API HTTP 401: {key_name} is expired or invalid. Update .env and restart Flask.")

        if response.status_code == 403:
            if "/spectator/tft/" in url:
                endpoint = "TFT spectator (live-game lookup)"
            elif "/tft/match/" in url:
                endpoint = "TFT match history"
            elif "/riot/account/" in url:
                endpoint = "Riot account-to-PUUID lookup"
            else:
                endpoint = "Riot API"
            try:
                riot_reason = str(response.json().get("status", {}).get("message", ""))[:180]
            except (ValueError, AttributeError, TypeError):
                riot_reason = ""
            raise RuntimeError(f"Riot API HTTP 403 on {endpoint} ({response.url.split('/')[2]}). "
                               f"{riot_reason or 'Forbidden by Riot for this endpoint/key combination.'} "
                               f"The key may work for other Riot endpoints even when this endpoint denies access.")

        if response.status_code == 404:
            if "/spectator/tft/v5/active-games/" in url:
                return None  # Expected: account is not currently in a TFT game.
            raise RuntimeError("Riot account or match not found. Check the Riot ID and region.")

        if response.status_code >= 500 and attempt < MAX_RATE_LIMIT_RETRIES:

            time.sleep(min(2 ** attempt, 15))

            continue

        response.raise_for_status()

        return response.json()

    if last_error:

        raise last_error

    raise RuntimeError("Riot API request failed after retries.")

# Reuse resolved Riot identities between syncs. The persistent SQLite cache remains
# the source of truth across server restarts; this hot cache avoids disk lookups.
RESOLVED_ACCOUNT_LOCK = threading.Lock()
RESOLVED_ACCOUNTS = {}

def resolve_player(player, region, use_tft_key=False):
    game_name = str(player["gameName"]).strip()
    tag_line = str(player["tagLine"]).strip()
    # Normalize case so capitalization changes do not trigger another API call.
    identity = (("tft:" + __import__("hashlib").sha256(TFT_API_KEY.encode()).hexdigest()[:12]) if use_tft_key else "lol", region.casefold(), game_name.casefold(), tag_line.casefold())
    with RESOLVED_ACCOUNT_LOCK:
        resolved = RESOLVED_ACCOUNTS.get(identity)
    if resolved is not None:
        return {**player, **resolved}

    # Keep the original persistent cache key so existing installations benefit
    # immediately without having to resolve all their accounts again.
    legacy_key = (f"tft:account:{region}:{game_name}#{tag_line}:{__import__('hashlib').sha256(TFT_API_KEY.encode()).hexdigest()[:12]}"
                  if use_tft_key else f"account:{game_name}#{tag_line}")
    def fetch():
        url = (f"https://{region}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
               f"{quote(game_name,safe='')}/{quote(tag_line,safe='')}")
        account = riot_get(url, use_tft_key=use_tft_key)
        return {"puuid": account["puuid"],
                "gameName": account.get("gameName", game_name),
                "tagLine": account.get("tagLine", tag_line)}

    result = cached(legacy_key, fetch, ttl=None)
    identity_data = {"puuid": result["puuid"],
                     "gameName": result.get("gameName", game_name),
                     "tagLine": result.get("tagLine", tag_line)}
    with RESOLVED_ACCOUNT_LOCK:
        RESOLVED_ACCOUNTS[identity] = identity_data
    return {**player, **identity_data}

def resolve_players_fast(players, region, max_workers=8):
    """Resolve accounts concurrently; unchanged Riot IDs hit memory/SQLite."""
    if not players:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(players))) as pool:
        return list(pool.map(lambda player: resolve_player(player, region), players))

def get_match_ids(puuid, queue_id, count=100, region="europe", platform="EUW1"):

    """Fetch up to `count` recent match IDs for one queue, using Riot's 100-ID pages."""

    url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    all_ids = []

    # Riot's Match-V5 endpoint returns at most 100 IDs per request. Fetch
    # additional pages when the user asks for more than 100 games.
    for start in range(0, count, 100):
        page_count = min(100, count - start)
        key = f"matchids:{platform}:{puuid}:queue:{queue_id}:start:{start}:count:{page_count}"
        page = cached(
            key,
            lambda start=start, page_count=page_count: riot_get(
                url, {"start": start, "count": page_count, "queue": queue_id}
            ),
            ttl=900,
        )
        all_ids.extend(page)
        if len(page) < page_count:
            break  # No older matches remain for this account/queue.

    return all_ids[:count]

def get_match(match_id, region):

    def fetch(): return riot_get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{quote(match_id,safe='')}")

    return cached(f"match:{match_id}", fetch, ttl=None)

def role_name(position): return ROLE_NAMES.get(position or "","Unknown")

def analyse_match(match,tracked):

    info=match.get("info",{}); tracked_by_puuid={p["puuid"]:p for p in tracked}; selected=[]

    for participant in info.get("participants",[]):

        player=tracked_by_puuid.get(participant.get("puuid"))

        if not player: continue

        selected.append({"gameName":player["gameName"],"tagLine":player["tagLine"],"puuid":player["puuid"],"champion":participant.get("championName","Unknown"),"championId":participant.get("championId"),"profileIconId":participant.get("profileIcon",29),"win":bool(participant.get("win")),"kills":int(participant.get("kills",0) or 0),"doubleKills":int(participant.get("doubleKills",0) or 0),"tripleKills":int(participant.get("tripleKills",0) or 0),"quadraKills":int(participant.get("quadraKills",0) or 0),"pentaKills":int(participant.get("pentaKills",0) or 0),"deaths":int(participant.get("deaths",0) or 0),"assists":int(participant.get("assists",0) or 0),"cs":int(participant.get("totalMinionsKilled",0) or 0)+int(participant.get("neutralMinionsKilled",0) or 0),"visionScore":int(participant.get("visionScore",0) or 0),"damage":int(participant.get("totalDamageDealtToChampions",0) or 0),"goldEarned":int(participant.get("goldEarned",0) or 0),"dragonKills":int(participant.get("dragonKills",0) or 0),"baronKills":int(participant.get("baronKills",0) or 0),"turretTakedowns":int(participant.get("turretTakedowns",0) or 0),"inhibitorTakedowns":int(participant.get("inhibitorTakedowns",0) or 0),"items":[int(participant.get(f"item{i}",0) or 0) for i in range(6)],"runes":[int(s.get("perk",0)) for style in (participant.get("perks",{}).get("styles",[]) or []) for s in (style.get("selections",[]) or []) if s.get("perk")],"teamId":participant.get("teamId"),"teamPosition":role_name(participant.get("teamPosition"))})

    if not selected: return None

    return {"matchId":match.get("metadata",{}).get("matchId"),"gameCreation":info.get("gameCreation"),"gameDuration":info.get("gameDuration",0),"queueId":info.get("queueId"),"gameMode":info.get("gameMode"),"players":selected,"trackedCount":len(selected)}

def new_record(): return {"games":0,"wins":0}

def add_result(records,key,won): records[key]["games"]+=1; records[key]["wins"]+=int(won)

def build_analytics(matches,players):

    player_by_id={p["puuid"]:p for p in players}

    player_stats={p["puuid"]:{"puuid":p["puuid"],"gameName":p["gameName"],"tagLine":p["tagLine"],"profileIconId":29,"games":0,"wins":0,"kills":0,"deaths":0,"assists":0,"cs":0,"visionScore":0,"csPerMinSum":0.0,"visionPerMinSum":0.0} for p in players}

    duos,teams,champ_pairs,champion_stats,player_champs,role_stats,role_pairs,named_role_pairs=(defaultdict(new_record) for _ in range(8))

    for match in matches:

        selected=match["players"]

        for p in selected:

            s=player_stats[p["puuid"]]

            if s["games"]==0: s["profileIconId"]=p.get("profileIconId",29)

            s["games"]+=1; s["wins"]+=int(p["win"]); s["kills"]+=p["kills"]; s["deaths"]+=p["deaths"]; s["assists"]+=p["assists"]

            s["cs"]+=int(p.get("cs",0)); s["visionScore"]+=int(p.get("visionScore",0))

            minutes=max(float(match.get("gameDuration",0))/60.0,1.0)

            s["csPerMinSum"]+=float(p.get("cs",0))/minutes; s["visionPerMinSum"]+=float(p.get("visionScore",0))/minutes

            add_result(champion_stats,p["champion"],p["win"]); add_result(player_champs,(p["puuid"],p["champion"]),p["win"]); add_result(role_stats,(p["puuid"],p["teamPosition"]),p["win"])

        teams_in_match=defaultdict(list)

        for p in selected: teams_in_match[p["teamId"]].append(p)

        for team in teams_in_match.values():

            team.sort(key=lambda p:p["puuid"]); won=bool(team and team[0]["win"])

            for size in range(2,min(5,len(team))+1):

                for group in combinations(team,size): add_result(teams,tuple(sorted(p["puuid"] for p in group)),won)

            for a,b in combinations(team,2):

                    add_result(duos,tuple(sorted((a["puuid"],b["puuid"]))),won)

                    add_result(champ_pairs,tuple(sorted((a["champion"],b["champion"]))),won)

                    add_result(role_pairs,tuple(sorted((a["teamPosition"],b["teamPosition"]))),won)

                    add_result(named_role_pairs,tuple(sorted([(a["puuid"],a["teamPosition"]),(b["puuid"],b["teamPosition"])])),won)

    def player_label(pid):

        p=player_by_id[pid]; return f"{p['gameName']}#{p['tagLine']}"

    for s in player_stats.values():

        g=s["games"]; s["winRate"]=round(s["wins"]*100/g,1) if g else 0; s["kda"]=round((s["kills"]+s["assists"])/max(s["deaths"],1),2)

        s["avgCS"]=round(s["cs"]/g,1) if g else 0; s["avgVisionScore"]=round(s["visionScore"]/g,1) if g else 0

        s["csPerMin"]=round(s["csPerMinSum"]/g,2) if g else 0; s["visionPerMin"]=round(s["visionPerMinSum"]/g,2) if g else 0

        s.pop("csPerMinSum",None); s.pop("visionPerMinSum",None)

    duo_results=[]

    for (a_id,b_id),v in duos.items():

        a,b=player_stats[a_id],player_stats[b_id]; baseline=(a["winRate"]+b["winRate"])/2; wr=round(v["wins"]*100/v["games"],1)

        duo_results.append({"players":[player_label(a_id),player_label(b_id)],"playerIds":[a_id,b_id],"games":v["games"],"wins":v["wins"],"winRate":wr,"baselineWinRate":round(baseline,1),"deltaPp":round(wr-baseline,1)})

    duo_results.sort(key=lambda x:(x["deltaPp"],x["games"]),reverse=True)

    team_results=[]

    for ids,v in teams.items(): team_results.append({"players":[player_label(i) for i in ids],"playerIds":list(ids),"size":len(ids),"games":v["games"],"wins":v["wins"],"winRate":round(v["wins"]*100/v["games"],1)})

    team_results.sort(key=lambda x:(x["size"],x["winRate"],x["games"]),reverse=True)

    champ_results=[{"champions":[a,b],"games":v["games"],"wins":v["wins"],"winRate":round(v["wins"]*100/v["games"],1)} for (a,b),v in champ_pairs.items()]

    champ_results.sort(key=lambda x:(x["winRate"],x["games"]),reverse=True)

    champ_stat_results=[]

    for champ,v in champion_stats.items():

        champ_stat_results.append({"champion":champ,"games":v["games"],"wins":v["wins"],"winRate":round(v["wins"]*100/v["games"],1),"players":sorted({player_label(p["puuid"]) for m in matches for p in m["players"] if p["champion"]==champ})})

    champ_stat_results.sort(key=lambda x:(x["winRate"],x["games"]),reverse=True)

    role_results=[{"player":player_label(pid),"playerId":pid,"role":role,"games":v["games"],"wins":v["wins"],"winRate":round(v["wins"]*100/v["games"],1)} for (pid,role),v in role_stats.items()]

    role_results.sort(key=lambda x:(x["role"],x["winRate"],x["games"]),reverse=True)

    role_pair_results=[{"roles":list(roles),"games":v["games"],"wins":v["wins"],"winRate":round(v["wins"]*100/v["games"],1)} for roles,v in role_pairs.items()]

    role_pair_results.sort(key=lambda x:(x["winRate"],x["games"]),reverse=True)

    named_role_results=[{"combination":[{"player":player_label(pid),"role":role} for pid,role in ass],"games":v["games"],"wins":v["wins"],"winRate":round(v["wins"]*100/v["games"],1)} for ass,v in named_role_pairs.items()]

    named_role_results.sort(key=lambda x:(x["winRate"],x["games"]),reverse=True)

    for duo in duo_results:

        a_id,b_id=duo["playerIds"]; impacts=[]

        for target_id,other_id in ((a_id,b_id),(b_id,a_id)):

            paired=[]; unpaired=[]

            for match in matches:

                by_id={p["puuid"]:p for p in match["players"]}

                if target_id not in by_id: continue

                target=by_id[target_id]

                if other_id in by_id and by_id[other_id]["teamId"]==target["teamId"]: paired.append(target["win"])

                else: unpaired.append(target["win"])

            if paired and unpaired:

                pr=sum(paired)*100/len(paired); ur=sum(unpaired)*100/len(unpaired)

                impacts.append({"player":player_label(target_id),"withPartnerWinRate":round(pr,1),"withoutPartnerWinRate":round(ur,1),"deltaPp":round(pr-ur,1)})

        duo["partnerImpact"]=impacts

    return {"players":list(player_stats.values()),"duos":duo_results,"teamCombos":team_results,"championCombos":champ_results,"championStats":champ_stat_results,"roleStats":role_results,"rolePairs":role_pair_results,"namedRolePairs":named_role_results}

@app.get("/favicon.svg")
def favicon(): return send_file(os.path.join(BASE_DIR,"favicon.svg"), mimetype="image/svg+xml")

@app.get("/decor/<path:filename>")
def decorative_art(filename):
    """Serve dashboard decorations from the project decor directory."""
    return send_from_directory(os.path.join(BASE_DIR, "decor"), filename)

def _apply_tier_filter_fix(html):
    """Patch tier-list scoring in the served HTML; no frontend file changes needed.

    This is idempotent, so a future frontend containing the fix stays unchanged.
    """
    marker = "function rmApplyTierGrades(rows,playerId,role,champion='all'){"
    weight_helper = """// Normalize role-specific tier weights, including missing/zero settings.
function rmSafeTierWeights(pairs){
 const clean=pairs.map(([key,weight])=>[key,Number.isFinite(Number(weight))?Math.max(0,Number(weight)):0]);
 const total=clean.reduce((sum,[,weight])=>sum+weight,0);
 if(total<=0)return clean.map(([key])=>[key,100/clean.length]);
 return clean.map(([key,weight])=>[key,weight*100/total]);
}
"""
    if 'function rmSafeTierWeights(' not in html:
        if html.count(marker) != 1:
            app.logger.warning("Tier fix not applied: grading function missing or ambiguous")
            return html
        html = html.replace(marker, weight_helper + marker, 1)
    replacements = {
        "const unfiltered=role==='all'&&champion==='all';":
            "const unfiltered=role==='all';",
        "const metrics=PERF_METRICS.map(([key,w])=>[key,rmStatSetting(w,role)]);":
            "const metrics=rmSafeTierWeights(PERF_METRICS.map(([key,w])=>[key,rmStatSetting(w,role)]));",
        "const metrics=[['smoothedWinRate',rmStatSetting('runeWinRateWeight',role)],['games',rmStatSetting('runePopularityWeight',role)]];":
            "const metrics=rmSafeTierWeights([['smoothedWinRate',rmStatSetting('runeWinRateWeight',role)],['games',rmStatSetting('runePopularityWeight',role)]]);",
        "for(const [key,weight] of [['smoothedWinRate',rmStatSetting('runeWinRateWeight',role)],['games',rmStatSetting('runePopularityWeight',role)]]){":
            "for(const [key,weight] of rmSafeTierWeights([['smoothedWinRate',rmStatSetting('runeWinRateWeight',role)],['games',rmStatSetting('runePopularityWeight',role)]])){",
    }
    for old, new in replacements.items():
        if new in html:
            continue
        if html.count(old) != 1:
            app.logger.warning("Tier fix: expected one match for %r, found %d", old[:70], html.count(old))
            continue
        html = html.replace(old, new, 1)
    return html


@app.get("/")

def home():
    # Inject a lightweight navigation tracker without modifying the existing frontend.
    with open(os.path.join(BASE_DIR, "riftmates.html"), encoding="utf-8") as f:
        html = f.read()
    html = _apply_tier_filter_fix(html)
    # Emit an explicit navigation event from Riftmates' own go(id) function.
    # This is more reliable than inferring SPA navigation from DOM mutations.
    nav_marker = "function go(id){"
    if nav_marker in html:
        html = html.replace(nav_marker, "function go(id){window.dispatchEvent(new CustomEvent('riftmates:navigate',{detail:{view:id}}));", 1)
    tag = '<script defer src="/analytics.js?v=3"></script><script defer src="/my_account.js?v=2"></script>'
    if '</body>' in html.lower():
        import re as _re
        html = _re.sub(r'</body\s*>', tag + '</body>', html, count=1, flags=_re.I)
    else:
        html += tag
    return Response(html, mimetype="text/html")


# Optional self-selected account. This is a preference, NOT Riot ownership proof.
def _self_account_db():
    conn = _user_db()
    conn.execute("CREATE TABLE IF NOT EXISTS user_my_account (user_id TEXT PRIMARY KEY, game_name TEXT NOT NULL, tag_line TEXT NOT NULL)")
    return conn

@app.get("/api/my-account")
def get_my_account():
    with closing(_self_account_db()) as conn:
        row = conn.execute("SELECT game_name, tag_line FROM user_my_account WHERE user_id=?", (current_user_id(),)).fetchone()
    if not row:
        return jsonify({"account": None, "verified": False})
    account = next((p for p in _get_user_players() if p["gameName"].casefold()==row["game_name"].casefold() and p["tagLine"].casefold()==row["tag_line"].casefold()), None)
    return jsonify({"account": account, "verified": False})

def _same_origin_preference_request():
    from urllib.parse import urlsplit
    origin = request.headers.get("Origin")
    if origin:
        return urlsplit(origin).netloc == urlsplit(request.host_url).netloc and urlsplit(origin).scheme in ("http", "https")
    referer = request.headers.get("Referer")
    return bool(referer and urlsplit(referer).netloc == urlsplit(request.host_url).netloc and urlsplit(referer).scheme in ("http", "https"))

@app.route("/api/my-account", methods=["PUT", "DELETE"])
def set_my_account():
    if not _same_origin_preference_request():
        return jsonify({"error": "Same-origin request required"}), 403
    uid = current_user_id()
    if request.method == "DELETE":
        with closing(_self_account_db()) as conn, conn:
            conn.execute("DELETE FROM user_my_account WHERE user_id=?", (uid,))
        return jsonify({"account": None, "verified": False})
    payload = request.get_json(silent=True) or {}
    game = str(payload.get("gameName", "")).strip()
    tag = str(payload.get("tagLine", "")).strip()
    account = next((p for p in _get_user_players() if p["gameName"].casefold()==game.casefold() and p["tagLine"].casefold()==tag.casefold()), None)
    if not account:
        return jsonify({"error": "Choose an account from your saved squad"}), 400
    with closing(_self_account_db()) as conn, conn:
        conn.execute("INSERT INTO user_my_account(user_id,game_name,tag_line) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET game_name=excluded.game_name, tag_line=excluded.tag_line", (uid, account["gameName"], account["tagLine"]))
    return jsonify({"account": account, "verified": False})

@app.get("/my_account.js")
def my_account_script():
    return send_from_directory(BASE_DIR, "my_account.js", mimetype="application/javascript")

# TFT first-draft API: uses the same saved Riot IDs and regional routing as League.
TFT_QUEUES = {"ranked": 1100, "doubleup": 1160}

@app.get("/api/tft/sync")
def api_tft_sync():
    analytics_sync("tft", "requested")
    mode = request.args.get("mode", "ranked").lower()
    if mode not in (*TFT_QUEUES, "all"):
        return jsonify({"error": "Select ranked, doubleup or all."}), 400
    try:
        count = int(request.args.get("count", "25"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid match count."}), 400
    if not 1 <= count <= 1000:
        return jsonify({"error": "Choose 1 to 1000 games per account."}), 400
    return _tft_sync_data(mode, count, _get_user_settings(), _get_user_players())

def _tft_participant_augments(part):
    """Read Riot's augment IDs without inventing missing augment data."""
    raw = part.get("augments")
    if raw is None:
        raw = part.get("augment_ids")
    if raw is None:
        raw = part.get("augmentIds")
    if raw is None:
        raw = part.get("augments_data")
    if raw is None:
        raw = part.get("augmentsData")
    if not isinstance(raw, list):
        return []
    result = []
    for augment in raw:
        if isinstance(augment, str) and augment.strip():
            result.append(augment.strip())
        elif isinstance(augment, dict):
            value = augment.get("apiName") or augment.get("api_name") or augment.get("name") or augment.get("id")
            if value:
                result.append(str(value))
    return list(dict.fromkeys(result))


def _tft_sync_data(mode, count, settings, players, progress=None):
    region = routing_for(settings)
    if not players:
        return jsonify({"error": "Add accounts in Your squad first."}), 400
    tracked = {}
    failures = []
    for player in [p for p in players if "tft" in p.get("games", ["lol", "tft"])][:30]:
        try:
            resolved = resolve_player(player, region, use_tft_key=True)
            tracked[resolved["puuid"]] = resolved
        except Exception as exc:
            failures.append(f"{player['gameName']}: account lookup failed: {str(exc)[:180]}")
    if not tracked:
        return jsonify({"error":"No Riot accounts could be resolved. Check your Riot API key, region and account names.","details":failures[:5]}), 502
    if progress: progress(4, "Resolving TFT accounts")
    ids = []
    for puuid in tracked:
        if progress: progress(8, "Finding Ranked and Double Up games")
        try:
            url = f"https://{region}.api.riotgames.com/tft/match/v1/matches/by-puuid/{quote(puuid,safe='')}/ids"
            # The TFT match-ID endpoint cannot filter by Ranked/Double Up queue.
            # Request up to twice the requested sample to allow for other modes.
            # Each API page has a maximum of 100 IDs.
            scan_limit = min(count * 2, 2000)
            seen_for_player = set()
            for start in range(0, scan_limit, 100):
                page_count = min(100, scan_limit - start)
                page = cached(f"tft:ids:{region}:{puuid}:{start}:{page_count}",
                    lambda url=url,start=start,page_count=page_count: riot_get(
                        url, {"start":start,"count":page_count}), ttl=900)
                if not isinstance(page, list):
                    raise ValueError("TFT match ID endpoint returned an invalid response")
                # Guard against a Riot pagination regression returning the same page.
                new_ids = [mid for mid in page if isinstance(mid,str) and mid not in seen_for_player]
                if not new_ids:
                    break
                ids.extend(new_ids)
                seen_for_player.update(new_ids)
                if len(page) < page_count:
                    break
        except Exception as exc:
            failures.append(f"{tracked[puuid]['gameName']}: TFT match list failed: {str(exc)[:180]}")
    if progress: progress(15, f"Fetching {len(set(ids))} unique TFT matches")
    # Fetch each unique match once; cached match details avoid redundant API requests.
    matches=[]
    unique_ids=list(dict.fromkeys(ids))
    total_ids=len(unique_ids)
    for index,match_id in enumerate(unique_ids):
        if progress: progress(15+int(80*index/max(total_ids,1)), f"Reading TFT match {index+1} of {total_ids}")
        try:
            raw=cached(f"tft:match:{match_id}",lambda mid=match_id: riot_get(
                f"https://{region}.api.riotgames.com/tft/match/v1/matches/{quote(mid,safe='')}"),ttl=None)
            info=raw.get("info") or {}
            queue_id=int(info.get("queue_id") or 0)
            queue_name=next((name for name,qid in TFT_QUEUES.items() if qid==queue_id),None)
            if not queue_name or not settings.get("tftQueues", {}).get(queue_name, True) or (mode!="all" and queue_name!=mode): continue
            participants=[]
            for part in info.get("participants",[]):
                puuid=part.get("puuid","")
                participants.append({"puuid":puuid,"name":tracked.get(puuid,{}).get("gameName") or puuid[:10],
                    "tracked":puuid in tracked,"placement":part.get("placement"),
                    "level":part.get("level"),"goldLeft":part.get("gold_left"),
                    "playersEliminated":part.get("players_eliminated"),"totalDamage":part.get("total_damage_to_players"),
                    "traits":[{"name":t.get("name"),"tier":t.get("tier_current"),"units":t.get("num_units")} for t in part.get("traits",[]) if t.get("tier_current",0)>0],
                    "units":[{"name":u.get("character_id"),"tier":u.get("tier"),"items":u.get("itemNames") or []} for u in part.get("units",[])],
                    "partnerGroup":part.get("partner_group_id"),"augments":_tft_participant_augments(part)})
            if not any(p["tracked"] for p in participants):continue
            matches.append({"id":match_id,"queue":queue_name,"timestamp":info.get("game_datetime"),
                "duration":info.get("game_length"),"set":info.get("tft_set_number"),"participants":participants})
        except Exception as exc:
            failures.append(f"Match {match_id[-8:]}: {str(exc)[:160]}")
    if not matches and failures:
        return jsonify({"error": "TFT sync could not retrieve matches. " + failures[0], "details": failures[:8]}), 502
    matches.sort(key=lambda m:m.get("timestamp") or 0,reverse=True)
    # Bound each player's history to the requested sample, not total shared games.
    counts={puuid:0 for puuid in tracked}
    selected=[]
    for match in matches:
        present=[p["puuid"] for p in match["participants"] if p["puuid"] in counts]
        if not any(counts[p]<count for p in present):continue
        selected.append(match)
        for p in present:counts[p]+=1
    stats=[]
    for puuid,account in tracked.items():
        rows=[p for m in selected for p in m["participants"] if p["puuid"]==puuid]
        placements=[int(p["placement"]) for p in rows if isinstance(p["placement"],int)]
        n=len(placements)
        stats.append({"puuid":puuid,"name":account["gameName"],"tagLine":account["tagLine"],
            "games":n,"averagePlacement":round(sum(placements)/n,2) if n else None,
            "top4Rate":round(100*sum(x<=4 for x in placements)/n,1) if n else None,
            "firstRate":round(100*sum(x==1 for x in placements)/n,1) if n else None,
            "averageLevel":round(sum((p.get("level") or 0) for p in rows)/len(rows),1) if rows else None})
    # TFT ladder entries and summoner profile use TFT credentials, never League credentials.
    for stat in stats:
        puuid=stat["puuid"]
        try:
            platform = str(settings.get("riotPlatform") or settings.get("platform") or "euw1").lower()
            if platform not in ("euw1","eun1","na1","br1","kr","jp1","oc1","tr1","ru","la1","la2","ph2","sg2","th2","tw2","vn2"):
                platform="euw1"
            base=f"https://{platform}.api.riotgames.com"
            rank_entries=riot_get(f"{base}/tft/league/v1/by-puuid/{quote(puuid,safe='')}", use_tft_key=True)
            stat["ranks"]={e.get("queueType"): {"tier":e.get("tier"),"rank":e.get("rank"),"leaguePoints":e.get("leaguePoints"),"wins":e.get("wins"),"losses":e.get("losses")} for e in rank_entries if isinstance(e,dict)}
        except Exception as exc:
            stat["ranks"]={}
            failures.append(f"{stat['name']}: rank lookup unavailable: {str(exc)[:90]}")
        try:
            profile=riot_get(f"{base}/tft/summoner/v1/summoners/by-puuid/{quote(puuid,safe='')}", use_tft_key=True)
            stat["profileIconId"]=profile.get("profileIconId")
        except Exception:
            stat["profileIconId"]=None
    if progress: progress(100, "TFT sync complete")
    return jsonify({"mode":mode,"queueId":TFT_QUEUES.get(mode),"players":stats,"matches":selected,
        "errors":failures[:12],"sampleCount":count,
        "notes":"Double Up placements are team placements; top-four rate is not a Double Up win rate."})

# Cloudflare/proxies may close long requests with HTTP 502/524. Execute TFT
# downloads in a worker and let the browser poll short JSON requests instead.
_TFT_JOBS = {}
_TFT_JOBS_LOCK = threading.Lock()

def _tft_snapshot_save(uid, payload):
    # Store the most recent successful TFT sample independently from the
    # expiring Riot response cache so reloading the page requires no API calls.
    with _user_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tft_last_sync (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL, saved_at REAL NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO tft_last_sync (user_id,payload,saved_at) VALUES (?,?,?)", (uid, json.dumps(payload), time.time()))

@app.get("/api/tft/live")
def api_tft_live():
    """Independent spectator lookup; never starts match-history sync."""
    settings = _get_user_settings()
    region = routing_for(settings)
    platform = settings.get("platform", "EUW1").lower()
    players = [p for p in _get_user_players() if "tft" in p.get("games", ["lol", "tft"])][:30]
    results, errors, game_by_id = [], [], {}
    for player in players:
        name = str(player.get("gameName", "Unknown"))
        try:
            account = resolve_player(player, region, use_tft_key=True)
            puuid = account["puuid"]
            try:
                game = riot_get(
                    f"https://{platform}.api.riotgames.com/lol/spectator/tft/v5/active-games/by-puuid/{quote(puuid,safe='')}",
                    use_tft_key=True)
            except Exception as exc:
                # Only 404 is an expected offline status; other failures are surfaced.
                if "404" in str(exc) and "429" not in str(exc):
                    results.append({"name": name, "puuid": puuid, "inGame": False})
                    continue
                raise
            if not isinstance(game, dict) or not game.get("gameId"):
                results.append({"name": name, "puuid": puuid, "inGame": False})
                continue
            gid = str(game["gameId"])
            game_by_id.setdefault(gid, game)
            results.append({"name": name, "puuid": puuid, "inGame": True,
                            "gameId": gid, "gameMode": game.get("gameMode"),
                            "gameQueueConfigId": game.get("gameQueueConfigId"),
                            "gameStartTime": game.get("gameStartTime"),
                            "gameLength": game.get("gameLength")})
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:160]}")
            results.append({"name": name, "inGame": None, "error": True})
    games = []
    for gid, game in game_by_id.items():
        members = [p for p in results if p.get("gameId") == gid]
        games.append({"gameId": gid, "gameMode": game.get("gameMode"),
                      "gameQueueConfigId": game.get("gameQueueConfigId"),
                      "gameStartTime": game.get("gameStartTime"),
                      "gameLength": game.get("gameLength"),
                      "trackedPlayers": [{"name": p["name"], "puuid": p["puuid"]} for p in members],
                      "lobbySize": len(game.get("participants") or [])})
    return jsonify({"players": results, "games": games, "errors": errors, "checked": len(players),
                    "checkedAt": int(time.time() * 1000)})

@app.get("/api/tft/cache")
def api_tft_cached():
    with _user_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tft_last_sync (user_id TEXT PRIMARY KEY, payload TEXT NOT NULL, saved_at REAL NOT NULL)")
        row=conn.execute("SELECT payload,saved_at FROM tft_last_sync WHERE user_id=?", (current_user_id(),)).fetchone()
    if not row:
        return jsonify({"available":False})
    result=json.loads(row["payload"])
    result["available"]=True
    result["fromCache"]=True
    result["cachedAt"]=row["saved_at"]
    return jsonify(result)

@app.post("/api/tft/sync/start")
def api_tft_sync_start():
    analytics_sync("tft", "requested")
    import uuid
    mode = str(request.args.get("mode", "all")).lower()
    if mode not in (*TFT_QUEUES, "all"):
        return jsonify({"error":"Invalid TFT queue"}), 400
    try: count = int(request.args.get("count", "100"))
    except (TypeError, ValueError): return jsonify({"error":"Invalid count"}), 400
    if not 1 <= count <= 1000: return jsonify({"error":"Count must be 1–1000"}), 400
    settings = _get_user_settings()
    players = _get_user_players()
    uid = current_user_id()
    if not players: return jsonify({"error":"No accounts configured"}), 400
    job_id = uuid.uuid4().hex
    owner = session.get("tft_job_owner")
    if not owner:
        owner = uuid.uuid4().hex
        session["tft_job_owner"] = owner
    with _TFT_JOBS_LOCK:
        # Discard old completed jobs to limit memory use.
        for key, value in list(_TFT_JOBS.items()):
            if time.time() - value["created"] > 3600:
                del _TFT_JOBS[key]
        _TFT_JOBS[job_id] = {"owner":owner,"created":time.time(),"status":"running","percent":1,"message":"Starting TFT sync","result":None,"error":None}
    def run():
        def progress(percent,message):
            with _TFT_JOBS_LOCK:
                job = _TFT_JOBS.get(job_id)
                if job: job.update(percent=percent,message=message)
        try:
            with app.app_context():
                response = _tft_sync_data(mode,count,settings,players,progress)
                if isinstance(response,tuple):
                    payload=response[0].get_json()
                    raise RuntimeError(payload.get("error", "TFT API error"))
                result=response.get_json()
                _tft_snapshot_save(uid,result)
            analytics_sync("tft", "completed")
            with _TFT_JOBS_LOCK:
                _TFT_JOBS[job_id].update(status="complete",percent=100,message="Done",result=result)
        except Exception as exc:
            analytics_sync("tft", "failed")
            app.logger.exception("TFT background sync failed")
            with _TFT_JOBS_LOCK:
                _TFT_JOBS[job_id].update(status="failed",error=str(exc)[:500],message="Sync failed")
    threading.Thread(target=run,daemon=True,name="tft-sync-"+job_id[:8]).start()
    return jsonify({"jobId":job_id,"status":"running"}), 202

@app.get("/api/tft/sync/status/<job_id>")
def api_tft_sync_status(job_id):
    with _TFT_JOBS_LOCK:
        job = _TFT_JOBS.get(job_id)
        if not job or job["owner"] != session.get("tft_job_owner"):
            return jsonify({"error":"TFT sync job not found or expired"}), 404
        return jsonify({k:v for k,v in job.items() if k not in ("owner","created")})

@app.get("/api/tft/access-check")
def api_tft_access_check():
    """Safe endpoint-specific access probe; never exposes the API token."""
    settings = _get_user_settings()
    region = routing_for(settings)
    players = _get_user_players()
    if not players:
        return jsonify({"error": "Configure at least one Riot account first."}), 400
    try:
        player = resolve_player(players[0], region, use_tft_key=True)
    except Exception as exc:
        return jsonify({"stage":"account_resolution", "error":str(exc)}), 502
    try:
        url = f"https://{region}.api.riotgames.com/tft/match/v1/matches/by-puuid/{quote(player['puuid'],safe='')}/ids"
        ids = riot_get(url, {"count": 1})
        return jsonify({"ok":True,"stage":"tft_match_list","region":region,"matchIdsReturned":len(ids)})
    except Exception as exc:
        return jsonify({"ok":False,"stage":"tft_match_list","region":region,"error":str(exc)}), 502

@app.get("/api/health")

def health(): return jsonify({"ok":True,"service":"Riftmates"})

@app.get("/api/accounts")
def get_accounts():
    return jsonify({"players": _get_user_players()})

@app.put("/api/accounts")
def update_accounts():
    payload = request.get_json(silent=True) or {}
    items = payload.get("players", [])
    if not isinstance(items, list):
        return jsonify({"error": "players must be a list."}), 400
    for item in items:
        if not isinstance(item, dict) or not str(item.get("gameName", "")).strip() or not str(item.get("tagLine", "")).strip():
            return jsonify({"error": "Every account needs both a game name and tag line."}), 400
    try:
        players = _save_players(items)
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Could not save accounts: {exc}"}), 400
    return jsonify({"players": players})

@app.post("/api/accounts/restore-defaults")
def restore_default_accounts():
    """Restore the nine starter accounts for this browser profile only."""
    try:
        players = _save_players(DEFAULT_PLAYERS)
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Could not restore default accounts: {exc}"}), 400
    return jsonify({"players": players})

@app.get("/api/settings")
def get_settings():
    return jsonify(_get_user_settings())

@app.put("/api/settings")
def update_settings():
    payload = request.get_json(silent=True) or {}
    try:
        settings = _save_settings(payload)
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Could not save settings: {exc}"}), 400
    return jsonify(settings)

# Public Vrayd champion-page snapshots are fetched on demand and cached locally.
# This is deliberately not a bulk crawler: one page is requested only when the
# user selects a champion/role, and rank-specific data is not inferred.
_VRAYD_BENCH_CACHE = {}
_VRAYD_BENCH_LOCK = threading.Lock()
_VRAYD_BENCH_TTL = 6 * 60 * 60

class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        text = " ".join(data.split())
        if text:
            self.parts.append(text)
    def get_text(self):
        return " ".join(self.parts)

@app.get("/api/benchmark/public")
def public_champion_benchmark():
    """Fetch one publicly visible Vrayd champion page for an on-demand comparison."""
    champion = str(request.args.get("champion", "")).strip()
    role = str(request.args.get("role", "all")).strip().lower()
    rank = str(request.args.get("rank", "ALL")).strip().upper()
    if not champion or len(champion) > 64:
        return jsonify({"error": "A champion name is required."}), 400
    role_paths = {"top": "top", "jungle": "jungle", "middle": "mid", "bottom": "adc", "utility": "support", "all": ""}
    if role not in role_paths:
        return jsonify({"error": "Unsupported role."}), 400
    if rank != "ALL":
        return jsonify({"available": False, "reason": "Vrayd's public champion pages currently provide a verified all-ranks snapshot only through this integration. Rank-specific values are not substituted."})
    slug = re.sub(r"[^a-z0-9]+", "-", champion.lower()).strip("-")
    # The public champion route is case-insensitive in its displayed name, and
    # role routes are explicit. Keep the URL restricted to a slug we construct.
    path = (f"https://vrayd.com/champion/{slug}/" if role_paths[role] else f"https://vrayd.com/champions/{slug}/")
    if role_paths[role]:
        path += role_paths[role] + "/"
    cache_key = (slug, role, rank)
    now = time.time()
    with _VRAYD_BENCH_LOCK:
        cached = _VRAYD_BENCH_CACHE.get(cache_key)
        if cached and now - cached["at"] < _VRAYD_BENCH_TTL:
            return jsonify(cached["data"])
    try:
        response = requests.get(path, timeout=12, headers={"User-Agent": "Riftmates personal stats comparison/1.0"})
        if response.status_code == 404:
            return jsonify({"available": False, "reason": "No matching public champion page was found.", "sourceUrl": path})
        response.raise_for_status()
        parser = _VisibleText()
        parser.feed(response.text)
        visible = parser.get_text()
        # Header metrics: role/rank/patch, win rate, KDA, game count.
        win_match = re.search(r"(\d{1,2}(?:\.\d+)?)%\s*win rate", visible, re.I)
        kda_match = re.search(r"(\d+(?:\.\d+)?)\s*KDA", visible, re.I)
        games_match = re.search(r"([\d,]+)\s*(?:games|matches)", visible, re.I)
        cs_match = re.search(r"CS\s*/\s*min\s*(\d+(?:\.\d+)?)", visible, re.I)
        gold_match = re.search(r"Gold\s*/\s*min\s*(\d+(?:\.\d+)?)", visible, re.I)
        damage_match = re.search(r"Damage to champs\s*([\d,]+)", visible, re.I)
        kp_match = re.search(r"Kill participation\s*(\d+(?:\.\d+)?)%", visible, re.I)
        vision_match = re.search(r"Vision score\s*(\d+(?:\.\d+)?)", visible, re.I)
        if not win_match and not kda_match and not cs_match:
            return jsonify({"available": False, "reason": "The public page did not expose a parseable champion summary.", "sourceUrl": path})
        data = {
            "available": True, "source": "Vrayd public champion page", "sourceUrl": response.url,
            "champion": champion, "role": role, "rank": "ALL",
            "winRate": float(win_match.group(1)) if win_match else None,
            "kda": float(kda_match.group(1)) if kda_match else None,
            "games": int(games_match.group(1).replace(",", "")) if games_match else None,
            "csPerMin": float(cs_match.group(1)) if cs_match else None,
            "goldPerMin": float(gold_match.group(1)) if gold_match else None,
            "damagePerGame": int(damage_match.group(1).replace(",", "")) if damage_match else None,
            "killParticipation": float(kp_match.group(1)) if kp_match else None,
            "visionPerGame": float(vision_match.group(1)) if vision_match else None,
            "note": "Public all-ranks champion summary; queue/region/patch coverage follows the source page. This is not a rank-matched comparison."
        }
        with _VRAYD_BENCH_LOCK:
            _VRAYD_BENCH_CACHE[cache_key] = {"at": now, "data": data}
        return jsonify(data)
    except requests.RequestException as exc:
        return jsonify({"available": False, "reason": f"Could not reach the public benchmark source: {exc}", "sourceUrl": path}), 502

@app.get("/api/pro-teams")
def api_pro_teams():
    try:
        teams=get_dpm_teams()
        selected = _get_pro_config().get("teams", [])
        selected_slugs={x.get("slug") if isinstance(x,dict) else x for x in selected}
        return jsonify({"teams":teams,"selected":[t for t in teams if t["slug"] in selected_slugs]})
    except requests.RequestException as exc:
        return jsonify({"error":f"Could not load DPM.LOL teams: {exc}"}),502

@app.put("/api/pro-teams")
def save_pro_teams():
    payload=request.get_json(silent=True) or {}; teams=payload.get("teams",[])
    if not isinstance(teams,list): return jsonify({"error":"teams must be a list"}),400
    clean=[]; seen=set()
    for t in teams:
        if isinstance(t,dict): slug=str(t.get("slug","")).strip(); name=str(t.get("name",slug)).strip()
        else: slug=str(t).strip(); name=slug
        if slug and re.fullmatch(r"[A-Za-z0-9_-]{1,80}",slug) and slug.casefold() not in seen:
            seen.add(slug.casefold()); clean.append({"slug":slug,"name":name})
    _set_user_field("pro_config", {"teams": clean})
    return jsonify({"teams":clean})

def _dpm_team_soloq_accounts(slug, roster_players=None):
    """Read the team's published SoloQ account page, e.g. /esport/soloq/teams/GX.

    This page is distinct from /pro/teams/<slug>: the latter is a leaderboard,
    while the former is intended to list pro players' account IDs. No IDs are
    synthesized when the source does not publish them.
    """
    roster_players = roster_players or []
    html = dpm_get(f"/esport/soloq/teams/{quote(slug, safe='')}")
    page = dpm_parse(html)
    text = _visible_html_text(html)
    # The account page is a list, so parse the actual published Riot IDs from
    # the rendered text and any linked account labels, then de-duplicate.
    candidates = _riot_ids_from_text(text)
    for link in page.links:
        if "#" in link.get("text", ""):
            candidates.extend(_riot_ids_from_text(link["text"]))

    result, seen = [], set()
    for item in candidates:
        raw_game = item["gameName"].strip()
        tag = item["tagLine"].strip()
        game = raw_game
        player_name = "Unassigned (DPM team SoloQ page)"
        # Some team account rows render as "Player Name - Riot GameName#TAG".
        # Strip that player label only when it exactly matches a known roster name.
        for player in roster_players:
            pname = str(player.get("name", "")).strip()
            if pname and re.match(r"^" + re.escape(pname) + r"\s*[-–:|]\s*", game, re.I):
                game = re.sub(r"^" + re.escape(pname) + r"\s*[-–:|]\s*", "", game, count=1, flags=re.I).strip()
                player_name = pname
                break
        if not _is_plausible_riot_id(game, tag):
            continue
        key = (game.casefold(), tag.casefold())
        if key in seen:
            continue
        seen.add(key)
        # Otherwise associate only when the source text puts a roster name close
        # to the account. If ambiguous, leave it explicitly unassigned.
        lower_text = text.casefold()
        account_pos = lower_text.find(f"{raw_game}#{tag}".casefold())
        if account_pos < 0:
            account_pos = lower_text.find(raw_game.casefold())
        if player_name.startswith("Unassigned"):
            for player in roster_players:
                pname = str(player.get("name", "")).strip()
                if not pname:
                    continue
                ppos = lower_text.rfind(pname.casefold(), 0, max(0, account_pos) + 1)
                if ppos >= 0 and account_pos >= ppos and account_pos - ppos < 180:
                    player_name = pname
                    break
        result.append({"gameName": game, "tagLine": tag, "player": player_name,
                       "source": f"DPM.LOL · team SoloQ page ({slug})"})
    return result


def _discover_team_accounts(slug, name):
    """Use DPM's dedicated team SoloQ page first, with player profiles as backup.

    Team leaderboard pages are useful for roster discovery but are not the same
    thing as DPM's /esport/soloq/teams/<slug> account directory. Never invent
    account IDs as a fallback.
    """
    players, accounts, seen, player_report = [], [], set(), []
    roster_sources = []
    # Resolve to the actual DPM team slug where possible (e.g. GX, not GIANTX).
    try:
        directory = get_dpm_teams()
        matched = next((t for t in directory if t["slug"].casefold() == slug.casefold()
                        or t["name"].casefold() == name.casefold()), None)
        if matched:
            slug = matched["slug"]
    except (requests.RequestException, ValueError):
        pass

    try:
        dpm_players = get_dpm_team_players(slug)
        if dpm_players:
            roster_sources.append(("DPM.LOL", dpm_players))
    except (requests.RequestException, ValueError):
        pass
    try:
        rv_players = _riftvanta_team_players(name, slug)
        if rv_players:
            roster_sources.append(("RIFTVANTA", rv_players))
    except (requests.RequestException, ValueError):
        pass

    merged = {}
    for roster_source, roster in roster_sources:
        for player in roster:
            key = re.sub(r"[^a-z0-9]", "", player.get("name", player["slug"]).casefold())
            if not key:
                continue
            if key not in merged:
                merged[key] = {"name": player.get("name", player["slug"]), "profiles": []}
            merged[key]["profiles"].append((roster_source, player["slug"]))
    players = [{"slug": data["profiles"][0][1], "name": data["name"], "profiles": data["profiles"]}
               for data in merged.values()]

    # Primary: DPM's dedicated team SoloQ page. It is designed to aggregate
    # public account listings and avoids relying on every player's profile page.
    try:
        for account in _dpm_team_soloq_accounts(slug, players):
            game, tag = account["gameName"].strip(), account["tagLine"].strip()
            key = (game.casefold(), tag.casefold())
            if _is_plausible_riot_id(game, tag) and key not in seen:
                seen.add(key)
                accounts.append(account)
    except requests.RequestException as exc:
        player_report.append({"source": "DPM team SoloQ page", "error": str(exc)[:240]})

    # Secondary: per-player profile sections from DPM and RIFTVANTA.
    player_images = {}
    for player in players:
        player_count = 0
        source_used = "No public account IDs found"
        for profile_source, profile_slug in player["profiles"]:
            try:
                if profile_source == "RIFTVANTA":
                    found = _riftvanta_player_accounts(profile_slug)
                    if not found:
                        found = _lolprofile_player_accounts(player["name"])
                else:
                    found = get_dpm_player_accounts(profile_slug, player["name"])
                for account in found:
                    if account.get("playerImage"):
                        player_images[player["name"].casefold()] = account["playerImage"]
                    game, tag = account["gameName"].strip(), account["tagLine"].strip()
                    if not _is_plausible_riot_id(game, tag):
                        continue
                    key = (game.casefold(), tag.casefold())
                    if key not in seen:
                        seen.add(key)
                        accounts.append({"gameName": game, "tagLine": tag, "player": player["name"],
                                         **({"playerImage": account["playerImage"]} if account.get("playerImage") else {}),
                                         "source": f"{profile_source} · {player['name']} profile"})
                        player_count += 1
                        source_used = profile_source
            except requests.RequestException as exc:
                player_report.append({"player": player["name"], "accountsFound": 0,
                                      "source": profile_source, "error": str(exc)[:180]})
                continue
        player_report.append({"player": player["name"], "accountsFound": player_count, "source": source_used})

    # Do not ship the old hard-coded G2 IDs. If the sources do not publish an
    # account, show that as missing and let the user add/correct it manually.
    for account in accounts:
        player_name = str(account.get("player", "")).casefold()
        if not account.get("playerImage") and player_name in player_images:
            account["playerImage"] = player_images[player_name]

    if not players and not accounts:
        raise requests.RequestException("No roster or SoloQ accounts could be read from DPM.LOL or RIFTVANTA")
    return slug, players, accounts, player_report

@app.post("/api/pro-teams/import")
def import_pro_team():
    """Discover accounts and return a preview. Does not replace the squad until confirmed."""
    payload = request.get_json(silent=True) or {}
    slug = str(payload.get("slug", "")).strip()
    name = str(payload.get("name", slug)).strip() or slug
    if not slug or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", slug):
        return jsonify({"error": "Choose a valid professional team."}), 400
    try:
        slug, players, accounts, player_report = _discover_team_accounts(slug, name)
        if not accounts:
            return jsonify({"error": f"No valid SoloQ accounts were found for {name}. DPM.LOL and RIFTVANTA did not expose usable account IDs; your current squad was not changed.", "playersFound": len(players), "playerReport": player_report}), 502
        return jsonify({"preview": True, "team": {"slug": slug, "name": name}, "players": accounts,
                        "playersFound": len(players), "accountsFound": len(accounts), "playerReport": player_report, "sourcesTried": ["DPM.LOL team SoloQ page", "DPM.LOL player profiles", "RIFTVANTA player profiles", "LoLProfile"]})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not read team/player data: {exc}. Your current squad was not changed."}), 502

@app.post("/api/pro-teams/confirm")
def confirm_pro_team_import():
    """Save only explicitly previewed, valid accounts after the user confirms."""
    payload = request.get_json(silent=True) or {}
    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    name = str(team.get("name", "Professional team")).strip()
    slug = str(team.get("slug", "")).strip()
    items = payload.get("players", [])
    if not isinstance(items, list):
        return jsonify({"error": "players must be a list"}), 400
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        game, tag = str(item.get("gameName", "")).strip(), str(item.get("tagLine", "")).strip()
        if _is_plausible_riot_id(game, tag):
            clean.append({"gameName": game, "tagLine": tag})
    clean = _clean_player_list(clean)
    if not clean:
        return jsonify({"error": "No valid Riot IDs selected. Your current squad was not changed."}), 400
    try:
        saved = _save_players(clean)
        # The confirmed squad is the exact account list saved above. Do not
        # leave this team in the auto-discovery list: sync() also loads every
        # account from selected teams, which would silently re-add unchecked
        # accounts belonging to the same pro players.
        _set_user_field("pro_config", {"teams": [], "activeTeam": {"slug": slug, "name": name}, "selectedAccounts": clean})
        return jsonify({"team": {"slug": slug, "name": name}, "players": saved, "accountsFound": len(saved)})
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Could not activate this team: {exc}"}), 500

def _cached_analytics_for_user(uid, count):
    """Rebuild a dashboard from locally cached Riot responses; NEVER call Riot."""
    players = _get_user_players(uid)
    settings = _get_user_settings(uid)
    region = routing_for(settings)
    queues = set()
    if settings["queues"].get("normals"):
        queues.update((400, 430, 490))
    if settings["queues"].get("flex"):
        queues.add(440)
    if settings["queues"].get("rankedSolo"):
        queues.add(420)
    if not players or not queues or not os.path.exists(CACHE_DB):
        return None

    # Resolve Riot IDs exclusively from the existing persistent cache.
    resolved = []
    with sqlite3.connect(CACHE_DB, timeout=30) as conn:
        for player in players:
            key = f"account:{player['gameName']}#{player['tagLine']}"
            row = conn.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
            if row is None:
                # Riot IDs may have changed capitalization between syncs.
                row = conn.execute("SELECT value FROM cache WHERE lower(key)=lower(?) LIMIT 1", (key,)).fetchone()
            if row:
                try:
                    item = json.loads(row[0])
                    if item.get("puuid"):
                        resolved.append(item)
                except (ValueError, TypeError, AttributeError):
                    continue
        if not resolved:
            return None

        wanted = {p["puuid"] for p in resolved}
        # Match payloads are globally cached, including games downloaded by other visitors.
        # Iterate in batches so SQLite doesn't have to load the entire cache into RAM.
        matches = []
        cursor = conn.execute("SELECT value FROM cache WHERE key LIKE 'match:%'")
        while True:
            rows = cursor.fetchmany(200)
            if not rows:
                break
            for (raw,) in rows:
                try:
                    match = json.loads(raw)
                    if not str(match.get("metadata", {}).get("matchId", "")).startswith(settings["platform"] + "_"): continue
                    info = match.get("info", {})
                    if info.get("queueId") not in queues:
                        continue
                    if not any(p.get("puuid") in wanted for p in info.get("participants", [])):
                        continue
                    analysed = analyse_match(match, resolved)
                    if analysed:
                        matches.append(analysed)
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue

    if not matches:
        return None
    matches.sort(key=lambda m: m.get("gameCreation") or 0, reverse=True)
    counts = {p["puuid"]: 0 for p in resolved}
    selected = []
    for match in matches:
        if not any(counts.get(p["puuid"], count) < count for p in match["players"]):
            continue
        selected.append(match)
        for p in match["players"]:
            if p["puuid"] in counts and counts[p["puuid"]] < count:
                counts[p["puuid"]] += 1
        if all(n >= count for n in counts.values()):
            break

    return {
        "updatedAt": int(time.time()), "region": region,
        "requestedPerPlayer": count, "queueIds": sorted(queues), "settings": settings,
        "players": [{"gameName": p["gameName"], "tagLine": p["tagLine"]} for p in resolved],
        "proTeams": [], "proAccountCount": 0, "matches": selected,
        "analytics": build_analytics(selected, resolved),
        "playerGameCounts": counts, "fromCache": True,
    }


@app.get("/api/cache/analytics")
def api_cached_analytics():
    """Load locally cached stats immediately, without making Riot API requests."""
    try:
        count = min(max(int(request.args.get("count", "100")), 1), 1000)
        result = _cached_analytics_for_user(current_user_id(), count)
        if result is None:
            return jsonify({"available": False, "reason": "No cached matches for these accounts and queues yet."})
        return jsonify(result)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid count parameter"}), 400
    except sqlite3.Error as exc:
        app.logger.exception("Could not read Riot match cache")
        return jsonify({"error": f"Could not read match cache: {exc}"}), 500



# Ranked snapshots are observations made during syncs; Riot does not expose historical LP timelines.
def _ranked_db():
    conn=sqlite3.connect(USER_DB, timeout=30)
    conn.execute("""CREATE TABLE IF NOT EXISTS ranked_snapshots (
        user_id TEXT NOT NULL, platform TEXT NOT NULL, puuid TEXT NOT NULL,
        queue TEXT NOT NULL, observed_at INTEGER NOT NULL, tier TEXT NOT NULL,
        division TEXT NOT NULL, lp INTEGER NOT NULL, wins INTEGER NOT NULL,
        losses INTEGER NOT NULL, PRIMARY KEY(user_id,platform,puuid,queue,observed_at))""")
    return conn

def _ranked_read(uid, platform):
    with closing(_ranked_db()) as conn:
        rows=conn.execute("""SELECT puuid,queue,observed_at,tier,division,lp,wins,losses
          FROM ranked_snapshots WHERE user_id=? AND platform=? ORDER BY observed_at""",(uid,platform)).fetchall()
    return [dict(zip(("puuid","queue","at","tier","division","lp","wins","losses"),r)) for r in rows]

def _ranked_fetch(uid, resolved, platform):
    result={}
    now=int(time.time())
    for player in resolved:
        puuid=player["puuid"]
        queues={"RANKED_SOLO_5x5":None,"RANKED_FLEX_SR":None}
        try:
            url=f"https://{platform.lower()}.api.riotgames.com/lol/league/v4/entries/by-puuid/{quote(puuid,safe='')}"
            for entry in riot_get(url):
                if entry.get("queueType") in queues:
                    queues[entry["queueType"]]={"tier":entry.get("tier","UNRANKED"),"division":entry.get("rank",""),"lp":int(entry.get("leaguePoints",0)),"wins":int(entry.get("wins",0)),"losses":int(entry.get("losses",0))}
        except Exception as exc:
            app.logger.warning("Rank lookup failed for %s on %s: %s",puuid,platform,exc)
            continue
        result[puuid]=queues
    with closing(_ranked_db()) as conn,conn:
        for puuid,queues in result.items():
            for queue,rank in queues.items():
                if rank is None: continue
                conn.execute("""INSERT OR REPLACE INTO ranked_snapshots
                (user_id,platform,puuid,queue,observed_at,tier,division,lp,wins,losses)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",(uid,platform,puuid,queue,now,rank["tier"],rank["division"],rank["lp"],rank["wins"],rank["losses"]))
    return result

@app.get("/api/ranked")
def ranked_data():
    uid=current_user_id()
    platform=_get_user_settings(uid)["platform"]
    return jsonify({"platform":platform,"history":_ranked_read(uid,platform)})

@app.get("/api/live-games")
def api_live_games():
    """Spectator snapshot for configured accounts, grouped by live match."""
    uid = current_user_id()
    settings = _get_user_settings(uid)
    platform = settings["platform"].lower()
    region = routing_for(settings)
    configured = _get_user_players(uid)
    if not configured:
        return jsonify({"games": [], "checked": 0, "errors": [], "checkedAt": int(time.time())})
    resolved, errors = [], []
    def resolve_one(account):
        try:
            return resolve_player(account, region), None
        except Exception as exc:
            return None, f"Could not resolve {account['gameName']}: {str(exc)[:110]}"
    with ThreadPoolExecutor(max_workers=5) as pool:
        for player, error in pool.map(resolve_one, configured):
            if player: resolved.append(player)
            if error: errors.append(error)
    def check_one(player):
        url = f"https://{platform}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{quote(player['puuid'],safe='')}"
        try:
            # Spectator 404 means not in game; do not cache negative results.
            response = requests.get(url, headers=HEADERS, timeout=12)
            if response.status_code == 404: return None, None
            if response.status_code == 429: return None, "Riot rate limit reached; retry shortly."
            response.raise_for_status()
            return response.json(), None
        except requests.RequestException as exc:
            return None, f"Spectator lookup failed for {player['gameName']}: {str(exc)[:100]}"
    found = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (raw, error) in pool.map(check_one, resolved):
            if error: errors.append(error)
            if raw and raw.get('gameId'):
                found[str(raw['gameId'])] = raw
    tracked = {p['puuid']: p for p in resolved}
    games = []
    for raw in found.values():
        participants = []
        for member in raw.get('participants', []):
            puuid = member.get('puuid', '')
            owner = tracked.get(puuid)
            if not owner:
                spectator_name = (member.get('riotId') or member.get('summonerName') or '').casefold().replace(' ', '')
                for known in resolved:
                    full_id = (known.get('gameName', '') + '#' + known.get('tagLine', '')).casefold().replace(' ', '')
                    if spectator_name and spectator_name == full_id:
                        owner = known
                        break
            participants.append({
                'puuid': puuid if owner else '',
                'teamId': member.get('teamId'),
                'championId': member.get('championId'),
                'spell1Id': member.get('spell1Id'), 'spell2Id': member.get('spell2Id'),
                'gameName': (owner or {}).get('gameName') or member.get('riotId') or member.get('summonerName') or 'Unknown player',
                'tagLine': (owner or {}).get('tagLine', ''),
                'tracked': bool(owner),
                'profileIconId': member.get('profileIconId', 0),
            })
        tracked_count = sum(1 for m in participants if m['tracked'])
        if tracked_count < 1: continue
        games.append({'gameId': str(raw['gameId']), 'queueId': raw.get('gameQueueConfigId'),
                      'gameMode': raw.get('gameMode'), 'gameStartTime': raw.get('gameStartTime'),
                      'gameLength': raw.get('gameLength', 0), 'trackedCount': tracked_count,
                      'participants': participants})
    games.sort(key=lambda g: (-g['trackedCount'], str(g['gameId'])))
    return jsonify({'games': games, 'checked': len(resolved), 'errors': errors,
                    'checkedAt': int(time.time())})

@app.get("/api/live-games/<game_id>/spectate")
def api_live_spectator_launcher(game_id):
    """Download an opt-in local launcher; no spectator credentials are stored."""
    import re
    from flask import Response
    if not re.fullmatch(r"[0-9]{1,20}", game_id):
        return jsonify({"error": "Invalid game identifier"}), 400
    os_name = request.args.get("os", "windows").lower()
    if os_name not in ("windows", "mac"):
        return jsonify({"error": "Choose Windows or macOS"}), 400
    uid = current_user_id()
    settings = _get_user_settings(uid)
    platform = settings["platform"].lower()
    region = routing_for(settings)
    configured = _get_user_players(uid)
    # Recheck the game and membership instead of trusting a supplied game ID.
    raw = None
    for account in configured:
        try:
            player = resolve_player(account, region)
            url = f"https://{platform}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{quote(player['puuid'],safe='')}"
            response = requests.get(url, headers=HEADERS, timeout=12)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            candidate = response.json()
            if str(candidate.get("gameId")) == game_id:
                raw = candidate
                break
        except Exception:
            continue
    if not raw:
        return jsonify({"error": "This game is no longer spectatable or could not be verified. Refresh live games."}), 404
    key = str((raw.get("observers") or {}).get("encryptionKey") or "")
    # The spectator launch protocol is undocumented; no guarantee the client accepts it.
    platform_id = str(raw.get("platformId") or platform).upper()
    if not re.fullmatch(r"[A-Za-z0-9]{1,80}", key) or not re.fullmatch(r"[A-Z0-9]{2,12}", platform_id):
        return jsonify({"error": "Riot did not provide usable spectator connection details for this game."}), 409
    hosts = {"EUW1":"spectator.euw1.lol.pvp.net:80", "EUN1":"spectator.eun1.lol.pvp.net:80", "NA1":"spectator.na1.lol.pvp.net:80", "KR":"spectator.kr.lol.pvp.net:80", "BR1":"spectator.br.lol.pvp.net:80", "LA1":"spectator.la1.lol.pvp.net:80", "LA2":"spectator.la2.lol.pvp.net:80", "OC1":"spectator.oc1.lol.pvp.net:80", "JP1":"spectator.jp1.lol.pvp.net:80", "TR1":"spectator.tr1.lol.pvp.net:80", "RU":"spectator.ru.lol.pvp.net:80"}
    host = hosts.get(platform_id)
    if not host:
        return jsonify({"error": f"Spectator launcher is not configured for {platform_id}."}), 409
    arg = f"spectator {host} {key} {game_id} {platform_id}"
    if os_name == "windows":
        body = ("@echo off\r\n"
                "REM Riftmates spectator launcher. Review this file before running.\r\n"
                "REM Third-party spectator launch is not officially supported and may fail.\r\n"
                "set \"LOL_EXE=C:\\Riot Games\\League of Legends\\Game\\League of Legends.exe\"\r\n"
                "if not exist %LOL_EXE% (echo League executable not found. Edit LOL_EXE in this file. & pause & exit /b 1)\r\n"
                f"start \"\" %LOL_EXE% \"{arg}\"\r\n")
        filename = f"riftmates_spectate_{game_id}.bat"
    else:
        body = ("#!/bin/sh\n"
                "# Riftmates spectator launcher. Review this file before running.\n"
                "# Third-party spectator launch is not officially supported and may fail.\n"
                "APP='/Applications/League of Legends.app'\n"
                "if [ ! -d \"$APP\" ]; then echo 'League app not found. Edit APP in this file.'; exit 1; fi\n"
                f"open -a \"$APP\" --args \"{arg}\"\n")
        filename = f"riftmates_spectate_{game_id}.command"
    return Response(body, mimetype="text/plain", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/sync/progress")
def sync_progress():
    with SYNC_PROGRESS_LOCK:
        return jsonify(dict(SYNC_PROGRESS.get(current_user_id(), {"active": False, "percent": 0, "message": "Ready", "done": 0, "total": 0})))

@app.get("/api/sync")
def sync():
    analytics_sync("lol", "requested")
    uid = current_user_id()
    lock = user_sync_lock(uid)
    if not lock.acquire(blocking=False):
        return jsonify({"error": "A sync is already running for this browser session."}), 409
    try:
        result = _sync_user(uid)
        status = result[1] if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], int) else getattr(result, 'status_code', 200)
        analytics_sync("lol", "completed" if status < 400 else "failed")
        return result
    except Exception:
        analytics_sync("lol", "failed")
        raise
    finally:
        lock.release()

def _sync_user(uid):
    set_sync_progress(2, "Loading saved Riot account identities (only new accounts need lookup)…")
    try:

        count = min(max(int(request.args.get("count", "100")), 1), 1000)

        settings = _get_user_settings(uid)
        region = routing_for(settings)
        pro_accounts, pro_report = load_selected_pro_accounts()
        combined=[]; account_seen=set()
        for player in [p for p in _get_user_players(uid) if "lol" in p.get("games", ["lol", "tft"])]+pro_accounts:
            key=(player["gameName"].casefold(),player["tagLine"].casefold())
            if key not in account_seen:
                account_seen.add(key); combined.append({"gameName":player["gameName"],"tagLine":player["tagLine"]})
        resolved = resolve_players_fast(combined, region)
        set_sync_progress(8, f"Resolved {len(resolved)} accounts, including selected pro teams. Preparing match-history requests…")

        settings = _get_user_settings(uid)
        eligible_queues = set()
        if settings["queues"].get("normals"):
            eligible_queues.update({400, 430, 490})  # Normal Draft, Blind Pick, Quickplay
        if settings["queues"].get("flex"):
            eligible_queues.add(440)  # Ranked Flex
        if settings["queues"].get("rankedSolo"):
            eligible_queues.add(420)  # Ranked Solo/Duo
        if not eligible_queues:
            return jsonify({"error": "Select at least one queue in Settings."}), 400

        # Queue-filtered ID calls avoid downloading full payloads for Solo/Duo, ARAM, etc.

        # Requests overlap network latency, while riot_get enforces the shared API-key limiter.

        id_jobs = []

        with ThreadPoolExecutor(max_workers=8) as pool:

            jobs = []

            for player in resolved:

                for queue_id in sorted(eligible_queues):

                    jobs.append((player["puuid"], queue_id,

                                 pool.submit(get_match_ids, player["puuid"], queue_id, count, region, settings["platform"])))

            id_lists = {}
            total_jobs = len(jobs)
            # Map each Future directly to its account/queue pair. Avoid searching
            # the jobs list with next(), which can raise StopIteration and abort
            # the entire sync if a Future is not found for any reason.
            future_to_job = {future: (puuid, queue_id)
                             for puuid, queue_id, future in jobs}
            for completed, future in enumerate(as_completed(future_to_job), start=1):
                job = future_to_job.get(future)
                if job is None:
                    continue
                puuid, queue_id = job
                try:
                    id_lists[(puuid, queue_id)] = future.result()
                except Exception as exc:
                    # A single account/queue failure should not abort every
                    # other account's sync; report it and continue.
                    print(f"Match-ID lookup failed for {puuid[:8]}… queue {queue_id}: {type(exc).__name__}: {exc}")
                    id_lists[(puuid, queue_id)] = []
                set_sync_progress(8 + 22 * completed / max(total_jobs, 1),
                                  f"Fetching match IDs: {completed}/{total_jobs} account/queue requests",
                                  completed, total_jobs)

        # Interleave queues and players, deduplicating games that include multiple tracked players.

        candidate_ids, seen_ids = [], set()

        max_len = max((len(ids) for ids in id_lists.values()), default=0)

        for index in range(max_len):

            for player in resolved:

                for queue_id in sorted(eligible_queues):

                    ids = id_lists[(player["puuid"], queue_id)]

                    if index < len(ids) and ids[index] not in seen_ids:

                        seen_ids.add(ids[index])

                        candidate_ids.append(ids[index])

        matches = []

        player_game_counts = {p["puuid"]: 0 for p in resolved}

        batch_size = 8
        total_batches = max(1, (len(candidate_ids) + batch_size - 1) // batch_size)
        completed_batches = 0
        set_sync_progress(32, f"Found {len(candidate_ids)} unique candidate matches. Fetching match details…", 0, total_batches)

        with ThreadPoolExecutor(max_workers=8) as pool:

            for offset in range(0, len(candidate_ids), batch_size):

                if all(v >= count for v in player_game_counts.values()):

                    break

                batch_ids = candidate_ids[offset:offset + batch_size]

                futures = [pool.submit(get_match, match_id, region) for match_id in batch_ids]

                batch_matches = []

                for future in futures:

                    match = future.result()

                    if match.get("info", {}).get("queueId") not in eligible_queues:

                        continue

                    analysed = analyse_match(match, resolved)

                    if not analysed:

                        continue

                    if not any(player_game_counts.get(p["puuid"], 0) < count for p in analysed["players"]):

                        continue

                    batch_matches.append(analysed)

                completed_batches += 1
                set_sync_progress(32 + 63 * completed_batches / total_batches,
                                  f"Loading match details: {min(offset + len(batch_ids), len(candidate_ids))}/{len(candidate_ids)} matches checked",
                                  completed_batches, total_batches)

                for analysed in batch_matches:

                    if all(v >= count for v in player_game_counts.values()):

                        break

                    matches.append(analysed)

                    for p in analysed["players"]:

                        if player_game_counts[p["puuid"]] < count:

                            player_game_counts[p["puuid"]] += 1

        set_sync_progress(97, "Calculating player stats and champion rankings…")
        matches.sort(key=lambda m: m.get("gameCreation") or 0, reverse=True)

        result = {"updatedAt": int(time.time()), "region": region,

                        "requestedPerPlayer": count, "queueIds": sorted(eligible_queues), "settings": settings,

                        "players": [{"gameName": p["gameName"], "tagLine": p["tagLine"]} for p in resolved],
                        "proTeams": pro_report, "proAccountCount": len(pro_accounts),

                        "matches": matches, "analytics": build_analytics(matches, resolved),

                        "playerGameCounts": player_game_counts}
        set_sync_progress(98, "Fetching Solo/Duo and Flex ranks…")
        result["ranked"]=_ranked_fetch(uid,resolved,settings["platform"])
        result["rankedHistory"]=_ranked_read(uid,settings["platform"])
        set_sync_progress(100, "Sync complete", 1, 1, active=False)
        return jsonify(result)

    except RiotRateLimitError as exc:
        set_sync_progress(100, str(exc), active=False)
        return jsonify({"error": str(exc)}), 429
    except requests.RequestException as exc:
        message = f"Riot API request failed: {exc}"
        set_sync_progress(100, message, active=False)
        return jsonify({"error": message}), 502
    except (RuntimeError, ValueError, KeyError) as exc:
        set_sync_progress(100, str(exc), active=False)
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        # Never let an unhandled sync exception become Flask's HTML error page;
        # the frontend expects JSON and otherwise reports "Unexpected token '<'".
        app.logger.exception("Unhandled error while syncing Riot data")
        message = f"Unexpected sync error ({type(exc).__name__}): {exc}"
        set_sync_progress(100, message, active=False)
        return jsonify({"error": message}), 500

@app.get('/api/match-timeline/<match_id>')
def api_match_timeline(match_id):
    """Read-only, cached Riot match timeline; limited event payload for UI."""
    if not re.fullmatch(r'[A-Za-z0-9_]{5,80}', match_id):
        return jsonify({'error':'Invalid match ID'}),400
    try:
        region=routing_for(_get_user_settings())
        def fetch():
            return riot_get(f"https://{region}.api.riotgames.com/lol/match/v5/matches/{quote(match_id,safe='')}/timeline")
        raw=cached(f"timeline:{region}:{match_id}",fetch,ttl=None)
        frames=raw.get('info',{}).get('frames',[])
        keep={'CHAMPION_KILL','ELITE_MONSTER_KILL','BUILDING_KILL','TURRET_PLATE_DESTROYED','INHIBITOR_BUILDING_KILL'}
        events=[];gold=[]
        for frame in frames:
            minute=round(float(frame.get('timestamp',0))/60000,1)
            totals={100:0,200:0}
            for pid,part in frame.get('participantFrames',{}).items():
                try: team=100 if int(pid)<=5 else 200
                except (ValueError,TypeError):continue
                totals[team]+=int(part.get('totalGold',0) or 0)
            gold.append({'minute':minute,'blue':totals[100],'red':totals[200],'diff':totals[100]-totals[200]})
            for e in frame.get('events',[]):
                typ=e.get('type','')
                if typ not in keep:continue
                pos=e.get('position') or {}
                events.append({'minute':round(float(e.get('timestamp',0))/60000,1),'type':typ,'killerId':e.get('killerId',0),'victimId':e.get('victimId',0),'teamId':e.get('teamId',0),'monsterType':e.get('monsterType',''),'monsterSubType':e.get('monsterSubType',''),'buildingType':e.get('buildingType',''),'x':pos.get('x'),'y':pos.get('y'),'assistingParticipantIds':e.get('assistingParticipantIds',[])})
        # Timeline IDs refer to match participants, not the user's roster ordering.
        # Include a roster mapping so the UI can show champion portraits and tracked Riot IDs.
        match_raw=get_match(match_id,region)
        roster=[]
        for part in match_raw.get('info',{}).get('participants',[]):
            roster.append({'participantId':int(part.get('participantId',0) or 0),
                           'puuid':part.get('puuid',''),
                           'gameName':part.get('riotIdGameName') or part.get('summonerName') or '',
                           'tagLine':part.get('riotIdTagline',''),
                           'champion':part.get('championName','Unknown'),
                           'profileIconId':int(part.get('profileIcon',0) or 0),
                           'teamId':int(part.get('teamId',0) or 0)})
        # Cluster geographically close kills within a short window as estimated fights.
        kills=[e for e in events if e['type']=='CHAMPION_KILL' and isinstance(e.get('x'),(int,float)) and isinstance(e.get('y'),(int,float))]
        clusters=[]
        for e in kills:
            matched=None
            for c in reversed(clusters):
                if e['minute']-c[-1]['minute']<=1.5 and ((e['x']-c[-1]['x'])**2+(e['y']-c[-1]['y'])**2)**.5<=2600:
                    matched=c;break
            if matched is None:clusters.append([e])
            else:matched.append(e)
        fights=[]
        for cluster in clusters:
            if len(cluster)<2:continue
            ids=set()
            for e in cluster:
                ids.update([e.get('killerId'),e.get('victimId')]);ids.update(e.get('assistingParticipantIds') or [])
            ids.discard(0);ids.discard(None)
            fights.append({'startMinute':cluster[0]['minute'],'endMinute':cluster[-1]['minute'],'kills':len(cluster),'blueKills':sum(1 for e in cluster if 1<=int(e.get('killerId') or 0)<=5),'redKills':sum(1 for e in cluster if 6<=int(e.get('killerId') or 0)<=10),'participantIds':sorted(ids)})
        # Cumulative frame stats and full kill/objective timestamps for cross-match playstyle badges.
        # Riot timeline frame damageStats are sometimes absent: return null, never fabricate damage.
        snapshots=[]
        for frame in frames:
            snap={}
            for pid,pf in frame.get('participantFrames',{}).items():
                ds=pf.get('damageStats') or {}
                dmg=ds.get('totalDamageDoneToChampions')
                snap[str(pid)]={'cs':int(pf.get('minionsKilled',0) or 0)+int(pf.get('jungleMinionsKilled',0) or 0),
                                'damage':int(dmg) if isinstance(dmg,(int,float)) else None}
            snapshots.append({'minute':round(float(frame.get('timestamp',0))/60000,2),'players':snap})
        phase_events=[]
        for frame in frames:
            for e in frame.get('events',[]):
                if e.get('type') not in ('CHAMPION_KILL','ELITE_MONSTER_KILL'):continue
                pos=e.get('position') or {}
                phase_events.append({'minute':round(float(e.get('timestamp',0))/60000,2),
                                     'type':e.get('type'),'killerId':int(e.get('killerId') or 0),
                                     'victimId':int(e.get('victimId') or 0),'assistingParticipantIds':e.get('assistingParticipantIds',[]),'shutdownBounty':int(e.get('shutdownBounty') or 0),'stolen':e.get('stolen',False),'monsterType':e.get('monsterType',''),
                                     'x':pos.get('x'),'y':pos.get('y')})
        return jsonify({'events':events[:800],'gold':gold[:100],'teamfights':fights[:100],
                        'participants':roster,'matchId':match_id,'phaseSnapshots':snapshots,'phaseEvents':phase_events})
    except Exception as exc:
        app.logger.warning('Timeline unavailable: %s',exc)
        return jsonify({'error':'Riot timeline unavailable for this match'}),502

@app.get('/api/match-detail/<match_id>')
def api_match_detail(match_id):
    # Only Riot match IDs are accepted; do not expose arbitrary upstream URLs.
    if not re.fullmatch(r'[A-Za-z0-9_]{5,80}', match_id):
        return jsonify({'error':'Invalid match ID'}),400
    settings=_get_user_settings()
    try:
        raw=get_match(match_id,routing_for(settings))
        info=raw.get('info',{})
        roster=[]
        for p in info.get('participants',[]):
            roster.append({'participantId':int(p.get('participantId',0) or 0),'profileIconId':int(p.get('profileIcon',0) or 0),'puuid':p.get('puuid',''),'gameName':p.get('riotIdGameName') or p.get('summonerName') or 'Unknown','tagLine':p.get('riotIdTagline',''),'champion':p.get('championName','Unknown'),'teamId':p.get('teamId'),'teamPosition':role_name(p.get('teamPosition')),'win':bool(p.get('win')),'kills':int(p.get('kills',0) or 0),'deaths':int(p.get('deaths',0) or 0),'assists':int(p.get('assists',0) or 0),'cs':int(p.get('totalMinionsKilled',0) or 0)+int(p.get('neutralMinionsKilled',0) or 0),'visionScore':int(p.get('visionScore',0) or 0),'damage':int(p.get('totalDamageDealtToChampions',0) or 0),'goldEarned':int(p.get('goldEarned',0) or 0),'dragonKills':int(p.get('dragonKills',0) or 0),'baronKills':int(p.get('baronKills',0) or 0),'turretDamage':int(p.get('damageDealtToTurrets',0) or 0),'damageTaken':int(p.get('totalDamageTaken',0) or 0),'healShield':int(p.get('totalHealsOnTeammates',0) or 0)+int(p.get('totalDamageShieldedOnTeammates',0) or 0),'turretTakedowns':int(p.get('turretTakedowns',0) or 0),'inhibitorTakedowns':int(p.get('inhibitorTakedowns',0) or 0)})
        if len(roster)!=10:return jsonify({'error':'Complete 5v5 data is not available for this match'}),404
        return jsonify({'match':{'matchId':match_id,'gameCreation':info.get('gameCreation'),'gameDuration':info.get('gameDuration',0),'queueId':info.get('queueId'),'gameMode':info.get('gameMode'),'players':roster}})
    except Exception as exc:
        app.logger.warning('Match detail fetch failed: %s',exc)
        return jsonify({'error':'Unable to retrieve full match details from Riot'}),502


if __name__=="__main__":

    print(f"Riftmates backend listening on port {PORT}")

    app.run(host="0.0.0.0",port=PORT,debug=False,threaded=True)


