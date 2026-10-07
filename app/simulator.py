"""Controllable buoy-controller simulator.

Serves a 16-bit register address space over::

    GET /read?deviceId=...&start=<addr>&count=<n>[&revision=<rev>]

Registers can be updated mid-test, bumping ``revision``; conditional
reads carrying a stale revision get ``409 revision_mismatch``.

Control endpoints (used by the smoke tests):

* ``POST /control/devices/{id}/regenerate`` - rewrite every register
  and bump the revision (simulates a config revision change).
* ``POST /control/devices/{id}/set`` body ``{"addr": n, "value": v}``
  - write one register and bump the revision.
* ``POST /control/devices`` - create a device with a fresh register map.
* ``POST /control/fault`` body ``{"mode": "...", "n": k, "skip": s}``
  - after ``s`` unmodified reads, inject faults for the next *k* reads:

    - ``rev_change`` - bump the revision (page 1 observes it; later
      conditional pages get ``409``), models a mid-read config update;
    - ``timeout``  - stall past the gateway's timeout;
    - ``error500`` - answer HTTP 500;
    - ``short_page`` - return fewer registers than requested;
    - ``bad_value`` - return a non-u16 value;
    - ``bad_echo``  - echo a wrong start address;
    - ``none``      - clear the injected fault.

* ``GET  /control/state`` - inspect revisions and fault counters.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import FAULT_SLEEP_SECONDS, REGISTER_SPACE

app = FastAPI(title="Buoy Controller Simulator", version="1.0.0")
_lock = threading.Lock()

# deviceId -> {"registers": [...u16...], "revision": int}
_devices: dict[str, dict[str, Any]] = {}

# fault state: {"mode": str, "remaining": int, "skip": int, "count": int}
_fault = {"mode": "none", "remaining": 0, "skip": 0, "count": 0}

MAX_PAGE = 64


def _device(device_id: str) -> dict[str, Any]:
    dev = _devices.get(device_id)
    if dev is None:
        rng = random.Random(hash(device_id) & 0xFFFFFFFF)
        dev = {
            "registers": [rng.randrange(0, 0x10000) for _ in range(REGISTER_SPACE)],
            "revision": 1,
        }
        _devices[device_id] = dev
    return dev


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/read")
def read(
    deviceId: str = Query(...),
    start: int = Query(..., ge=0),
    count: int = Query(..., ge=1, le=MAX_PAGE),
    revision: int | None = Query(None),
) -> JSONResponse:
    # The lock only guards shared state; the injected timeout stall must
    # happen outside it so other requests are not blocked (a real
    # controller answers independently per request).
    with _lock:
        dev = _device(deviceId)
        size = len(dev["registers"])
        if start + count > size:
            return JSONResponse(
                status_code=400,
                content={"error": f"range out of bounds: {start}+{count} > {size}"},
            )

        # Fault scheduling: skip first `skip` reads, then inject on the
        # next `remaining` reads.
        if _fault["skip"] > 0:
            _fault["skip"] -= 1
            mode = None
        elif _fault["remaining"] > 0:
            _fault["remaining"] -= 1
            _fault["count"] += 1
            mode = _fault["mode"]
        else:
            mode = None

        if mode == "rev_change":
            # Simulate a config update: revision bumps. A conditional
            # read with the old revision gets 409; an unconditional
            # first page simply observes the new revision.
            dev["revision"] += 1

        current_rev = dev["revision"]
        values = list(dev["registers"][start:start + count])

    # Conditional read: the revision must still match the first page.
    if revision is not None and revision != current_rev:
        return JSONResponse(
            status_code=409,
            content={"error": "revision_mismatch",
                     "expected": revision, "current": current_rev},
        )

    if mode == "timeout":
        # Stall without the lock, past the gateway's read timeout.
        time.sleep(FAULT_SLEEP_SECONDS)
        return JSONResponse(
            status_code=200,
            content={"deviceId": deviceId, "start": start, "count": count,
                     "revision": current_rev, "values": values},
        )
    if mode == "error500":
        return JSONResponse(status_code=500, content={"error": "injected fault"})

    echo_start, echo_count = start, count
    if mode == "short_page":
        values = values[:-1]
    elif mode == "bad_value":
        values = [0x10000] + values[1:]
    elif mode == "bad_echo":
        echo_start = start + 1

    return JSONResponse(
        status_code=200,
        content={
            "deviceId": deviceId,
            "start": echo_start,
            "count": echo_count,
            "revision": current_rev,
            "values": values,
        },
    )


class FaultRequest(BaseModel):
    mode: str = Field(pattern="^(timeout|error500|short_page|bad_value|bad_echo|rev_change|none)$")
    n: int = Field(default=1, ge=0, le=10_000)
    skip: int = Field(default=0, ge=0, le=10_000)


@app.post("/control/fault")
def inject_fault(req: FaultRequest) -> dict[str, Any]:
    with _lock:
        _fault["mode"] = req.mode
        _fault["remaining"] = 0 if req.mode == "none" else req.n
        _fault["skip"] = req.skip
        _fault["count"] = 0
        return {"mode": _fault["mode"], "remaining": _fault["remaining"],
                "skip": _fault["skip"]}


@app.post("/control/devices/{device_id}/regenerate")
def regenerate(device_id: str) -> dict[str, Any]:
    with _lock:
        rng = random.Random()
        dev = _device(device_id)
        dev["registers"] = [rng.randrange(0, 0x10000) for _ in range(REGISTER_SPACE)]
        dev["revision"] += 1
        return {"deviceId": device_id, "revision": dev["revision"]}


class SetRegister(BaseModel):
    addr: int = Field(ge=0)
    value: int = Field(ge=0, le=0xFFFF)


@app.post("/control/devices/{device_id}/set")
def set_register(device_id: str, req: SetRegister) -> dict[str, Any]:
    with _lock:
        dev = _device(device_id)
        if req.addr >= len(dev["registers"]):
            return JSONResponse(status_code=400, content={"error": "addr out of bounds"})
        dev["registers"][req.addr] = req.value
        dev["revision"] += 1
        return {"deviceId": device_id, "addr": req.addr,
                "revision": dev["revision"]}


@app.post("/control/devices")
def create_device() -> dict[str, str]:
    return {"status": "devices are created lazily on first /read"}


@app.get("/control/state")
def control_state() -> dict[str, Any]:
    with _lock:
        return {
            "devices": {
                d: {"revision": v["revision"], "size": len(v["registers"])}
                for d, v in _devices.items()
            },
            "fault": dict(_fault),
        }
