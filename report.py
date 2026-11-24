#!/usr/bin/env python3
"""report.py - Horde AH Market Report (WoW Forever / US / PvP / Horde AH 113 only).

Builds output/Horde AH Market Report.html + .pdf from ah_113.sqlite + recipes_db.json.
Every chart is a static inline SVG, so the PDF has nothing scrollable or interactive.

Sections
  1. Raid prep          consumes for the Dec 9 raids, then every material that goes into them
  2. Gear for alts      low-level crafted weapons/armor that sell for 1g+ over their materials
  3. Cooldowns          every profession cooldown that clears 1g, with each material's price
  4. Leveling kits      cheapest 1-150 route per crafting profession at current prices
"""
import csv
import html
import json
import os
import sqlite3
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, date

import forever_ah as f
import plays

HERE = f.HERE
OUT = os.path.join(HERE, "output")
AH = "113"                       # Horde AH on the US PvP ruleset - the only market this report reads
CUT = f.AH_CUT
RAID_DAY = date(2026, 12, 9)
RAIDS = "Barrow Deeps (10), Hyjal Summit (20) and Onyxia's Lair (40)"
GEAR_MAX_LVL = 45
MIN_PROFIT = 10000               # 1 gold, in copper
EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]

# reagents sold by trade-supply vendors (price = vendor buy price from the item API)
VENDOR_SOLD = {"Spinefin Halibut"}   # finished goods vendors also sell: never worth more than the vendor price
VENDOR = {"Star Wood", "Thick Logs", "Empty Vial", "Leaded Vial", "Crystal Vial", "Imbued Vial", "Coarse Thread", "Fine Thread",
          "Silken Thread", "Heavy Silken Thread", "Rune Thread", "Weak Flux", "Strong Flux", "Salt",
          "Mild Spices", "Hot Spices", "Soothing Spices", "Simple Flour", "Refreshing Spring Water",
          "Ice Cold Milk", "Morning Glory Dew", "Copper Rod", "Wooden Stock", "Heavy Stock", "Simple Wood",
          "Gray Dye", "Blue Dye", "Red Dye", "Green Dye", "Yellow Dye", "Purple Dye", "Black Dye", "Bleach",
          "Pink Dye", "Orange Dye", "Flask of Oil", "Rhapsody Malt", "Shiny Red Apple", "Melon Juice",
          "Skin of Dwarven Stout", "Flagon of Dwarven Honeymead", "Thunder Ale", "Bottle of Dalaran Noir",
          "Holy Salts", "Exquisite Spices", "Blank Parchment", "Light Parchment"}


def esc(s):
    return html.escape(str(s))


def money(c, short=False):
    if c is None:
        return "-"
    c = int(round(c))
    neg, c = c < 0, abs(c)
    g, s, cp = c // 10000, c // 100 % 100, c % 100
    if g >= 100 or (short and g):
        out = f"{g}g" if not s or g >= 100 else f"{g}g {s}s"
    elif g:
        out = f"{g}g {s}s"
    elif s:
        out = f"{s}s {cp}c" if cp and not short else f"{s}s"
    else:
        out = f"{cp}c"
    return ("-" if neg else "") + out


# --------------------------------------------------------------------------- data

VIEW_DAYS = 14   # day-by-day columns and charts show up to 14 days


