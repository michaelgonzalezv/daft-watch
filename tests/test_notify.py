from daftwatch.config import SmtpConfig
from daftwatch.models import Listing
from daftwatch.store import Event
from daftwatch.notify import EmailNotifier


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.tls = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None): self.tls = True; self.tls_context = context
    def login(self, u, p): self.logged_in = (u, p)
    def send_message(self, msg): self.sent.append(msg)
    def quit(self): self.quit_called = True


class FakeSmtplib:
    SMTP = FakeSMTP


def smtp_cfg():
    return SmtpConfig(host="h", port=587, user="u", password="pw",
                      sender="from@x.com", recipient="to@x.com")


def mk_listing(id, price):
    return Listing(id=id, category="rent", title=f"Flat {id}",
                   url=f"https://daft.ie/{id}", price_eur=price, beds=2, baths=1,
                   property_type="Apartment", area="D8", county="Dublin",
                   lat=None, lng=None, raw={})


def mk_share(id, price_native, **kw):
    fields = dict(
        id=id, category="sharing", title=f"Flat {id}",
        url=f"https://daft.ie/{id}", price_eur=price_native, beds=2, baths=1,
        property_type="Apartment", area="D8", county="Dublin",
        lat=None, lng=None, raw={}, currency="EUR", price_native=price_native,
        rooms_available=1, sharing_with=3, preferences="Female",
        first_published="2026-09-01", distances_km={"centre": 2.345},
    )
    fields.update(kw)
    return Listing(**fields)


def _html_part(msg):
    return next(
        p for p in msg.walk() if p.get_content_type() == "text/html"
    )


def _plain_part(msg):
    return next(
        p for p in msg.walk() if p.get_content_type() == "text/plain"
    )


def setup_function():
    FakeSMTP.instances.clear()


def test_send_digest_builds_one_email():
    items = [
        (Event(1, "1", "NEW", None, 2000), mk_listing("1", 2000)),
        (Event(2, "2", "PRICE_DROP", 2200, 2000), mk_listing("2", 2000)),
    ]
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest(items)
    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.tls and smtp.logged_in == ("u", "pw") and smtp.quit_called
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["To"] == "to@x.com"
    assert msg["From"] == "from@x.com"
    assert "2 update(s)" in msg["Subject"]
    assert "1 new" in msg["Subject"]
    assert "1 price drop(s)" in msg["Subject"]
    body = _plain_part(msg).get_content()
    assert "https://daft.ie/1" in body
    assert "2200" in body and "2000" in body


def test_send_digest_attaches_html_table():
    items = [
        (Event(1, "1", "NEW", None, 725), mk_share("1", 725)),
        (Event(2, "2", "PRICE_DROP", 900, 800),
         mk_share("2", 800, price_weekly=185)),
    ]
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest(items)
    msg = FakeSMTP.instances[0].sent[0]
    html_part = _html_part(msg)
    assert html_part.get_content_type() == "text/html"
    html = html_part.get_content()
    assert "<table" in html
    # 1 header row + 2 data rows
    assert html.count("<tr") == 3
    assert "EUR 725/mo" in html
    assert "https://daft.ie/1" in html and "https://daft.ie/2" in html
    assert "/wk" in html
    # plaintext still present and unchanged in shape
    plain = _plain_part(msg).get_content()
    assert "[NEW] Flat 1" in plain
    assert "  https://daft.ie/2" in plain


def test_digest_html_escapes_listing_text():
    items = [(Event(1, "1", "NEW", None, 700),
              mk_share("1", 700, title="A & B <flat>"))]
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest(items)
    html = _html_part(FakeSMTP.instances[0].sent[0]).get_content()
    assert "A &amp; B &lt;flat&gt;" in html
    assert "<flat>" not in html


def test_digest_html_dashes_for_none_values():
    items = [(Event(1, "1", "NEW", None, 700),
              mk_share("1", 700, sharing_with=None, preferences=None,
                       distances_km={}))]
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest(items)
    html = _html_part(FakeSMTP.instances[0].sent[0]).get_content()
    assert html.count("—") >= 3


def test_send_uses_verified_tls_context_and_timeout():
    import ssl
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_alert("x", "y")
    smtp = FakeSMTP.instances[0]
    assert smtp.timeout == 30
    assert isinstance(smtp.tls_context, ssl.SSLContext)
    assert smtp.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert smtp.tls_context.check_hostname is True


def test_smtp_password_not_in_repr():
    assert "pw" not in repr(smtp_cfg())


def test_send_digest_empty_is_noop():
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest([])
    assert FakeSMTP.instances == []


def test_send_alert_prefixes_subject():
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_alert("scraper broken", "traceback here")
    msg = FakeSMTP.instances[0].sent[0]
    assert msg["Subject"] == "[daft-watch] scraper broken"
    assert "traceback here" in msg.get_content()
