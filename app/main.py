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
from pydantic import BaseModel, Field

from . import config, snapshot_service, storage
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
