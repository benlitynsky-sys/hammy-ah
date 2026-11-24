#!/usr/bin/env python3
"""
plays.py - the trading plan for the primary market (WoW Forever / US / PvP / Horde AH only).

Sections
  1. Cooldown crafts    every profession's cooldown craft (recipes.csv): cost, value, profit, timing signal
  2. Daily swingers    the 20 liquid materials whose price moves most within a day
  3. Futures            calendar plays (events.csv) + the multi-day trend of each item

Usage:  python plays.py [--days 14]       writes output/plays.txt (also run hourly by forever_ah.py run)
"""
import argparse
import csv
import json
import math
import os
import statistics
import time
from collections import defaultdict
from datetime import date, datetime

import forever_ah as f

MIN_PRICE = 50          # copper; ignore penny items
MAX_JUMP = 3.0
CD_MIN_SWING = 0.10        # cooldown-craft prices: only list ones that move 10%+ within a day
MOVER_MIN_PRICE = 200       # 2s - below this, one listing swings the % wildly
MOVER_MIN_LISTED = 300000   # 30g of stock listed - a real market, not a handful of units          # moves over +300% are buyout spikes, not market moves


def load_events():
    path = os.path.join(f.HERE, "events.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        try:
            d = date.fromisoformat(r["date"].strip())
        except ValueError:
            continue
        names, subs = set(), set()
        for m in (r.get("match") or "").split(";"):
            k, _, v = m.partition(":")
            (names if k.strip() == "name" else subs if k.strip() == "subclass" else set()).add(v.strip().lower())
        out.append(dict(date=d, event=r["event"], effect=r.get("effect", "rise"), names=names, subs=subs,
                        note=r.get("note", "")))
    return out


def load_rows(con, since):
    """{item_id: [(ts, floor, qty, sold)]} - all commodities, plus watchlist history from before allsnap."""
    # The floor is the average of the cheapest 20 units. When fewer than 20 are listed it averages in every
    # overpriced listing (5 listed: 50s, 50s, 1g, 3g, 4g -> "1.7g"), so for thin markets use the cheapest one instead.
    data = defaultdict(dict)
    for ts, iid, fl, qty, sold in con.execute(
            "SELECT ts, item_id, CASE WHEN qty < 20 AND min_unit > 0 THEN min_unit ELSE floor END, qty, sold_est "
            "FROM snap WHERE ts>=?", (since,)):
        data[iid][ts] = (fl, qty, sold)
    for ts, iid, fl, qty, sold in con.execute(
            "SELECT ts, item_id, CASE WHEN qty < 20 AND min_unit > 0 THEN min_unit ELSE floor END, qty, sold_est "
            "FROM allsnap WHERE ts>=?", (since,)):
        old = data[iid].get(ts)
        # watchlist rows use per-item floor_units and sold counts; keep them when both exist
        data[iid][ts] = old if old else (fl, qty, sold)
    return {iid: sorted((ts, *v) for ts, v in d.items()) for iid, d in data.items()}


def hourly(rows):
    b = defaultdict(lambda: ([], [], 0))
    for ts, fl, qty, sold in rows:
        d = datetime.fromtimestamp(ts)
        fs, qs, s = b[(d.date(), d.hour)]
        fs.append(fl); qs.append(qty)
        b[(d.date(), d.hour)] = (fs, qs, s + (sold or 0))
    return {k: (statistics.median(fs), statistics.median(qs), s) for k, (fs, qs, s) in b.items()}


def price_at(rows, t, slack=3 * 3600):
    best = None
    for ts, fl, _, _ in rows:
        if ts <= t:
            best = (ts, fl)
        else:
            break
    return best[1] if best and t - best[0] <= slack else None


def daily_medians(rows):
    by = defaultdict(list)
    for ts, fl, _, _ in rows:
        by[datetime.fromtimestamp(ts).date()].append(fl)
    return {d: statistics.median(v) for d, v in sorted(by.items()) if len(v) >= 6}


def trend_per_day(dm):
    """Least-squares slope of log(daily median price), as a fraction per day."""
    if len(dm) < 3:
        return None
    xs = [(d - min(dm)).days for d in dm]
    ys = [math.log(v) for v in dm.values() if v > 0]
    if len(ys) != len(xs):
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return math.exp(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den) - 1 if den else None


def sold_per_day(rows):
    s = [x for _, _, _, x in rows if x is not None]
    if len(s) < 3:
        return 0
    hours = (rows[-1][0] - rows[0][0]) / 3600 or 1
    return sum(s) * 24 / hours


def build(cfg, days=14, quiet=False):
    api = f.Api(cfg)
    f.check_market(cfg, api)
    con = f.db_connect(cfg)
    now = time.time()
    rows_by = load_rows(con, int(now) - days * 86400)
    watch = f.load_watchlist()
    events = [e for e in load_events() if e["date"] >= date.today()]

    items = {}
    for iid, rows in rows_by.items():
        if len(rows) < 1 or rows[-1][0] < now - 3 * 3600:   # not listed in the latest scans
            continue
        last = rows[-1][1]
        if last < MIN_PRICE:
            continue
        items[iid] = dict(rows=rows, now=last, qty=rows[-1][2], sold=sold_per_day(rows),
                          dm=daily_medians(rows))

    # names/classes for everything that matters (cached in the iteminfo table after the first run)
    for iid in items:
        name, cls, sub, lvl, _ = f.item_info(con, api, iid)
        if iid in watch:
            name = watch[iid]["name"]
            cls = cls or "Trade Goods"
        items[iid].update(name=name, cls=cls, sub=sub or "", lvl=lvl or 0, group=group_of(iid, cls, lvl, watch))
    con.commit()
    items = {k: v for k, v in items.items() if v["cls"] in f.MATERIAL_CLASSES}
    for v in items.values():
        v["trend"] = trend_per_day(v["dm"])
    n_days = max((len(v["dm"]) for v in items.values()), default=0)

    D = dict(generated=datetime.now().strftime("%a %b %d %Y, %I:%M %p"), n_days=n_days, n_items=len(items),
             cooldowns=[], cd_prices=[], swing=[], buy=[], sell=[], events=[], falling=[], rising=[])
    L = [f"WoW Forever - Horde AH (US PvP) market report - {datetime.now():%a %Y-%m-%d %H:%M} local",
         f"Data: {n_days} day(s) of hourly scans, {len(items)} materials/consumables tracked. "
         f"Profits are after the 5% AH cut.",
         "'gone/day' = units that left the AH early (sales plus some cancelled relists, so it runs high). "
         "'Size' = ~5% of that, so you don't flood it.", ""]

    # chart window = last 7 days, or all the history we have if that's shorter
    first = min(v["rows"][0][0] for v in items.values())
    span_h = max(24, min(7 * 24, int((now - first) // 3600) + 1))
    SPARK_H = 1 if span_h <= 72 else 2            # hours per point
    n_pts = -(-span_h // SPARK_H)
    t0 = int(now // 3600 * 3600) + 3600 - n_pts * SPARK_H * 3600
    D["spark_t0"], D["spark_h"], D["spark_days"] = t0, SPARK_H, round(span_h / 24, 1)

    def spark(v):
        """Up to 7 days of the item's cheapest price, one median point per SPARK_H hours (None = no scan then)."""
        b = defaultdict(list)
        for ts, fl, _, _ in v["rows"]:
            if ts >= t0:
                b[int((ts - t0) // (SPARK_H * 3600))].append(fl)
        return [int(statistics.median(b[i])) if b.get(i) else None for i in range(n_pts)]

    def week_stats(v):
        w = [fl for ts, fl, _, _ in v["rows"] if ts >= t0]
        return dict(spark=spark(v), w_lo=min(w), w_hi=max(w), w_med=int(statistics.median(w)))

    def daily_swing(v):
        """How much the price moves within a day. Per calendar day (8+ scans): the 10th-90th percentile
        range over the day's median, so one stray buyout doesn't count. Plus the hours it is usually
        cheapest / dearest, and where today's price sits in the last 24h."""
        by = defaultdict(list)
        for ts, fl, _, _ in v["rows"]:
            by[datetime.fromtimestamp(ts).date()].append((ts, fl))
        sws, los, his, prof = [], [], [], defaultdict(list)
        for pts in by.values():
            if len(pts) < 8:
                continue
            fs = sorted(fl for _, fl in pts)
            k = len(fs) // 10
            lo, hi, med = fs[k], fs[-1 - k], statistics.median(fs)
            if med <= 0:
                continue
            sws.append((hi - lo) / med); los.append(lo); his.append(hi)
            for ts, fl in pts:
                prof[datetime.fromtimestamp(ts).hour].append(fl / med)
        if not sws:
            return None
        hp = {h: statistics.mean(x) for h, x in prof.items()}
        lo, hi = statistics.median(los), statistics.median(his)
        r24 = [fl for ts, fl, _, _ in v["rows"] if ts >= now - 86400]
        a, b = min(r24), max(r24)
        pos = (v["now"] - a) / (b - a) if b > a else 0.5
        sig = ("Buy now - near its daily low" if pos <= 0.33 else "Sell now - near its daily high" if pos >= 0.67
               else "Mid-range - wait")
        return dict(swing=statistics.median(sws), lo=int(lo), hi=int(hi), lo_h=min(hp, key=hp.get),
                    hi_h=max(hp, key=hp.get), each=int(hi * (1 - f.AH_CUT) - lo), pos=pos, signal=sig)

    def change_24h(v):
        then = price_at(v["rows"], now - 86400)
        return v["now"] / then - 1 if then else None

    # ---------------------------------------------------------------- 1. cooldown crafts
    L.append("1. COOLDOWN CRAFTS - every profession's cooldown craft, valued at tonight's prices")
    by_name = {}
    for iid, v in sorted(items.items(), key=lambda kv: -kv[1]["qty"]):
        by_name.setdefault(v["name"].lower(), iid)
    cd_ids = set()
    for rc in load_cd_recipes():
        out_id = by_name.get(rc["output"].lower())
        ins = [(by_name.get(n.lower()), n, q) for n, q in rc["inputs"]]
        cd_ids.update(i for i in [out_id] + [i for i, _, _ in ins] if i)
        row = dict(recipe=rc["recipe"], prof=rc["profession"], cd=rc["cd"], output=rc["output"], out_qty=rc["out_qty"],
                   inputs=[f"{q:g}x {n}" for n, q in rc["inputs"]],
                   missing=[n for i, n, _ in ins if not i] + ([] if out_id else [rc["output"]]))
        if not row["missing"]:
            ov = items[out_id]

            def profit_at(t, ov=ov, ins=ins, rc=rc):
                o = price_at(ov["rows"], t)
                cs = [price_at(items[i]["rows"], t) for i, _, _ in ins]
                if o is None or None in cs:
                    return None
                return o * rc["out_qty"] * (1 - f.AH_CUT) - sum(c * q for c, (_, _, q) in zip(cs, ins))
            cost = sum(items[i]["now"] * q for i, _, q in ins)
            value = ov["now"] * rc["out_qty"] * (1 - f.AH_CUT)
            prof = value - cost
            hist = [p for p in (profit_at(ts) for ts, *_ in ov["rows"]) if p is not None]
            med = statistics.median(hist) if hist else prof
            y = profit_at(now - 86400)
            if prof <= 0:
                sig = "Skip - inputs sell for more"
            elif len(hist) < 6:
                sig = "Craft - not enough history to time it"
            elif prof >= med * 1.05:
                sig = "Craft and sell now - above normal"
            elif prof < med * 0.9:
                sig = "Craft, but hold to sell - below normal"
            else:
                sig = "Craft - normal margin"
            row.update(cost=int(cost), value=int(value), profit=int(prof), median=int(med),
                       y=None if y is None else int(y),
                       per_day=int(prof * 24 / rc["cd"]) if rc["cd"] >= 20 else None, signal=sig,
                       out_now=ov["now"], out_chg=change_24h(ov), out_sold=ov["sold"])
        D["cooldowns"].append(row)
    D["cooldowns"].sort(key=lambda r: (r.get("profit") is None, -(r.get("per_day") or r.get("profit") or 0)))
    for r in D["cooldowns"]:
        if r["missing"]:
            L.append(f"  [{r['prof']}] {r['recipe']:<32} not on the AH right now: {', '.join(r['missing'])}")
            continue
        pd = f"  ~{f.money(r['per_day'])}/day" if r["per_day"] is not None else ""
        L.append(f"  [{r['prof']}] {r['recipe']:<32} cd {r['cd']:.0f}h  cost {f.money(r['cost']):>11}"
                 f"  sells {f.money(r['value']):>11}  profit {f.money(r['profit']):>11}"
                 f" (normal {f.money(r['median'])}){pd}  -> {r['signal']}")
    L.append(f"  Prices behind these crafts - only the ones that swing {CD_MIN_SWING*100:.0f}%+ within a day:")
    cd_sw = [(daily_swing(items[i]), i) for i in cd_ids]
    cd_sw = sorted([x for x in cd_sw if x[0] and x[0]["swing"] >= CD_MIN_SWING], key=lambda x: -x[0]["swing"])
    for sw, iid in cd_sw:
        v = items[iid]
        D["cd_prices"].append(dict(name=v["name"], now=v["now"], c=change_24h(v), qty=v["qty"], sold=v["sold"],
                                   **sw, **week_stats(v)))
        L.append(f"    {v['name'][:26]:<26} {f.money(v['now']):>12}  swings {sw['swing']*100:3.0f}%/day"
                 f"  low ~{sw['lo_h']:02d}:00 {f.money(sw['lo']):>11}  high ~{sw['hi_h']:02d}:00 {f.money(sw['hi']):>11}"
                 f"  -> {sw['signal']}")
    if not cd_sw:
        L.append("    (none swing that much yet)")

    # ---------------------------------------------------------------- 2. top 20 daily swingers
    L += ["", "2. TOP 20 DAILY SWINGERS - materials whose price moves the most within a day; buy the dip, sell the peak"]
    cands = []
    for iid, v in items.items():
        if v["cls"] == "Consumable" or v["now"] < MOVER_MIN_PRICE or v["qty"] * v["now"] < MOVER_MIN_LISTED:
            continue
        if v["name"].startswith("Waylaid Crate") or v["sold"] < 20 or len(v["rows"]) < 12:
            continue
        sw = daily_swing(v)
        if not sw or sw["each"] <= sw["lo"] * 0.03:          # must clear the 5% cut with something left over
            continue
        cands.append((sw["swing"], v, sw))
    cands.sort(key=lambda x: x[0], reverse=True)
    for _, v, sw in cands[:20]:
        size = max(1, int(v["sold"] * 0.05))
        c24 = change_24h(v)
        D["swing"].append(dict(name=v["name"], now=v["now"], size=size, gday=int(sw["each"] * size), c24=c24, sold=v["sold"],
                               falling=c24 is not None and c24 <= -0.15, **sw, **week_stats(v)))
        L.append(f"    {v['name'][:26]:<26} now {f.money(v['now']):>11}  swings {sw['swing']*100:3.0f}%/day"
                 f"  low ~{sw['lo_h']:02d}:00 {f.money(sw['lo']):>11}  high ~{sw['hi_h']:02d}:00 {f.money(sw['hi']):>11}"
                 f"  +{f.money(sw['each'])}/ea after cut  size {size}  -> {sw['signal']}"
                 + ("  << FALLING FAST" if D["swing"][-1]["falling"] else ""))
    if not cands:
        L.append("    (nothing swings enough yet)")
    if n_days < 4:
        L.append(f"  (Only {n_days} day(s) of scans - the low/high hours are a first read and firm up over the week.)")
    # ---------------------------------------------------------------- 3. futures
    L += ["", "3. FUTURES - stock up before calendar events (raids, new rulesets, PvP updates)"]
    for e in events:
        dleft = (e["date"] - date.today()).days
        L.append(f"  {e['date']:%a %b %d} ({dleft} days): {e['event']} -> expect {e['effect'].upper()}")
        if e["note"]:
            L.append(f"    {e['note']}")
        hits = []
        for iid, v in items.items():
            by_sub = (v["sub"] or "").lower() in e["subs"] and (v["lvl"] or 0) >= 40
            if v["name"].lower() in e["names"] or by_sub:
                rs = sorted(fl for _, fl, _, _ in v["rows"])
                pos = (v["now"] - rs[0]) / (rs[-1] - rs[0]) if rs[-1] > rs[0] else 0.5
                hits.append(((v["sold"] or v["qty"]) * v["now"], v, pos, rs[0], rs[-1]))
        hits.sort(key=lambda x: x[0], reverse=True)
        ev = dict(date=f"{e['date']:%a %b %d}", days=dleft, event=e["event"], effect=e["effect"], note=e["note"],
                  groups=[])
        D["events"].append(ev)
        for label, want in (("Consumables", True), ("Mats", False)):
            part = [h for h in hits if (h[1]["cls"] == "Consumable") == want][:12]
            if part:
                L.append(f"    {label}:")
            grp = dict(label=label, rows=[])
            ev["groups"].append(grp)
            for _, v, pos, lo, hi in part:
                tag = ("no history yet" if len(v["rows"]) < 2 else "BUY - near its low" if pos <= 0.25
                       else "wait for a dip" if pos >= 0.6 else "ok")
                grp["rows"].append(dict(name=v["name"], now=v["now"], lo=lo, hi=hi, qty=v["qty"], sold=v["sold"],
                                        tag=tag, **week_stats(v)))
                L.append(f"      {v['name'][:28]:<28} now {f.money(v['now']):>12}  range {f.money(lo)}..{f.money(hi)}"
                         f"  {v['qty']} listed  ~{v['sold']:.0f} gone/day  [{tag}]")
        if not hits and (e["names"] or e["subs"]):
            L.append("    (none of the tagged items are listed yet)")
    tr = [(v["trend"], v) for v in items.values() if v["trend"] is not None and v["sold"] >= 10]
    tr.sort(key=lambda x: x[0])
    if tr:
        L.append("  Falling steadily - sell what you hold, don't stockpile (levelling wave / more farmers):")
        for t, v in [x for x in tr if x[0] < -0.02][:8]:
            D["falling"].append(dict(name=v["name"], trend=t, now=v["now"]))
            L.append(f"    {v['name'][:26]:<26} {pct(t)}/day  now {f.money(v['now'])}")
        L.append("  Rising steadily - holding pays if it continues:")
        for t, v in [x for x in reversed(tr) if x[0] > 0.02][:8]:
            D["rising"].append(dict(name=v["name"], trend=t, now=v["now"]))
            L.append(f"    {v['name'][:26]:<26} {pct(t)}/day  now {f.money(v['now'])}")
    else:
        L.append("  Trends need 3+ full days of scans.")

    text = "\n".join(L)
    os.makedirs(cfg["OUTPUT_DIR"], exist_ok=True)
    with open(os.path.join(cfg["OUTPUT_DIR"], "plays.txt"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    with open(os.path.join(cfg["OUTPUT_DIR"], "plays.json"), "w", encoding="utf-8") as fh:
        json.dump(D, fh)
    if not quiet:
        print(text)
    return text


def group_of(iid, cls, lvl, watch):
    """endgame / mid / '' - watchlist tier first, else consumables by required level."""
    if iid in watch:
        return "endgame" if watch[iid]["tier"] == "endgame" else "mid"
    if cls == "Consumable" and lvl:
        return "endgame" if lvl >= 45 else "mid" if lvl >= 20 else ""
    return ""


def load_cd_recipes():
    """Cooldown recipes from recipes.csv, by item name (works for any listed item, not just the watchlist)."""
    out = []
    with open(os.path.join(f.HERE, "recipes.csv"), encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            cd = float(r["cooldown_hours"] or 0)
            if cd <= 0:
                continue
            ins = [(n.strip(), float(q)) for n, q in (p.rsplit(":", 1) for p in r["inputs"].split("|"))]
            out.append(dict(recipe=r["recipe"], cd=cd, output=r["output"], out_qty=float(r["output_qty"] or 1),
                            inputs=ins, profession=(r.get("profession") or "").strip() or "Other"))
    return out


def cooldown_items(watch):
    """Every item that is an output or input of a cooldown recipe in recipes.csv."""
    ids = set()
    for r in f.load_recipes(watch):
        if r["cd"] > 0:
            ids.add(r["out_id"])
            ids.update(i for i, _ in r["inputs"] if i)
    return ids


def pct(t):
    return "  n/a" if t is None else f"{t*100:+.1f}%"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=14)
    a = p.parse_args()
    build(f.load_config(), days=a.days)
