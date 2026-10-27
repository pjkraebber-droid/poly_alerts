#!/usr/bin/env python3
"""
whalewatch - phone alerts when Polymarket traders you follow make big bets.

Read-only. It uses Polymarket's public Data API and Gamma API, so it needs no
Polymarket account, no wallet and no keys, and it never places trades.

Soccer markets are ALWAYS dropped. That rule is in the code below and there is
deliberately no setting that turns it off.

Commands
  python3 whalewatch.py run              one pass: check the watchlist, send alerts
  python3 whalewatch.py run --loop 300   keep running on your laptop, every 300 seconds
  python3 whalewatch.py run --dry-run    print alerts instead of sending them
  python3 whalewatch.py scout            rank leaderboard traders by how copyable they look
  python3 whalewatch.py check 0x...      quick profile of one wallet
  python3 whalewatch.py test-alert       send a test notification to your phone

Only the Python standard library is used (Python 3.11+).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
EVENT_URL = "https://polymarket.com/event/{slug}"
USER_AGENT = "whalewatch/1.0 (personal read-only tracker)"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "watchlist.toml")
STATE_PATH = os.path.join(HERE, "state.json")

LOOKBACK_SECONDS = 3 * 3600        # each run re-reads the last 3 hours; repeats are skipped
BUCKET_WINDOW_SECONDS = 6 * 3600   # fills in one market add up until the trader goes quiet for 6h
EVENT_CACHE_SECONDS = 7 * 86400    # how long a market's topic (soccer / sports / other) is cached
SPORTS_INDEX_SECONDS = 86400       # refresh Polymarket's list of sports leagues daily
MAX_LOOKUP_ATTEMPTS = 6            # runs to retry an unclassifiable trade before dropping it
HEALTH_ALERT_AFTER = 12            # failed runs in a row (~1 hour) before a "tracker is down" ping
WALLET_RE = re.compile(r"^0x[0-9a-f]{40}$")

DEFAULT_ALERTS = {
    "min_bet_usd": 1000.0,
    "digest_min_usd": 500.0,
    "include_sells": True,
    "block_all_sports": True,
    "timezone": "America/New_York",
    "ntfy_server": "https://ntfy.sh",
}

# Which bets each group alerts on. Soccer is blocked for every group, always.
#   sports: only non-soccer sports bets
#   events: only non-sports bets (politics, economics, world events, crypto…)
#   all:    everything except soccer, plus other sports unless block_all_sports
GROUPS = {"sports": "Sports", "events": "Current events", "all": "Other"}


def wanted(group, cls, block_all_sports):
    """Should a trade of this class reach the phone for a trader in this group?"""
    if cls == "soccer":
        return False
    if group == "sports":
        return cls == "sports"
    if group == "events":
        return cls == "other"
    return not (cls == "sports" and block_all_sports)


DEFAULT_SCOUT_CATEGORIES = [
    "overall", "politics", "economics", "finance", "crypto",
    "geopolitics", "culture", "tech", "weather",
]


# --------------------------------------------------------------------------
# Soccer / sports detection
#
# Soccer is always blocked. Several independent signals are checked and ANY one
# of them blocks the trade: Polymarket's soccer tag, soccer league tags and
# slug codes from Polymarket's own /sports list, soccer words in the title or
# slug, and soccer-specific resolution language ("stoppage time") in the rules.
# If a market can't be looked up at all, the trade is held back and retried,
# never sent unchecked.
# --------------------------------------------------------------------------
SOCCER_TAG_IDS = {"100350"}   # Polymarket's soccer tag (on every soccer league in /sports)
SPORTS_TAG_IDS = {"1"}        # Polymarket's top-level Sports tag
SOCCER_TAG_WORDS = ("soccer", "football", "futbol")

SOCCER_PATTERNS = [
    r"soccer", r"football", r"futbol", r"fútbol",
    r"premier league", r"la ?liga", r"serie a", r"bundesliga", r"ligue 1",
    r"eredivisie", r"primeira liga", r"scottish premiership",
    r"champions league", r"europa league", r"conference league", r"ucl", r"uel", r"uecl",
    r"mls", r"nwsl", r"usl", r"upsl", r"liga mx", r"leagues cup", r"open cup",
    r"fifa", r"uefa", r"concacaf", r"conmebol", r"world cup", r"club world cup", r"copa",
    r"ballon d ?or", r"golden boot", r"golden ball", r"fa cup", r"carabao", r"efl",
    r"league cup", r"dfb pokal", r"coppa italia", r"copa del rey",
    r"fc", r"nycfc", r"inter miami", r"red bulls",
    r"stoppage time", r"penalty shoot ?out",
]
SOCCER_RE = re.compile(r"\b(?:" + "|".join(SOCCER_PATTERNS) + r")\b", re.I)
SOCCER_DESC_RE = re.compile(
    r"stoppage time|added time|penalty shoot ?out|90 minutes|france football|"
    r"\bfifa\b|\buefa\b|\bconcacaf\b|\bconmebol\b|\bmls\b|\bsoccer\b",
    re.I,
)
SHORT_TERM_RE = re.compile(r"up or down|updown", re.I)


def norm(text) -> str:
    """Lowercase, drop apostrophes, turn slug separators into spaces."""
    s = str(text or "").lower().replace("’", "").replace("'", "")
    return re.sub(r"[-_/]+", " ", s)


def first_token(slug) -> str:
    s = str(slug or "").lower().strip()
    return s.split("-", 1)[0] if s else ""


class SportsIndex:
    """Tag ids and slug codes for Polymarket's sports leagues, from GET /sports."""

    def __init__(self, soccer_tag_ids=(), sport_tag_ids=(), soccer_codes=(), sport_codes=()):
        self.soccer_tag_ids = set(map(str, soccer_tag_ids))
        self.sport_tag_ids = set(map(str, sport_tag_ids))
        self.soccer_codes = set(soccer_codes)
        self.sport_codes = set(sport_codes)

    @classmethod
    def from_sports_list(cls, entries):
        soccer_tags, other_tags, all_tags = set(), set(), set()
        soccer_codes, sport_codes = set(), set()
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            code = str(e.get("sport") or "").lower().strip()
            raw = e.get("tags")
            if isinstance(raw, str):
                ids = {x.strip() for x in raw.split(",")}
            else:
                ids = {str(x) for x in (raw or [])}
            ids.discard("")
            all_tags |= ids
            if ids & SOCCER_TAG_IDS:
                soccer_tags |= ids
                if code:
                    soccer_codes.add(code)
            else:
                other_tags |= ids
                if code:
                    sport_codes.add(code)
        # A tag counts as soccer-only if it never appears on a non-soccer league.
        return cls(soccer_tags - other_tags, all_tags, soccer_codes, sport_codes | soccer_codes)

    def to_dict(self):
        return {
            "soccer_tag_ids": sorted(self.soccer_tag_ids),
            "sport_tag_ids": sorted(self.sport_tag_ids),
            "soccer_codes": sorted(self.soccer_codes),
            "sport_codes": sorted(self.sport_codes),
        }

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(d.get("soccer_tag_ids", ()), d.get("sport_tag_ids", ()),
                   d.get("soccer_codes", ()), d.get("sport_codes", ()))


