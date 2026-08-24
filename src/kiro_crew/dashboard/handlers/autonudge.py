"""Auto-nudge HTTP API — list / start / stop / update loops for chat slots."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from aiohttp import web

from kiro_crew.autonudge import get_instance as _autonudge_get

# The security chokepoint lives in the transport-agnostic module (see its
# docstring); re-exported here so existing importers keep working. This file
# is intentionally a THIN HTTP mapping over it.
from kiro_crew.autonudge_authz import (  # noqa: F401 - re-exported
    authorize_and_add_nudge,
    authorize_and_update_nudge,
    resolve_stop_sentinel,
)
from kiro_crew.config.loader import KiroCrewConfig, resolve_variables
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.sel import sel
from kiro_crew.session_ledger import ledger_key, render_snapshot
from kiro_crew.variables import expand as expand_variables

logger = logging.getLogger(__name__)


def render_nudge_message(
    message: str,
    stop_sentinel_path: str | None,
    agent_name: str | None = None,
    operator_authored: bool = False,
) -> str:
    """Render one nudge body: crew variables first, then ``{{STOP_FILE}}``.

    The loop's stored ``message`` keeps its literal tokens, so editing a variable
    changes what the next cycle receives. Variables are substituted FIRST and
    ``{{STOP_FILE}}`` last, which is what makes the sentinel path
    unforgeable from a variable: ``STOP_FILE`` is in
    :data:`kiro_crew.variables.RESERVED_TOKENS` so no variable may be named it,
    and expansion is single-pass, so a value that itself contained
    ``{{STOP_FILE}}`` is inserted verbatim and then resolved by the gateway's own
    replace to the same sentinel every other cycle uses — never to an
    attacker-chosen path.

    *agent_name* is the loop's crew; ``None`` resolves the default crew's layers.
    A configuration failure leaves the message unexpanded rather than stopping
    the loop.
    """
    # `{{STOP_FILE}}` still resolves below either way: it is the runner's own sentinel,
    # not operator config, and a loop that could not find its stop file would never
    # terminate. Only the VARIABLE half is gated.
    if not operator_authored:
        return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")
    try:
        values = resolve_variables(KiroCrewConfig.load(), agent_name or None).values
    except Exception:
        logger.debug("crew-variable resolution failed for nudge; left unexpanded", exc_info=True)
        values = {}
    if values:
        message, unresolved = expand_variables(message, values)
        if unresolved:
            # Left in place on purpose (see variables.expand); a typo hint only.
            logger.debug(
                "nudge message references undefined crew variables: %s",
                ", ".join(sorted(unresolved)),
            )
    return message.replace("{{STOP_FILE}}", stop_sentinel_path or "")


async def compose_nudge_body(
    message: str,
    stop_sentinel_path: str | None,
    slot_key: str | None,
    agent: str | None = None,
    operator_authored: bool = False,
) -> str:
    """Compose one nudge cycle's full body text — the shared fire-path composer.

    Applies :func:`render_nudge_message`'s template substitution and, when the
    loop's session has a non-empty, non-terminal work ledger, prefixes a
    compact snapshot of it so every cycle starts from the durable state
    instead of from transcript memory. Derived server-side at fire time;
    sessions without a ledger render exactly as before.

    *agent* is the crew the loop was ARMED under, threaded through to
    :func:`render_nudge_message` so a loop bound to a non-default crew expands that
    crew's variables rather than the default crew's. ``None`` resolves the default,
    which is the right answer for a genuinely unbound loop.

    The ledger read is filesystem I/O, so it runs in a worker thread — a slow
    or wedged filesystem costs this loop's snapshot, never the event loop.
    Best-effort throughout: a snapshot failure must not cost the nudge itself.
    """
    # Off-loop for the same reason as the ledger read below it: resolving variables
    # does a config load plus a store read and JSON parse, and every caller of this
    # composer is on the event loop.
    body = await asyncio.to_thread(
        render_nudge_message, message, stop_sentinel_path, agent, operator_authored
    )
    if slot_key:
        try:
            snapshot = await asyncio.to_thread(render_snapshot, ledger_key(slot_key))
        except Exception:
            logger.debug("nudge: ledger snapshot failed for %s", slot_key, exc_info=True)
            snapshot = ""
        if snapshot:
            return f"{snapshot}\n\n{body}"
    return body


def _serialize(loop: Any) -> dict:
    return asdict(loop)


# Upper bound on the app id copied into a SEL audit tag. Long enough for any real
# app slug, short enough that a caller cannot pad an audit line with one.
_MAX_AUDIT_APP_ID = 64


def _nudge_source(request: web.Request) -> str:
    """The audited `source` tag for a nudge arriving over REST.

    ``"dashboard"`` only when the operator's own browser session sent it. An app token
    (``request["app"]``, App Kit §5.2) is automation, and `source` is what decides
    ``operator_authored`` downstream — so a hardcoded constant here silently told the
    chokepoint that an app's body was the operator's text.

    Keyed on the same ``request["app"]`` the ownership checks use, so there is one
    notion of who is calling rather than two that can disagree.
    """
    from kiro_crew.dashboard.chat_utils import request_is_operator

    if request_is_operator(request):
        return "dashboard"
    # The app id is caller-supplied and lands in a SEL audit record, so it is coerced
    # and bounded rather than interpolated raw -- an unbounded value would let a caller
    # pad an audit line. The `app:` prefix is what keeps this out of the operator
    # branch: an app literally named "dashboard" still tags as "app:dashboard", which
    # is not equal to "dashboard" and therefore never reads as operator-authored.
    app_id = str(request.get("app", ""))[:_MAX_AUDIT_APP_ID]
    return "app:" + app_id


async def api_autonudge_list(request: web.Request) -> web.Response:
    """GET /api/autonudge — list all active loops."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response({"enabled": False, "loops": []})
    return web.json_response({"enabled": True, "loops": [_serialize(lp) for lp in svc.list_all()]})


