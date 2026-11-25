#!/usr/bin/env python3
"""alerts.py - after each Horde AH 113 scan, check holdings.json and say when to sell.
Prices in copper. Writes alerts.log (UTF-8) and pops a Windows notification."""
import json
import os
import sqlite3
import subprocess
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HOLD = os.path.join(HERE, "holdings.json")
LOG = os.path.join(HERE, "alerts.log")
PING = os.path.join(HERE, "ping.json")   # {"discord_user_id": "..."} - who sell alerts @mention


def money(c):
    c = int(round(c))
    g, s = divmod(c // 100, 100)
    return (f"{g}g {s}s" if g else f"{s}s {c % 100}c")


def latest(con, iid):
    best = None
    for q in ("SELECT ts, floor, qty FROM snap WHERE item_id=? ORDER BY ts DESC LIMIT 1",
              "SELECT ts, floor, qty FROM allsnap WHERE item_id=? ORDER BY ts DESC LIMIT 1",
              "SELECT ts, min_buyout, auctions FROM gearsnap WHERE item_id=? ORDER BY ts DESC LIMIT 1"):
        try:
            r = con.execute(q, (iid,)).fetchone()
        except sqlite3.OperationalError:
            r = None
        if r and (best is None or r[0] > best[0]):
            best = r
    return best


def toast(msg):
    if os.environ.get("CLOUD"):   # running on GitHub, not this PC: send the alert to Discord instead of a popup
        try:
            import requests
            url = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8")).get("DISCORD_WEBHOOK_URL")
            if url:
                uid = str(json.load(open(PING, encoding="utf-8")).get("discord_user_id", "")) if os.path.exists(PING) else ""
                body = {"content": (f"<@{uid}> " if uid.isdigit() else "") + ":bell: " + msg,
                        "allowed_mentions": {"users": [uid] if uid.isdigit() else []}}   # pings Hammy only, never @everyone
                requests.post(url, json=body, timeout=30)
        except Exception:
            print("Discord alert failed")   # never print the webhook URL
        return
    ps = ("Add-Type -AssemblyName System.Windows.Forms;$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
          f"$n.ShowBalloonTip(20000,'AH alert',\"{msg.replace(chr(34), '')}\",'Info');Start-Sleep 25;$n.Dispose()")
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def usual(con, iid, days=7):
    """Median cheapest price over the last 7 days - the same 'usual' the web page shows."""
    import statistics
    import time
    cut = int(time.time()) - days * 86400
    for t in ("snap", "allsnap"):
        try:
            p = [r[0] for r in con.execute(f"SELECT floor FROM {t} WHERE item_id=? AND ts>=? AND floor>0", (iid, cut))]
        except sqlite3.OperationalError:
            p = []
        if len(p) >= 12:
            return statistics.median(p)
    return None


def check():
    if not os.path.exists(HOLD):
        return
    hold = json.load(open(HOLD, encoding="utf-8"))
    con = sqlite3.connect(os.path.join(HERE, "ah_113.sqlite"))   # Horde AH on US PvP only
    changed = False
    for h in hold:
        r = latest(con, h["item_id"])
        if not r:
            continue
        ts, price, qty = r
        if h.get("target") == "usual":            # follow the page's weekly usual price (7-day median)
            u = usual(con, h["item_id"])
            if not u:
                continue
            h = dict(h, target=u, great=h.get("great") or u * 1.1, stop=h.get("stop") or 0, _usual=True)
        net = price * 0.95                       # after the 5% AH cut
        profit = (net - h["paid"]) * h["qty"]
        level = ("great" if price >= h["great"] else "target" if price >= h["target"] else None)   # no stop-loss alerts (Hammy's rule)
        if level and level not in h["alerted"]:
            word = {"great": "SELL NOW - big price", "target": "SELL - target hit",
                    "stop": "DROPPING - below your stop"}[level]
            if h.get("_usual") and level != "stop":
                word += f" (above its usual {money(h['target'])})"
            msg = (f"{word}: {h['name']} cheapest is {money(price)} ({qty} listed). "
                   f"Your {h['qty']} at {money(h['paid'])}: list just under {money(price)} "
                   f"for about {'+' if profit >= 0 else '-'}{money(abs(profit))} total.")
            line = f"{datetime.fromtimestamp(ts):%a %I:%M %p} ALERT {msg}"
            open(LOG, "a", encoding="utf-8").write(line + "\n")
            print(line, flush=True)
            toast(msg)
            h["alerted"].append(level)
            changed = True
        if level is None and h["alerted"] and price < h["target"] * 0.95:
            h["alerted"].clear()                 # dropped back under: alert again next time it climbs above
            changed = True
    if changed:
        json.dump(hold, open(HOLD, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    check()
