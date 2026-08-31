from hospes import routing


def _cand(name, city, window=""):
    return {"guest_name": name, "preferred_city": city, "date_window": window}


def test_la_routing():
    d = routing.route_to_studio(_cand("A", "Los Angeles", "Q3 2026"))
    assert d.studio == "LA"
    assert d.city == "Los Angeles"
    assert d.matched
    assert "Q3 2026" in d.rationale


def test_nyc_synonym():
    d = routing.route_to_studio(_cand("B", "Brooklyn"))
    assert d.studio == "NYC"


def test_austin_routing():
    d = routing.route_to_studio(_cand("C", "ATX"))
    assert d.studio == "AUSTIN"


def test_unknown_city_is_remote():
    d = routing.route_to_studio(_cand("D", "Portland"))
    assert d.studio == "REMOTE"
    assert not d.matched


def test_batching_groups_same_city():
    cands = [
        _cand("A", "Los Angeles"),
        _cand("B", "LA"),
        _cand("C", "Austin"),
    ]
    decisions = routing.route_all(cands)
    batches = routing.batching_suggestion(decisions)
    assert set(batches["LA"]) == {"A", "B"}
    assert batches["AUSTIN"] == ["C"]


def test_studios_registry_from_dna():
    reg = routing.studios_registry()
    # Flagship DNA declares LA / NYC / Austin.
    assert "LA" in reg and "NYC" in reg and "AUSTIN" in reg


def test_city_field_fallback_key():
    # Accept 'city' when 'preferred_city' absent.
    d = routing.route_to_studio({"guest_name": "E", "city": "New York City"})
    assert d.studio == "NYC"
