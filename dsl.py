"""dsl.py - Durotar Supply and Logistics guide (WoW Forever, Horde): the cheapest way to the rep and Merchant's Favor
each profession needs for the DSL camp's recipes, priced from AH 113 right now.

Data: dsl_vendor.csv (recipe | standing | Favor), dsl_writs.csv (150 Craftsman's Writ orders), Waylaid Crate bundle
lists from Blizzard item data (dsl_crates.json). Rep/Favor rules from wowcalc / ConquestCapped / wow-professions (Nov 2026):
  writs: Journeyman 75, Expert 125, Artisan 200 rep, no Favor; each different writ counts once per day
  crates: no rep; Favor Apprentice 5/10, Journeyman 10/20, Expert 15/30 (Textiles/Ore/Herbs/Curiosities vs Fabrics/Ingots/Parts)
  first crate ever: Shipping Label quest +2,000 rep +50 Favor; Friendly: Forged Delivery Receipt (15 Favor) + empty crate = +50 rep
"""
import csv
import json
import os
import re
from collections import defaultdict

import report as R

HERE = os.path.dirname(os.path.abspath(__file__))
STAND = {"Neutral": 0, "Friendly": 3000, "Honored": 9000}   # rep from the start of Neutral
LABEL_REP, LABEL_FAVOR, RECEIPT_FAVOR, RECEIPT_REP = 2000, 50, 15, 50
CRATE_FAVOR = {"Apprentice": (5, 10), "Journeyman": (10, 20), "Expert": (15, 30)}
CRATE_LEVEL = {"Apprentice": 10, "Journeyman": 10, "Expert": 20, "Artisan": 35}
BIG = ("Fabrics", "Ingots", "Parts")
CANT_FILL = {("Linen Cloth", 40)}   # known bug: needs 40 but a stack is 20


def _rows(name):
    return list(csv.DictReader(open(os.path.join(HERE, name), encoding="utf-8"), delimiter="|"))


def item_cost(m, iid):
    """Cheapest way to get one right now: AH (commodity or gear), vendor, or crafting it (any profession)."""
    if not iid:
        return None
    opts = [p for p in (m.ah(iid)[0], m.gear_now(iid)[0], m.vendor(iid)) if p]
    for r in m.by_out.get(iid, []):
        c = m.recipe_cost(r)
        if c is not None:
            opts.append(c / (r.get("qty") or 1))
    return min(opts) if opts else None


def writs(m):
    out = []
    for w in _rows("dsl_writs.csv"):
        wid = m.name_id.get(w["writ"].lower())
        wp = (m.ah(wid)[0] or m.gear_now(wid)[0]) if wid else None
        iid = m.name_id.get(w["item"].lower())
        ic = item_cost(m, iid)
        if not wp or ic is None:
            continue
        q, rep = int(w["qty"]), int(w["rep"])
        tot = wp + ic * q
        out.append(dict(wid=wid, writ=w["writ"], tier=w["tier"], rep=rep, wp=wp, iid=iid, item=w["item"], q=q,
                        known=w["known"] == "1", ic=ic * q, tot=tot, per=tot / rep))
    out.sort(key=lambda x: x["per"])
    return out


