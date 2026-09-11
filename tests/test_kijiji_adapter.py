import json

import pytest
from daftwatch.adapter import AdapterError, RateLimited
from daftwatch.config import Search
from daftwatch.kijiji_adapter import (
    KijijiListingsAdapter,
    _classify_gender_pref,
    _classify_property_type,
    _detect_rate_period,
    _norm_phone_ca,
    parse_detail,
    to_listing,
)

# Trimmed, hand-built shapes matching what a real fetch of
# kijiji.ca/b-room-rental-roommate/canada/c36l<id> returns (verified by hand
# against the live site while building this adapter): search results and
# detail pages both embed a __NEXT_DATA__ Apollo cache, keyed "Type:id".

_ROOM = {
    "__typename": "StandardListing",
    "id": "1714658868",
    "title": "High Park Female Furnished Private Room",
    "description": "FEMALE PRIVATE FURNISHED BEDROOM in female-only residence.",
    "categoryId": 36,
    "url": "https://www.kijiji.ca/v-room-rental-roommate/city-of-toronto/high-park/1714658868",
    "activationDate": "2025-04-08T05:04:00.000Z",
    "sortingDate": "2026-07-27T08:20:57.000Z",
    "location": {
        "name": "Toronto",
        "address": "Toronto, ON M6P 3K9",
        "coordinates": {"latitude": 43.65317, "longitude": -79.4681},
    },
    "price": {"type": "FIXED", "amount": 70000, "originalAmount": None},
    "posterInfo": {"posterId": "77964287", "phoneNumber": None},
}

_OFF_TOPIC = {  # a promoted ad from an unrelated category, same page
    "__typename": "StandardListing",
    "id": "1726355545",
    "title": "Dedicated Office for Rent",
    "categoryId": 40,
    "url": "https://www.kijiji.ca/v-commercial-office-space/x/1726355545",
    "location": {"name": "Vaughan", "coordinates": {"latitude": 43.8, "longitude": -79.5}},
    "price": {"amount": 71700},
}


def _html(apollo: dict) -> str:
    page = {"props": {"pageProps": {"__APOLLO_STATE__": apollo}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(page)}</script>'


def _apollo(*entries: dict) -> dict:
    return {f"StandardListing:{e['id']}": e for e in entries}


def test_to_listing_maps_fields():
    l = to_listing(_ROOM)
    assert l.id == "kj1714658868"
    assert l.source == "kijiji"
    assert l.currency == "CAD"
    assert l.country == "Canada"
    assert l.category == "sharing"
    assert l.property_type == "Room"
    assert l.price_native == 700  # 70000 cents
    assert l.price_eur == 700
    assert l.url == _ROOM["url"]
    assert l.lat == pytest.approx(43.65317)
    assert l.lng == pytest.approx(-79.4681)
    assert l.first_published == "2025-04-08"
    assert l.city is None  # resolved later by geo.city_of(search.name)


def test_to_listing_defensive_on_missing_fields():
    l = to_listing({"id": "1"})
    assert l.id == "kj1"
    assert l.price_native == 0
    assert l.lat is None and l.lng is None
    assert l.title == ""
    assert l.url == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4165551234", "+14165551234"),
        ("14165551234", "+14165551234"),
        ("(416) 555-1234", "+14165551234"),
        ("+1 416 555 1234", "+14165551234"),
        ("555-1234", None),          # too short
        (None, None),
        (12345, None),               # not a string
    ],
)
def test_norm_phone_ca(raw, expected):
    assert _norm_phone_ca(raw) == expected


def test_parse_detail_extracts_phone_and_description():
    entry = dict(_ROOM, posterInfo={"phoneNumber": "4165551234"})
    d = parse_detail(entry)
    assert d["description"] == _ROOM["description"]
    assert d["last_updated"] == "2026-07-27"
    assert d["agent_phone"] == "+14165551234"
    assert d["agent_name"] is None
    assert d["sharing_with"] is None  # not structured on kijiji, unlike daft


def test_parse_detail_no_phone():
    d = parse_detail(_ROOM)  # posterInfo.phoneNumber is None
    assert d["agent_phone"] is None


def _search(**params) -> Search:
    return Search(name="Toronto sharing", category="sharing", params=params, source="kijiji")


def test_fetch_filters_to_roommates_category_only():
    html = _html(_apollo(_ROOM, _OFF_TOPIC))
    a = KijijiListingsAdapter(fetch_html=lambda url: html, sleeper=lambda s: None)
    listings = a.fetch(_search(location_id="1700273"))
    assert [l.id for l in listings] == ["kj1714658868"]


def test_fetch_requires_location_id():
    a = KijijiListingsAdapter(fetch_html=lambda url: "", sleeper=lambda s: None)
    with pytest.raises(AdapterError):
        a.fetch(_search())


def test_fetch_uses_location_id_in_url():
    seen = {}

    def fake_fetch(url):
        seen["url"] = url
        return _html(_apollo(_ROOM))

    a = KijijiListingsAdapter(fetch_html=fake_fetch, sleeper=lambda s: None)
    a.fetch(_search(location_id="1700199"))
    assert "c36l1700199" in seen["url"]


def test_fetch_retries_on_rate_limited_then_succeeds():
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited("cloudflare-ish block")
        return _html(_apollo(_ROOM))

    sleeps = []
    a = KijijiListingsAdapter(fetch_html=flaky, sleeper=sleeps.append)
    listings = a.fetch(_search(location_id="1700273"))
    assert len(listings) == 1
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept between the two failed attempts


