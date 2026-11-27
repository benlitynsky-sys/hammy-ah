#!/usr/bin/env python3
"""discord_post.py - post Hammy's report (as pictures that show inside Discord) to a Discord channel through a webhook, then on every later
run EDIT that same message and swap in the new files (one post that keeps updating, no spam).

Setup: Discord channel > Edit Channel > Integrations > Webhooks > New Webhook > Copy Webhook URL,
then add  "DISCORD_WEBHOOK_URL": "<that url>"  to config.json yourself. Nothing posts until it's set.
Run by hand:  python discord_post.py        Start a fresh post:  python discord_post.py --new"""
import json
import os
import sys
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "discord_state.json")


def webhook():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    return (cfg.get("DISCORD_WEBHOOK_URL") or "").strip()


def summary(market_html):
    """One-line flip highlights pulled from the built report."""
    import html
    import re
    t = open(market_html, encoding="utf-8").read()
    when = re.search(r"prices as of <b>(.*?)</b>", t)
    flips = t[t.find("AH Deviation Watchlist</h2>"):]
    flips = flips[:flips.find("</section>")]
    cert, pot, buy, sell = [], [], [], []
    for name, cell in re.findall(r'<td class="nm"[^>]*>([^<]*)((?:(?!</td>).)*)</td>', flips):
        badges, name = set(re.findall(r'badge (\w+)"', cell)), html.unescape(name).strip()
        for b, lst in (("cert", cert), ("pot", pot), ("buy", buy), ("sell", sell)):
            if b in badges:
                lst.append(name)
    manip = sorted({html.unescape(re.sub(r"<[^>]+>", "", n)).strip() for n in
                    re.findall(r'<td class="nm[^"]*"[^>]*>([^<]*)(?:(?!</td>).)*badge manip', t)})
    title = re.search(r"<h1>(.*?)</h1>", t).group(1)
    lines = [f"**{html.unescape(title)}** - prices as of {when.group(1) if when else '?'}"]
    import datetime as _dt
    import report as _R
    try:
        pk = json.load(open(os.path.join(os.path.dirname(market_html), "pick.json")))
    except (OSError, ValueError):
        pk = {}
    days = (_R.RAID_DAY - _dt.date.today()).days
    if pk:
        off = 1 - pk["now"] / pk["usual"]
        lines += ["",
                  f":package: **Raid Prep Pick of the Hour** - our top stock-up buy ahead of the raid release in {days} days "
                  f"(Dec 9), when demand for raid supplies is expected to push prices up:",
                  f"> **{pk['name']}** at **{_R.money(pk['now'])}** each - "
                  + (f"{off:.0%} under its usual {_R.money(pk['usual'])}" if off >= 0.01 else f"right at its usual {_R.money(pk['usual'])}")
                  + f", about {pk['sold']:,.0f} sell per day",
                  "_Disclaimer: this is an educated guess from current market data - the raids aren't out yet, so price "
                  "moves aren't guaranteed._"]
    lines += ["", "**Inside the Toolchest:**",
              ":mag: Item search - full price history for any item",
              ":chart_with_upwards_trend: AH Most Active",
              ":arrows_counterclockwise: AH Deviation Watchlist",
              ":detective: AH Manipulation",
              ":dragon: Raid consumables",
              ":package: Durotar Supply and Logistics",
              ":hammer_pick: Top crafts",
              ":hourglass: Cooldowns",
              ":toolbox: 1-150, 150-300 Profession Kits",
              ":meat_on_bone: Cooking",
              ":firecracker: New in Forever",
              ":alembic: New Forever Crafts", ""]
    where = " (runs in the cloud)" if os.environ.get("CLOUD") else ""
    lines.append(f"_updates every hour{where} - last edit {__import__('report').pacific(fmt='%a {h}:%M %p')}_")
    return "\n".join(lines)


def post(pdfs, content, new=False):
    url = webhook()
    if not url:
        return "skipped: DISCORD_WEBHOOK_URL not set in config.json"
    state = json.load(open(STATE)) if os.path.exists(STATE) and not new else {}
    files = {f"files[{i}]": (os.path.basename(p), open(p, "rb"), "image/png" if p.endswith(".png") else "application/pdf")
             for i, p in enumerate(pdfs)}
    payload = {"content": content,
               "attachments": [{"id": i, "filename": os.path.basename(p)} for i, p in enumerate(pdfs)]}
    try:
        mid = state.get("message_id")
        if mid:   # edit the existing post; listing only the new files replaces the old attachments
            r = requests.patch(f"{url}/messages/{mid}", data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
            if r.status_code == 404:   # post was deleted - start a new one
                mid = None
                for _, fh, _ in files.values():
                    fh.seek(0)
        if not mid:
            r = requests.post(f"{url}?wait=true", data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
        r.raise_for_status()
        json.dump({"message_id": r.json()["id"]}, open(STATE, "w"))
        return f"{'edited' if mid else 'posted'} Discord message"
    except requests.RequestException as e:
        code = getattr(e.response, "status_code", "")
        return f"Discord post failed {code}".strip()   # never print the webhook URL
    finally:
        for _, fh, _ in files.values():
            fh.close()


def build_and_post(new=False):
    import onepage
    market, routes = onepage.build()
    # pictures show right inside Discord (click to zoom); fall back to the PDF if a picture failed
    pics = [p[:-4] + ".png" if os.path.exists(p[:-4] + ".png") else p for p in (market, routes)]
    import glob
    parts = sorted(glob.glob(glob.escape(market[:-4]) + " part*.png"))
    if parts:   # readable split pictures of the market report, then the routes page
        pics = parts + pics[1:]
    banner = os.path.join(HERE, "banner.png")
    if os.path.exists(banner):   # the post just shows the banner - the numbers live on the web page
        pics = [banner]
    content = summary(market.replace(".pdf", ".html"))
    web = market[:-4] + " Interactive.html"
    if os.path.exists(web):   # interactive page on GitHub Pages, linked under the summary
        import publish_web
        print(publish_web.publish_file(os.path.join(os.path.dirname(web), "lookup.json"), "lookup.json"))   # search data first
        url, msg = publish_web.publish(web)
        print(msg)
        if url:
            content += f"\n:link: **Interactive charts** (hover any item for its hourly 7-day price): <{url}>"
    return post(pics, content, new=new)


if __name__ == "__main__":
    print(build_and_post(new="--new" in sys.argv))
