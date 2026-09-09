from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from daftwatch.adapter import DaftListingsAdapter
from daftwatch.config import SmtpConfig, load_config
from daftwatch.notify import EmailNotifier
from daftwatch.runner import loop, run_cycle
from daftwatch.store import Store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daftwatch")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "loop"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", default="./config.yaml")
        sp.add_argument("--db", default="./data/daft.db")
        sp.add_argument("--heartbeat", default="./data/heartbeat")
    return p


def build(args, env: Mapping[str, str]):
    config = load_config(args.config)
    smtp = SmtpConfig.from_env(env)
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(db_path))
    adapter = DaftListingsAdapter(rate_limit_seconds=config.rate_limit_seconds)
    notifier = EmailNotifier(smtp)
    logger = logging.getLogger("daftwatch")
    return config, store, adapter, notifier, logger


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    env = os.environ if env is None else env
    args = _parser().parse_args(argv)

    try:
        config, store, adapter, notifier, logger = build(args, env)
    except KeyError as exc:
        print(f"missing required environment variable: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"configuration file not found: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "run":
            result = run_cycle(config, store, adapter, notifier, logger)
            print(
                f"cycle done: {result.events_sent} sent, "
                f"{len(result.searches_failed)} search(es) failed"
            )
            return 0
        loop(
            config, store, adapter, notifier, logger,
            heartbeat_path=args.heartbeat,
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
