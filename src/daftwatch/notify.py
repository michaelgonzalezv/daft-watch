from __future__ import annotations

import html as _html
import smtplib
import ssl
from email.message import EmailMessage

from daftwatch.config import SmtpConfig
from daftwatch.models import Listing
from daftwatch.store import Event

_LABEL = {
    "NEW": "new", "PRICE_DROP": "price drop(s)", "PRICE_UP": "price rise(s)",
    "GONE": "gone", "BACK": "relisted",
}


_TABLE_STYLE = (
    "border-collapse:collapse;font-size:13px;"
    "font-family:Arial,Helvetica,sans-serif"
)
_TH = "padding:6px 10px;border:1px solid #ddd;background:#f3f3f3;color:#333"
_TD = "padding:6px 10px;border:1px solid #ddd"
_TD_NUM = _TD + ";text-align:right"
_COLUMNS = (
    ("Price", True), ("Rooms", True), ("Sharing", False), ("Prefs", False),
    ("Type", False), ("Area", False), ("km", True), ("Published", False),
    ("Link", False),
)


def _esc(value) -> str:
    return _html.escape("" if value is None else str(value))


def _digest_html(items: list[tuple[Event, Listing]], summary: str) -> str:
    head = (
        "<tr>"
        + "".join(
            f'<th style="{_TH};text-align:{"right" if num else "left"}">{name}</th>'
            for name, num in _COLUMNS
        )
        + "</tr>"
    )
    rows = []
    for _event, listing in items:
        price = f"{_esc(listing.currency)} {_esc(listing.price_native)}/mo"
        if listing.price_weekly:
            price += (
                f"<br><small>{_esc(listing.currency)} "
                f"{_esc(listing.price_weekly)}/wk</small>"
            )
        centre = listing.distances_km.get("centre")
        km = f"{centre:.1f}" if centre is not None else "—"
        rooms = (
            _esc(listing.rooms_available)
            if listing.rooms_available is not None else "—"
        )
        sharing = (
            _esc(listing.sharing_with)
            if listing.sharing_with is not None else "—"
        )
        prefs = _esc(listing.preferences) if listing.preferences else "—"
        ptype = _esc(listing.property_type) if listing.property_type else "—"
        area = _esc(listing.area or listing.city or "—")
        published = _esc(listing.first_published) if listing.first_published else "—"
        link = (
            f'<a href="{_esc(listing.url)}" title="{_esc(listing.title)}">view</a>'
        )
        cells = [
            (price, True), (rooms, True), (sharing, False), (prefs, False),
            (ptype, False), (area, False), (km, True), (published, False),
            (link, False),
        ]
        rows.append(
            "<tr>"
            + "".join(
                f'<td style="{_TD_NUM if num else _TD}">{value}</td>'
                for value, num in cells
            )
            + "</tr>"
        )
    return (
        f"<h2>daft-watch digest</h2><p>{_esc(summary)}</p>"
        f'<table style="{_TABLE_STYLE}">{head}{"".join(rows)}</table>'
    )


class EmailNotifier:
    def __init__(self, smtp: SmtpConfig, smtplib_module=smtplib):
        self._smtp = smtp
        self._smtplib = smtplib_module

    def _send(self, subject: str, body: str, html: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"] = self._smtp.sender
        msg["To"] = self._smtp.recipient
        msg["Subject"] = subject
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
        server = self._smtplib.SMTP(self._smtp.host, self._smtp.port, timeout=30)
        try:
            server.starttls(context=ssl.create_default_context())
            server.login(self._smtp.user, self._smtp.password)
            server.send_message(msg)
        finally:
            server.quit()

    def send_alert(self, subject: str, body: str) -> None:
        self._send(f"[daft-watch] {subject}", body)

    def send_digest(self, items: list[tuple[Event, Listing]]) -> None:
        if not items:
            return
        counts: dict[str, int] = {}
        for event, _ in items:
            counts[event.type] = counts.get(event.type, 0) + 1
        parts = [
            f"{counts[t]} {_LABEL.get(t, t.lower())}"
            for t in ("NEW", "PRICE_DROP", "PRICE_UP", "GONE", "BACK")
            if t in counts
        ]
        subject = (
            f"[daft-watch] {len(items)} update(s): " + ", ".join(parts)
        )

        lines: list[str] = []
        for event, listing in items:
            lines.append(f"[{event.type}] {listing.title}")
            if event.type in ("PRICE_DROP", "PRICE_UP"):
                lines.append(
                    f"  price: {event.old_price} -> {event.new_price} EUR/month"
                )
            else:
                lines.append(f"  price: {listing.price_eur} EUR/month")
            beds = "?" if listing.beds is None else listing.beds
            lines.append(f"  {beds} bed | {listing.area or '-'}")
            lines.append(f"  {listing.url}")
            lines.append("")

        summary = f"{len(items)} update(s): " + ", ".join(parts)
        self._send(subject, "\n".join(lines), _digest_html(items, summary))
