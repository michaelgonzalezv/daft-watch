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


def test_load_config_with_publish_and_email_distance(tmp_path):
    """Config with full publish and email.distance_km blocks."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        searches:
          - name: "Cork sharing"
            category: sharing
            params: { location: [cork-city], max_price: 800 }
        detail_price_cap: 750
        publish:
          json_path: "D:/Github/caleta-web/tools/rentals/listings.json"
          repo_dir: "D:/Github/caleta-web"
          file_rel: "tools/rentals/listings.json"
          git_push: true
        email:
          distance_km:
            dublin: 6
            cork: 4
    """))
    cfg = load_config(p)
    assert cfg.detail_price_cap == 750
    assert cfg.detail_max_per_cycle == 60  # default
    assert cfg.publish is not None
    assert cfg.publish.json_path == "D:/Github/caleta-web/tools/rentals/listings.json"
    assert cfg.publish.repo_dir == "D:/Github/caleta-web"
    assert cfg.publish.file_rel == "tools/rentals/listings.json"
    assert cfg.publish.git_push is True
    assert cfg.email_distance_km == {"dublin": 6.0, "cork": 4.0}


def test_load_config_without_publish_email_detail(tmp_path):
    """Config without publish, email, or detail keys uses defaults."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        searches:
          - name: "Dublin rent"
            category: rent
            params: { location: [dublin-city] }
    """))
    cfg = load_config(p)
    assert cfg.publish is None
    assert cfg.email_distance_km == {}
    assert cfg.detail_price_cap == 800
    assert cfg.detail_max_per_cycle == 60


def test_load_config_publish_git_push_false(tmp_path):
    """publish.git_push can be explicitly set to false."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        searches: []
        publish:
          json_path: "path.json"
          repo_dir: "dir"
          file_rel: "file.json"
          git_push: false
    """))
    cfg = load_config(p)
    assert cfg.publish is not None
    assert cfg.publish.git_push is False


def test_load_config_publish_missing_repo_dir(tmp_path):
    """publish block missing repo_dir raises ValueError."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        searches: []
        publish:
          json_path: "path.json"
          file_rel: "file.json"
    """))
    with pytest.raises(ValueError, match="repo_dir"):
        load_config(p)


def test_load_config_publish_missing_json_path(tmp_path):
    """publish block missing json_path raises ValueError."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        searches: []
        publish:
          repo_dir: "dir"
          file_rel: "file.json"
    """))
    with pytest.raises(ValueError, match="json_path"):
        load_config(p)


def test_load_config_publish_missing_file_rel(tmp_path):
    """publish block missing file_rel raises ValueError."""
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        searches: []
        publish:
          json_path: "path.json"
          repo_dir: "dir"
    """))
    with pytest.raises(ValueError, match="file_rel"):
        load_config(p)
