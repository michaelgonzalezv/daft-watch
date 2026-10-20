from daftwatch.config import EmailConfig
from daftwatch.models import Listing
from daftwatch.store import Event
from daftwatch.notify import EmailNotifier


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Records every POST made by EmailNotifier, one dict per call."""
    calls = []

    @classmethod
    def post(cls, url, headers=None, json=None, timeout=None):
        cls.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse()


def email_cfg():
    return EmailConfig(api_key="re_123", sender="from@x.com", recipient="to@x.com")


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


def setup_function():
    FakeRequests.calls.clear()


def test_send_digest_builds_one_email():
    items = [
        (Event(1, "1", "NEW", None, 2000), mk_listing("1", 2000)),
        (Event(2, "2", "PRICE_DROP", 2200, 2000), mk_listing("2", 2000)),
    ]
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_digest(items)
    assert len(FakeRequests.calls) == 1
    call = FakeRequests.calls[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == "Bearer re_123"
    assert call["timeout"] == 30
    payload = call["json"]
    assert payload["to"] == ["to@x.com"]
    assert payload["from"] == "from@x.com"
    assert "2 update(s)" in payload["subject"]
    assert "1 new" in payload["subject"]
    assert "1 price drop(s)" in payload["subject"]
    body = payload["text"]
    assert "https://daft.ie/1" in body
    assert "2200" in body and "2000" in body


def test_send_digest_attaches_html_table():
    items = [
        (Event(1, "1", "NEW", None, 725), mk_share("1", 725)),
        (Event(2, "2", "PRICE_DROP", 900, 800),
         mk_share("2", 800, price_weekly=185)),
    ]
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_digest(items)
    html = FakeRequests.calls[0]["json"]["html"]
    assert "<table" in html
    # 1 header row + 2 data rows
    assert html.count("<tr") == 3
    assert "EUR 725/mo" in html
    assert "https://daft.ie/1" in html and "https://daft.ie/2" in html
    assert "/wk" in html
    # plaintext still present and unchanged in shape
    plain = FakeRequests.calls[0]["json"]["text"]
    assert "[NEW] Flat 1" in plain
    assert "  https://daft.ie/2" in plain


def test_digest_html_escapes_listing_text():
    items = [(Event(1, "1", "NEW", None, 700),
              mk_share("1", 700, title="A & B <flat>"))]
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_digest(items)
    html = FakeRequests.calls[0]["json"]["html"]
    assert "A &amp; B &lt;flat&gt;" in html
    assert "<flat>" not in html


def test_digest_html_dashes_for_none_values():
    items = [(Event(1, "1", "NEW", None, 700),
              mk_share("1", 700, sharing_with=None, preferences=None,
                       distances_km={}))]
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_digest(items)
    html = FakeRequests.calls[0]["json"]["html"]
    assert html.count("—") >= 3


def test_send_raises_on_http_error():
    import pytest

    class FailingRequests(FakeRequests):
        @classmethod
        def post(cls, url, headers=None, json=None, timeout=None):
            cls.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return FakeResponse(status_code=401)

    n = EmailNotifier(email_cfg(), requests_module=FailingRequests)
    with pytest.raises(RuntimeError):
        n.send_alert("x", "y")


def test_email_api_key_not_in_repr():
    assert "re_123" not in repr(email_cfg())


def test_send_digest_empty_is_noop():
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_digest([])
    assert FakeRequests.calls == []


def test_send_alert_prefixes_subject():
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_alert("scraper broken", "traceback here")
    payload = FakeRequests.calls[0]["json"]
    assert payload["subject"] == "[daft-watch] scraper broken"
    assert "traceback here" in payload["text"]


def _row_for(html, needle):
    """The <tr>...</tr> that contains *needle* (a listing url)."""
    return next(r for r in html.split("<tr")[1:] if needle in r)


def _digest_html(items):
    EmailNotifier(email_cfg(), requests_module=FakeRequests).send_digest(items)
    return FakeRequests.calls[-1]["json"]["html"]  # the email just sent


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
    n = EmailNotifier(email_cfg(), requests_module=FakeRequests)
    n.send_digest(
        [(Event(1, "1", "NEW", None, 700), mk_share("1", 700))],
        watchlist_items=[(Event(2, "2", "GONE", 737, None), mk_share("2", 737))],
    )
    html = FakeRequests.calls[0]["json"]["html"]
    assert "GONE" in _row_for(html, "daft.ie/2") and "NEW" in _row_for(html, "daft.ie/1")
