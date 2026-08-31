import pytest

from hospes import states
from hospes.states import HospesStateError, advance


def test_spec_loads_and_has_all_states():
    names = states.all_states()
    assert "DISCOVERED" in names
    assert "PUBLISHED" in names
    assert "DO_NOT_CONTACT" in names
    # 26 main + 8 branch = 34 canonical states.
    assert len(names) == 34


def test_legal_linear_advance():
    opp = {"state": "DISCOVERED"}
    advance(opp, "RESEARCHING")
    assert opp["state"] == "RESEARCHING"
    advance(opp, "QUALIFIED")
    assert opp["state"] == "QUALIFIED"


def test_illegal_transition_raises_and_leaves_state():
    opp = {"state": "DISCOVERED"}
    with pytest.raises(HospesStateError):
        advance(opp, "PUBLISHED")
    assert opp["state"] == "DISCOVERED"  # untouched


def test_advance_via_event_name():
    opp = {"state": "DISCOVERED"}
    advance(opp, "appearance.researching")
    assert opp["state"] == "RESEARCHING"


def test_event_resolves_to_state():
    assert states.resolve_target("outreach.sent") == "OUTREACH_SENT"
    assert states.resolve_target("RESEARCHING") == "RESEARCHING"


def test_unknown_event_raises():
    with pytest.raises(HospesStateError):
        states.resolve_target("not.a.real.event")


def test_do_not_contact_is_terminal():
    assert states.legal_next_states("DO_NOT_CONTACT") == []
    opp = {"state": "DO_NOT_CONTACT"}
    with pytest.raises(HospesStateError):
        advance(opp, "RESEARCHING")


def test_can_advance_helper():
    assert states.can_advance("APPROVED", "CONTACT_ROUTE_IDENTIFIED") is True
    assert states.can_advance("APPROVED", "PUBLISHED") is False


def test_needs_human_can_recover_to_many_states():
    opp = {"state": "NEEDS_HUMAN"}
    advance(opp, "RESEARCHING")
    assert opp["state"] == "RESEARCHING"


def test_missing_state_field_raises():
    with pytest.raises(HospesStateError):
        advance({}, "RESEARCHING")