def classify_event(event, row, index: SportsIndex) -> str:
    """Return 'soccer', 'sports' or 'other' for a trade row and its Gamma event."""
    event = event or {}
    texts = [row.get("title"), row.get("slug"), row.get("event_slug"),
             event.get("title"), event.get("slug"), event.get("ticker"), event.get("seriesSlug")]
    for s in event.get("series") or []:
        if isinstance(s, dict):
            texts += [s.get("slug"), s.get("title")]
    blob = " | ".join(norm(t) for t in texts if t)

    tags = [t for t in (event.get("tags") or []) if isinstance(t, dict)]
    tag_ids = {str(t.get("id")) for t in tags}
    tag_words = " ".join(norm(t.get("slug")) + " " + norm(t.get("label")) for t in tags)
    desc = norm(event.get("description"))
    prefix = first_token(row.get("event_slug") or event.get("slug"))

    if (tag_ids & (SOCCER_TAG_IDS | index.soccer_tag_ids)
            or any(w in tag_words for w in SOCCER_TAG_WORDS)
            or SOCCER_RE.search(blob)
            or SOCCER_RE.search(tag_words)
            or SOCCER_DESC_RE.search(desc)
            or (prefix and prefix in index.soccer_codes)):
        return "soccer"

    if (tag_ids & (SPORTS_TAG_IDS | index.sport_tag_ids)
            or re.search(r"\be?sports?\b", tag_words)
            or norm(event.get("category")) in ("sports", "sport")
            or (prefix and prefix in index.sport_codes)
            or event.get("gameId")):
        return "sports"
    return "other"


class Unknown(Exception):
    """A trade whose market couldn't be looked up. It is held back, never sent."""


class Classifier:
    def __init__(self, api, cache: dict, index: SportsIndex, now: int):
        self.api = api
        self.cache = cache
        self.index = index
        self.now = now
        self.failed = set()

    def classify(self, row) -> str:
        # Cheapest check first: obvious soccer in the trade itself, no network needed.
        if classify_event(None, row, self.index) == "soccer":
            return "soccer"
        slug = row.get("event_slug") or ""
        if not slug:
            raise Unknown("trade has no event slug")
        hit = self.cache.get(slug)
        if hit and self.now - int(hit.get("ts", 0)) < EVENT_CACHE_SECONDS:
            return hit["cls"]
        if slug in self.failed:
            raise Unknown(slug)
        try:
            event = self.api.event(slug)
        except ApiError as e:
            self.failed.add(slug)
            raise Unknown(slug) from e
        if not event:
            self.failed.add(slug)
            raise Unknown(slug)
        cls = classify_event(event, row, self.index)
        self.cache[slug] = {"cls": cls, "ts": int(self.now)}
        return cls


# --------------------------------------------------------------------------
# Polymarket API (public, read-only)
# --------------------------------------------------------------------------
class ApiError(Exception):
    pass


def http_get_json(url, params=None, retries=3, timeout=20):
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = url + "?" + urllib.parse.urlencode(clean)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404, 422):
                raise ApiError(f"HTTP {e.code} for {url}") from e
            time.sleep(2 * (attempt + 1) if e.code == 429 else 1 + attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1 + attempt)
    raise ApiError(f"Couldn't reach {url}: {last}")


class Polymarket:
    def __init__(self, getter=None, pause=0.0):
        self.getter = getter or http_get_json
        self.pause = pause

    def _get(self, base, path, params=None):
        if self.pause:
            time.sleep(self.pause)
        return self.getter(base + path, params or {})

    @staticmethod
    def _data(resp):
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    def activity(self, wallet, start=None, limit=500, max_pages=4):
        """Trades for one wallet, newest first."""
        rows, cursor = [], None
        for _ in range(max_pages):
            params = {"user": wallet, "type": "TRADE", "limit": limit}
            if start:
                params["start"] = int(start)
            if cursor:
                params["cursor"] = cursor
            resp = self._get(DATA_API, "/v2/activity", params)
            page = self._data(resp) or []
            if not isinstance(page, list):
                page = []
            rows.extend(page)
            pag = resp.get("pagination") if isinstance(resp, dict) else None
            pag = pag or {}
            cursor = pag.get("next_cursor")
            if not page or not pag.get("has_more") or not cursor:
                break
            if start and min(int(r.get("timestamp") or 0) for r in page) < start:
                break
        return rows

    def leaderboard(self, period="month", category="overall", limit=25):
        resp = self._get(DATA_API, "/v2/leaderboard",
                         {"time_period": period, "category": category, "sort_by": "PNL", "limit": limit})
        data = self._data(resp)
        return data if isinstance(data, list) else []

    def leaderboard_user(self, wallet, period="all"):
        data = self._data(self._get(DATA_API, "/v2/leaderboard", {"user": wallet, "time_period": period}))
        return data if isinstance(data, dict) else {}

    def user_stats(self, wallet):
        data = self._data(self._get(DATA_API, "/v2/user-stats", {"user": wallet}))
        return data if isinstance(data, dict) else {}

    def event(self, slug):
        resp = self._get(GAMMA_API, "/events", {"slug": slug})
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            resp = resp["data"]
        if isinstance(resp, list):
            return resp[0] if resp else None
        if isinstance(resp, dict) and resp.get("slug"):
            return resp
        return None

    def market_price(self, slug, outcome_index):
        resp = self._get(GAMMA_API, "/markets", {"slug": slug})
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            resp = resp["data"]
        market = resp[0] if isinstance(resp, list) and resp else None
        if not market:
            return None
        prices = market.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        return float(prices[int(outcome_index)])

    def sports(self):
        resp = self._get(GAMMA_API, "/sports")
        data = self._data(resp)
        return data if isinstance(data, list) else []


