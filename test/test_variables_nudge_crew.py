"""The crew a monitor loop was armed under governs its nudge-body expansion.

Without this, a loop armed in a session bound to a non-default crew resolved the
DEFAULT crew's variables, so that crew's tokens were left literal or -- worse --
substituted with another crew's values.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as nudge_mod
from kiro_crew.slack import gateway as gateway_mod


class TestLoopCarriesItsCrew:
    def test_the_field_exists_and_defaults_to_the_default_crew(self):
        assert NudgeLoop("i", "chat-1", "m").agent == ""

    def test_a_persisted_loop_without_the_field_still_loads(self):
        """The store filters raw keys through __dataclass_fields__, so a record
        written before this field takes the default rather than raising."""
        raw = {
            "id": "abc",
            "slot_key": "chat-1",
            "message": "check the PR",
            "idle_secs": 300,
            "unknown_future_key": 1,
        }
        loop = NudgeLoop(**{k: raw[k] for k in raw if k in NudgeLoop.__dataclass_fields__})
        assert loop.agent == ""
        assert loop.message == "check the PR"

    @pytest.mark.asyncio
    async def test_the_armed_crew_survives_arming_end_to_end(self):
        """The signature accepting `agent` proves nothing — the value has to reach
        the stored loop. Both review lanes independently caught the version of this
        code where `add()` took the argument and never forwarded it to
        `_add_locked`, leaving every loop with agent="" and the whole crew-carrying
        path inert while every signature assertion still passed."""
        from kiro_crew.autonudge import AutoNudgeService

        svc = AutoNudgeService.__new__(AutoNudgeService)
        captured: dict[str, object] = {}

        async def _fake_locked(slot_key, message, **kwargs):
            captured.update(kwargs)
            return NudgeLoop("id", slot_key, message, agent=kwargs.get("agent", ""))

        svc._add_locked = _fake_locked  # type: ignore[method-assign]
        svc._inflight_adds = set()
        loop = await AutoNudgeService.add(svc, slot_key="chat-1", message="check", agent="oncall")
        assert captured.get("agent") == "oncall", "add() dropped the armed crew"
        assert loop.agent == "oncall"

    def test_add_accepts_and_stores_the_armed_crew(self):
        from kiro_crew.autonudge import AutoNudgeService

        assert "agent" in inspect.signature(AutoNudgeService.add).parameters
        assert "agent" in inspect.signature(AutoNudgeService._add_locked).parameters

    def test_the_delegation_forwards_the_crew(self):
        """A source guard beside the behavioural test: the forward is one keyword
        that is easy to drop in a later edit and invisible in any output."""
        from kiro_crew.autonudge import AutoNudgeService

        source = inspect.getsource(AutoNudgeService.add)
        assert "agent=agent" in source


class TestFirePathsPassTheCrew:
    def test_every_fire_site_passes_the_loops_crew(self):
        """A fire path that drops the argument silently reverts to the default crew,
        which is invisible in output — so pin all three sites.

        RE-ANCHORED, not relaxed. The sites used to call the renderer directly (and
        then via ``asyncio.to_thread``); they now go through ``compose_nudge_body``,
        the shared fire-path composer, which also prefixes the work-ledger snapshot.
        The property is unchanged — the crew reaches the renderer from every site —
        so this follows the call rather than asserting a shape that no longer exists.
        """
        source = inspect.getsource(gateway_mod)
        # One argument per line now: the sites also pass whether the loop's message
        # is operator-authored. Counts the crew argument itself rather than a run.
        calls = source.count("loop.agent,\n            loop.operator_authored,")
        assert calls == 3, f"expected 3 crew-passing fire sites, found {calls}"
        # The crew-less form must not reappear, in any of the three dispatch shapes
        # this call has now had.
        assert "loop.stop_sentinel_path, loop.slot_key)" not in source
        assert source.count("loop.operator_authored") == 3, (
            "a fire site stopped passing authorship; an agent-armed loop would "
            "then expand the operator's variables into text the agent wrote"
        )
        assert "loop.message, loop.stop_sentinel_path)" not in source
        assert "render_nudge_message, loop.message, loop.stop_sentinel_path\n" not in source

    def test_the_composer_threads_the_crew_to_the_renderer(self):
        """The gateway passing a crew proves nothing if the composer drops it on the
        floor — that is the exact defect both review lanes caught on `add()`."""
        seen: dict[str, object] = {}

        def _fake(message, stop_sentinel_path, agent_name=None, operator_authored=False):
            seen["agent"] = agent_name
            seen["operator_authored"] = operator_authored
            return message

        async def _run():
            with patch.object(nudge_mod, "render_nudge_message", _fake):
                return await nudge_mod.compose_nudge_body("body", "/tmp/stop", None, "oncall")

        assert asyncio.run(_run()) == "body"
        assert seen["agent"] == "oncall", "compose_nudge_body dropped the armed crew"

    def test_the_composer_resolves_variables_off_the_event_loop(self):
        """`render_nudge_message` does a config load plus a store read and JSON parse,
        and every caller of this composer is on the loop."""
        source = inspect.getsource(nudge_mod.compose_nudge_body)
        # Whitespace-insensitive: the call has been reformatted twice as arguments
        # were added, and the property is the offload, not the wrapping.
        assert "asyncio.to_thread( render_nudge_message" in " ".join(
            source.split()
        ), "the renderer is called directly on the event loop"

    def test_the_renderer_still_resolves_the_stop_file_token(self):
        with patch.object(nudge_mod, "resolve_variables", side_effect=RuntimeError("no config")):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                out = nudge_mod.render_nudge_message(
                    "halt at {{STOP_FILE}}", "/tmp/stop", "crew1", operator_authored=True
                )
        assert out == "halt at /tmp/stop"

    def test_the_crew_reaches_resolution(self):
        seen: list[str | None] = []

        def _capture(_cfg, agent_name=None):
            seen.append(agent_name)
            return MagicMock(values={})

        with patch.object(nudge_mod, "resolve_variables", _capture):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                nudge_mod.render_nudge_message("body", "", "oncall", operator_authored=True)
        assert seen == ["oncall"]

    def test_an_empty_crew_resolves_the_default(self):
        seen: list[str | None] = []

        def _capture(_cfg, agent_name=None):
            seen.append(agent_name)
            return MagicMock(values={})

        with patch.object(nudge_mod, "resolve_variables", _capture):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                nudge_mod.render_nudge_message("body", "", "", operator_authored=True)
        assert seen == [None], "an empty crew must resolve the default, not the literal ''"


class TestArmRecordsTheCrew:
    def test_a_channel_binding_key_resolves_its_crew_from_the_session(self):
        """A `slack:`/`discord:` key has no dashboard slot, so `_slots` answers nothing
        and the crew lives only on the session. Without this leg the loop armed with
        agent="" and every nudge body resolved the DEFAULT crew's variables."""
        from kiro_crew import autonudge_authz as authz

        state = MagicMock()
        state._slots = {}
        state.sessions.get_agent.return_value = "oncall"
        assert authz._armed_crew_for(state, "slack:T1:C1:123.45") == "oncall"
        state.sessions.get_agent.assert_called_once_with("slack:T1:C1:123.45")

    def test_a_dashboard_slot_still_wins_over_the_session(self):
        """The slot is the more specific answer and must not be overridden by a
        session record that disagrees."""
        from kiro_crew import autonudge_authz as authz

        state = MagicMock()
        slot = MagicMock()
        slot.agent = "reviewers"
        state._slots = {"chat-1": slot}
        state.sessions.get_agent.return_value = "oncall"
        assert authz._armed_crew_for(state, "chat-1") == "reviewers"
        state.sessions.get_agent.assert_not_called()

    def test_an_unbound_loop_still_resolves_the_default_crew(self):
        """Neither source knows a crew: the empty string is the correct answer and
        means "default crew" downstream, not an error."""
        from kiro_crew import autonudge_authz as authz

        state = MagicMock()
        state._slots = {}
        state.sessions.get_agent.return_value = ""
        assert authz._armed_crew_for(state, "chat-9") == ""

    def test_a_slot_with_an_empty_crew_falls_through_to_the_session(self):
        """A slot can exist and name no crew; that is not an answer, so the session
        still gets asked. Guards the `if armed:` rather than `if slot is not None:`."""
        from kiro_crew import autonudge_authz as authz

        state = MagicMock()
        slot = MagicMock()
        slot.agent = ""
        state._slots = {"chat-1": slot}
        state.sessions.get_agent.return_value = "oncall"
        assert authz._armed_crew_for(state, "chat-1") == "oncall"

    def test_a_failing_session_store_does_not_take_the_arming_down(self):
        """A crew name is an optimisation over the default-crew fallback. Raising here
        would refuse to arm a loop that would otherwise run correctly."""
        from kiro_crew import autonudge_authz as authz

        state = MagicMock()
        state._slots = {}
        state.sessions.get_agent.side_effect = RuntimeError("session store down")
        assert authz._armed_crew_for(state, "slack:T1:C1:123.45") == ""

    def test_a_state_without_a_session_store_is_tolerated(self):
        from kiro_crew import autonudge_authz as authz

        class _Bare:
            _slots: dict = {}

        assert authz._armed_crew_for(_Bare(), "chat-1") == ""

    def test_the_chokepoint_reads_the_real_slots_attribute(self):
        """DashboardState exposes `_slots`; there is no `chat_slots` and no
        __getattr__, so the wrong name silently yields {} and every loop would
        record an empty crew while looking correct."""
        source = inspect.getsource(__import__("kiro_crew.autonudge_authz", fromlist=["x"]))
        assert 'getattr(state, "_slots", None)' in source
        assert "chat_slots" not in source

    def test_the_chokepoint_passes_the_crew_to_add(self):
        source = inspect.getsource(__import__("kiro_crew.autonudge_authz", fromlist=["x"]))
        assert "agent=armed_agent" in source


