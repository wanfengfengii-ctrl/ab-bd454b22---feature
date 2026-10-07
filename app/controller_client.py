"""Client for the buoy controller's paged register-read endpoint.

Protocol
--------
``GET {base}/read?deviceId=...&start=<addr>&count=<n>[&revision=<rev>]``

* The first page is read *without* ``revision``: the response carries
  the controller's current revision, which becomes the condition for
  every subsequent page.
* Later pages carry ``revision=<first-page revision>``. The controller
  replies ``409 revision_mismatch`` if its registers were updated since
  the first page.
* At most 64 registers may be requested per page.

Any other anomaly (timeout, missing/short page, echoed addresses that
do not match the request, non-u16 values, duplicate addresses) is a
hard :class:`ReadError`: the caller must discard the whole round and
restart from the first page.
"""
from __future__ import annotations

from typing import Any

import httpx

U16_MAX = 0xFFFF


class RevisionMismatch(Exception):
    def __init__(self, expected: int, current: int | None = None) -> None:
        super().__init__(f"controller revision changed: expected {expected}, got {current}")
        self.expected = expected
        self.current = current


class ReadError(Exception):
    """Timeout, transport failure, or any malformed/illegal page."""


class NonRetryableReadError(ReadError):
    """A definitive rejection (e.g. 400 bad range) retries cannot fix."""


def read_page(
    client: httpx.Client,
    base_url: str,
    device_id: str,
    start: int,
    count: int,
    revision: int | None = None,
) -> tuple[int, list[int]]:
    """Read one page; return ``(revision, values_in_address_order)``."""
    params: dict[str, Any] = {"deviceId": device_id, "start": start, "count": count}
    if revision is not None:
        params["revision"] = revision

    try:
        resp = client.get(f"{base_url}/read", params=params)
    except httpx.TimeoutException as exc:
        raise ReadError(f"controller timeout reading {start}+{count}") from exc
    except httpx.HTTPError as exc:
        raise ReadError(f"controller transport error: {exc}") from exc

    if resp.status_code == 409:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        current = body.get("current") if isinstance(body, dict) else None
        raise RevisionMismatch(revision if revision is not None else -1, current)

    if resp.status_code == 400:
        try:
            detail = resp.json().get("error", resp.text[:120])
        except ValueError:
            detail = resp.text[:120]
        raise NonRetryableReadError(f"bad register range: {detail}")

    if resp.status_code != 200:
        raise ReadError(f"controller HTTP {resp.status_code} reading {start}+{count}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise ReadError("controller returned non-JSON page") from exc

    if not isinstance(body, dict):
        raise ReadError("controller page is not an object")

    values = body.get("values")
    page_rev = body.get("revision")
    echo_start = body.get("start")
    echo_count = body.get("count")
    echo_device = body.get("deviceId")

    # Structural validation: the page must be exactly what was asked.
    if echo_device != device_id:
        raise ReadError("controller echoed mismatched deviceId")
    if echo_start != start or echo_count != count:
        raise ReadError("controller returned a missing or duplicated address range")
    if not isinstance(page_rev, int) or isinstance(page_rev, bool) or page_rev < 0:
        raise ReadError("controller page lacks a valid revision")
    if revision is not None and page_rev != revision:
        # Defence in depth: controller should have answered 409.
        raise RevisionMismatch(revision, page_rev)
    if not isinstance(values, list) or len(values) != count:
        raise ReadError("controller page is missing registers (short page)")

    clean: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ReadError("controller returned a non-integer register value")
        if not 0 <= value <= U16_MAX:
            raise ReadError("controller returned an out-of-range (non-u16) value")
        clean.append(value)
    return page_rev, clean
