#!/usr/bin/env python3
"""One-off verification entrypoint (the ``verify`` compose service).

It exits 0 only when *all* of the following pass; otherwise non-zero:

1. Build check  - every source file byte-compiles.
2. Unit tests   - the pytest suite (store + acquisition protocol).
3. Smoke tests  - against the live gateway and simulator:
   a. a stable multi-page read yields single-revision evidence with a
      correct big-endian SHA-256;
   b. a revision change mid-read causes a whole-round discard/retry and
      still returns one consistent revision;
   c. replaying the same snapshotId/parameters returns the identical
      snapshot (idempotent), while different parameters conflict;
   d. a controller that keeps changing revisions yields a *stable*
      conflict and leaves no queryable evidence.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import uuid

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
CONTROLLER = os.environ.get("CONTROLLER_URL", "http://controller:8080")

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

failures: list[str] = []


def report(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def sha256_be(values: list[int]) -> str:
    blob = b"".join(v.to_bytes(2, "big") for v in values)
    return hashlib.sha256(blob).hexdigest()


def ctrl_fault(mode: str, n: int = 1, skip: int = 0) -> None:
    httpx.post(
        f"{CONTROLLER}/control/fault",
        json={"mode": mode, "n": n, "skip": skip},
        timeout=5,
    ).raise_for_status()


# ------------------------------------------------------------ stages

def stage_build() -> None:
    print("== build check ==")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", os.path.join(root, "app")]
    )
    report("all modules byte-compile", proc.returncode == 0)


def stage_unit_tests() -> None:
    print("== unit tests ==")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=root)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", os.path.join(root, "tests"),
         "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=root,
        env=env,
    )
    report("pytest suite passes", proc.returncode == 0)


def stage_smoke() -> None:
    print("== smoke tests (live gateway + simulator) ==")
    with httpx.Client(timeout=30) as http:
        _wait_healthy(http)

        # (a) stable multi-page read --------------------------------
        sid = f"smoke-stable-{uuid.uuid4()}"
        r = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid, "deviceId": "smoke-a",
            "startAddr": 0, "count": 200,
        })
        ok = r.status_code == 201
        body = r.json() if ok else {}
        ok = ok and len(body.get("values", [])) == 200
        ok = ok and body.get("sha256") == sha256_be(body.get("values", []))
        report("stable read: 200 regs, single revision, valid hash", ok,
               f"status={r.status_code}")

        # (c1) idempotent replay ------------------------------------
        r2 = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid, "deviceId": "smoke-a",
            "startAddr": 0, "count": 200,
        })
        rg = http.get(f"{GATEWAY}/api/snapshots/{sid}")
        report("idempotent replay returns identical evidence",
               r2.status_code == 200 and r2.json() == body
               and rg.status_code == 200 and rg.json() == body,
               f"status={r2.status_code}")

        # (c2) different parameters conflict ------------------------
        r3 = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid, "deviceId": "smoke-a",
            "startAddr": 0, "count": 201,
        })
        report("same snapshotId + different params -> 409 conflict",
               r3.status_code == 409
               and r3.json().get("error") == "parameter_conflict",
               f"status={r3.status_code}")

        # (b) revision change mid-read, then recovery ---------------
        # Fault: first page OK (skip=1), revision bumps on page 2 of
        # attempt 1; attempt 2 then reads the new revision cleanly.
        ctrl_fault("rev_change", n=1, skip=1)
        sid2 = f"smoke-retry-{uuid.uuid4()}"
        rb = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid2, "deviceId": "smoke-b",
            "startAddr": 0, "count": 200,
        })
        okb = rb.status_code == 201
        bb = rb.json() if okb else {}
        okb = okb and bb.get("revision", 0) >= 2
        okb = okb and bb.get("sha256") == sha256_be(bb.get("values", []))
        report("revision-change: round discarded, retried, one revision",
               okb, f"status={rb.status_code}")
        ctrl_fault("none", n=0)

        # one-shot transport fault also recovers
        ctrl_fault("short_page", n=1, skip=1)
        sid3 = f"smoke-fault-{uuid.uuid4()}"
        rt = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid3, "deviceId": "smoke-t",
            "startAddr": 0, "count": 100,
        })
        okt = rt.status_code == 201
        bt = rt.json() if okt else {}
        okt = okt and bt.get("sha256") == sha256_be(bt.get("values", []))
        report("transient short-page fault is retried and succeeds",
               okt, f"status={rt.status_code}")
        ctrl_fault("none", n=0)

        # one-shot controller timeout also recovers after round discard
        ctrl_fault("timeout", n=1, skip=1)
        sid_to = f"smoke-timeout-{uuid.uuid4()}"
        rto = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid_to, "deviceId": "smoke-to",
            "startAddr": 0, "count": 100,
        })
        okto = rto.status_code == 201
        bto = rto.json() if okto else {}
        okto = okto and bto.get("sha256") == sha256_be(bto.get("values", []))
        report("transient timeout is retried and succeeds",
               okto, f"status={rto.status_code}")
        ctrl_fault("none", n=0)

        # (d) continuously changing revision -> stable failure ------
        ctrl_fault("rev_change", n=10_000, skip=1)
        sid4 = f"smoke-churn-{uuid.uuid4()}"
        rd = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid4, "deviceId": "smoke-d",
            "startAddr": 0, "count": 200,
        })
        okd = rd.status_code == 409
        okd = okd and rd.json().get("error") == "revision_conflict"
        rq = http.get(f"{GATEWAY}/api/snapshots/{sid4}")
        okd = okd and rq.status_code == 404
        # Retry after churn: the failure reason must remain stable.
        rd2 = http.post(f"{GATEWAY}/api/snapshots", json={
            "snapshotId": sid4, "deviceId": "smoke-d",
            "startAddr": 0, "count": 200,
        })
        okd = okd and rd2.status_code == 409
        report("continuous revision churn -> stable 409, no evidence",
               okd, f"status={rd.status_code}/{rd2.status_code}")
        ctrl_fault("none", n=0)


def _wait_healthy(http: httpx.Client) -> None:
    for url in (f"{GATEWAY}/healthz", f"{CONTROLLER}/healthz"):
        for _ in range(50):
            try:
                if http.get(url).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            import time
            time.sleep(0.3)
        else:  # pragma: no cover
            raise RuntimeError(f"service never became healthy: {url}")


def main() -> int:
    print(f"gateway={GATEWAY} controller={CONTROLLER}\n")
    stage_build()
    stage_unit_tests()
    stage_smoke()

    print()
    if failures:
        print(f"{FAIL} verification failed: {len(failures)} stage(s): "
              + ", ".join(failures))
        return 1
    print(f"{PASS} verification passed: build, unit tests and all smoke tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
