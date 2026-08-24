"""Account center — aggregate the deploy profile registry by AWS account.

The registry (``deploy/profiles.py``) is profile-shaped: a flat list of
``{name, region, account, verified_at, note}`` entries. The portal is
account-shaped: an account owns one or more profiles ("keys"), and the page
shows one row per account. This module is the fold between the two.

Everything here is read-only against AWS and free: the only call made is
``sts:GetCallerIdentity`` (via :func:`kiro_crew.aws_consent.probe_identity`,
shared with the consent surface so the two never disagree about what a
profile resolves to), plus ``aws configure get`` reads to classify HOW a
profile authenticates. Both go through the deploy engine's sandboxed CLI
chokepoint; neither reads a credential file.

Names-only invariant (inherited, load-bearing): this module stores profile
names, regions, probe outcomes and display metadata. It never reads, writes,
or caches credential material, and it never mutates the registry — the deploy
surface stays the single writer.

Probe results are cached in-process (:data:`_PROBE_TTL_SECS`) because the
page re-renders far more often than credentials change state; ``refresh=1``
bypasses the cache for the explicit "check again" click.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import aws_consent
from kiro_crew.deploy import engine
from kiro_crew.deploy import profiles as deploy_profiles
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)

#: How long an aggregated account snapshot stays served without re-probing.
#: Long enough to absorb page re-renders and tab switches, short enough that
#: an expired SSO session is noticed within minutes without the user clicking
#: anything.
_PROBE_TTL_SECS = 300.0

#: At most this many identity probes run concurrently. Each probe is an AWS
#: CLI subprocess; a registry of dozens of profiles must not fork them all at
#: once.
_PROBE_CONCURRENCY = 4

#: ``aws configure get`` reads used to classify a profile's auth mechanism.
#: Values are setting names passed to the CLI — the CLI parses the config
#: files itself, so the names-only invariant holds.
_KIND_SSO_SESSION = "sso_session"
_KIND_SSO_START_URL = "sso_start_url"
_KIND_CREDENTIAL_PROCESS = "credential_process"

#: Profile auth kinds, in the order the UI cares about them.
KIND_SSO = "sso"
KIND_CREDENTIAL_PROCESS = "credential-process"
KIND_OTHER = "other"

#: LoopBoundLock, not asyncio.Lock: a module-global asyncio primitive binds
#: to the import-time loop and raises when acquired from another (#4800).
_snapshot_lock = LoopBoundLock()
_snapshot: dict[str, Any] | None = None
_snapshot_at: float = 0.0


@dataclass
class ProfileView:
    """One registry profile, folded with its live probe outcome."""

    name: str
    region: str
    kind: str = KIND_OTHER
    identity_ok: bool = False
    account: str = ""
    arn: str = ""
    detail: str = ""
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "region": self.region,
            "kind": self.kind,
            "identityOk": self.identity_ok,
            "account": self.account,
            "arn": self.arn,
            "detail": self.detail,
            "default": self.is_default,
        }


@dataclass
class AccountView:
    """One AWS account row: every profile that resolved to it."""

    account: str
    profiles: list[ProfileView] = field(default_factory=list)

    @property
    def health(self) -> str:
        """``ok`` | ``degraded`` | ``unknown`` — the one light per account.

        ``unknown`` is reserved for the pseudo-row of profiles that could not
        be resolved to any account (probe failed AND the registry never
        recorded one) — there is nothing to be healthy ABOUT. A known account
        with any failing profile is ``degraded``: the account itself was
        reachable once, and at least one of its keys is not working now.
        """
        if not self.account:
            return "unknown"
        if all(p.identity_ok for p in self.profiles):
            return "ok"
        return "degraded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "health": self.health,
            "profiles": [p.to_dict() for p in self.profiles],
            # P1 fills these from real reads (storage scan, CE). Explicit
            # nulls rather than zeros: the page must render "not measured
            # yet", never a fake $0.00.
            "summary": {
                "storage": None,
                "sites": None,
                "tasks": None,
                "costMonthToDate": None,
            },
        }


async def classify_profile(name: str) -> str:
    """How ``name`` authenticates, from CLI config reads only.

    The name is re-validated HERE, not only at registration: the registry
    file is agent-writable config, so a hostile entry written out-of-band
    must not reach an argv (or, worse, the display command a user copies
    into a shell). An invalid name classifies as ``other`` — fail closed.

    ``aws configure get`` exits 0 with the value when the setting exists and
    non-zero when it does not — that exit code is the whole classification.
    The value itself is discarded: an SSO start URL or a credential_process
    command line is configuration the user wrote, but the portal only needs
    the SHAPE (which Reconnect path applies), so nothing beyond the kind is
    kept or returned.
    """
    if not aws_consent._PROFILE_RE.match(name or ""):
        return KIND_OTHER
    for setting, kind in (
        (_KIND_SSO_SESSION, KIND_SSO),
        (_KIND_SSO_START_URL, KIND_SSO),
        (_KIND_CREDENTIAL_PROCESS, KIND_CREDENTIAL_PROCESS),
    ):
        try:
            rc, out, _err = await asyncio.to_thread(
                engine.run_aws, ["configure", "get", setting], name, 10
            )
        except Exception:
            # A broken CLI resolution must degrade ONE profile's badge, not
            # crash the whole listing (the probe already reports health).
            logger.debug("classify_profile failed for %s", name, exc_info=True)
            return KIND_OTHER
        if rc == 0 and (out or "").strip():
            return kind
    return KIND_OTHER


def reconnect_plan(kind: str, name: str) -> dict[str, Any]:
    """What "Reconnect" can honestly offer for a profile of ``kind``.

    P0 answers the feasibility question without performing anything:

    * ``sso`` — re-auth is a device flow (``aws sso login``) that PRINTS a URL
      and code for the user's browser. The gateway can run it in a later
      phase; until that lands, the honest offer is the exact command.
    * ``credential-process`` — the process is the user's own tooling; only
      terminal guidance applies.
    * ``other`` — static keys or an ambient chain; nothing to re-run, point
      at ``aws configure``.

    The returned ``command`` is DISPLAY text for the guidance card — a user
    will paste it into a terminal, so the name is constrained to the AWS
    profile charset (no whitespace, no shell metacharacters) before it may
    appear here. A name failing that shape yields guidance with no command.
    """
    if not aws_consent._PROFILE_RE.match(name or ""):
        return {"method": "terminal", "kind": KIND_OTHER, "command": ""}
    if kind == KIND_SSO:
        return {
            "method": "terminal",
            "kind": kind,
            "command": f"aws sso login --profile {name}",
        }
    if kind == KIND_CREDENTIAL_PROCESS:
        return {
            "method": "terminal",
            "kind": kind,
            "command": f"aws sts get-caller-identity --profile {name}",
        }
    return {
        "method": "terminal",
        "kind": kind,
        "command": f"aws configure --profile {name}",
    }


async def _fold_profile(entry: dict[str, str], default_name: str) -> ProfileView:
    """Probe + classify one registry entry into its view."""
    name = entry["name"]
    region = entry.get("region") or deploy_profiles.DEFAULT_REGION
    identity = await aws_consent.probe_identity(name, region)
    kind = await classify_profile(name)
    return ProfileView(
        name=name,
        region=region,
        kind=kind,
        identity_ok=identity.ok,
        # A failed probe says nothing about which account the profile is FOR;
        # fall back to the account the registry recorded at verify time so the
        # row stays grouped where the user last saw it.
        account=identity.account or entry.get("account", ""),
        arn=identity.arn,
        detail=identity.detail,
        is_default=(name == default_name),
    )


async def _build_snapshot() -> dict[str, Any]:
    reg = await asyncio.to_thread(deploy_profiles.load_registry)
    default_name = reg.get("default", "")
    entries = reg.get("profiles", [])

    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _bounded(entry: dict[str, str]) -> ProfileView:
        async with sem:
            return await _fold_profile(entry, default_name)

    views = list(await asyncio.gather(*(_bounded(e) for e in entries)))

    by_account: dict[str, AccountView] = {}
    for view in views:
        row = by_account.setdefault(view.account, AccountView(account=view.account))
        row.profiles.append(view)

    # Known accounts first (registry order within), the unresolved pseudo-row
    # last — it is the row that only offers Reconnect.
    ordered = sorted(by_account.values(), key=lambda a: (a.account == "",))
    healthy = sum(1 for v in views if v.identity_ok)
    return {
        "accounts": [a.to_dict() for a in ordered],
        "totals": {
            "accounts": sum(1 for a in ordered if a.account),
            "profiles": len(views),
            "profilesHealthy": healthy,
        },
        "generatedAt": deploy_profiles.now_iso(),
    }


async def list_accounts(*, refresh: bool = False) -> dict[str, Any]:
    """The aggregated account snapshot, cached for :data:`_PROBE_TTL_SECS`.

    The lock makes concurrent first-loads coalesce into one probe sweep
    instead of forking one sweep per open tab.
    """
    global _snapshot, _snapshot_at
    async with _snapshot_lock:
        fresh = _snapshot is not None and (time.monotonic() - _snapshot_at) < _PROBE_TTL_SECS
        if fresh and not refresh:
            assert _snapshot is not None
            return _snapshot
        snapshot = await _build_snapshot()
        _snapshot = snapshot
        _snapshot_at = time.monotonic()
        return snapshot


async def resolve_account_profile(account: str) -> tuple[str, str] | None:
    """The working (profile, region) for operations on ``account``.

    Preference order: the registry default when it belongs to this account
    and probes healthy, then any healthy profile, then None — an account
    with no working key gets NO silent fallback to a different account's
    credentials.
    """
    if not account:
        return None
    snapshot = await list_accounts()
    for row in snapshot["accounts"]:
        if row["account"] != account:
            continue
        profiles = row["profiles"]
        healthy = [p for p in profiles if p["identityOk"]]
        chosen = next((p for p in healthy if p["default"]), None) or (
            healthy[0] if healthy else None
        )
        if chosen is None:
            return None
        return chosen["name"], chosen["region"]
    return None


def invalidate_cache() -> None:
    """Drop the snapshot (tests, and any future registry-mutation hook)."""
    global _snapshot, _snapshot_at
    _snapshot = None
    _snapshot_at = 0.0
