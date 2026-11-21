import threading

import pytest

from opencomputeruseagent import consent
from opencomputeruseagent.consent import ACT, LOOK, Grants, Refusal


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, answers=(), lock_free=True):
        self.answers = list(answers)
        self.cards = []
        self.lock_free = lock_free
        self.held = False
        self.ended = []
        self.clock = Clock()
        self.grants = Grants(self.ask, self.acquire, self.release, self.ended.append, clock=self.clock)
        self.grants.can_ask = True

    def ask(self, params, cancel):
        self.cards.append(params)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def acquire(self):
        if not self.lock_free:
            return False
        self.held = True
        return True

    def release(self):
        self.held = False


def accept(value):
    return {"action": "accept", "content": {"access": value}}


def test_one_card_then_no_more_for_the_chat():
    h = Harness([accept(consent.LOOK_AND_ACT)])
    assert h.grants.ensure("chat-1", LOOK) == ACT
    assert h.grants.ensure("chat-1", ACT) == ACT
    assert h.grants.ensure("chat-1", LOOK) == ACT
    assert len(h.cards) == 1 and h.held


def test_card_text_and_schema():
    h = Harness([accept(consent.LOOK_ONLY)])
    h.grants.ensure("chat-1", LOOK)
    card = h.cards[0]
    assert len(card["message"]) < 100
    access = card["requestedSchema"]["properties"]["access"]
    assert access["enum"] == [consent.DENY, consent.LOOK_ONLY, consent.LOOK_AND_ACT]
    assert access["default"] == consent.DENY
    assert "outside your institution" in access["description"]
    assert card["requestedSchema"]["required"] == ["access"]


def test_look_only_then_an_act_raises_the_upgrade_card():
    h = Harness([accept(consent.LOOK_ONLY), accept(consent.LOOK_AND_ACT)])
    assert h.grants.ensure("c", LOOK) == LOOK
    assert h.grants.ensure("c", ACT) == ACT
    upgrade = h.cards[1]
    assert upgrade["message"] == consent.UPGRADE_MESSAGE
    assert upgrade["requestedSchema"]["properties"]["access"]["enum"] == [consent.DENY, consent.LOOK_AND_ACT]


def test_refusing_the_upgrade_keeps_looking():
    h = Harness([accept(consent.LOOK_ONLY), accept(consent.DENY)])
    h.grants.ensure("c", LOOK)
    with pytest.raises(Refusal, match="only to look"):
        h.grants.ensure("c", ACT)
    assert h.grants.level("c") == LOOK and h.held


def test_an_act_first_answered_look_only_is_refused_but_looking_is_kept():
    h = Harness([accept(consent.LOOK_ONLY)])
    with pytest.raises(Refusal, match="only to look"):
        h.grants.ensure("c", ACT)
    assert h.grants.level("c") == LOOK


@pytest.mark.parametrize("answer", [
    {"action": "decline"},
    accept(consent.DENY),
    accept("rm -rf everything"),
    {"action": "accept", "content": {"access": consent.LOOK_AND_ACT, "extra": 1}},
    {"action": "accept", "content": {}},
    {"action": "accept"},
    {"action": "accept", "content": "Allow looking and acting"},
    "not a dict",
    RuntimeError("client error"),
])
def test_anything_but_a_well_formed_allow_refuses_and_frees_the_desktop(answer):
    h = Harness([answer])
    with pytest.raises(Refusal, match="did not allow"):
        h.grants.ensure("c", LOOK)
    assert h.grants.level("c") == 0 and not h.held and h.grants.owner is None


def test_an_upgrade_card_never_accepts_look_only_as_acting():
    h = Harness([accept(consent.LOOK_ONLY), accept(consent.LOOK_ONLY)])
    h.grants.ensure("c", LOOK)
    with pytest.raises(Refusal):
        h.grants.ensure("c", ACT)
    assert h.grants.level("c") == LOOK


def test_a_cancelled_card_ends_the_grant():
    h = Harness([accept(consent.LOOK_ONLY), {"action": "cancel"}])
    h.grants.ensure("c", LOOK)
    with pytest.raises(Refusal, match="cancelled"):
        h.grants.ensure("c", ACT)
    assert h.grants.level("c") == 0 and not h.held and h.ended == ["c"]


def test_unattended_runs_are_cancelled_and_refused_with_the_reason():
    h = Harness([{"action": "cancel"}])
    with pytest.raises(Refusal) as refused:
        h.grants.ensure("scheduled", LOOK)
    assert "person's answer" in str(refused.value) and "BIOROUTER_CLI_CLASSIC=1" in str(refused.value)


def test_expiry_after_fifteen_idle_minutes():
    h = Harness([accept(consent.LOOK_AND_ACT), accept(consent.LOOK_AND_ACT)])
    h.grants.ensure("c", LOOK)
    h.clock.now += 14 * 60
    h.grants.ensure("c", ACT)  # a call renews it
    h.clock.now += 14 * 60
    assert h.grants.level("c") == ACT
    h.clock.now += 61
    h.grants.expire()
    assert h.grants.level("c") == 0 and not h.held and h.ended == ["c"]
    h.grants.ensure("c", LOOK)
    assert len(h.cards) == 2


def test_two_chats_in_one_process_never_share_the_desktop():
    h = Harness([accept(consent.LOOK_AND_ACT), accept(consent.LOOK_AND_ACT)])
    h.grants.ensure("chat-a", LOOK)
    with pytest.raises(Refusal, match="another chat"):
        h.grants.ensure("chat-b", LOOK)
    assert len(h.cards) == 1
    h.grants.end("chat-a")
    h.grants.ensure("chat-b", LOOK)
    assert h.grants.owner == "chat-b"


def test_no_elicitation_capability_refuses_without_a_card():
    h = Harness([])
    h.grants.can_ask = False
    with pytest.raises(Refusal, match="cannot show the question"):
        h.grants.ensure("c", LOOK)
    assert h.cards == []


def test_busy_desktop_refuses_before_asking():
    h = Harness([], lock_free=False)
    with pytest.raises(Refusal, match="Biorouter Copilot is using this computer"):
        h.grants.ensure("c", LOOK)
    assert h.cards == []


def test_no_session_id_is_refused():
    h = Harness([])
    with pytest.raises(Refusal, match="chat id"):
        h.grants.ensure(None, LOOK)


def test_concurrent_calls_in_one_chat_ask_once():
    gate = threading.Event()
    h = Harness()

    def ask(params, cancel):
        h.cards.append(params)
        gate.wait(5)
        return accept(consent.LOOK_AND_ACT)

    h.grants.ask = ask
    results = []
    threads = [threading.Thread(target=lambda: results.append(h.grants.ensure("c", LOOK))) for _ in range(3)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [ACT, ACT, ACT] and len(h.cards) == 1
