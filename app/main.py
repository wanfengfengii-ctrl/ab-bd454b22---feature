"""Ocean-station snapshot gateway.

``POST /api/snapshots`` returns register evidence that always comes
from a *single* controller revision. See :mod:`app.snapshot_service`
for the read/discard/retry protocol and :mod:`app.storage` for the
idempotent persistence rules.
"""
from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import compatibility, config, snapshot_service, storage
from .storage import COMPLETE, FAILED, InflightRegistry


@asynccontextmanager
async def lifespan(app: FastAPI):
    import os

    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    storage.init_db()
    yield


app = FastAPI(title="Buoy Snapshot Gateway", version="1.0.0", lifespan=lifespan)
_inflight = InflightRegistry()

MAX_REGISTERS = 512


class SnapshotRequest(BaseModel):
    snapshotId: str = Field(min_length=1, max_length=200)
    deviceId: str = Field(min_length=1, max_length=200)
    startAddr: int = Field(ge=0, le=0xFFFF)
    count: int = Field(ge=1, le=MAX_REGISTERS)


class CompatibilityMaskRule(BaseModel):
    startAddr: int = Field(ge=0, le=0xFFFF)
    endAddr: int = Field(ge=0, le=0xFFFF)  # inclusive
    # 16-bit masks may arrive as integers or "0x...." hex strings.
    setAllowed: int | str = Field(...)
    clearAllowed: int | str = Field(...)

    @field_validator("setAllowed", "clearAllowed")
    @classmethod
    def _mask_is_u16(cls, value: int | str) -> int | str:
        if isinstance(value, bool):
            raise ValueError("mask must be a 16-bit unsigned integer, not a boolean")
        if isinstance(value, int):
            if not 0 <= value <= 0xFFFF:
                raise ValueError("mask must be within the 16-bit range 0..0xFFFF")
            return value
        text = value.strip()
        try:
            parsed = int(text, 16) if text.lower().startswith("0x") else int(text, 10)
        except ValueError:
            raise ValueError(f"mask {value!r} is not a valid 16-bit mask") from None
        if not 0 <= parsed <= 0xFFFF or len(text) == 0:
            raise ValueError(f"mask {value!r} is not a valid 16-bit mask")
        return value


class CompatibilityRequest(BaseModel):
    baselineSnapshotId: str = Field(min_length=1, max_length=200)
    candidateSnapshotId: str = Field(min_length=1, max_length=200)
    startAddr: int = Field(ge=0, le=0xFFFF)
    count: int = Field(ge=1, le=MAX_REGISTERS)
    rules: list[CompatibilityMaskRule] = Field(min_length=1, max_length=64)


def _evidence_response(row: dict[str, Any], replayed: bool) -> JSONResponse:
    return JSONResponse(
        status_code=200 if replayed else 201,
        content={
            "snapshotId": row["snapshot_id"],
            "deviceId": row["device_id"],
            "startAddr": row["start_addr"],
            "count": row["count"],
            "revision": row["revision"],
            "values": row["values"],
            "sha256": row["sha256"],
        },
    )


def _existing_row_response(row: dict[str, Any]) -> JSONResponse | None:
    """Map an already-persisted row to the canonical HTTP response."""
    if row["state"] == COMPLETE:
        return _evidence_response(row, replayed=True)
    if row["state"] == FAILED:
        reason = row.get("error") or ""
        kind = reason[1:].split("]", 1)[0] if reason.startswith("[") else "upstream_error"
        status = 409 if kind in ("revision_conflict", "parameter_conflict") else 502
        return JSONResponse(
            status_code=status,
            content={
                "error": kind,
                "reason": reason,
                "snapshotId": row["snapshot_id"],
            },
        )
    return None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _failure_response(exc: "snapshot_service.Failure", sid: str) -> JSONResponse:
    if exc.kind in ("revision_conflict", "parameter_conflict"):
        status = 409
    elif exc.kind == "bad_range":
        status = 400
    else:
        status = 502
    return JSONResponse(
        status_code=status,
        content={"error": exc.kind, "reason": str(exc), "snapshotId": sid},
    )


