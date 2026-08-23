"""Turning a failed model call into something a student can act on.

Every Anthropic call in this codebase used to be written the same way:
construct the client inside a `try` that raised a clear "no credentials"
error, and then call the API outside any `try` at all. Missing credentials
were handled beautifully and every other failure -- an empty credit balance,
a rejected key, a rate limit, an overloaded API -- escaped the endpoint as an
unhandled exception and reached the browser as a bare 500.

That is the worst possible reporting for these failures, because almost all of
them are things the person running the app can fix in a minute *if they are
told what happened*. "500" tells them their software is broken. "Your
Anthropic account has no credit" tells them where to go.

So: one place that knows how to read an Anthropic exception, and one context
manager that every call site wraps itself in.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

CONSOLE_BILLING = "https://console.anthropic.com/settings/billing"

# Matched against the upstream message, lowercased. Ordered: the first hit
# wins, so put the specific ones above the general ones.
#
# Substrings rather than status codes because the status is often the same for
# several unrelated problems -- a 400 is both "you have no credit" and "that
# model does not exist", and those need completely different advice.
_ADVICE: tuple[tuple[str, str], ...] = (
    (
        "credit balance is too low",
        "Your Anthropic account is out of credit, so it will not run the "
        f"request. Add credit at {CONSOLE_BILLING}. Writing cards yourself "
        "with `::` inside a note costs nothing and needs no key.",
    ),
    (
        "invalid x-api-key",
        "Anthropic rejected that API key. Check ANTHROPIC_API_KEY -- a copied "
        "key sometimes picks up a trailing space or gets truncated.",
    ),
    (
        "authentication_error",
        "Anthropic rejected that API key. Check ANTHROPIC_API_KEY -- a copied "
        "key sometimes picks up a trailing space or gets truncated.",
    ),
    (
        "permission",
        "That Anthropic key is not allowed to use this model. Check the key's "
        "workspace permissions in the console.",
    ),
    (
        "not_found_error",
        "Anthropic does not recognise the model this app is configured to "
        "use. Set ANTHROPIC_MODEL to a model your account can reach.",
    ),
    (
        "rate_limit",
        "Anthropic is rate-limiting this key. Wait a few seconds and try "
        "again.",
    ),
    (
        "overloaded",
        "Anthropic is overloaded right now. This usually clears in a minute.",
    ),
)


def explain(exc: BaseException) -> str:
    """A sentence naming what went wrong and what to do about it.

    Falls back to the upstream text rather than to something generic. An
    unrecognised Anthropic message is still far more use than "the request
    failed", and every message here describes the caller's own account -- there
    is nothing in one that they should not see.
    """
    raw = str(exc).strip()
    haystack = raw.lower()
    for needle, advice in _ADVICE:
        if needle in haystack:
            return advice
    if not raw:
        return f"The model call failed with {type(exc).__name__} and no message."
    return f"The model call failed: {raw}"


@contextmanager
def upstream(raise_as: Callable[[str], BaseException]) -> Iterator[None]:
    """Run a model call, reporting any failure as `raise_as(explanation)`.

    Deliberately catches Exception rather than `anthropic.APIError`. This
    module must not import anthropic -- it is an optional dependency, and the
    modules that use it import it lazily so the rest of the app runs without
    it installed. Catching broadly is also the right behaviour here regardless:
    an httpx timeout, a DNS failure, and a proxy rejection are all "the model
    call did not work", and none of them should reach a user as a 500.

    KeyboardInterrupt and SystemExit are BaseException and pass straight
    through, which is what you want when someone is trying to stop a worker.
    """
    try:
        yield
    except Exception as exc:
        raise raise_as(explain(exc)) from exc
