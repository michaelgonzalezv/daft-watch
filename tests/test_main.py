import textwrap
from daftwatch.__main__ import main


def write_cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent("""
        interval_minutes: 1
        searches:
          - name: s1
            category: rent
            params: {}
    """))
    return p


ENV = {
    "SMTP_HOST": "h", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASS": "p",
    "ALERT_FROM": "f@x.com", "ALERT_TO": "t@x.com",
}


def test_run_subcommand_creates_db_and_exits_zero(tmp_path, monkeypatch):
    cfg = write_cfg(tmp_path)
    db = tmp_path / "data" / "daft.db"

    # stub the adapter so no network call happens
    import daftwatch.__main__ as m

    class FakeAdapter:
        def __init__(self, *a, **k): pass
        def fetch(self, search): return []
    monkeypatch.setattr(m, "DaftListingsAdapter", FakeAdapter)

    rc = main(["run", "--config", str(cfg), "--db", str(db)], env=ENV)
    assert rc == 0
    assert db.exists()


def test_missing_smtp_env_returns_nonzero(tmp_path):
    cfg = write_cfg(tmp_path)
    rc = main(["run", "--config", str(cfg), "--db", str(tmp_path / "d.db")],
              env={"SMTP_HOST": "h"})
    assert rc != 0
