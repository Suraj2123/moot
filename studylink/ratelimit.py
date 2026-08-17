"""Rate limiting for the authentication endpoints.

A sliding-window counter kept in memory. Two things about that are worth being
honest about up front, because both are real limits and neither is hidden:

**It is per process.** Two uvicorn workers means two independent counters and
twice the effective limit. That is fine for one process, and the fix when it is
not is a shared store -- the `Limiter` interface below is deliberately the shape
Redis would implement, so swapping it is a change to this file.

**It is not a defence against a distributed attacker.** Ten thousand hosts each
making one request per minute defeat any per-IP limit. What this stops is the
single-source case: credential stuffing from one machine, and signup enumeration
that would otherwise walk a breach dump through /auth/signup at wire speed.

The interesting design question is what to key on.

Keying only on IP lets an attacker behind many addresses hammer one account.
Keying only on the account lets an attacker lock a victim out by failing their
login on purpose -- the rate limit becomes the denial of service. So both are
counted, with different budgets: IP gets a tight limit because a legitimate
client rarely trips it, and the account limit is deliberately loose and only
counts *failures*, so a victim under attack degrades rather than being locked
out while their own correct password keeps working for longer.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Optional


@dataclass(frozen=True)
class Rule:
    """`limit` events allowed in any `window` seconds."""

    limit: int
    window: float

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.window <= 0:
            raise ValueError("Rate limit rules need a positive limit and window")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: float = 0.0


# A browser doing a normal login makes one request. Ten in five minutes is
# already someone retyping a forgotten password with feeling.
LOGIN_PER_IP = Rule(limit=10, window=300)

# Signup is rarer still, and this is the limit that stops /auth/signup being
# walked through a breach dump to learn which addresses are registered.
SIGNUP_PER_IP = Rule(limit=5, window=3600)

# Deliberately looser than the IP rule, and only incremented on failure. This
# exists to slow a distributed attack on one account, not to lock its owner out
# -- which is why it is not tight enough to be worth triggering on purpose.
LOGIN_PER_ACCOUNT = Rule(limit=50, window=3600)


class Limiter:
    """Sliding-window counters keyed by an arbitrary string.

    Sliding rather than fixed windows: a fixed window resets on a clock
    boundary, so twice the limit gets through by straddling it. Keeping the
    timestamps costs a little memory and removes the edge entirely.
    """

    def __init__(self) -> None:
        self._events: dict[str, Deque[float]] = defaultdict(deque)
        # Endpoints are served from a threadpool, so two requests really can be
        # in here at once. Without the lock the check and the append interleave
        # and the limit is advisory.
        self._lock = threading.Lock()

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str, rule: Rule) -> Decision:
        """Whether this event is allowed, recording it if so."""
        now = self._now()
        cutoff = now - rule.window

        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= rule.limit:
                # The oldest event is what has to age out before there is room.
                retry_after = max(0.0, events[0] + rule.window - now)
                return Decision(allowed=False, retry_after=retry_after)

            events.append(now)
            return Decision(allowed=True)

    def peek(self, key: str, rule: Rule) -> int:
        """How many events are currently counted. Does not record anything."""
        now = self._now()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - rule.window:
                events.popleft()
            return len(events)

    def reset(self, key: Optional[str] = None) -> None:
        """Clear one key, or everything. Used on success, and by the tests."""
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)

    def sweep(self) -> int:
        """Drop keys with no live events. Returns how many were removed.

        Without this the dict grows one entry per distinct IP forever, which is
        a slow memory leak that an attacker can drive.
        """
        now = self._now()
        with self._lock:
            stale = []
            for key, events in self._events.items():
                # A key is stale once nothing in it is inside the longest window
                # any rule uses.
                while events and events[0] <= now - _LONGEST_WINDOW:
                    events.popleft()
                if not events:
                    stale.append(key)
            for key in stale:
                del self._events[key]
            return len(stale)


_LONGEST_WINDOW = max(
    rule.window for rule in (LOGIN_PER_IP, SIGNUP_PER_IP, LOGIN_PER_ACCOUNT)
)

# One limiter for the process. Tests reset it rather than building their own, so
# that what they exercise is the object the app actually uses.
limiter = Limiter()


def client_ip(request) -> str:
    """Best-effort client address.

    X-Forwarded-For is honoured only when TRUST_PROXY_HEADERS is set, because a
    header the client controls is a rate-limit bypass by default: anyone can
    send a new value per request and get a fresh budget every time. Behind a
    proxy that overwrites it, the header is trustworthy and the socket address
    is useless -- so this has to be a deployment decision, not a guess.
    """
    import os

    if os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in {"1", "true", "yes"}:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # Left-most entry is the original client; the rest are proxies.
            return forwarded.split(",")[0].strip()

    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"
