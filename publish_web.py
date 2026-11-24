"""publish_web.py - put the interactive market report on GitHub Pages (as index.html) so Discord can link to it.
Needs two settings you add to config.json yourself:
  "GITHUB_REPO":  "yourname/hammy-ah"
  "GITHUB_TOKEN": "<fine-grained token with Contents: Read and write on that one repo>"
Nothing is uploaded until both are set. The token is never printed.
Run by hand:  python publish_web.py  (uploads the newest Interactive page)"""
import base64
import glob
import json
import os

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def settings():
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    return (cfg.get("GITHUB_REPO") or "").strip().strip("/"), (cfg.get("GITHUB_TOKEN") or "").strip()


def page_url(repo):
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def publish(html_path):
    """Upload html_path as index.html. Returns (url or None, message)."""
    repo, token = settings()
    if not repo or not token:
        return None, "web page skipped: GITHUB_REPO / GITHUB_TOKEN not set in config.json"
    api = f"https://api.github.com/repos/{repo}/contents/index.html"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(api, headers=h, timeout=30)
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = {"message": f"report update {os.path.basename(html_path)}",
                "content": base64.b64encode(open(html_path, "rb").read()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(api, headers=h, json=body, timeout=60)
        r.raise_for_status()
        return page_url(repo), "web page updated"
    except requests.RequestException as e:
        return None, f"web page upload failed {getattr(e.response, 'status_code', '')}".strip()   # never print the token


if __name__ == "__main__":
    newest = max(glob.glob(os.path.join(HERE, "output", "*Market Report Interactive.html")), key=os.path.getmtime)
    url, msg = publish(newest)
    print(msg, url or "")
