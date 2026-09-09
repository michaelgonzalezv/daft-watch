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


@dataclass
class NotifyConfig:
    min_event_types: list[str] = field(
        default_factory=lambda: list(DEFAULT_MIN_EVENT_TYPES)
    )


@dataclass
class Config:
    interval_minutes: int = 30
    gone_after_cycles: int = 2
    rate_limit_seconds: float = 2.0
    searches: list[Search] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    notify: NotifyConfig = field(default_factory=NotifyConfig)


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
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
        category = raw["category"] if "category" in raw else raw.get("category", "")
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"search {raw.get('name')!r} has category {category!r}; "
                f"must be one of {sorted(VALID_CATEGORIES)}"
            )
        searches.append(
            Search(name=raw["name"], category=category, params=raw.get("params", {}))
        )

    notify_raw = data.get("notify", {}) or {}
    notify = NotifyConfig(
        min_event_types=notify_raw.get(
            "min_event_types", list(DEFAULT_MIN_EVENT_TYPES)
        )
    )

    return Config(
        interval_minutes=data.get("interval_minutes", 30),
        gone_after_cycles=data.get("gone_after_cycles", 2),
        rate_limit_seconds=data.get("rate_limit_seconds", 2.0),
        searches=searches,
        filters=data.get("filters", {}) or {},
        notify=notify,
    )
