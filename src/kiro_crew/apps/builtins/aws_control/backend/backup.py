"""Backup — memory/workspace snapshots and session archives on ``backup/``.

Two backup kinds, one push path:

* **Snapshot** (the mockup's "Memory & workspace" row): the existing
  ``kiro_crew.snapshot`` engine builds its portable ``.tar.gz`` (memory,
  crons, config, skills, workspace, notifications, security — its component
  set, unchanged), and the archive is pushed to
  ``backup/snapshots/<name>.tar.gz``.
* **Sessions archive** (the "Sessions archive" row): one tarball of BOTH
  session halves — ``<data home>/sessions/`` (transcripts + rotated
  archives) and ``<kiro home>/sessions/cli/`` (the CLI replay logs) — pushed
  to ``backup/sessions/<stamp>.tar.gz``. Whole-set, not per-session: the
  "both halves move together" invariant is honoured by construction, and the
  per-session incremental integration with the storage inventory is future
  work.

**Restore is a download, deliberately.** A restore lands the archive in
``<app data dir>/restore/`` and hands back the path; nothing hot-swaps a
live ``memory.db`` or sessions dir under a running gateway. The snapshot
engine's own merge/replace tooling (or a stopped gateway) takes it from
there, and the UI copy says exactly that.

State (`<app data dir>/backup.json`): last run per kind + the nightly
toggle. The nightly loop lives in the app's ``on_startup`` hook.

CALLER CONTRACT: handlers hold the consent gate; sync, subprocess/tar-bound
— call via ``asyncio.to_thread`` (pushes of a large sessions set can run
minutes; handlers use generous timeouts).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Optional

from kiro_crew.apps.builtins.aws_control.backend import storage
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home, kiro_sessions_dir
from kiro_crew.history import SESSIONS_DIR_NAME
from kiro_crew.platform_compat import file_lock
from kiro_crew.snapshot import snapshot_main

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
KIND_SNAPSHOT = "snapshot"
KIND_SESSIONS = "sessions"
_PUSH_TIMEOUT_SECS = 3600


def _state_path() -> Path:
    return app_data_dir(APP_NAME) / "backup.json"


def read_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=1))


def _locked_state_update(mutate) -> Any:
    """Read-modify-write the state file under the sidecar lock.

    Two backup kinds can finish concurrently (a manual run racing the
    nightly loop); an unlocked read-modify-write would let the later atomic
    write silently discard the earlier run record. Same sidecar-lock shape
    as the share ledger.
    """
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            state = read_state()
            result = mutate(state)
            write_state(state)
    return result


def _account_state(state: dict[str, Any], account: str) -> dict[str, Any]:
    """The per-account slice of the state file.

    Keyed by account, not global: two connected accounts each own their
    nightly toggle and run records, so switching the default cannot make one
    console report the other's backups.
    """
    return state.setdefault("accounts", {}).setdefault(account, {})


def _record_run(account: str, kind: str, key: str, size: int) -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        runs = _account_state(state, account).setdefault("runs", {})
        runs[kind] = {
            "key": key,
            "bytes": size,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        return runs[kind]

    return _locked_state_update(mutate)


def _stamp() -> str:
    """A second-resolution timestamp plus entropy.

    A manual run racing the nightly loop can land in the same second; on a
    versioned bucket an identical key does not destroy the earlier archive,
    but it hides it — listings and restore only see the current version. The
    hex suffix keeps every archive its own key.
    """
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"


def run_snapshot_backup(account: str, profile: str, region: str, bucket: str) -> dict[str, Any]:
    """Build a snapshot archive and push it. Returns the run record."""
    with tempfile.TemporaryDirectory(prefix="kc-backup-") as tmp:
        rc = snapshot_main([tmp, "--keep", "1"])
        if rc != 0:
            raise RuntimeError(f"snapshot build failed (rc={rc})")
        archives = sorted(Path(tmp).glob("kirocrew-snapshot-*.tar.gz"))
        if not archives:
            raise RuntimeError("snapshot build produced no archive")
        archive = archives[-1]
        # snapshot_main names by second-resolution timestamp; a racing pair
        # would collide on the key, so the pushed key carries its own
        # entropy (the _stamp shape) rather than trusting the file name.
        key = f"snapshots/kirocrew-snapshot-{_stamp()}.tar.gz"
        storage.put_file(profile, region, bucket, "backup", key, str(archive))
        return _record_run(account, KIND_SNAPSHOT, key, archive.stat().st_size)


def _add_tree(tar: tarfile.TarFile, root: Path, arc_prefix: str) -> int:
    """Add a directory tree (files only, no symlink following). Returns count."""
    added = 0
    if not root.is_dir():
        return added
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        tar.add(str(path), arcname=f"{arc_prefix}/{path.relative_to(root)}")
        added += 1
    return added


def run_sessions_backup(account: str, profile: str, region: str, bucket: str) -> dict[str, Any]:
    """Tar both session halves and push. Returns the run record."""
    crew_sessions = data_home() / SESSIONS_DIR_NAME
    cli_sessions = kiro_sessions_dir()
    with tempfile.TemporaryDirectory(prefix="kc-backup-") as tmp:
        archive = Path(tmp) / f"sessions-{_stamp()}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            count = _add_tree(tar, crew_sessions, "crew")
            count += _add_tree(tar, cli_sessions, "cli")
        if count == 0:
            raise RuntimeError("no session files to archive")
        key = f"sessions/{archive.name}"
        storage.put_file(profile, region, bucket, "backup", key, str(archive))
        return _record_run(account, KIND_SESSIONS, key, archive.stat().st_size)


def list_remote_backups(profile: str, region: str, bucket: str) -> dict[str, Any]:
    """Remote backup listings for both kinds (newest first, capped page)."""
    result: dict[str, Any] = {}
    for kind, sub in ((KIND_SNAPSHOT, "snapshots"), (KIND_SESSIONS, "sessions")):
        page = storage.list_section(profile, region, bucket, "backup", sub)
        files = sorted(page["files"], key=lambda f: f.get("key", ""), reverse=True)
        result[kind] = files[:20]
    return result


def restore_download(profile: str, region: str, bucket: str, key: str) -> dict[str, Any]:
    """Download one backup archive to the staging dir; return its local path.

    ``key`` is section-relative (``snapshots/...`` or ``sessions/...``) and
    validated by the handler with the same key rules as every drive key.
    """
    staging = app_data_dir(APP_NAME) / "restore"
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / Path(key).name
    storage.get_file(profile, region, bucket, "backup", key, str(dest))
    return {"path": str(dest), "bytes": dest.stat().st_size}


def nightly_enabled(account: str) -> bool:
    return bool(read_state().get("accounts", {}).get(account, {}).get("nightly"))


def set_nightly(account: str, enabled: bool) -> None:
    def mutate(state: dict[str, Any]) -> None:
        _account_state(state, account)["nightly"] = bool(enabled)

    _locked_state_update(mutate)


def last_runs(account: str) -> dict[str, Any]:
    return read_state().get("accounts", {}).get(account, {}).get("runs", {})


def due_for_nightly(account: str, now: Optional[dt.datetime] = None) -> bool:
    """True when the nightly snapshot has not run in the last ~23 hours."""
    if not nightly_enabled(account):
        return False
    runs = last_runs(account).get(KIND_SNAPSHOT)
    if not runs:
        return True
    try:
        last = dt.datetime.fromisoformat(runs["at"])
    except (KeyError, ValueError):
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - last).total_seconds() > 23 * 3600