def get_sports_index(api, state, now, log=print) -> SportsIndex:
    cached = state.get("sports_index") or {}
    if cached and now - int(cached.get("ts", 0)) < SPORTS_INDEX_SECONDS:
        return SportsIndex.from_dict(cached)
    try:
        index = SportsIndex.from_sports_list(api.sports())
        state["sports_index"] = {**index.to_dict(), "ts": int(now)}
        return index
    except ApiError as e:
        log(f"Couldn't refresh the sports list ({e}); using the last copy and tag checks.")
        return SportsIndex.from_dict(cached)


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------
def load_config(path=CONFIG_PATH):
    if tomllib is None:
        sys.exit("This needs Python 3.11 or newer (for reading watchlist.toml).")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    alerts = {**DEFAULT_ALERTS, **(raw.get("alerts") or {})}
    alerts["min_bet_usd"] = float(alerts["min_bet_usd"])
    if "block_soccer" in alerts:
        alerts.pop("block_soccer")
        print("Note: soccer blocking is built in and can't be turned off; ignoring block_soccer.")
    whales, seen = [], set()
    for w in raw.get("whales") or []:
        addr = str(w.get("wallet", "")).strip().lower()
        name = str(w.get("name") or addr[:10])
        if not WALLET_RE.match(addr):
            sys.exit(f"Bad wallet for {name!r}: {addr!r}. It should be 0x followed by 40 letters/numbers.")
        if addr in seen:
            continue
        seen.add(addr)
        group = str(w.get("group") or "all").strip().lower()
        if group not in GROUPS:
            sys.exit(f"Bad group for {name!r}: {group!r}. Use \"sports\", \"events\" or leave it out.")
        whales.append({
            "name": name,
            "wallet": addr,
            "group": group,
            "note": str(w.get("note") or ""),
            "min_bet_usd": float(w.get("min_bet_usd", alerts["min_bet_usd"])),
        })
    return alerts, whales


def load_state(path=STATE_PATH):
    try:
        with open(path) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("version", 1)
    for key in ("wallets", "seen", "buckets", "events", "lookups"):
        state.setdefault(key, {})
    state.setdefault("health", {"failures": 0})
    return state


def save_state(state, path=STATE_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def fill_key(row) -> str:
    raw = "|".join(str(row.get(k, "")) for k in
                   ("transaction_hash", "token_id", "side", "size", "price", "timestamp"))
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
def money(x) -> str:
    x = float(x or 0)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e6:
        return f"{sign}${x / 1e6:.1f}M"
    if x >= 1e4:
        return f"{sign}${x / 1e3:.0f}K"
    if x >= 1e3:
        return f"{sign}${x / 1e3:.1f}K"
    return f"{sign}${x:,.0f}"


def cents(p) -> str:
    return f"{float(p) * 100:.0f}¢"


def clock(ts, tz_name="America/New_York", now=None) -> str:
    tz = timezone.utc
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
    dt = datetime.fromtimestamp(int(ts), tz)
    today = datetime.fromtimestamp(int(now or time.time()), tz).date()
    hm = dt.strftime("%I:%M%p").lstrip("0").lower()
    return hm if dt.date() == today else dt.strftime("%a ") + hm


def short_name(name, wallet) -> str:
    name = str(name or "")
    if not name or (name.lower().startswith("0x") and len(name) >= 40):
        return wallet[:6] + "…" + wallet[-4:]
    return name


def format_alert(whale, b, fresh, price_now, tz_name, now):
    buying = b["side"] == "BUY"
    label = {"sports": "Sports · ", "events": "Events · "}.get(whale.get("group"), "")
    title = f"{label}{whale['name']}: {'BUY' if buying else 'SELL'} {money(fresh)}"
    outcome = b.get("outcome") or f"Outcome {b.get('outcome_index')}"
    lines = [f"{outcome} — {b.get('title') or b.get('slug')}"]
    parts = []
    if b.get("shares"):
        parts.append(f"avg {cents(b['usd'] / b['shares'])}")
    if price_now is not None:
        parts.append(f"now {cents(price_now)}")
    if parts:
        lines.append("Price " + " · ".join(parts))
    if b["usd"] - fresh >= 1:
        lines.append(f"{money(b['usd'])} in this market since {clock(b['first_ts'], tz_name, now)}")
    if not buying:
        lines.append("Selling = taking profit or getting out.")
    if whale.get("note"):
        lines.append(whale["note"])
    tags = (["trophy"] if whale.get("group") == "sports" else ["whale"]) if buying else ["outbox_tray"]
    # Sells arrive silently; a bet 5x the trader's threshold buzzes harder.
    priority = 2 if not buying else (4 if fresh >= 5 * whale["min_bet_usd"] else 3)
    click = EVENT_URL.format(slug=b["event_slug"]) if b.get("event_slug") else None
    return title, "\n".join(lines), click, tags, priority


# --------------------------------------------------------------------------
# Notifications: Telegram and/or ntfy, whichever is configured
#
#   Telegram: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (optional TELEGRAM_CHAT_SPORTS
#             and TELEGRAM_CHAT_EVENTS send each group to its own chat/channel)
#   ntfy:     NTFY_TOPIC (optional NTFY_TOKEN)
# --------------------------------------------------------------------------
def http_post_json(url, payload, headers=None, timeout=15):
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def channels_configured(env=None):
    env = os.environ if env is None else env
    tg = bool(env.get("TELEGRAM_BOT_TOKEN", "").strip() and env.get("TELEGRAM_CHAT_ID", "").strip())
    return {"telegram": tg, "ntfy": bool(env.get("NTFY_TOPIC", "").strip())}


def make_notifier(alerts, dry_run=False, log=print, env=None, poster=None):
    env = os.environ if env is None else env
    post = poster or http_post_json
    topic = env.get("NTFY_TOPIC", "").strip()
    ntfy_token = env.get("NTFY_TOKEN", "").strip()
    server = str(alerts.get("ntfy_server") or DEFAULT_ALERTS["ntfy_server"]).rstrip("/")
    tg_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_default = env.get("TELEGRAM_CHAT_ID", "").strip()
    tg_chats = {"sports": env.get("TELEGRAM_CHAT_SPORTS", "").strip() or tg_default,
                "events": env.get("TELEGRAM_CHAT_EVENTS", "").strip() or tg_default}

    def send(title, message, click=None, tags=None, priority=3, group=None):
        if dry_run:
            log(f"--- [dry run{' · ' + group if group else ''}] {title}\n{message}\n{click or ''}\n")
            return True
        delivered = False
        if tg_token and tg_default:
            chat = tg_chats.get(group or "", tg_default) or tg_default
            text = f"<b>{html.escape(title)}</b>\n{html.escape(message)}"
            payload = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                       "disable_notification": int(priority) <= 2,
                       "link_preview_options": {"is_disabled": True}}
            if click:
                label = "Open market" if "polymarket.com" in click else "Open report"
                payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": click}]]}
            try:
                resp = post(f"https://api.telegram.org/bot{tg_token}/sendMessage", payload)
                if resp.get("ok", True):
                    delivered = True
                else:
                    log(f"Telegram refused the message: {resp.get('description')}")
            except Exception as e:  # never print the token
                log(f"Couldn't send to Telegram ({type(e).__name__}); will retry next run.")
        if topic:
            payload = {"topic": topic, "title": title, "message": message, "priority": int(priority)}
            if tags:
                payload["tags"] = list(tags)
            if click:
                payload["click"] = click
            headers = {"Authorization": f"Bearer {ntfy_token}"} if ntfy_token else {}
            try:
                post(server, payload, headers)
                delivered = True
            except Exception as e:
                log(f"Couldn't send to ntfy ({type(e).__name__}); will retry next run.")
        return delivered

    return send


