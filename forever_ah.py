#!/usr/bin/env python3
"""
forever_ah.py - WoW Forever auction house poller + daily-swing analyzer.

Zero dependencies (Python 3.9+ stdlib only).

Commands
  discover        find the WoW Forever namespace, realms and auction house ids
  verify-items    check watchlist item ids against Blizzard's item names
  resolve-items   fill in ids for Forever-only items (blank item_id rows) by name
  poll            take one snapshot (for cron / Task Scheduler)
  run             poll forever on a loop (default every 20 min, dedupes by Last-Modified)
  analyze         rank items by daily swing and print the buy/sell windows

Config comes from environment variables or a config.json next to this file:
  BLIZZ_CLIENT_ID, BLIZZ_CLIENT_SECRET   from https://develop.battle.net (free)
  REGION            us | eu                     (default us)
  NAMESPACE         e.g. dynamic-xxxxx-us        (run `discover` to find it)
  CONNECTED_REALM   connected realm id           (from `discover`)
  AUCTION_HOUSE     auction house id (Horde)     (from `discover`)
  OUTPUT_DIR        where reports go - point it at a Google Drive synced folder
"""
import argparse
import base64
import csv
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
AH_CUT = 0.05  # auction house takes 5% of the sale price


# --------------------------------------------------------------------------- config

