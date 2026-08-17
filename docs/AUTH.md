# Authentication: what it defends against, and what it does not

StudyLink went from one hardcoded local user to real accounts on the web. This
is the record of how, and — more usefully — of where the edges are.

## The shape of it

```
POST /auth/signup   ->  account + session token
POST /auth/login    ->  session token
POST /auth/logout   ->  ends the calling session
GET  /auth/me       ->  who the token belongs to
GET  /auth/sessions ->  the caller's live sessions
DELETE /auth/sessions/{id}        ->  end one
POST   /auth/sessions/revoke-others ->  end all but this one
POST /auth/password ->  change it, ending every other session
```

Everything else requires `Authorization: Bearer <token>`.

| | |
|---|---|
| Passwords | scrypt, n=2^17 r=8 p=1 (OWASP's floor), ~420ms and ~128MB per hash |
| Tokens | 256 random bits, stored only as SHA-256 |
| Expiry | absolute, 30 days, never extended by use |
| Revocation | a column, not a delete |

## The decisions worth defending

**A failed login reveals nothing about which part failed.** Wrong password,
unknown address, and an account with no password all return one status and one
message. An endpoint that says "no such account" is a membership oracle: point
it at a breach dump and it tells you which addresses have accounts here.

The half people miss is timing. Rejecting an unknown address without hashing
returns in microseconds while a real rejection costs ~420ms, and that gap
rebuilds the oracle the shared message was hiding. So the unknown path hashes
against a dummy and throws the result away. There is a test for the gap.

**Signup does reveal that an address is taken**, and cannot avoid it — the
account genuinely cannot be created. That is why signup is the more strictly
rate-limited endpoint.

**Password changes end every other session.** Changing a password while the
attacker's session keeps working does not remove the attacker. This is the most
common way the feature is built wrong. The current password is required even
though the caller is authenticated, because otherwise a stolen token is enough
to take the account permanently — the exact situation someone is in when they
reach for it.

**Cross-user access is 404, never 403.** A 403 confirms the row exists and
belongs to somebody else, which is the fact worth hiding. Applies to notes,
assignments, and sessions alike.

**Rate limits key on both IP and account, with different budgets.** Keying only
on IP lets a distributed attacker hammer one account. Keying only on the account
lets an attacker lock a victim out by failing their login on purpose — the rate
limit becomes the denial of service. So the IP budget is tight and cleared by a
correct password, and the account budget is loose and spent only by failures.

**Tokens are opaque and server-side, not JWTs.** A JWT cannot be revoked before
it expires without a server-side denylist, at which point you have the database
lookup you were avoiding, plus a signing key to rotate. Revocation is a feature
here (`revoke-others`, password change), so the lookup is the point.

## What this does not defend against

Stated plainly, because a security document that lists only wins is marketing.

**A distributed attacker.** Ten thousand hosts making one request each defeat
any per-IP limit. What the limits stop is the single-source case: credential
stuffing from one machine and signup enumeration at wire speed.

**Rate limits across multiple processes.** The counters are in memory, so two
uvicorn workers means two independent budgets. The `Limiter` interface is
deliberately the shape Redis would implement; until then, run one process or
accept the multiplier.

**Anything after database compromise.** Password hashes are expensive to attack
and tokens are stored hashed, so a stolen backup is not a set of working logins.
It is still every note every user has written. There is no application-level
encryption of note content.

**XSS in a frontend that stores the token badly.** The API sets sensible
headers, but where a browser app puts the token is that app's decision, and
`localStorage` is readable by any script that gets injected. This lands with
Days 8–14.

**Account recovery.** There is no password reset, which means a forgotten
password is a lost account. That is a deliberate gap, not an oversight: email
delivery, single-use expiring tokens, and the enumeration questions that come
back with them are their own day of work. Until then the honest statement is
that this is not usable by strangers.

**Second factors, email verification, and session binding.** No TOTP, no
verification that an address belongs to the person who typed it, and no tying
of a session to an IP or device. The last is a deliberate omission rather than a
missing feature — session binding breaks people on mobile networks constantly
and is weak against an attacker who is usually on the same network anyway.

## Threat model in one paragraph

The attacker is remote, not on the machine, and does not have the database. They
can make unlimited requests from a small number of addresses, may hold a
password from an unrelated breach, and may have stolen a session token from a
browser. Against that: credentials are expensive to guess offline, failed logins
are indistinguishable and throttled, a stolen token expires and can be revoked
from another session, and a password change removes every other session. An
attacker who has the database, the server, or the user's browser is outside what
this defends.

## Operating it

```bash
# expired sessions and stale rate-limit keys
python scripts/purge_sessions.py --dry-run
python scripts/purge_sessions.py
```

`CORS_ALLOW_ORIGINS` is empty by default — correct for a same-origin frontend,
and there is no wildcard option because a wildcard lets any page on the internet
call this API with a user's token. `TRUST_PROXY_HEADERS` should be set only
behind a proxy that overwrites `X-Forwarded-For`; otherwise it is a rate-limit
bypass, since the client picks its own value and gets a fresh budget per request.