def telegram_chats(token, getter=None):
    """List the chats and channels a bot can see (people who messaged it, channels it was added to)."""
    get = getter or (lambda url: http_get_json(url, {"allowed_updates": json.dumps(
        ["message", "channel_post", "my_chat_member"])}))
    data = get(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = {}
    for u in data.get("result", []) if isinstance(data, dict) else []:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (u.get(key) or {}).get("chat")
            if chat and "id" in chat:
                name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                chats[chat["id"]] = {"id": chat["id"], "type": chat.get("type"), "name": name or "",
                                     "username": chat.get("username") or ""}
    return list(chats.values())


# --------------------------------------------------------------------------
# The tracker
# --------------------------------------------------------------------------
def run_once(api, alerts, whales, state, notify, now=None, log=print):
    now = int(now or time.time())
    index = get_sports_index(api, state, now, log)
    clf = Classifier(api, state["events"], index, now)
    by_wallet = {w["wallet"]: w for w in whales}
    stats = {"checked": 0, "alerts": 0, "soccer_skipped": 0, "sports_skipped": 0, "off_group": 0,
             "held_back": 0, "dropped": 0, "failures": 0}
    new_names, attempted = [], set()

    # Forget wallets that were removed from the watchlist.
    for addr in list(state["wallets"]):
        if addr not in by_wallet:
            del state["wallets"][addr]
            for k in [k for k in state["buckets"] if k.startswith(addr + "|")]:
                del state["buckets"][k]

    for whale in whales:
        addr = whale["wallet"]
        if addr not in state["wallets"]:
            # First time we see this wallet: start watching from now, no backlog flood.
            state["wallets"][addr] = {"since": now}
            new_names.append(whale["name"])
            continue
        stats["checked"] += 1
        start = max(now - LOOKBACK_SECONDS, int(state["wallets"][addr].get("since", 0)))
        try:
            rows = api.activity(addr, start=start)
        except ApiError as e:
            stats["failures"] += 1
            log(f"[{whale['name']}] couldn't fetch trades: {e}")
            continue

        rows = [r for r in rows if str(r.get("type") or "TRADE").upper() == "TRADE"
                and int(r.get("timestamp") or 0) >= start]
        rows.sort(key=lambda r: int(r.get("timestamp") or 0))

        for r in rows:
            key = fill_key(r)
            if key in state["seen"]:
                continue
            ts = int(r.get("timestamp") or 0)
            if r.get("is_combo"):
                state["seen"][key] = ts      # parlays can't be copied one-to-one; skip
                continue
            slug = r.get("event_slug") or "?"
            try:
                cls = clf.classify(r)
            except Unknown:
                look = state["lookups"].setdefault(slug, {"n": 0, "ts": now})
                if slug not in attempted:
                    look["n"] += 1
                    look["ts"] = now
                    attempted.add(slug)
                if look["n"] >= MAX_LOOKUP_ATTEMPTS:
                    state["seen"][key] = ts  # give up quietly rather than send it unchecked
                    stats["dropped"] += 1
                else:
                    stats["held_back"] += 1
                continue
            state["lookups"].pop(slug, None)
            state["seen"][key] = ts

            if cls == "soccer":
                stats["soccer_skipped"] += 1
                continue
            if not wanted(whale.get("group", "all"), cls, alerts["block_all_sports"]):
                stats["sports_skipped" if cls == "sports" else "off_group"] += 1
                continue
            side = str(r.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            if side == "SELL" and not alerts["include_sells"]:
                continue

            bkey = f"{addr}|{r.get('condition_id')}|{r.get('outcome_index')}|{side}"
            b = state["buckets"].get(bkey)
            if b is None or ts - int(b.get("last_ts", 0)) > BUCKET_WINDOW_SECONDS:
                b = {"wallet": addr, "side": side, "usd": 0.0, "shares": 0.0, "alerted_usd": 0.0,
                     "first_ts": ts, "last_ts": ts}
                state["buckets"][bkey] = b
            b["usd"] += float(r.get("usdc_size") or 0)
            b["shares"] += float(r.get("size") or 0)
            b["last_ts"] = max(int(b["last_ts"]), ts)
            for f in ("title", "slug", "event_slug", "outcome", "outcome_index", "condition_id"):
                if r.get(f) is not None:
                    b[f] = r.get(f)

    # Check every open bucket, not just ones that grew this run, so an alert
    # that failed to send last time is retried.
    for bkey in sorted(state["buckets"]):
        b = state["buckets"][bkey]
        whale = by_wallet.get(b["wallet"])
        if not whale:
            continue
        fresh = b["usd"] - b["alerted_usd"]
        if fresh < whale["min_bet_usd"]:
            continue
        price_now = None
        try:
            if b.get("slug") is not None and b.get("outcome_index") is not None:
                price_now = api.market_price(b["slug"], b["outcome_index"])
        except Exception:
            price_now = None
        title, message, click, tags, priority = format_alert(
            whale, b, fresh, price_now, alerts.get("timezone"), now)
        if notify(title, message, click, tags, priority, whale.get("group")):
            b["alerted_usd"] = b["usd"]
            stats["alerts"] += 1

    if new_names:
        by_group = defaultdict(list)
        for w in whales:
            if w["name"] in new_names:
                by_group[w.get("group", "all")].append(w["name"])
        listing = "\n".join(f"{GROUPS[g]}: {', '.join(n)}" for g, n in by_group.items())
        notify("Whale tracker is on",
               f"Now watching:\n{listing}\n\n"
               f"You'll get a message when one of them makes a big bet (each trader has their own size). "
               f"Sells arrive silently. Soccer markets are always skipped.",
               None, ["white_check_mark"], 3)

    # Housekeeping keeps state.json small.
    keep_after = now - LOOKBACK_SECONDS - 3600
    state["seen"] = {k: v for k, v in state["seen"].items() if int(v) >= keep_after}
    state["buckets"] = {k: v for k, v in state["buckets"].items()
                        if now - int(v.get("last_ts", 0)) <= BUCKET_WINDOW_SECONDS}
    state["events"] = {k: v for k, v in state["events"].items()
                       if now - int(v.get("ts", 0)) < EVENT_CACHE_SECONDS}
    state["lookups"] = {k: v for k, v in state["lookups"].items() if now - int(v.get("ts", 0)) < 86400}

    # Tell you once if Polymarket has been unreachable for about an hour.
    health = state["health"]
    if stats["checked"] and stats["failures"] == stats["checked"]:
        health["failures"] = int(health.get("failures", 0)) + 1
        if health["failures"] == HEALTH_ALERT_AFTER:
            notify("Whale tracker can't reach Polymarket",
                   "Every check has failed for about an hour. It will keep trying and "
                   "alerts will resume on their own once Polymarket responds.",
                   None, ["warning"], 3)
    else:
        health["failures"] = 0

    log(f"Checked {stats['checked']} wallet(s) · {stats['alerts']} alert(s) sent · "
        f"{stats['soccer_skipped']} soccer trade(s) skipped · {stats['sports_skipped']} other sports skipped"
        + (f" · {stats['held_back']} held back for a retry" if stats["held_back"] else "")
        + (f" · {len(new_names)} new wallet(s) baselined" if new_names else ""))
    return stats


# --------------------------------------------------------------------------
# Daily recap: one message summing up the last 24 hours
# --------------------------------------------------------------------------
def build_digest(api, alerts, whales, hours=24, now=None, max_prices=15, log=print):
    """Group each trader's fills from the last `hours` by market and side; drop soccer/sports."""
    now = int(now or time.time())
    start = now - hours * 3600
    index = get_sports_index(api, {}, now, log)
    clf = Classifier(api, {}, index, now)
    floor = float(alerts.get("digest_min_usd", 500))
    moves, quiet, unreachable = [], [], []
    skipped = {"soccer": 0, "sports": 0, "off_group": 0, "unchecked": 0}
    for whale in whales:
        try:
            rows = api.activity(whale["wallet"], start=start)
        except ApiError:
            unreachable.append(whale["name"])
            continue
        groups = {}
        for r in rows:
            ts = int(r.get("timestamp") or 0)
            if ts < start or str(r.get("type") or "TRADE").upper() != "TRADE" or r.get("is_combo"):
                continue
            side = str(r.get("side") or "").upper()
            if side not in ("BUY", "SELL") or (side == "SELL" and not alerts["include_sells"]):
                continue
            try:
                cls = clf.classify(r)
            except Unknown:
                skipped["unchecked"] += 1
                continue
            if cls == "soccer":
                skipped["soccer"] += 1
                continue
            if not wanted(whale.get("group", "all"), cls, alerts["block_all_sports"]):
                skipped["sports" if cls == "sports" else "off_group"] += 1
                continue
            k = (r.get("condition_id"), r.get("outcome_index"), side)
            g = groups.setdefault(k, {"whale": whale, "side": side, "usd": 0.0, "shares": 0.0, "last_ts": 0,
                                      "title": r.get("title"), "slug": r.get("slug"),
                                      "event_slug": r.get("event_slug"), "outcome": r.get("outcome"),
                                      "outcome_index": r.get("outcome_index")})
            g["usd"] += float(r.get("usdc_size") or 0)
            g["shares"] += float(r.get("size") or 0)
            g["last_ts"] = max(g["last_ts"], ts)
        # The recap lists moves of at least half a trader's alert size (and never under the floor).
        big = [g for g in groups.values() if g["usd"] >= max(floor, whale["min_bet_usd"] / 2)]
        if big:
            moves.extend(big)
        else:
            quiet.append(whale["name"])
    moves.sort(key=lambda g: -g["usd"])
    for g in moves[:max_prices]:
        try:
            g["price_now"] = api.market_price(g["slug"], g["outcome_index"])
        except Exception:
            g["price_now"] = None
    return {"moves": moves, "quiet": quiet, "unreachable": unreachable, "skipped": skipped,
            "hours": hours, "now": now, "floor": floor}


def format_digest(d, tz_name="America/New_York", max_lines=12):
    moves = d["moves"]
    tz = timezone.utc
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            pass
    dt = datetime.fromtimestamp(d["now"], tz)
    day = f"{dt.strftime('%a %b')} {dt.day}"
    title = f"Whale recap {day}: " + (f"{len(moves)} big move(s)" if moves else "quiet day")
    lines = []
    groups = [k for k in GROUPS if any(g["whale"].get("group", "all") == k for g in moves)]
    per_group = max(4, max_lines // max(1, len(groups)))
    for key in groups:
        mine = [g for g in moves if g["whale"].get("group", "all") == key]
        if len(groups) > 1 or key != "all":
            lines.append(f"{GROUPS[key].upper()}")
        for g in mine[:per_group]:
            avg = g["usd"] / g["shares"] if g["shares"] else None
            price = f" @ {cents(avg)}" if avg else ""
            if g.get("price_now") is not None:
                price += f" (now {cents(g['price_now'])})"
            lines.append(f"• {g['whale']['name']}: {g['side']} {g.get('outcome') or ''} {money(g['usd'])}{price}\n"
                         f"  {str(g.get('title') or g.get('slug'))[:90]}")
        if len(mine) > per_group:
            lines.append(f"…and {len(mine) - per_group} smaller move(s).")
    if d["quiet"]:
        lines.append("No big moves: " + ", ".join(d["quiet"]))
    if d["unreachable"]:
        lines.append("Couldn't check: " + ", ".join(d["unreachable"]))
    s = d["skipped"]
    if s["soccer"]:
        lines.append(f"Soccer filtered out: {s['soccer']} trade(s).")
    message = "\n".join(lines) or "None of your traders made a big move."
    if len(message) > 3800:  # ntfy's message limit is 4,096 bytes
        message = message[:3790] + "…"
    markdown = [f"## {title}", ""]
    if moves:
        markdown += ["| Group | Trader | Side | Amount | Avg price | Now | Market |", "|---|---|---|---:|---:|---:|---|"]
        for g in moves:
            avg = g["usd"] / g["shares"] if g["shares"] else None
            link = EVENT_URL.format(slug=g["event_slug"]) if g.get("event_slug") else ""
            markdown.append(f"| {GROUPS[g['whale'].get('group', 'all')]} | {g['whale']['name']} | "
                            f"{g['side']} {g.get('outcome') or ''} | {money(g['usd'])} | "
                            f"{cents(avg) if avg else '-'} | "
                            f"{cents(g['price_now']) if g.get('price_now') is not None else '-'} | "
                            f"[{str(g.get('title') or g.get('slug'))[:80]}]({link}) |")
    if d["quiet"]:
        markdown += ["", "No big moves: " + ", ".join(d["quiet"])]
    return title, message, "\n".join(markdown)


# --------------------------------------------------------------------------
# Scout: which leaderboard traders are worth following?
# --------------------------------------------------------------------------
def profile_wallet(api, wallet, clf, now, recent_limit=500):
    p = {"wallet": wallet}
    try:
        lb = api.leaderboard_user(wallet, "all")
    except ApiError:
        lb = {}
    try:
        st = api.user_stats(wallet)
    except ApiError:
        st = {}
    pnl = lb.get("pnl")
    if pnl is None and isinstance(st.get("all_time_pnl"), dict):
        pnl = st["all_time_pnl"].get("economic_pnl")
    p["alltime_pnl"] = float(pnl) if pnl is not None else None
    p["name"] = short_name(lb.get("user_name"), wallet)
    p["markets_ever"] = st.get("trades")
    p["biggest_win"] = st.get("biggest_win")
    p["join_ts"] = st.get("join_date")

    try:
        rows = api.activity(wallet, limit=recent_limit, max_pages=1)
    except ApiError:
        rows = []
    trades = [r for r in rows if str(r.get("type") or "TRADE").upper() == "TRADE" and not r.get("is_combo")]
    p["recent_fills"] = len(trades)
    if not trades:
        p.update(last_trade_days=None, recent_markets=0, short_term_share=0.0, flip_ratio=0.0,
                 median_order=0.0, sports_share=0.0, soccer_share=0.0, topics="")
        return p

    p["last_trade_days"] = (now - max(int(r.get("timestamp") or 0) for r in trades)) / 86400
    p["recent_markets"] = len({r.get("condition_id") for r in trades})
    total = sum(float(r.get("usdc_size") or 0) for r in trades) or 1.0
    short = sum(float(r.get("usdc_size") or 0) for r in trades
                if SHORT_TERM_RE.search(norm(r.get("title")) + " " + norm(r.get("event_slug"))))
    p["short_term_share"] = short / total

    # Many tiny fills are usually one order; group them to see real bet sizes.
    orders = defaultdict(float)
    per_market = defaultdict(list)
    for r in trades:
        ts = int(r.get("timestamp") or 0)
        orders[(r.get("condition_id"), r.get("outcome_index"), r.get("side"), ts // 600)] += float(r.get("usdc_size") or 0)
        per_market[(r.get("condition_id"), r.get("outcome_index"))].append((ts, r.get("side")))
    p["median_order"] = statistics.median(orders.values()) if orders else 0.0

    # Market makers flip between buying and selling the same outcome constantly.
    flips, steps = 0, 0
    for fills in per_market.values():
        fills.sort()
        for (_, a), (_, b) in zip(fills, fills[1:]):
            steps += 1
            flips += a != b
    p["flip_ratio"] = flips / steps if steps else 0.0

    # Buying at 90¢+ is usually sweeping near-certain outcomes late in a game: little left to copy.
    buys = [r for r in trades if str(r.get("side") or "").upper() == "BUY"]
    buy_usd = sum(float(r.get("usdc_size") or 0) for r in buys) or 1.0
    p["late_share"] = sum(float(r.get("usdc_size") or 0) for r in buys if float(r.get("price") or 0) >= 0.9) / buy_usd

    by_event = defaultdict(float)
    sample = {}
    for r in trades:
        slug = r.get("event_slug") or ""
        by_event[slug] += float(r.get("usdc_size") or 0)
        sample.setdefault(slug, r)
    top = sorted(by_event.items(), key=lambda kv: -kv[1])[:12]
    known = soccer = sports = 0.0
    for slug, usd in top:
        try:
            cls = clf.classify(sample[slug])
        except Unknown:
            continue
        known += usd
        if cls == "soccer":
            soccer += usd
            sports += usd
        elif cls == "sports":
            sports += usd
    p["sports_share"] = sports / known if known else 0.0
    p["soccer_share"] = soccer / known if known else 0.0
    titles = [str(sample[s].get("title") or s)[:48] for s, _ in top[:3]]
    p["topics"] = " | ".join(titles)
    return p


def verdict(p, block_all_sports=True, now=None, group="events"):
    now = now or time.time()
    skip, flags = [], []
    pnl = p.get("alltime_pnl")
    if pnl is None or pnl <= 0:
        skip.append("loses money all-time" if pnl is not None else "no lifetime record")
    if p.get("last_trade_days") is None or p["last_trade_days"] > 21:
        skip.append("inactive")
    if p.get("short_term_share", 0) > 0.3:
        skip.append("mostly 5-minute crypto (bot-style)")
    if p.get("flip_ratio", 0) > 0.35:
        skip.append("trades both sides (market maker)")
    if (p.get("markets_ever") or 0) >= 10000 and p.get("median_order", 0) < 50:
        skip.append("bot: thousands of markets, tiny bets")
    if p.get("soccer_share", 0) > 0.5:
        skip.append("mostly soccer, which you can't trade")
    elif group == "sports":
        if p.get("sports_share", 0) < 0.5:
            skip.append("mostly not sports")
    elif (group == "events" or block_all_sports) and p.get("sports_share", 0) > 0.5:
        skip.append("mostly sports")
    if p.get("late_share", 0) > 0.5:
        (skip if group == "sports" else flags).append("mostly buys at 90¢+ (late sweeping)")
    if (p.get("markets_ever") or 0) < 20:
        flags.append("few markets")
    if pnl and pnl > 0 and p.get("biggest_win") and float(p["biggest_win"]) / pnl > 0.5:
        flags.append("one big win")
    if p.get("join_ts") and now - float(p["join_ts"]) < 90 * 86400:
        flags.append("new wallet")
    if 0 < p.get("soccer_share", 0) <= 0.5:
        flags.append(f"{p['soccer_share']:.0%} soccer (gets filtered)")
    label = "SKIP" if skip else ("MAYBE" if flags else "GOOD")
    return label, skip + flags


def scout(api, alerts, whales, period, categories, top, max_wallets, out_csv, summary_path=None,
          now=None, log=print, group="events"):
    now = int(now or time.time())
    watching = {w["wallet"] for w in whales}
    index = get_sports_index(api, {}, now, log)
    clf = Classifier(api, {}, index, now)
    cands = {}
    for cat in categories:
        try:
            rows = api.leaderboard(period, cat, top)
        except ApiError as e:
            log(f"(skipping category '{cat}': {e})")
            continue
        for r in rows:
            addr = str(r.get("user_id") or r.get("proxy_wallet") or "").lower()
            if not WALLET_RE.match(addr):
                continue
            c = cands.setdefault(addr, {"wallet": addr, "boards": [], "period_pnl": float("-inf"),
                                        "lb_name": r.get("user_name")})
            c["boards"].append(f"{cat} #{r.get('rank')}")
            c["period_pnl"] = max(c["period_pnl"], float(r.get("pnl") or 0))
    picked = sorted(cands.values(), key=lambda c: -c["period_pnl"])[:max_wallets]
    log(f"Profiling {len(picked)} traders from {len(categories)} leaderboard(s) ({period})…")

    results = []
    for i, c in enumerate(picked, 1):
        p = profile_wallet(api, c["wallet"], clf, now)
        if p["name"].endswith(c["wallet"][-4:]) and c.get("lb_name"):
            p["name"] = short_name(c["lb_name"], c["wallet"])
        p["period_pnl"] = c["period_pnl"]
        p["boards"] = ", ".join(c["boards"])
        p["verdict"], p["reasons"] = verdict(p, alerts["block_all_sports"], now, group)
        p["watching"] = c["wallet"] in watching
        results.append(p)
        if i % 10 == 0:
            log(f"  …{i}/{len(picked)}")

    order = {"GOOD": 0, "MAYBE": 1, "SKIP": 2}
    results.sort(key=lambda p: (order[p["verdict"]], -(p.get("alltime_pnl") or -1e18)))

    def days(p):
        d = p.get("last_trade_days")
        return "never" if d is None else ("today" if d < 1 else f"{d:.0f}d ago")

    header = f"{'VERDICT':<7} {'TRADER':<22} {'LIFETIME':>9} {period.upper():>9} {'MKTS':>6} {'LAST':>8} {'SPORT':>6}  NOTES"
    log("\n" + header)
    log("-" * len(header))
    for p in results:
        star = "*" if p["watching"] else ""
        log(f"{p['verdict']:<7} {(p['name'] + star)[:22]:<22} {money(p.get('alltime_pnl')):>9} "
            f"{money(p['period_pnl']):>9} {str(p.get('markets_ever') or '-'):>6} {days(p):>8} "
            f"{p.get('sports_share', 0):>6.0%}  {'; '.join(p['reasons'])}")
    log("(* = already on your watchlist)")

    fields = ["verdict", "name", "wallet", "alltime_pnl", "period_pnl", "markets_ever", "last_trade_days",
              "recent_markets", "median_order", "sports_share", "soccer_share", "short_term_share",
              "flip_ratio", "boards", "reasons", "topics", "watching"]
    with open(out_csv, "w", newline="") as f:
        wtr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        wtr.writeheader()
        for p in results:
            row = dict(p)
            row["reasons"] = "; ".join(p["reasons"])
            wtr.writerow(row)
    log(f"\nFull report: {out_csv}")

    good = [p for p in results if p["verdict"] in ("GOOD", "MAYBE") and not p["watching"]][:8]
    snippet = ""
    if good:
        snippet = "\n".join(
            f'[[whales]]\nname = "{p["name"]}"\nwallet = "{p["wallet"]}"\ngroup = "{group}"\n'
            f'note = "{p["verdict"].title()}: {money(p.get("alltime_pnl"))} lifetime"\n'
            for p in good)
        log("\nTo follow any of these, paste into watchlist.toml:\n\n" + snippet)

    if summary_path:
        with open(summary_path, "a") as f:
            f.write(f"## Scout report: {GROUPS.get(group, group)} ({period} leaderboards)\n\n")
            f.write("| Verdict | Trader | Lifetime P&L | " + period.title() + " P&L | Markets | Last trade | Sports | Notes |\n")
            f.write("|---|---|---:|---:|---:|---|---:|---|\n")
            for p in results:
                link = f"[{p['name']}](https://polymarket.com/profile/{p['wallet']})"
                f.write(f"| {p['verdict']} | {link}{' ⭐' if p['watching'] else ''} | {money(p.get('alltime_pnl'))} | "
                        f"{money(p['period_pnl'])} | {p.get('markets_ever') or '-'} | {days(p)} | "
                        f"{p.get('sports_share', 0):.0%} | {'; '.join(p['reasons'])} |\n")
            if snippet:
                f.write("\n### Paste into watchlist.toml to follow\n\n```toml\n" + snippet + "```\n")
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Phone alerts for big Polymarket bets by traders you follow.")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--state", default=STATE_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="check the watchlist and send alerts")
    r.add_argument("--loop", type=int, default=0, metavar="SECONDS", help="repeat forever with this pause")
    r.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")

    s = sub.add_parser("scout", help="rank leaderboard traders by how copyable they look")
    s.add_argument("--period", default="month", choices=["day", "week", "month", "all"])
    s.add_argument("--group", default="events", choices=["events", "sports"],
                   help="events: politics, economics, world news; sports: non-soccer sports bettors")
    s.add_argument("--categories", help="leaderboard categories (default depends on --group)")
    s.add_argument("--top", type=int, default=25, help="traders to take from each leaderboard")
    s.add_argument("--max-wallets", type=int, default=40)
    s.add_argument("--out", default="scout_report.csv")
    s.add_argument("--summary", help="also append a markdown table to this file")

    c = sub.add_parser("check", help="profile one wallet")
    c.add_argument("wallet")

    sub.add_parser("test-alert", help="send a test message to every configured chat")

    d = sub.add_parser("digest", help="send one recap of the last 24 hours")
    d.add_argument("--hours", type=int, default=24)
    d.add_argument("--dry-run", action="store_true", help="print the recap instead of sending it")
    d.add_argument("--summary", help="also write a markdown table to this file")

    n = sub.add_parser("notify", help="send one message (used by the weekly scout)")
    n.add_argument("title")
    n.add_argument("message")
    n.add_argument("--click")

    t = sub.add_parser("telegram-chats", help="list the Telegram chats/channels your bot can post to")
    t.add_argument("--summary", help="also write the list to this file")
    args = ap.parse_args(argv)

    alerts, whales = load_config(args.config)

    if args.cmd == "telegram-chats":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            sys.exit("TELEGRAM_BOT_TOKEN isn't set. Add it as a GitHub secret first.")
        try:
            chats = telegram_chats(token)
        except ApiError:
            sys.exit("Telegram didn't accept the bot token. Check the TELEGRAM_BOT_TOKEN secret.")
        lines = ["| Chat ID | Type | Name |", "|---|---|---|"] + [
            f"| `{c['id']}` | {c['type']} | {c['name']}{' (@' + c['username'] + ')' if c['username'] else ''} |"
            for c in chats]
        if not chats:
            lines = ["No chats yet. Open your bot in Telegram, tap Start and send it a message, "
                     "(and post once in any channel you added it to), then run this again."]
        text = "## Chats your bot can reach\n\n" + "\n".join(lines) + "\n\n" \
               "Save the private chat's ID as the TELEGRAM_CHAT_ID secret. For separate feeds, save the " \
               "channels' IDs as TELEGRAM_CHAT_EVENTS and TELEGRAM_CHAT_SPORTS.\n"
        print(text)
        if args.summary:
            with open(args.summary, "a") as f:
                f.write(text)
        return

    if args.cmd in ("run", "test-alert", "digest", "notify") and not getattr(args, "dry_run", False) \
            and not any(channels_configured().values()):
        sys.exit("No alert channel is set up. Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, or NTFY_TOPIC, "
                 "as GitHub secrets (or use --dry-run to just print).")

    if args.cmd == "notify":
        ok = make_notifier(alerts)(args.title, args.message, args.click, ["mag"], 3)
        sys.exit(0 if ok else 1)

    if args.cmd == "digest":
        dig = build_digest(Polymarket(pause=0.05), alerts, whales, args.hours)
        title, message, md = format_digest(dig, alerts.get("timezone"))
        if args.summary:
            with open(args.summary, "a") as f:
                f.write(md + "\n")
        run_url = None
        if os.environ.get("GITHUB_RUN_ID"):
            run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                       f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        ok = make_notifier(alerts, dry_run=args.dry_run)(title, message, run_url, ["whale", "spiral_notepad"], 3)
        sys.exit(0 if ok else 1)

    if args.cmd == "test-alert":
        send = make_notifier(alerts)
        ok = send("Whale tracker test", "If you can read this, alerts reach your phone. 🐋", None, ["whale"], 3)
        env = os.environ
        for g in ("events", "sports"):
            if env.get(f"TELEGRAM_CHAT_{g.upper()}", "").strip():
                ok = send(f"Whale tracker test: {GROUPS[g]}",
                          f"{GROUPS[g]} alerts will arrive here.", None, ["whale"], 3, g) and ok
        sys.exit(0 if ok else 1)

    if args.cmd == "run":
        if not whales:
            sys.exit("Your watchlist is empty. Add at least one [[whales]] entry to watchlist.toml.")
        api = Polymarket(pause=0.05)
        notify = make_notifier(alerts, dry_run=args.dry_run)
        while True:
            state = load_state(args.state)
            run_once(api, alerts, whales, state, notify)
            save_state(state, args.state)
            if not args.loop:
                break
            time.sleep(max(60, args.loop))
        return

    api = Polymarket(pause=0.1)
    if args.cmd == "scout":
        default = ["sports"] if args.group == "sports" else DEFAULT_SCOUT_CATEGORIES
        cats = [x.strip() for x in args.categories.split(",") if x.strip()] if args.categories else default
        scout(api, alerts, whales, args.period, cats, args.top, args.max_wallets, args.out, args.summary,
              group=args.group)
    elif args.cmd == "check":
        wallet = args.wallet.strip().lower()
        if not WALLET_RE.match(wallet):
            sys.exit("That doesn't look like a wallet address (0x + 40 letters/numbers).")
        now = int(time.time())
        clf = Classifier(api, {}, get_sports_index(api, {}, now), now)
        p = profile_wallet(api, wallet, clf, now)
        label, reasons = verdict(p, alerts["block_all_sports"], now)
        last = p.get("last_trade_days")
        print(f"{p['name']}  ({wallet})\n"
              f"  Verdict:        {label}{' — ' + '; '.join(reasons) if reasons else ''}\n"
              f"  Lifetime P&L:   {money(p.get('alltime_pnl'))}\n"
              f"  Markets traded: {p.get('markets_ever') or '-'}\n"
              f"  Last trade:     {'never' if last is None else f'{last:.1f} days ago'}\n"
              f"  Typical bet:    {money(p.get('median_order'))}\n"
              f"  Sports share:   {p.get('sports_share', 0):.0%} (soccer {p.get('soccer_share', 0):.0%})\n"
              f"  Recent topics:  {p.get('topics') or '-'}")


if __name__ == "__main__":
    main()
