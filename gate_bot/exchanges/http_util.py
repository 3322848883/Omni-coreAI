"""Shared HTTP helper for exchange adapters."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .base import ExchangeError


def http_json(
    method: str,
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    json_body: bool = False,
    timeout: int = 20,
    exchange: str = "",
) -> Any:
    params = dict(params or {})
    headers = dict(headers or {})
    method = method.upper()
    if method == "GET":
        qs = urllib.parse.urlencode(params)
        full = f"{url}?{qs}" if qs else url
        req = urllib.request.Request(full, headers=headers, method="GET")
    else:
        if json_body:
            data = json.dumps(params).encode()
            headers.setdefault("Content-Type", "application/json")
        else:
            data = urllib.parse.urlencode(params).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:300]
        raise ExchangeError(f"{exchange or url} {e.code}: {err}", status=e.code, exchange=exchange) from e
    except Exception as e:  # noqa: BLE001
        raise ExchangeError(f"{exchange or url} failed: {e}", exchange=exchange) from e
    try:
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {"raw": raw}
