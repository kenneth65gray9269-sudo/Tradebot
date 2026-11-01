"""WebSocket feeds: public market data and the private account/order stream.

Both follow the documented heartbeat rules:
  * market  wss://api.sunx.io/ws/v1/market        server sends {"ping": n} -> reply {"pong": n}
  * private wss://api.sunx.io/ws/v1/notification  server sends {"op":"ping","ts":...} ->
            reply {"op":"pong","ts": <same ts>}
A connection that stops answering is closed by the server, so the reply is mandatory.
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import random
import time

import websockets

from .config import WS_MARKET, WS_NOTIFICATION, Config
from .signing import build_signed_params, utc_timestamp

log = logging.getLogger("bot.feed")


def _decode(raw) -> dict | None:
    if isinstance(raw, (bytes, bytearray)):
        with contextlib.suppress(Exception):
            raw = gzip.decompress(raw)
        raw = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    try:
        return json.loads(raw)
    except Exception:
        log.warning("undecodable frame: %r", str(raw)[:120])
        return None


class _BaseFeed:
    url = ""
    name = "feed"

    def __init__(self, on_message):
        self.on_message = on_message
        self.connected = asyncio.Event()
        self.last_message_at = 0.0
        self.reconnects = 0
        self._ws = None
        self._stop = asyncio.Event()

    async def _subscribe(self, ws):          # overridden
        raise NotImplementedError

    async def _handle_heartbeat(self, ws, msg) -> bool:
        raise NotImplementedError

    async def send(self, obj: dict) -> None:
        if self._ws is None:
            raise RuntimeError(f"{self.name}: not connected")
        await self._ws.send(json.dumps(obj))

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, open_timeout=20, ping_interval=None,
                                              close_timeout=5, max_queue=2048) as ws:
                    self._ws = ws
                    self.connected.set()
                    backoff = 1.0
                    log.info("%s connected", self.name)
                    await self._subscribe(ws)
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        self.last_message_at = time.time()
                        msg = _decode(raw)
                        if msg is None:
                            continue
                        if await self._handle_heartbeat(ws, msg):
                            continue
                        await self.on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected.clear()
                self._ws = None
                self.reconnects += 1
                log.warning("%s disconnected (%s: %s); reconnecting in %.1fs",
                            self.name, type(exc).__name__, exc, backoff)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 30.0)
        self.connected.clear()


class MarketFeed(_BaseFeed):
    """Public channels. Needs no keys."""
    url = WS_MARKET
    name = "market"

    def __init__(self, contract_code: str, on_message, channels=("bbo", "trade.detail")):
        super().__init__(on_message)
        self.contract_code = contract_code
        self.channels = channels

    async def _subscribe(self, ws) -> None:
        for i, ch in enumerate(self.channels):
            await ws.send(json.dumps({"sub": f"market.{self.contract_code}.{ch}", "id": f"s{i}"}))

    async def _handle_heartbeat(self, ws, msg) -> bool:
        if "ping" in msg:
            await ws.send(json.dumps({"pong": msg["ping"]}))
            return True
        return False


class PrivateFeed(_BaseFeed):
    """Account / order / position stream. Signed with the documented WebSocket auth op.

    The docs sign the path of the WebSocket endpoint (for example '/ws/v1') and put the
    auth fields in JSON without URL encoding.
    """
    url = WS_NOTIFICATION
    name = "private"

    def __init__(self, cfg: Config, on_message, topics=("orders", "positions", "accounts")):
        super().__init__(on_message)
        self.cfg = cfg
        self.topics = topics
        self.authenticated = asyncio.Event()
        self.ws_sign_path = "/ws/v1"

    def _auth_frame(self) -> dict:
        params = build_signed_params(
            method="GET", host="api.sunx.io", sign_path=self.ws_sign_path,
            access_key=self.cfg.access_key, secret_key=self.cfg.secret_key,
            signature_method=self.cfg.signature_method, business_params=None,
            timestamp=utc_timestamp(),
        )
        return {"op": "auth", "type": "api",
                "AccessKeyId": params["AccessKeyId"],
                "SignatureMethod": params["SignatureMethod"],
                "SignatureVersion": params["SignatureVersion"],
                "Timestamp": params["Timestamp"],
                "Signature": params["Signature"]}

    async def _subscribe(self, ws) -> None:
        self.authenticated.clear()
        await ws.send(json.dumps(self._auth_frame()))

    async def _send_topic_subs(self, ws) -> None:
        for i, topic in enumerate(self.topics):
            await ws.send(json.dumps({"op": "sub", "cid": f"c{i}", "topic": topic,
                                      "contract_code": self.cfg.contract_code}))

    async def _handle_heartbeat(self, ws, msg) -> bool:
        op = msg.get("op")
        if op == "ping":
            await ws.send(json.dumps({"op": "pong", "ts": msg.get("ts")}))
            return True
        if op == "auth":
            code = msg.get("err-code", 0)
            # Missing err-code means success. JSON null does not: str(None) == "None".
            if code is not None and str(code) in ("0", ""):
                self.authenticated.set()
                log.info("private feed authenticated: %s", msg.get("data"))
                await self._send_topic_subs(ws)
            else:
                log.error("private feed auth failed: %s", msg)
            return True
        if op == "close":
            log.error("server closed the private feed: %s", msg)
            return True
        if op == "error":
            log.error("private feed error frame: %s", msg)
            return True
        return False
