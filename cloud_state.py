"""cloud_state.py - pack / unpack the tracker's saved data (price history, Discord post id, holdings) into
state.zip, which the GitHub workflow keeps as a release file between runs.
  python cloud_state.py save     python cloud_state.py load"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["ah_113.sqlite", "prev_all_113.json", "discord_state.json", "holdings.json", "alerts.log",
         "stack_obs.json", "stackable_cache.json", "binding_cache.json"]
ZIP = os.path.join(HERE, "state.zip")

if __name__ == "__main__":
    if sys.argv[1:] == ["save"]:
        db = os.path.join(HERE, "ah_113.sqlite")
        if not os.path.exists(db) or os.path.getsize(db) < 1_000_000:   # never overwrite the history with an empty file
            sys.exit("ah_113.sqlite missing or too small - not saving")
        with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
            for f in FILES:
                if os.path.exists(os.path.join(HERE, f)):
                    z.write(os.path.join(HERE, f), f)
        print(f"saved {os.path.getsize(ZIP) / 1e6:.1f} MB")
    elif sys.argv[1:] == ["load"]:
        with zipfile.ZipFile(ZIP) as z:
            z.extractall(HERE)
        print("loaded", ", ".join(zipfile.ZipFile(ZIP).namelist()))
    else:
        sys.exit(__doc__)
