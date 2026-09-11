import subprocess
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


def write_publishing_cfg(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    # git_publish commits into this dir, so it has to be a real repo
    subprocess.run(["git", "init"], cwd=out, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=out,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=out,
                   check=True, capture_output=True)
    p = tmp_path / "config-publish.yaml"
    p.write_text(textwrap.dedent(f"""
        interval_minutes: 1
        searches:
          - name: s1
            category: rent
            params: {{}}
        publish:
          json_path: {(out / "listings.json").as_posix()}
          repo_dir: {out.as_posix()}
          file_rel: listings.json
          git_push: false
    """))
    return p, out


def test_republish_never_touches_the_adapter(tmp_path, monkeypatch):
    # the whole point of `republish`: no scrape, so the adapter must never
    # be asked to fetch anything — a fetch here would mean the fast path
    # silently fell back to a slow one.
    cfg, out = write_publishing_cfg(tmp_path)
    db = tmp_path / "data" / "daft.db"

    import daftwatch.__main__ as m

    class ExplodingAdapter:
        def __init__(self, *a, **k):
            pass

        def fetch(self, search):
            raise AssertionError("republish must not call adapter.fetch")
    monkeypatch.setattr(m, "DaftListingsAdapter", ExplodingAdapter)
    monkeypatch.setattr("daftwatch.runner.fetch_rates_usd", lambda c: {})

    rc = main(["republish", "--config", str(cfg), "--db", str(db)], env=ENV)
    assert rc == 0
    assert (out / "listings.json").exists()


def test_republish_without_a_publish_block_is_an_error_not_a_silent_noop(tmp_path):
    cfg = write_cfg(tmp_path)  # no publish: block
    rc = main(["republish", "--config", str(cfg),
               "--db", str(tmp_path / "d.db")], env=ENV)
    assert rc == 2


def test_missing_smtp_env_returns_nonzero(tmp_path):
    cfg = write_cfg(tmp_path)
    rc = main(["run", "--config", str(cfg), "--db", str(tmp_path / "d.db")],
              env={"SMTP_HOST": "h"})
    assert rc != 0


def test_missing_config_file_returns_two_no_traceback(tmp_path, capsys):
    rc = main(["run", "--config", str(tmp_path / "nope.yaml"),
               "--db", str(tmp_path / "d.db")], env=ENV)
    assert rc == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "not found" in err


def test_bad_category_returns_two(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("searches:\n  - name: x\n    category: sale\n    params: {}\n")
    rc = main(["run", "--config", str(p), "--db", str(tmp_path / "d.db")], env=ENV)
    assert rc == 2
