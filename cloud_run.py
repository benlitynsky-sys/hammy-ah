"""cloud_run.py - one GitHub Actions pass (no PC needed): scan Horde AH 113 once; if Blizzard published a new
snapshot, build the report, update the web page and edit the Discord post (hourly.py), then trim old rows.
WoW Forever / US / PvP ruleset / Horde AH 113 only - it refuses to run with any other setting."""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import forever_ah as F

KEEP_DAYS = 13   # the working file keeps 13 days; anything older is moved to the permanent monthly archive first


def prune(cfg):
    """Once a day (3am), move rows older than KEEP_DAYS into the permanent archive (release "archive"), and only
    then delete them here. If the archive upload fails nothing is deleted - it just tries again the next day."""
    con = F.db_connect(cfg, "113")
    oldest = con.execute("SELECT MIN(ts) FROM allsnap").fetchone()[0]
    db = con.execute("PRAGMA database_list").fetchone()[2]
    con.close()
    late = oldest and oldest < time.time() - (KEEP_DAYS + 4) * 86400   # missed a day or two - catch up now
    if time.localtime().tm_hour != 3 and not late:
        return
    try:
        import archive
        print(archive.archive(cfg, db, keep_days=KEEP_DAYS))
    except Exception as e:
        print(f"archive failed, nothing deleted: {e}")


def name_gear(cfg, api, budget=300):
    """Look up the name of every gear/one-off item seen on AH 113 that we don't have a name for yet, so the
    search tile can find it. Cached in iteminfo, so each item is only ever looked up once."""
    from concurrent.futures import ThreadPoolExecutor
    import urllib.error
    con = F.db_connect(cfg, "113")
    todo = [i for (i,) in con.execute("SELECT DISTINCT item_id FROM gearsnap WHERE item_id NOT IN "
                                      "(SELECT item_id FROM iteminfo WHERE name NOT LIKE 'item %')")]
    if not todo:
        con.close()
        return
    api._token()
    stop = time.time() + budget

    def one(iid):
        if time.time() > stop:
            return None
        it = {}
        # Forever's own item list first; older items missing there come from Blizzard's Classic Era list (names only -
        # prices still only ever come from AH 113)
        for ns in ("static-forever-us", "static-classic1x-us"):
            try:
                it, _ = api.get(f"/data/wow/item/{iid}", ns)
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    return None   # rate limit / outage: try again next hour
            except Exception:
                return None
        s = lambda v: v.get("name", "") if isinstance(v, dict) else (v or "")
        sell = it.get("sell_price")
        sell = sell if isinstance(sell, int) else (sell or {}).get("value", 0) if isinstance(sell, dict) else 0
        return (iid, s(it.get("name")) or f"item {iid}", s(it.get("item_class")), s(it.get("item_subclass")),
                it.get("required_level") or 0, sell or 0)

    with ThreadPoolExecutor(30) as ex:
        rows = [r for r in ex.map(one, todo) if r]
    con.executemany("INSERT OR REPLACE INTO iteminfo VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    print(f"gear names: {len(rows)} looked up, {len(todo) - len(rows)} left for next time")


PUBLISH_MIN = 26     # Blizzard posts each new AH 113 snapshot at about :26 past the hour
WAIT_UNTIL_MIN = 45  # a run that starts before this keeps watching for the snapshot instead of quitting
RUN_BUDGET = 18 * 60  # GitHub stops a run at 30 min - leave room for the report build


def wait_for_snapshot(cfg, api, started):
    """Watch for Blizzard's next snapshot with a cheap 'changed since?' check every 15 s (no download). Returns True
    when new data is out; False when it's past :WAIT_UNTIL_MIN or this run is out of time."""
    import urllib.error
    con = F.db_connect(cfg, "113")
    lm = F.meta_get(con, "last_modified")
    con.close()
    path = f"/data/wow/ruleset/{cfg['RULESET']}/auctions/113/commodities"
    while time.time() - started < RUN_BUDGET and time.localtime().tm_min < WAIT_UNTIL_MIN:
        try:
            api.get(path, cfg["NAMESPACE"], {"If-Modified-Since": lm} if lm else None)
            return True                      # 200 = something new
        except urllib.error.HTTPError as e:
            if e.code != 304:
                print(f"watch: Blizzard said {e.code}")
        except Exception as e:
            print(f"watch: {e}")
        time.sleep(15)
    return False


def start_next_run(cfg):
    """Hand the watch to a fresh run (GitHub caps a run at 30 min); it starts the moment this one saves."""
    import requests
    try:
        requests.post(f"https://api.github.com/repos/{cfg['GITHUB_REPO']}/actions/workflows/scan.yml/dispatches",
                      headers={"Authorization": f"Bearer {cfg['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json"},
                      json={"ref": "main"}, timeout=30).raise_for_status()
        print("watch: handed off to a new run")
    except Exception as e:
        print(f"watch: could not start the next run ({e})")


def main():
    cfg = F.load_config()
    if (str(cfg.get("AUCTION_HOUSE")) != "113" or str(cfg.get("RULESET")) != "1"
            or cfg.get("NAMESPACE") != "dynamic-forever-us"):
        sys.exit("cloud scan is for WoW Forever US PvP Horde AH 113 only")
    api = F.Api(cfg)
    started = time.time()
    F.check_market(cfg, api)
    new = F.poll_once(cfg, "113", api)
    if not new and time.localtime().tm_min < WAIT_UNTIL_MIN:
        # GitHub often starts scheduled runs late, so rather than miss the :26 snapshot, wait for it right here
        print(f"watch: waiting for the :{PUBLISH_MIN} snapshot")
        if wait_for_snapshot(cfg, api, started):
            new = F.poll_once(cfg, "113", api)
        elif time.localtime().tm_min < WAIT_UNTIL_MIN:
            start_next_run(cfg)
    if new:
        try:   # older history for the item charts (they show everything since tracking began)
            import archive
            archive.fetch_all(cfg)
        except Exception as e:
            print(f"archive download skipped: {e}")
        try:
            name_gear(cfg, api)
        except Exception as e:
            print(f"gear names skipped: {e}")
        subprocess.run([sys.executable, "-u", os.path.join(HERE, "hourly.py")], timeout=1500)
        prune(cfg)


if __name__ == "__main__":
    main()
