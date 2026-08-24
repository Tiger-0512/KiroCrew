"""Only the operator's own dashboard text may have ``{{name}}`` expanded.

``_run_chat`` is the dashboard turn engine, but it is reached from ~22 call sites,
including the Slack linked-thread route, which hands it a channel participant's raw
message. While the expansion gate keyed only on ``is_slash`` and ``_prompt_depth``
-- both satisfied by an ordinary inbound message -- a participant could send
``{{NAME}}`` and read operator config back off the thread.

The transport ratchet in ``test_variables_channels.py`` could not catch this: the
expansion is not IN a transport module, it is in the dashboard engine the transport
calls. So this file guards the OTHER axis -- who is allowed to ask for expansion.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import kiro_crew.dashboard.chat_runner as runner_mod

SRC = pathlib.Path(runner_mod.__file__).resolve().parents[1]

# The only call sites permitted to pass operator_authored=True. Each is text the
# operator typed into the dashboard themselves. Adding an entry here is a security
# claim and should be argued in review, which is the point of pinning the set.
# The ONLY call site permitted to pass operator_authored=True: the composer POST,
# where the text arrives in an authenticated dashboard request body.
#
# The stored-message replay paths (regenerate, edit-resend, rewind) were briefly on
# this list and are deliberately NOT on it now. A stored user row is not proof the
# operator wrote it: slack/handler.py appends a channel participant's message as a
# user row on the linked-thread path, so replaying a stored row can replay
# participant text. Adding an entry here is a security claim about who can write the
# text that reaches it, and should be argued in review.
ALLOWED_OPT_IN = {
    "dashboard/chat_handlers.py",  # the composer POST
}


def _call_sites() -> list[tuple[str, int, bool]]:
    """Every ``_run_chat(...)`` call in the package, with whether it opts in."""
    found: list[tuple[str, int, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        # as_posix(), not str(): on Windows str() yields "dashboard\\chat_handlers.py",
        # which matches neither the allowlist nor the per-file checks below, so the
        # guard failed on the Windows shard while passing everywhere else.
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "_run_chat":
                continue
            # cli_chat defines an unrelated sync _run_chat(message, model, agent).
            if rel.startswith("cli_chat.py"):
                continue
            opts_in = any(
                kw.arg == "operator_authored"
                # Any value that is not the literal `False` counts as opting in.
                # `api_chat` passes `not request_app` — it serves the dashboard
                # composer AND app tokens through one handler, so the claim is
                # conditional rather than constant. Treating only `True` as opt-in
                # made this read as "the composer stopped opting in", when what it
                # actually stopped doing was claiming authorship for apps.
                and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                for kw in node.keywords
            )
            found.append((rel, node.lineno, opts_in))
    return found


class TestExpansionIsOptIn:
    def test_the_parameter_defaults_to_false(self):
        """The default is the security property: a new caller does not expand."""
        param = inspect.signature(runner_mod._run_chat).parameters["operator_authored"]
        assert param.default is False, "expansion must be opt-in, never opt-out"
        assert (
            param.kind is inspect.Parameter.KEYWORD_ONLY
        ), "keyword-only so it can never be set by accident from position"

    def test_the_gate_requires_it(self):
        """Source guard: the expansion call must sit behind the new conjunct.

        Anchored on the conjunct rather than the whole expression, because the two
        older conjuncts have each been legitimately extended before.
        """
        src = inspect.getsource(runner_mod._run_chat)
        # Anchored on the identifier, not the call syntax: the site is now offloaded
        # via asyncio.to_thread, which passes the helper by name.
        gate = [ln for ln in src.splitlines() if "_expand_message_variables" in ln]
        assert len(gate) == 1, f"expected one expansion site, found {len(gate)}"
        assert (
            "if operator_authored and" in src
        ), "the expansion site is no longer gated on operator_authored"

    def test_only_allowlisted_call_sites_opt_in(self):
        """The enumeration. A new opt-in outside the allowlist fails here."""
        sites = _call_sites()
        assert sites, "found no _run_chat call sites; the walker is broken"
        offenders = sorted(
            {rel for rel, _lineno, opts_in in sites if opts_in and rel not in ALLOWED_OPT_IN}
        )
        assert not offenders, (
            "these call sites claim operator-authored text without being allowlisted: "
            f"{offenders}. If the claim is genuine, add it to ALLOWED_OPT_IN with a "
            "reason; if the text can come from a channel participant, it must not expand."
        )

    def test_the_slack_linked_thread_route_does_not_opt_in(self):
        """The specific regression. This caller passes a participant's raw text."""
        sites = _call_sites()
        slack_sites = [(rel, ln, opt) for rel, ln, opt in sites if rel == "slack/handler.py"]
        assert slack_sites, (
            "slack/handler.py no longer calls _run_chat; if the linked-thread route "
            "moved, re-point this test at its new home rather than deleting it"
        )
        for rel, lineno, opts_in in slack_sites:
            assert not opts_in, (
                f"{rel}:{lineno} opts into variable expansion, but the linked-thread "
                "route forwards a channel participant's message verbatim"
            )

    def test_stored_message_replay_paths_never_opt_in(self):
        """The vector the allowlist alone would not have caught.

        The first version of this fix DID opt these in, reasoning that a stored user
        row is the operator's own composer text. That reasoning was wrong:
        ``slack/handler.py`` appends a channel PARTICIPANT's message as a user row on
        the linked-thread path, so replaying a stored row can replay participant text
        and mirror the expansion back to the thread they read.

        Named per file rather than left to the allowlist, because the allowlist only
        says "these files may opt in" -- it would have happily accepted the wrong
        claim. This asserts the conclusion the claim was wrong about.
        """
        replay = {
            "dashboard/chat_regenerate.py": "regenerate and edit-resend",
            "dashboard/chat_rewind.py": "rewind replay",
        }
        offenders = [
            f"{rel}:{lineno} ({replay[rel]})"
            for rel, lineno, opts_in in _call_sites()
            if opts_in and rel in replay
        ]
        assert not offenders, (
            "a stored-message replay path opts into expansion: "
            f"{offenders}. A stored user row can hold channel-participant text."
        )

    def test_the_composer_does_opt_in(self):
        """Positive control. Without this the suite would pass on a feature that
        never expands anything at all -- which is exactly what an absence-only
        assertion cannot distinguish."""
        sites = _call_sites()
        composer = [
            (rel, ln) for rel, ln, opt in sites if rel == "dashboard/chat_handlers.py" and opt
        ]
        assert composer, (
            "the dashboard composer no longer opts in, so no operator text expands "
            "and the feature is inert"
        )


