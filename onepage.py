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
DAYS = 7
POT_DIP = 0.85         # Potential flip only when the price is at least 15% under its weekly usual
FLIP_MIN = 4000          # 40s
FLIP_SWING = 0.07        # a leg must be 7%+ to count (what a flip needs to beat the 5% cut)
RAID_MAT_MIN = 500       # leave out materials under 5s (vials, spices, cheap fish)
TOP_FLIPS = 10
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
TOP_RAID = 16           # consumables and materials shown in section 1 (the top players only)


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
    usual = statistics.median(p for _, p in s) if s else None
    halves = defaultdict(list)
    for t, p in s:
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
    hd = half_days(m, iid, cols)
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

def sold_day(m, iid, hours=72):
    """Estimated units sold per day over the last 3 days (listings that vanished before expiring, scan to scan)."""
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


def vc(m, iid):
    v = sold_day(m, iid)
    return '<td class="r mut">-</td>' if v is None else f'<td class="r">{v:,.0f}</td>'


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
    cons = [i for _, i in sorted(cons, key=lambda x: -x[0])][:TOP_RAID]     # most profitable to craft first
    mat_ids.sort(key=lambda i: -m.ah(i)[0] * len(mats[i]))                     # pricey + used in many consumes
    mat_ids = mat_ids[:TOP_RAID]
    last = cols[-1]

    def rows(title, ids):
        return (f'<tr class="grp"><td colspan="{3 + 2 * len(cols)}"><b>{title}</b></td></tr>'
                + "".join(f'<tr><td class="nm" data-iid="{i}">{R.esc(m.name(i))}</td><td class="r now">{sm(m.ah(i)[0])}</td>{vc(m, i)}'
                          f'{hilo_cells(m, i, cols)}</tr>' for i in ids))
    days = (R.RAID_DAY - date.today()).days
    note = (f'<div class="fn">Top {TOP_RAID} of each: consumables ranked by craft profit, materials by price x how many raid consumes use them. '
            f'No listings yet: {R.esc(", ".join(unlisted))}.</div>')
    return (f'<section><div class="sh"><span class="no">4</span><h2>Raid consumables</h2>'
            f'<span class="tag">{R.esc(R.RAIDS)} - Dec 9, {days} days</span></div>'
            f'<table class="g">{thead([("Item", "nm"), ("Now", "r"), ("Sold/day", "r")], cols, last)}<tbody>'
            f'{rows("Consumables - most profitable to craft", cons)}{rows("Materials - most used in raid consumes", mat_ids)}'
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
        days = [d for d in by_day(s).values() if len(d) >= 6]
        if len(days) < 2 or not all(zig_legs(d, FLIP_SWING) >= 3 for d in days):
            continue
        lo = statistics.mean(sorted(d)[1] for d in days)       # typical daily low (2nd lowest hour)
        hi = statistics.mean(sorted(d)[-2] for d in days)      # typical daily high (2nd highest hour)
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
        x["badge"] = ("nope", "pot", "cert")[x["rank"]]
        if x["rank"] and x["now"] and x["now"] > x["usual"] * 1.03:   # flips, but it's pricey right now - wait for the dip
            x["rank"], x["status"], x["badge"] = 0.5, "Above usual price", "wait"
        elif x["rank"] == 1 and not (x["now"] and x["now"] <= x["usual"] * POT_DIP):   # potential only when it's 15%+ under its weekly usual
            x["rank"], x["status"], x["badge"] = 0.4, "Waiting for dip", "dip"
    res.sort(key=lambda x: (-x["rank"], -x["per_stack"]))
    rows = []
    for x in res[:TOP_FLIPS]:
        pos = (x["now"] - x["lo"]) / max(1, x["hi"] - x["lo"]) if x["now"] else 0.5
        zone = "up" if pos <= 0.3 else "dn" if pos >= 0.7 else ""
        badge = x["badge"]
        rows.append(f'<tr class="row-{badge}"><td class="nm" data-iid="{x["iid"]}">{R.esc(m.name(x["iid"]))} <span class="badge {badge}">{x["status"]}</span></td><td class="r now {zone}">{sm(x["now"])}</td>'
                    f'<td class="r"><b>{R.chg(m.change_24h(x["iid"]))}</b></td><td class="r">{x["qty"]}</td>{vc(m, x["iid"])}'
                    f'{hilo_cells(m, x["iid"], cols)}</tr>')
    if not rows:
        rows.append(f'<tr><td colspan="{5 + 2 * len(cols)}" class="mut">Nothing passes yet.</td></tr>')
    return ('<section><div class="sh"><span class="no">1</span><h2>Flip list</h2>'
            '<span class="tag">40s+ each, 1g+ per stack, up &amp; down every day</span></div>'
            f'<table class="g">{thead([("Item", "nm"), ("Now", "r"), ("24h", "r"), ("Listed", "r"), ("Sold/day", "r")], cols, cols[-1])}<tbody>'
            + "".join(rows) + '</tbody></table>'
            f'<div class="fn">Each listed item went up, down and up again (7%+ moves) on every day scanned. '
            f'Certified flip = 3+ green/red/green (or red/green/red) streaks across the half-days in the last 7 days; '
            f'Potential flip = at least 1 streak in the last 3 days and priced 15%+ under its weekly usual right now (a buy); Waiting for dip = it has a streak but isn&rsquo;t 15% under its usual yet; Above usual price = it flips, but right now it&rsquo;s priced above its usual: sell any you have, and buy on tomorrow&rsquo;s dip; Not flippable yet = no streak. Tools are left out - a character only ever buys one, so their price only jumps on buyouts. '
            f'Only items where a full stack flips for 1g+ profit (buy near the daily low, sell near the daily high, after the 5% cut) make the list, best first. '
            f'24h = price change since this time yesterday. Sold/day = estimated units bought per day over the last 3 days (listings that disappeared before expiring; mass pull-outs ignored). '
            f'Now is <span class="up">green</span> when it&rsquo;s near the daily low (buy) and <span class="dn">red</span> near the daily high (sell). Cooldown items are in section 2.</div>'
            '</section>')


MANIP_MIN = 500   # 5s usual price - ignore trash
TOP_MANIP = 12


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
        if now > 1.3 * usual and q < 0.6 * uq:
            res.append(dict(iid=iid, now=now, usual=usual, q=q, uq=uq, man=m.manipulated(iid)))
    res.sort(key=lambda x: (-x["man"], -x["now"] / x["usual"]))
    rows = "".join(
        f'<tr><td class="nm" data-iid="{x["iid"]}">{R.esc(m.name(x["iid"]))}'
        + ('' if x["man"] else ' <span class="badge watch">Watch</span>') + '</td>'
        f'<td class="r now">{sm(x["now"])}</td><td class="r">{sm(x["usual"])}</td>'
        f'<td class="r"><b class="up">+{round((x["now"] / x["usual"] - 1) * 100)}%</b></td>'
        f'<td class="r">{x["q"]:,} <span class="mut">of {x["uq"]:,}</span></td>'
        f'<td class="r"><b class="dn">-{round((1 - x["q"] / x["uq"]) * 100)}%</b></td>{vc(m, x["iid"])}</tr>'
        for x in res[:TOP_MANIP])
    if not rows:
        rows = '<tr><td colspan="7" class="mut">nothing looks bought out right now</td></tr>'
    return ('<section><div class="sh"><span class="no">2</span><h2>Potential manipulation</h2>'
            '<span class="tag">cheap stock vanishing while the price climbs (all AH 113 goods worth 5s+)</span></div>'
            '<table class="g"><thead><tr><th>Item</th><th class="r">Now</th><th class="r">Usual</th><th class="r">vs usual</th>'
            '<th class="r">Listed (of usual)</th><th class="r">Stock</th><th class="r">Sold/day</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<div class="fn"><span class="badge manip">Manipulated</span> over 2x usual with under 40% of the usual listings - '
            'sell into it, don&rsquo;t buy, it usually falls back once people relist. '
            '<span class="badge watch">Watch</span> 30%+ over usual with stock down 40%+ - may be the start of a buyout.</div></section>')


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
                + (vc(m, iid) if iid else '<td class="r mut">-</td>'))
    rows = "".join(f'<tr><td class="nm">{p}</td>{cell(mid)}{cell(end)}</tr>' for p, mid, end in CAMP)
    return ('<section><div class="sh"><span class="no">5</span><h2>Campfire drops</h2>'
            '<span class="tag">camp features from dungeons - price now (listed)</span></div>'
            '<table class="g"><thead><tr><th>Profession</th><th>Mid-level drop (skill 140)</th><th class="r">Now</th><th class="r">Sold/day</th>'
            '<th>End-game drop (skill 300)</th><th class="r">Now</th><th class="r">Sold/day</th></tr></thead><tbody>' + rows + '</tbody></table>'
            '<div class="fn">Placed at a campfire for an hourly group buff. Tradeable, so you can buy one instead of farming the boss. '
            'Mid / end-game split is by dungeon level.</div></section>')


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
            '<table class="g"><thead><tr><th>Makes</th><th class="r">Sells</th><th class="r">Listed</th><th class="r">Sold/day</th>'
            '<th class="r">Farm mats</th><th class="r">Buy mats</th><th>Mats (* = farm or buy, rest from a vendor)</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            '<div class="fn">Farm mats = profit if you gather the * mats yourself and only pay the vendor for spices etc. '
            'Buy mats = profit if you buy every mat at today&rsquo;s cheapest (AH or vendor); red = loses money. Best farm profit first.</div></section>')


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
                + (' <span class="badge cert">BUY</span>' if n in buys else '') + '</td>')
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
            '<th class="r">Listed</th><th class="r">Sold/day</th><th class="r">Mats cost</th><th class="r">Profit each</th><th>Mats</th></tr></thead>'
            f'<tbody>{"".join(rows)}{"".join(wood)}</tbody></table>'
            '<div class="fn">Profit each = AH price after the 5% cut minus buying the mats at today&rsquo;s cheapest. '
            'Thick Logs cost about 20g at cooking supply vendors - more than a kit sells for - so only AH logs under ~15g pay. '
            '<span class="badge cert">BUY</span> = at or under the buy target (Thick Logs 13g, Expert Campfire Kit 15g).</div>')


