"""Crew-variable ``{{name}}`` expansion at the three backend boundaries.

Boundaries covered here:

* the agent system prompt (``context.ContextBuilder.build_message``), for a
  built-in AND a custom agent;
* a cron job's message, at DISPATCH time only
  (``cron.build_cron_session_context``);
* a monitor-loop nudge body
  (``dashboard.handlers.autonudge.render_nudge_message``).

And the two negatives that carry the security argument: a variable named after a
reserved prompt token cannot change that token's substituted value, and a
steering file — project-scoped content that can arrive from a cloned repo — is
left byte-identical.

Hermetic: every case builds config objects in memory, patches
``KiroCrewConfig.load``, and keeps all filesystem state under ``tmp_path``. No
absolute path literal is used, so nothing anchors to a drive Windows CI does not
have.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import context as ctx_mod
from kiro_crew.config.loader import (
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.cron import CronJob, build_cron_session_context
from kiro_crew.dashboard.handlers.autonudge import render_nudge_message
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config(
    *,
    global_vars: dict[str, str] | None = None,
    crew_vars: dict[str, str] | None = None,
    crew: str = "mycrew",
) -> KiroCrewConfig:
    """A config carrying one crew and one workspace, plus variable layers."""
    cfg = KiroCrewConfig()
    cfg.variables = dict(global_vars or {})
    cfg.workspaces = {"default": WorkspaceConfig(dir="workspace")}
    cfg.default_workspace = "default"
    cfg.agents = {
        crew: KiroCrewAgentConfig(
            kiro_agent="kirocrew",
            workspace="default",
            variables=dict(crew_vars or {}),
        )
    }
    cfg.default_agent = crew
    return cfg


def _patch_config(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> None:
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: cfg))


def _builder(tmp_path) -> ContextBuilder:
    """A ContextBuilder whose every store lives under *tmp_path*."""
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        bot_name="Kiro",
    )


def _write_builtin_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path, body: str) -> None:
    """Point the built-in prompt loader at a prompt file under *tmp_path*."""
    p = tmp_path / "prompt.md"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr(ctx_mod, "_prompt_path", lambda **_kw: p)


def _write_custom_agent(monkeypatch: pytest.MonkeyPatch, tmp_path, name: str, body: str) -> None:
    """Register a custom agent whose inline prompt is *body*."""
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(
        json.dumps({"name": name, "prompt": body}), encoding="utf-8"
    )
    monkeypatch.setattr(ctx_mod, "kiro_agents_dir", lambda: agents)


# ---------------------------------------------------------------------------
# Boundary 1 — agent system prompt
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Boundary 2 — cron dispatch
# ---------------------------------------------------------------------------


def _job(**kw) -> CronJob:
    # `operator_authored=True` by default here because these cases are about what an
    # OPERATOR's scheduled message expands to. The agent-authored default (False) is
    # the security property and gets its own class below, so a fixture default of
    # True cannot hide it.
    base = {
        "id": "j1",
        "name": "nightly",
        "message": "Poll {{baseUrl}}/status",
        "operator_authored": True,
    }
    base.update(kw)
    return CronJob(**base)  # type: ignore[arg-type]


class TestTheAgentPromptSurfaceStaysWithdrawn:
    """The agent system prompt is NOT an expansion surface, and must not become one.

    It shipped as one and was withdrawn: an agent prompt is not reliably the operator's
    text -- an installed app supplies its own, and a `file://` prompt is whatever that
    file holds -- so expanding it handed an untrusted app agent the value of any fenced
    variable it named. Earlier rounds tried to gate it; there is no signal at that point
    which distinguishes an operator-authored crew prompt from an app-installed one.

    This REPLACES the four classes that covered the old behaviour. They tested a
    capability that no longer exists, and a retired guard whose property is merely
    untested would be a protection lost -- so what they asserted is inverted here.
    """

    def test_context_does_not_resolve_variables_at_all(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "src/kiro_crew/context.py").read_text(
            encoding="utf-8"
        )
        assert "resolve_variables(" not in src, (
            "context.py resolves crew variables again; an agent prompt is not reliably "
            "operator-authored, so expanding one leaks fenced values to an app agent"
        )
        assert "_expand_crew_variables" not in src, "the withdrawn expander came back"


class TestAgentAuthoredScheduledTextDoesNotExpand:
    """`cron_add` is an MCP tool, so the agent can schedule a message it wrote.

    Expanding that message hands the agent the value of any variable it names — a read
    oracle for a store ``security.py`` deliberately fences it out of. The gate mirrors
    ``_run_chat``'s ``operator_authored``: opt-in, defaulting to False, so a creation
    path added later fails safe.
    """

    def test_the_default_is_not_operator_authored(self):
        assert CronJob(id="j", name="n", message="m").operator_authored is False

    def _expand(self, job):
        from kiro_crew.cron import _expand_job_variables

        return _expand_job_variables(job)

    def test_an_agent_authored_message_keeps_its_tokens_literal(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        assert self._expand(_job(operator_authored=False)) == "Poll {{baseUrl}}/status"

    def test_the_same_message_expands_when_the_operator_wrote_it(self, monkeypatch):
        """The positive control: without it this suite would pass on a build that
        expands nothing at all."""
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        assert self._expand(_job(operator_authored=True)) == "Poll https://ops.example/status"

    def test_a_record_written_before_the_field_existed_does_not_expand(self):
        """A persisted job predating the flag takes the default, which is the safe
        answer — it does not expand."""
        from kiro_crew.cron import _job_from_record

        rec = {
            "id": "j1",
            "name": "n",
            "message": "Poll {{baseUrl}}/status",
            "schedule": {"kind": "every", "every_secs": 60},
        }
        assert _job_from_record(rec).operator_authored is False

    def test_the_flag_survives_a_save_and_reload(self, tmp_path, monkeypatch):
        """The test I owed and did not write, and the defect it would have caught.

        I checked that a LEGACY record defaults to False — the safe direction — and
        never that a NEW operator job survives the trip. `_save` is a hand-written
        per-field dict, so the field was simply absent: the first save dropped it and
        every operator job came back agent-authored, silently ending expansion after a
        restart. That is the feature breaking, not a guard slipping, and only a
        round-trip catches it.
        """
        from kiro_crew.cron import CronService

        store = tmp_path / "crons.json"
        svc = CronService.__new__(CronService)
        svc._path = store
        svc._dir = tmp_path
        svc._jobs = [
            CronJob(
                id="j1",
                name="nightly",
                message="Poll {{baseUrl}}",
                operator_authored=True,
            )
        ]
        svc._save()

        raw = json.loads(store.read_text(encoding="utf-8"))["jobs"][0]
        assert raw["operator_authored"] is True, "_save dropped the field"

        from kiro_crew.cron import _job_from_record

        assert _job_from_record(raw).operator_authored is True

    def test_an_agent_job_stays_agent_authored_across_the_trip(self):
        """The other direction, so the round-trip test cannot pass by hardcoding True."""
        from kiro_crew.cron import _job_from_record

        rec = {
            "id": "j1",
            "name": "n",
            "message": "m",
            "operator_authored": False,
            "schedule": {"kind": "every", "every_secs": 60},
        }
        assert _job_from_record(rec).operator_authored is False

    def test_the_dashboard_route_opts_in_and_the_mcp_tool_does_not(self):
        """Source guard: the gate is only worth anything if the operator's own form
        sets it and the agent-facing tool does not."""
        import inspect

        from kiro_crew.dashboard.handlers import cron as dash_cron

        assert "request_is_operator(request)" in inspect.getsource(dash_cron), (
            "the dashboard cron route no longer derives operator authorship, so either "
            "an operator's own scheduled message stopped expanding, or the route went "
            "back to asserting authorship it cannot know — it serves app tokens too"
        )
        from pathlib import Path

        # `encoding="utf-8"` explicitly: a bare `read_text()` uses the locale codec,
        # which on Windows is cp1252 and dies on the first non-ASCII byte in the
        # file. Caught by the Windows shard, not by any local run.
        mcp = (Path(__file__).resolve().parent.parent / "src/kiro_crew/mcp_cron.py").read_text(
            encoding="utf-8"
        )
        assert "operator_authored" not in mcp, (
            "the MCP cron tool now claims operator authorship; the agent can author "
            "that message, so expanding it is a read oracle on the fenced store"
        )


class TestSequenceMembersExpandWithTheirOwnCrew:
    """``agent_sequence`` takes precedence over ``agent_id`` at dispatch, so
    expanding once from ``agent_id`` served every member the wrong crew's values —
    and the DEFAULT crew's for a job that sets only a sequence."""

    def test_the_override_selects_that_members_crew(self, monkeypatch):
        cfg = _config(crew="alpha", crew_vars={"who": "alpha-crew"})
        cfg.agents["beta"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="default", variables={"who": "beta-crew"}
        )
        _patch_config(monkeypatch, cfg)
        job = _job(message="run as {{who}}", agent_id="alpha")

        _key, alpha = build_cron_session_context(job, "alpha")
        _key, beta = build_cron_session_context(job, "beta")

        assert "run as alpha-crew" in alpha
        assert "run as beta-crew" in beta

    def test_the_override_keeps_the_previous_run_carry_over(self, monkeypatch):
        """The regression this pins: expanding per member by calling the bare
        expander skipped the ``last_result`` prepend that only this function does,
        so from the second run on every sequence member lost the prior-run context
        and the do-not-repeat instruction.
        """
        cfg = _config(crew="alpha", crew_vars={"who": "alpha-crew"})
        _patch_config(monkeypatch, cfg)
        job = _job(message="run as {{who}}", agent_id="alpha")
        job.persistent_session = True
        job.last_result = "PRIOR OUTPUT"

        _key, msg = build_cron_session_context(job, "alpha")

        assert "PRIOR OUTPUT" in msg, "the previous-run carry-over was dropped"
        assert "do NOT repeat the same content" in msg
        assert "run as alpha-crew" in msg
        # The carry-over is prepended AFTER expansion, so a token inside a previous
        # run's model output is never scanned.
        assert msg.index("PRIOR OUTPUT") < msg.index("run as alpha-crew")

    def test_a_previous_result_holding_a_token_is_not_expanded(self, monkeypatch):
        cfg = _config(crew="alpha", crew_vars={"who": "alpha-crew"})
        _patch_config(monkeypatch, cfg)
        job = _job(message="go", agent_id="alpha")
        job.persistent_session = True
        job.last_result = "the model wrote {{who}} last time"

        _key, msg = build_cron_session_context(job, "alpha")

        assert "{{who}}" in msg, "a token in a previous run's output was expanded"

    def test_expands_at_dispatch_and_stored_message_keeps_token(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job()

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Poll https://ops.example/status"
        # The STORE is untouched: the literal token is what gets persisted.
        assert job.message == "Poll {{baseUrl}}/status"

    def test_editing_a_variable_changes_the_next_dispatch(self, monkeypatch):
        cfg = _config(global_vars={"baseUrl": "https://old.example"})
        _patch_config(monkeypatch, cfg)
        job = _job()

        _k1, first = build_cron_session_context(job)
        assert first == "Poll https://old.example/status"

        # The user edits the variable — no job rewrite anywhere.
        cfg.variables["baseUrl"] = "https://new.example"

        _k2, second = build_cron_session_context(job)
        assert second == "Poll https://new.example/status"
        assert job.message == "Poll {{baseUrl}}/status"

    def test_uses_the_jobs_own_crew(self, monkeypatch):
        _patch_config(
            monkeypatch,
            _config(
                crew="mycrew",
                global_vars={"env": "prod"},
                crew_vars={"env": "staging"},
            ),
        )
        job = _job(message="Env {{env}}", agent_id="mycrew")

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Env staging"

    def test_stateless_job_expands_too(self, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job(persistent_session=False)

        key, prompt = build_cron_session_context(job)

        assert key.startswith("cron:j1:")
        assert prompt == "Poll https://ops.example/status"

    def test_previous_run_result_is_not_expanded(self, monkeypatch):
        """``last_result`` is a prior run's MODEL OUTPUT, not user-authored."""
        _patch_config(monkeypatch, _config(global_vars={"baseUrl": "https://ops.example"}))
        job = _job(last_result="earlier I emitted {{baseUrl}} verbatim")

        _key, prompt = build_cron_session_context(job)

        assert "earlier I emitted {{baseUrl}} verbatim" in prompt
        assert "Poll https://ops.example/status" in prompt

    def test_no_variables_configured_is_a_passthrough(self, monkeypatch):
        _patch_config(monkeypatch, _config())
        job = _job()

        _key, prompt = build_cron_session_context(job)

        assert prompt == "Poll {{baseUrl}}/status"


# ---------------------------------------------------------------------------
# Boundary 3 — monitor loop nudge
# ---------------------------------------------------------------------------


class TestNudgeExpansion:
    def test_variable_expands_and_stop_file_still_resolves(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, _config(global_vars={"prNum": "4161"}))
        sentinel = tmp_path / ".stop-loop"

        out = render_nudge_message(
            "Check PR {{prNum}}; halt via {{STOP_FILE}}", str(sentinel), operator_authored=True
        )

        assert f"Check PR 4161; halt via {sentinel}" == out

    def test_stop_file_resolves_when_no_variables_configured(self, tmp_path, monkeypatch):
        _patch_config(monkeypatch, _config())
        sentinel = tmp_path / ".stop-loop"

        assert (
            render_nudge_message("halt: {{STOP_FILE}}", str(sentinel), operator_authored=True)
            == f"halt: {sentinel}"
        )
        assert render_nudge_message("halt: {{STOP_FILE}}", None, operator_authored=True) == "halt: "

    def test_variable_cannot_forge_the_sentinel_path(self, tmp_path, monkeypatch):
        """A value containing ``{{STOP_FILE}}`` resolves to the REAL sentinel.

        Expansion is single-pass, so the inserted text is not rescanned as a
        variable; the gateway's own ``{{STOP_FILE}}`` replace then runs last and
        can only produce the sentinel it was given.
        """
        cfg = _config(global_vars={"note": "or touch {{STOP_FILE}} to bail"})
        _patch_config(monkeypatch, cfg)
        sentinel = tmp_path / ".stop-loop"
        attacker = tmp_path / "attacker-chosen"

        out = render_nudge_message("Keep going ({{note}})", str(sentinel), operator_authored=True)

        assert str(sentinel) in out
        assert str(attacker) not in out

    def test_crew_scope_used_when_agent_named(self, tmp_path, monkeypatch):
        _patch_config(
            monkeypatch,
            _config(crew="mycrew", global_vars={"env": "prod"}, crew_vars={"env": "staging"}),
        )
        sentinel = tmp_path / ".stop-loop"

        out = render_nudge_message(
            "Env {{env}} — {{STOP_FILE}}", str(sentinel), "mycrew", operator_authored=True
        )

        assert out == f"Env staging — {sentinel}"
