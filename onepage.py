#!/usr/bin/env python3
"""onepage.py - one-page Horde AH market report (WoW Forever, US, PvP ruleset, AH 113 only).

Sections: 1 Raid consumables + their materials (daily high/low, last 7 days)
          2 Flip list (items 40s+ that go up AND down every day, best flip profit first)
          3 Profession cooldowns (outputs, inputs, daily high/low)
          4 Cheapest 1-150 route per profession
Writes output/Hammys <Morning|Mid Day|Evening> Market Report.pdf (by the time it's built), auto-shrinking until it fits on one letter page."""
import csv
import json
import os
import re
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime

import report as R

HERE = R.HERE
DAYS = 14   # day-by-day columns shown (data for these is kept in the working file; older days live in the archive)
PRICE_BADGES = [(1.20, "sell", "Sell"), (1.10, "mup", "Moving up"), (0.80, "buy", "Buy"), (0.90, "mdn", "Moving down")]
POT_DIP = 0.85         # Potential flip only when the price is at least 15% under its weekly usual
FLIP_MIN = 4000          # 40s
FLIP_SWING = 0.07        # a leg must be 7%+ to count (what a flip needs to beat the 5% cut)
RAID_MAT_MIN = 500       # leave out materials under 5s (vials, spices, cheap fish)
TOP_FLIPS = 20
# One-time-buy items (tools) never go on the lists - their price only rises on stock buyouts:
DURABLE = {"salt shaker", "sewing machine", "spinning wheel", "molten foundry", "delicate arcanite converter"}   # tools: bought once, never used up

# camp features that drop in dungeons (GameFragger camping guide, Nov 22 2026). (profession, (mid item, boss, place), (end-game item, boss, place))
CAMP = [
    ("Alchemy", ("Fermenter", "Rath'mael", "Ruins of Lordaeron"), ("Alchemy Laboratory", "Ras Frostwhisper", "Scholomance")),
    ("Blacksmithing", ("Anvil", "Gilnid", "Deadmines"), ("Master Forge", "Golem Lord Argelmach", "Blackrock Depths")),
    ("Cooking", ("Cookie's Feast", "Cookie", "Deadmines"), ("Iron Oven", "Quartermaster Zigris", "Blackrock Spire")),
    ("Enchanting", ("Arcane Salvager", "Charlga Razorflank", "Razorfen Kraul"), ("Arcane Forge", "Nanaya", "Shapers Terrace")),
    ("Engineering", ("Repair Bot", "Sneed", "Deadmines"), ("Anarchist's Workbench", "Ezra Grimm", "Stratholme")),
    ("First Aid", ("Toxin Study", "Commander Springvale", "Shadowfang Keep"), ("Plague Doctor's Laboratory", "Doctor Theolen Krastinov", "Scholomance")),
    ("Fishing", ("Fishing Rack", "", "Blackfathom Deeps"), ("Fishing Hut", "Jammal'an the Prophet", "Sunken Temple")),
    ("Herbalism", ("Greenhouse", "Shadetooth", "Wetlands excavation"), ("Seed Hybridizer", "Tendris Warpwood", "Dire Maul")),
    ("Leatherworking", ("Tanning Rack", "Razorclaw the Butcher", "Shadowfang Keep"), ("Sewing Machine", "Vectus", "Scholomance")),
    ("Mining", ("Rock Garden", "", ""), ("Molten Foundry", "Relic Guardian", "")),
    ("Skinning", ("Field Guide", "Lady Anacondra", "Wailing Caverns"), ("Trapper's Workbench", "The Beast", "Blackrock Spire")),
    ("Tailoring", ("Spinning Wheel", "Unstable Sentinel", "Dalaran"), ("Loom", "Baroness Anastari", "Stratholme")),
]
DURABLE |= {x[0].lower() for _, *tiers in CAMP for x in tiers}   # camp features are tools too


FLIP_STACK_MIN = 10000   # a flip has to make 1g+ on one full stack
STACK_CSV = os.path.join(HERE, "stack_sizes.csv")          # your overrides: item,stack
STACK_CACHE = os.path.join(HERE, "stackable_cache.json")   # Blizzard's is_stackable flag per item
_stackable = None


def stack_size(m, iid):
    """Classic stack size by item type; non-stackable = 1; stack_sizes.csv wins."""
    global _stackable
    it = m.items.get(iid) or {}
    name = (it.get("name") or "").lower()
    if os.path.exists(STACK_CSV):
        for r in csv.DictReader(open(STACK_CSV, encoding="utf-8")):
            if r["item"].strip().lower() == name:
                return int(r["stack"])
    if _stackable is None:
        _stackable = json.load(open(STACK_CACHE)) if os.path.exists(STACK_CACHE) else {}
    if str(iid) not in _stackable:
        try:
            import forever_ah as f
            d, _ = f.Api(f.load_config()).get(f"/data/wow/item/{iid}", "static-forever-us")
            _stackable[str(iid)] = bool(d.get("is_stackable"))
        except Exception:
            _stackable[str(iid)] = True
        json.dump(_stackable, open(STACK_CACHE, "w"))
    if not _stackable[str(iid)]:
        return 1
    sub, cls = it.get("sub") or "", it.get("cls") or ""
    if sub in ("Potion", "Elixir"):
        return 5
    if sub == "Flask":
        return 1
    if sub == "Elemental" or (sub == "Enchanting" and "essence" in name):
        return 10
    if sub == "Metal & Stone" and name.endswith(" ore"):
        return 10
    if name.startswith("bolt of") or name.startswith("cured "):
        return 10
    return 20


def one_time_buy(m, iid):
    """Tools: a character buys one and never needs another, so the price only jumps on buyouts."""
    it = m.items.get(iid) or {}
    return (it.get("name") or "").lower() in DURABLE or it.get("sub") == "Device"
TOP_RAID = 20           # consumables and materials shown in section 1 (the top players only)


def part_of_day(h):
    return "Morning" if h < 12 else "Mid Day" if h < 17 else "Evening"


# --------------------------------------------------------------------------- helpers

def sm(c):
    """Short money: 16.6g, 9.99g, 85s, 4.6s, 45c."""
    if c is None:
        return "-"
    c = round(c)
    if c >= 100000:
        return f"{c / 10000:.1f}g"
    if c >= 9950:   # 99.5s+ would round to "100s" - show it as gold
        return f"{c / 10000:.2f}g"
    if c >= 1000:
        return f"{c / 100:.0f}s"
    if c >= 100:
        return f"{c / 100:.1f}s"
    return f"{c}c"


def clean(m, iid):
    """Hourly series with one-hour listing gaps removed (points 2.5x above / 0.4x below the item's median)."""
    s = m.series(iid)
    if len(s) < 3:
        return s
    med = statistics.median(p for _, p in s)
    return [(t, p) for t, p in s if 0.4 * med <= p <= 2.5 * med]


def by_day(s):
    d = defaultdict(list)
    for t, p in s:
        d[datetime.fromtimestamp(t).date()].append(p)
    return d


def day_cols(m):
    last = datetime.fromtimestamp(m.last).date()
    first = datetime.fromtimestamp(min(m.first, m.all_first)).date()   # watchlist scans began Saturday
    out = []
    for k in range(DAYS - 1, -1, -1):
        d = date.fromordinal(last.toordinal() - k)
        if d >= first:
            out.append(d)
    return out


def half_days(m, iid, cols):
    """One entry per half-day (Morning = midnight-noon, Evening = noon-midnight) for the shown days:
    (date, is_evening, price, sign). Shows whichever is further from the item's usual price:
    its HIGHEST (sign +1) or its LOWEST (sign -1); within 3% of usual sign 0; no scans None."""
    s = clean(m, iid)
    usual = statistics.median(p for _, p in s) if s else None   # the weekly usual (7 days)
    halves = defaultdict(list)
    sl = m.series(iid, long=True)
    if len(sl) >= 3 and usual:
        sl = [(t, p) for t, p in sl if 0.4 * usual <= p <= 2.5 * usual]   # same one-hour-gap filter, over all 14 days
    for t, p in sl:
        dt = datetime.fromtimestamp(t)
        halves[(dt.date(), dt.hour >= 12)].append(p)
    out = []
    for c in cols:
        for pm in (False, True):
            v = halves.get((c, pm))
            if not v:
                out.append((c, pm, None, None))
                continue
            up, dn = max(v) / usual - 1, 1 - min(v) / usual   # biggest spike vs biggest dip in this half-day
            if up >= dn:
                out.append((c, pm, max(v), 1 if up >= 0.03 else 0))
            else:
                out.append((c, pm, min(v), -1 if dn >= 0.03 else 0))
    return out