def test_fetch_raises_adapter_error_after_exhausting_backoff():
    def always_limited(url):
        raise RateLimited("still blocked")

    a = KijijiListingsAdapter(fetch_html=always_limited, sleeper=lambda s: None)
    with pytest.raises(AdapterError):
        a.fetch(_search(location_id="1700273"))


def test_detail_returns_matching_entity():
    html = _html(_apollo(_ROOM))
    a = KijijiListingsAdapter(fetch_html=lambda url: html, sleeper=lambda s: None)
    d = a.detail(_ROOM["url"])
    assert d["id"] == "1714658868"


def test_detail_returns_none_when_delisted():
    html = _html(_apollo(_ROOM))  # detail page no longer has the requested id
    a = KijijiListingsAdapter(fetch_html=lambda url: html, sleeper=lambda s: None)
    d = a.detail("https://www.kijiji.ca/v-room-rental-roommate/x/9999999999")
    assert d is None


@pytest.mark.parametrize(
    "title,description,expected",
    [
        ("3 bed house for rent", "", "House"),
        ("", "Spacious townhouse near transit", "House"),
        ("Room in a bungalow", "", "House"),
        ("Bright apartment room", "", "Apartment"),
        ("", "Condo unit, private bedroom", "Apartment"),
        ("Room in a flat downtown", "", "Apartment"),
        ("Basement room for rent", "", "Basement"),
        ("", "Newly renovated basement, female only", "Basement"),
        # a real title from the site: no house/apartment/basement keyword at all
        ("Room for Japanese Students – TTC Nearby", "", "Room"),
        ("", "", "Room"),
        # genuine conflict — both keywords present, don't guess which wins
        ("Apartment in a house, private room", "", "Room"),
    ],
)
def test_classify_property_type(title, description, expected):
    assert _classify_property_type(title, description) == expected


def test_to_listing_uses_classified_property_type():
    house = dict(_ROOM, title="Private room in a house, quiet street")
    assert to_listing(house).property_type == "House"

    apt = dict(_ROOM, title="Room in a condo downtown")
    assert to_listing(apt).property_type == "Apartment"

    no_signal = dict(_ROOM, title="Room for rent")
    assert to_listing(no_signal).property_type == "Room"


@pytest.mark.parametrize(
    "title,description,expected",
    [
        # unambiguous restriction, either word order
        ("Room for rent (Female Only)", "", "Female only"),
        ("", "Quiet female-only room", "Female only"),
        ("Basement room, only for female", "", "Female only"),
        ("Men only apartment", "", "Male only"),
        ("", "no females please", "Male only"),
        # a genuine ask, not incidental mention
        ("", "we are looking for a third female roommate", "Female preferred"),
        ("Room for rent", "looking for a male tenant", "Male preferred"),
        # real false positives caught while measuring against live listings —
        # must NOT be classified as a preference
        ("", "Send ALL answers) Name + male/female + age", None),  # asks the applicant's own gender
        ("", "kitchen shared with other males", None),             # describes current roommates, not a request
        ("Room for Japanese Students", "", None),                  # no gender mention at all
        ("", "", None),
    ],
)
def test_classify_gender_pref(title, description, expected):
    assert _classify_gender_pref(title, description) == expected


def test_to_listing_uses_classified_gender_pref():
    female_only = dict(_ROOM, title="Room for rent (Female Only)")
    assert to_listing(female_only).preferences == "Female only"

    no_signal = dict(_ROOM, title="Room for rent", description="Nice quiet street, close to transit.")
    assert to_listing(no_signal).preferences is None


@pytest.mark.parametrize(
    "title,description,price,expected",
    [
        # real cases found in the live data — both mention day AND week, and
        # in both, price_native turned out to hold the day/night figure
        ("Room Available until OCT 31. $50/day or $300/week", "", 50, "day"),
        ("Short Term -  Weekly ($450) or Nightly ($99) Stays", "", 99, "day"),
        # a pure weekly rate, no day/night mention at all — the case that
        # was being silently zeroed before this: $140/week is a real,
        # usable price, not "no price"
        ("Great room, $140/week, all bills included", "", 140, "week"),
        ("", "rent is $130 per week", 130, "week"),
        ("Room, Rooms for rent. Fully furnished, all is supplied. $35", "", 35, None),  # no period phrasing at all
        # price plausible as monthly -> don't reinterpret even with a day-rate mention
        ("High quality female's room", "short term rental $30-100/day, depending on the room", 700, None),
        # day/night phrasing but price already plausible as monthly
        ("Nightly cleaning included", "", 900, None),
        # implausibly low price but no period signal — leave alone (this is
        # what "contact for price" / a data error looks like, not something
        # this heuristic should guess about)
        ("Room for rent, cheap!", "", 50, None),
    ],
)
def test_detect_rate_period(title, description, price, expected):
    assert _detect_rate_period(title, description, price) == expected


def test_to_listing_zeroes_price_for_a_day_rate():
    daily = dict(_ROOM, title="Room Available until OCT 31. $50/day or $300/week",
                 price={"amount": 5000})  # 50.00 CAD — the day rate, not a real month's rent
    l = to_listing(daily)
    assert l.price_native == 0
    assert l.price_eur == 0
    assert l.price_weekly is None


def test_to_listing_converts_a_week_rate_to_monthly():
    weekly = dict(_ROOM, title="Great room, $140/week, all bills included",
                  price={"amount": 14000})  # 140.00 CAD
    l = to_listing(weekly)
    assert l.price_weekly == 140
    assert l.price_native == round(140 * 52 / 12)
    assert l.price_eur == round(140 * 52 / 12)
