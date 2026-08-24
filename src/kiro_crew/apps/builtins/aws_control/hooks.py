"""Lifecycle hooks — the nightly backup loop.

One background task, started on enable, that wakes every half hour and runs
the snapshot backup when it is due (nightly toggle on AND >23 h since the
last run — see ``backup.due_for_nightly``). Every AWS-reaching step keeps
the same guards the HTTP path has: consent fails closed (a silent skip plus
a log line, never an unconfirmed charge), and the drive is tag-discovered
per run rather than trusted from memory.

The loop runs against the REGISTRY DEFAULT account only — the same account
the consent card confirms. Multi-account nightly schedules arrive with the
per-account grant store (spec §9).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import backup as backup_mod
from kiro_crew.apps.builtins.aws_control.backend import storage as storage_mod
from kiro_crew.deploy import profiles as deploy_profiles

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECS = 30 * 60

_task: asyncio.Task[None] | None = None


async def _run_once() -> None:
    """One due-check + backup attempt. Every failure is a log line, not a crash."""
    resolved = await asyncio.to_thread(deploy_profiles.resolve_profile, "")
    if resolved is None:
        logger.info("aws-control nightly: no registered profile; skipping")
        return
    profile, region = resolved
    # Backup state is keyed per account, so the loop resolves which
    # account the default profile is actually pointing at right now.
    identity = await aws_consent.probe_identity(profile, region)
    if not identity.ok or not identity.account:
        logger.info("aws-control nightly: account unresolved; skipping")
        return
    account = identity.account
    if not await asyncio.to_thread(backup_mod.due_for_nightly, account):
        return
    allowed = await aws_consent.refuse_and_log(
        aws_consent.SERVICE_S3, profile=profile, region=region
    )
    if not allowed:
        return  # refuse_and_log already logged + audited
    try:
        bucket = await asyncio.to_thread(storage_mod.find_drive, profile, region)
        if not bucket:
            logger.info("aws-control nightly: no drive bucket yet; skipping")
            return
        record = await asyncio.to_thread(
            backup_mod.run_snapshot_backup, account, profile, region, bucket
        )
        logger.info("aws-control nightly backup pushed: %s", record.get("key", ""))
    except Exception:
        logger.warning("aws-control nightly backup failed", exc_info=True)


async def _loop() -> None:
    while True:
        try:
            await _run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("aws-control nightly loop error", exc_info=True)
        await asyncio.sleep(_CHECK_INTERVAL_SECS)


async def on_startup(ctx: Any) -> None:  # noqa: ARG001 — kept for the hook ABI
    """Start the nightly loop. Idempotent across enable/disable cycles."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_running_loop().create_task(_loop())


async def on_shutdown(ctx: Any) -> None:  # noqa: ARG001 — kept for the hook ABI
    """Stop the loop; a backup mid-push is cancelled with the task."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
