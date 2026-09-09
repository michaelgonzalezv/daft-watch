from daftwatch.config import SmtpConfig
from daftwatch.models import Listing
from daftwatch.store import Event
from daftwatch.notify import EmailNotifier


class FakeSMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.tls = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self): self.tls = True
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
    msg = smtp.sent[0]
    assert msg["To"] == "to@x.com"
    assert msg["From"] == "from@x.com"
    assert "2 update(s)" in msg["Subject"]
    assert "1 new" in msg["Subject"]
    assert "1 price drop(s)" in msg["Subject"]
    body = msg.get_content()
    assert "https://daft.ie/1" in body
    assert "2200" in body and "2000" in body


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
