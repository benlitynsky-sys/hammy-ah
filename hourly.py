"""hourly.py - everything that runs after each fresh AH 113 scan. The poller starts this as a new process every
time, so edits to the report code are picked up without restarting the poller."""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

try:  # market report PDF
    import report
    report.build()
except Exception as e:
    print(f"market report failed: {e}")
try:  # sell alerts for what's in holdings.json
    import alerts
    alerts.check()
except Exception as e:
    print(f"alerts failed: {e}")
try:  # Hammy's one-page report -> web page + Discord (edits the same post each hour; off until settings exist)
    import discord_post
    print(discord_post.build_and_post())
except Exception as e:
    print(f"discord report failed: {e}")
