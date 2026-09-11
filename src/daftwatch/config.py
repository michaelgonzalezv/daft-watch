from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

VALID_CATEGORIES = {"rent", "sharing"}
DEFAULT_MIN_EVENT_TYPES = ["NEW", "PRICE_DROP", "GONE"]


@dataclass
class Search:
    name: str
    category: str
    params: dict
    source: str = "daft"


@dataclass
class NotifyConfig:
    min_event_types: list[str] = field(
        default_factory=lambda: list(DEFAULT_MIN_EVENT_TYPES)
    )


@dataclass
class PublishConfig:
    json_path: str
    repo_dir: str
    file_rel: str
    git_push: bool = True
    events_path: str | None = None      # where events.json is written
    events_rel: str | None = None       # its path inside repo_dir
    history_path: str | None = None     # where history.json is written
    history_rel: str | None = None      # its path inside repo_dir
    compare_path: str | None = None     # where compare.json is written
    compare_rel: str | None = None      # its path inside repo_dir


@dataclass
class BackupConfig:
    sql_path: str
    repo_dir: str
    git_push: bool = True
    every_hours: int = 24


@dataclass
class Config:
    interval_minutes: int = 30
    gone_after_cycles: int = 2
    rate_limit_seconds: float = 2.0
    searches: list[Search] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    detail_price_cap: int = 800
    detail_max_per_cycle: int = 60
    publish: PublishConfig | None = None
    email_distance_km: dict[str, float] = field(default_factory=dict)
    email_max_price: int | None = None
    export_gone_within_days: int = 30   # keep off-market listings this long
    events_history_days: int = 90       # events.json window
    price_history_days: int = 400       # history.json window
    watchlist_api: str | None = None    # dashboard /api/favs URL (from env FAVS_API)
    watchlist_key: str | None = None    # its x-fav-key (from env FAVS_KEY)
    backup: "BackupConfig | None" = None


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    sender: str
    recipient: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SmtpConfig":
        return cls(
            host=env["SMTP_HOST"],
            port=int(env["SMTP_PORT"]),
            user=env["SMTP_USER"],
            password=env["SMTP_PASS"],
            sender=env["ALERT_FROM"],
            recipient=env["ALERT_TO"],
        )


def load_config(path: str | os.PathLike) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    searches: list[Search] = []
    for raw in data.get("searches", []):
        category = raw.get("category", "")
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"search {raw.get('name')!r} has category {category!r}; "
                f"must be one of {sorted(VALID_CATEGORIES)}"
            )
        searches.append(
            Search(
                name=raw["name"],
                category=category,
                params=raw.get("params", {}),
                source=raw.get("source", "daft"),
            )
        )

    notify_raw = data.get("notify", {}) or {}
    notify = NotifyConfig(
        min_event_types=notify_raw.get(
            "min_event_types", list(DEFAULT_MIN_EVENT_TYPES)
        )
    )

    # Parse publish block (optional)
    publish = None
    publish_raw = data.get("publish")
    if publish_raw is not None:
        required_keys = {"json_path", "repo_dir", "file_rel"}
        provided_keys = set(publish_raw.keys())
        missing_keys = required_keys - provided_keys
        if missing_keys:
            missing = ", ".join(sorted(missing_keys))
            raise ValueError(f"publish block missing required key(s): {missing}")
        publish = PublishConfig(
            json_path=publish_raw["json_path"],
            repo_dir=publish_raw["repo_dir"],
            file_rel=publish_raw["file_rel"],
            git_push=publish_raw.get("git_push", True),
            events_path=publish_raw.get("events_path"),
            events_rel=publish_raw.get("events_rel"),
            history_path=publish_raw.get("history_path"),
            history_rel=publish_raw.get("history_rel"),
            compare_path=publish_raw.get("compare_path"),
            compare_rel=publish_raw.get("compare_rel"),
        )

    backup = None
    backup_raw = data.get("backup")
    if backup_raw is not None:
        backup = BackupConfig(
            sql_path=backup_raw["sql_path"],
            repo_dir=backup_raw["repo_dir"],
            git_push=backup_raw.get("git_push", True),
            every_hours=int(backup_raw.get("every_hours", 24)),
        )

    # Parse email.distance_km / email.max_price (both optional)
    email_distance_km: dict[str, float] = {}
    email_raw = data.get("email") or {}
    distance_raw = email_raw.get("distance_km") or {}
    if distance_raw:
        email_distance_km = {k: float(v) for k, v in distance_raw.items()}
    email_max_price = email_raw.get("max_price")
    if email_max_price is not None:
        email_max_price = int(email_max_price)

    return Config(
        interval_minutes=data.get("interval_minutes", 30),
        gone_after_cycles=data.get("gone_after_cycles", 2),
        rate_limit_seconds=data.get("rate_limit_seconds", 2.0),
        searches=searches,
        filters=data.get("filters", {}) or {},
        notify=notify,
        detail_price_cap=data.get("detail_price_cap", 800),
        detail_max_per_cycle=data.get("detail_max_per_cycle", 60),
        publish=publish,
        email_distance_km=email_distance_km,
        email_max_price=email_max_price,
        export_gone_within_days=data.get("export_gone_within_days", 30),
        events_history_days=data.get("events_history_days", 90),
        price_history_days=data.get("price_history_days", 400),
        backup=backup,
    )