class TestStubsTrackTheRealSignature:
    """A hand-written ``_run_chat`` stand-in must tolerate extra keyword arguments.

    Adding ``operator_authored`` broke seven test stubs across two files, and the
    breakage surfaced as ``assert 500 == 200`` from the chat API rather than as
    anything mentioning the new parameter -- a TypeError inside the turn becomes a
    500. That cost a CI round, so the tolerance is pinned rather than left to be
    rediscovered the next time the signature grows.

    Scoped to stubs that stand in for the COMPOSER's call, since that is the only
    site passing the keyword; a stub patched over a channel path is unaffected and is
    deliberately not required to change.
    """

    def test_composer_path_stubs_accept_extra_kwargs(self):
        import ast

        offenders: list[str] = []
        for path in sorted((SRC.parent.parent / "test").glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            if "chat_handlers._run_chat" not in text:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.args.kwarg is not None:
                    continue
                # Only STAND-INS, not helpers that merely mention run_chat in their
                # name. `_make_state_for_run_chat` builds a state object and is never
                # called with _run_chat's signature, so requiring **kwargs of it
                # would be noise that teaches the reader to ignore this guard.
                bare = node.name.lstrip("_")
                is_stub = bare.startswith(("fake", "stub", "mock")) and "run_chat" in bare
                if is_stub:
                    offenders.append(f"{path.name}:{node.lineno} {node.name}()")

        assert not offenders, (
            "these stubs stand in for the composer's _run_chat but reject extra "
            f"keyword arguments, so the next parameter added will 500: {offenders}"
        )


class TestAppTokensAreNotTheOperator:
    """`api_chat` serves the dashboard composer AND app tokens (App Kit §5.2).

    An app is automation, like the MCP and workflow callers. Claiming operator
    authorship for it hands an app the value of any variable it names.
    """

    def test_the_claim_is_derived_from_request_app(self):
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat)
        assert "operator_authored=request_is_operator(request)" in src, (
            "api_chat no longer derives operator authorship from the shared predicate; "
            "it serves app tokens too, whose messages the operator did not write"
        )
        assert "operator_authored=True" not in src, "an unconditional claim came back"

    def test_it_reuses_the_ownership_check_s_own_notion_of_the_caller(self):
        """`request_app` is what the App Kit ownership check above already keys on.
        A second notion of who is calling could disagree with the first."""
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat)
        assert "request_is_operator" in src, (
            "the claim must route through the ONE shared predicate, so there is a "
            "single notion of who is calling rather than two that can disagree"
        )


