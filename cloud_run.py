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
    late = oldest and oldest < time.time() - (KEEP_DAYS + 2) * 86400   # missed a day or two - catch up now
    if time.localtime().tm_hour != 3 and not late:
        return
    try:
        import archive
        print(archive.archive(cfg, db, keep_days=KEEP_DAYS))
    except Exception as e:
        print(f"archive failed, nothing deleted: {e}")


def main():
    cfg = F.load_config()
    if (str(cfg.get("AUCTION_HOUSE")) != "113" or str(cfg.get("RULESET")) != "1"
            or cfg.get("NAMESPACE") != "dynamic-forever-us"):
        sys.exit("cloud scan is for WoW Forever US PvP Horde AH 113 only")
    api = F.Api(cfg)
    F.check_market(cfg, api)
    if F.poll_once(cfg, "113", api):
        subprocess.run([sys.executable, "-u", os.path.join(HERE, "hourly.py")], timeout=1500)
        prune(cfg)


if __name__ == "__main__":
    main()
