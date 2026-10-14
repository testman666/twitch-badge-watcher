#!/usr/bin/env python3
"""Twitch badge watcher.

Polls Twitch's Helix API for global badges (and optionally specific channels'
badges), compares against the last snapshot, and alerts you when a badge is
added. Alerts go to ntfy.sh (browser / phone push) and/or email.

Usage:
    python watcher.py            # check once (good for cron / GitHub Actions)
    python watcher.py --loop     # keep running, check every POLL_SECONDS
"""
import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path

import requests

try:  # optional: load a local .env file if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

API = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"

CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
CHANNELS = [c.strip().lower() for c in os.environ.get("TWITCH_CHANNELS", "").split(",") if c.strip()]
WATCH_GLOBAL = os.environ.get("WATCH_GLOBAL", "true").lower() != "false"
STATE_FILE = Path(os.environ.get("STATE_FILE", "badges_state.json"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USER)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


class Twitch:
    def __init__(self):
        self.token = None

    def refresh_token(self):
        r = requests.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]

    def get(self, path, params=None):
        if not self.token:
            self.refresh_token()
        for attempt in range(2):
            r = requests.get(
                f"{API}{path}",
                params=params,
                headers={"Client-Id": CLIENT_ID, "Authorization": f"Bearer {self.token}"},
                timeout=20,
            )
            if r.status_code == 401 and attempt == 0:
                self.refresh_token()
                continue
            r.raise_for_status()
            return r.json()["data"]

    def resolve_logins(self, logins):
        ids = {}
        for i in range(0, len(logins), 100):
            chunk = logins[i : i + 100]
            for u in self.get("/users", [("login", l) for l in chunk]):
                ids[u["login"]] = u["id"]
        return ids


def flatten(badge_sets, scope):
    """{ 'scope/set_id/version_id': {title, description, image} }"""
    out = {}
    for s in badge_sets:
        for v in s["versions"]:
            key = f"{scope}/{s['set_id']}/{v['id']}"
            out[key] = {
                "scope": scope,
                "set_id": s["set_id"],
                "version": v["id"],
                "title": v.get("title", ""),
                "description": v.get("description", ""),
                "image": v.get("image_url_4x") or v.get("image_url_2x") or v.get("image_url_1x"),
            }
    return out


def fetch_all(tw):
    snapshot = {}
    if WATCH_GLOBAL:
        snapshot.update(flatten(tw.get("/chat/badges/global"), "global"))
    if CHANNELS:
        ids = tw.resolve_logins(CHANNELS)
        for login in CHANNELS:
            if login not in ids:
                log(f"warning: channel '{login}' not found")
                continue
            snapshot.update(
                flatten(tw.get("/chat/badges", {"broadcaster_id": ids[login]}), f"channel:{login}")
            )
    return snapshot


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(snapshot):
    STATE_FILE.write_text(json.dumps(snapshot, indent=2, sort_keys=True))


# ---------- notifications ----------

def send_ntfy(title, body, click=None):
    if not NTFY_TOPIC:
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "medal_sports"}
    if click:
        headers["Click"] = click
    r = requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


def send_mail(title, body):
    if not (SMTP_HOST and MAIL_TO):
        return
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def notify(new_badges):
    n = len(new_badges)
    title = f"New Twitch badge{'s' if n != 1 else ''} ({n})"
    lines = []
    for b in new_badges:
        where = "Global" if b["scope"] == "global" else b["scope"].split(":", 1)[1]
        lines.append(f"- [{where}] {b['title']} ({b['set_id']} v{b['version']})")
        if b["description"]:
            lines.append(f"    {b['description']}")
        if b["image"]:
            lines.append(f"    {b['image']}")
    body = "\n".join(lines)
    for fn, args in ((send_ntfy, (title, body)), (send_mail, (title, body))):
        try:
            fn(*args)
        except Exception as e:  # one channel failing shouldn't block the other
            log(f"{fn.__name__} failed: {e}")


# ---------- main ----------

def check_once(tw):
    current = fetch_all(tw)
    previous = load_state()

    if previous is None:
        save_state(current)
        log(f"first run: saved baseline of {len(current)} badge versions")
        if NOTIFY_ON_FIRST_RUN:
            notify(list(current.values())[:20])
        return

    added = [current[k] for k in current if k not in previous]
    if added:
        log(f"{len(added)} new badge(s) found")
        notify(added)
    else:
        log("no new badges")
    # keep old entries too, so a badge that disappears and returns isn't re-announced
    save_state({**previous, **current})


def main():
    if not (CLIENT_ID and CLIENT_SECRET):
        sys.exit("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (see .env.example).")
    if not (NTFY_TOPIC or (SMTP_HOST and MAIL_TO)):
        sys.exit("Set NTFY_TOPIC and/or SMTP_HOST + MAIL_TO so there is somewhere to send alerts.")

    tw = Twitch()
    if "--loop" not in sys.argv:
        check_once(tw)
        return
    log(f"watching every {POLL_SECONDS}s (Ctrl+C to stop)")
    while True:
        try:
            check_once(tw)
        except Exception as e:
            log(f"check failed: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
