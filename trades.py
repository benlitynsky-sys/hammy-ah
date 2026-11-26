#!/usr/bin/env python3
"""trades.py - Hammy's trade log (trades.json, kept in the cloud state next to holdings.json).
  python trades.py buy  <item id> <qty> <price each, copper> ["when"] [--target C] [--great C]
  python trades.py sell <item id> <qty> <price each, copper> ["when"]
A buy adds to holdings.json (sell alerts); a sell takes it off. "when" is Hammy's clock, e.g. "2026-11-25 8:35 PM"."""
import json
import os
import sys
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(HERE, "trades.json")
HOLD = os.path.join(HERE, "holdings.json")
CUT = 0.05


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def when_ts(s):
    """'2026-11-25 8:35 PM' on Hammy's clock (UTC-7) -> unix time."""
    t = datetime.strptime(s, "%Y-%m-%d %I:%M %p") + timedelta(hours=7)
    return int((t - datetime(1970, 1, 1)).total_seconds())


def name_of(iid):
    import sqlite3
    try:
        r = sqlite3.connect(os.path.join(HERE, "ah_113.sqlite")).execute("SELECT name FROM iteminfo WHERE item_id=?", (iid,)).fetchone()
        return r[0] if r else str(iid)
    except Exception:
        return str(iid)


def main(a):
    flags = {a[i]: int(a[i + 1]) for i in range(len(a) - 1) if a[i].startswith("--")}
    a = [x for i, x in enumerate(a) if not x.startswith("--") and not (i and a[i - 1].startswith("--"))]
    kind, iid, qty, price = a[0], int(a[1]), int(a[2]), int(a[3])
    ts = when_ts(a[4]) if len(a) > 4 else int(time.time())
    name = name_of(iid)
    trades, hold = load(TRADES), load(HOLD)
    t = {"ts": ts, "kind": kind, "item_id": iid, "name": name, "qty": qty, "price": price}
    if kind == "sell":   # profit against what was paid (latest buy of that item)
        paid = next((h["paid"] for h in hold if h["item_id"] == iid), None) or \
            next((x["price"] for x in sorted(trades, key=lambda x: -x["ts"]) if x["item_id"] == iid and x["kind"] == "buy"), None)
        if paid:
            t["paid"] = paid
            t["profit"] = int(round((price * (1 - CUT) - paid) * qty))
        for h in hold:
            if h["item_id"] == iid:
                h["qty"] -= qty
        hold = [h for h in hold if h["qty"] > 0]
    elif kind == "buy":
        h = {"item_id": iid, "name": name, "qty": qty, "paid": price, "target": flags.get("--target", "usual"),
             "alerted": [], "bought_ts": ts}
        if "--great" in flags:
            h["great"] = flags["--great"]
        hold = [x for x in hold if x["item_id"] != iid] + [h]
    else:
        sys.exit(__doc__)
    trades.append(t)
    json.dump(trades, open(TRADES, "w", encoding="utf-8"), indent=1)
    json.dump(hold, open(HOLD, "w", encoding="utf-8"), indent=1)
    print(kind, qty, name, "@", price, "logged;", len(hold), "holdings,", len(trades), "trades")


if __name__ == "__main__":
    main(sys.argv[1:])