class TestAgentArmedLoopsDoNotExpand:
    """`monitor_start` reaches the arming chokepoint through the MCP, workflow and
    app paths, so the agent can arm a loop whose message it wrote.

    Expanding that message hands the agent the value of any variable it names — a read
    oracle for a store ``security.py`` deliberately fences it out of. The gate mirrors
    ``_run_chat``'s ``operator_authored``: opt-in, defaulting to False.
    """

    def test_the_default_is_not_operator_authored(self):
        assert NudgeLoop("i", "chat-1", "m").operator_authored is False

    def test_an_agent_authored_body_keeps_its_tokens_literal(self):
        # Resolution returns a REAL value rather than raising. Raising would be
        # swallowed by the renderer's own `except Exception`, leaving the message
        # unexpanded for the wrong reason and passing whether the gate exists or not.
        with patch.object(
            nudge_mod, "resolve_variables", return_value=MagicMock(values={"apiToken": "SEEKRIT"})
        ):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                out = nudge_mod.render_nudge_message("use {{apiToken}}", "/tmp/stop", "crew1")
        assert out == "use {{apiToken}}"
        assert "SEEKRIT" not in out, "the agent was handed a configured value"

    def test_the_stop_file_still_resolves_for_an_agent_armed_loop(self):
        """Only the VARIABLE half is gated. The sentinel is the runner's own path, and
        a loop that could not find its stop file would never terminate."""
        out = nudge_mod.render_nudge_message("halt at {{STOP_FILE}}", "/tmp/stop", "crew1")
        assert out == "halt at /tmp/stop"

    def test_the_same_body_expands_when_the_operator_wrote_it(self):
        """Positive control: without it this class would pass on a build that expands
        nothing at all."""
        with patch.object(
            nudge_mod, "resolve_variables", return_value=MagicMock(values={"apiToken": "V"})
        ):
            with patch.object(
                nudge_mod.KiroCrewConfig, "load", classmethod(lambda cls: MagicMock())
            ):
                out = nudge_mod.render_nudge_message(
                    "use {{apiToken}}", "", "crew1", operator_authored=True
                )
        assert out == "use V"

    @pytest.mark.parametrize(
        "source,expected",
        [
            ("dashboard", True),
            ("workflow", False),
            ("mcp-directive", False),
            ("app:spec-builder", False),
        ],
    )
    def test_only_the_dashboard_source_claims_operator_authorship(self, source, expected):
        """Derived from `source`, which the chokepoint already carries and audits.
        A second discriminator could disagree with it; this pins that it does not."""
        source_text = inspect.getsource(__import__("kiro_crew.autonudge_authz", fromlist=["x"]))
        assert (
            'operator_authored=(source == "dashboard")' in source_text
        ), "the arming chokepoint no longer derives authorship from `source`"
        assert (source == "dashboard") is expected

    def test_the_composer_forwards_authorship_to_the_renderer(self):
        """The gate is inert if the composer drops the flag — the same defect both
        review lanes caught on `add()`."""
        seen: dict[str, object] = {}

        def _fake(message, stop_sentinel_path, agent_name=None, operator_authored=False):
            seen["operator_authored"] = operator_authored
            return message

        async def _run():
            with patch.object(nudge_mod, "render_nudge_message", _fake):
                return await nudge_mod.compose_nudge_body("b", "", None, "crew1", True)

        assert asyncio.run(_run()) == "b"
        assert seen["operator_authored"] is True, "compose_nudge_body dropped authorship"