async def api_autonudge_get(request: web.Request) -> web.Response:
    """GET /api/autonudge/{slot_key} — loop bound to this slot (or null)."""
    svc = _autonudge_get()
    slot_key = request.match_info["slot_key"]
    if svc is None:
        return web.json_response({"enabled": False, "loop": None})
    loop = svc.get_by_slot(slot_key)
    return web.json_response({"enabled": True, "loop": _serialize(loop) if loop else None})


async def api_autonudge_start(request: web.Request) -> web.Response:
    """POST /api/autonudge — start or replace a loop on a slot.

    Body: { slot_key, message, idle_secs?, max_cycles?, max_runtime_secs?, stop_sentinel_path? }
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled (KIROCREW_AUTONUDGE not set)",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # idle_secs/max_cycles/max_runtime_secs come straight from the request
    # body: int() raises ValueError on "abc", TypeError on null/list, and
    # OverflowError on float("inf") (1e309 is legal JSON in aiohttp's parser),
    # any of which would surface as a 500 instead of a 400. Non-integral
    # floats are rejected rather than silently truncated (int(1.5) -> 1 would
    # store a value the caller never asked for). Coerce up front and reject
    # bad input, matching the sibling handlers_instances.api_instances_add
    # guard on the same pattern.
    try:
        for _name in ("idle_secs", "max_cycles", "max_runtime_secs"):
            _val = body.get(_name)
            if isinstance(_val, float) and not _val.is_integer():
                return web.json_response(
                    {"error": f"{_name} must be a whole number", "code": "not_a_whole_number"},
                    status=400,
                )
        idle_secs = int(body.get("idle_secs", 60))
        max_cycles = int(body.get("max_cycles", 0))
        max_runtime_secs = int(body.get("max_runtime_secs", 0))
    except (TypeError, ValueError, OverflowError):
        return web.json_response(
            {"error": "idle_secs, max_cycles and max_runtime_secs must be integers"}, status=400
        )
    loop, error, status = await authorize_and_add_nudge(
        svc=svc,
        state=state,
        slot_key=(body.get("session_key") or body.get("slot_key") or ""),
        message=(body.get("message") or ""),
        idle_secs=idle_secs,
        max_cycles=max_cycles,
        stop_sentinel_path=(body.get("stop_sentinel_path") or ""),
        max_runtime_secs=max_runtime_secs,
        # Derived, not hardcoded: this route also serves APP tokens, and an app is
        # automation like the MCP and workflow callers. `source` is what decides
        # `operator_authored` downstream, so a constant "dashboard" told the chokepoint
        # an app-authored body was the operator's.
        source=_nudge_source(request),
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_update(request: web.Request) -> web.Response:
    """PATCH /api/autonudge/{loop_id} — update message / idle_secs / active.

    Thin HTTP mapping over ``authorize_and_update_nudge``, which owns the
    message redaction, the integer coercion, and the audit-or-deny policy — see
    its docstring for why those live in the transport-agnostic module and not
    here.
    """
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    loop, error, status = await authorize_and_update_nudge(
        svc=svc,
        loop_id=loop_id,
        message=body.get("message"),
        idle_secs=body.get("idle_secs"),
        max_cycles=body.get("max_cycles"),
        active=body.get("active"),
        max_runtime_secs=body.get("max_runtime_secs"),
        source=_nudge_source(request),
        caller=request.remote or "",
    )
    if error is not None:
        return web.json_response({"error": error}, status=status)
    return web.json_response({"ok": True, "loop": _serialize(loop)})


async def api_autonudge_delete(request: web.Request) -> web.Response:
    """DELETE /api/autonudge/{loop_id} — stop and remove a loop."""
    svc = _autonudge_get()
    if svc is None:
        return web.json_response(
            {
                "error": "auto-nudge disabled",
                "code": "autonudge_disabled",
            },
            status=503,
        )
    loop_id = request.match_info["loop_id"]
    # Capture slot_key for audit before removal (loop is gone after remove()).
    existing = next((lp for lp in svc.list_all() if lp.id == loop_id), None)
    await svc.remove(loop_id)
    sel().log_tool_invocation(
        session_key=existing.slot_key if existing else "",
        source=_nudge_source(request),
        tool_name="autonudge_delete",
        outcome="success" if existing else "noop",
        metadata={"loop_id": loop_id, "caller": request.remote or ""},
    )
    return web.json_response({"ok": True})