def flip_badge(m, iid, cols):
    """Flip-list style badge for items kept out of the flip list (cooldown items): same streak rule and price check."""
    rank, status, _ = flip_status(m, iid, cols)
    s = [p for _, p in clean(m, iid)]
    if not rank or len(s) < 15 or statistics.median(s) < FLIP_MIN:
        return ""
    now = m.ah(iid)[0]
    if now and now > statistics.median(s) * 1.03:
        return ' <span class="badge wait">Above usual price</span>'
    if rank == 1 and not (now and now <= statistics.median(s) * POT_DIP):
        return ' <span class="badge dip">Waiting for dip</span>'
    return f' <span class="badge {("", "pot", "cert")[rank]}">{status}</span>'


def sec_cooldowns(m, cds, cols):
    rows, seen = [], set()
    last = cols[-1]
    for c in cds:
        rows.append(f'<tr class="grp"><td colspan="{3 + 2 * len(cols)}"><b>{R.esc(c["recipe"])}</b> '
                    f'<span class="mut">{R.esc(c["prof"])} {c["cd"]:.0f}h</span>'
                    + (f'<span class="pr">+{sm(c["profit"])} ({sm(c["per_day"])}/day)</span></td></tr>' if c["profit"] >= 0 else
                     f'<span class="pr dn">loses {sm(-c["profit"])} - sell the inputs instead</span></td></tr>'))
        for iid, nm, lab, note in ([(c["oid"], c["oname"], "", "")] + [(i, n, "in", "") for i, n, _ in c["ins"]]):
            if not iid or not m.ah(iid)[0]:
                continue
            rows.append(f'<tr><td class="nm {lab}" data-iid="{iid}">{R.esc(m.name(iid))}<span class="mut">{note}</span>{flip_badge(m, iid, cols)}</td><td class="r now">{sm(m.ah(iid)[0])}</td>{vc(m, iid)}'
                        f'{hilo_cells(m, iid, cols)}</tr>')
    return ('<section><div class="sh"><span class="no">3</span><h2>Cooldowns</h2>'
            '<span class="tag">1g+ profit, best per day first - flip badges use the same rules as the flip list</span></div>'
            f'<table class="g">{thead([("Makes / uses", "nm"), ("Now", "r"), ("Sold/day", "r")], cols, last)}'
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
.g .now{font-weight:600}
.g .d{text-align:center;padding:.6px 1px}
.g th.d{text-align:center}
.g td.hi{color:var(--up)}.g td.lo{color:var(--dn)}
.g .first{border-left:1px solid var(--rule)}
.g .ar{font-size:.8em;margin-left:1px}
.badge{border-radius:3px;padding:0 3px;font-weight:700;white-space:nowrap}
.badge.cert{background:#238636;color:#fff}.badge.pot{background:#d29922;color:#1b1300}.badge.wait{background:#1f6feb;color:#fff}.row-wait td{background:#0f1d33}.badge.dip{background:#3a3220;color:#e3b341}.badge.nope{background:#2a3038;color:#9aa3ad}.badge.manip{background:#8957e5;color:#fff}.sub{font-size:1em;margin:8px 0 3px;color:var(--ink)}.badge.watch{background:#3d2a5c;color:#d2b8ff}
.row-cert td{background:#12261a}.row-pot td{background:#2a2410}
.g th.dh{text-align:center;border-left:1px solid var(--rule);border-bottom:none;color:var(--ink);text-transform:none;letter-spacing:0}
.g .in{padding-left:9px;color:var(--ink2)}
.grp td{background:var(--band);padding:1px 2px}
.grp .pr{float:right;color:var(--up);font-weight:700}.grp .pr.dn{color:var(--dn)}
.g .mats{white-space:normal;color:var(--ink2)}
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


def header(title, last, extra):
    return (f'<header><div><h1>{title}</h1>'
            f'<div class="scope">WoW Forever - US - PvP ruleset - Horde auction house</div></div>'
            f'<div class="when">prices as of <b>{last:%a %b %d, %I:%M %p}</b><br>{extra}</div></header>')


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
    title = f"Hammy's {when} Market Report"
    body = (header(title, last, f'each day: <b>Morning</b> (12am-noon) and <b>Evening</b> (noon-12am), last {DAYS} days<br>'
                                f'<span class="up">&#9650; green = spiked: its highest price</span> &middot; '
                                f'<span class="dn">&#9660; red = dipped: its lowest price</span> &middot; grey = near usual<br>'
                                f'<span class="badge manip">Manipulated</span> = someone bought out the cheap stock: price is real now but artificial - sell into it, don&rsquo;t buy')
            + sec_flips(m, skip, cols) + sec_manip(m) + sec_cooldowns(m, cds, cols)
            + sec_raid(m, cols, skip) + sec_campfire(m)
            + sec_cooking(m).replace("</section>", sec_camp_cooking(m) + "</section>"))
    body = re.sub(r'(<td class="nm[^"]*" data-iid="(\d+)">.*?)(</td>)',   # "Manipulated" badge on bought-out items
                  lambda x: x.group(1) + (' <span class="badge manip">Manipulated</span>' if m.manipulated(int(x.group(2))) else '')
                  + x.group(3), body)
    market = render(title, body)
    try:   # web-page version: hover an item for its hour-by-hour 7-day chart
        import interactive
        interactive.make(m, title, body, market[:-4] + " Interactive.html")
    except Exception as e:
        print(f"interactive page failed: {e}")
    # profession routes: its own page
    title2 = f"Hammy's {when} Profession Routes 1-150"
    routes = render(title2, header(title2, last, "cheapest route at current prices") + sec_leveling(m))
    for p in (market, routes):
        try:
            snapshot(p)
        except Exception as e:
            print(f"snapshot failed for {os.path.basename(p)}: {e}")
    return market, routes


if __name__ == "__main__":
    print(build())
