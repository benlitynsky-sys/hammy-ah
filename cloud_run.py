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

KEEP_DAYS = 14   # the report needs 7 days; keep two weeks so the saved data stays small


def prune(cfg):
    con = F.db_connect(cfg, "113")
    cut = int(time.time()) - KEEP_DAYS * 86400
    for t in ("snap", "allsnap", "gearsnap"):
        try:
            con.execute(f"DELETE FROM {t} WHERE ts < ?", (cut,))
        except Exception:
            pass
    con.commit()
    if time.localtime().tm_hour == 3:   # once a day, shrink the file
        con.execute("VACUUM")
    con.close()


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
