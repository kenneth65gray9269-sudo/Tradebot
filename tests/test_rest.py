"""REST signing rules and error handling, with a fake transport. No network."""
import asyncio

import httpx
import pytest

from bot.config import Config
from bot.rest import RestError, SunXRest
from bot.signing import build_signed_params


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _rest(tmp_path, handler, **kw):
    cfg = Config(paper=True, log_dir=str(tmp_path), access_key=kw.get("access_key", "ak"),
                 secret_key=kw.get("secret_key", "sk"), contract_code="JST-USDT")
    rest = SunXRest(cfg)
    rest._c = _client(handler)
    return rest


def test_public_endpoints_are_not_signed(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("contract_info"):
            return httpx.Response(200, json={"code": 200, "data": [{"contract_size": "10"}]})
        if request.url.path.endswith("risk/limit"):
            return httpx.Response(200, json={"code": 200, "data": [{"max_lever": 20}]})
        return httpx.Response(200, json={"code": 200, "ticks": [{"ask": [1, 1], "bid": [1, 1]}]})

    rest = _rest(tmp_path, handler, access_key="", secret_key="")

    async def go():
        info = await rest.contract_info("JST-USDT")
        rl = await rest.risk_limit("JST-USDT")
        bbo = await rest.bbo("JST-USDT")
        await rest.aclose()
        return info, rl, bbo

    info, rl, bbo = asyncio.run(go())
    assert info["contract_size"] == "10"
    assert rl["max_lever"] == 20
    assert bbo["ask"][0] == 1
    assert all("Signature=" not in url for url in seen)


def test_private_call_without_keys_fails_before_http(tmp_path):
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"code": 200, "data": {}})

    rest = _rest(tmp_path, handler, access_key="", secret_key="")

    async def go():
        with pytest.raises(RestError, match="API keys"):
            await rest.account_balance()
        await rest.aclose()

    asyncio.run(go())
    assert called["n"] == 0


def test_get_signs_business_params_and_post_does_not(tmp_path, monkeypatch):
    calls = []
    real = build_signed_params

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr("bot.rest.build_signed_params", spy)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "data": {}, "ticks": [{}]})

    rest = _rest(tmp_path, handler)

    async def go():
        await rest.positions("JST-USDT")
        await rest.place_order({"contract_code": "JST-USDT", "volume": "1", "side": "buy"})
        await rest.aclose()

    asyncio.run(go())
    assert calls[0]["method"] == "GET"
    assert calls[0]["business_params"]["contract_code"] == "JST-USDT"
    assert calls[1]["method"] == "POST"
    assert calls[1]["business_params"] is None


def test_error_payloads_become_rest_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "balance" in request.url.path:
            return httpx.Response(200, json={"code": 400, "message": "denied"})
        if "fee_rate" in request.url.path:
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json={"status": "error", "err-msg": "bad"})

    rest = _rest(tmp_path, handler)

    async def go():
        with pytest.raises(RestError, match="denied"):
            await rest.account_balance()
        with pytest.raises(RestError, match="non-JSON"):
            await rest.fee_rate("JST-USDT")
        with pytest.raises(RestError, match="bad"):
            await rest.open_orders("JST-USDT")
        with pytest.raises(RestError, match="No contract"):
            rest._c = _client(lambda r: httpx.Response(200, json={"code": 200, "data": []}))
            await rest.contract_info("NOPE-USDT")
        await rest.aclose()

    asyncio.run(go())
