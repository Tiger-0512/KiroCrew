"""Resetting a slot's conversation without destroying its record.

Resume is key-driven -- ``resume_sid = self._session_map.get(key)`` -- and a slot
key is stable by design, so reopening a slot continues the conversation it had.
That is correct for a tab the user closed and came back to, and wrong once a
long-lived conversation has drifted, filled up, or outlived the thing it was
about. Until this route existed the only way to break the link was to DELETE the
session from history, which destroys the record in order to reset the pointer.

Three things decide whether the route is correct, and each has a way of failing
silently rather than loudly:

**Which key it clears.** A slot's session is addressed by
``effective_session_key``, which prefers a channel-born slot's
``linked_session_key``. Deriving ``dashboard:<slot>`` unconditionally instead
yields a key no session ever had, so the call succeeds, reports a reset, and
clears nothing.

**Which verb it uses.** ``discard_conversation`` drops the sid and keeps the
entry, because that entry also carries the channel linkage and the reverse index
built from it. ``destroy`` would take the row with it and silently unlink a
mirrored session.

**Who may ask.** An app may reset only slots it created -- the same boundary
``api_chat_slot_delete`` draws, and for the same reason: a slot's conversation is
its own state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import api_chat_slot_reset_conversation
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

OWNER = "acme-app"
OTHER = "other-app"


def _make_app(state: DashboardState, *, declared_app: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        # Stands in for the token middleware, which publishes the validated app
        # token's name. Empty for a dashboard user.
        request["app"] = declared_app
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
    )
    return app


def _state(*slots: _ChatSlot, subagents: list | None = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {s.key: s for s in slots}
    state.sessions = MagicMock()
    state.sessions.discard_conversation = AsyncMock()
    state.sessions.destroy = AsyncMock()
    if subagents is None:
        # No registry at all: the sub-agent gate has nothing to probe and abstains,
        # which is what ``MagicMock(spec=DashboardState)`` produces on its own since
        # ``subagents`` is assigned in ``__init__``.
        state.subagents = None
    else:
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=subagents)
        subs._queued_depth = MagicMock(return_value=0)
        state.subagents = subs
    return state


def _slot(key: str, *, app: str = "", running: bool = False, linked: str = "") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    if linked:
        slot.linked_session_key = linked
    if running:
        # ``running`` is derived from the live task, which is what the route reads.
        slot.task = MagicMock(done=MagicMock(return_value=False))
    return slot


async def _post(app: web.Application, slot: str):
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/chat/slots/{slot}/reset-conversation")
        body = await resp.json()
        return resp.status, body


class TestItClearsTheRightThing:
    @pytest.mark.asyncio
    async def test_it_clears_the_slot_s_own_session(self):
        state = _state(_slot("chat-1-foo"))

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        assert body["reset"] is True
        state.sessions.discard_conversation.assert_awaited_once_with("dashboard:chat-1-foo")

    @pytest.mark.asyncio
    async def test_a_channel_born_slot_clears_its_channel_session(self):
        """The failure this pins is silent.

        A channel-born slot's turns run on the channel's own session, so its key
        is the channel key. Deriving ``dashboard:<slot>`` instead produces
        ``dashboard:slack:<ts>``, which no session ever had -- the clear finds
        nothing, and the route still answers 200 with ``reset: true``.
        """
        state = _state(_slot("slack_123.456", linked="slack:123.456"))

        status, _ = await _post(_make_app(state), "slack_123.456")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with("slack:123.456")

    @pytest.mark.asyncio
    async def test_it_keeps_the_entry_rather_than_deleting_it(self):
        """``discard_conversation``, never ``destroy``.

        The entry carries the Slack thread/channel linkage and the reverse index
        built from it, so deleting the row to clear a pointer would silently
        unlink a mirrored session -- a reset that costs the caller something it
        never asked to give up.
        """
        state = _state(_slot("chat-1-foo"))

        await _post(_make_app(state), "chat-1-foo")

        state.sessions.destroy.assert_not_awaited()


class TestItRefusesWhenItCannotBeSafe:
    @pytest.mark.asyncio
    async def test_an_unknown_slot_is_not_found(self):
        state = _state(_slot("chat-1-foo"))

        status, body = await _post(_make_app(state), "chat-9-nope")

        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_running_turn_blocks_the_reset(self):
        """Discarding the session a turn is writing into loses that turn's work,
        and the caller cannot tell that from a reset that did nothing."""
        state = _state(_slot("chat-1-foo", running=True))

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 409
        assert body["code"] == "turn_in_flight"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_plan_between_stages_blocks_the_reset(self):
        """``running`` reads False BETWEEN an autopilot plan's stages while the
        plan is still mid-flight, so it alone would discard the conversation the
        plan is writing into and cold-start its next stage."""
        slot = _slot("chat-1-foo")
        slot._in_stage_execution = True
        state = _state(slot)

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 409
        assert body["code"] == "slot_orchestrating"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attached_sub_agents_block_the_reset(self):
        """``discard_conversation`` is a full teardown — it releases the shared
        runtime the parent's children run on. ``running`` is False while they keep
        going, because the parent's turn ends first, so only the sub-agent gate
        catches this."""
        state = _state(_slot("chat-1-foo"), subagents=[{"id": "sub-1"}])

        status, body = await _post(_make_app(state), "chat-1-foo")

        assert status == 409
        assert body["code"] == "slot_subagents_running"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_children_does_not_block(self):
        """The negative half: the gate must not refuse every slot that HAS a
        sub-agent registry, only one with children attached."""
        state = _state(_slot("chat-1-foo"), subagents=[])

        status, _ = await _post(_make_app(state), "chat-1-foo")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with("dashboard:chat-1-foo")


class TestAppScope:
    @pytest.mark.asyncio
    async def test_an_app_may_reset_a_slot_it_created(self):
        state = _state(_slot("acme-obj-1", app=OWNER))

        status, _ = await _post(_make_app(state, declared_app=OWNER), "acme-obj-1")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with("dashboard:acme-obj-1")

    @pytest.mark.asyncio
    async def test_an_app_cannot_reset_another_app_s_slot(self):
        state = _state(_slot("acme-obj-1", app=OWNER))

        status, body = await _post(_make_app(state, declared_app=OTHER), "acme-obj-1")

        # 404, not 403: a foreign slot must be indistinguishable from a missing
        # one, or the error itself enumerates other apps' slots (CWE-204).
        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_app_cannot_reset_an_unscoped_slot(self):
        """A user's own tab is not an app's to reset."""
        state = _state(_slot("chat-1-mine"))

        status, body = await _post(_make_app(state, declared_app=OWNER), "chat-1-mine")

        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_app_cannot_reset_a_channel_session_its_slot_is_bound_to(self):
        """Owning the slot is not owning the session.

        ``get_or_create_slot`` resolves ``linked_session_key`` from the session map
        for a name shaped like a channel stem, so an app that names a live channel
        thread ends up OWNING a slot bound to a conversation it has no claim on.
        Authorizing on slot ownership alone would turn that binding into capability
        escalation — the app could wipe a channel conversation's resume pointer.
        """
        state = _state(_slot("slack_123.456", app=OWNER, linked="slack:123.456"))

        status, body = await _post(_make_app(state, declared_app=OWNER), "slack_123.456")

        assert status == 404
        assert body["code"] == "slot_not_found"
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_dashboard_user_may_reset_an_app_owned_slot(self):
        """The ownership check scopes APPS, not the person whose machine this is."""
        state = _state(_slot("acme-obj-1", app=OWNER))

        status, _ = await _post(_make_app(state, declared_app=""), "acme-obj-1")

        assert status == 200
        state.sessions.discard_conversation.assert_awaited_once_with("dashboard:acme-obj-1")
