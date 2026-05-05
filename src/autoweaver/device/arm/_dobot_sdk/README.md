# Dobot Python SDK (vendored)

This directory contains a verbatim copy of the Dobot TCP/IP Python V4 SDK
used to drive Dobot Nova 5 / Nova 2 controllers via the Dashboard (29999)
and Feedback (30004) ports.

## Source

- Upstream: https://github.com/Dobot-Arm/TCP-IP-Python-V4
- Vendored commit: `65a19c9` ("Add CalcUser")
- Vendored on: 2026-05-05
- License: MIT (see `LICENSE`)

## Why vendored

The upstream project does not publish a PyPI package. Vendoring keeps the
import path stable, lets us pin the version, and guarantees that the code
runs on a host that does not have internet access.

## Files

- `dobot_api.py` — the SDK itself, unmodified from upstream.
- `files/alarmController.json`, `files/alarmServo.json` — alarm code
  dictionaries the SDK loads at import time.
- `LICENSE` — upstream MIT license.

## Updating

When you need to take a newer upstream version:

1. Diff the new `dobot_api.py` against the version here so you understand
   what changed.
2. Copy the new file in unmodified, along with any new files in `files/`.
3. Update the "Vendored commit" line above.
4. Re-run the integration smoke tests against a real arm.

Do **not** edit `dobot_api.py` in place. Wrap behavior you need in
`device/arm/dobot.py` instead, so future SDK updates remain easy to apply.
