import textwrap
import pytest
from daftwatch.config import load_config, SmtpConfig


def test_load_config_parses_searches_and_notify(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        interval_minutes: 15
        gone_after_cycles: 3
        rate_limit_seconds: 2
        searches:
          - name: "Dublin rent"
            category: rent
            params: { location: [dublin-city], max_price: 2200 }
          - name: "Sharing"
            category: sharing
            params: { location: [dublin-8-dublin] }
        filters:
          keywords_exclude: [student]
        notify:
          min_event_types: [NEW, PRICE_DROP]
    """))
    cfg = load_config(p)
    assert cfg.interval_minutes == 15
    assert cfg.gone_after_cycles == 3
    assert [s.name for s in cfg.searches] == ["Dublin rent", "Sharing"]
    assert cfg.searches[0].category == "rent"
    assert cfg.searches[0].params["max_price"] == 2200
    assert cfg.filters["keywords_exclude"] == ["student"]
    assert cfg.notify.min_event_types == ["NEW", "PRICE_DROP"]


def test_load_config_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("searches: []\n")
    cfg = load_config(p)
    assert cfg.interval_minutes == 30
    assert cfg.gone_after_cycles == 2
    assert cfg.rate_limit_seconds == 2
    assert cfg.filters == {}
    assert cfg.notify.min_event_types == ["NEW", "PRICE_DROP", "GONE"]


def test_load_config_rejects_unknown_category(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("searches:\n  - name: x\n    category: sale\n    params: {}\n")
    with pytest.raises(ValueError, match="category"):
        load_config(p)


def test_smtp_config_from_env_missing_var():
    with pytest.raises(KeyError):
        SmtpConfig.from_env({"SMTP_HOST": "h"})


def test_smtp_config_from_env_ok():
    env = {
        "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
        "SMTP_USER": "u", "SMTP_PASS": "p",
        "ALERT_FROM": "from@example.com", "ALERT_TO": "to@example.com",
    }
    s = SmtpConfig.from_env(env)
    assert s.host == "smtp.example.com"
    assert s.port == 587
    assert s.recipient == "to@example.com"
