"""Environment parsing and hard-limit validation."""
from bot.config import Config


def test_invalid_numbers_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("BOT_CAPITAL_USDT", "nope")
    monkeypatch.setenv("BOT_MAX_LEVERAGE", "x")
    monkeypatch.setenv("BOT_MAX_ORDERS_PER_MINUTE", "")
    cfg = Config()
    assert cfg.capital_usdt == 15.0
    assert cfg.max_leverage == 3
    assert cfg.max_orders_per_minute == 10


def test_fractional_int_env_truncates(monkeypatch):
    monkeypatch.setenv("BOT_MAX_LEVERAGE", "3.9")
    assert Config().max_leverage == 3


def test_live_flag_accepts_only_the_documented_truthy_values(monkeypatch):
    monkeypatch.delenv("BOT_LIVE", raising=False)
    assert Config().paper is True
    for value in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("BOT_LIVE", value)
        assert Config().paper is False, value
    for value in ("0", "false", "no", "off", "2", "live"):
        monkeypatch.setenv("BOT_LIVE", value)
        assert Config().paper is True, value


def test_config_validation_catches_bad_limits():
    c = Config(capital_usdt=15, max_leverage=3, max_notional_usdt=1000, paper=True)
    assert any("exceeds capital" in p for p in c.validate())
    c2 = Config(capital_usdt=15, max_daily_loss_usdt=20, paper=True)
    assert any("daily_loss" in p.lower() for p in c2.validate())
    c3 = Config(paper=False, access_key="", secret_key="")
    assert any("Live mode" in p for p in c3.validate())
    c4 = Config(capital_usdt=15, max_leverage=0, paper=True)
    assert any("BOT_MAX_LEVERAGE" in p for p in c4.validate())
    c5 = Config(capital_usdt=15, margin_mode="weird", paper=True)
    assert any("BOT_MARGIN_MODE" in p for p in c5.validate())
    exact = Config(capital_usdt=15, max_leverage=3, max_notional_usdt=45,
                   max_daily_loss_usdt=14.9, margin_mode="isolated", paper=True)
    assert exact.validate() == []