class TestUpdatingAMessageRestampsItsProvenance:
    """The flag is a property of the TEXT, not of the loop.

    An operator-armed loop whose body is later rewritten through an agent-reachable
    path must not keep expanding on text the operator never wrote.
    """

    @pytest.mark.asyncio
    async def test_a_message_update_restamps_rather_than_carries_over(self):
        from kiro_crew.autonudge import AutoNudgeService, NudgeLoop

        svc = AutoNudgeService.__new__(AutoNudgeService)
        loop = NudgeLoop("i", "chat-1", "operator text", operator_authored=True)
        svc._loops = [loop]

        async def _fake_locked(loop_id, *, message=None, operator_authored=False, **kw):
            if message is not None:
                loop.message = message
                loop.operator_authored = operator_authored
            return loop

        svc._update_locked = _fake_locked  # type: ignore[method-assign]
        svc._inflight_adds = set()

        await AutoNudgeService.update(svc, "i", message="agent text")

        assert loop.operator_authored is False, "stale operator provenance survived"
        assert loop.message == "agent text"

    def test_the_update_chokepoint_derives_it_from_source(self):
        import inspect

        from kiro_crew import autonudge_authz

        src = inspect.getsource(autonudge_authz)
        needle = 'operator_authored=(source == "dashboard")'
        assert src.count(needle) == 2, (
            "arming and updating must derive provenance the same way; found "
            f"{src.count(needle)} site(s)"
        )

    def test_the_default_is_the_safe_answer(self):
        """A caller that rewrites the message without saying who wrote it gets False."""
        import inspect

        from kiro_crew.autonudge import AutoNudgeService

        sig = inspect.signature(AutoNudgeService.update)
        assert sig.parameters["operator_authored"].default is False


class TestCronUpdateRestampsProvenance:
    """The same class as the loop case above, in the second carrier.

    Reported chain: an agent creates a job (False), an operator edits it through the
    dashboard (True), then the agent replaces `message` through `cron_update` -- and
    the stored True carried over, so the fenced variables expanded into text the agent
    had just written. Exercised through the REAL store, because the bug was that the
    service silently dropped a kwarg the handler was already passing correctly.
    """

    def _svc(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        svc._load()
        return svc

    def test_an_agent_edit_drops_the_operators_provenance(self, tmp_path):
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="operator text", every_secs=300)
        svc.update_job(job.id, message="operator's own edit", operator_authored=True)
        assert svc.get_job(job.id).operator_authored is True

        # The agent path: an explicit allowlist that carries no authorship key.
        svc.update_job(job.id, message="agent text with {{SECRET}}")

        after = svc.get_job(job.id)
        assert after.message == "agent text with {{SECRET}}"
        assert after.operator_authored is False, (
            "stale operator provenance survived an agent message replace; the fenced "
            "variables would expand into text the agent wrote"
        )

    def test_the_flag_cannot_be_promoted_without_new_text(self, tmp_path):
        """Authorship is a property of the message, so it is only ever consumed
        alongside one. A bare flag is a no-op, not a promotion -- otherwise a caller
        that could set it would re-authorize text somebody else wrote."""
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="agent text", every_secs=300)
        assert job.operator_authored is False

        svc.update_job(job.id, operator_authored=True)

        assert svc.get_job(job.id).operator_authored is False

    def test_a_non_message_update_keeps_the_operators_provenance(self, tmp_path):
        """Not an everything-goes-False pass: rescheduling an operator's job leaves
        their text theirs. A fix that just cleared the flag on every update would
        silently stop expanding an operator's own job."""
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="operator text", every_secs=300, operator_authored=True)

        svc.update_job(job.id, every_secs=600)

        assert svc.get_job(job.id).operator_authored is True

    def test_it_survives_a_save_and_reload(self, tmp_path):
        """The record outlives the process, so the re-stamp has to reach disk."""
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="operator text", every_secs=300, operator_authored=True)
        svc.update_job(job.id, message="agent text")

        from kiro_crew.cron import CronService

        reloaded = CronService(base_dir=tmp_path)
        reloaded._load()
        assert reloaded.get_job(job.id).operator_authored is False


