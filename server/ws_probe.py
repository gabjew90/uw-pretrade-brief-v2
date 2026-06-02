"""TEMPORARY WebSocket connectivity probe for the UW socket — server-side.

Runs ON Railway (where UW_API_KEY + good network already are) so a phone-only
operator can answer the feasibility/protocol questions the public docs don't:
  1. Does the Basic key authenticate to wss://api.unusualwhales.com/socket?
     (handshake 101 = in; 401 = auth shape; 403 = tier blocked; 404 = path)
  2. What auth + subscribe(join) frame actually works?
  3. Payload shape + message RATE (the firehose risk).

Read-only, time-boxed, key never returned. Reached via the token-guarded
GET /admin/ws-probe. DELETE this module + the route once we've read the findings
(it's a spike, not a feature). websockets is already an image dep (via uvicorn).
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from urllib.parse import urlencode

import websockets  # already in the image (uvicorn/google-genai pull it)

BASE_HOST = "api.unusualwhales.com"


def _redact(s: str, key: str) -> str:
    return s.replace(key, "<KEY>") if key and isinstance(s, str) else s


def _truncate(raw, key: str, limit: int = 600) -> str:
    text = raw if isinstance(raw, str) else f"<binary {len(raw)}B>"
    try:
        text = json.dumps(json.loads(text))
    except Exception:
        pass
    return _redact(text[:limit], key)


async def _attempt(label: str, url: str, headers: dict, key: str,
                   join_frames: list[str], seconds: float, max_msgs: int) -> dict:
    out: dict = {"label": label, "url": _redact(url, key),
                 "handshake": "fail", "status": None, "error": None,
                 "server_first": None, "sent": [_redact(j, key) for j in join_frames],
                 "messages": [], "msg_count": 0, "rate_per_s": 0.0, "duration_s": 0.0}
    try:
        kwargs = {"open_timeout": 8, "ping_interval": None, "max_size": 2 ** 22}
        if headers:
            kwargs["additional_headers"] = headers
        async with websockets.connect(url, **kwargs) as ws:
            out["handshake"] = "ok"
            out["status"] = 101
            try:
                hello = await asyncio.wait_for(ws.recv(), timeout=2)
                out["server_first"] = _truncate(hello, key)
            except asyncio.TimeoutError:
                out["server_first"] = "(none in 2s)"
            for jf in join_frames:
                await ws.send(jf)

            async def _hb():
                ref = 1000
                while True:
                    await asyncio.sleep(25)
                    ref += 1
                    try:
                        await ws.send(json.dumps([None, str(ref), "phoenix", "heartbeat", {}]))
                    except Exception:
                        return
            hb = asyncio.create_task(_hb())
            started = time.monotonic()
            while time.monotonic() - started < seconds and out["msg_count"] < max_msgs:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=seconds)
                except asyncio.TimeoutError:
                    break
                except Exception as e:
                    out["error"] = f"recv ended: {type(e).__name__}: {_redact(str(e), key)}"
                    break
                out["msg_count"] += 1
                if len(out["messages"]) < 25:        # cap echoed payloads
                    out["messages"].append(_truncate(raw, key))
            dur = time.monotonic() - started
            out["duration_s"] = round(dur, 2)
            out["rate_per_s"] = round(out["msg_count"] / max(0.001, dur), 1)
            hb.cancel()
    except Exception as e:
        status = (getattr(e, "status_code", None)
                  or getattr(getattr(e, "response", None), "status_code", None))
        out["status"] = status
        out["error"] = f"{type(e).__name__}: {_redact(str(e), key)}"
    return out


async def probe(channel: str = "flow_alerts", seconds: float = 12.0,
                max_msgs: int = 60) -> dict:
    """Try a few connect/auth strategies; return a JSON-able findings dict.
    Stops at the first strategy that handshakes AND yields >=1 message."""
    key = os.environ.get("UW_API_KEY", "")
    if not key:
        return {"error": "UW_API_KEY not set in env"}
    seconds = max(2.0, min(float(seconds), 20.0))   # hard cap

    phx_join = json.dumps(["1", "1", channel, "phx_join", {}])
    alt_a = json.dumps({"channel": channel, "msg_type": "join"})
    alt_b = json.dumps({"action": "subscribe", "channel": channel})

    qp = urlencode({"token": key, "vsn": "2.0.0"})
    strategies = [
        ("Phoenix: token query param @ /socket/websocket",
         f"wss://{BASE_HOST}/socket/websocket?{qp}", {}, [phx_join]),
        ("Phoenix: bare /socket + token query param",
         f"wss://{BASE_HOST}/socket?{urlencode({'token': key})}", {}, [phx_join]),
        ("Bearer header @ /socket/websocket",
         f"wss://{BASE_HOST}/socket/websocket?vsn=2.0.0",
         {"Authorization": f"Bearer {key}"}, [phx_join, alt_a, alt_b]),
    ]

    attempts = []
    for label, url, headers, joins in strategies:
        res = await _attempt(label, url, headers, key, joins, seconds, max_msgs)
        attempts.append(res)
        if res["handshake"] == "ok" and res["msg_count"] > 0:
            break

    any_ok = any(a["handshake"] == "ok" for a in attempts)
    statuses = [a["status"] for a in attempts if a["handshake"] == "fail"]
    summary = ("connected — see the strategy with handshake=ok for the working auth"
               if any_ok else
               f"all handshakes failed; statuses={statuses} "
               "(403=tier blocked · 401=auth shape · 404=path)")
    return {"channel": channel, "seconds": seconds, "summary": summary,
            "attempts": attempts}
