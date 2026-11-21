"""The per chat consent card and the grants it gives.

Before the first look in a chat the wrapper asks the person through an MCP elicitation, which
Biorouter shows as a card in that chat and lets only the person answer (its answer route needs
the user action proof). Biorouter checks neither the answer's shape nor its value, so this
module does: only `accept` with exactly one `access` key holding one of the offered labels
counts, and anything else is a refusal.

A grant is keyed by `_meta["biorouter-session-id"]`. It ends after 15 minutes with no call, when
the wrapper restarts, or when a card is cancelled. Biorouter 1.94.6 does not tell an extension
when the person presses Stop during an ordinary call, so Stop cannot end a grant there.
"""
import threading
import time

LOOK, ACT = 1, 2
IDLE_EXPIRY = 15 * 60
CARD_TIMEOUT = 300.0

DENY = "Don't allow"
LOOK_ONLY = "Only allow looking"
LOOK_AND_ACT = "Allow looking and acting"

FIRST_MESSAGE = "Open Computer Use Agent wants to use the apps on this computer in this chat."
UPGRADE_MESSAGE = "Open Computer Use Agent wants to act in the apps on this computer in this chat."
DISCLOSURE = (
    "It is a fallback for desktop control, separate from Biorouter Copilot. It reads app windows "
    "(their text and screenshots) and, if you allow acting, clicks and types in them. What it sees "
    "goes to this chat's model, which may run outside your institution. Access ends after 15 "
    "minutes without use."
)
UPGRADE_DISCLOSURE = (
    "So far it may only look. Acting means clicking, typing and changing values in apps on this "
    "computer. What it sees goes to this chat's model, which may run outside your institution."
)

NO_CARD_REFUSAL = (
    "This needs the person's answer in the chat, and this client cannot show the question. Open "
    "Computer Use Agent works in the Biorouter desktop app, or in the terminal with "
    "BIOROUTER_CLI_CLASSIC=1; scheduled and unattended runs cannot use it."
)
CANCELLED_REFUSAL = (
    "The question was cancelled, so Open Computer Use Agent did nothing. It needs the person's "
    "answer in the chat: in the Biorouter desktop app, or in the terminal with "
    "BIOROUTER_CLI_CLASSIC=1. Scheduled and unattended runs cannot use it."
)
DECLINED_REFUSAL = "The person did not allow Open Computer Use Agent in this chat, so it did nothing."
LOOK_ONLY_REFUSAL = ("The person allowed Open Computer Use Agent only to look in this chat, so it did not "
                     "act. Describe what you would do instead, or ask the person.")
NO_SESSION_REFUSAL = ("This call carries no chat id (_meta biorouter-session-id), so Open Computer Use Agent "
                      "cannot ask or remember the person's answer. It only works through Biorouter's chats.")
OTHER_CHAT_REFUSAL = ("Open Computer Use Agent is working for another chat on this computer. Finish there "
                      "first; its access ends after 15 minutes without use.")
SERVE_REFUSAL = ("Open Computer Use Agent does not work in a browser session (biorouter serve): only the "
                 "desktop app or the terminal can answer its question. Use the Biorouter desktop app.")


class Refusal(Exception):
    """A refusal sentence for the model; nothing ran."""


def card(level_needed, upgrade):
    options = [DENY, LOOK_AND_ACT] if upgrade else [DENY, LOOK_ONLY, LOOK_AND_ACT]
    return {
        "message": UPGRADE_MESSAGE if upgrade else FIRST_MESSAGE,
        "requestedSchema": {
            "type": "object",
            "properties": {
                "access": {
                    "type": "string",
                    "title": "Access",
                    "description": UPGRADE_DISCLOSURE if upgrade else DISCLOSURE,
                    "enum": options,
                    "default": DENY,
                }
            },
            "required": ["access"],
        },
    }