def hilo_cells(m, iid, cols):
    """Two cells per day: Morning and Evening - green up arrow = its high, red down arrow = its low."""
    cells = []
    for c, pm, p, sign in half_days(m, iid, cols):
        cls = "d" + ("" if pm else " first")
        if p is None:
            cells.append(f'<td class="{cls} mut">-</td>')
        elif sign == 1:
            cells.append(f'<td class="{cls} hi">{sm(p)}<span class="ar">&#9650;</span></td>')
        elif sign == -1:
            cells.append(f'<td class="{cls} lo">{sm(p)}<span class="ar">&#9660;</span></td>')
        else:
            cells.append(f'<td class="{cls} mut">{sm(p)}</td>')
    return "".join(cells)


def flip_status(m, iid, cols):
    """Streak = 3 half-days in a row flipping green/red/green or red/green/red (grey or no scan breaks it).
    3+ streaks in the last 7 days = certified; 1+ in the last 3 days = potential; none = not yet."""
    hd = half_days(m, iid, cols[-7:])   # streaks count over the last 7 days only
    recent = set(cols[-3:])
    total = in3 = 0
    for i in range(len(hd) - 2):
        a, b, c = (x[3] for x in hd[i:i + 3])
        if a in (1, -1) and b == -a and c == a:
            total += 1
            if all(x[0] in recent for x in hd[i:i + 3]):
                in3 += 1
    if total >= 3:
        return 2, "Certified flip", total
    if in3 >= 1:
        return 1, "Potential flip", total
    return 0, "Not flippable yet", total


def thead(left, cols, last):
    """Two header rows: the left columns span both; each day gets 'Monday 11/23' over Morning | Evening."""
    r1 = "".join(f'<th rowspan="2" class="{c}">{t}</th>' for t, c in left)
    r1 += "".join(f'<th colspan="2" class="dh">{c:%A} {c.month}/{c.day}{" (today)" if c == last else ""}</th>' for c in cols)
    r2 = "".join('<th class="d first">Morning</th><th class="d">Evening</th>' for _ in cols)
    return f'<thead><tr>{r1}</tr><tr>{r2}</tr></thead>'


def zig_legs(v, th):
    """Number of zigzag legs of at least th (up-down-up = 3 legs)."""
    if not v:
        return 0
    piv, trend, ext, n = v[0], 0, v[0], 0
    for p in v[1:]:
        if trend == 0:
            if p >= piv * (1 + th):
                trend, ext = 1, p
            elif p <= piv * (1 - th):
                trend, ext = -1, p
            else:
                continue
            n = 1
        elif trend == 1:
            if p > ext:
                ext = p
            elif p <= ext * (1 - th):
                trend, ext, n = -1, p, n + 1
        else:
            if p < ext:
                ext = p
            elif p >= ext * (1 + th):
                trend, ext, n = 1, p, n + 1
    return n


# --------------------------------------------------------------------------- cooldown items

ALWAYS_CD = {"Cured Rugged Hide"}   # always shown in Cooldowns, even at a loss


def cooldowns(m):
    out = []
    for r in csv.DictReader(open(os.path.join(HERE, "recipes.csv"), encoding="utf-8")):
        cd = float(r["cooldown_hours"] or 0)
        if cd <= 0:
            continue
        oid = m.name_id.get(r["output"].lower())
        ins = [(m.name_id.get(n.strip().lower()), n.strip(), float(q))
               for n, q in (p.rsplit(":", 1) for p in r["inputs"].split("|"))]
        oq = float(r["output_qty"] or 1)
        price = m.ah(oid)[0] if oid else None
        costs = [(i, n, q, m.cost(i)[0] if i else None) for i, n, q in ins]
        if not price or any(c is None for *_, c in costs):
            continue
        profit = price * oq * (1 - R.CUT) - sum(c * q for *_, q, c in costs)
        out.append(dict(recipe=r["recipe"], prof=r.get("profession") or "", cd=cd, oid=oid, oname=r["output"],
                        ins=ins, profit=profit, per_day=profit * 24 / cd))
    out.sort(key=lambda c: -c["per_day"])
    return [c for c in out if c["profit"] >= R.MIN_PROFIT or c["recipe"] in ALWAYS_CD]   # 1g+ cooldowns, plus ones Hammy tracks


# --------------------------------------------------------------------------- sections

def sold_day(m, iid, hours=168):
    """Estimated units sold per day over the last 7 days (listings that vanished before expiring, scan to scan)."""
    rows = [x for x in m.rows.get(iid, []) if x[0] >= m.last - hours * 3600]
    if len(rows) < 6:
        return None
    sold, prev_q = [], None
    for ts, _, q, s in rows:
        s = s or 0                               # no vanished listings that hour = nothing sold
        if prev_q and s >= 50 and s > 0.5 * prev_q:
            s = 0                                # half the market gone at once = mass cancel / bulk buyout, not normal sales
        sold.append(s)
        prev_q = q
    return sum(sold) / len(sold) * 24


def listed_day(m, iid, hours=168):
    """Estimated units newly put up per day over the last 7 days: stock change scan to scan plus what left (sold) in between."""
    rows = [x for x in m.rows.get(iid, []) if x[0] >= m.last - hours * 3600]
    if len(rows) < 6:
        return None
    new = [max(0, q - pq + (s or 0)) for (_, _, pq, _), (_, _, q, s) in zip(rows, rows[1:])]
    return sum(new) / len(new) * 24


def lc(m, iid):
    v = listed_day(m, iid)
    return '<td class="r mut">-</td>' if v is None else f'<td class="r">{v:,.0f}</td>'


_VENDOR = None


def vendor_price(iid):
    """Copper per unit if a Horde player can buy it from a vendor NPC somewhere (horde_vendor.json), else None."""
    global _VENDOR
    if _VENDOR is None:
        try:
            _VENDOR = json.load(open(os.path.join(HERE, "horde_vendor.json"), encoding="utf-8"))["items"]
        except (OSError, ValueError, KeyError):
            _VENDOR = {}
    v = _VENDOR.get(str(iid))
    return v[0] if v else None


def vnc(iid):
    """Vendor column: cheapest Horde vendor price per unit; blank when no vendor sells it."""
    p = vendor_price(iid) if iid else None
    if not p:
        return '<td class="r mut">-</td>'
    ltd = ' <span class="mut">ltd</span>' if _VENDOR.get(str(iid), [0, False])[1] else ''
    return f'<td class="r">{sm(max(1, round(p)))}{ltd}</td>'


def vc(m, iid):
    """Sold/day (7d avg) cell plus the Vendor cell to its right."""
    v = sold_day(m, iid)
    return ('<td class="r mut">-</td>' if v is None else f'<td class="r">{v:,.0f}</td>') + vnc(iid)


TOP_MOVERS = 20


def sec_movers(m):
    """Top daily movers: the 10 goods with the most gold changing hands in the last 24 hours (units sold x price)."""
    res = []
    for iid in m.rows:
        if m.name(iid).isdigit():
            continue
        sold = sold_day(m, iid, 24)
        now, q = m.ah(iid)
        if sold and now:
            res.append(dict(iid=iid, sold=sold, gold=sold * now, now=now, q=q))
    res.sort(key=lambda x: -x["gold"])
    rows = "".join(
        f'<tr><td class="nm" data-iid="{x["iid"]}">{R.esc(m.name(x["iid"]))}</td><td class="r now">{sm(x["now"])}</td>'
        f'<td class="r"><b>{R.chg(m.change_24h(x["iid"]))}</b></td><td class="r">{x["sold"]:,.0f}</td>'
        + (f'<td class="r"><b>{x["gold"] / 10000:,.0f}g</b></td>' if x["gold"] >= 1e7 else f'<td class="r"><b>{sm(x["gold"])}</b></td>')
        + f'<td class="r">{x["q"]:,}</td>{vc(m, x["iid"])}{lc(m, x["iid"])}</tr>'
        for x in res[:TOP_MOVERS])
    return ('<section><div class="sh"><span class="no">2</span><h2>Top daily movers</h2>'
            '<span class="tag">most gold traded in the last 24 hours (estimated)</span></div>'
            '<table class="g"><thead><tr><th>Item</th><th class="r">Now</th><th class="r">24h</th><th class="r">Sold (24h)</th>'
            '<th class="r">Gold traded (24h)</th><th class="r">Listed</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th><th class="r">Listed/day (7d avg)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></section>')


