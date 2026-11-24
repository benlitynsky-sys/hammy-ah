"""history.py - read the full price history of any AH 113 item: the permanent monthly archive on GitHub plus the
current 13 days. Prints one line per day (low / middle / high price and average listed).
  python history.py "Black Lotus"            python history.py "Black Lotus" --csv black_lotus.csv
Archive files are downloaded once into archive/ and refreshed when GitHub has a newer copy."""
import csv
import gzip
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
import zipfile
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ADIR = os.path.join(HERE, "archive")


def sources(cfg):
    """Local copies of every monthly archive file, plus the newest working file from the cloud."""
    import archive
    os.makedirs(ADIR, exist_ok=True)
    out = []
    try:
        rel = archive._release(cfg, archive.TAG)
        for a in rel.get("assets", []):
            gz = os.path.join(ADIR, a["name"])
            db = gz[:-3]
            if not os.path.exists(db) or os.path.getsize(gz if os.path.exists(gz) else db) != a["size"]:
                archive._download(cfg, a, gz)
                with gzip.open(gz, "rb") as s, open(db, "wb") as d:
                    shutil.copyfileobj(s, d)
            out.append(db)
    except requests.RequestException as e:
        print(f"(archive not reachable: {e})")
    cur = os.path.join(ADIR, "current.sqlite")
    try:   # the cloud's working file (last 13 days)
        r = requests.get(f"https://github.com/{cfg['GITHUB_REPO']}/releases/download/data/state.zip", timeout=300)
        r.raise_for_status()
        zp = os.path.join(ADIR, "state.zip")
        open(zp, "wb").write(r.content)
        with zipfile.ZipFile(zp) as z, z.open("ah_113.sqlite") as s, open(cur, "wb") as d:
            shutil.copyfileobj(s, d)
    except Exception as e:
        print(f"(current data not reachable: {e})")
    if os.path.exists(cur):
        out.append(cur)
    return out


def item_ids(name):
    import report as R
    m = R.Market()
    ids = [i for i in set(m.rows) | set(m.items) if m.name(i).lower() == name.lower()]
    return ids, m


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    name = sys.argv[1]
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    ids, m = item_ids(name)
    if not ids:
        sys.exit(f"no item called {name!r} found")
    rows = {}
    for db in sources(cfg):
        con = sqlite3.connect(db)
        for t, cols in (("allsnap", "ts, floor, qty"), ("gearsnap", "ts, min_buyout, auctions"), ("snap", "ts, floor, qty")):
            try:
                for ts, p, q in con.execute(f"SELECT {cols} FROM {t} WHERE item_id IN ({','.join('?' * len(ids))})", ids):
                    if p:
                        rows.setdefault(ts, (p, q))
            except sqlite3.Error:
                pass
        con.close()
    days = defaultdict(list)
    for ts, (p, q) in sorted(rows.items()):
        days[time.strftime("%Y-%m-%d", time.localtime(ts))].append((p, q))
    def money(c):
        c = int(c); return f"{c // 10000}g {c % 10000 // 100:02d}s" if c >= 10000 else f"{c // 100}s {c % 100:02d}c"
    out = []
    print(f"{m.name(ids[0])} - {len(rows)} hourly scans, {len(days)} days")
    print(f"{'day':<12}{'low':>12}{'middle':>12}{'high':>12}{'avg listed':>12}")
    for d, v in days.items():
        ps = [p for p, _ in v]
        line = (d, min(ps), statistics.median(ps), max(ps), round(sum(q for _, q in v) / len(v)))
        out.append(line)
        print(f"{d:<12}{money(line[1]):>12}{money(line[2]):>12}{money(line[3]):>12}{line[4]:>12,}")
    if "--csv" in sys.argv:
        f = sys.argv[sys.argv.index("--csv") + 1]
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "low_copper", "middle_copper", "high_copper", "avg_listed"])
            w.writerows(out)
        print("saved", f)
