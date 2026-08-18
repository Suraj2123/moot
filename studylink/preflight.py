"""Configuration checks that run before the app serves its first request.

Every check here exists because the failure it prevents is silent. A missing
encryption key does not crash the server -- it crashes the first person who
tries to connect Canvas, hours later. SQLite on a platform with an ephemeral
filesystem does not error -- it works perfectly until the next deploy takes
every note with it.

So the checks run at import and the process refuses to start rather than
starting wrong. The distinction that makes this usable is `STUDYLINK_ENV`:
locally these are warnings, and in production they are fatal. A development
machine should not need a key vault to run the tests.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def report(self) -> str:
        lines = []
        for message in self.errors:
            lines.append(f"  ERROR    {message}")
        for message in self.warnings:
            lines.append(f"  WARNING  {message}")
        return "\n".join(lines)


class ConfigurationError(RuntimeError):
    """The process cannot safely start with this configuration."""


def is_production() -> bool:
    return os.environ.get("STUDYLINK_ENV", "development").strip().lower() == "production"


def check(env: dict[str, str] | None = None) -> Findings:
    """Inspect the environment. Pure, so the tests can pass one in."""
    env = dict(os.environ if env is None else env)
    production = env.get("STUDYLINK_ENV", "development").strip().lower() == "production"
    findings = Findings()

    def problem(message: str) -> None:
        (findings.errors if production else findings.warnings).append(message)

    # --- storage -----------------------------------------------------------
    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        problem(
            "DATABASE_URL is not set, so this will use a local SQLite file. On a "
            "host with an ephemeral filesystem every deploy silently discards all "
            "user data. Provision Postgres and set DATABASE_URL."
        )
    elif database_url.startswith("sqlite"):
        problem(
            "DATABASE_URL points at SQLite. It works, but a single file does not "
            "survive a redeploy on most platforms and cannot be shared between "
            "the web process and the worker."
        )

    # --- secrets -----------------------------------------------------------
    secret_key = env.get("STUDYLINK_SECRET_KEY", "").strip()
    if not secret_key:
        problem(
            "STUDYLINK_SECRET_KEY is not set. Canvas connections will be refused, "
            "and a key generated on a container's disk is lost on the next deploy "
            "along with every stored Canvas token."
        )
    elif "AAAAAAAA" in secret_key:
        # The all-zero key is in the repo, in CI, and in this file's tests.
        findings.errors.append(
            "STUDYLINK_SECRET_KEY is the throwaway key used by CI and the docs. "
            "It is public. Generate a real one: "
            "python -c 'from studylink.vault import generate_key; print(generate_key())'"
        )

    # --- the web front door ------------------------------------------------
    if production and env.get("CORS_ALLOW_ORIGINS", "").strip() == "*":
        findings.errors.append(
            "CORS_ALLOW_ORIGINS is '*', which lets any page on the internet call "
            "this API with a signed-in user's token. Name the origins instead."
        )

    if production and not env.get("TRUST_PROXY_HEADERS", "").strip():
        findings.warnings.append(
            "TRUST_PROXY_HEADERS is unset. Behind a load balancer every request "
            "appears to come from the proxy, so per-IP rate limiting sees one "
            "client. Set it only if your proxy overwrites X-Forwarded-For."
        )

    # --- the LLM -----------------------------------------------------------
    if not env.get("ANTHROPIC_API_KEY", "").strip():
        findings.warnings.append(
            "ANTHROPIC_API_KEY is not set. Everything except the chat and the "
            "agent works; those return 503."
        )

    budget = env.get("LLM_MONTHLY_BUDGET_USD", "").strip()
    if production and budget in {"0", "0.0", "0.00"}:
        findings.warnings.append(
            "LLM_MONTHLY_BUDGET_USD is 0, which disables the per-user spend cap. "
            "One user with a script can then spend without limit."
        )

    return findings


def run(strict: bool | None = None) -> Findings:
    """Check, log, and raise in production if anything is wrong.

    Called at import of the API module, so a misconfigured deployment fails at
    boot -- where the platform will show it -- rather than on the first request
    that happens to need the missing piece.
    """
    findings = check()
    strict = is_production() if strict is None else strict

    if findings.errors or findings.warnings:
        header = "Configuration problems:" if findings.errors else "Configuration notes:"
        logger.warning("%s\n%s", header, findings.report())

    if strict and findings.errors:
        raise ConfigurationError(
            "Refusing to start with this configuration:\n" + findings.report()
        )
    return findings