def load_config():
    cfg = {}
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    for k in ("BLIZZ_CLIENT_ID", "BLIZZ_CLIENT_SECRET", "REGION", "NAMESPACE",
              "CONNECTED_REALM", "RULESET", "AUCTION_HOUSE", "OUTPUT_DIR", "DB_PATH",
              "LOCALE", "FLOOR_UNITS", "API_BASE"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("REGION", "us")
    cfg.setdefault("LOCALE", "en_US" if cfg["REGION"] == "us" else "en_GB")
    cfg.setdefault("OUTPUT_DIR", os.path.join(HERE, "output"))
    cfg.setdefault("DB_PATH", os.path.join(HERE, "ah.sqlite"))
    cfg["FLOOR_UNITS"] = int(cfg.get("FLOOR_UNITS", 20))
    cfg.setdefault("API_BASE", f"https://{cfg['REGION']}.api.blizzard.com")
    return cfg


def load_watchlist(include_unresolved=False):
    """{item_id: dict(name, category, floor_units, min_sold)}. Rows with a blank id
    (Forever-only items) are skipped until `resolve-items` fills them in."""
    items, unresolved = {}, []
    with open(os.path.join(HERE, "watchlist.csv"), encoding="utf-8") as f:
        for row in csv.DictReader(f):
            info = dict(name=row["name"], category=row["category"], tier=(row.get("tier") or "mid").strip(),
                        floor_units=int(row["floor_units"]) if row.get("floor_units") else None,
                        min_sold=int(row["min_sold"]) if row.get("min_sold") else None)
            if row["item_id"].strip():
                items[int(row["item_id"])] = info
            else:
                unresolved.append(info)
    return (items, unresolved) if include_unresolved else items


def load_recipes(watch):
    by_name = {v["name"]: k for k, v in watch.items()}
    out = []
    path = os.path.join(HERE, "recipes.csv")
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            inputs, ok = [], row["output"] in by_name
            for part in row["inputs"].split("|"):
                n, q = part.rsplit(":", 1)
                ok &= n in by_name
                inputs.append((by_name.get(n), float(q)))
            if ok:
                out.append(dict(recipe=row["recipe"], cd=float(row["cooldown_hours"] or 0),
                                out_id=by_name[row["output"]], out_qty=float(row["output_qty"]),
                                inputs=inputs))
    return out


# --------------------------------------------------------------------------- api

class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.token_exp = 0

    def _token(self):
        if self.token and time.time() < self.token_exp - 60:
            return self.token
        cid, sec = self.cfg.get("BLIZZ_CLIENT_ID"), self.cfg.get("BLIZZ_CLIENT_SECRET")
        if not cid or not sec:
            sys.exit("Set BLIZZ_CLIENT_ID and BLIZZ_CLIENT_SECRET (see README).")
        req = urllib.request.Request(
            "https://oauth.battle.net/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.load(r)
        self.token, self.token_exp = j["access_token"], time.time() + j.get("expires_in", 3600)
        return self.token

    def get(self, path_or_url, namespace, extra_headers=None):
        """Returns (json, headers). Raises urllib.error.HTTPError on 4xx/5xx."""
        if path_or_url.startswith("http"):
            u = urllib.parse.urlparse(path_or_url)
            q = dict(urllib.parse.parse_qsl(u.query))
            base = f"{u.scheme}://{u.netloc}{u.path}"
        else:
            q, base = {}, self.cfg["API_BASE"] + path_or_url
        q["namespace"] = namespace
        q["locale"] = self.cfg["LOCALE"]
        url = base + "?" + urllib.parse.urlencode(q)
        headers = {"Authorization": "Bearer " + self._token(), "Accept-Encoding": "identity"}
        headers.update(extra_headers or {})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                    return json.load(r), dict(r.headers)
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise


# --------------------------------------------------------------------------- discover

CANDIDATE_NAMESPACES = [
    "dynamic-forever-{r}", "dynamic-classicplus-{r}", "dynamic-classic-forever-{r}",
    "dynamic-classic1x-{r}", "dynamic-classic-{r}", "dynamic-classicann-{r}",
]


def discover_rulesets(api, ns):
    """WoW Forever has no realms: auction houses hang off rulesets (PvP, Normal, ...)."""
    try:
        idx, _ = api.get("/data/wow/ruleset/index", ns)
    except urllib.error.HTTPError:
        return False
    print(f"\n[{ns}] {len(idx.get('rulesets', []))} ruleset(s) - megaserver namespace, use RULESET")
    for ref in idx.get("rulesets", []):
        rs, _ = api.get(ref["key"]["href"], ns)
        print(f"   ruleset {rs['id']}: {rs.get('name')} ({(rs.get('status') or {}).get('name', '?')})")
        try:
            ahs, _ = api.get(f"/data/wow/ruleset/{rs['id']}/auctions/index", ns)
            for ah in ahs.get("auctions", []):
                print(f"      auction house {ah.get('id')}: {ah.get('name')}")
        except urllib.error.HTTPError as e:
            print(f"      auctions index -> HTTP {e.code}")
    return True


def cmd_discover(cfg, args):
    api = Api(cfg)
    r = cfg["REGION"]
    spaces = [args.namespace] if args.namespace else [c.format(r=r) for c in CANDIDATE_NAMESPACES]
    for ns in spaces:
        if discover_rulesets(api, ns):
            continue
        try:
            idx, _ = api.get("/data/wow/connected-realm/index", ns)
        except urllib.error.HTTPError as e:
            print(f"[{ns}] HTTP {e.code}")
            continue
        refs = idx.get("connected_realms", [])
        print(f"\n[{ns}] {len(refs)} connected realm(s)")
        for ref in refs[: args.limit]:
            try:
                cr, _ = api.get(ref["href"], ns)
            except urllib.error.HTTPError as e:
                print(f"   {ref['href']} -> HTTP {e.code}")
                continue
            names = ", ".join(x.get("name", "?") if isinstance(x.get("name"), str) else str(x.get("name"))
                              for x in cr.get("realms", []))
            print(f"   connected realm {cr['id']}: {names}")
            try:
                ahs, _ = api.get(f"/data/wow/connected-realm/{cr['id']}/auctions/index", ns)
                for ah in ahs.get("auctions", []):
                    print(f"      auction house {ah.get('id')}: {ah.get('name')}")
            except urllib.error.HTTPError as e:
                print(f"      auctions index -> HTTP {e.code}")
    print("\nPick the namespace whose realm names match WoW Forever, your ruleset's connected "
          "realm id, and the Horde auction house id. Put them in config.json.")


def cmd_verify(cfg, args):
    api = Api(cfg)
    ns = cfg.get("NAMESPACE")
    if not ns:
        sys.exit("Set NAMESPACE first (run discover).")
    static_ns = ns.replace("dynamic-", "static-", 1)
    bad = 0
    for iid, info in load_watchlist().items():
        name = info["name"]
        try:
            j, _ = api.get(f"/data/wow/item/{iid}", static_ns)
            real = j.get("name")
            real = real if isinstance(real, str) else (real or {}).get("en_US")
            flag = "" if real == name else "   <-- MISMATCH"
            bad += bool(flag)
            print(f"{iid:>6}  {name:<26} api: {real}{flag}")
        except urllib.error.HTTPError as e:
            bad += 1
            print(f"{iid:>6}  {name:<26} HTTP {e.code}")
    print(f"\n{bad} problem(s).")


def cmd_resolve(cfg, args):
    """Look up ids for watchlist rows with a blank item_id (Forever-only items) by exact name."""
    api = Api(cfg)
    ns = cfg.get("NAMESPACE")
    if not ns:
        sys.exit("Set NAMESPACE first (run discover).")
    static_ns = ns.replace("dynamic-", "static-", 1)
    path = os.path.join(HERE, "watchlist.csv")
    with open(path, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        fields, rows = rd.fieldnames, list(rd)
    loc = cfg["LOCALE"]
    for row in rows:
        if row["item_id"].strip():
            continue
        q = urllib.parse.urlencode({f"name.{loc}": row["name"], "orderby": "id", "_page": 1})
        try:
            j, _ = api.get(f"{cfg['API_BASE']}/data/wow/search/item?{q}", static_ns)
        except urllib.error.HTTPError as e:
            print(f"{row['name']}: search failed HTTP {e.code}")
            continue
        hits = [r["data"] for r in j.get("results", [])
                if (r["data"].get("name") or {}).get(loc) == row["name"]]
        if hits:
            row["item_id"] = str(hits[-1]["id"])  # newest id = most likely the Forever item
            print(f"{row['name']}: {row['item_id']}" + (f"  (also {[h['id'] for h in hits[:-1]]})" if len(hits) > 1 else ""))
        else:
            print(f"{row['name']}: not found - look it up on Wowhead (Forever) and fill it in by hand")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- storage

def db_connect(cfg, ah=None):
    """One database per auction house so markets never mix. Default: the primary (first) one."""
    ah = ah or (auction_houses(cfg) or [""])[0]
    path = cfg["DB_PATH"]
    if ah:
        root, ext = os.path.splitext(path)
        path = f"{root}_{ah}{ext}"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS snap (
            ts INTEGER, item_id INTEGER,
            floor INTEGER,      -- avg unit price of the cheapest FLOOR_UNITS units (copper)
            min_unit INTEGER,   -- single cheapest unit price
            qty INTEGER,        -- total units listed
            auctions INTEGER,   -- number of listings
            sold_est INTEGER,   -- units that vanished early since last snapshot (≈ sold)
            PRIMARY KEY (ts, item_id));
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS allsnap (   -- same columns as snap, every commodity in the feed
            ts INTEGER, item_id INTEGER, floor INTEGER, min_unit INTEGER,
            qty INTEGER, auctions INTEGER, sold_est INTEGER,
            PRIMARY KEY (ts, item_id));
        CREATE TABLE IF NOT EXISTS iteminfo (item_id INTEGER PRIMARY KEY, name TEXT,
            item_class TEXT, item_subclass TEXT, required_level INTEGER, vendor_sell INTEGER);
    """)
    return con


def meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))


# --------------------------------------------------------------------------- poll

def unit_price(a):
    q = a.get("quantity", 1) or 1
    if a.get("unit_price"):
        return a["unit_price"]
    if a.get("buyout"):
        return a["buyout"] / q
    return None  # bid-only listing, ignore


def summarize(auctions, watch, default_floor_units):
    by_item = defaultdict(list)
    for a in auctions:
        iid = a.get("item", {}).get("id")
        if iid in watch:
            up = unit_price(a)
            if up:
                by_item[iid].append((up, a.get("quantity", 1) or 1))
    out = {}
    for iid, lst in by_item.items():
        lst.sort()
        need, spent, got = watch[iid]["floor_units"] or default_floor_units, 0.0, 0
        for up, q in lst:
            take = min(q, need - got)
            spent += take * up
            got += take
            if got >= need:
                break
        out[iid] = dict(floor=round(spent / got), min_unit=round(lst[0][0]),
                        qty=sum(q for _, q in lst), auctions=len(lst))
    return out


def auction_houses(cfg):
    """AUCTION_HOUSE may be one id or a comma list; the first one is the market `analyze` reports on."""
    return [a.strip() for a in str(cfg.get("AUCTION_HOUSE", "")).split(",") if a.strip()]


def check_market(cfg, api):
    """Refuse to collect anything that isn't WoW Forever / US / PvP ruleset / Horde."""
    if cfg.get("NAMESPACE") != "dynamic-forever-us":
        sys.exit(f"NAMESPACE is {cfg.get('NAMESPACE')!r}, expected 'dynamic-forever-us' (WoW Forever US).")
    if str(cfg.get("RULESET")) != "1":
        sys.exit(f"RULESET is {cfg.get('RULESET')!r}, expected 1 (PvP).")
    rs, _ = api.get(f"/data/wow/ruleset/{cfg['RULESET']}", cfg["NAMESPACE"])
    if rs.get("name") != "PvP":
        sys.exit(f"Ruleset {cfg['RULESET']} is {rs.get('name')!r}, not PvP - stopping.")
    idx, _ = api.get(f"/data/wow/ruleset/{cfg['RULESET']}/auctions/index", cfg["NAMESPACE"])
    names = {str(a["id"]): a.get("name") for a in idx.get("auctions", [])}
    for ah in auction_houses(cfg):
        if names.get(ah) != "Horde Auction House":
            sys.exit(f"Auction house {ah} is {names.get(ah)!r}, not a Horde auction house - stopping.")


def poll_once(cfg, ah, api=None, con=None, quiet=False):
    for k in ("NAMESPACE", "AUCTION_HOUSE"):
        if not cfg.get(k):
            sys.exit(f"Set {k} (run discover).")
    if not cfg.get("RULESET") and not cfg.get("CONNECTED_REALM"):
        sys.exit("Set RULESET (WoW Forever) or CONNECTED_REALM (run discover).")
    api = api or Api(cfg)
    con = con or db_connect(cfg, ah)
    watch = load_watchlist()
    if cfg.get("RULESET"):
        # Forever: stackable goods (all the mats) live in a separate commodities feed
        path = f"/data/wow/ruleset/{cfg['RULESET']}/auctions/{ah}"
        data, headers = api.get(path + "/commodities", cfg["NAMESPACE"])
    else:
        path = f"/data/wow/connected-realm/{cfg['CONNECTED_REALM']}/auctions/{ah}"
        data, headers = api.get(path, cfg["NAMESPACE"])
    lm = headers.get("Last-Modified") or headers.get("last-modified")
    if lm and lm == meta_get(con, "last_modified"):
        if not quiet:
            print(f"{datetime.now():%Y-%m-%d %H:%M} AH {ah}: no new data yet (Last-Modified {lm})")
        return False

    auctions = data.get("auctions") or []
    commodities = list(auctions) if cfg.get("RULESET") else []
    if cfg.get("RULESET"):
        # non-stackable items (e.g. Salt Shaker) are in the regular feed
        items, _ = api.get(path, cfg["NAMESPACE"])
        auctions += [a for a in items.get("auctions") or [] if a.get("item", {}).get("id") in watch]
        gear_auctions = items.get("auctions") or []
    if not auctions:
        print(f"{datetime.now():%Y-%m-%d %H:%M} AH {ah}: API returned 0 auctions - endpoint may be down")
        return False
    ts = int(time.time())
    summary = summarize(auctions, watch, cfg["FLOOR_UNITS"])

    # sold estimate: watched listings present last time, gone now, and not about to expire
    cur = {a["id"]: (a["item"]["id"], a.get("quantity", 1) or 1, a.get("time_left", ""))
           for a in auctions if a.get("item", {}).get("id") in watch and "id" in a}
    prev = json.loads(meta_get(con, "prev_ids", "{}"))
    sold = defaultdict(int)
    if prev:
        for aid, (iid, q, tl) in prev.items():
            if int(aid) not in cur and tl != "SHORT":
                sold[iid] += q
    meta_set(con, "prev_ids", json.dumps({str(k): v for k, v in cur.items()}))

    rows = [(ts, iid, s["floor"], s["min_unit"], s["qty"], s["auctions"],
             sold.get(iid) if prev else None) for iid, s in summary.items()]
    con.executemany("INSERT OR REPLACE INTO snap VALUES (?,?,?,?,?,?,?)", rows)
    n_all = save_all_commodities(con, ah, ts, commodities, cfg["FLOOR_UNITS"]) if commodities else 0
    if cfg.get("RULESET"):
        save_gear(con, ts, gear_auctions)
    if lm:
        meta_set(con, "last_modified", lm)
    con.commit()
    if not quiet:
        print(f"{datetime.now():%Y-%m-%d %H:%M} AH {ah}: saved {len(rows)} watchlist items"
              f" + {n_all} commodities from {len(auctions)} auctions")
    return True


def save_gear(con, ts, auctions):
    """Snapshot the regular (non-stackable: weapons, armor, ...) feed: cheapest buyout per item id."""
    con.execute("CREATE TABLE IF NOT EXISTS gearsnap (ts INTEGER, item_id INTEGER, min_buyout INTEGER,"
                " median_buyout INTEGER, auctions INTEGER, PRIMARY KEY (ts, item_id))")
    by_item = defaultdict(list)
    for a in auctions:
        iid = a.get("item", {}).get("id")
        up = unit_price(a)
        if iid is not None and up:
            by_item[iid].append(up)
    rows = []
    for iid, lst in by_item.items():
        lst.sort()
        rows.append((ts, iid, round(lst[0]), round(lst[len(lst) // 2]), len(lst)))
    con.executemany("INSERT OR REPLACE INTO gearsnap VALUES (?,?,?,?,?)", rows)
    return len(rows)


def save_all_commodities(con, ah, ts, auctions, floor_units):
    """Snapshot every commodity in the feed (this market only) into allsnap, for the movers scan."""
    by_item = defaultdict(list)
    cur = {}
    for a in auctions:
        iid = a.get("item", {}).get("id")
        up = unit_price(a)
        if iid is None or not up:
            continue
        q = a.get("quantity", 1) or 1
        by_item[iid].append((up, q))
        if "id" in a:
            cur[str(a["id"])] = (iid, q, a.get("time_left", ""))
    prev_path = os.path.join(HERE, f"prev_all_{ah}.json")
    prev = {}
    if os.path.exists(prev_path):
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev = json.load(f)
        except ValueError:
            prev = {}
    sold = defaultdict(int)
    for aid, (iid, q, tl) in prev.items():
        if aid not in cur and tl != "SHORT":
            sold[iid] += q
    with open(prev_path, "w", encoding="utf-8") as f:
        json.dump(cur, f)
    rows = []
    for iid, lst in by_item.items():
        lst.sort()
        spent, got = 0.0, 0
        for up, q in lst:
            take = min(q, floor_units - got)
            spent += take * up
            got += take
            if got >= floor_units:
                break
        rows.append((ts, iid, round(spent / got), round(lst[0][0]), sum(q for _, q in lst), len(lst),
                     sold.get(iid) if prev else None))
    con.executemany("INSERT OR REPLACE INTO allsnap VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def cmd_poll(cfg, args):
    api = Api(cfg)
    if cfg.get("RULESET"):
        check_market(cfg, api)
    for ah in auction_houses(cfg):
        poll_once(cfg, ah, api)


def cmd_run(cfg, args):
    api = Api(cfg)
    if cfg.get("RULESET"):
        check_market(cfg, api)
    cons = {ah: db_connect(cfg, ah) for ah in auction_houses(cfg)}
    last_report = 0
    print(f"Polling auction house(s) {', '.join(cons)} every {args.every} min. Ctrl+C to stop.")
    while True:
        for ah, con in cons.items():
            try:
                if poll_once(cfg, ah, api, con) and ah == "113":
                    try:  # fresh scan -> reports, alerts, Discord, in a new process so code edits take effect
                        import subprocess
                        subprocess.run([sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hourly.py")],
                                       timeout=900)
                    except Exception as e:
                        print(f"hourly jobs failed: {e}")
            except Exception as e:  # keep going through API outages
                print(f"{datetime.now():%Y-%m-%d %H:%M} AH {ah}: poll failed: {e}")
        if time.time() - last_report > 3600:
            try:
                analyze(cfg, days=args.days, quiet=True)
                movers(cfg, days=7, quiet=True)
                import plays
                plays.build(cfg, days=args.days, quiet=True)
                last_report = time.time()
            except Exception as e:
                print(f"report failed: {e}")
        time.sleep(args.every * 60)


# --------------------------------------------------------------------------- analyze

def money(c):
    if c is None:
        return "-"
    c = int(round(c))
    g, s, cc = c // 10000, (c // 100) % 100, c % 100
    return (f"{g}g " if g else "") + (f"{s}s " if g or s else "") + f"{cc}c"


def hourly_series(con, iid, since):
    """{(date, hour): (floor_median, qty_median, sold_sum)} in this PC's local time."""
    b = defaultdict(lambda: ([], [], 0))
    for ts, fl, qty, sold in con.execute(
            "SELECT ts, floor, qty, sold_est FROM snap WHERE item_id=? AND ts>=? ORDER BY ts",
            (iid, since)):
        d = datetime.fromtimestamp(ts)
        f, q, s = b[(d.date(), d.hour)]
        f.append(fl); q.append(qty)
        b[(d.date(), d.hour)] = (f, q, s + (sold or 0))
    return {k: (statistics.median(f), statistics.median(q), s) for k, (f, q, s) in b.items()}


def analyze_item(series, min_days):
    keys = sorted(series)
    days = sorted({d for d, _ in keys})
    if len(days) < min_days:
        return None

    # detrend: each hourly price relative to the median of the surrounding 24h
    pts = [(datetime.combine(d, datetime.min.time()).timestamp() + h * 3600, series[(d, h)][0], (d, h))
           for d, h in keys]
    rel = {}
    for t, p, k in pts:
        window = [pp for tt, pp, _ in pts if abs(tt - t) <= 12 * 3600]
        rel[k] = p / statistics.median(window)

    # daily swing: (max - min) / min of hourly prices within each day
    swings = []
    for d in days:
        ps = [series[(d, h)][0] for h in range(24) if (d, h) in series]
        if len(ps) >= 12:
            swings.append((max(ps) - min(ps)) / min(ps))
    if not swings:
        return None

    # hour-of-day profile
    prof = defaultdict(list)
    for (d, h), r in rel.items():
        prof[h].append(r)
    profile = {h: statistics.median(v) for h, v in prof.items() if len(v) >= max(2, min_days // 2)}

    # best buy-hour -> sell-hour round trip (sell within 1-23h after buying)
    best = None
    for b in profile:
        for s in profile:
            if s == b:
                continue
            lag = (s - b) % 24
            trades = []
            for d in days:
                if (d, b) not in series:
                    continue
                t_sell = datetime.combine(d, datetime.min.time()).timestamp() + (b + lag) * 3600
                ds = datetime.fromtimestamp(t_sell)
                k2 = (ds.date(), ds.hour)
                if k2 in series:
                    buy, sell = series[(d, b)][0], series[k2][0]
                    trades.append((sell * (1 - AH_CUT) / buy - 1, sell * (1 - AH_CUT) - buy))
            if len(trades) < max(3, min_days // 2):
                continue
            med = statistics.median(t for t, _ in trades)
            med_g = statistics.median(g for _, g in trades)
            trades = [t for t, _ in trades]
            hit = sum(t > 0 for t in trades) / len(trades)
            score = med * hit
            if best is None or score > best["score"]:
                best = dict(buy_hour=b, sell_hour=s, edge=med, edge_c=med_g, hit=hit, n=len(trades), score=score)
    if not best:
        return None

    # typical prices at those hours (recent days), volume
    def typical(h):
        v = [series[(d, h)][0] for d in days[-7:] if (d, h) in series]
        return statistics.median(v) if v else None

    sold_day = []
    for d in days:
        s = [series[(d, h)][2] for h in range(24) if (d, h) in series]
        if len(s) >= 12:
            sold_day.append(sum(s))
    qty_now = series[keys[-1]][1]
    return dict(days=len(days), swing=statistics.median(swings), **best,
                buy_price=typical(best["buy_hour"]), sell_price=typical(best["sell_hour"]),
                sold_per_day=statistics.median(sold_day) if sold_day else 0,
                qty_now=qty_now, last_price=series[keys[-1]][0], last_key=keys[-1])


def analyze(cfg, days=14, min_days=5, min_sold=40, min_hit=0.6, quiet=False):
    con = db_connect(cfg)
    watch = load_watchlist()
    since = int(time.time()) - days * 86400
    results = []
    series_cache = {}
    for iid, info in watch.items():
        ser = hourly_series(con, iid, since)
        if not ser:
            continue
        series_cache[iid] = ser
        r = analyze_item(ser, min_days)
        if r:
            results.append(dict(item_id=iid, name=info["name"], category=info["category"], tier=info["tier"],
                                need_sold=info["min_sold"] if info["min_sold"] is not None else min_sold, **r))

    picks = [r for r in results if r["sold_per_day"] >= r["need_sold"] and r["hit"] >= min_hit and r["edge"] > 0]
    picks.sort(key=lambda r: r["score"], reverse=True)
    rest = sorted([r for r in results if r not in picks], key=lambda r: r["swing"], reverse=True)

    lines = [f"WoW Forever AH report - {datetime.now():%a %Y-%m-%d %H:%M} (local time)",
             f"Data: last {days} days. Edge is after the 5% AH cut. Hit = share of days the trade made money.",
             ""]
    if not results:
        lines.append(f"Not enough data yet - need at least {min_days} days of hourly snapshots.")
    else:
        def trade_line(r):
            now = ""
            if r["buy_price"] and r["last_price"] <= r["buy_price"]:
                now = "  << at/below buy price right now"
            return (f"  {r['name']:<24} buy ~{r['buy_hour']:02d}:00 at {money(r['buy_price']):>10} -> "
                    f"sell ~{r['sell_hour']:02d}:00 at {money(r['sell_price']):>10} | "
                    f"edge {r['edge']*100:+.1f}% ({money(r['edge_c'])}/unit)  hit {r['hit']*100:.0f}%  "
                    f"daily swing {r['swing']*100:.0f}%  ~{r['sold_per_day']:.0f} sold/day{now}")

        end_picks = [r for r in picks if r["tier"] == "endgame"]
        mid_picks = [r for r in picks if r["tier"] != "endgame"]
        lines.append("TRADE LIST - ENDGAME (enough volume, consistent swing)")
        lines += [trade_line(r) for r in end_picks] or ["  nothing passes the filters yet"]
        lines += ["", "MID-LEVEL - daily cycle only; buy and sell the same day, don't hold"]
        lines += [trade_line(r) for r in mid_picks] or ["  none cycling consistently"]
        lines += ["", "WATCHING (swing but fails volume/consistency)"]
        for r in rest[:15]:
            lines.append(f"  {r['name']:<24} [{r['tier']}] swing {r['swing']*100:.0f}%  edge {r['edge']*100:+.1f}%  "
                         f"hit {r['hit']*100:.0f}%  ~{r['sold_per_day']:.0f} sold/day")
    # cooldown / craft spreads: buy inputs, craft, sell output
    spreads = recipe_spreads(load_recipes(watch), series_cache)
    if spreads:
        lines += ["", "CRAFT SPREADS (sell output - buy inputs, after 5% cut)"]
        for sp in spreads:
            per_day = f"  ~{money(sp['per_day'])}/day on cooldown" if sp["cd"] >= 1 and sp["now"] is not None else ""
            lines.append(
                f"  {sp['recipe']:<32} now {money(sp['now']) if sp['now'] is not None else '-':>10} "
                f"({sp['now_pct']*100:+.0f}%)  median {money(sp['median']):>10}  "
                f"range {money(sp['lo'])} .. {money(sp['hi'])}{per_day}"
                if sp["now"] is not None else f"  {sp['recipe']:<32} no current price for all reagents")
    text = "\n".join(lines)

    os.makedirs(cfg["OUTPUT_DIR"], exist_ok=True)
    with open(os.path.join(cfg["OUTPUT_DIR"], "ah_report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    fields = ["item_id", "name", "category", "tier", "days", "swing", "buy_hour", "sell_hour", "edge", "edge_c", "hit",
              "n", "buy_price", "sell_price", "sold_per_day", "qty_now", "last_price"]
    with open(os.path.join(cfg["OUTPUT_DIR"], "ah_report.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields + ["pick"], extrasaction="ignore")
        w.writeheader()
        for r in picks + rest:
            w.writerow({**r, "pick": r in picks})
    # raw hourly export so the data can be re-analyzed elsewhere
    with open(os.path.join(cfg["OUTPUT_DIR"], "ah_hourly.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["utc_ts", "local_time", "item_id", "floor_copper", "min_unit", "qty", "auctions", "sold_est"])
        for row in con.execute("SELECT * FROM snap WHERE ts>=? ORDER BY ts, item_id", (since,)):
            w.writerow([row[0], datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M")] + list(row[1:]))
    try:
        write_xlsx(os.path.join(cfg["OUTPUT_DIR"], XLSX_NAME), picks, rest, spreads, con, since, watch)
    except PermissionError:
        print("Workbook is open/locked - skipped this update, will retry next hour.")
    except ImportError:
        print("openpyxl not installed - run `pip install openpyxl` to get the Excel workbook.")
    if not quiet:
        print(text)
    return picks, rest


# --------------------------------------------------------------------------- excel

XLSX_NAME = "Forever AH Tracker.xlsx"


def write_xlsx(path, picks, rest, spreads, con=None, since=0, watch=None):
    """Build the workbook. Prices are in gold (copper / 10000). Edge/margin columns are
    live formulas off the price columns and the AH-cut cell on the Settings sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    F = "Arial"
    head_font = Font(name=F, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", start_color="1F3A5F")
    body = Font(name=F)
    blue = Font(name=F, color="0000FF")
    yellow = PatternFill("solid", start_color="FFFF00")
    G, PCT, INT = '#,##0.00"g"', "0.0%", "#,##0"
    g = lambda c: None if c is None else round(c / 10000, 4)

    wb = Workbook()
    st = wb.active
    st.title = "Settings"

    def table(ws, title, headers, widths, rows, formats, note=None):
        ws["A1"] = title
        ws["A1"].font = Font(name=F, bold=True, size=14)
        ws["A2"] = f"Updated {datetime.now():%a %Y-%m-%d %H:%M} (PC local time)"
        ws["A2"].font = Font(name=F, italic=True, color="666666")
        hr = 4
        for i, (h, w) in enumerate(zip(headers, widths), 1):
            c = ws.cell(hr, i, h)
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[hr].height = 30
        if not rows:
            ws.cell(hr + 1, 1, note or "No data yet - leave forever_ah.py running; this fills in after ~5 days.").font = \
                Font(name=F, italic=True, color="999999")
        for r, row in enumerate(rows, hr + 1):
            for i, v in enumerate(row, 1):
                if isinstance(v, str) and "{r}" in v:
                    v = v.format(r=r)
                c = ws.cell(r, i, v)
                c.font = body
                if formats[i - 1]:
                    c.number_format = formats[i - 1]
        ws.freeze_panes = ws.cell(hr + 1, 2)
        last = hr + max(len(rows), 1)
        ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(headers))}{last}"

    cut = "Settings!$B$4"

    # Trade List (endgame) and Mid-level Trades: buy at buy-hour floor, sell at sell-hour floor
    def trade_rows(xs):
        return [[x["name"], x["category"], f"{x['buy_hour']:02d}:00", g(x["buy_price"]),
                 f"{x['sell_hour']:02d}:00", g(x["sell_price"]),
                 f"=F{{r}}*(1-{cut})-D{{r}}", f"=IF(D{{r}}>0,G{{r}}/D{{r}},0)",
                 x["hit"], x["swing"], round(x["sold_per_day"]), g(x["last_price"]),
                 f'=IF(L{{r}}<=D{{r}},"BUY ZONE","")', x["days"]] for x in xs]

    trade_headers = ["Item", "Type", "Buy hour", "Typical buy price", "Sell hour", "Typical sell price",
                     "Profit / unit (after cut)", "Profit %", "Hit rate", "Daily swing", "Est. sold / day",
                     "Price now", "Signal", "Days of data"]
    trade_widths = [24, 12, 9, 12, 9, 12, 13, 10, 9, 10, 10, 12, 11, 8]
    trade_fmts = [None, None, None, G, None, G, G, PCT, PCT, PCT, INT, G, None, INT]
    table(wb.create_sheet("Trade List"), "Trade List - endgame mats: buy low hour, sell high hour",
          trade_headers, trade_widths, trade_rows([x for x in picks if x.get("tier") == "endgame"]), trade_fmts)
    table(wb.create_sheet("Mid-level Trades"), "Mid-level mats - same-day flips only, don't hold (prices trending down)",
          trade_headers, trade_widths, trade_rows([x for x in picks if x.get("tier") != "endgame"]), trade_fmts,
          note="No mid-level mat is cycling consistently yet.")

    # Craft Spreads
    cs_rows = []
    for x in spreads:
        cs_rows.append([x["recipe"], x["cd"] or None,
                        g(x["now"]) if x["now"] is not None else None, g(x["median"]), g(x["lo"]), g(x["hi"]),
                        f'=IF(AND(ISNUMBER(C{{r}}),B{{r}}>=1),C{{r}}*24/B{{r}},"")'])
    table(wb.create_sheet("Craft Spreads"), "Craft Spreads - buy reagents, craft, sell (after AH cut)",
          ["Recipe", "Cooldown (hrs)", "Margin now", "Median margin", "Worst", "Best", "Gold / day on cooldown"],
          [32, 11, 12, 12, 12, 12, 14], cs_rows, [None, INT, G, G, G, G, G])

    # Watching
    w_rows = [[x["name"], x["category"], x.get("tier"), x["swing"], x["edge"], x["hit"], round(x["sold_per_day"]),
               x.get("need_sold"), x["days"]] for x in rest]
    table(wb.create_sheet("Watching"), "Watching - swings, but fails volume or consistency",
          ["Item", "Type", "Tier", "Daily swing", "Best edge", "Hit rate", "Est. sold / day", "Needs sold / day", "Days"],
          [24, 12, 9, 10, 10, 9, 11, 11, 8], w_rows, [None, None, None, PCT, PCT, PCT, INT, INT, INT])

    # Hourly Prices (raw)
    h_rows = []
    if con is not None:
        names = {k: v["name"] for k, v in (watch or {}).items()}
        for ts, iid, fl, mn, qty, auc, sold in con.execute(
                "SELECT * FROM snap WHERE ts>=? ORDER BY ts, item_id", (since,)):
            h_rows.append([datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"), names.get(iid, iid),
                           g(fl), g(mn), qty, auc, sold])
    table(wb.create_sheet("Hourly Prices"), "Hourly snapshots (raw data)",
          ["Local time", "Item", "Floor price", "Cheapest unit", "Units listed", "Listings", "Est. sold since last"],
          [17, 24, 12, 12, 11, 9, 12], h_rows, [None, None, G, G, INT, INT, INT],
          note="No snapshots yet.")

    # Settings / legend
    st["A1"] = "WoW Forever AH Tracker - Horde"
    st["A1"].font = Font(name=F, bold=True, size=14)
    st["A2"] = "Rewritten every hour by forever_ah.py. Don't add your own tabs or notes here - they get overwritten."
    st["A2"].font = Font(name=F, italic=True, color="666666")
    st["A4"], st["B4"] = "Auction house cut", AH_CUT
    st["A4"].font, st["B4"].font, st["B4"].fill, st["B4"].number_format = body, blue, yellow, PCT
    st["C4"] = "Blizzard default 5%. Edit here to stress-test (resets on next rewrite)."
    st["C4"].font = Font(name=F, color="666666")
    legend = [
        ("Trade List", "Endgame mats worth trading: enough volume + the swing repeats most days."),
        ("Mid-level Trades", "Leveling mats that still cycle daily. Sell the same day - their prices are trending down."),
        ("Typical buy/sell price", "Median over the last 7 days of the avg cost of the cheapest units (2-20 per item)."),
        ("Profit / unit", "Sell price after the AH cut minus buy price. Deposits not included."),
        ("Hit rate", "Share of days the buy-hour -> sell-hour trade made money."),
        ("Daily swing", "Median (high - low) / low within a day."),
        ("Est. sold / day", "Listings that vanished before expiring. Cancels inflate it a little."),
        ("BUY ZONE", "Current price is at/below the typical buy price."),
        ("Craft Spreads", "Output price after cut minus reagent cost, per craft."),
        ("Prices", "In gold: 0.50g = 50 silver."),
    ]
    st["A6"] = "Legend"
    st["A6"].font = Font(name=F, bold=True)
    for i, (k, v) in enumerate(legend, 7):
        st.cell(i, 1, k).font = Font(name=F, bold=True)
        st.cell(i, 2, v).font = body
    st.column_dimensions["A"].width = 22
    st.column_dimensions["B"].width = 12
    wb.move_sheet("Settings", offset=len(wb.sheetnames) - 1)
    wb.active = 0
    wb.save(path)  # saved in place so Drive keeps the same file


def recipe_spreads(recipes, series_cache):
    out = []
    for rc in recipes:
        ids = [rc["out_id"]] + [i for i, _ in rc["inputs"]]
        if not all(i in series_cache for i in ids):
            continue
        common = set.intersection(*(set(series_cache[i]) for i in ids))
        if not common:
            continue
        def margin(k):
            cost = sum(series_cache[i][k][0] * q for i, q in rc["inputs"])
            return series_cache[rc["out_id"]][k][0] * rc["out_qty"] * (1 - AH_CUT) - cost, cost
        ms = [margin(k) for k in sorted(common)]
        last_k = max(common)
        now, cost_now = margin(last_k)
        fresh = last_k == max(series_cache[rc["out_id"]])
        out.append(dict(recipe=rc["recipe"], cd=rc["cd"], median=statistics.median(m for m, _ in ms),
                        lo=min(m for m, _ in ms), hi=max(m for m, _ in ms),
                        now=now if fresh else None, now_pct=(now / cost_now) if cost_now else 0,
                        per_day=(now * 24 / rc["cd"]) if rc["cd"] >= 1 else None))
    out.sort(key=lambda x: (x["per_day"] if x["per_day"] is not None else x["median"]), reverse=True)
    return out


MATERIAL_CLASSES = {"Trade Goods", "Tradeskill", "Reagent", "Gem", "Consumable"}


def item_info(con, api, iid):
    row = con.execute("SELECT name, item_class, item_subclass, required_level, vendor_sell FROM iteminfo "
                      "WHERE item_id=?", (iid,)).fetchone()
    if row:
        return row
    try:
        it, _ = api.get(f"/data/wow/item/{iid}", "static-forever-us")
    except urllib.error.HTTPError:
        it = {}
    def s(v):
        return v.get("name", "") if isinstance(v, dict) else (v or "")
    sell = it.get("sell_price")
    sell = sell if isinstance(sell, int) else (sell or {}).get("value", 0) if isinstance(sell, dict) else 0
    row = (s(it.get("name")) or f"item {iid}", s(it.get("item_class")), s(it.get("item_subclass")),
           it.get("required_level") or 0, sell or 0)
    con.execute("INSERT OR REPLACE INTO iteminfo VALUES (?,?,?,?,?,?)", (iid, *row))
    return row


def movers(cfg, days=7, top=40, everything=False, min_gold_day=0.5, quiet=False):
    """Rank every commodity on this market by how much gold its daily swing could make."""
    con = db_connect(cfg)
    api = Api(cfg)
    since = int(time.time()) - days * 86400
    data = defaultdict(list)
    for ts, iid, fl, qty, sold in con.execute(
            "SELECT ts, item_id, floor, qty, sold_est FROM allsnap WHERE ts>=? ORDER BY ts", (since,)):
        data[iid].append((ts, fl, qty, sold))
    n_scans = con.execute("SELECT COUNT(DISTINCT ts) FROM allsnap WHERE ts>=?", (since,)).fetchone()[0]
    watch = load_watchlist()
    cands = []
    for iid, rows in data.items():
        if len(rows) < max(6, n_scans // 2):
            continue
        by_day = defaultdict(list)
        for ts, fl, qty, sold in rows:
            by_day[datetime.fromtimestamp(ts).date()].append(fl)
        swings = [max(v) / min(v) - 1 for v in by_day.values() if len(v) >= 6 and min(v) > 0]
        if not swings:
            continue
        floors = sorted(fl for _, fl, _, _ in rows)
        p20, p80 = floors[len(floors) // 5], floors[len(floors) * 4 // 5]
        if p20 <= 0 or p80 / p20 - 1 > 3.0:  # >300% looks like a buyout spike, not a cycle
            continue
        solds = [s for _, _, _, s in rows if s is not None]
        hours = (rows[-1][0] - rows[0][0]) / 3600 or 1
        sold_day = sum(solds) * 24 / hours if solds else 0
        cap = sold_day * 0.10 * (p80 * (1 - AH_CUT) - p20)  # gold/day if you handle 10% of sales
        cands.append(dict(item_id=iid, swing=statistics.median(swings), days=len(swings), p20=p20, p80=p80,
                          sold_day=sold_day, cap=cap, now=rows[-1][1], qty=rows[-1][2]))
    cands.sort(key=lambda r: r["cap"], reverse=True)
    out = []
    for r in cands:
        if r["cap"] < min_gold_day * 10000 or len(out) >= top:
            break
        name, cls, sub, lvl, _ = item_info(con, api, r["item_id"])
        if not everything and cls not in MATERIAL_CLASSES:
            continue
        out.append(dict(r, name=name, cls=cls, sub=sub, lvl=lvl, watched=r["item_id"] in watch))
    con.commit()

    span = f"{days} days" if n_scans >= days * 20 else f"{n_scans} scans so far"
    lines = [f"Biggest movers - every commodity on this market - {datetime.now():%a %Y-%m-%d %H:%M} ({span})",
             "gold/day = if you handled 10% of sales, buying at the usual low (p20) and selling at the usual "
             "high (p80), after the 5% cut. * = already on the watchlist.", ""]
    for r in out:
        pos = (r["now"] - r["p20"]) / (r["p80"] - r["p20"]) if r["p80"] > r["p20"] else 0
        where = "LOW now" if pos <= 0.25 else "high now" if pos >= 0.75 else ""
        lines.append(f"{'*' if r['watched'] else ' '} {r['name'][:28]:<28} {r['sub'][:14]:<14} "
                     f"{money(int(r['cap'])):>11}/day  low {money(r['p20']):>10}  high {money(r['p80']):>10}  "
                     f"swing {r['swing']*100:3.0f}% ({r['days']}d)  ~{r['sold_day']:.0f} sold/day  "
                     f"now {money(r['now'])} {where}")
    if not out:
        lines.append("Not enough all-commodity scans yet - give it a few hours.")
    text = "\n".join(lines)
    os.makedirs(cfg["OUTPUT_DIR"], exist_ok=True)
    with open(os.path.join(cfg["OUTPUT_DIR"], "movers.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    if not quiet:
        print(text)
    return out


def cmd_movers(cfg, args):
    movers(cfg, days=args.days, top=args.top, everything=args.all)


def cmd_analyze(cfg, args):
    analyze(cfg, days=args.days, min_days=args.min_days, min_sold=args.min_sold, min_hit=args.min_hit)


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("--namespace"); d.add_argument("--limit", type=int, default=25)
    sub.add_parser("verify-items")
    sub.add_parser("resolve-items")
    sub.add_parser("poll")
    r = sub.add_parser("run"); r.add_argument("--every", type=int, default=20); r.add_argument("--days", type=int, default=14)
    a = sub.add_parser("analyze")
    a.add_argument("--days", type=int, default=14)
    a.add_argument("--min-days", type=int, default=5)
    a.add_argument("--min-sold", type=int, default=40, help="min est. units sold per day")
    a.add_argument("--min-hit", type=float, default=0.6, help="min share of days the trade was profitable")
    m = sub.add_parser("movers", help="rank every commodity on the market by daily-swing gold")
    m.add_argument("--days", type=int, default=7)
    m.add_argument("--top", type=int, default=40)
    m.add_argument("--all", action="store_true", help="include non-materials (gear bits, misc)")
    args = p.parse_args()
    cfg = load_config()
    {"discover": cmd_discover, "verify-items": cmd_verify, "resolve-items": cmd_resolve, "poll": cmd_poll,
     "run": cmd_run, "analyze": cmd_analyze, "movers": cmd_movers}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
