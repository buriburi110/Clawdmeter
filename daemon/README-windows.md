# Clawdmeter - Windows daemon

Windows port of `claude_usage_daemon.py`. **Wire-compatible** with the
existing firmware - same payload, same BLE service / characteristic UUIDs,
same poll interval - so a board that already pairs with the macOS / Linux
daemon will pair with this one without re-flashing.

## What changed vs. the original

| | Original (macOS / Linux) | Windows |
| --- | --- | --- |
| Token store | macOS Keychain (`security find-generic-password`) or `~/.claude/.credentials.json` | `%USERPROFILE%\.claude\.credentials.json` only |
| HTTP client | `httpx` | stdlib `urllib` (one less wheel to install) |
| BLE backend | bleak / CoreBluetooth or BlueZ | bleak / WinRT |
| Console-only verification | not built in | `--no-ble` / `--once` flags |
| Token auto-refresh | not built in | yes, via Claude Code CLI wrapper |

API call, response-header parsing, payload shape, BLE UUIDs and
reconnect / backoff logic are all kept identical.

### About auto-refresh

Anthropic's OAuth refresh endpoint isn't publicly documented, so we don't
re-implement it. Instead, when the access token's `expiresAt` is within 60
seconds of now, the daemon shells out to `claude.exe -p hi` once. The CLI
checks `expiresAt` on startup, uses the stored `refreshToken` to fetch a
new access token, rewrites `~/.claude/.credentials.json`, then exits. We
re-read the file and continue polling. No extra credentials, no
reverse-engineering, no maintenance burden when Anthropic rotates their
OAuth flow.

The CLI is found by globbing
`%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\claude-code\*\claude.exe`
and picking the most recently modified version directory, so app upgrades
are followed automatically. If the CLI isn't installed, auto-refresh is
silently skipped and the daemon falls back to its previous behaviour
(log a 401 and try again next poll).

## Status

- [x] Polls Claude Code usage every 60 seconds via `POST /v1/messages`
- [x] Reads OAuth token from `%USERPROFILE%\.claude\.credentials.json`
- [x] Builds the `{s, sr, w, wr, st, ok}` payload the firmware expects
- [x] BLE scan / connect / write / refresh-notify (via `bleak`)
- [x] Console-only mode for board-free verification
- [x] Automatic token refresh (delegates to the Claude Code CLI)
- [ ] Hardware-tested (waiting on board)

## Requirements

- Windows 10 / 11
- Python 3.9+
- A logged-in Claude Code session (so `~/.claude/.credentials.json` exists)
- For BLE mode: `pip install -r daemon/requirements-windows.txt`
  (installs `bleak`; not needed for `--no-ble`)

## Run

**Verify the API/payload pipeline without a board:**

```powershell
python daemon\daemon_windows.py --once --no-ble
```

Expected:

```
[21:38:05] === Claude Usage Tracker Daemon (console, Windows) ===
[21:38:06] Payload: {"s":12,"sr":251,"w":38,"wr":2601,"st":"ok","ok":true}
```

**Continuous console loop** (still no board):

```powershell
python daemon\daemon_windows.py --no-ble
```

**Full BLE daemon** (needs board paired and reachable):

```powershell
pip install -r daemon\requirements-windows.txt
python daemon\daemon_windows.py
```

The cached BLE address lives at
`%USERPROFILE%\.config\claude-usage-monitor\ble-address` - same path the
original daemon uses, so caches are interchangeable across OSes.

## Payload reference

Written byte-identical to the firmware's `RX` characteristic
(`4c41555a-4465-7669-6365-000000000002`):

| Key | Type | Source header | Meaning |
| --- | --- | --- | --- |
| `s` | int 0..100 | `anthropic-ratelimit-unified-5h-utilization` * 100 | 5-hour window utilization % |
| `sr` | int (minutes) | `anthropic-ratelimit-unified-5h-reset` - now | minutes until 5h reset (0 if past) |
| `w` | int 0..100 | `anthropic-ratelimit-unified-7d-utilization` * 100 | 7-day window utilization % |
| `wr` | int (minutes) | `anthropic-ratelimit-unified-7d-reset` - now | minutes until 7d reset (0 if past) |
| `st` | string | `anthropic-ratelimit-unified-5h-status` | status string (e.g. `"ok"`) |
| `ok` | bool | derived | true if HTTP status < 400 |

## Known limitations

- **Auto-refresh requires Claude Code CLI installed.** If the CLI isn't
  found under `%LOCALAPPDATA%\Packages\Claude_*\...`, the daemon logs the
  miss and falls back to "log 401 and retry next poll" - the original
  behaviour. Install Claude Code (any recent version) to enable refresh.
- **Auto-refresh blocks the poll loop briefly.** Each refresh runs
  `claude.exe -p hi` and waits for it to exit (timeout 20 s). On a slow
  network this can stall a single poll cycle by a few seconds.
- **Windows BLE quirks**: `bleak`'s WinRT backend requires Bluetooth to
  be enabled in Windows Settings, and pairing is sometimes flakier than
  on Linux. If you see `OSError: [WinError -2147418113]` during
  `connect()`, toggle Bluetooth off/on in Windows Settings and retry.
