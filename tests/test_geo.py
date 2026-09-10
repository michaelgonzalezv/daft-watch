import math
from daftwatch.geo import haversine_km, city_of, distance_to_centre, CENTRES


def test_haversine_zero():
    assert haversine_km(53.0, -6.0, 53.0, -6.0) == 0.0


def test_haversine_known():
    # O'Connell Bridge Dublin -> Grand Parade Cork ~ 219 km
    d = haversine_km(53.3473, -6.2591, 51.8979, -8.4706)
    assert 210 < d < 230


def test_city_of():
    assert city_of("dublin-city") == "dublin"
    assert city_of("Cork sharing <=800") == "cork"
    assert city_of("limerick-city-2") == "limerick"
    assert city_of("galway") is None


def test_distance_to_centre():
    d = distance_to_centre(51.9020, -8.4765, "cork")
    assert d is not None and d < 1.0
    assert distance_to_centre(None, -8.0, "cork") is None
    assert distance_to_centre(51.9, -8.4, "galway") is None
