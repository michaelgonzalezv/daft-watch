from __future__ import annotations

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


class EmailNotifier:
    def __init__(self, smtp: SmtpConfig, smtplib_module=smtplib):
        self._smtp = smtp
        self._smtplib = smtplib_module

    def _send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._smtp.sender
        msg["To"] = self._smtp.recipient
        msg["Subject"] = subject
        msg.set_content(body)
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
        self._send(subject, "\n".join(lines))
