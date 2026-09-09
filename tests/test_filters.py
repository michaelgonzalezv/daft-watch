from daftwatch.models import Listing
from daftwatch.filters import apply


def mk(id, title):
    return Listing(id=id, category="rent", title=title, url="u", price_eur=1000,
                   beds=1, baths=1, property_type=None, area=None, county=None,
                   lat=None, lng=None, raw={})


def test_empty_config_passes_everything():
    ls = [mk("1", "Nice flat"), mk("2", "Student digs")]
    assert apply(ls, {}) == ls


def test_keywords_exclude_case_insensitive():
    ls = [mk("1", "Nice flat"), mk("2", "STUDENT accommodation"),
          mk("3", "short term let")]
    out = apply(ls, {"keywords_exclude": ["student", "short term"]})
    assert [l.id for l in out] == ["1"]


def test_unknown_keys_ignored():
    ls = [mk("1", "flat")]
    assert apply(ls, {"nonsense": True}) == ls
