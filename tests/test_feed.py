"""Heartbeat, auth, and frame decoding. The socket itself is fake."""
import asyncio
import gzip
import json
from unittest.mock import patch

from bot.config import Config
from bot.feed import MarketFeed, PrivateFeed, _decode


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def test_decode_gzip_text_and_garbage():
    assert _decode('{"ping": 1}') == {"ping": 1}
    raw = gzip.compress(json.dumps({"op": "ping", "ts": 5}).encode())
    assert _decode(raw) == {"op": "ping", "ts": 5}
    assert _decode(b"not-json{{") is None
    assert _decode("nope") is None


def test_market_pong_echoes_the_ping():
    feed = MarketFeed("JST-USDT", None)
    ws = _WS()

    async def go():
        assert await feed._handle_heartbeat(ws, {"ping": 42}) is True
        assert await feed._handle_heartbeat(ws, {"ch": "market.JST-USDT.bbo"}) is False

    asyncio.run(go())
    assert json.loads(ws.sent[0]) == {"pong": 42}


def test_private_auth_success_subscribes_and_null_is_not_success():
    cfg = Config(access_key="ak", secret_key="sk", paper=True, contract_code="JST-USDT",
                 log_dir="data")
    feed = PrivateFeed(cfg, None, topics=("orders", "positions"))
    ws = _WS()

    async def go():
        assert await feed._handle_heartbeat(ws, {"op": "ping", "ts": "9"}) is True
        assert await feed._handle_heartbeat(ws, {"op": "auth", "err-code": None}) is True
        assert not feed.authenticated.is_set()
        assert await feed._handle_heartbeat(ws, {"op": "auth", "err-code": 401}) is True
        assert not feed.authenticated.is_set()
        assert await feed._handle_heartbeat(ws, {"op": "auth", "err-code": 0}) is True
        assert feed.authenticated.is_set()
        assert await feed._handle_heartbeat(ws, {"op": "error", "err-code": 1}) is True
        assert await feed._handle_heartbeat(ws, {"topic": "orders"}) is False

    asyncio.run(go())
    bodies = [json.loads(item) for item in ws.sent]
    assert bodies[0] == {"op": "pong", "ts": "9"}
    assert [b["topic"] for b in bodies[1:]] == ["orders", "positions"]
    assert all(b["contract_code"] == "JST-USDT" for b in bodies[1:])


def test_auth_frame_signs_the_documented_ws_path():
    cfg = Config(access_key="ak", secret_key="sk", paper=True, contract_code="JST-USDT")
    feed = PrivateFeed(cfg, None)
    with patch("bot.feed.build_signed_params", wraps=__import__("bot.signing", fromlist=["build_signed_params"]).build_signed_params) as spy:
        frame = feed._auth_frame()
    assert spy.call_args.kwargs["sign_path"] == "/ws/v1"
    assert spy.call_args.kwargs["method"] == "GET"
    assert frame["op"] == "auth" and frame["type"] == "api"
    assert ":" in frame["Timestamp"] and "%3A" not in frame["Timestamp"]
    assert frame["Signature"]


def test_reconnect_counts_one_failure_then_stops():
    feed = MarketFeed("JST-USDT", None)

    def fail(*args, **kwargs):
        feed.stop()
        raise OSError("down")

    async def no_sleep(*args, **kwargs):
        return None

    async def go():
        with patch("bot.feed.websockets.connect", side_effect=fail), \
             patch("bot.feed.asyncio.sleep", no_sleep):
            await feed.run()

    asyncio.run(go())
    assert feed.reconnects == 1
    assert not feed.connected.is_set()