def crates(m):
    try:
        data = json.load(open(os.path.join(HERE, "dsl_crates.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for cid, c in data.items():
        cid = int(cid)
        name = c["name"]
        tier = next((t for t in CRATE_LEVEL if t in name), None)
        kind = name.split()[-1]
        fav = CRATE_FAVOR.get(tier, (None, None))[1 if kind in BIG else 0]
        price = m.ah(cid)[0] or m.gear_now(cid)[0]
        best = None
        for q, n in c["bundles"]:
            if (n, q) in CANT_FILL:
                continue
            ic = item_cost(m, m.name_id.get(n.lower()))
            if ic is not None and (best is None or ic * q < best[0]):
                best = (ic * q, f"{q:g} {n}")
        if not price or not best:
            continue
        tot = price + best[0]
        out.append(dict(cid=cid, name=name, tier=tier, lvl=CRATE_LEVEL.get(tier), price=price, bundle=best[1],
                        bc=best[0], tot=tot, fav=fav, per=(tot / fav) if fav else None))
    out.sort(key=lambda x: (x["per"] is None, x["per"] or 0))
    return out


def rep_cost(ws, need):
    """Gold to earn `need` rep using the cheapest writs, each different writ once (a day) - cheapest first."""
    got = gold = 0
    for w in ws:
        if got >= need:
            break
        got += w["rep"]
        gold += w["tot"]
    if got < need and got:   # more than one day of writs: repeat at the same average
        gold *= need / got
    return gold


def sec_dsl(m, sm):
    ws, cs = writs(m), crates(m)
    if not ws or not cs:
        return ""
    fav_c = next((c for c in cs if c["per"]), None)
    empty = min((c for c in cs), key=lambda c: c["price"])
    label = min((c for c in cs if c["tier"] in ("Apprentice", "Journeyman")), key=lambda c: c["tot"], default=None)
    rec_per = (empty["price"] + RECEIPT_FAVOR * fav_c["per"]) / RECEIPT_REP if fav_c else None
    esc = R.esc

    def wrow(w):
        need = f'{w["q"]:g} {esc(w["item"])}' + ('' if w["known"] else ' <span class="mut" title="amount not published - assumed 1">(1?)</span>')
        return (f'<tr><td class="nm" data-iid="{w["wid"]}">{esc(w["writ"].replace("Craftsman's Writ: ", ""))}</td>'
                f'<td>{w["tier"]}</td><td class="r">+{w["rep"]}</td><td class="r">{sm(w["wp"])}</td>'
                f'<td class="mats">{need}</td><td class="r">{sm(w["ic"])}</td><td class="r"><b>{sm(w["tot"])}</b></td>'
                f'<td class="r"><b class="up">{sm(w["per"] * 100)}</b></td></tr>')

    def crow(c):
        return (f'<tr><td class="nm" data-iid="{c["cid"]}">{esc(c["name"].replace("Waylaid Crate: ", ""))}</td>'
                f'<td class="r">{c["lvl"]}</td><td class="r">{sm(c["price"])}</td><td class="mats">{esc(c["bundle"])}</td>'
                f'<td class="r">{sm(c["bc"])}</td><td class="r"><b>{sm(c["tot"])}</b></td>'
                f'<td class="r">{c["fav"] if c["fav"] else "?"}</td>'
                f'<td class="r">{("<b class=up>" + sm(c["per"]) + "</b>") if c["per"] else "-"}</td></tr>')

    how = ('<div class="mut cgn" style="line-height:1.6">'
           '<b>How it works:</b> the DSL camp sits just outside the west gate of the Crossroads (The Barrens). Its vendors sell '
           'recipes for <b>Merchant&rsquo;s Favor</b> (a currency), but the good ones also need <b>reputation</b> '
           '(Friendly = 3,000, Honored = 9,000).<br>'
           '<b>1. Reputation = Craftsman&rsquo;s Writs.</b> Buy a writ on the AH plus the item it asks for, right-click the writ '
           'to accept it, and hand both to the customer. Journeyman +75, Expert +125, Artisan +200. Each different writ counts '
           'once per day, so do many different ones. Writs give no Favor.<br>'
           '<b>2. Favor = Waylaid Crates.</b> Buy an empty crate on the AH, right-click it, pick ONE bundle (e.g. 20 Copper Ore) '
           'and it seals with those mats inside. Hand the sealed crate to <b>Dokimi</b> for 5-30 Favor. Crates give no rep.<br>'
           '<b>3. Your first crate ever</b> starts the Shipping Label quest: <b>+2,000 rep and +50 Favor</b> - always do this first.<br>'
           '<b>4. At Friendly</b>, Kurvelk Shadowhoof sells a Forged Delivery Receipt (15 Favor): put it on an empty crate for +50 rep.</div>')

    step1 = ""
    if label:
        step1 = (f'<h3 class="cg">Step 1 - your first crate (+2,000 rep, +50 Favor)</h3><div class="mut cgn">Cheapest right now: buy '
                 f'<b>{esc(label["name"])}</b> ({sm(label["price"])}) and fill it with <b>{esc(label["bundle"])}</b> '
                 f'({sm(label["bc"])}) = <b class="up">{sm(label["tot"])}</b> total, level {label["lvl"]}+. That alone takes you '
                 f'two thirds of the way to Friendly.</div>')

    wt = ('<h3 class="cg">Step 2 - reputation: cheapest writs right now</h3>'
          '<div class="mut cgn">Writ + the item it asks for (cheapest of buying it or crafting it), sorted by gold per 100 rep. '
          'Each different writ once per day. (1?) = the amount isn&rsquo;t published, assumed 1 - check the writ before buying.'
          + (f' After Friendly the Forged Delivery Receipt route costs about <b>{sm(rec_per * 100)}</b> per 100 rep '
             f'(cheapest empty crate {sm(empty["price"])} + 15 Favor).' if rec_per else '') + '</div>'
          '<table class="g"><thead><tr><th>Writ</th><th>Tier</th><th class="r">Rep</th><th class="r">Writ</th><th>Needs</th>'
          '<th class="r">Item cost</th><th class="r">Total</th><th class="r">Per 100 rep</th></tr></thead><tbody>'
          + "".join(wrow(w) for w in ws[:20]) + '</tbody></table>')
    sealed = []
    for n, rep in (("Sealed Journeyman's Writ", 75), ("Sealed Expert's Writ", 125), ("Sealed Artisan's Writ", 200)):
        i = m.name_id.get(n.lower())
        p = m.ah(i)[0] if i else None
        if p:
            sealed.append(f'{esc(n)} {sm(p)} (+{rep}, random item still to buy)')
    if sealed:
        wt += '<div class="mut cgn">Random sealed writs: ' + " &middot; ".join(sealed) + ' - the named writs above are safer since you know the item.</div>'

    ct = ('<h3 class="cg">Step 3 - Merchant&rsquo;s Favor: cheapest crates right now</h3>'
          '<div class="mut cgn">Empty crate + its cheapest bundle, sorted by gold per Favor. Artisan crate Favor is not known yet.</div>'
          '<table class="g"><thead><tr><th>Crate</th><th class="r">Level</th><th class="r">Crate</th><th>Cheapest bundle</th>'
          '<th class="r">Bundle cost</th><th class="r">Total</th><th class="r">Favor</th><th class="r">Per Favor</th></tr></thead><tbody>'
          + "".join(crow(c) for c in cs) + '</tbody></table>')

    # per-profession plan
    vend = _rows("dsl_vendor.csv")
    byp = defaultdict(list)
    for v in vend:
        byp[v["prof"]].append(v)
    plan_rows, lists = "", ""
    for prof in sorted(byp):
        lst = byp[prof]
        top = max(lst, key=lambda v: STAND[v["standing"]])["standing"]
        rep_need = max(0, STAND[top] - LABEL_REP)
        fav = sum(int(v["favor"]) for v in lst) - LABEL_FAVOR
        to_fr = min(rep_need, STAND["Friendly"] - LABEL_REP)   # writs up to Friendly
        after = rep_need - to_fr
        g_rep = rep_cost(ws, to_fr)
        if after:   # from Friendly: receipts or writs, whichever is cheaper per rep right now
            writ_avg = rep_cost(ws, after) / after
            g_rep += after * min(writ_avg, rec_per or writ_avg)
        g_fav = max(0, fav) * fav_c["per"] if fav_c else 0
        cnt = {s: sum(v["standing"] == s for v in lst) for s in STAND}
        plan_rows += (f'<tr><td class="nm">{esc(prof)}</td><td class="r">{len(lst)}</td>'
                      f'<td class="r">{cnt["Neutral"]} / {cnt["Friendly"]} / {cnt["Honored"]}</td><td>{top}</td>'
                      f'<td class="r">{rep_need:,}</td><td class="r">{sm(g_rep) if rep_need else "-"}</td>'
                      f'<td class="r">{max(0, fav):,}</td><td class="r">{sm(g_fav)}</td>'
                      f'<td class="r"><span class="badge kitp">{sm(g_rep + g_fav + (label["tot"] if label else 0))}</span></td></tr>')
        body = ""
        for st in STAND:
            grp = [v for v in lst if v["standing"] == st]
            if not grp:
                continue
            body += f'<tr class="grp"><td colspan="4"><b>{st}</b> <span class="mut">{len(grp)}</span></td></tr>'
            for v in sorted(grp, key=lambda v: (int(v["favor"]), v["recipe"])):
                oid = m.name_id.get(v["recipe"].lower())
                now = (m.ah(oid)[0] or m.gear_now(oid)[0]) if oid else None
                nm = f'<td class="nm" data-iid="{oid}">' if oid and (oid in m.rows or oid in m.gear) else '<td class="nm">'
                body += (f'<tr>{nm}{esc(v["recipe"])}</td><td class="r">{v["favor"]} Favor</td><td>{st}</td>'
                         f'<td class="r">{sm(now) if now else "-"}</td></tr>')
        lists += (f'<h3 class="cg">{esc(prof)} recipes at the DSL camp <span class="mut cgn">{len(lst)}</span></h3>'
                  '<table class="g"><thead><tr><th>Makes</th><th class="r">Cost</th><th>Standing</th><th class="r">Item sells for</th>'
                  f'</tr></thead><tbody>{body}</tbody></table>')
    plan = ('<h3 class="cg">Plan per profession - to buy every DSL recipe</h3>'
            '<div class="mut cgn">Rep and Favor still needed after your first crate. Rep: writs up to Friendly, then the cheaper of writs or '
            'Forged Delivery Receipts' + (f' (receipts right now: {sm(rec_per * 100)} per 100 rep)' if rec_per else '') +
            '. Favor: the cheapest crate above. Total = first crate + rep + Favor. Rough guide - prices move every hour.</div>'
            '<table class="g"><thead><tr><th>Profession</th><th class="r">Recipes</th><th class="r">Neutral / Friendly / Honored</th>'
            '<th>Needs</th><th class="r">Rep to earn</th><th class="r">Rep gold</th><th class="r">Favor to earn</th>'
            '<th class="r">Crates gold</th><th class="r">Total</th></tr></thead><tbody>' + plan_rows + '</tbody></table>')
    return ('<section><div class="sh"><h2>Durotar Supply and Logistics</h2><span class="tag">cheapest way to the rep and '
            'Merchant&rsquo;s Favor for the DSL camp recipes, priced from the Horde PvP ruleset AH now</span></div>'
            + how + step1 + wt + ct + plan + lists +
            '<div class="mut cgn" style="margin-top:8px">Sources: wowcalc.net, ConquestCapped, wow-professions.com (Nov 2026); '
            'item data from Blizzard. Recipe costs and standings from wow-professions.</div></section>')
