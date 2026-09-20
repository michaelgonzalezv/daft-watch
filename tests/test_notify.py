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


def _row_for(html, needle):
    """The <tr>...</tr> that contains *needle* (a listing url)."""
    return next(r for r in html.split("<tr")[1:] if needle in r)


def _digest_html(items):
    EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib).send_digest(items)
    return _html_part(FakeSMTP.instances[-1].sent[0]).get_content()  # the email just sent


def test_every_row_says_what_happened_to_it():
    # the report: subject says "1 price drop(s)" but no row could be told apart
    items = [
        (Event(1, "1", "NEW", None, 725), mk_share("1", 725)),
        (Event(2, "2", "PRICE_DROP", 780, 750), mk_share("2", 750)),
        (Event(3, "3", "PRICE_UP", 700, 730), mk_share("3", 730)),
        (Event(4, "4", "GONE", 700, None), mk_share("4", 700)),
        (Event(5, "5", "BACK", None, 690), mk_share("5", 690)),
    ]
    html = _digest_html(items)
    assert "<th" in html and ">Update</th>" in html
    assert "NEW" in _row_for(html, "daft.ie/1")
    assert "PRICE &#9660;" in _row_for(html, "daft.ie/2")
    assert "PRICE &#9650;" in _row_for(html, "daft.ie/3")
    assert "GONE" in _row_for(html, "daft.ie/4")
    assert "RELISTED" in _row_for(html, "daft.ie/5")
    # and they don't leak into each other's rows
    assert "PRICE" not in _row_for(html, "daft.ie/1")
    assert "NEW" not in _row_for(html, "daft.ie/2")


def test_a_price_change_shows_the_old_price_and_direction():
    html = _digest_html([
        (Event(1, "1", "PRICE_DROP", 780, 750), mk_share("1", 750)),
        (Event(2, "2", "PRICE_UP", 700, 730), mk_share("2", 730)),
    ])
    drop, up = _row_for(html, "daft.ie/1"), _row_for(html, "daft.ie/2")
    assert "EUR 750/mo" in drop and "was EUR 780" in drop and "&#9660;" in drop
    assert "EUR 730/mo" in up and "was EUR 700" in up and "&#9650;" in up
    # a plain NEW has no 'was' line
    assert "was" not in _digest_html([(Event(3, "3", "NEW", None, 700), mk_share("3", 700))])


def test_a_gone_listing_is_greyed_out():
    html = _digest_html([
        (Event(1, "1", "GONE", 700, None), mk_share("1", 700)),
        (Event(2, "2", "NEW", None, 700), mk_share("2", 700)),
    ])
    assert 'color:#888' in _row_for(html, "daft.ie/1")
    assert 'color:#888' not in _row_for(html, "daft.ie/2")


def test_an_unknown_event_type_still_gets_a_badge():
    html = _digest_html([(Event(1, "1", "WEIRD", None, 700), mk_share("1", 700))])
    assert "WEIRD" in html


def test_watchlist_rows_are_labelled_too():
    n = EmailNotifier(smtp_cfg(), smtplib_module=FakeSmtplib)
    n.send_digest(
        [(Event(1, "1", "NEW", None, 700), mk_share("1", 700))],
        watchlist_items=[(Event(2, "2", "GONE", 737, None), mk_share("2", 737))],
    )
    html = _html_part(FakeSMTP.instances[0].sent[0]).get_content()
    assert "GONE" in _row_for(html, "daft.ie/2") and "NEW" in _row_for(html, "daft.ie/1")
