# Clawdmeter - Windows daemon (alternative)

Windows-friendly Python daemon for the Clawdmeter project.

## Status

- [x] Polls Claude Code usage every 60 seconds
- [x] Reads OAuth token from `%USERPROFILE%\.claude\.credentials.json`
- [ ] BLE write (firmware integration) - planned once an ESP32-S3 board is in hand
- [ ] Automatic refresh-token handling

## Why a separate daemon

The original `claude_usage_daemon.py` parses rate-limit headers returned by a
1-token `/v1/messages` call. This Windows variant calls the dedicated
`GET /api/oauth/usage` endpoint instead, which:

- returns 5h / 7d / 7d-Opus utilization as structured JSON,
- consumes no inference tokens,
- uses no third-party dependencies (Python stdlib only).

Functionally equivalent for the meter; different transport.

## Requirements

- Windows 10 / 11
- Python 3.7+
- A logged-in Claude Code session (so `~/.claude/.credentials.json` exists)

## Run

One-shot (handy for verification):

```powershell
python daemon\daemon_windows.py --once
```

Continuous (60-second polling):

```powershell
python daemon\daemon_windows.py
```

## Sample output

```
Token loaded (108 chars).
[21:38:05] 5h:  12.0% (reset in 251m)  |  7d:  38.0% (reset in 2601m)
```

## Known limitations

- If the access token has expired, the daemon prints a warning and the API
  returns HTTP 401. Refresh by running the Claude Code CLI once; auto-refresh
  will be added later.
- BLE write is not implemented yet. Use the original Linux/macOS daemon if
  you have a working board, or wait for the firmware integration.