def sec_raid(m, cols, skip):
    cons, mats, unlisted = [], defaultdict(set), []
    for r in csv.DictReader(open(os.path.join(HERE, "raid_consumes.csv"), encoding="utf-8")):
        iid = m.name_id.get(r["item"].lower())
        rec = m.by_out.get(iid, []) if iid else []
        if not rec:
            continue
        rec = min(rec, key=lambda x: m.recipe_cost(x) or 1e18)
        for rid, _, _ in rec["reagents"]:
            mats[rid].add(iid)
            c, how = m.cost(rid, rec["prof"])
            if isinstance(how, tuple):
                for sid, _, _ in how[1]["reagents"]:
                    mats[sid].add(rid)
        if iid in skip:
            continue
        price = m.ah(iid)[0]
        if price:
            cost = m.recipe_cost(rec)
            profit = price * (rec.get("qty") or 1) * (1 - R.CUT) - cost if cost is not None else -1e12
            cons.append((profit, iid))
        else:
            unlisted.append(r["item"])
    mat_ids = [i for i in mats if i not in skip and i not in {c for _, c in cons} and m.ah(i)[0] and m.ah(i)[0] >= RAID_MAT_MIN]
    turnover = lambda i: sold_day(m, i) or 0                                   # most sold per day first
    cons = sorted((i for _, i in cons), key=turnover, reverse=True)[:TOP_RAID]
    mat_ids = sorted(mat_ids, key=turnover, reverse=True)[:TOP_RAID]
    last = cols[-1]

    def rows(title, ids):
        return (f'<tr class="grp"><td colspan="{5 + 2 * len(cols)}"><b>{title}</b></td></tr>'
                + "".join(f'<tr><td class="nm" data-iid="{i}">{R.esc(m.name(i))}</td><td class="r now">{sm(m.ah(i)[0])}</td>{lc(m, i)}{vc(m, i)}'
                          f'{hilo_cells(m, i, cols)}</tr>' for i in ids))
    days = (R.RAID_DAY - date.today()).days
    note = (f'<div class="fn">Top {TOP_RAID} of each: both ranked by units sold per day (7-day average). '
            f'No listings yet: {R.esc(", ".join(unlisted))}.</div>')
    return (f'<section><div class="sh"><span class="no">5</span><h2>Raid consumables</h2>'
            f'<span class="tag">{R.esc(R.RAIDS)} - Dec 9, {days} days</span></div>'
            f'<table class="g">{thead([("Item", "nm"), ("Now", "r"), ("Listed/day (7d avg)", "r"), ("Sold/day (7d avg)", "r"), ("Vendor", "r")], cols, last)}<tbody>'
            f'{rows("Consumables - most sold first", cons)}{rows("Materials used in raid consumes - most sold first", mat_ids)}'
            f'</tbody></table>{note}</section>')


def sec_flips(m, skip, cols):
    res = []
    for iid in m.rows:
        it = m.items.get(iid) or {}
        if iid in skip or it.get("cls") in ("Armor", "Weapon") or one_time_buy(m, iid):
            continue
        s = clean(m, iid)
        v = [p for _, p in s]
        if len(v) < 15 or statistics.median(v) < FLIP_MIN:
            continue
        # smooth each day over 3-hour windows so a lone one-hour blip can't pass as a swing
        # full days only (12+ scans); a real up-and-down every day
        days = [[statistics.median(d[max(0, i - 1):i + 2]) for i in range(len(d))] for d in by_day(s).values() if len(d) >= 12]
        if len(days) < 2 or not all(zig_legs(d, FLIP_SWING) >= 2 for d in days):
            continue
        day_lo, day_hi = [min(d) for d in days], [max(d) for d in days]
        if not all(h * (1 - R.CUT) >= l * 1.05 for l, h in zip(day_lo, day_hi)):   # every day pays 5%+ after the cut, not one spike day
            continue
        mids = [statistics.median(d) for d in days]
        if max(mids) > 1.25 * min(mids):   # price drifting up or down day to day = a trend, not a flip
            continue
        lo, hi = statistics.median(day_lo), statistics.median(day_hi)   # a typical day, so one spike can't inflate it
        profit = hi * (1 - R.CUT) - lo
        if profit <= 0:
            continue
        stack = stack_size(m, iid)
        if profit * stack < FLIP_STACK_MIN:
            continue
        now, qty = m.ah(iid)
        res.append(dict(iid=iid, lo=lo, hi=hi, profit=profit, now=now, qty=qty, n=len(days), usual=statistics.median(v),
                        stack=stack, per_stack=profit * stack))
    for x in res:
        x["rank"], x["status"], x["streaks"] = flip_status(m, x["iid"], cols)
        x["badge"] = ("", "pot", "cert")[x["rank"]]
        if x["rank"] == 1:   # only Certified flip gets a badge
            x["badge"] = ""
        if not x["badge"]:
            x["status"] = ""
    res.sort(key=lambda x: (-x["rank"], -x["per_stack"]))
    head = thead([("Item", "nm"), ("Now", "r"), ("24h", "r"), ("Listed", "r"), ("Sold/day (7d avg)", "r"), ("Vendor", "r")], cols, cols[-1])
    lists = {"low": [], "high": []}
    for x in res[:TOP_FLIPS]:
        pos = (x["now"] - x["lo"]) / max(1, x["hi"] - x["lo"]) if x["now"] else 0.5
        zone = "up" if pos <= 0.3 else "dn" if pos >= 0.7 else ""
        badge = x["badge"]
        lists["low" if pos < 0.5 else "high"].append(
            f'<tr class="row-{badge}"><td class="nm" data-iid="{x["iid"]}">{R.esc(m.name(x["iid"]))}{f' <span class="badge {badge}">{x["status"]}</span>' if badge else ''}</td><td class="r now {zone}">{sm(x["now"])}</td>'
            f'<td class="r"><b>{R.chg(m.change_24h(x["iid"]))}</b></td><td class="r">{x["qty"]}</td>{vc(m, x["iid"])}'
            f'{hilo_cells(m, x["iid"], cols)}</tr>')
    empty = f'<tr><td colspan="{6 + 2 * len(cols)}" class="mut">Nothing right now.</td></tr>'
    out = ""
    for k, title, note in (("low", "Priced low", "now in the bottom half of its usual daily range - buy here"),
                           ("high", "Priced high", "now in the top half of its usual daily range - sell here")):
        out += (f'<h3 class="cg">{title} <span class="mut cgn">{note}</span></h3>'
                f'<table class="g">{head}<tbody>' + ("".join(lists[k]) or empty) + '</tbody></table>')
    return ('<section><div class="sh"><span class="no">1</span><h2>Flip list</h2>'
            '<span class="tag">40s+ each, 1g+ per stack, a real up &amp; down every day at a steady price</span></div>'
            + out + '</section>')


MANIP_MIN = 500   # 5s usual price - ignore trash
TOP_MANIP = 20


