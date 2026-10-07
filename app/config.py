"""Runtime settings, all overridable via environment variables."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# --- gateway ---
# Base URL of the buoy controller (the simulator in Docker Compose)
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8080")

# Registers fetched per upstream page. The controller caps this at 64;
# the gateway must never ask for more in a single request.
PAGE_SIZE = min(64, max(1, _int("PAGE_SIZE", 64)))

# Per-request timeout for one controller page read (seconds)
CONTROLLER_TIMEOUT = _int("CONTROLLER_TIMEOUT", 3)

# Full read-through attempts (always restarted from the first page)
# before the snapshot is declared unstable.
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 3)

# SQLite file holding complete snapshots (mounted volume in compose)
DB_PATH = os.environ.get("DB_PATH", "/data/snapshots.db")

GATEWAY_HOST = os.environ.get("GATEWAY_HOST", "0.0.0.0")
GATEWAY_PORT = _int("GATEWAY_PORT", 8000)

# --- buoy controller simulator ---
# Size of the simulated 16-bit register address space
REGISTER_SPACE = _int("REGISTER_SPACE", 4096)

# How long the injected "timeout" fault stalls before answering (seconds)
FAULT_SLEEP_SECONDS = _int("FAULT_SLEEP_SECONDS", 5)
