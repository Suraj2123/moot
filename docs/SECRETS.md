# Secrets and background work

Day 4 did two things that look unrelated and are not: Canvas credentials became
per-user, and the work that uses them stopped running inside the request. Both
follow from the same fact — once there are real accounts, "the deployment's
Canvas token" and "a sync that blocks the browser" are each broken in a way they
were not when there was one user on a laptop.

## Two kinds of secret

| | Password | Canvas token |
|---|---|---|
| Ever needs reading back? | No | Yes — it gets sent to Canvas |
| Therefore | hashed, scrypt | encrypted, AES-GCM |
| Stored where | `users.password_hash` | `canvas_credentials.encrypted_token` |
| Key lives | nowhere — there is none | `STUDYLINK_SECRET_KEY` |

That difference decides everything else. A hash needs no key and cannot be
undone; encryption needs a key, which means key management, rotation, and a
failure mode where the key is gone and the data is not.

It is also why `cryptography` is the one third-party crypto dependency here.
The stdlib has a real KDF, so passwords need nothing else. It has no AEAD, and
the alternative is assembling AES-CTR plus HMAC by hand — the most reliable way
to ship something that looks encrypted and is not.

## What the vault guarantees

**Fresh nonce per encryption.** GCM does not merely weaken when a nonce repeats
under one key; it leaks the authentication key.

**Ciphertexts are bound to a user and a purpose.** Without authenticated
additional data a ciphertext is just bytes: move Alice's encrypted token into
Bob's row and it decrypts happily, and Bob's sync runs against Alice's Canvas
account. Every value is bound to `(user_id, purpose)` and fails to decrypt under
any other. There is a test that performs exactly that row move.

**No plaintext fallback.** Connecting Canvas with no key configured is refused.
Falling back to storing the token in the clear is how secrets end up unencrypted
in production with nobody noticing.

**Failures are loud.** A token that will not decrypt raises, rather than
reporting "not connected" — which would send one user off to reconnect while
hiding a key problem affecting every account on the server.

**No read path.** `credentials.get_token` is the only function that returns a
readable token, and no endpoint calls it. The object the API returns,
`CanvasConnection`, has no field that could hold one.

## Rotating the key

Order matters, and getting it wrong loses every stored token.

```bash
# 1. new key first, old key kept
export STUDYLINK_SECRET_KEY="v2:<new>,v1:<old>"

# 2. re-encrypt
python scripts/rotate_vault_key.py --status
python scripts/rotate_vault_key.py

# 3. only once that reports zero remaining
export STUDYLINK_SECRET_KEY="v2:<new>"
```

Dropping the old key between steps 1 and 3 makes those rows permanently
unreadable. The script refuses to report success while any row failed, and rows
it could not read are counted as failures rather than skipped — skipping quietly
is how someone concludes it is safe to move on.

## Why the work moved to a queue

Chunking and embedding grows with the corpus and sits on the path of the most
common write in the app. A Canvas sync is a paginated series of calls to
somebody else's server. Neither is something to hold a browser connection open
for; a sync that outlives the proxy timeout fails in a way the user cannot act
on.

So `/sync` and `/reindex` return `202` with a job, and `POST /notes` queues the
reindex. **A note is durable when the request returns and searchable once the
job runs** — a real behaviour change, and the one worth knowing about.

The database is the queue. A broker is a second thing to run, monitor, and back
up, and the throughput it buys is throughput this app does not have.

**The claim is the only hard part.** Two workers reading "the oldest queued row"
and both marking it running means the sync runs twice. So the claim is one
conditional `UPDATE ... WHERE id = ? AND status = 'queued'`, and the worker owns
the job only if it reported a row. No window, no `FOR UPDATE`, no advisory
locks, identical on both backends.

Worth recording: the first version of those tests passed for the wrong reason.
Setting a row to `running` before calling `claim_next` proves nothing, because
the `SELECT` already filters on status and never looks at that row — so removing
the `UPDATE`'s condition broke no test. They now reproduce the real window by
claiming the row between the `SELECT` and the `UPDATE`, and assert the race
actually fired so they cannot drift back to proving nothing.

## What this does not do

**Retries.** A failed job stays failed. Re-queueing is the user pressing the
button again, which for sync and reindex is both safe and idempotent.

**Priorities, scheduling, cancellation.** Each is real work and none of it is
needed to stop a browser waiting on a Canvas sync.

**Multiple workers as a scaling story.** More than one is safe — that is what
the conditional claim is for — but nothing here is throughput-bound, and each
worker holds a connection.

**Encryption of note content.** A stolen database no longer yields working
Canvas tokens or passwords. It still yields every note every user has written.

**Protecting a token from the server itself.** The key is in the server's
environment, so anyone who can read that environment can decrypt. What this
defends is a leaked backup, a dumped table, or a row moved between accounts —
not a compromised host.

## Operating it

```bash
python scripts/run_worker.py                 # process jobs
python scripts/run_worker.py --once          # one job, then exit
python scripts/purge_sessions.py             # expired sessions
python scripts/rotate_vault_key.py --status  # key rotation progress
```

The worker handles `SIGTERM`, so a deploy finishes the job in flight instead of
stranding it in `running` for `reap_stale` to fail half an hour later.
