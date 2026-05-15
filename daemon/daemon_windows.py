"""Windows variant of the Clawdmeter daemon.

Original project: https://github.com/HermannBjorgvin/Clawdmeter
This file is an alternative Windows-friendly implementation.

Differences from the original `claude_usage_daemon.py`:
- Uses the dedicated OAuth usage endpoint (GET /api/oauth/usage) instead
  of parsing rate-limit headers from a 1-token /v1/messages call.
- Pure stdlib only (no httpx / requests / keyring required).
- Reads the OAuth token from the standard Windows path
  %USERPROFILE%\\.claude\\.credentials.json.
- BLE write is NOT yet implemented; this is the polling+console layer
  (Phase 1). BLE integration is planned once hardware is in hand.
- No automatic refresh-token handling yet; warns when the token is expired.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
POLL_INTERVAL = 60


def load_token() -> str:
    data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    oauth = data["claudeAiOauth"]
    if oauth.get("expiresAt", 0) / 1000 < time.time():
        print("[warn] access token expired - run Claude Code CLI once to refresh", file=sys.stderr)
    return oauth["accessToken"]


def http_request(url, *, method="GET", headers=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def fetch_oauth_usage(token):
    status, _, body = http_request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    if status != 200:
        print(f"[oauth-usage] HTTP {status}: {body[:200].decode(errors='replace')}", file=sys.stderr)
        return None
    return json.loads(body)


def fetch_messages_headers(token):
    status, headers, body = http_request(
        MESSAGES_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        body={
            "model": "claude-haiku-4-5",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        },
    )
    if status >= 400:
        print(f"[messages] HTTP {status}: {body[:200].decode(errors='replace')}", file=sys.stderr)
    return {k: v for k, v in headers.items() if "ratelimit" in k.lower() or k.lower().startswith("anthropic")}


def minutes_until(iso_ts):
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((ts - datetime.now(timezone.utc)).total_seconds() // 60))


def format_oauth(usage):
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d"), ("seven_day_opus", "7d_opus")):
        block = usage.get(key)
        if not block:
            continue
        mins = minutes_until(block.get("resets_at"))
        reset = f"reset in {mins}m" if mins is not None else "no reset"
        parts.append(f"{label}: {block.get('utilization', 0):>5.1f}% ({reset})")
    return "  |  ".join(parts) if parts else f"(unrecognized payload: {json.dumps(usage)[:200]})"


def poll_once(token):
    stamp = datetime.now().strftime("%H:%M:%S")
    usage = fetch_oauth_usage(token)
    if usage is not None:
        print(f"[{stamp}] {format_oauth(usage)}")
        return
    headers = fetch_messages_headers(token)
    if headers:
        kv = " ".join(f"{k}={v}" for k, v in headers.items())
        print(f"[{stamp}] (fallback) {kv}")
    else:
        print(f"[{stamp}] both endpoints failed")


def main():
    token = load_token()
    print(f"Token loaded ({len(token)} chars).")
    if "--once" in sys.argv:
        poll_once(token)
        return
    print(f"Polling every {POLL_INTERVAL}s. Ctrl+C to stop.\n")
    while True:
        poll_once(token)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