def parse_answer(response, upgrade):
    """The level the person allowed (0, LOOK or ACT), or None when the card was cancelled.

    Anything but a well formed accept is a refusal: Biorouter delivers an out of enum value and
    extra keys as accept, and the classic CLI passes free text through."""
    if not isinstance(response, dict):
        return 0
    action = response.get("action")
    if action == "cancel":
        return None
    if action != "accept":
        return 0
    content = response.get("content")
    if not isinstance(content, dict) or set(content) != {"access"}:
        return 0
    value = content["access"]
    if value == LOOK_AND_ACT:
        return ACT
    if value == LOOK_ONLY and not upgrade:
        return LOOK
    return 0


class Grant:
    def __init__(self, level, now):
        self.level = level
        self.last_used = now


class Grants:
    """Grants per chat, one desktop owner at a time.

    `ask(params, cancel_event)` sends the elicitation and returns the client's answer;
    `acquire_desktop()` / `release_desktop()` take and drop the lock shared with Biorouter
    Copilot; `on_owner_end(session)` stops the owner's helper.
    """

    def __init__(self, ask, acquire_desktop, release_desktop, on_owner_end=None, clock=time.monotonic,
                 expiry=IDLE_EXPIRY):
        self.ask = ask
        self.acquire_desktop = acquire_desktop
        self.release_desktop = release_desktop
        self.on_owner_end = on_owner_end or (lambda session: None)
        self.clock = clock
        self.expiry = expiry
        self.grants = {}
        self.owner = None
        self.can_ask = False
        self._lock = threading.RLock()
        self._asking = {}

    def level(self, session):
        with self._lock:
            self._expire()
            grant = self.grants.get(session)
            return grant.level if grant else 0

    def _expire(self):
        now = self.clock()
        for session, grant in list(self.grants.items()):
            if now - grant.last_used >= self.expiry:
                self._end_locked(session)

    def expire(self):
        with self._lock:
            self._expire()

    def _end_locked(self, session):
        self.grants.pop(session, None)
        if self.owner == session:
            self.owner = None
            self.release_desktop()
            self.on_owner_end(session)

    def end(self, session):
        with self._lock:
            self._end_locked(session)

    def end_all(self):
        with self._lock:
            for session in list(self.grants):
                self._end_locked(session)
            if self.owner is not None:
                self.owner = None
                self.release_desktop()

    def ensure(self, session, needed, cancel=None):
        """Return when `session` may do `needed` (LOOK or ACT); raise Refusal otherwise."""
        if not session:
            raise Refusal(NO_SESSION_REFUSAL)
        with self._lock:
            asking = self._asking.setdefault(session, threading.Lock())
        with asking:  # one card at a time per chat; a second call waits for the first answer
            with self._lock:
                self._expire()
                grant = self.grants.get(session)
                if grant and grant.level >= needed:
                    grant.last_used = self.clock()
                    return grant.level
                if self.owner is not None and self.owner != session:
                    raise Refusal(OTHER_CHAT_REFUSAL)
                if not self.can_ask:
                    raise Refusal(NO_CARD_REFUSAL)
                took_desktop = False
                if self.owner is None:
                    if not self.acquire_desktop():
                        from .desktop_lock import BUSY_REFUSAL

                        raise Refusal(BUSY_REFUSAL)
                    self.owner, took_desktop = session, True
                upgrade = grant is not None
            try:
                response = self.ask(card(needed, upgrade), cancel)
            except Exception:  # an error or a timeout is a refusal
                response = {"action": "decline"}
            allowed = parse_answer(response, upgrade)
            with self._lock:
                if allowed is None:
                    self._end_locked(session)
                    raise Refusal(CANCELLED_REFUSAL)
                if allowed:
                    self.grants[session] = Grant(max(allowed, grant.level if grant else 0), self.clock())
                    self.owner = session
                    if self.grants[session].level >= needed:
                        return self.grants[session].level
                    raise Refusal(LOOK_ONLY_REFUSAL)
                if session not in self.grants and took_desktop:
                    self.owner = None
                    self.release_desktop()
                raise Refusal(LOOK_ONLY_REFUSAL if upgrade else DECLINED_REFUSAL)