def sec_manip(m):
    """Potential manipulation: cheap stock vanishing while the price climbs. Manipulated = over 2x usual with listings
    under 40% of normal (someone bought it out). Watch = early signs: 30%+ over usual with listings under 60% of normal."""
    res = []
    for iid, r in m.rows.items():
        if not r or r[-1][0] < m.last - 2 * 3600:
            continue
        hist = [x for x in r if x[0] >= m.last - 3 * 86400 and x[1]]
        if len(hist) < 12:
            continue
        ps, qs = sorted(x[1] for x in hist), sorted(x[2] for x in hist)
        usual, uq = ps[len(ps) // 2], qs[len(qs) // 2]
        now, q = r[-1][1], r[-1][2]
        if usual < MANIP_MIN or uq < 50 or q < 20 or iid in DURABLE_IDS(m):
            continue
        if now >= 1.80 * usual and q < 0.6 * uq:   # 80%+ over usual with stock down 40%+
            res.append(dict(iid=iid, now=now, usual=usual, q=q, uq=uq, man=m.manipulated(iid)))
    res.sort(key=lambda x: (-x["man"], -x["now"] / x["usual"]))
    rows = "".join(
        f'<tr><td class="nm" data-iid="{x["iid"]}">{R.esc(m.name(x["iid"]))}</td>'
        f'<td class="r now">{sm(x["now"])}</td><td class="r">{sm(x["usual"])}</td>'
        f'<td class="r"><b class="up">+{round((x["now"] / x["usual"] - 1) * 100)}%</b></td>'
        f'<td class="r">{x["q"]:,} <span class="mut">of {x["uq"]:,}</span></td>'
        f'<td class="r"><b class="dn">-{round((1 - x["q"] / x["uq"]) * 100)}%</b></td>{vc(m, x["iid"])}</tr>'
        for x in res[:TOP_MANIP])
    if not rows:
        rows = '<tr><td colspan="8" class="mut">nothing looks bought out right now</td></tr>'
    return ('<section><div class="sh"><span class="no">3</span><h2>Potential manipulation</h2>'
            '<span class="tag">cheap stock vanishing while the price climbs (all AH 113 goods worth 5s+)</span></div>'
            '<table class="g"><thead><tr><th>Item</th><th class="r">Now</th><th class="r">Usual</th><th class="r">vs usual</th>'
            '<th class="r">Listed (of usual)</th><th class="r">Stock</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<div class="fn"><span class="badge manip">Manipulated</span> over 2x usual with under 40% of the usual listings - '
            'sell into it, don&rsquo;t buy, it usually falls back once people relist.</div></section>')


def DURABLE_IDS(m):
    if not hasattr(m, "_durable"):
        m._durable = {i for i, v in m.items.items() if (v.get("name") or "").lower() in DURABLE}
    return m._durable


def sec_campfire(m):
    """Small table: each profession's camp feature drops, where they drop, and what they sell for now."""
    def cell(x):
        item, boss, place = x
        iid = m.name_id.get(item.lower())
        now, qty = m.ah(iid) if iid else (None, 0)
        where = " - ".join(w for w in (boss, place) if w) or "drop location unknown"
        price = f'{sm(now)} <span class="mut">({qty})</span>' if now else '<span class="mut">not listed</span>'
        name = f'<span data-iid="{iid}">{R.esc(item)}</span>' if iid and now else R.esc(item)
        return (f'<td><b>{name}</b> <span class="mut">{R.esc(where)}</span></td><td class="r">{price}</td>'
                + (vc(m, iid) if iid else '<td class="r mut">-</td>' * 2))
    rows = "".join(f'<tr><td class="nm">{p}</td>{cell(mid)}{cell(end)}</tr>' for p, mid, end in CAMP)
    return ('<section><div class="sh"><span class="no">6</span><h2>Campfire drops</h2>'
            '<span class="tag">camp features from dungeons - price now (listed)</span></div>'
            '<table class="g"><thead><tr><th>Profession</th><th>Mid-level drop (skill 140)</th><th class="r">Now</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th>'
            '<th>End-game drop (skill 300)</th><th class="r">Now</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th></tr></thead><tbody>' + rows + '</tbody></table>'
            '<div class="fn">Placed at a campfire for an hourly group buff. Tradeable, so you can buy one instead of farming the boss. '
            'Mid / end-game split is by dungeon level.</div></section>')


GUIDE_SKIP_TYPES = {"Everyday Meals", "Miscellaneous Cuisines", "SEASON OF DISCOVERY", "Winter Veil Treats"}


EASY_PLACES = ("Orgrimmar", "Thunder Bluff", "Undercity", "Durotar", "Mulgore", "Tirisfal Glades")


def guide_kind(learn):
    """Which 'hard to get' group a recipe source belongs in, or None if any trainer / a home-city vendor has it."""
    l = learn.lower()
    if l.startswith("cooking trainer"):
        return None
    if "alliance" in l:
        return "alliance"
    if l.startswith("drop"):
        return "drop"
    if l.startswith("quest"):
        return "quest"
    if "merchant's favor" in l:
        return "favor"
    if "alliance" in l:
        return "alliance"
    if any(p.lower() in l for p in EASY_PLACES) and " or " not in l and "," not in l.split(":", 1)[-1]:
        return None   # sold in a Horde city / starting zone - easy
    if any(p.lower() in l for p in EASY_PLACES):
        return None   # at least one easy vendor
    return "far"


def sec_cook_guide(m):
    """Hard-to-get cooking recipes only: drops, quests, faraway vendors, Merchant's Favor and Alliance-only recipes,
    with where a Horde cook gets each one (cooking_sources.csv - from wow.gg / method.gg, Nov 2026). Anything any cooking
    trainer teaches, or a vendor in a Horde city / starting zone sells, is left out."""
    src = list(csv.DictReader(open(os.path.join(HERE, "cooking_sources.csv"), encoding="utf-8"), delimiter="|"))
    groups = {k: [] for k in ("drop", "quest", "far", "favor", "alliance")}
    for x in src:
        kind = guide_kind(x["learn"])
        if not kind:
            continue
        name, skill = x["recipe"], int(x["skill"])
        r = (m.by_name.get(("Cooking", name.lower())) or [None])[0]
        oid = r.get("item_id") if r else None
        now = m.ah(oid)[0] if oid else None
        farm = None
        if r and now:
            sell = now * (r.get("qty") or 1) * (1 - R.CUT)
            farm = sell - sum((m.vendor(i) or 0) * q for i, _, q in r["reagents"])
        cat = (r or {}).get("category") or ""
        eff = x["effect"] or (cat if cat and cat not in GUIDE_SKIP_TYPES else "")
        groups[kind].append(dict(name=name, skill=skill, learn=x["learn"], eff=eff, oid=oid if now else None, farm=farm))
    for v in groups.values():
        v.sort(key=lambda x: (x["skill"], x["name"]))

    def tr(x):
        nm = (f'<td class="nm" data-iid="{x["oid"]}">' if x["oid"] else '<td class="nm">') + R.esc(x["name"]) + '</td>'
        pf = f'<b class="up">{sm(x["farm"])}</b>' if x["farm"] and x["farm"] > 0 else '<span class="mut">-</span>'
        return (f'<tr>{nm}<td class="r">{x["skill"]}</td><td>{R.esc(x["eff"]) or "<span class=mut>-</span>"}</td>'
                f'<td>{R.esc(x["learn"])}</td><td class="r">{pf}</td>'
                + (vc(m, x["oid"]) if x["oid"] else '<td class="r mut">-</td>' * 2) + '</tr>')

    def grp(title, rows):
        return f'<tr class="grp"><td colspan="7"><b>{title}</b></td></tr>' + "".join(tr(x) for x in rows) if rows else ""

    camp_tbl = sec_campfire(m).replace('<span class="no">6</span>', '')
    return ('<h3 class="cg">Hard-to-get cooking recipes (Horde)</h3>'
            '<div class="mut cgn">Drops, quests, faraway vendors and rep recipes only - anything a cooking trainer teaches or a '
            'Horde city vendor sells is left out. Cooking cap: Expert Cookbook at 125 from Wulan (Desolace); 225-300 from the quest '
            'Clamlette Surprise, Dirge Quikcleave in Gadgetzan (character level 35). Trainers out in the world: Mudduk '
            '(Stranglethorn), Duhng (Barrens), Ortak Lomgok (Mount Hyjal), Gorg (Riverglades). Profit each = farm your own meat/fish. '
            'Sources: wow.gg and method.gg (Nov 2026).</div>'
            '<table class="g"><thead><tr><th>Recipe</th><th class="r">Skill</th><th>Buff / use</th><th>Where to get it</th>'
            '<th class="r">Profit each</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th></tr></thead><tbody>'
            + grp("Drops", groups["drop"])
            + grp("Quests", groups["quest"])
            + grp("Faraway vendors", groups["far"])
            + grp("Merchant&rsquo;s Favor (Aza&rsquo;bek, near the Crossroads)", groups["favor"])
            + grp("Alliance-only vendors - look for the recipe on the AH", groups["alliance"])
            + '</tbody></table>' + camp_tbl)


TOP_COOK = 20


def sec_cooking(m):
    """Top cooking recipes by profit: farming the meat/fish yourself (only vendor spices cost money) vs buying every mat."""
    res = []
    for r in m.recipes:
        if r["prof"] != "Cooking" or not r.get("item_id"):
            continue
        oid = r["item_id"]
        now, qty = m.ah(oid)
        if not now:
            continue
        sell = now * (r.get("qty") or 1) * (1 - R.CUT)
        vend = sum((m.vendor(i) or 0) * q for i, _, q in r["reagents"])   # what you still pay when you farm the rest
        buy = m.recipe_cost(r)
        mats = ", ".join(f'{q:g} {n}' + ('' if m.vendor(i) else '*') for i, n, q in r["reagents"])
        res.append(dict(r=r, oid=oid, now=now, qty=qty, farm=sell - vend, buy=None if buy is None else sell - buy, mats=mats))
    res.sort(key=lambda x: -x["farm"])
    def pc(v):
        return '<span class="mut">-</span>' if v is None else f'<b class="{"up" if v > 0 else "dn"}">{"" if v > 0 else "-"}{sm(abs(v))}</b>'
    rows = "".join(f'<tr><td class="nm" data-iid="{x["oid"]}">{R.esc(m.name(x["oid"]))}</td><td class="r">{sm(x["now"])}</td>'
                   f'<td class="r">{x["qty"]}</td>{vc(m, x["oid"])}<td class="r">{pc(x["farm"])}</td><td class="r">{pc(x["buy"])}</td>'
                   f'<td class="mats">{R.esc(x["mats"])}</td></tr>' for x in res[:TOP_COOK])
    return ('<section><div class="sh"><span class="no">6</span><h2>Cooking</h2>'
            f'<span class="tag">top {TOP_COOK} recipes by profit each, after the 5% cut</span></div>'
            '<table class="g"><thead><tr><th>Makes</th><th class="r">Sells</th><th class="r">Listed</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th>'
            '<th class="r">Farm mats</th><th class="r">Buy mats</th><th>Mats (* = farm or buy, rest from a vendor)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<div class="fn">Farm mats = profit if you gather the * mats yourself and only pay the vendor for spices etc. '
            'Buy mats = profit if you buy every mat at today&rsquo;s cheapest (AH or vendor); red = loses money. Best farm profit first.</div>'
            + sec_cook_guide(m) + '</section>')


TOP_CRAFTS = 10   # per profession
CRAFT_MIN_SOLD = 10   # output must sell 10+ a day so you can list it right after crafting
GEAR_MIN_LISTED = 3   # crafted gear: 3+ on the AH right now
GEAR_MAX_MARKUP = 6   # and priced at most 6x its mats - above that it is a lone silly ask, not a market
GEAR_MIN_HOURS = 54   # and seen in 54+ of the last 72 hourly scans


_SOLD = None


def last_sold(m, iid):
    """(ts, highest unit price) from the most recent hour this item really sold, within 3 days; else None.
    Sales = listings that vanished early priced at or under the new cheapest (see forever_ah.save_sales)."""
    global _SOLD
    if _SOLD is None:
        _SOLD = {}
        try:
            import sqlite3
            con = sqlite3.connect(os.path.join(HERE, "ah_113.sqlite"))
            for ts, i, hi in con.execute("SELECT ts, item_id, hi FROM sales WHERE ts >= ? ORDER BY ts", (m.last - 3 * 86400,)):
                _SOLD[i] = (ts, hi)
            con.close()
        except Exception:
            pass
    return _SOLD.get(iid)


def ago(ts, now):
    h = max(0, round((now - ts) / 3600))
    return "this hour" if h == 0 else f"{h}h ago" if h < 48 else f"{h // 24}d ago"


def craft_row(m, x):
    makes = f' <span class="mut">x{x["makes"]:g}</span>' if x["makes"] > 1 else ''
    if x["ls"]:
        sold = f'<td class="r">{sm(x["price"])} <span class="mut">{ago(x["ls"][0], m.last)}</span></td>'
    else:
        sold = f'<td class="r mut" title="no sale seen yet - using the cheapest listing">~{sm(x["price"])}</td>'
    vol = ('<td class="r mut" title="gear: Blizzard gives no sold count">gear</td>' + vnc(x["oid"])) if x["gear"] else vc(m, x["oid"])
    return (f'<tr><td class="nm" data-iid="{x["oid"]}">{R.esc(m.name(x["oid"]))}{makes}</td>{sold}'
            f'<td class="r">{sm(x["now"])}</td><td class="r">{x["qty"]}</td>{vol}'
            f'<td class="r">{sm(x["cost"])}</td><td class="r"><b class="up">{sm(x["profit"])}</b></td>'
            f'<td class="mats">{R.esc(x["mats"])}</td></tr>')


def sec_crafts(m, shown=()):
    """Top 10 crafts per profession by profit per craft (buying every mat at today's cheapest), cooldown recipes left out."""
    cd = set()
    for r in csv.DictReader(open(os.path.join(HERE, "recipes.csv"), encoding="utf-8")):
        if float(r["cooldown_hours"] or 0) > 0:
            cd |= {r["recipe"].strip().lower(), r["output"].strip().lower()}
    cand = []
    for r in m.recipes:
        oid = r.get("item_id")
        if not oid or r["name"].lower() in cd or r["name"].lower().startswith("transmute") or m.name(oid).lower() in cd:
            continue
        if oid in shown or one_time_buy(m, oid) or m.manipulated(oid):   # anything already in a section above is left out
            continue
        now, qty = m.ah(oid)
        gear = False
        if now:
            v = [p for _, p in clean(m, oid)]
            price = min(now, statistics.median(v)) if len(v) >= 15 else now   # a spike right now doesn't count
            sold = sold_day(m, oid)
            if not sold or sold < CRAFT_MIN_SOLD:
                continue
        else:   # crafted gear (one per listing): Blizzard gives no sold count, so ask for a steady market instead
            now, qty = m.gear_now(oid)
            if not now or qty < GEAR_MIN_LISTED:
                continue
            hist = [(t, p) for t, p in m.series(oid, gear=True) if t >= m.last - 3 * 86400]
            if len(hist) < GEAR_MIN_HOURS:   # listed most hours of the last 3 days = people keep buying and relisting it
                continue
            price = min(now, statistics.median(p for _, p in hist))   # cheapest ask, never above its usual
            gear = True
        cost = m.recipe_cost(r)
        if cost is None:
            continue
        ls = last_sold(m, oid)
        if ls:   # what it really sold for: the highest price paid in its latest hour of sales
            price = ls[1]
        if gear and price > GEAR_MAX_MARKUP * cost:
            continue
        profit = price * (r.get("qty") or 1) * (1 - R.CUT) - cost
        if profit <= 0:
            continue
        mats = ", ".join(f'{q:g} {n}' for _, n, q in r["reagents"])
        cand.append(dict(oid=oid, prof=r["prof"], makes=r.get("qty") or 1, now=now, qty=qty, cost=cost, profit=profit, mats=mats, gear=gear, price=price, ls=ls))
    best = {}
    for x in cand:   # one line per item, its most profitable recipe
        if x["oid"] not in best or best[x["oid"]]["profit"] < x["profit"]:
            best[x["oid"]] = x
    byprof = defaultdict(list)
    for x in best.values():
        byprof[x["prof"]].append(x)
    for v in byprof.values():
        v.sort(key=lambda x: -x["profit"])
    head = ('<table class="g"><thead><tr><th>Makes</th><th class="r">Last sold (highest)</th><th class="r">Cheapest now</th><th class="r">Listed</th>'
            '<th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th><th class="r">Mats cost</th><th class="r">Profit per craft</th><th>Mats</th></tr></thead>')
    out = ""
    for prof in sorted(byprof, key=lambda p: -byprof[p][0]["profit"]):   # profession with the best craft first
        top = byprof[prof][:TOP_CRAFTS]
        rows = "".join(craft_row(m, x) for x in top)
        out += f'<h3 class="cg">{R.esc(prof)} <span class="mut cgn">top {len(top)} by profit per craft</span></h3>{head}<tbody>{rows}</tbody></table>'
    out = out or '<div class="mut">nothing profitable right now</div>'
    return ('<section><div class="sh"><span class="no">7</span><h2>Top crafts</h2>'
            f'<span class="tag">top {TOP_CRAFTS} per profession by profit per craft, buying all mats - no cooldowns, nothing already listed above. Profit uses the highest price it actually sold for in its latest hour of sales (~ = no sale seen yet, cheapest listing used). Gear must be listed 3+ and steadily on the AH</span></div>'
            + out + '</section>')


WOOD_NOTE = {"Thick Logs": "cooking supply vendors (Xen'to Orgrimmar, Ronald Burch Undercity, Aza'bek Barrens) about 20g; vendor buys back at 5g",
             "Star Wood": "trade supplies vendors", "Simple Wood": "trade supplies vendors"}
CAMP_BUY = {"Thick Logs": 130000, "Expert Campfire Kit": 150000}   # buy alert at or under (copper)


def camp_buys(m):
    """[(name, now, target)] for campfire goods priced at or under their buy target right now."""
    out = []
    for n, tgt in CAMP_BUY.items():
        iid = m.name_id.get(n.lower())
        now = m.ah(iid)[0] if iid else None
        if now and now <= tgt:
            out.append((n, now, tgt))
    return out


def sec_camp_cooking(m):
    """Cooking's camping craftables (campfire kits, oven, feast) plus the woods they need - lives inside the Cooking section."""
    def usual(iid):
        r = sorted(x[1] for x in m.rows.get(iid, []) if x[0] >= m.last - 3 * 86400 and x[1])
        return r[len(r) // 2] if r else None
    buys = {n for n, _, _ in camp_buys(m)}
    def nm(iid, extra=""):
        n = m.name(iid)
        return (f'<td class="nm" data-iid="{iid}">{R.esc(n)}{extra}'
                + (' <span class="badge tgt">Buy target</span>' if n in buys else '') + '</td>')
    rows = []
    for r in sorted((r for r in m.recipes if r["prof"] == "Cooking" and (r.get("category") or "") == "Camping" and r.get("item_id")),
                    key=lambda r: -(usual(r["item_id"]) or 0)):
        oid = r["item_id"]
        now, q = m.ah(oid)
        cost = m.recipe_cost(r)
        sell = now * (r.get("qty") or 1) * (1 - R.CUT) if now else None
        prof = None if sell is None or cost is None else sell - cost
        sk = re.search(r"\((\d+)\)", (m.items.get(oid) or {}).get("skill") or "")
        mats = ", ".join(f"{q_:g} {n}" for _, n, q_ in r["reagents"])
        pc = '<span class="mut">-</span>' if prof is None else f'<b class="{"up" if prof > 0 else "dn"}">{"" if prof > 0 else "-"}{sm(abs(prof))}</b>'
        rows.append(f'<tr>{nm(oid)}<td class="r">{sk.group(1) if sk else "-"}</td><td class="r">{sm(now) if now else "-"}</td>'
                    f'<td class="r">{sm(usual(oid)) if usual(oid) else "-"}</td><td class="r">{q}</td>{vc(m, oid)}'
                    f'<td class="r">{sm(cost) if cost else "-"}</td><td class="r">{pc}</td><td class="mats">{R.esc(mats)}</td></tr>')
    wood = []
    for n in ("Thick Logs", "Star Wood", "Simple Wood"):
        iid = m.name_id.get(n.lower())
        if not iid:
            continue
        now, q = m.ah(iid)
        it = m.items.get(iid) or {}
        tgt = CAMP_BUY.get(n)
        wood.append(f'<tr>{nm(iid, " <span class=mut>(wood)</span>")}<td class="r">-</td><td class="r">{sm(now) if now else "-"}</td>'
                    f'<td class="r">{sm(usual(iid)) if usual(iid) else "-"}</td><td class="r">{q}</td>{vc(m, iid)}'
                    f'<td class="r mut">vendor {sm(it["buy"]) if it.get("buy") else "-"}</td><td class="r mut">{("buy at " + sm(tgt) + " or less") if tgt else ""}</td>'
                    f'<td class="mats mut">{WOOD_NOTE.get(n, "")}</td></tr>')
    return ('<h3 class="sub">Campfire craftables</h3>'
            '<table class="g"><thead><tr><th>Makes</th><th class="r">Cooking</th><th class="r">Now</th><th class="r">Usual</th>'
            '<th class="r">Listed</th><th class="r">Sold/day (7d avg)</th><th class="r">Vendor</th><th class="r">Mats cost</th><th class="r">Profit each</th><th>Mats</th></tr></thead>'
            f'<tbody>{"".join(rows)}{"".join(wood)}</tbody></table>'
            '<div class="fn">Profit each = AH price after the 5% cut minus buying the mats at today&rsquo;s cheapest. '
            'Thick Logs cost about 20g at cooking supply vendors - more than a kit sells for - so only AH logs under ~15g pay. '
            '<span class="badge tgt">Buy target</span> = at or under the buy target (Thick Logs 13g, Expert Campfire Kit 15g).</div>')


BUY_MIN_DAYS = 4   # need 4+ days of history before calling anything a Buy


def fresh_dip(m, cs, usual):
    """Buy = a fresh dip under a steady price. Not when: too new to know, the last day has been low all day, or the price
    has been stepping down day after day (each day's middle price lower than the day before - a slide, not a dip)."""
    if cs[0][0] > m.last - BUY_MIN_DAYS * 86400:
        return False
    days = []
    for k in range(3):   # middle price of the last 24h, the 24h before that, and the 24h before that
        v = [p for t, p in cs if m.last - (k + 1) * 86400 < t <= m.last - k * 86400]
        days.append(statistics.median(v) if len(v) >= 4 else None)
    d0, d1, d2 = days
    if d0 and d0 <= 0.85 * usual:
        return False
    if d0 and d1 and d0 < 0.92 * d1:                       # dropped 8%+ day over day
        return False
    if d0 and d1 and d2 and d0 < 0.97 * d1 and d1 < 0.97 * d2:   # two down days in a row
        return False
    return True


def price_badge(m, iid):
    """Where the price is now vs its weekly usual (median of the last 7 days, listing gaps removed)."""
    cs = clean(m, iid)
    s = [p for _, p in cs]
    now = m.ah(iid)[0]
    if len(s) < 15 or not now:
        return ""
    usual = statistics.median(s)
    r = now / usual
    for lo, cls, label in PRICE_BADGES:
        if (lo >= 1 and r >= lo) or (lo < 1 and r <= lo):
            if cls == "buy" and not fresh_dip(m, cs, usual):
                cls, label = "mdn", "Moving down"
            return f' <span class="badge {cls}">{label}</span>'
    return ""


def flip_badge(m, iid, cols):
    """Flip-list style badge for items kept out of the flip list (cooldown items): same streak rule and price check."""
    rank, status, _ = flip_status(m, iid, cols)
    s = [p for _, p in clean(m, iid)]
    if not rank or len(s) < 15 or statistics.median(s) < FLIP_MIN:
        return ""
    if rank < 2:   # only Certified flip gets a badge
        return ""
    return f' <span class="badge cert">{status}</span>'


def sec_cooldowns(m, cds, cols):
    rows, seen = [], set()
    last = cols[-1]
    for c in cds:
        rows.append(f'<tr class="grp"><td colspan="{5 + 2 * len(cols)}"><b>{R.esc(c["recipe"])}</b> '
                    f'<span class="mut">{R.esc(c["prof"])} {c["cd"]:.0f}h</span>'
                    + (f'<span class="pr">+{sm(c["profit"])} ({sm(c["per_day"])}/day)</span></td></tr>' if c["profit"] >= 0 else
                     f'<span class="pr dn">loses {sm(-c["profit"])} - sell the inputs instead</span></td></tr>'))
        for iid, nm, lab, note in ([(c["oid"], c["oname"], "", "")] + [(i, n, "in", "") for i, n, _ in c["ins"]]):
            if not iid or not m.ah(iid)[0]:
                continue
            rows.append(f'<tr><td class="nm {lab}" data-iid="{iid}">{R.esc(m.name(iid))}<span class="mut">{note}</span>{flip_badge(m, iid, cols)}</td><td class="r now">{sm(m.ah(iid)[0])}</td>{lc(m, iid)}{vc(m, iid)}'
                        f'{hilo_cells(m, iid, cols)}</tr>')
    return ('<section><div class="sh"><span class="no">4</span><h2>Cooldowns</h2>'
            '<span class="tag">1g+ profit, best per day first - flip badges use the same rules as the flip list</span></div>'
            f'<table class="g">{thead([("Makes / uses", "nm"), ("Now", "r"), ("Listed/day (7d avg)", "r"), ("Sold/day (7d avg)", "r"), ("Vendor", "r")], cols, last)}'
            '<tbody>' + "".join(rows) + '</tbody></table></section>')


def sec_leveling(m):
    """Cheapest 1-150 route per profession, one block each (goes in its own PDF)."""
    steps, order = defaultdict(lambda: defaultdict(list)), []
    for r in csv.DictReader(open(os.path.join(HERE, "leveling.csv"), encoding="utf-8")):
        if r["profession"] not in order:
            order.append(r["profession"])
        steps[r["profession"]][(int(r["from"]), int(r["to"]), int(r["crafts"]))].append(r["recipe"])
    blocks = []
    for prof in order:
        rows, total, unknown = [], 0, False
        for (lo, hi, n), names in sorted(steps[prof].items()):
            opts = []
            for nm in names:
                for r in m.by_name.get((prof, nm.lower()), [])[:1]:
                    c = m.recipe_cost(r)
                    opts.append((c if c is not None else 1e15, nm, c, r))
            if not opts:
                continue
            _, nm, c, r = min(opts, key=lambda o: o[0])
            if c is None:
                unknown = True
            else:
                total += c * n
            mats = ", ".join(f"{q:g} {x}" for _, x, q in r["reagents"])
            rows.append(f'<tr><td class="r">{lo}-{hi}</td><td><b>{R.esc(nm)}</b><div class="mut">{R.esc(mats)}</div></td>'
                        f'<td class="r">x{n}</td><td class="r">{sm(c * n) if c is not None else "-"}</td></tr>')
        blocks.append(f'<section><div class="sh"><h2>{R.esc(prof)}</h2><span class="tag">kit <b>{sm(total)}</b>'
                      f'{" + mats not on AH" if unknown else ""}</span></div>'
                      '<table class="g lv"><thead><tr><th class="r">Skill</th><th>Recipe / each needs</th>'
                      '<th class="r">Crafts</th><th class="r">Cost</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></section>')
    half = (len(blocks) + 1) // 2
    return (f'<div class="cols"><div class="col">{"".join(blocks[:half])}</div><div class="col">{"".join(blocks[half:])}</div></div>'
            '<div class="fn">Cost = buying every material at today&rsquo;s cheapest price. Where a range has several good recipes '
            'the cheapest one right now is picked. Skill brackets follow classic; Forever reworked some recipes, so check recipe colors at your trainer.</div>')


CSS = """
:root{--bg:#0f1216;--ink:#e6e9ee;--ink2:#aab2bd;--mut:#7d8692;--line:#262c34;--band:#1a1f26;--acc:#58a6ff;--up:#3fb950;--dn:#f85149;--rule:#3a424d}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:7.4px/1.3 "Segoe UI",system-ui,sans-serif}
.wrap{width:100%;zoom:__ZOOM__}
header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid var(--ink);padding-bottom:3px;margin-bottom:4px}
header h1{font-size:15px;margin:0}
header .scope{color:var(--ink2);}
header .when{text-align:right;color:var(--ink2);}
header .when b{color:var(--ink);}
section{margin-top:5px;break-inside:avoid}
.sh{display:flex;align-items:center;gap:5px;margin-bottom:2px}
.sh .no{background:var(--ink);color:var(--bg);font-weight:700;width:13px;height:13px;border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:8px}
.sh h2{font-size:10.5px;margin:0}
.sh .tag{margin-left:auto;color:var(--ink2);}
.two{display:flex;gap:8px;align-items:flex-start}
.two>table{flex:1}
.cols{display:flex;gap:8px;align-items:flex-start}
.cols>section,.cols>.col{flex:1;min-width:0}
.lv{table-layout:fixed}.lv td{white-space:normal;overflow-wrap:anywhere}
.lv th:nth-child(1){width:15%}.lv th:nth-child(3){width:11%}.lv th:nth-child(4){width:13%}
table.g{width:100%;border-collapse:collapse}
.g th{text-align:left;text-transform:uppercase;letter-spacing:.03em;color:var(--mut);font-weight:600;border-bottom:1px solid var(--ink);padding:1px 2px}
.g td{border-bottom:1px solid var(--line);padding:.6px 2px;vertical-align:middle;white-space:nowrap}
.g tr{break-inside:avoid}
.g .r{text-align:right}
.g .nm{max-width:118px;overflow:hidden;text-overflow:ellipsis}
.g .nm.hb{white-space:normal;overflow:visible;line-height:1.35}
.g .nm .badge{display:inline-block;margin:1px 0}
.g .now{font-weight:600}
.g .d{text-align:center;padding:.6px 1px}
.g th.d{text-align:center}
.g td.hi{color:var(--up)}.g td.lo{color:var(--dn)}
.g .first{border-left:1px solid var(--rule)}
.g .ar{font-size:.8em;margin-left:1px}
.badge{border-radius:3px;padding:0 3px;font-weight:700;white-space:nowrap}
.badge.cert{background:#1f6feb;color:#fff}.badge.pot{background:#0d2d5e;color:#79c0ff;box-shadow:inset 0 0 0 1px #388bfd}.badge.buy{background:#238636;color:#fff}.badge.sell{background:#da3633;color:#fff}.badge.mup{background:#db6d28;color:#fff}.badge.mdn{background:#d29922;color:#1b1300}.badge.tgt{background:#2ea043;color:#fff}.legend{margin:0 0 8px;padding:5px 8px;border:1px solid #2a3038;border-radius:6px;line-height:1.9}.badge.wait{background:#1f6feb;color:#fff}.row-wait td{background:#0f1d33}.badge.dip{background:#3a3220;color:#e3b341}.badge.nope{background:#2a3038;color:#9aa3ad}.badge.manip{background:#8957e5;color:#fff}.sub{font-size:1em;margin:8px 0 3px;color:var(--ink)}.badge.watch{background:#3d2a5c;color:#d2b8ff}
.row-cert td{background:#0f1d33}.row-pot td{background:#2a2410}
.g th.dh{text-align:center;border-left:1px solid var(--rule);border-bottom:none;color:var(--ink);text-transform:none;letter-spacing:0}
.g .in{padding-left:9px;color:var(--ink2)}
.grp td{background:var(--band);padding:1px 2px}
.grp .pr{margin-left:12px;color:var(--up);font-weight:700}.grp .pr.dn{color:var(--dn)}
.g .mats{white-space:normal;color:var(--ink2)}
h3.cg{margin:14px 0 2px;font-size:13px}.cgn{font-size:10px;margin-bottom:4px;white-space:normal}
.lv .route{white-space:normal;color:var(--ink2);}
.lv .st b{color:var(--ink)}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}
.fn{color:var(--mut);margin-top:1px}
.legend{color:var(--mut);margin-bottom:2px}
@page{size:letter;margin:0}
body{padding:.3in}
"""


def render(title, body):
    """Write output/<title>.pdf, shrinking the text until it fits on one letter page."""
    name = title.replace("'", "")
    os.makedirs(R.OUT, exist_ok=True)
    hp, pdf = (os.path.join(R.OUT, f"{name}.{e}") for e in ("html", "pdf"))
    edge = next(e for e in R.EDGE if os.path.exists(e))
    for zoom in (1.5, 1.42, 1.35, 1.28, 1.21, 1.15, 1.08, 1.0, 0.94, 0.88, 0.82, 0.76, 0.7, 0.64):
        if os.path.exists(pdf):
            os.remove(pdf)
        page = (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
                f'<style>{CSS.replace("__ZOOM__", str(zoom))}</style></head><body><div class="wrap">{body}</div></body></html>')
        open(hp, "w", encoding="utf-8").write(page)
        subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={pdf}",
                        "file:///" + hp.replace("\\", "/")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        for _ in range(30):
            if os.path.exists(pdf) and os.path.getsize(pdf) > 0:
                break
            time.sleep(1)
        if len(re.findall(rb"/Type\s*/Page[^s]", open(pdf, "rb").read())) <= 1:
            break
    return pdf


def snapshot(pdf):
    """Picture of the page (PNG) next to the PDF, for Discord to show inline: rendered at 2x, blank space trimmed."""
    from PIL import Image, ImageChops, ImageOps
    hp, png = pdf[:-4] + ".html", pdf[:-4] + ".png"
    if os.path.exists(png):
        os.remove(png)
    edge = next(e for e in R.EDGE if os.path.exists(e))
    subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--window-size=816,1056", f"--screenshot={png}", "file:///" + hp.replace("\\", "/")],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    for _ in range(30):
        if os.path.exists(png) and os.path.getsize(png) > 0:
            break
        time.sleep(1)
    im = Image.open(png).convert("RGB")
    box = ImageChops.difference(im, Image.new("RGB", im.size, (15, 18, 22))).getbbox()
    if box:
        im = im.crop(box)
    ImageOps.expand(im, border=28, fill=(15, 18, 22)).save(png, optimize=True)
    return png


SECTION_ORDER = ["cooldowns", "raid", "manip", "movers", "crafts", "flips", "cooking"]
PIC_GROUPS = [(0,), (1,), (2, 3), (4, 5), (6,)]   # sections per Discord picture, in page order   # sections per Discord picture: flips+movers+manipulation, cooldowns, raid, campfire+cooking


def snapshot_parts(pdf, body):
    """Readable Discord pictures: the market report split into a few images at normal text size (the one-page PDF
    shrinks the text to fit, which is unreadable on a phone). Returns the PNG paths in order."""
    from PIL import Image, ImageChops, ImageOps
    import glob
    for old in glob.glob(glob.escape(pdf[:-4]) + " part*.png"):
        os.remove(old)
    head, *secs = body.split("<section>")
    secs = ["<section>" + x for x in secs]
    edge = next(e for e in R.EDGE if os.path.exists(e))
    out = []
    for n, grp in enumerate(PIC_GROUPS, 1):
        part = (head if n == 1 else "") + "".join(secs[i] for i in grp if i < len(secs))
        if not part:
            continue
        hp, png = f"{pdf[:-4]} part{n}.html", f"{pdf[:-4]} part{n}.png"
        open(hp, "w", encoding="utf-8").write(
            f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS.replace("__ZOOM__", "1")}</style></head>'
            f'<body><div class="wrap">{part}</div></body></html>')
        subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                        "--window-size=900,4000", f"--screenshot={png}", "file:///" + hp.replace("\\", "/")],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        for _ in range(30):
            if os.path.exists(png) and os.path.getsize(png) > 0:
                break
            time.sleep(1)
        im = Image.open(png).convert("RGB")
        box = ImageChops.difference(im, Image.new("RGB", im.size, (15, 18, 22))).getbbox()
        if box:
            im = im.crop(box)
        ImageOps.expand(im, border=28, fill=(15, 18, 22)).save(png, optimize=True)
        os.remove(hp)
        out.append(png)
    return out


