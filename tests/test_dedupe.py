from app.dedupe import decide
from app.filters import FilterRules
from app.models import Alert


RULES = FilterRules(include_exact=["Severe Thunderstorm Warning"], include_suffix=[], exclude_exact=[])


def alert(identifier="one", headline="Severe Thunderstorm Warning for Illawarra", references=None, message_type="Alert"):
    return Alert(
        alert_id=identifier,
        event="Severe Thunderstorm Warning",
        headline=headline,
        area_desc="Illawarra",
        effective="2026-09-28T08:00:00+00:00",
        expires="2026-09-28T10:00:00+00:00",
        message_type=message_type,
        ends="2026-09-28T10:00:00+00:00",
        onset="2026-09-28T08:00:00+00:00",
        references=references or [],
    )


class FakeState:
    def __init__(self):
        self.rows = {}

    def lookup(self, alert_id):
        return self.rows.get(alert_id)

    def record(self, item, disposition="sent"):
        self.rows[item.alert_id] = {
            "disposition": disposition,
            "msg_hash": item.content_hash(),
            "headline": item.headline,
            "expires": item.expires,
        }


def test_new_alert_is_sent():
    assert decide(alert(), RULES, FakeState().lookup).disposition == "sent"


def test_same_alert_is_duplicate():
    state = FakeState()
    item = alert()
    state.record(item)
    result = decide(item, RULES, state.lookup)
    assert result.disposition == "duplicate"
    assert not result.transmit


def test_referenced_update_is_sent():
    state = FakeState()
    original = alert("one")
    state.record(original)
    update = alert("two", headline="Severe Thunderstorm Warning updated for Illawarra", references=["one"])
    result = decide(update, RULES, state.lookup)
    assert result.disposition == "update"
    assert result.transmit


def test_cancel_of_sent_alert_is_broadcast():
    state = FakeState()
    original = alert("one")
    state.record(original)
    cancel = alert("two", references=["one"], message_type="Cancel")
    result = decide(cancel, RULES, state.lookup)
    assert result.disposition == "cancelled"
    assert result.transmit