class TestAnAppCannotForgeOperatorAuthorship:
    """The regression my own re-stamp fix introduced, and the reason it is derived now.

    Before the re-stamp, `operator_authored` in kwargs was silently DROPPED by the
    service. Making it take effect turned an ignored kwarg into a forgeable one:
    `CronSDK.update_job` forwards ``**kwargs`` verbatim, so an app could send
    `operator_authored=True` and have fenced values expanded into text it wrote.

    `CronSDK.add_job` never could -- it has an explicit keyword signature. That
    asymmetry between create and update is the whole bug.
    """

    def _svc(self, tmp_path):
        from kiro_crew.cron import CronService

        svc = CronService(base_dir=tmp_path)
        svc._load()
        return svc

    def test_the_service_refuses_the_claim_on_an_app_owned_job(self, tmp_path):
        """Enforced at the service because that is where every caller converges. A
        per-surface strip has to be remembered by each new app-facing surface, and the
        one that forgets fails open."""
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="app text", every_secs=300, created_by="app:evil")

        svc.update_job(job.id, message="{{SECRET}} exfil", operator_authored=True)

        assert svc.get_job(job.id).operator_authored is False, (
            "an app forged operator authorship; the dispatcher would expand fenced "
            "values into text the app wrote"
        )

    def test_an_operators_own_job_still_expands(self, tmp_path):
        """Not an everything-goes-False pass -- that would silently stop expanding the
        jobs this feature exists for."""
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="op text", every_secs=300)

        svc.update_job(job.id, message="new op text", operator_authored=True)

        assert svc.get_job(job.id).operator_authored is True

    def test_a_legacy_job_with_no_creator_is_treated_as_the_operators(self, tmp_path):
        """`created_by` is stamped by the SDK, so its ABSENCE is meaningful rather than
        unknown. Failing closed here would silently stop expanding every job that
        predates app ownership."""
        svc = self._svc(tmp_path)
        job = svc.add_job(name="j", message="op text", every_secs=300, created_by="")

        svc.update_job(job.id, message="edited", operator_authored=True)

        assert svc.get_job(job.id).operator_authored is True

    def test_the_sdk_strips_the_claim_before_it_travels(self):
        from kiro_crew.apps.cron_sdk import _without_authorship_claim

        assert _without_authorship_claim({"message": "m", "operator_authored": True}) == {
            "message": "m"
        }
        # Untouched when absent, so the common path allocates nothing.
        original = {"message": "m"}
        assert _without_authorship_claim(original) is original