LEGEND = ('<div class="legend"><b>Badges</b> (price now vs its weekly usual): '
          '<span class="badge buy">Buy</span> 20%+ under usual &middot; '
          '<span class="badge mdn">Moving down</span> 10-20% under &middot; '
          '<span class="badge mup">Moving up</span> 10-20% over &middot; '
          '<span class="badge sell">Sell</span> 20%+ over &middot; no badge = near usual<br>'
          '<span class="badge manip">Manipulated</span> not a normal swing: someone bought out the cheap stock - the price is real now but '
          'artificial; sell into it, don&rsquo;t buy &middot; '
          '<span class="badge cert">Certified flip</span> up &amp; down 3+ times this week</div>')


def header(title, last, extra):
    return (f'<header><div><h1>{title}</h1>'
            f'<div class="scope">WoW Forever - US - PvP ruleset - Horde auction house</div></div>'
            f'<div class="when">prices as of <b>{R.pacific(last.timestamp())}</b><br>{extra}</div></header>')


def build():
    m = R.Market()
    cols = day_cols(m)
    cds = cooldowns(m)
    skip = set()
    for c in cds:
        skip.add(c["oid"])
        skip |= {i for i, _, _ in c["ins"] if i}
    last = datetime.fromtimestamp(m.last)
    when = part_of_day(datetime.now().hour)
    # market report: flips, then cooldowns, then raid consumables (the biggest) across the bottom half - all full width
    title = "Hammy's Toolchest"
    try:   # show when Blizzard posted this snapshot (about :26 past), not when our scan ran
        from email.utils import parsedate_to_datetime
        lm = m.con.execute("SELECT v FROM meta WHERE k='last_modified'").fetchone()
        last = datetime.fromtimestamp(parsedate_to_datetime(lm[0]).timestamp()) if lm and lm[0] else last
    except Exception:
        pass
    body = header(title, last, f'each day: <b>Morning</b> (12am-noon) and <b>Evening</b> (noon-12am), last {DAYS} days')
    parts = dict(cooldowns=sec_cooldowns(m, cds, cols), raid=sec_raid(m, cols, skip), manip=sec_manip(m), movers=sec_movers(m),
                 flips=sec_flips(m, skip, cols), cooking=sec_cooking(m).replace("</section>", sec_camp_cooking(m) + "</section>"))
    parts["crafts"] = sec_crafts(m, {int(i) for i in re.findall(r'data-iid="(\d+)"', "".join(parts.values()))})   # nothing listed elsewhere
    body += "".join(parts[k] for k in SECTION_ORDER)
    n = iter(range(1, 99))
    body = re.sub(r'<span class="no">\d+</span>', lambda _: f'<span class="no">{next(n)}</span>', body)   # number in page order
    body = re.sub(r'(<td class="nm[^"]*" data-iid="(\d+)">.*?)(</td>)',   # "Manipulated" badge on bought-out items
                  lambda x: x.group(1) + (' <span class="badge manip">Manipulated</span>' if m.manipulated(int(x.group(2))) else '')
                  + price_badge(m, int(x.group(2))) + x.group(3), body)   # price badges on every item in every section
    body = re.sub(r'<div class="fn">(?:(?!</?div).)*</div>', '', body, flags=re.S)   # no explanation notes under sections
    body = re.sub(r'<td class="(nm[^"]*)"([^>]*>(?:(?!</td>).)*?class="badge )',   # name cells with a badge may wrap so the badge isn't cut off
                  r'<td class="\1 hb"\2', body)
    market = render(title, body)
    try:   # web-page version: hover an item for its hour-by-hour 7-day chart
        import interactive
        kits = re.sub(r'<div class="fn">(?:(?!</?div).)*</div>', '', sec_leveling(m), flags=re.S)
        kits = ('<section><div class="sh"><h2>1-150 Profession Kits</h2><span class="tag">cheapest route at current prices'
                ' &middot; campfire drops</span></div>' + kits + sec_campfire(m).replace('<span class="no">6</span>', '') + '</section>')
        interactive.make(m, title, body + kits, market[:-4] + " Interactive.html")   # kits tile at the bottom of the web page
    except Exception as e:
        print(f"interactive page failed: {e}")
    # profession routes: its own page
    title2 = f"Hammy's {when} Profession Routes 1-150"
    routes = render(title2, re.sub(r'<div class="fn">(?:(?!</?div).)*</div>', '',
                                   header(title2, last, "cheapest route at current prices") + sec_leveling(m)
                                   + sec_campfire(m).replace('<span class="no">6</span>', ''), flags=re.S))   # campfire drops at the bottom of the routes page
    for p in (market, routes):
        try:
            snapshot(p)
        except Exception as e:
            print(f"snapshot failed for {os.path.basename(p)}: {e}")
    try:
        snapshot_parts(market, body)
    except Exception as e:
        print(f"picture split failed: {e}")
    return market, routes


if __name__ == "__main__":
    print(build())
