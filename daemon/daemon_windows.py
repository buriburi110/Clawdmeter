"""Claude Usage Tracker Daemon - Windows variant.

Windows port of `claude_usage_daemon.py`. Aligned with the original on:
- API call shape (POST /v1/messages with anthropic-ratelimit-* response headers)
- Payload format ({"s","sr","w","wr","st","ok"})
- BLE device name / service / characteristic UUIDs
- Address cache location (~/.config/claude-usage-monitor/ble-address)
- Reconnect / backoff behaviour

Differences from the original:
- macOS Keychain branch removed; token always read from
  %USERPROFILE%\\.claude\\.credentials.json.
- httpx dependency dropped in favour of stdlib urllib (one less wheel
  on Windows); the API call runs in a thread executor to stay async.
- `--once` and `--no-ble` flags for board-free verification (Phase 1).
- bleak is imported lazily so `--no-ble --once` works without bleak
  installed at all.

Usage:
  python daemon_windows.py --once --no-ble    # one poll + print, no BLE
  python daemon_windows.py --no-ble           # console-only loop
  python daemon_windows.py                    # full BLE daemon (board needed)
"""
from __future__ import annotations

import asyncio
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEVICE_NAME = "Claude Controller"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Same shape-tolerance as the original: nested object, direct object,
    regex fallback, raw token.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def read_token() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Windows BLE addresses (via WinRT) come as either MAC AA:BB:CC:DD:EE:FF
    # or as the same colon-separated form bleak normalises to. Accept both
    # MAC and macOS-style UUID for compatibility with caches written on
    # other OSes.
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


def _http_post_sync(url: str, headers: dict, body: dict, timeout: float = 20.0):
    """Blocking POST; returns (status, headers_dict, body_bytes)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        status, resp_headers, _body = await asyncio.to_thread(
            _http_post_sync, API_URL, headers, API_BODY
        )
    except (urllib.error.URLError, OSError) as e:
        log(f"API call failed: {e}")
        return None

    if status >= 500:
        log(f"API returned {status}; skipping this poll")
        return None
    # 4xx still has rate-limit headers attached; useful to surface auth errors.
    if status >= 400:
        log(f"API returned {status} (token may be expired)")

    # Header lookup is case-insensitive in HTTP; urllib's dict() over a
    # Message preserves header names roughly as-sent. Build a lowercased
    # view so we don't depend on capitalisation.
    lower = {k.lower(): v for k, v in resp_headers.items()}

    def hdr(name: str, default: str = "0") -> str:
        return lower.get(name.lower(), default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except (ValueError, TypeError):
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except (ValueError, TypeError):
            return 0

    payload = {
        "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "ok": status < 400,
    }
    return payload


# ---------------------------------------------------------------------------
# BLE session — only used when --no-ble is NOT passed. bleak is imported
# lazily so console-only verification works without the dependency.
# ---------------------------------------------------------------------------


def _import_bleak():
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError

    return BleakClient, BleakScanner, BleakError


class Session:
    def __init__(self, client, BleakError) -> None:
        self.client = client
        self._BleakError = BleakError
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (self._BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except self._BleakError as e:
            log(f"Write failed: {e}")
            return False


async def scan_for_device(BleakScanner) -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


async def connect_and_run(address: str, stop_event: asyncio.Event,
                          BleakClient, BleakError) -> bool:
    log(f"Connecting to {address}...")
    client = BleakClient(address)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client, BleakError)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                token = read_token()
                if not token:
                    log("No token; skipping poll")
                else:
                    payload = await poll_api(token)
                    if payload is not None:
                        if await session.write_payload(payload):
                            last_poll = time.time()
                            used_successfully = True

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def ble_main(stop_event: asyncio.Event) -> None:
    BleakClient, BleakScanner, BleakError = _import_bleak()
    log("=== Claude Usage Tracker Daemon (BLE, Windows) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    while not stop_event.is_set():
        address = load_cached_address()
        if not address:
            address = await scan_for_device(BleakScanner)
            if address:
                save_address(address)
            else:
                log(f"Device not found, retrying in {backoff}s...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)
                continue

        ok = await connect_and_run(address, stop_event, BleakClient, BleakError)
        if not ok:
            log("Invalidating cached address")
            SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


# ---------------------------------------------------------------------------
# Console-only mode for board-free verification.
# ---------------------------------------------------------------------------


async def console_main(stop_event: asyncio.Event, once: bool) -> None:
    log("=== Claude Usage Tracker Daemon (console, Windows) ===")
    if not once:
        log(f"Poll interval: {POLL_INTERVAL}s. Ctrl+C to stop.")
    while not stop_event.is_set():
        token = read_token()
        if not token:
            log("No token; sleeping")
        else:
            payload = await poll_api(token)
            if payload is None:
                log("Poll failed")
            else:
                log(f"Payload: {json.dumps(payload, separators=(',', ':'))}")
        if once:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    once = "--once" in sys.argv
    no_ble = "--no-ble" in sys.argv

    stop_event = asyncio.Event()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    # Windows: add_signal_handler raises NotImplementedError on ProactorEventLoop.
    # Fall back to signal.signal which works for SIGINT/SIGBREAK there.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _stop)
        except (NotImplementedError, AttributeError):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

    if no_ble:
        await console_main(stop_event, once)
    else:
        if once:
            log("--once is only supported with --no-ble; ignoring")
        await ble_main(stop_event)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