class Market:
    def __init__(self):
        cfg = f.load_config()
        assert str(cfg.get("RULESET")) == "1" and AH in f.auction_houses(cfg), "report is for US PvP Horde AH 113 only"
        self.con = sqlite3.connect(os.path.join(HERE, f"ah_{AH}.sqlite"))
        self.last = self.con.execute("SELECT max(ts) FROM allsnap").fetchone()[0]
        self.first = self.con.execute("SELECT min(ts) FROM snap").fetchone()[0]
        self.all_first = self.con.execute("SELECT min(ts) FROM allsnap").fetchone()[0]
        self.t0 = max(self.first, self.last - 7 * 86400)
        self.n_scans = self.con.execute("SELECT count(DISTINCT ts) FROM snap WHERE ts>=?", (self.t0,)).fetchone()[0]
        # 14 days are loaded for the day-by-day columns and charts; every calculation uses the last 7 (self.rows)
        self.t0_long = max(self.first, self.last - VIEW_DAYS * 86400)
        self.rows_long = plays.load_rows(self.con, self.t0_long)   # {iid: [(ts, floor, qty, sold)]}
        self.rows = {i: [x for x in v if x[0] >= self.t0] for i, v in self.rows_long.items()}
        self.rows = {i: v for i, v in self.rows.items() if v}
        self.inflated = {}   # iid -> the silly asking price ah() replaced with the typical price
        self.gear = defaultdict(list)
        try:
            for ts, iid, mn, md, n in self.con.execute(
                    "SELECT ts, item_id, min_buyout, median_buyout, auctions FROM gearsnap WHERE ts>=?", (self.t0,)):
                self.gear[iid].append((ts, mn, n, md))
        except sqlite3.OperationalError:
            pass
        self.gear_last = max((r[-1][0] for r in self.gear.values() if r), default=None)
        for v in self.gear.values():
            v.sort()
        db = json.load(open(os.path.join(HERE, "recipes_db.json"), encoding="utf-8"))
        self.items = {int(k): v for k, v in db["items"].items()}
        for iid, name, cls, sub, lvl, vs in self.con.execute("SELECT * FROM iteminfo"):
            self.items.setdefault(iid, dict(name=name, cls=cls, sub=sub, req=lvl, vendor=vs))
        self.recipes = [r for r in db["recipes"] if r["prof"] != "Test Profession [DNT]" and r.get("reagents")]
        self.by_name = defaultdict(list)
        self.by_out = defaultdict(list)
        for r in self.recipes:
            self.by_name[(r["prof"], r["name"].lower())].append(r)
            if r.get("item_id"):
                self.by_out[r["item_id"]].append(r)
        self.name_id = {}
        for iid, v in self.items.items():
            if v.get("name"):
                n = v["name"].lower()
                if n not in self.name_id or iid in self.rows:
                    self.name_id[n] = iid

    def name(self, iid):
        return (self.items.get(iid) or {}).get("name") or str(iid)

    def ah(self, iid):
        """(price per unit, qty) at the last scan, or (None, 0) if it isn't listed right now.
        Thin markets: when fewer than 20 are listed and the cheapest is over 2x its 3-day typical price, one seller is
        asking a silly price (13 Mrrggl Smrrthle at 1g each when it sells for 2-5s) - use the typical price instead,
        the same one the charts show. Items vendors also sell never count above the vendor price."""
        r = self.rows.get(iid)
        if not (r and r[-1][0] >= self.last - 2 * 3600):
            return None, 0
        p, q = r[-1][1], r[-1][2]
        if q < 20:
            hist = sorted(x[1] for x in r if x[0] >= self.last - 3 * 86400 and x[1])
            typ = hist[len(hist) // 2] if len(hist) >= 6 else None
            if typ and p > 2 * typ:
                self.inflated[iid] = p
                p = typ
        it = self.items.get(iid) or {}
        if it.get("name") in VENDOR_SOLD and it.get("buy"):
            p = min(p, it["buy"])
        return p, q

    def manipulated(self, iid):
        """Just bought out and still inflated: plenty listed (20+), the cheapest RIGHT NOW is over 2x its weekly usual,
        listings are under 40% of their usual count, and within the last 24h it was near usual (under 1.3x) with
        at least twice today's stock - so the jump is a fresh buyout, not a normal swing or an old one that faded."""
        r = self.rows.get(iid)
        if not (r and r[-1][0] >= self.last - 2 * 3600) or r[-1][2] < 20:
            return False
        week = [x for x in r if x[0] >= self.last - 7 * 86400 and x[1]]
        if len(week) < 12:
            return False
        ps, qs = sorted(x[1] for x in week), sorted(x[2] for x in week)
        usual, usual_q = ps[len(ps) // 2], qs[len(qs) // 2]
        now, q = r[-1][1], r[-1][2]
        if now <= 2 * usual or q >= 0.4 * usual_q:
            return False
        before = [x for x in r[:-1] if x[0] >= self.last - 24 * 3600]
        return any(p <= 1.3 * usual and bq >= 2 * q for _, p, bq, *_ in before)

    def gear_now(self, iid):
        r = self.gear.get(iid)
        if r and self.gear_last and r[-1][0] >= self.gear_last - 2 * 3600:
            return r[-1][1], r[-1][2]
        return None, 0

    def vendor(self, iid):
        it = self.items.get(iid) or {}
        return it.get("buy") if it.get("name") in VENDOR and it.get("buy") else None

    def cost(self, iid, prof=None, depth=0, seen=()):
        """Cheapest way to get one: buy on the AH, buy from a vendor, or craft it (same profession only).
        Returns (copper or None, how) - how is 'ah', 'vendor' or ('craft', recipe)."""
        opts = []
        p, _ = self.ah(iid)
        if p:
            opts.append((p, "ah"))
        v = self.vendor(iid)
        if v:
            opts.append((v, "vendor"))
        if prof and depth < 3 and iid not in seen:
            for r in self.by_out.get(iid, []):
                if r["prof"] != prof:
                    continue
                c = self.recipe_cost(r, depth + 1, seen + (iid,))
                if c is not None:
                    opts.append((c / (r.get("qty") or 1), ("craft", r)))
        return min(opts, key=lambda o: o[0]) if opts else (None, None)

    def recipe_cost(self, r, depth=0, seen=()):
        tot = 0
        for iid, _, q in r["reagents"]:
            c, _ = self.cost(iid, r["prof"], depth, seen)
            if c is None:
                return None
            tot += c * q
        return tot

    def leaves(self, r, n, out, depth=0):
        """Shopping list for n crafts of recipe r, expanding intermediates that are cheaper to craft."""
        for iid, _, q in r["reagents"]:
            c, how = self.cost(iid, r["prof"], depth)
            if isinstance(how, tuple) and depth < 3:
                sub = how[1]
                self.leaves(sub, n * q / (sub.get("qty") or 1), out, depth + 1)
            else:
                out[iid] = out.get(iid, 0) + n * q
        return out

    def series(self, iid, gear=False, long=False):
        rows = [(ts, p) for ts, p, *_ in (self.gear if gear else self.rows_long if long else self.rows).get(iid, [])]
        b = defaultdict(list)
        for ts, p in rows:
            b[ts // 3600].append(p)
        return sorted((h * 3600, statistics.median(v)) for h, v in b.items())

    def vs_med(self, iid):
        """Price now vs its median over the charted period."""
        now, _ = self.ah(iid)
        s = [p for _, p in self.series(iid)]
        return now / statistics.median(s) - 1 if now and len(s) >= 4 else None

    def change_24h(self, iid):
        r = self.rows.get(iid)
        if not r:
            return None
        then = plays.price_at(r, self.last - 86400)
        now, _ = self.ah(iid)
        return now / then - 1 if then and now else None


# --------------------------------------------------------------------------- chart

def chart(m, pts, w=230, h=46, gear=False):
    """Static SVG line chart of hourly price from the first scan to the last one."""
    if not pts:
        return '<div class="nochart">no listings yet</div>'
    t0, t1 = pts[0][0], (m.gear_last if gear else m.last) or m.last
    t1 = max(t1, m.last)
    padl, padr, padt, padb = 2, 40, 4, 11
    pw, ph = w - padl - padr, h - padt - padb
    vals = [p for _, p in pts]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        lo, hi = lo * 0.95, hi * 1.05 or 1
    X = lambda t: padl + (t - t0) / max(1, t1 - t0) * pw
    Y = lambda p: padt + (hi - p) / (hi - lo) * ph
    parts = [f'<svg class="ch" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
             f'<rect x="{padl}" y="{padt}" width="{pw}" height="{ph}" class="bg"/>']
    # midnight ticks
    d = datetime.fromtimestamp(t0).replace(hour=0, minute=0, second=0, microsecond=0)
    while True:
        d = datetime.fromtimestamp(d.timestamp() + 86400 + 3600).replace(hour=0, minute=0)
        if d.timestamp() > t1:
            break
        x = X(d.timestamp())
        parts.append(f'<line x1="{x:.1f}" y1="{padt}" x2="{x:.1f}" y2="{padt + ph}" class="day"/>')
        if x < padl + pw - 14:
            parts.append(f'<text x="{x + 2:.1f}" y="{h - 3}" class="tick">{d:%a}</text>')
    med = statistics.median(vals)
    parts.append(f'<line x1="{padl}" y1="{Y(med):.1f}" x2="{padl + pw}" y2="{Y(med):.1f}" class="med"/>')
    # line, broken where a scan is missing for 3+ hours
    seg, path = None, []
    for t, p in pts:
        if seg is not None and t - seg > 3 * 3600:
            path.append("M")
        path.append(f"{X(t):.1f},{Y(p):.1f}")
        seg = t
    dstr, cmd = "", "M"
    for tok in path:
        if tok == "M":
            cmd = "M"
            continue
        dstr += f"{cmd}{tok} "
        cmd = "L"
    parts.append(f'<path d="{dstr}" class="ln"/>')
    if len(pts) == 1:
        parts.append(f'<circle cx="{X(pts[0][0]):.1f}" cy="{Y(pts[0][1]):.1f}" r="2.5" class="dot"/>')
    tl, pl = pts[-1]
    parts.append(f'<circle cx="{X(tl):.1f}" cy="{Y(pl):.1f}" r="2.4" class="dot"/>')
    parts.append(f'<text x="{w - padr + 4}" y="{padt + 7}" class="ax">{money(hi, True)}</text>')
    parts.append(f'<text x="{w - padr + 4}" y="{padt + ph}" class="ax">{money(lo, True)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def chg(c):
    if c is None:
        return '<span class="mut">-</span>'
    cls = "up" if c > 0.03 else "dn" if c < -0.03 else "mut"
    txt = "0%" if abs(c) < 0.005 else f"{c * 100:+.0f}%"
    return f'<span class="{cls}">{txt}</span>'


def profit_cell(p):
    if p is None:
        return '<span class="mut">-</span>'
    return f'<b class="{"up" if p > 0 else "dn"}">{money(p)}</b>'


# --------------------------------------------------------------------------- sections

def section_raid(m):
    rows, mats = [], defaultdict(set)
    groups = []
    for r in csv.DictReader(open(os.path.join(HERE, "raid_consumes.csv"), encoding="utf-8")):
        iid = m.name_id.get(r["item"].lower())
        rec = [x for x in m.by_out.get(iid, [])] if iid else []
        if not rec:
            continue
        rec = min(rec, key=lambda x: m.recipe_cost(x) or 1e18)
        price, qty = m.ah(iid)
        cost = m.recipe_cost(rec)
        per = rec.get("qty") or 1
        profit = price * per * (1 - CUT) - cost if price and cost is not None else None
        if not price and cost is None:
            continue
        for rid, _, q in rec["reagents"]:
            mats[rid].add(r["item"])
            c, how = m.cost(rid, rec["prof"])
            if isinstance(how, tuple):
                for sid, _, _ in how[1]["reagents"]:
                    mats[sid].add(m.name(rid))
        if r["group"] not in groups:
            groups.append(r["group"])
        rows.append(dict(g=r["group"], iid=iid, name=r["item"], prof=rec["prof"], price=price, qty=qty,
                         cost=cost, profit=profit, per=per,
                         mats=", ".join(f"{q:g} {n}" for _, n, q in rec["reagents"])))
    days = (RAID_DAY - date.today()).days
    h = [f'<section><div class="sh"><span class="no">1</span><h2>Raid prep</h2>'
         f'<span class="tag">{esc(RAIDS)} open Dec 9 - {days} days</span></div>',
         '<p class="lede">Consumes raiders will burn through from opening night. Profit = cheapest listing '
         'after the 5% cut, minus buying every material at today\'s floor (vendor price for vials, spices and thread).</p>',
         '<h3>Consumes</h3><table class="t"><thead><tr><th>Consume</th><th>Made with</th><th class="r">Price</th>'
         '<th class="r">Mats cost</th><th class="r">Profit</th><th class="r">Listed</th>'
         '<th class="r">vs usual</th><th>Hourly price</th></tr></thead><tbody>']
    for g in groups:
        h.append(f'<tr class="grp"><td colspan="8">{esc(g)}</td></tr>')
        for r in sorted([x for x in rows if x["g"] == g], key=lambda x: -(x["profit"] or -1e12)):
            h.append(f'<tr><td><b>{esc(r["name"])}</b><div class="sub">{esc(r["prof"])}'
                     f'{f" - makes {r["per"]:g}" if r["per"] != 1 else ""}</div></td>'
                     f'<td class="mats">{esc(r["mats"])}</td>'
                     f'<td class="r">{money(r["price"]) if r["price"] else "<span class=mut>none listed</span>"}</td>'
                     f'<td class="r">{money(r["cost"])}</td><td class="r">{profit_cell(r["profit"])}</td>'
                     f'<td class="r">{r["qty"] or "-"}</td><td class="r">{chg(m.vs_med(r["iid"]))}</td>'
                     f'<td>{chart(m, m.series(r["iid"]))}</td></tr>')
    h.append("</tbody></table>")

    # materials
    ah_mats = [i for i in mats if m.ah(i)[0]]
    vend = sorted({m.name(i) for i in mats if not m.ah(i)[0] and m.vendor(i)})
    missing = sorted({m.name(i) for i in mats if not m.ah(i)[0] and not m.vendor(i)})
    h.append('<h3 class="pb">Every material for those consumes</h3><table class="t"><thead><tr><th>Material</th>'
             '<th>Goes into</th><th class="r">Price</th><th class="r">7-day low</th><th class="r">7-day high</th>'
             '<th class="r">Listed</th><th class="r">vs usual</th><th>Hourly price</th></tr></thead><tbody>')
    for i in sorted(ah_mats, key=lambda i: -m.ah(i)[0]):
        p, q = m.ah(i)
        s = [v for _, v in m.series(i)]
        used = sorted(mats[i])
        u = ", ".join(used[:4]) + (f" +{len(used) - 4} more" if len(used) > 4 else "")
        h.append(f'<tr><td><b>{esc(m.name(i))}</b></td><td class="mats">{esc(u)}</td><td class="r">{money(p)}</td>'
                 f'<td class="r">{money(min(s))}</td><td class="r">{money(max(s))}</td><td class="r">{q}</td>'
                 f'<td class="r">{chg(m.vs_med(i))}</td><td>{chart(m, m.series(i))}</td></tr>')
    h.append("</tbody></table>")
    if vend:
        h.append(f'<p class="note">Bought from trade vendors, not the AH: {esc(", ".join(vend))}.</p>')
    if missing:
        h.append(f'<p class="note">Not listed on the AH right now (farm or wait for a listing): {esc(", ".join(missing))}.</p>')
    h.append("</section>")
    return "".join(h)


def section_gear(m):
    rows = []
    for r in m.recipes:
        iid = r.get("item_id")
        it = m.items.get(iid) or {}
        if it.get("cls") not in ("Weapon", "Armor") or not it.get("req") or it["req"] > GEAR_MAX_LVL:
            continue
        price, n = m.gear_now(iid)
        if not price:
            continue
        cost = m.recipe_cost(r)
        if cost is None:
            continue
        profit = price * (r.get("qty") or 1) * (1 - CUT) - cost
        if profit < MIN_PROFIT:
            continue
        rows.append(dict(r=r, iid=iid, it=it, price=price, n=n, cost=cost, profit=profit))
    rows.sort(key=lambda x: -x["profit"])
    best = {}
    for x in rows:                       # one row per item (some have two recipes)
        best.setdefault(x["iid"], x)
    rows = list(best.values())[:40]
    h = ['<section class="pb"><div class="sh"><span class="no">2</span><h2>Gear for alts</h2>'
         f'<span class="tag">crafted, level {GEAR_MAX_LVL} and under, 1g+ profit</span></div>',
         '<p class="lede">Weapons and armor people buy for leveling alts. Price is the cheapest copy listed now; '
         '"Listed" is how many copies you\'d be competing with. Weapon and armor prices only started recording '
         'on Nov 23, so these charts are short for now.</p>',
         '<table class="t"><thead><tr><th>Item</th><th>Materials</th><th class="r">Cheapest listed</th>'
         '<th class="r">Mats cost</th><th class="r">Profit</th><th class="r">Listed</th><th>Hourly price</th>'
         '</tr></thead><tbody>']
    for x in rows:
        it, r = x["it"], x["r"]
        warn = ' <span class="flag">only 1 listed</span>' if x["n"] == 1 else ""
        h.append(f'<tr><td><b>{esc(it["name"])}</b>{warn}<div class="sub">{esc(r["prof"])} - level {it["req"]} '
                 f'{esc(it.get("sub") or "")}{" " + esc(it["slot"]) if it.get("slot") and it.get("slot") not in (it.get("sub"),) else ""}</div></td>'
                 f'<td class="mats">{esc(", ".join(f"{q:g} {n}" for _, n, q in r["reagents"]))}</td>'
                 f'<td class="r">{money(x["price"])}</td><td class="r">{money(x["cost"])}</td>'
                 f'<td class="r">{profit_cell(x["profit"])}</td><td class="r">{x["n"]}</td>'
                 f'<td>{chart(m, m.series(x["iid"], gear=True), gear=True)}</td></tr>')
    if not rows:
        h.append('<tr><td colspan="7" class="mut">Nothing clears 1g right now.</td></tr>')
    h.append("</tbody></table></section>")
    return "".join(h)


def section_cooldowns(m):
    cards = []
    for r in csv.DictReader(open(os.path.join(HERE, "recipes.csv"), encoding="utf-8")):
        cd = float(r["cooldown_hours"] or 0)
        if cd <= 0:
            continue
        out = m.name_id.get(r["output"].lower())
        ins = [(m.name_id.get(n.strip().lower()), n.strip(), float(q))
               for n, q in (p.rsplit(":", 1) for p in r["inputs"].split("|"))]
        oq = float(r["output_qty"] or 1)
        price, qty = m.ah(out) if out else (None, 0)
        if price is None and out:
            price, qty = m.gear_now(out)
        costs = [(i, n, q, m.cost(i)[0] if i else None) for i, n, q in ins]
        if not price or any(c is None for *_, c in costs):
            continue
        cost = sum(c * q for _, _, q, c in costs)
        profit = price * oq * (1 - CUT) - cost
        if profit < MIN_PROFIT:
            continue
        cards.append(dict(recipe=r["recipe"], prof=r.get("profession") or "", cd=cd, out=out, oname=r["output"],
                          oq=oq, price=price, qty=qty, costs=costs, cost=cost, profit=profit,
                          per_day=profit * 24 / cd))
    cards.sort(key=lambda c: -c["per_day"])
    h = ['<section class="pb"><div class="sh"><span class="no">3</span><h2>Profession cooldowns</h2>'
         '<span class="tag">only crafts that clear 1g</span></div>',
         '<p class="lede">Each cooldown with the price of what it makes and of every material. '
         'Per day = profit spread over the cooldown, so you can compare a 2-day transmute with a 4-day Mooncloth. '
         'If alchemy transmutes share one cooldown on Forever (check your spellbook after one), only the top transmute here is worth doing.</p>',
         '<table class="t"><thead><tr><th>Cooldown / item</th><th class="r">Price</th><th class="r">Qty</th>'
         '<th class="r">Listed</th><th class="r">vs usual</th><th>Hourly price</th></tr></thead>']
    for c in cards:
        h.append(f'<tbody class="card"><tr class="grp2"><td colspan="6"><b>{esc(c["recipe"])}</b> '
                 f'<span class="sub">{esc(c["prof"])} - {c["cd"]:.0f}h cooldown</span>'
                 f'<span class="pr">profit {profit_cell(c["profit"])} per craft '
                 f'<span class="mut">({money(c["per_day"])}/day)</span></span></td></tr>')
        h.append(f'<tr><td>makes <b>{esc(c["oname"])}</b></td><td class="r">{money(c["price"])}</td>'
                 f'<td class="r">x{c["oq"]:g}</td><td class="r">{c["qty"]}</td><td class="r">{chg(m.vs_med(c["out"]))}</td>'
                 f'<td>{chart(m, m.series(c["out"]))}</td></tr>')
        for i, n, q, cost in c["costs"]:
            p, lq = m.ah(i)
            src = "" if p and p <= cost else ' <span class="mut">(vendor)</span>'
            h.append(f'<tr><td class="in">uses {esc(n)}{src}</td><td class="r">{money(cost)}</td><td class="r">x{q:g}</td>'
                     f'<td class="r">{lq or "-"}</td><td class="r">{chg(m.vs_med(i))}</td>'
                     f'<td>{chart(m, m.series(i)) if p else ""}</td></tr>')
        h.append("</tbody>")
    if not cards:
        h.append('<tbody><tr><td colspan="6" class="mut">No cooldown clears 1g right now.</td></tr></tbody>')
    h.append("</table></section>")
    return "".join(h)


def section_leveling(m):
    steps = defaultdict(lambda: defaultdict(list))
    order = []
    for r in csv.DictReader(open(os.path.join(HERE, "leveling.csv"), encoding="utf-8")):
        if r["profession"] not in order:
            order.append(r["profession"])
        steps[r["profession"]][(int(r["from"]), int(r["to"]), int(r["crafts"]))].append(r["recipe"])
    h = ['<section class="pb"><div class="sh"><span class="no">4</span><h2>1-150 leveling kits</h2>'
         '<span class="tag">cheapest route at current prices</span></div>',
         '<p class="lede">Where a skill range has more than one good recipe, the cheapest one right now is picked '
         '(the others are listed under it). Intermediates you can make yourself - bolts of cloth, tubes, bolts, '
         'Minor Healing Potions - are crafted when that beats buying them. Blizzard\'s data doesn\'t include skill-up '
         'levels for Forever, so the brackets follow classic and the craft counts are estimates; check the '
         'recipe\'s color at your trainer.</p>']
    for prof in order:
        rows, shop, total, unknown = [], {}, 0, []
        for (lo, hi, n), names in sorted(steps[prof].items()):
            opts = []
            for nm in names:
                for r in m.by_name.get((prof, nm.lower()), [])[:1]:
                    c = m.recipe_cost(r)
                    opts.append((c if c is not None else 1e15, nm, r, c))
            if not opts:
                continue
            opts.sort(key=lambda o: o[0])
            _, nm, r, c = opts[0]
            if c is None:
                unknown += [m.name(i) for i, _, _ in r["reagents"] if m.cost(i, prof)[0] is None]
            else:
                total += c * n
                m.leaves(r, n, shop)
            alts = ", ".join(f"{o[1]} {money(o[3] * n) if o[3] is not None else '(mats not listed)'}"
                             for o in opts[1:])
            rows.append(f'<tr><td class="r nw">{lo}-{hi}</td><td><b>{esc(nm)}</b>'
                        f'{f"<div class=sub>instead of: {esc(alts)}</div>" if alts else ""}</td>'
                        f'<td class="mats">{esc(", ".join(f"{q:g} {x}" for _, x, q in r["reagents"]))}</td>'
                        f'<td class="r">{n}</td><td class="r">{money(c)}</td>'
                        f'<td class="r"><b>{money(c * n) if c is not None else "-"}</b></td></tr>')
        h.append(f'<div class="prof"><div class="ph"><h3>{esc(prof)}</h3><span class="kit">kit total '
                 f'<b>{money(total)}</b>{" + unlisted mats" if unknown else ""}</span></div>'
                 '<table class="t"><thead><tr><th class="r">Skill</th><th>Craft</th><th>Each needs</th>'
                 '<th class="r">Crafts</th><th class="r">Each</th><th class="r">Step cost</th></tr></thead><tbody>'
                 + "".join(rows) + "</tbody></table>")
        h.append('<table class="t shop"><thead><tr><th>Shopping list</th><th class="r">Qty</th>'
                 '<th class="r">Each</th><th class="r">Total</th><th class="r">Listed</th><th>Hourly price</th></tr></thead><tbody>')
        for i, q in sorted(shop.items(), key=lambda kv: -(m.cost(kv[0])[0] or 0) * kv[1]):
            c, how = m.cost(i)
            q = int(-(-q // 1))
            p, lq = m.ah(i)
            src = "vendor" if how == "vendor" else ""
            enough = "" if how != "ah" or lq >= q else f' <span class="flag">only {lq} listed</span>'
            h.append(f'<tr><td>{esc(m.name(i))}{f" <span class=mut>({src})</span>" if src else ""}{enough}</td>'
                     f'<td class="r">{q}</td><td class="r">{money(c)}</td><td class="r">{money(c * q)}</td>'
                     f'<td class="r">{lq or "-"}</td><td>{chart(m, m.series(i), w=200, h=36) if how == "ah" else ""}</td></tr>')
        h.append("</tbody></table>")
        if unknown:
            h.append(f'<p class="note">Not on the AH right now: {esc(", ".join(sorted(set(unknown))))}.</p>')
        h.append("</div>")
    h.append("</section>")
    return "".join(h)


CSS = """
:root{--ink:#1b1f24;--ink2:#4a525c;--mut:#8a929c;--line:#e3e6ea;--bg:#ffffff;--band:#f5f6f8;
--acc:#1f6feb;--up:#1a7f37;--dn:#cf222e;--warn:#9a6700}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font:11.5px/1.4 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:22px 22px 30px}
header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid var(--ink);padding-bottom:10px;margin-bottom:6px}
header h1{font-size:24px;margin:0;letter-spacing:-.3px}
header .scope{color:var(--ink2);font-size:12px;margin-top:2px}
header .when{text-align:right;color:var(--ink2);font-size:11px}
header .when b{color:var(--ink);font-size:13px}
.legend{color:var(--mut);font-size:10.5px;margin:6px 0 14px;display:flex;gap:14px;align-items:center}
.legend svg{vertical-align:middle}
section{margin-top:18px}
.sh{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.sh .no{background:var(--ink);color:#fff;font-weight:700;width:24px;height:24px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:13px}
.sh h2{font-size:18px;margin:0}
.sh .tag{margin-left:auto;color:var(--ink2);background:var(--band);border-radius:20px;padding:3px 10px;font-size:11px}
.lede{color:var(--ink2);margin:4px 0 10px;max-width:880px}
h3{font-size:13px;margin:14px 0 6px}
table.t{width:100%;border-collapse:collapse;margin-bottom:8px}
.t th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:600;border-bottom:1.5px solid var(--ink);padding:4px 6px}
.t td{border-bottom:1px solid var(--line);padding:3px 6px;vertical-align:middle}
.t tr{break-inside:avoid;page-break-inside:avoid}
.t .r{text-align:right;white-space:nowrap}
.t .nw{white-space:nowrap}
.t .mats{color:var(--ink2);font-size:10.5px;max-width:230px}
.t .sub{color:var(--mut);font-size:10px}
.t .in{padding-left:18px;color:var(--ink2)}
.grp td{background:var(--band);font-weight:700;font-size:11px;padding:4px 6px;color:var(--ink2);text-transform:uppercase;letter-spacing:.05em}
.grp2 td{background:var(--band);padding:5px 6px}
.grp2 .pr{float:right}
tbody.card{break-inside:avoid;page-break-inside:avoid}
.up{color:var(--up)}.dn{color:var(--dn)}.mut{color:var(--mut)}
.flag{color:var(--warn);font-size:9.5px;border:1px solid #e6cf9a;border-radius:3px;padding:0 3px;white-space:nowrap}
.note{color:var(--ink2);font-size:10.5px;margin:4px 0}
.prof{margin:14px 0 18px;break-inside:avoid-page}
.ph{display:flex;align-items:baseline;justify-content:space-between;border-bottom:2px solid var(--acc);margin-bottom:2px}
.ph h3{margin:0 0 3px;font-size:15px;color:var(--acc)}
.kit{font-size:12px;color:var(--ink2)}.kit b{color:var(--ink);font-size:14px}
.shop{width:auto;min-width:62%}
.ch{display:block}
.ch .bg{fill:#fafbfc}
.ch .day{stroke:#d5d9de;stroke-width:.7}
.ch .med{stroke:#9aa3ad;stroke-width:.8;stroke-dasharray:2 2}
.ch .ln{fill:none;stroke:var(--acc);stroke-width:1.6;stroke-linejoin:round}
.ch .dot{fill:var(--acc)}
.ch .tick{font-size:8px;fill:var(--mut)}
.ch .ax{font-size:8.5px;fill:var(--ink2)}
.nochart{color:var(--mut);font-size:10px}
.pb{break-before:page;page-break-before:always}
@page{size:letter;margin:.35in}
@media print{.wrap{padding:0;max-width:none}section{margin-top:0}h3.pb{break-before:page}}
"""


def build():
    m = Market()
    last = datetime.fromtimestamp(m.last)
    demo = ('<svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke="#1f6feb" stroke-width="1.6"/></svg> hourly price'
            '<svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke="#9aa3ad" stroke-dasharray="2 2"/></svg> its median'
            '<svg width="10" height="10"><line x1="5" y1="0" x2="5" y2="10" stroke="#d5d9de"/></svg> midnight'
            '<span>right-hand labels = highest / lowest price on the chart</span>'
            f'<span>each chart starts at the item&rsquo;s first scan and ends at the last one</span>')
    body = (f'<div class="wrap"><header><div><h1>Horde AH Market Report</h1>'
            f'<div class="scope">WoW Forever - US - PvP ruleset - Horde auction house</div></div>'
            f'<div class="when">prices as of the last scan<br><b>{last:%a %b %d, %I:%M %p}</b><br>'
            f'{m.n_scans} hourly scans since {datetime.fromtimestamp(m.t0):%a %b %d}</div></header>'
            f'<div class="legend">{demo}</div>'
            + section_raid(m) + section_gear(m) + section_cooldowns(m) + section_leveling(m) + "</div>")
    page = f'<!doctype html><html><head><meta charset="utf-8"><title>Horde AH Market Report</title><style>{CSS}</style></head><body>{body}</body></html>'
    os.makedirs(OUT, exist_ok=True)
    hp = os.path.join(OUT, "Horde AH Market Report.html")
    open(hp, "w", encoding="utf-8").write(page)
    pdf = os.path.join(OUT, "Horde AH Market Report.pdf")
    edge = next((e for e in EDGE if os.path.exists(e)), None)
    if edge:
        subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf}", "file:///" + hp.replace("\\", "/")], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    return hp, pdf


if __name__ == "__main__":
    hp, pdf = build()
    print("wrote", hp, "and", pdf)
