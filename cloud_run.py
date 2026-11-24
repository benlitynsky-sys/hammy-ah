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


def main():
    cfg = F.load_config()
    if (str(cfg.get("AUCTION_HOUSE")) != "113" or str(cfg.get("RULESET")) != "1"
            or cfg.get("NAMESPACE") != "dynamic-forever-us"):
        sys.exit("cloud scan is for WoW Forever US PvP Horde AH 113 only")
    api = F.Api(cfg)
    F.check_market(cfg, api)
    if F.poll_once(cfg, "113", api):
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