class TestEveryCarrierRestampsOnMessageReplace:
    """The ratchet, and the reason this finding is worth a structural test.

    Two modules independently grew a record carrying `operator_authored` beside a
    mutable `message`, and both shipped the same defect: the update path replaced the
    text and left the flag. Fixing the second one by hand leaves the third to be found
    the same way, so the carriers are DISCOVERED here rather than listed -- a new class
    with an `operator_authored` field is picked up automatically and has to satisfy the
    coupling on the day it is written.
    """

    def _carrier_modules(self) -> dict[str, ast.Module]:
        """Modules defining a class with an `operator_authored` field."""
        # Anchored on this file, NOT on the CWD. A relative "src/kiro_crew" resolves
        # against wherever pytest was invoked from, and the failure is asymmetric: the
        # discovery guard below goes red, but the offender scan finds nothing and
        # passes VACUOUSLY -- a ratchet reporting all-clear because it looked in an
        # empty directory. `SRC` is the constant this module already anchors with.
        found: dict[str, ast.Module] = {}
        for path in SRC.rglob("*.py"):
            if "_vendor" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                fields = {
                    t.target.id
                    for t in node.body
                    if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
                }
                if {"operator_authored", "message"} <= fields:
                    found[str(path)] = tree
                    break
        return found

    def test_the_carriers_are_the_ones_we_think(self):
        """Guards the discovery itself: a rename that made this find nothing would
        turn the ratchet below into a silent pass."""
        carriers = self._carrier_modules()
        assert carriers, "found no record carrying operator_authored beside message"
        names = {pathlib.Path(p).name for p in carriers}
        assert {"cron.py", "autonudge.py"} <= names, f"expected both carriers, got {names}"

    def test_no_function_replaces_a_message_without_restamping(self):
        """Structural, because the behavioural tests above only cover the paths
        somebody already thought to write."""
        carriers = self._carrier_modules()
        # A scan that found nothing reports "no offenders", which is indistinguishable
        # from a clean tree. Assert the scan HAPPENED before trusting its verdict.
        assert carriers, "the carrier scan found no modules; its verdict is meaningless"
        offenders: list[str] = []
        for path, tree in carriers.items():
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name == "__init__":
                    continue
                assigns = [
                    a
                    for a in ast.walk(fn)
                    if isinstance(a, ast.Assign)
                    for t in a.targets
                    if isinstance(t, ast.Attribute)
                ]
                writes_message = any(
                    isinstance(t, ast.Attribute) and t.attr == "message"
                    for a in assigns
                    for t in a.targets
                )
                restamps = any(
                    isinstance(t, ast.Attribute) and t.attr == "operator_authored"
                    for a in assigns
                    for t in a.targets
                )
                if writes_message and not restamps:
                    offenders.append(f"{pathlib.Path(path).name}:{fn.lineno} {fn.name}")
        assert not offenders, (
            "these replace a record's `message` without re-stamping "
            "`operator_authored` beside it, so the new text inherits whatever "
            "provenance the old text had: " + ", ".join(sorted(offenders))
        )


class TestEveryPersistedAuthorshipRecordIsFenced:
    """Deriving the flag honestly is worthless if the agent can write the answer.

    The in-process work -- deriving authorship from the request, refusing an app's
    claim, re-stamping on a message change, the AST ratchet below -- all of it is
    undone by editing the record on disk and waiting for a reload. Measured before this
    was fenced: a direct edit setting `"operator_authored": true` survived `_load()`,
    and the dispatcher then expanded fenced variables into text the agent wrote.

    So the flag's integrity is a property of the FILE, not of the code that sets it.
    """

    # The record each carrier persists to, relative to the data home.
    PERSISTED = {
        "cron.py": "cron.json",
        "autonudge.py": "autonudge.json",
    }

    @pytest.mark.parametrize("record", sorted(set(PERSISTED.values())))
    def test_the_record_is_read_and_write_fenced(self, record: str) -> None:
        from kiro_crew import security

        path = str(pathlib.Path.home() / ".kiro" / "crew" / record)
        assert security.is_sensitive_path(
            path
        ), f"{record} persists an authorship decision but the agent can READ it"
        assert security.is_sensitive_write_path(path), (
            f"{record} persists an authorship decision and the agent can WRITE it -- "
            "every in-process guard on that flag is bypassed by editing this file"
        )

    @pytest.mark.parametrize("record", sorted(set(PERSISTED.values())))
    def test_the_shell_gate_refuses_it_too(self, record: str) -> None:
        from kiro_crew import security

        assert security.is_sensitive_bash_command(f"cat ~/.kiro/crew/{record}") is not None

    def test_the_carriers_and_their_records_stay_in_step(self) -> None:
        """The coupling. A third module growing an `operator_authored` field is a third
        file that has to be fenced, and nothing else would say so -- the flag would be
        derived perfectly and forged trivially.
        """
        carriers = {
            pathlib.Path(path).name
            for path in TestEveryCarrierRestampsOnMessageReplace()._carrier_modules()
        }
        unfenced = carriers - set(self.PERSISTED)
        assert not unfenced, (
            f"these carry operator_authored but no fenced record is listed for them: "
            f"{sorted(unfenced)}. Add the file to PERSISTED and to "
            "security._SENSITIVE_HOME_DIRS, or the flag is forgeable on disk."
        )


