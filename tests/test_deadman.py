"""Dead man's switch. Paper mode never calls the exchange."""
import asyncio

from bot.config import Config
from bot.deadman import DeadMansSwitch
from bot.rest import RestError


class _Rest:
    def __init__(self, results):
        self.results = list(results)
        self.arms = 0
        self.disarms = 0

    async def arm_deadman(self, timeout_ms):
        self.arms += 1
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def disarm_deadman(self):
        self.disarms += 1
        if self.results and isinstance(self.results[0], Exception):
            raise self.results.pop(0)
        return {"data": {}}


def _cfg(tmp_path, paper):
    return Config(paper=paper, log_dir=str(tmp_path), deadman_rearm_seconds=0,
                  deadman_timeout_ms=15000, access_key="ak", secret_key="sk")


def test_paper_mode_does_not_arm_or_disarm(tmp_path):
    rest = _Rest([])
    dms = DeadMansSwitch(_cfg(tmp_path, paper=True), rest)
    dms.armed = True

    async def go():
        await dms.run()
        await dms.disarm()

    asyncio.run(go())
    assert rest.arms == 0 and rest.disarms == 0
    assert dms.armed is True


def test_live_failure_then_success_resets_the_failure_count(tmp_path):
    rest = _Rest([RestError("down"), {"data": {"trigger_time": "t1"}}])
    dms = DeadMansSwitch(_cfg(tmp_path, paper=False), rest)

    async def go():
        original = dms.rest.arm_deadman

        async def arm(timeout_ms):
            result = await original(timeout_ms)
            if rest.arms >= 2:
                dms.stop()
            return result

        dms.rest.arm_deadman = arm
        await dms.run()

    asyncio.run(go())
    assert dms.armed is True
    assert dms.failures == 0
    assert dms.last_trigger_time == "t1"


def test_disarm_clears_the_flag_and_keeps_it_when_the_call_fails(tmp_path):
    rest = _Rest([])
    dms = DeadMansSwitch(_cfg(tmp_path, paper=False), rest)
    dms.armed = True
    asyncio.run(dms.disarm())
    assert dms.armed is False and rest.disarms == 1

    rest.results.append(RestError("no"))
    dms.armed = True
    asyncio.run(dms.disarm())
    assert dms.armed is True and rest.disarms == 2
