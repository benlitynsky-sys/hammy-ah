"""archive.py - keep every hour of AH 113 price history forever.

The working file (ah_113.sqlite) only keeps the last 13 days so the hourly cloud run stays quick. Once a day,
before anything is trimmed, every row older than that is copied into a monthly archive file on GitHub:
    release "archive"  ->  ah113-2026-11.sqlite.gz, ah113-2026-12.sqlite.gz, ...
Rows are only deleted from the working file after their month's archive uploaded and was checked.

Each archive file is a normal SQLite database (gunzip it first) with the same tables as the working file:
    allsnap  - every stackable good:  ts, item_id, floor, min_unit, qty, auctions, sold_est   (prices in copper)
    gearsnap - every gear/one-off item: ts, item_id, min_buyout, median_buyout, auctions
    snap     - the watch-list goods (same columns as allsnap)
ts = unix time of the scan. Open with "DB Browser for SQLite", or:  python history.py "Black Lotus"
WoW Forever / US / PvP / Horde AH 113 only."""
import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TABLES = ("snap", "allsnap", "gearsnap", "sales")
TAG = "archive"
NOTES = ("Permanent hourly price history for WoW Forever US PvP, Horde AH 113. One file per month (local time, UTC-7). "
         "Each .sqlite.gz is a gzipped SQLite database: allsnap = every stackable good (ts, item_id, floor, min_unit, qty, "
         "auctions, sold_est), gearsnap = every gear/one-off item (ts, item_id, min_buyout, median_buyout, auctions), "
         "snap = watch-list goods. Prices are in copper, ts is unix time.")


def _gh(cfg):
    return cfg["GITHUB_REPO"], {"Authorization": f"Bearer {cfg['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"}


def _release(cfg, tag):
    repo, h = _gh(cfg)
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=h, timeout=60)
    if r.status_code == 404:
        r = requests.post(f"https://api.github.com/repos/{repo}/releases", headers=h, timeout=60,
                          json={"tag_name": tag, "name": "AH 113 price archive", "body": NOTES})
    r.raise_for_status()
    return r.json()


def _download(cfg, asset, path):
    _, h = _gh(cfg)
    with requests.get(asset["url"], headers={**h, "Accept": "application/octet-stream"}, timeout=600, stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            shutil.copyfileobj(r.raw, f)


def _upload(cfg, rel, name, path):
    repo, h = _gh(cfg)
    for a in rel.get("assets", []):
        if a["name"] == name:
            requests.delete(f"https://api.github.com/repos/{repo}/releases/assets/{a['id']}", headers=h, timeout=60).raise_for_status()
    with open(path, "rb") as f:
        r = requests.post(f"https://uploads.github.com/repos/{repo}/releases/{rel['id']}/assets?name={name}",
                          headers={**h, "Content-Type": "application/gzip"}, data=f, timeout=1200)
    r.raise_for_status()


def archive(cfg, db_path, keep_days=13, tag=TAG, delete=True, delete_days=15):
    """Move rows older than keep_days into the monthly archive files. Returns a short status line."""
    cut = int(time.time()) - keep_days * 86400
    con = sqlite3.connect(db_path)
    tables = [t for t in TABLES if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    schema = {t: con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()[0] for t in tables}
    months = {}
    for t in tables:
        for (ts,) in con.execute(f"SELECT DISTINCT ts FROM {t} WHERE ts < ?", (cut,)):
            months.setdefault(time.strftime("%Y-%m", time.localtime(ts)), set()).add(ts)
    if not months:
        con.close()
        return "archive: nothing older than %d days" % keep_days
    rel = _release(cfg, tag)
    done = []
    tmp = tempfile.mkdtemp()
    try:
        for month, stamps in sorted(months.items()):
            name = f"ah113-{month}.sqlite.gz"
            local = os.path.join(tmp, f"ah113-{month}.sqlite")
            asset = next((a for a in rel.get("assets", []) if a["name"] == name), None)
            if asset:   # add to what's already archived for that month
                _download(cfg, asset, local + ".gz")
                with gzip.open(local + ".gz", "rb") as src, open(local, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            arc = sqlite3.connect(local)
            lo, hi = min(stamps), max(stamps)
            want = {}
            for t in tables:
                arc.execute(schema[t].replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1))
                rows = con.execute(f"SELECT * FROM {t} WHERE ts BETWEEN ? AND ? AND ts < ?", (lo, hi, cut)).fetchall()
                rows = [r for r in rows if r[0] in stamps]
                if rows:
                    ph = ",".join("?" * len(rows[0]))
                    arc.executemany(f"INSERT OR REPLACE INTO {t} VALUES ({ph})", rows)
                want[t] = len(rows)
            arc.commit()
            # check every row made it before anything is deleted
            for t, n in want.items():
                got = arc.execute(f"SELECT COUNT(*) FROM {t} WHERE ts BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
                if got < n:
                    raise RuntimeError(f"archive check failed for {t} {month}")
            arc.close()
            with open(local, "rb") as src, gzip.open(local + ".gz", "wb", compresslevel=9) as dst:
                shutil.copyfileobj(src, dst)
            _upload(cfg, rel, name, local + ".gz")
            rel = _release(cfg, tag)   # refresh the asset list
            if not any(a["name"] == name and a["size"] > 0 for a in rel.get("assets", [])):
                raise RuntimeError(f"archive upload of {name} not found")
            done.append((month, stamps))
        if delete:   # archived at 13 days; the working file keeps them until 15 days so the 14-day view has them
            gone = int(time.time()) - delete_days * 86400
            for _, stamps in done:
                for t in tables:
                    con.executemany(f"DELETE FROM {t} WHERE ts = ?", [(s,) for s in stamps if s < gone])
            con.commit()
            con.execute("VACUUM")
        return "archive: saved " + ", ".join(f"{m} ({len(s)} scans)" for m, s in done)
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def fetch_all(cfg, dest=None, tag=TAG):
    """Download every monthly archive file (unzipped) into archive/, for the full-history charts. Returns the paths."""
    dest = dest or os.path.join(HERE, "archive")
    os.makedirs(dest, exist_ok=True)
    repo, h = _gh(cfg)
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=h, timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out = []
    for a in r.json().get("assets", []):
        if not a["name"].endswith(".sqlite.gz"):
            continue
        gz = os.path.join(dest, a["name"])
        db = gz[:-3]
        if not (os.path.exists(gz) and os.path.getsize(gz) == a["size"] and os.path.exists(db)):
            _download(cfg, a, gz)
            with gzip.open(gz, "rb") as s, open(db, "wb") as d:
                shutil.copyfileobj(s, d)
        out.append(db)
    return out


if __name__ == "__main__":
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    print(archive(cfg, os.path.join(HERE, "ah_113.sqlite")))
