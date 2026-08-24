"""AWS Control P0 — account aggregation, reconnect classification, route gates.

The properties that must hold before anything billable ever ships on this app:

1. **Aggregation is account-shaped and honest.** Profiles group by the account
   the live probe resolves; a probe failure falls back to the account the
   registry recorded (grouping stays stable), and a profile with neither lands
   in the ``unknown`` pseudo-row instead of inventing an account. Summaries
   are explicit nulls — P0 measured nothing, so the page must say "not
   measured", never a fake zero.
2. **The one light per account is derived, not stored**: all-probes-ok → ok,
   any-failed → degraded, no-account → unknown.
3. **Every route is gated** — 403 while the app is disabled, owner-only once
   it is on. Account ids and caller ARNs are what the consent leaf is fenced
   from, so a non-owner (app token, allow-listed messaging user) must not
   read them here either.
4. **Reconnect never executes.** The plan endpoint classifies and returns
   display text; the profile must be REGISTERED or the request 404s, so
   attacker-shaped names are never echoed into a guidance card.
5. **The consent enum grew without changing the mechanism**: ``s3``/``ce``
   are gated services with labels, and their (profile, region) target resolves
   from the deploy registry default — the same resolution the engine will use
   for the calls those grants authorize.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import aws_consent
from kiro_crew.apps.builtins.aws_control.backend import accounts as accounts_mod
from kiro_crew.apps.builtins.aws_control.backend import routes as routes_mod

BASE = "/api/apps/aws-control"

#: The full one-PR contract, as a table — a route added later without a gate
#: fails the inventory assertion rather than shipping open.
P0_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/accounts"),
    ("GET", "/profiles/{name}/reconnect-plan"),
    ("GET", "/drive/{account}"),
    ("GET", "/drive/{account}/list"),
    ("GET", "/drive/{account}/download"),
    ("GET", "/costs/{account}"),
    ("GET", "/library/{account}"),
    ("GET", "/backup/{account}"),
    ("GET", "/shares"),
    ("GET", "/iam-policy"),
    ("POST", "/drive/{account}/bootstrap"),
    ("POST", "/drive/{account}/upload"),
    ("POST", "/drive/{account}/delete"),
    ("POST", "/drive/{account}/share"),
    ("POST", "/shares/{id}/forget"),
    ("POST", "/library/{account}/push"),
    ("POST", "/backup/{account}/run"),
    ("POST", "/backup/{account}/nightly"),
    ("POST", "/backup/{account}/restore"),
)

#: Every POST is a mutation and must also refuse restricted sessions.
MUTATIONS: tuple[tuple[str, str], ...] = tuple((m, p) for (m, p) in P0_ROUTES if m == "POST")


def _registered() -> dict[tuple[str, str], object]:
    app = web.Application()
    routes_mod.register_routes(app)
    return {
        (route.method, str(route.resource.canonical)[len(BASE) :]): route.handler
        for route in app.router.routes()
        if str(route.resource.canonical).startswith(BASE)
        # add_get auto-registers a HEAD twin; the contract names the real verbs.
        and route.method != "HEAD"
    }


def _request(
    method: str,
    path: str,
    *,
    owner: bool = True,
    app_claim: str = "",
    match_info: dict | None = None,
    headers: dict | None = None,
) -> web.Request:
    """A real (mocked) aiohttp request carrying dashboard-owner identity.

    ``is_owner_dashboard_request`` reads ``request.app["state"].owner_id`` and
    the middleware-set ``app``/``user`` keys, so a real Application with a
    state object is attached — a duck-typed stub would make every ``get``
    truthy and take the owner branch unconditionally.
    """
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner-1")
    kwargs: dict = {"app": app}
    if match_info is not None:
        kwargs["match_info"] = match_info
    if headers is not None:
        kwargs["headers"] = headers
    req = make_mocked_request(method, f"{BASE}{path}", **kwargs)
    req["app"] = app_claim
    req["user"] = "owner-1" if owner else "someone-else"
    return req


def _payload(response: web.StreamResponse) -> dict:
    raw = response.body  # type: ignore[attr-defined]
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


def _identity(ok: bool, account: str = "", arn: str = "", detail: str = "") -> aws_consent.Identity:
    return aws_consent.Identity(ok=ok, account=account, arn=arn, detail=detail)


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    accounts_mod.invalidate_cache()
    yield
    accounts_mod.invalidate_cache()


def _registry(entries: list[dict], default: str = "") -> dict:
    return {"version": 2, "profiles": entries, "default": default}


def _entry(name: str, region: str = "us-east-1", account: str = "") -> dict:
    return {"name": name, "region": region, "account": account, "verified_at": "", "note": ""}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def _snapshot(self, registry: dict, identities: dict[str, aws_consent.Identity]) -> dict:
        async def probe(profile: str, region: str, **_kw) -> aws_consent.Identity:
            return identities[profile]

        with (
            mock.patch.object(accounts_mod.deploy_profiles, "load_registry", return_value=registry),
            mock.patch.object(accounts_mod.aws_consent, "probe_identity", side_effect=probe),
            mock.patch.object(
                accounts_mod, "classify_profile", AsyncMock(return_value=accounts_mod.KIND_SSO)
            ),
        ):
            return asyncio.run(accounts_mod.list_accounts(refresh=True))

    def test_profiles_group_by_resolved_account(self):
        snap = self._snapshot(
            _registry([_entry("a"), _entry("b"), _entry("c")], default="a"),
            {
                "a": _identity(True, account="111122223333", arn="arn:a"),
                "b": _identity(True, account="111122223333", arn="arn:b"),
                "c": _identity(True, account="444455556666", arn="arn:c"),
            },
        )
        by_account = {row["account"]: row for row in snap["accounts"]}
        assert set(by_account) == {"111122223333", "444455556666"}
        assert [p["name"] for p in by_account["111122223333"]["profiles"]] == ["a", "b"]
        assert by_account["111122223333"]["health"] == "ok"
        assert snap["totals"] == {"accounts": 2, "profiles": 3, "profilesHealthy": 3}
        # The registry default is marked on its profile, not invented elsewhere.
        assert by_account["111122223333"]["profiles"][0]["default"] is True

    def test_failed_probe_falls_back_to_recorded_account_and_degrades(self):
        snap = self._snapshot(
            _registry([_entry("a"), _entry("b", account="111122223333")]),
            {
                "a": _identity(True, account="111122223333"),
                "b": _identity(False, detail="SSO session expired"),
            },
        )
        (row,) = snap["accounts"]
        assert row["account"] == "111122223333"
        assert row["health"] == "degraded"
        failed = next(p for p in row["profiles"] if p["name"] == "b")
        assert failed["identityOk"] is False
        assert failed["detail"] == "SSO session expired"

    def test_unresolvable_profile_lands_in_unknown_pseudo_row_last(self):
        snap = self._snapshot(
            _registry([_entry("mystery"), _entry("a")]),
            {
                "mystery": _identity(False, detail="no credentials"),
                "a": _identity(True, account="111122223333"),
            },
        )
        assert [row["account"] for row in snap["accounts"]] == ["111122223333", ""]
        unknown = snap["accounts"][-1]
        assert unknown["health"] == "unknown"
        # The pseudo-row is not an account and must not count as one.
        assert snap["totals"]["accounts"] == 1

    def test_summaries_are_explicit_nulls_not_zeros(self):
        snap = self._snapshot(_registry([_entry("a")]), {"a": _identity(True, account="1")})
        summary = snap["accounts"][0]["summary"]
        assert summary == {"storage": None, "sites": None, "tasks": None, "costMonthToDate": None}

    def test_snapshot_is_cached_until_refresh(self):
        registry = _registry([_entry("a")])
        calls = 0

        async def probe(profile: str, region: str, **_kw) -> aws_consent.Identity:
            nonlocal calls
            calls += 1
            return _identity(True, account="1")

        with (
            mock.patch.object(accounts_mod.deploy_profiles, "load_registry", return_value=registry),
            mock.patch.object(accounts_mod.aws_consent, "probe_identity", side_effect=probe),
            mock.patch.object(
                accounts_mod, "classify_profile", AsyncMock(return_value=accounts_mod.KIND_OTHER)
            ),
        ):
            asyncio.run(accounts_mod.list_accounts())
            asyncio.run(accounts_mod.list_accounts())
            assert calls == 1  # served from the snapshot
            asyncio.run(accounts_mod.list_accounts(refresh=True))
            assert calls == 2  # refresh bypasses it


# ---------------------------------------------------------------------------
# Reconnect classification + plan
# ---------------------------------------------------------------------------


class TestReconnect:
    def _classify(self, responses: dict[str, tuple[int, str, str]]) -> str:
        def run_aws(args: list[str], profile: str, timeout: int = 30):
            return responses.get(args[2], (1, "", "not set"))

        with mock.patch.object(accounts_mod.engine, "run_aws", side_effect=run_aws):
            return asyncio.run(accounts_mod.classify_profile("p"))

    def test_sso_session_classifies_sso(self):
        assert self._classify({"sso_session": (0, "my-sso\n", "")}) == accounts_mod.KIND_SSO

    def test_legacy_sso_start_url_classifies_sso(self):
        assert (
            self._classify({"sso_start_url": (0, "https://x.awsapps.com/start\n", "")})
            == accounts_mod.KIND_SSO
        )

    def test_credential_process_classifies_credential_process(self):
        assert (
            self._classify({"credential_process": (0, "/usr/local/bin/tool\n", "")})
            == accounts_mod.KIND_CREDENTIAL_PROCESS
        )

    def test_nothing_set_classifies_other(self):
        assert self._classify({}) == accounts_mod.KIND_OTHER

    def test_plan_is_display_text_naming_the_profile(self):
        plan = accounts_mod.reconnect_plan(accounts_mod.KIND_SSO, "team-prod")
        assert plan["method"] == "terminal"
        assert plan["command"] == "aws sso login --profile team-prod"
        for kind in (accounts_mod.KIND_CREDENTIAL_PROCESS, accounts_mod.KIND_OTHER):
            plan = accounts_mod.reconnect_plan(kind, "team-prod")
            assert "team-prod" in plan["command"]
            assert plan["method"] == "terminal"


# ---------------------------------------------------------------------------
# Route gates
# ---------------------------------------------------------------------------


class TestRouteGates:
    def test_registrar_installs_exactly_the_p0_contract(self):
        assert set(_registered()) == set(P0_ROUTES)

    def test_every_route_refuses_while_disabled(self):
        handlers = _registered()
        with mock.patch.object(routes_mod, "is_app_enabled", return_value=False):
            for (method, path), handler in handlers.items():
                resp = asyncio.run(handler(_request(method, path)))  # type: ignore[operator]
                assert resp.status == 403, (method, path)
                assert _payload(resp)["code"] == "app_disabled"

    def test_every_route_refuses_non_owner_when_enabled(self):
        handlers = _registered()
        with mock.patch.object(routes_mod, "is_app_enabled", return_value=True):
            for (method, path), handler in handlers.items():
                # An app token (non-empty app claim) and a mismatched user are
                # the two callers the consent surface had to shut out.
                for req in (
                    _request(method, path, app_claim="some-app"),
                    _request(method, path, owner=False),
                ):
                    resp = asyncio.run(handler(req))  # type: ignore[operator]
                    assert resp.status == 403, (method, path)
                    assert _payload(resp)["code"] == "dashboard_owner_required"

    def test_accounts_returns_the_snapshot_for_the_owner(self):
        handlers = _registered()
        snapshot = {"accounts": [], "totals": {}, "generatedAt": "now"}
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod, "list_accounts", AsyncMock(return_value=snapshot)
            ) as listed,
        ):
            resp = asyncio.run(
                handlers[("GET", "/accounts")](_request("GET", "/accounts"))  # type: ignore[operator]
            )
        assert resp.status == 200
        assert _payload(resp) == snapshot
        listed.assert_awaited_once_with(refresh=False)

    def test_accounts_refresh_param_bypasses_cache(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.accounts_mod,
                "list_accounts",
                AsyncMock(return_value={"accounts": []}),
            ) as listed,
        ):
            asyncio.run(
                handlers[("GET", "/accounts")](  # type: ignore[operator]
                    _request("GET", "/accounts?refresh=1")
                )
            )
        listed.assert_awaited_once_with(refresh=True)

    def test_reconnect_plan_404s_for_an_unregistered_profile(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles,
                "load_registry",
                return_value=_registry([_entry("real")]),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/{name}/reconnect-plan")](  # type: ignore[operator]
                    _request(
                        "GET",
                        "/profiles/ghost/reconnect-plan",
                        match_info={"name": "ghost"},
                    )
                )
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_profile"

    def test_reconnect_plan_returns_classification_for_registered_profile(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch.object(
                routes_mod.deploy_profiles,
                "load_registry",
                return_value=_registry([_entry("real")]),
            ),
            mock.patch.object(
                routes_mod.accounts_mod,
                "classify_profile",
                AsyncMock(return_value=accounts_mod.KIND_SSO),
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/profiles/{name}/reconnect-plan")](  # type: ignore[operator]
                    _request(
                        "GET",
                        "/profiles/real/reconnect-plan",
                        match_info={"name": "real"},
                    )
                )
            )
        assert resp.status == 200
        plan = _payload(resp)
        assert plan["kind"] == accounts_mod.KIND_SSO
        assert plan["command"] == "aws sso login --profile real"


# ---------------------------------------------------------------------------
# Consent enum extension
# ---------------------------------------------------------------------------


class TestConsentExtension:
    def test_s3_and_ce_are_gated_services_with_labels(self):
        assert aws_consent.SERVICE_S3 in aws_consent.GATED_SERVICES
        assert aws_consent.SERVICE_COST_EXPLORER in aws_consent.GATED_SERVICES
        for service in (aws_consent.SERVICE_S3, aws_consent.SERVICE_COST_EXPLORER):
            assert aws_consent.SERVICE_LABELS[service]

    def test_existing_services_are_untouched(self):
        assert aws_consent.SERVICE_POLLY in aws_consent.GATED_SERVICES
        assert aws_consent.SERVICE_TRANSCRIBE in aws_consent.GATED_SERVICES

    def test_effective_target_resolves_deploy_registry_default(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        with mock.patch.object(
            deploy_profiles, "resolve_profile", return_value=("acct-key", "eu-west-1")
        ):
            for service in (aws_consent.SERVICE_S3, aws_consent.SERVICE_COST_EXPLORER):
                target = asyncio.run(consent_handlers._effective_target(service))
                assert target == ("acct-key", "eu-west-1")

    def test_effective_target_names_the_default_chain_when_registry_is_empty(self):
        from kiro_crew.dashboard.handlers import aws_consent as consent_handlers
        from kiro_crew.deploy import profiles as deploy_profiles

        with mock.patch.object(deploy_profiles, "resolve_profile", return_value=None):
            target = asyncio.run(consent_handlers._effective_target(aws_consent.SERVICE_S3))
        # Empty profile = the CLI default chain, which the card labels
        # explicitly (credential_source names it); the region still defaults.
        assert target == ("", deploy_profiles.DEFAULT_REGION)


# ---------------------------------------------------------------------------
# Storage engine — key validation and presign clamp
# ---------------------------------------------------------------------------


class TestStorageValidation:
    def test_good_keys_pass(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        for key in ("a.txt", "photos 2026/img (1).png", "a/b/c.tar.gz", "x_y" * 3):
            assert storage.validate_key(key) is None, key

    def test_hostile_keys_are_refused(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        for key in (
            "",
            "/etc/passwd",
            "a/../b",
            "a//b",
            ".hidden",
            "..",
            "a/",
            "-flag",
            "a\x00b",
            "x" * 901,
            "a/./b",
        ):
            assert storage.validate_key(key) is not None, key

    def test_section_prefixes_cover_exactly_the_three_sections(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        assert storage.SECTION_PREFIXES == {
            "library": "artifacts/",
            "drive": "drive/",
            "backup": "backup/",
        }

    def test_presign_clamps_expiry_to_the_sigv4_ceiling(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        seen: dict[str, str] = {}

        def checked(args, profile, *, action, timeout=30):
            seen["expires"] = args[args.index("--expires-in") + 1]
            return "https://example.com/signed\n"

        with mock.patch.object(storage, "_checked", side_effect=checked):
            storage.presign("p", "us-east-1", "b", "drive", "k.txt", 10**9)
            assert seen["expires"] == str(storage.PRESIGN_MAX_SECS)
            storage.presign("p", "us-east-1", "b", "drive", "k.txt", 1)
            assert seen["expires"] == "60"

    def test_find_drive_refuses_ambiguity(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage
        from kiro_crew.deploy.engine import AWSError

        two = json.dumps(
            {
                "ResourceTagMappingList": [
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-aaa"},
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-bbb"},
                ]
            }
        )
        with mock.patch.object(storage, "_checked", return_value=two):
            with pytest.raises(AWSError, match="ambiguous"):
                storage.find_drive("p", "us-east-1")

    def test_find_drive_ignores_foreign_naming(self):
        from kiro_crew.apps.builtins.aws_control.backend import storage

        payload = json.dumps(
            {
                "ResourceTagMappingList": [
                    {"ResourceARN": "arn:aws:s3:::stolen-tag-bucket"},
                    {"ResourceARN": "arn:aws:s3:::kirocrew-drive-real"},
                ]
            }
        )
        with mock.patch.object(storage, "_checked", return_value=payload):
            assert storage.find_drive("p", "us-east-1") == "kirocrew-drive-real"


# ---------------------------------------------------------------------------
# New-surface guards: consent, confirm gate, restricted sessions, upload cap
# ---------------------------------------------------------------------------


ACCOUNT = "111122223333"


def _enabled_owner_env():
    """Patches shared by every guarded-surface test: app on, account resolvable."""
    return (
        mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
        mock.patch.object(
            routes_mod.accounts_mod,
            "resolve_account_profile",
            AsyncMock(return_value=("prof", "us-west-2")),
        ),
    )


class TestDriveGuards:
    def test_no_bucket_name_cache_exists(self):
        # Discovery is a trust decision: a cached name would outlive an
        # out-of-band delete + hostile re-creation. Pin its absence.
        assert not hasattr(routes_mod, "_bucket_cache")

    def test_consent_refusal_answers_409_before_any_aws_call(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive") as find,
        ):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", f"/drive/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        assert resp.status == 409
        assert _payload(resp)["code"] == "aws_consent_required"
        find.assert_not_called()

    def test_invalid_account_is_a_400_not_a_probe(self):
        handlers = _registered()
        with mock.patch.object(routes_mod, "is_app_enabled", return_value=True):
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}")](  # type: ignore[operator]
                    _request("GET", "/drive/not-an-id", match_info={"account": "not-an-id"})
                )
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_account"

    def test_bootstrap_without_confirm_previews_and_creates_nothing(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=None),
            mock.patch.object(routes_mod.storage_mod, "create_drive") as create,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/bootstrap", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](req)  # type: ignore[operator]
            )
        assert resp.status == 200
        assert _payload(resp)["preview"] is True
        create.assert_not_called()

    def test_bootstrap_with_confirm_creates_once(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive", return_value=None),
            mock.patch.object(
                routes_mod.storage_mod, "create_drive", return_value="kirocrew-drive-abc"
            ) as create,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/bootstrap", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"confirm": True})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/bootstrap")](req)  # type: ignore[operator]
            )
        assert _payload(resp) == {"created": True, "bucket": "kirocrew-drive-abc"}
        create.assert_called_once()

    def test_every_mutation_refuses_a_restricted_session(self):
        handlers = _registered()
        with (
            mock.patch.object(routes_mod, "is_app_enabled", return_value=True),
            mock.patch(
                "kiro_crew.dashboard.handlers._shared._is_restricted_session",
                return_value=True,
            ),
        ):
            for method, path in MUTATIONS:
                concrete = path.replace("{account}", ACCOUNT).replace("{id}", "x")
                info = {}
                if "{account}" in path:
                    info["account"] = ACCOUNT
                if "{id}" in path:
                    info["id"] = "x"
                req = _request(method, concrete, match_info=info)
                req.json = AsyncMock(return_value={})  # type: ignore[method-assign]
                resp = asyncio.run(handlers[(method, path)](req))  # type: ignore[operator]
                assert resp.status == 403, (method, path)
                assert _payload(resp)["code"] == "restricted_session", (method, path)

    def test_upload_over_the_cap_is_refused_by_header(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
        ):
            req = _request(
                "POST",
                f"/drive/{ACCOUNT}/upload?section=drive&key=big.bin",
                match_info={"account": ACCOUNT},
                headers={"Content-Length": str(routes_mod._MAX_UPLOAD_BYTES + 1)},
            )
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/upload")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "upload_too_large"

    def test_download_urls_are_short_lived(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(
                routes_mod.storage_mod, "presign", return_value="https://signed"
            ) as presign,
        ):
            req = _request(
                "GET",
                f"/drive/{ACCOUNT}/download?section=drive&key=a.txt",
                match_info={"account": ACCOUNT},
            )
            resp = asyncio.run(
                handlers[("GET", "/drive/{account}/download")](req)  # type: ignore[operator]
            )
        assert _payload(resp)["expiresSecs"] == routes_mod._DOWNLOAD_URL_SECS
        assert presign.call_args.args[-1] == routes_mod._DOWNLOAD_URL_SECS


# ---------------------------------------------------------------------------
# Share ledger
# ---------------------------------------------------------------------------


class TestShares:
    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        monkeypatch.setattr(shares, "_store_path", lambda: tmp_path / "shares.json")
        yield

    def test_ledger_records_metadata_never_the_url(self, tmp_path):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        record = shares.record_share(
            account=ACCOUNT,
            section="drive",
            key="a.txt",
            expires_secs=3600,
            note="for alex",
        )
        raw = (tmp_path / "shares.json").read_text(encoding="utf-8")
        assert "https://" not in raw
        assert record["id"] in raw
        assert shares.list_shares(ACCOUNT)[0]["key"] == "a.txt"

    def test_expired_shares_are_pruned_and_forget_removes(self):
        from kiro_crew.apps.builtins.aws_control.backend import shares

        dead = shares.record_share(account=ACCOUNT, section="drive", key="old.txt", expires_secs=60)
        # Backdate the expiry.
        entries = shares._load()
        entries[0]["expiresAt"] = "2000-01-01T00:00:00+00:00"
        shares._save(entries)
        assert shares.list_shares() == []
        assert shares.forget_share(dead["id"]) is None  # already pruned

        live = shares.record_share(
            account=ACCOUNT, section="drive", key="new.txt", expires_secs=3600
        )
        assert shares.forget_share(live["id"]) is not None
        assert shares.list_shares() == []


# ---------------------------------------------------------------------------
# Costs cache
# ---------------------------------------------------------------------------


class TestCosts:
    @pytest.fixture(autouse=True)
    def _isolated_cache(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        monkeypatch.setattr(costs, "_cache_path", lambda account: tmp_path / f"{account}.json")
        yield

    def test_fetch_parses_groups_and_caches(self):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        ce_payload = json.dumps(
            {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Amazon S3"],
                                "Metrics": {"UnblendedCost": {"Amount": "1.25"}},
                            },
                            {"Keys": ["AWS Lambda"], "Metrics": {"UnblendedCost": {"Amount": "0"}}},
                        ]
                    }
                ]
            }
        )
        with mock.patch.object(costs, "_checked", return_value=ce_payload):
            result = costs.fetch_month_costs("p", "us-east-1", ACCOUNT)
        assert result["monthToDate"] == 1.25
        assert result["byService"] == [{"service": "Amazon S3", "amount": 1.25}]
        assert result["projected"] >= result["monthToDate"]
        cached = costs.read_cached(ACCOUNT)
        assert cached is not None and costs.is_fresh(cached)

    def test_stale_cache_is_not_fresh(self):
        from kiro_crew.apps.builtins.aws_control.backend import costs

        assert not costs.is_fresh(None)
        assert not costs.is_fresh({"fetchedAt": "2000-01-01T00:00:00+00:00"})

    def test_costs_endpoint_serves_stale_cache_when_consent_missing(self):
        handlers = _registered()
        stale = {"account": ACCOUNT, "monthToDate": 3.42, "fetchedAt": "2000-01-01T00:00:00+00:00"}
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(routes_mod.costs_mod, "read_cached", return_value=stale),
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=False)
            ),
        ):
            resp = asyncio.run(
                handlers[("GET", "/costs/{account}")](  # type: ignore[operator]
                    _request("GET", f"/costs/{ACCOUNT}", match_info={"account": ACCOUNT})
                )
            )
        body = _payload(resp)
        assert body["fresh"] is False and body["consentMissing"] is True
        assert body["monthToDate"] == 3.42


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


class TestBackup:
    @pytest.fixture(autouse=True)
    def _isolated_state(self, tmp_path, monkeypatch):
        from kiro_crew.apps.builtins.aws_control.backend import backup

        monkeypatch.setattr(backup, "_state_path", lambda: tmp_path / "backup.json")
        yield

    def test_run_rejects_unknown_kind(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
        ):
            req = _request("POST", f"/backup/{ACCOUNT}/run", match_info={"account": ACCOUNT})
            req.json = AsyncMock(return_value={"kind": "everything"})  # type: ignore[method-assign]
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/run")](req)  # type: ignore[operator]
            )
        assert resp.status == 400
        assert _payload(resp)["code"] == "invalid_kind"

    def test_restore_key_must_name_a_backup_archive(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
        ):
            req = _request("POST", f"/backup/{ACCOUNT}/restore", match_info={"account": ACCOUNT})
            req.json = AsyncMock(  # type: ignore[method-assign]
                return_value={"key": "not-a-backup/evil.tar.gz"}
            )
            resp = asyncio.run(
                handlers[("POST", "/backup/{account}/restore")](req)  # type: ignore[operator]
            )
        assert resp.status == 400

    def test_nightly_due_logic(self, tmp_path):
        import datetime as dt

        from kiro_crew.apps.builtins.aws_control.backend import backup

        assert not backup.due_for_nightly(ACCOUNT)  # toggle off
        backup.set_nightly(ACCOUNT, True)
        assert backup.due_for_nightly(ACCOUNT)  # never ran
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/x.tar.gz", 10)
        assert not backup.due_for_nightly(ACCOUNT)  # just ran
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)
        assert backup.due_for_nightly(ACCOUNT, now=future)  # a day later

    def test_backup_state_is_account_scoped(self, tmp_path):
        # Two accounts must not share a nightly toggle or run records —
        # switching the default account cannot make one console report
        # the other account's backups.
        from kiro_crew.apps.builtins.aws_control.backend import backup

        other = "444455556666"
        backup.set_nightly(ACCOUNT, True)
        backup._record_run(ACCOUNT, backup.KIND_SNAPSHOT, "snapshots/a.tar.gz", 1)
        assert not backup.nightly_enabled(other)
        assert backup.last_runs(other) == {}
        assert backup.last_runs(ACCOUNT)[backup.KIND_SNAPSHOT]["key"] == "snapshots/a.tar.gz"


# ---------------------------------------------------------------------------
# Drive IAM tier
# ---------------------------------------------------------------------------


class TestDriveIamTier:
    def test_drive_tier_is_self_contained_and_scoped(self):
        from kiro_crew.deploy import iam

        doc = iam.policy_document(tier="drive")
        sids = [s["Sid"] for s in doc["Statement"]]
        assert sids == ["DriveBucketLevel", "DriveObjectLevel", "DriveDiscovery", "DriveBill"]
        for statement in doc["Statement"]:
            if statement["Sid"].startswith("DriveBucket") or statement["Sid"].startswith(
                "DriveObject"
            ):
                for arn in statement["Resource"]:
                    assert arn.startswith("arn:aws:s3:::kirocrew-drive-"), arn
        # No deploy-web statements leak into the drive tier.
        assert not any("kirocrew-web" in json.dumps(s) for s in doc["Statement"])
        assert not any("cloudfront" in json.dumps(s).lower() for s in doc["Statement"])

    def test_static_tier_is_unchanged_by_the_drive_tier(self):
        from kiro_crew.deploy import iam

        doc = iam.policy_document(tier="static")
        assert not any(s["Sid"].startswith("Drive") for s in doc["Statement"])


# ---------------------------------------------------------------------------
# Round-3 hardening pins
# ---------------------------------------------------------------------------


class TestRound3Hardening:
    def test_hostile_profile_name_never_reaches_a_display_command(self):
        # The registry file is agent-writable config; a name written
        # out-of-band must not appear in copy-into-terminal text.
        plan = accounts_mod.reconnect_plan(accounts_mod.KIND_SSO, "x; touch /tmp/pwn")
        assert plan["command"] == ""
        assert plan["kind"] == accounts_mod.KIND_OTHER

    def test_hostile_profile_name_is_not_classified_via_argv(self):
        with mock.patch.object(accounts_mod.engine, "run_aws") as run:
            kind = asyncio.run(accounts_mod.classify_profile("evil name"))
        assert kind == accounts_mod.KIND_OTHER
        run.assert_not_called()

    def test_classification_failure_degrades_one_profile_not_the_listing(self):
        with mock.patch.object(
            accounts_mod.engine, "run_aws", side_effect=FileNotFoundError("no aws")
        ):
            kind = asyncio.run(accounts_mod.classify_profile("fine-profile"))
        assert kind == accounts_mod.KIND_OTHER

    def test_concurrent_bootstrap_confirms_create_exactly_one_drive(self):
        handlers = _registered()
        created: list[str] = []

        def create(profile, region):
            created.append(profile)
            return f"kirocrew-drive-{len(created)}"

        # First discovery sees nothing; once created, discovery finds it.
        def find(profile, region):
            return f"kirocrew-drive-{len(created)}" if created else None

        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(routes_mod.storage_mod, "find_drive", side_effect=find),
            mock.patch.object(routes_mod.storage_mod, "create_drive", side_effect=create),
        ):

            async def confirm_twice():
                async def one():
                    req = _request(
                        "POST",
                        f"/drive/{ACCOUNT}/bootstrap",
                        match_info={"account": ACCOUNT},
                    )
                    req.json = AsyncMock(return_value={"confirm": True})  # type: ignore[method-assign]
                    return await handlers[("POST", "/drive/{account}/bootstrap")](req)  # type: ignore[operator]

                return await asyncio.gather(one(), one())

            first, second = asyncio.run(confirm_twice())
        assert len(created) == 1
        bodies = [_payload(first), _payload(second)]
        assert any(b.get("created") for b in bodies)
        assert any(b.get("code") == "drive_exists" for b in bodies)

    def test_share_refuses_a_missing_object_and_records_nothing(self):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(routes_mod.storage_mod, "object_exists", return_value=False),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
            mock.patch.object(routes_mod.shares_mod, "record_share") as record,
        ):
            req = _request("POST", f"/drive/{ACCOUNT}/share", match_info={"account": ACCOUNT})
            req.json = AsyncMock(  # type: ignore[method-assign]
                return_value={"section": "drive", "key": "ghost.txt"}
            )
            resp = asyncio.run(
                handlers[("POST", "/drive/{account}/share")](req)  # type: ignore[operator]
            )
        assert resp.status == 404
        assert _payload(resp)["code"] == "unknown_object"
        presign.assert_not_called()
        record.assert_not_called()


# ---------------------------------------------------------------------------
# Round-5 pins: publish governance + credential scan on the egress paths
# ---------------------------------------------------------------------------


class TestPublishGovernance:
    def _egress(self, method: str, path: str, info: dict, body: dict):
        handlers = _registered()
        p1, p2 = _enabled_owner_env()
        with (
            p1,
            p2,
            mock.patch.object(
                routes_mod.aws_consent, "refuse_and_log", AsyncMock(return_value=True)
            ),
            mock.patch.object(
                routes_mod.storage_mod, "find_drive", return_value="kirocrew-drive-abc"
            ),
            mock.patch.object(
                routes_mod, "publish_denied_reason", return_value="capability denied"
            ),
            mock.patch.object(routes_mod.storage_mod, "presign") as presign,
            mock.patch.object(routes_mod.library_mod, "push_artifact") as push,
        ):
            req = _request(method, path, match_info=info)
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
            resp = asyncio.run(handlers[(method, path.split("?")[0].replace(ACCOUNT, "{account}"))](req))  # type: ignore[operator]
        return resp, presign, push

    def test_library_push_is_denied_by_the_publish_gate(self):
        resp, _presign, push = self._egress(
            "POST", f"/library/{ACCOUNT}/push", {"account": ACCOUNT}, {"slug": "x"}
        )
        assert resp.status == 403
        assert _payload(resp)["code"] == "publish_denied"
        push.assert_not_called()

    def test_share_is_denied_by_the_publish_gate(self):
        resp, presign, _push = self._egress(
            "POST",
            f"/drive/{ACCOUNT}/share",
            {"account": ACCOUNT},
            {"section": "drive", "key": "a.txt"},
        )
        assert resp.status == 403
        assert _payload(resp)["code"] == "publish_denied"
        presign.assert_not_called()


class TestLibraryScan:
    def test_credential_bearing_artifact_is_refused(self, tmp_path, monkeypatch):
        from types import SimpleNamespace as NS

        from kiro_crew.apps.builtins.aws_control.backend import library

        monkeypatch.setattr(library, "_ledger_path", lambda: tmp_path / "library.json")
        fake_artifact = NS(
            slug="leaky",
            name="ok",
            kind="text",
            version=1,
            description="",
            tags=[],
            content="aws_secret_access_key = AKIAIOSFODNN7EXAMPLEKEYX",
        )
        with (
            mock.patch.object(library, "get_default_store") as store,
            mock.patch.object(library.storage, "put_file") as put,
        ):
            store.return_value.get.return_value = fake_artifact
            with pytest.raises(ValueError, match="credential-like"):
                library.push_artifact("p", "us-west-2", "b", ACCOUNT, "leaky")
        put.assert_not_called()