class TestNoHandlerAssertsOperatorAuthorship:
    """Every dashboard route that can expand `{{name}}` must DERIVE authorship from
    the request, never assert it.

    This is the ratchet the previous five review rounds each lacked. The composer, both
    monitor-loop routes and both cron routes hardcoded the claim, and every one was
    found in a separate round -- because the same routes serve APP tokens (App Kit
    §5.2) as well as the operator's browser, and an app is automation like the MCP and
    workflow callers. Asserting authorship tells the expander an app-authored message
    was the operator's, handing an app the value of any fenced variable it names.

    `request_is_operator` is the one place that answer is computed. A route that
    hardcodes it again fails here rather than in a sixth review round.
    """

    _HANDLER_DIRS = ("dashboard/handlers", "dashboard")

    def _handler_sources(self):
        root = pathlib.Path(__file__).resolve().parent.parent / "src/kiro_crew"
        for rel in self._HANDLER_DIRS:
            for path in sorted((root / rel).glob("*.py")):
                yield path.relative_to(root), path.read_text(encoding="utf-8")

    def test_no_handler_hardcodes_a_truthy_claim(self):
        offenders: list[str] = []
        for rel, src in self._handler_sources():
            tree = ast.parse(src)
            for node in ast.walk(tree):
                # `operator_authored=True` as a call keyword.
                if isinstance(node, ast.keyword) and node.arg == "operator_authored":
                    if isinstance(node.value, ast.Constant) and node.value.value is True:
                        offenders.append(f"{rel}:{node.lineno} operator_authored=True")
                # `"operator_authored": True` in a kwargs dict.
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if (
                            isinstance(k, ast.Constant)
                            and k.value == "operator_authored"
                            and isinstance(v, ast.Constant)
                            and v.value is True
                        ):
                            offenders.append(f'{rel}:{k.lineno} "operator_authored": True')
        assert not offenders, (
            "a dashboard route asserts operator authorship instead of deriving it from "
            f"the request: {offenders}. These routes also serve app tokens — call "
            "`chat_utils.request_is_operator(request)` instead."
        )

    def test_the_routes_that_expand_all_call_the_shared_predicate(self):
        """Positive control. Without it this class would pass on a build where every
        route stopped claiming authorship at all — which is the feature going inert,
        not the hole being closed."""
        root = pathlib.Path(__file__).resolve().parent.parent / "src/kiro_crew"
        expected = {
            "dashboard/chat_handlers.py",  # the composer
            "dashboard/handlers/cron.py",  # create AND update
            "dashboard/handlers/autonudge.py",  # arm AND update, via _nudge_source
        }
        for rel in sorted(expected):
            src = (root / rel).read_text(encoding="utf-8")
            assert "request_is_operator" in src, (
                f"{rel} no longer derives authorship from the request; either it "
                "stopped expanding operator text, or it went back to asserting"
            )

    def test_the_predicate_answers_on_the_app_token(self):
        """Behavioural half: the shared answer is keyed on `request['app']`, the same
        value the App Kit ownership checks use."""
        from kiro_crew.dashboard.chat_utils import request_is_operator

        class _Req(dict):
            pass

        assert request_is_operator(_Req()) is True, "a plain dashboard request"
        assert request_is_operator(_Req(app="")) is True, "an empty app tag"
        assert request_is_operator(_Req(app="spec-builder")) is False, "an app token"


class TestTheNudgeAuditTagCannotImpersonateTheOperator:
    """`source` decides `operator_authored` downstream, so its construction matters."""

    def _tag(self, **kw):
        from kiro_crew.dashboard.handlers.autonudge import _nudge_source

        class _Req(dict):
            pass

        return _nudge_source(_Req(**kw))

    def test_an_operator_request_tags_as_dashboard(self):
        assert self._tag() == "dashboard"

    def test_an_app_named_dashboard_still_tags_as_an_app(self):
        """The prefix is load-bearing: without it an app called `dashboard` would tag
        as the operator and its message would expand."""
        assert self._tag(app="dashboard") == "app:dashboard"
        assert self._tag(app="dashboard") != "dashboard"

    def test_the_app_id_is_bounded(self):
        """Caller-supplied and copied into a SEL audit record, so an unbounded value
        would let a caller pad an audit line."""
        from kiro_crew.dashboard.handlers import autonudge

        tag = self._tag(app="x" * 500)
        assert len(tag) == len("app:") + autonudge._MAX_AUDIT_APP_ID

    def test_a_non_string_app_value_does_not_raise(self):
        assert self._tag(app=123) == "app:123"
