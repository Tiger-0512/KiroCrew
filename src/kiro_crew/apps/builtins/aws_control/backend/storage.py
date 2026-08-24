"""Drive storage engine — one private bucket per account, three prefixes.

The bucket is the substrate for three console sections, each a view over one
key prefix: ``artifacts/`` (Library), ``drive/`` (Drive), ``backup/``
(Backup). One engine, one discipline, three views.

Everything routes through :func:`kiro_crew.deploy.engine.run_aws` — the AWS
CLI subprocess chokepoint (``--profile``, fixed argv, OS sandbox). No boto3,
no credential material, gateway-side only. The deploy engine's discipline is
inherited deliberately:

* **Stateless-by-tag discovery.** The bucket carries an opaque generated name
  (``kirocrew-drive-<12hex>``) and is found by tags, requiring BOTH
  ``kirocrew:managed=true`` AND ``kirocrew:drive=default``, plus the naming
  scheme — and multiple matches fail loud rather than last-match-wins,
  because discovery is a trust decision (delete/overwrite operate on what it
  returns).
* **Hardened at creation** via the deploy engine's own ``_harden_bucket``
  (BPA on, AES256 SSE, BucketOwnerEnforced), THEN versioning is enabled — the
  drive's deliberate delta from deploy-web. deploy-web keeps versioning off
  because its teardown empties with ``s3 rm`` (current versions only); the
  drive has no teardown surface in this PR, and artifact versions ↔ object
  versions is the point of the Library. A future destroy needs the
  version-aware purge the spec calls out.

CALLER CONTRACT (load-bearing): these functions do NOT check consent. Every
HTTP handler must gate with ``aws_consent.refuse_and_log(SERVICE_S3, ...)``
before calling in, and every mutating handler must run the two-call confirm
gate. The functions are sync (subprocess-bound) — call via
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Optional

from kiro_crew.deploy import engine
from kiro_crew.deploy.engine import AWSError, _checked, _harden_bucket

logger = logging.getLogger(__name__)

BUCKET_PREFIX = "kirocrew-drive-"
TAG_DRIVE = "kirocrew:drive"
#: One drive per account for now; the tag VALUE is reserved for a future
#: multi-drive world so discovery never has to change shape.
DRIVE_ID = "default"

#: Console section → key prefix. The section name is the API-level concept;
#: handlers map it here and a raw prefix never crosses the HTTP boundary.
SECTION_PREFIXES: dict[str, str] = {
    "library": "artifacts/",
    "drive": "drive/",
    "backup": "backup/",
}

#: SigV4's own ceiling. Real expiry can be SHORTER: a URL signed with
#: temporary credentials (SSO / assumed role) dies when that session ends.
#: The UI labels shares accordingly instead of promising the full window.
PRESIGN_MAX_SECS = 7 * 24 * 3600

#: Object keys are user-derived (file and folder names). One conservative
#: shape: printable segments joined by ``/``, no empty / dot / dot-dot
#: segment, no leading slash, bounded length. S3 allows far more; the drive
#: does not need to.
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+@=-]{0,254}$")
_MAX_KEY_LEN = 900


def validate_key(key: str) -> Optional[str]:
    """Return an error string when ``key`` is not a drive-shaped object key."""
    if not key or len(key) > _MAX_KEY_LEN:
        return "key must be 1-900 characters"
    if key.startswith("/") or key.endswith("/"):
        return "key must not start or end with '/'"
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            return "key must not contain empty, '.' or '..' segments"
        if not _KEY_SEGMENT_RE.match(segment):
            return (
                "key segments must start alphanumeric and use only letters, "
                "digits, spaces, and ._()+@=- (max 255 chars each)"
            )
    return None


def section_key(section: str, key: str) -> str:
    """The full object key for ``key`` inside ``section`` (validated)."""
    prefix = SECTION_PREFIXES[section]
    return f"{prefix}{key}"


def new_bucket_name() -> str:
    return f"{BUCKET_PREFIX}{secrets.token_hex(6)}"


# --- discovery (stateless-by-tag) ------------------------------------------


def find_drive(profile: str, region: str) -> Optional[str]:
    """Resolve the account's drive bucket by tags, or None when absent.

    Same trust posture as deploy-web's ``find_site_by_tag``: both tags ANDed,
    naming scheme required, ambiguity fails loud.
    """
    out = _checked(
        [
            "resourcegroupstaggingapi",
            "get-resources",
            "--tag-filters",
            f"Key={TAG_DRIVE},Values={DRIVE_ID}",
            f"Key={engine.TAG_MANAGED},Values=true",
            "--resource-type-filters",
            "s3:bucket",
            "--region",
            region or engine.DEFAULT_REGION,
            "--output",
            "json",
        ],
        profile,
        action="tag:GetResources",
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return None
    buckets: list[str] = []
    for mapping in data.get("ResourceTagMappingList", []):
        arn = mapping.get("ResourceARN", "")
        if not arn.startswith("arn:aws:s3:::"):
            continue
        candidate = arn.split(":::", 1)[1]
        if candidate.startswith(BUCKET_PREFIX):
            buckets.append(candidate)
    if len(buckets) > 1:
        raise AWSError(
            f"ambiguous drive: {len(buckets)} buckets carry the drive tags — "
            "refusing to guess; remove the tag from the impostor"
        )
    return buckets[0] if buckets else None


# --- creation ---------------------------------------------------------------


def create_drive(profile: str, region: str) -> str:
    """Create + harden the drive bucket, versioning ON. Returns the name.

    Caller holds the confirm gate; by the time this runs a human has approved
    the resource. Recovery-safe: if a prior attempt created the bucket but
    died before tagging, discovery misses it — acceptable at this stage
    because the opaque name never collides and hardening puts are idempotent.
    """
    bucket = new_bucket_name()
    create = ["s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    _checked(create, profile, action="s3:CreateBucket")
    # Versioning BEFORE the discovery tags (the drive's delta from
    # deploy-web): tags are what make the bucket discoverable, so everything
    # a discovered drive promises must already hold by the time they land.
    # A crash or missing permission here leaves an untagged bucket that
    # discovery never returns — an orphan to clean up, never a
    # half-configured drive that silently loses overwrite history.
    _checked(
        [
            "s3api",
            "put-bucket-versioning",
            "--bucket",
            bucket,
            "--versioning-configuration",
            "Status=Enabled",
        ],
        profile,
        action="s3:PutBucketVersioning",
    )
    _harden_bucket(
        bucket,
        profile,
        f"TagSet=[{{Key={engine.TAG_MANAGED},Value=true}},"
        f"{{Key={TAG_DRIVE},Value={DRIVE_ID}}}]",
    )
    return bucket


# --- object I/O -------------------------------------------------------------


def list_section(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    subpath: str = "",
    token: str = "",
) -> dict[str, Any]:
    """One '/'-delimited listing page under a section (folders + files)."""
    prefix = SECTION_PREFIXES[section] + (f"{subpath}/" if subpath else "")
    args = [
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        prefix,
        "--delimiter",
        "/",
        "--max-items",
        "500",
        "--output",
        "json",
    ]
    if token:
        args += ["--starting-token", token]
    out = _checked(args, profile, action="s3:ListBucket", timeout=60)
    data = json.loads(out or "{}")
    files = [
        {
            "key": obj["Key"][len(SECTION_PREFIXES[section]) :],
            "size": obj.get("Size", 0),
            "modified": obj.get("LastModified", ""),
        }
        for obj in data.get("Contents", [])
        if obj.get("Key", "") != prefix  # the folder placeholder itself
    ]
    folders = [
        cp["Prefix"][len(SECTION_PREFIXES[section]) :].rstrip("/")
        for cp in data.get("CommonPrefixes", [])
    ]
    return {
        "files": files,
        "folders": folders,
        "nextToken": data.get("NextToken", ""),
    }


def put_file(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    key: str,
    local_path: str,
    *,
    timeout: int = 600,
) -> None:
    """Upload one local file to ``section/key`` (multipart via ``s3 cp``)."""
    _checked(
        ["s3", "cp", local_path, f"s3://{bucket}/{section_key(section, key)}", "--no-progress"],
        profile,
        action="s3:PutObject",
        timeout=timeout,
    )


def get_file(
    profile: str,
    region: str,
    bucket: str,
    section: str,
    key: str,
    dest_path: str,
    *,
    timeout: int = 600,
) -> None:
    """Download ``section/key`` to a local path (handler-owned temp dir)."""
    _checked(
        ["s3", "cp", f"s3://{bucket}/{section_key(section, key)}", dest_path, "--no-progress"],
        profile,
        action="s3:GetObject",
        timeout=timeout,
    )


def delete_key(profile: str, region: str, bucket: str, section: str, key: str) -> None:
    """Delete one object. On the versioned bucket this writes a delete marker,
    so 'deleted' is recoverable at the S3 layer until a purge exists."""
    _checked(
        ["s3api", "delete-object", "--bucket", bucket, "--key", section_key(section, key)],
        profile,
        action="s3:DeleteObject",
    )


def object_exists(profile: str, region: str, bucket: str, section: str, key: str) -> bool:
    """Whether ``section/key`` currently exists (head-object).

    Presigning is LOCAL signing — S3 is never consulted — so without this
    check a typo'd key would mint a working-looking URL that 404s for the
    recipient AND leave a phantom entry in the share ledger.
    """
    rc, _out, _err = engine.run_aws(
        ["s3api", "head-object", "--bucket", bucket, "--key", section_key(section, key)],
        profile,
        timeout=30,
    )
    return rc == 0


def presign(
    profile: str, region: str, bucket: str, section: str, key: str, expires_secs: int
) -> str:
    """A time-boxed share URL for one object.

    ``expires_secs`` is clamped to [60, PRESIGN_MAX_SECS]. The caller records
    the share in the ledger; this function only mints the URL.
    """
    expires = max(60, min(int(expires_secs), PRESIGN_MAX_SECS))
    out = _checked(
        [
            "s3",
            "presign",
            f"s3://{bucket}/{section_key(section, key)}",
            "--expires-in",
            str(expires),
            "--region",
            region or engine.DEFAULT_REGION,
        ],
        profile,
        action="s3:GetObject",
    )
    url = (out or "").strip()
    if not url.startswith("https://"):
        raise AWSError("presign returned no URL")
    return url


# --- usage ------------------------------------------------------------------


def usage(profile: str, region: str, bucket: str) -> dict[str, Any]:
    """Objects + bytes per section, by paginated listing.

    Listing the whole bucket is acceptable at drive scale (LIST is cheap and
    this is cached by the caller); CloudWatch storage metrics would need
    another permission grant for a day-old number.
    """
    per_section: dict[str, dict[str, int]] = {
        name: {"objects": 0, "bytes": 0} for name in SECTION_PREFIXES
    }
    out = _checked(
        [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--output",
            "json",
            "--query",
            "Contents[].{Key: Key, Size: Size}",
        ],
        profile,
        action="s3:ListBucket",
        timeout=120,
    )
    try:
        rows = json.loads(out or "[]") or []
    except json.JSONDecodeError:
        rows = []
    for row in rows:
        key = row.get("Key", "")
        for name, prefix in SECTION_PREFIXES.items():
            if key.startswith(prefix):
                per_section[name]["objects"] += 1
                per_section[name]["bytes"] += int(row.get("Size", 0) or 0)
                break
    total_bytes = sum(s["bytes"] for s in per_section.values())
    total_objects = sum(s["objects"] for s in per_section.values())
    return {
        "bytes": total_bytes,
        "objects": total_objects,
        "sections": per_section,
    }