@app.post("/api/snapshots")
def create_snapshot(req: SnapshotRequest) -> JSONResponse:
    sid = req.snapshotId

    # Fast path: a terminal row already exists (replay / stable failure).
    existing = storage.get(sid)
    if existing is not None:
        if not storage.same_params(existing, req.deviceId, req.startAddr, req.count):
            return JSONResponse(
                status_code=409,
                content={
                    "error": "parameter_conflict",
                    "reason": (
                        f"snapshotId {sid!r} already exists with different parameters "
                        f"(deviceId={existing['device_id']!r}, "
                        f"startAddr={existing['start_addr']}, count={existing['count']})"
                    ),
                },
            )
        resp = _existing_row_response(existing)
        assert resp is not None
        return resp

    # Coalesce same-snapshotId requests inside this process.
    state, slot = _inflight.acquire(sid, req.deviceId, req.startAddr, req.count)
    if state == "conflict":
        return JSONResponse(
            status_code=409,
            content={"error": "parameter_conflict",
                     "reason": "snapshotId is in progress with different parameters"},
        )
    if state == "waiter":
        slot.event.wait()
        if isinstance(slot.exc, snapshot_service.Failure):
            # Worker raised (e.g. bad range: by design nothing is
            # persisted) - share the same definitive answer.
            return _failure_response(slot.exc, sid)
        if slot.exc is not None:
            return JSONResponse(
                status_code=503,
                content={"error": "snapshot_pending",
                         "reason": "acquisition failed unexpectedly; retry"},
            )
        row = storage.get(sid)
        if row is None:
            # Owner vanished without persisting anything (e.g. killed):
            # never fabricate evidence; ask the client to retry.
            return JSONResponse(
                status_code=503,
                content={"error": "snapshot_pending",
                         "reason": "acquisition abandoned before completion; retry"},
            )
        resp = _existing_row_response(row)
        assert resp is not None
        return resp

    try:
        row = snapshot_service.acquire(sid, req.deviceId, req.startAddr, req.count)
        slot.fulfill(row)
        return _evidence_response(row, replayed=row.get("outcome") == "replayed")
    except snapshot_service.Failure as exc:
        slot.fail(exc)
        return _failure_response(exc, sid)
    except Exception as exc:  # never leave waiters hanging or leak 500 HTML
        slot.fail(exc)
        return JSONResponse(
            status_code=500,
            content={"error": "gateway_error", "reason": str(exc),
                     "snapshotId": sid},
        )
    finally:
        _inflight.release(sid, slot)


@app.get("/api/snapshots/{snapshot_id}")
def get_snapshot(snapshot_id: str) -> JSONResponse:
    row = storage.get(snapshot_id)
    # Failed or missing snapshots are not queryable as evidence.
    if row is None or row["state"] != COMPLETE:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found",
                     "reason": "no complete snapshot exists for this snapshotId"},
        )
    return _evidence_response(row, replayed=True)


def _compatibility_error(exc: "compatibility.CompatibilityError") -> JSONResponse:
    statuses = {
        "snapshot_not_found": 404,
        "device_mismatch": 409,
        "insufficient_coverage": 422,
        "bad_window": 400,
        "invalid_rule": 400,
        "rule_outside_window": 400,
    }
    return JSONResponse(
        status_code=statuses.get(exc.kind, 400),
        content={"error": exc.kind, "reason": exc.reason},
    )


@app.post("/api/snapshots/compatibility")
def compare_compatibility(req: CompatibilityRequest) -> JSONResponse:
    """Adjudicate a firmware upgrade against a bit-direction policy.

    The decision is all-or-nothing: missing evidence, a cross-device pair,
    an uncovered window or an illegal rule tiling each return a stable
    reason without any partial verdict; a legal request pinpoints every
    register bit that moved in a forbidden direction.
    """
    try:
        baseline = storage.get(req.baselineSnapshotId)
        candidate = storage.get(req.candidateSnapshotId)
        # A FAILED row is not evidence: treat it the same as missing so a
        # verdict can never rest on an incomplete snapshot.
        if baseline is not None and baseline["state"] != COMPLETE:
            baseline = None
        if candidate is not None and candidate["state"] != COMPLETE:
            candidate = None
        compatibility.require_same_device_snapshots(
            baseline, candidate, req.baselineSnapshotId, req.candidateSnapshotId
        )
        assert baseline is not None and candidate is not None
        verdict = compatibility.check_compatibility(
            baseline,
            candidate,
            req.startAddr,
            req.count,
            [r.model_dump() for r in req.rules],
        )
    except compatibility.CompatibilityError as exc:
        return _compatibility_error(exc)
    return JSONResponse(status_code=200, content=verdict)
