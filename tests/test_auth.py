"""Local authentication.

The terminal binds to loopback, which keeps the network out but not whoever is
sitting at the machine — and it holds API keys and a webhook that can post to a
channel. These tests care most about the parts that would be quietly wrong:
hashing, timing, session handling, and what the gate actually lets through.
"""

import time

import pytest
from fastapi.testclient import TestClient

from backend.config import Config
from backend.db import database
from backend.main import create_app
from backend.services import auth


@pytest.fixture
def client(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        yield c


# --- password hashing --------------------------------------------------------

def test_a_password_is_never_stored_as_typed():
    stored = auth.hash_password("hunter2")
    assert "hunter2" not in stored


def test_the_same_password_hashes_differently_each_time():
    """Without a per-password salt, identical passwords share a digest and one
    cracked hash breaks every account that chose it."""
    assert auth.hash_password("same") != auth.hash_password("same")


def test_the_hash_records_its_own_cost():
    """Storing the parameters is what lets the cost rise later without
    invalidating every existing password."""
    stored = auth.hash_password("x")
    assert stored.startswith("$argon2id$")
    assert "m=65536" in stored and "t=3" in stored and "p=4" in stored
    assert auth.verify_password("x", stored)


def test_the_hash_is_memory_hard():
    """PBKDF2 at 600k is only CPU-hard, so a GPU runs thousands of guesses in
    parallel. Argon2id makes every guess hold 64 MiB, which is what stops racks
    of GPUs being the cheap way in."""
    assert auth.hash_password("x").startswith("$argon2id$")
    assert "m=65536" in auth.hash_password("x"), "less than the RFC 9106 profile"


def test_hashes_written_before_the_switch_still_open_the_door():
    """Changing the algorithm must not lock the owner out of their own
    terminal."""
    legacy = auth._pbkdf2_for_test("legacy-passphrase")
    assert auth.verify_password("legacy-passphrase", legacy)
    assert not auth.verify_password("wrong", legacy)


def test_an_old_hash_is_flagged_for_upgrade():
    assert auth.needs_rehash(auth._pbkdf2_for_test("x")) is True


def test_a_current_hash_is_not_rehashed_pointlessly():
    assert auth.needs_rehash(auth.hash_password("x")) is False


def test_verification_accepts_the_right_password():
    assert auth.verify_password("correct horse", auth.hash_password("correct horse"))


def test_verification_rejects_the_wrong_one():
    assert not auth.verify_password("wrong", auth.hash_password("right"))


def test_verification_of_junk_is_false_not_an_exception():
    for stored in (None, "", "garbage", "pbkdf2_sha256$abc", "a$b$c$d", "$$$"):
        assert auth.verify_password("x", stored) is False


def test_an_unknown_algorithm_is_rejected():
    """A stored hash naming md5 must not be honoured just because it parses."""
    assert auth.verify_password("x", "md5$1$00$00") is False


def test_no_digest_is_compared_with_equals():
    """`==` on digests exits early and leaks the matching prefix length.

    Argon2's own verify is constant-time by construction; the legacy PBKDF2
    path has to say so itself, and so does every token and backup-code check.
    """
    import inspect

    for fn in (auth._verify_pbkdf2, auth.check_backup_code,
               auth.verify_totp, auth.SessionStore.valid):
        source = inspect.getsource(fn)
        assert "compare_digest" in source, fn.__name__


# --- sessions ----------------------------------------------------------------

def test_a_new_session_is_valid():
    store = auth.SessionStore()
    assert store.valid(store.create())


def test_an_unknown_token_is_not_valid():
    assert auth.SessionStore().valid("made-up") is False
    assert auth.SessionStore().valid(None) is False


def test_a_session_expires():
    now = [0.0]
    store = auth.SessionStore(ttl=10, clock=lambda: now[0])
    token = store.create()
    now[0] = 11
    assert store.valid(token) is False


def test_revoking_ends_a_session():
    store = auth.SessionStore()
    token = store.create()
    store.revoke(token)
    assert store.valid(token) is False


def test_tokens_are_unpredictable():
    tokens = {auth.new_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 32 for t in tokens)


def test_revoke_all_ends_every_session():
    store = auth.SessionStore()
    tokens = [store.create() for _ in range(3)]
    store.revoke_all()
    assert not any(store.valid(t) for t in tokens)


# --- attempt limiting --------------------------------------------------------

def test_repeated_failures_lock_out():
    limiter = auth.AttemptLimiter(max_attempts=3, lockout=60)
    for _ in range(3):
        assert limiter.locked is False
        limiter.record_failure()
    assert limiter.locked is True


def test_the_lockout_expires():
    now = [0.0]
    limiter = auth.AttemptLimiter(max_attempts=1, lockout=30, clock=lambda: now[0])
    limiter.record_failure()
    assert limiter.locked
    now[0] = 31
    assert limiter.locked is False


def test_a_success_clears_the_count():
    limiter = auth.AttemptLimiter(max_attempts=3)
    limiter.record_failure()
    limiter.record_failure()
    limiter.record_success()
    limiter.record_failure()
    assert limiter.locked is False


# --- setup and login ---------------------------------------------------------

def test_the_first_run_has_no_password(client):
    assert client.get("/api/auth/status").json()["configured"] is False


def test_setting_a_password_signs_you_in(client):
    r = client.post("/api/auth/setup", json={"password": "opensesame"})
    assert r.status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_setup_is_refused_once_a_password_exists(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    # Long enough to clear the length rule, so the refusal it hits is the
    # "already configured" one this test is actually about.
    assert client.post("/api/auth/setup",
                       json={"password": "another-one"}).status_code == 409


def test_a_short_password_is_rejected(client):
    assert client.post("/api/auth/setup", json={"password": "abc"}).status_code == 422


def test_the_right_password_logs_in(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login",
                       json={"password": "opensesame"}).status_code == 200


def test_the_wrong_password_does_not(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401


def test_a_wrong_password_says_nothing_useful(client):
    """"No password set" and "wrong password" must read identically, or the
    difference is free information for whoever is guessing."""
    unset = client.post("/api/auth/login", json={"password": "x"}).json()["detail"]
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    wrong = client.post("/api/auth/login", json={"password": "y"}).json()["detail"]
    assert unset == wrong


def test_logging_out_ends_the_session(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_the_session_cookie_is_not_readable_by_scripts(client):
    """An XSS must not be able to lift the session."""
    r = client.post("/api/auth/setup", json={"password": "opensesame"})
    header = r.headers.get("set-cookie", "")
    assert "httponly" in header.lower()
    assert "samesite=strict" in header.lower()


def test_repeated_failures_lock_the_login_route(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    codes = [
        client.post("/api/auth/login", json={"password": "wrong"}).status_code
        for _ in range(auth.MAX_ATTEMPTS + 1)
    ]
    assert 429 in codes


# --- changing the password ---------------------------------------------------

def test_changing_requires_the_current_password(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    assert client.put("/api/auth/password",
                      json={"current": "wrong", "password": "newpassword"}
                      ).status_code == 401


def test_changing_works_with_the_right_one(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    assert client.put("/api/auth/password",
                      json={"current": "opensesame", "password": "newpassword"}
                      ).status_code == 200
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login",
                       json={"password": "newpassword"}).status_code == 200


def test_the_old_password_stops_working(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.put("/api/auth/password",
               json={"current": "opensesame", "password": "newpassword"})
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login",
                       json={"password": "opensesame"}).status_code == 401


def test_changing_the_password_ends_other_sessions(db_path):
    """Usually the reason for changing it."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as first, TestClient(app) as second:
        first.post("/api/auth/setup", json={"password": "opensesame"})
        second.post("/api/auth/login", json={"password": "opensesame"})
        assert second.get("/api/auth/status").json()["authenticated"] is True

        first.put("/api/auth/password",
                  json={"current": "opensesame", "password": "newpassword"})
        assert second.get("/api/auth/status").json()["authenticated"] is False


# --- the gate ----------------------------------------------------------------

def test_data_routes_are_closed_once_a_password_exists(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    for path in ("/api/prices", "/api/watchlist", "/api/plans", "/api/settings"):
        assert client.get(path).status_code == 401, f"{path} was reachable"


def test_data_routes_open_again_after_logging_in(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"password": "opensesame"})
    assert client.get("/api/watchlist").status_code == 200


def test_the_first_run_is_not_locked_out_of_its_own_setup(client):
    """With no password there is nothing to authenticate against, and a gate
    that closed here would make the app unusable."""
    assert client.get("/api/watchlist").status_code == 200


def test_the_login_page_itself_stays_reachable(client):
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    assert client.get("/").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_static_assets_stay_reachable_when_locked(client):
    """The lock screen is drawn by them."""
    client.post("/api/auth/setup", json={"password": "opensesame"})
    client.post("/api/auth/logout")
    assert client.get("/static/app.js").status_code == 200


# --- biometrics --------------------------------------------------------------

def test_biometrics_are_refused_on_an_ip_origin(db_path):
    """A browser will not accept an IP as a WebAuthn RP ID. Saying so beats a
    cryptic browser error. The base_url matters here: TestClient defaults to
    the hostname "testserver", which would pass."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        body = c.get("/api/auth/status").json()
    assert body["biometric_possible"] is False
    assert "localhost" in body["biometric_hint"]


def test_registration_is_refused_on_an_ip_origin(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        c.post("/api/auth/setup", json={"password": "opensesame"})
        r = c.post("/api/auth/biometric/register/begin")
    assert r.status_code == 400
    assert "hostname" in r.json()["detail"].lower()


def test_ip_literals_are_recognised_as_unusable_rp_ids():
    """Checked against the predicate: TestClient will not take a bracketed
    IPv6 base_url, and contorting the harness would test the harness."""
    from types import SimpleNamespace

    from backend.routes.auth import _webauthn_possible

    def at(host):
        return _webauthn_possible(
            SimpleNamespace(url=SimpleNamespace(hostname=host, port=8000))
        )

    assert at("127.0.0.1") is False
    assert at("::1") is False
    assert at("192.168.1.10") is False
    assert at("localhost") is True
    assert at("roswell.local") is True


def test_a_hostname_origin_can_begin_registration(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app, base_url="http://localhost:8000") as c:
        c.post("/api/auth/setup", json={"password": "opensesame"})
        r = c.post("/api/auth/biometric/register/begin")
        assert r.status_code == 200
        body = r.json()
        assert body["rp"]["id"] == "localhost"
        assert body["authenticatorSelection"]["userVerification"] == "required"
        assert body["authenticatorSelection"]["authenticatorAttachment"] == "platform"


def test_login_without_a_registered_credential_is_refused(db_path):
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app, base_url="http://localhost:8000") as c:
        c.post("/api/auth/setup", json={"password": "opensesame"})
        assert c.post("/api/auth/biometric/login/begin").status_code == 400


def test_finishing_without_a_challenge_is_refused(db_path):
    """Replaying a response without a live challenge must not authenticate."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app, base_url="http://localhost:8000") as c:
        c.post("/api/auth/setup", json={"password": "opensesame"})
        r = c.post("/api/auth/biometric/login/finish", json={"id": "x"})
        assert r.status_code == 400


def test_the_challenge_is_consumed_by_a_failed_attempt(db_path):
    """A challenge that survives its use can be replayed."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app, base_url="http://localhost:8000") as c:
        c.post("/api/auth/setup", json={"password": "opensesame"})
        c.post("/api/auth/biometric/register/begin")
        with database.get_conn(db_path) as conn:
            assert database.get_setting(conn, "auth_webauthn_challenge")
        c.post("/api/auth/biometric/register/finish", json={"id": "nonsense"})
        with database.get_conn(db_path) as conn:
            assert database.get_setting(conn, "auth_webauthn_challenge") is None


# --- the origin problem, surfaced rather than swallowed -----------------------

def test_the_setup_button_is_not_disabled_on_an_unusable_origin(db_path):
    """A disabled button that does nothing on click is indistinguishable from
    a broken one. Reported as "nothing happens"."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        js = c.get("/static/app.js").text
    block = js[js.index("async function renderSecurity"):js.index('$("password-form")')]
    assert '$("bio-register").disabled = !supported' in block


def test_clicking_on_an_ip_origin_explains_and_links_to_localhost(db_path):
    """Same server, different host string — so offer the link rather than a
    sentence to retype."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        js = c.get("/static/app.js").text
    block = js[js.index('$("bio-register").addEventListener'):
               js.index('$("bio-forget").addEventListener')]
    assert "biometric_possible === false" in block
    assert "http://localhost:" in block
    assert "<a href=" in block


def test_the_status_check_happens_before_the_browser_call(db_path):
    """navigator.credentials.create would reject with a SecurityError that
    says nothing about how to fix it."""
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    with TestClient(app) as c:
        js = c.get("/static/app.js").text
    block = js[js.index('$("bio-register").addEventListener'):
               js.index('$("bio-forget").addEventListener')]
    assert block.index("/api/auth/status") < block.index("registerBiometric()")


# --- hardening ---------------------------------------------------------------
#
# Everything below was written after auditing the routes rather than the design
# notes. Two of these are real holes that existed and shipped.

def test_a_stranger_cannot_enrol_their_own_passkey(client):
    """The hole. Every /api/auth/ path is exempt from the global gate so the
    lock screen can reach login, and that exemption covered passkey enrolment
    too — so anyone who could reach the port could register their own
    authenticator and then log in as the owner.

    Enrolment is an account change, not a way in."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()
    assert client.post("/api/auth/biometric/register/begin").status_code == 401
    assert client.post(
        "/api/auth/biometric/register/finish", json={}
    ).status_code == 401


def test_a_stranger_cannot_delete_the_owners_passkey(client):
    """The same exemption let anyone unregister the owner's Touch ID, which is
    a lockout rather than a break-in but is still theirs to do, not a
    stranger's."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()
    assert client.delete("/api/auth/biometric").status_code == 401


def test_the_owner_can_still_enrol(client):
    """The fix must not lock out the person it protects."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    assert client.post("/api/auth/biometric/register/begin").status_code == 200


def test_enrolment_works_on_first_run_before_any_password(client):
    """With no password set there is no session to hold and nothing to
    protect; requiring one would make the app unusable out of the box."""
    assert client.post("/api/auth/biometric/register/begin").status_code == 200


def test_login_stays_reachable_without_a_session(client):
    """Gating this would lock everyone out permanently."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"password": "the-owners-passphrase"}
    ).status_code == 200


def test_the_api_map_is_not_public(client):
    """/openapi.json lists every route and request shape. The owner can read it
    after logging in; a stranger probing the port should not get a map."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401


def test_a_rebinding_host_is_refused(client):
    """Loopback keeps the network out, not a web page: a site can point its own
    hostname at 127.0.0.1 and have the browser make requests for it. The
    browser sends that site's name in Host, so the name is the tell."""
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 421
    assert client.get("/", headers={"Host": "localhost"}).status_code == 200


def test_loopback_names_are_allowed(client):
    for host in ("localhost", "localhost:8000", "127.0.0.1:8000", "app.localhost"):
        assert client.get("/", headers={"Host": host}).status_code == 200, host


def test_the_page_cannot_be_framed(client):
    """Clickjacking: a page that frames the terminal can float invisible
    controls over it and collect the clicks."""
    assert client.get("/").headers["X-Frame-Options"] == "DENY"
    assert client.get("/").headers["X-Content-Type-Options"] == "nosniff"


def test_obvious_passwords_are_refused(client):
    assert client.post(
        "/api/auth/setup", json={"password": "password"}
    ).status_code == 400
    assert client.post(
        "/api/auth/setup", json={"password": "aaaaaaaaaa"}
    ).status_code == 400


# --- second factor -----------------------------------------------------------

def enable_totp(client, password="the-owners-passphrase"):
    client.post("/api/auth/setup", json={"password": password})
    secret = client.post("/api/auth/totp/begin").json()["secret"]
    codes = client.post(
        "/api/auth/totp/enable", json={"code": auth.totp_code(secret)}
    ).json()["backup_codes"]
    return secret, codes


def test_a_password_alone_no_longer_opens_the_terminal(client):
    """The whole point of a second factor: a stolen password is not enough."""
    secret, _ = enable_totp(client)
    client.post("/api/auth/logout")
    client.cookies.clear()

    body = client.post(
        "/api/auth/login", json={"password": "the-owners-passphrase"}
    ).json()
    assert body["totp_required"] is True
    assert body["authenticated"] is False
    assert client.get("/api/watchlist").status_code == 401


def test_the_code_completes_the_login(client):
    secret, _ = enable_totp(client)
    client.post("/api/auth/logout")
    client.cookies.clear()
    client.post("/api/auth/login", json={"password": "the-owners-passphrase"})
    assert client.post(
        "/api/auth/login/totp", json={"code": auth.totp_code(secret)}
    ).status_code == 200
    assert client.get("/api/watchlist").status_code == 200


def test_a_code_cannot_be_used_twice(client):
    """A code stays valid for its whole 30-second step, so without this one
    read over a shoulder is still good."""
    secret, _ = enable_totp(client)
    code = auth.totp_code(secret)
    client.post("/api/auth/logout"); client.cookies.clear()
    client.post("/api/auth/login", json={"password": "the-owners-passphrase"})
    assert client.post("/api/auth/login/totp", json={"code": code}).status_code == 200

    client.post("/api/auth/logout"); client.cookies.clear()
    client.post("/api/auth/login", json={"password": "the-owners-passphrase"})
    again = client.post("/api/auth/login/totp", json={"code": code})
    assert again.status_code == 401
    assert "already been used" in again.json()["detail"]


def test_the_second_step_cannot_be_reached_without_the_first(client):
    """Otherwise the code alone would be a login and the password would be
    decorative."""
    secret, _ = enable_totp(client)
    client.post("/api/auth/logout"); client.cookies.clear()
    assert client.post(
        "/api/auth/login/totp", json={"code": auth.totp_code(secret)}
    ).status_code == 401


def test_a_backup_code_works_exactly_once(client):
    _, codes = enable_totp(client)
    client.post("/api/auth/logout"); client.cookies.clear()
    client.post("/api/auth/login", json={"password": "the-owners-passphrase"})
    assert client.post(
        "/api/auth/login/totp", json={"code": codes[0]}
    ).status_code == 200

    client.post("/api/auth/logout"); client.cookies.clear()
    client.post("/api/auth/login", json={"password": "the-owners-passphrase"})
    assert client.post(
        "/api/auth/login/totp", json={"code": codes[0]}
    ).status_code == 401


def test_backup_codes_are_never_stored_readable(client):
    """The database is a file on disk. A codes list in it would be ten spare
    keys taped to the door."""
    from backend.db import database

    _, codes = enable_totp(client)
    with database.get_conn(client.app.state.cfg.db_path) as conn:
        blob = database.get_setting(conn, "auth_totp_backup")
    for code in codes:
        assert code not in blob
        assert code.replace("-", "") not in blob


def test_the_secret_is_never_returned_once_enabled(client):
    """Handing it back would let anyone with a session clone the factor."""
    enable_totp(client)
    assert "secret" not in client.get("/api/auth/status").json()
    assert client.post("/api/auth/totp/begin").status_code == 409


def test_turning_the_factor_off_costs_the_password(client):
    """Exactly what someone at an unattended session would try."""
    enable_totp(client)
    assert client.request(
        "DELETE", "/api/auth/totp", json={"password": "wrong"}
    ).status_code == 401
    assert client.request(
        "DELETE", "/api/auth/totp", json={"password": "the-owners-passphrase"}
    ).status_code == 200
    assert client.get("/api/auth/status").json()["totp_enabled"] is False


def test_a_half_finished_setup_does_not_lock_anyone_out(client):
    """Nothing is switched on until a code from the new secret verifies."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.post("/api/auth/totp/begin")
    assert client.get("/api/auth/status").json()["totp_enabled"] is False
    client.post("/api/auth/logout"); client.cookies.clear()
    assert client.post(
        "/api/auth/login", json={"password": "the-owners-passphrase"}
    ).json()["authenticated"] is True


def test_a_wrong_code_at_enrolment_is_refused(client):
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.post("/api/auth/totp/begin")
    assert client.post(
        "/api/auth/totp/enable", json={"code": "000000"}
    ).status_code == 401


def test_a_stranger_cannot_start_enrolment(client):
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()
    assert client.post("/api/auth/totp/begin").status_code == 401


# --- the TOTP maths ----------------------------------------------------------

def test_a_code_from_the_previous_step_still_works():
    """Phone clocks drift. One step either side is the usual allowance."""
    secret = auth.new_totp_secret()
    now = 1_800_000_000
    assert auth.verify_totp(secret, auth.totp_code(secret, now - 30), at=now)


def test_a_code_from_five_minutes_ago_does_not():
    secret = auth.new_totp_secret()
    now = 1_800_000_000
    assert auth.verify_totp(secret, auth.totp_code(secret, now - 300), at=now) is None


def test_the_code_matches_rfc_6238():
    """The published test vector. Getting this wrong means every authenticator
    app disagrees with us and nobody can log in."""
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert auth.totp_code(secret, at=59) == "287082"
    assert auth.totp_code(secret, at=1111111109) == "081804"


def test_a_code_is_six_digits():
    assert len(auth.totp_code(auth.new_totp_secret())) == 6


def test_spaces_in_a_typed_code_are_tolerated():
    """Authenticator apps display "123 456"."""
    secret = auth.new_totp_secret()
    now = 1_800_000_000
    code = auth.totp_code(secret, now)
    assert auth.verify_totp(secret, f"{code[:3]} {code[3:]}", at=now) is not None


def test_junk_is_rejected_without_raising():
    secret = auth.new_totp_secret()
    for code in ("", "abc", "12345", "1234567", None):
        assert auth.verify_totp(secret, code) is None


def test_backup_codes_are_all_different():
    codes = auth.new_backup_codes()
    assert len(set(codes)) == len(codes) == auth.BACKUP_CODE_COUNT


# --- lockout and idle timeout ------------------------------------------------

def test_each_lockout_lasts_longer_than_the_last():
    """A flat lockout that reset its counter afterwards allowed eight guesses
    a minute forever — about eleven thousand a day, which is fine against a
    passphrase and not fine against anything a person actually types."""
    clock = [0.0]
    limiter = auth.AttemptLimiter(clock=lambda: clock[0])
    waits = []
    for _ in range(4):
        for _ in range(auth.MAX_ATTEMPTS):
            limiter.record_failure()
        waits.append(limiter.seconds_remaining)
        clock[0] += limiter.seconds_remaining + 1     # wait it out
    assert waits == sorted(waits) and waits[-1] >= waits[0] * 8, waits


def test_the_lockout_has_a_ceiling():
    """Doubling forever would brick the terminal for its owner after a bad
    afternoon."""
    clock = [0.0]
    limiter = auth.AttemptLimiter(clock=lambda: clock[0])
    for _ in range(40):
        # Wait out the previous lockout, then burn another round of guesses, so
        # the assertion below reads a lockout that has just been set rather
        # than one the clock has already run past. The first version of this
        # advanced the clock last and asserted on a remaining time of zero,
        # which passed with the ceiling deleted.
        clock[0] += limiter.seconds_remaining + 1
        for _ in range(auth.MAX_ATTEMPTS):
            limiter.record_failure()
    assert limiter.seconds_remaining > 0, "should be locked right now"
    assert limiter.seconds_remaining <= auth.LOCKOUT_MAX_SECONDS


def test_a_correct_password_clears_the_escalation():
    clock = [0.0]
    limiter = auth.AttemptLimiter(clock=lambda: clock[0])
    for _ in range(auth.MAX_ATTEMPTS * 3):
        limiter.record_failure()
    limiter.record_success()
    for _ in range(auth.MAX_ATTEMPTS):
        limiter.record_failure()
    assert limiter.seconds_remaining <= auth.LOCKOUT_SECONDS


def test_an_idle_session_expires_before_its_absolute_ttl():
    """The absolute TTL bounds a stolen token; the idle one bounds an
    unattended desk."""
    clock = [0.0]
    store = auth.SessionStore(ttl=10_000, idle_timeout=100, clock=lambda: clock[0])
    token = store.create()
    clock[0] = 50
    assert store.valid(token)      # still inside the idle window
    clock[0] = 200
    assert not store.valid(token)  # idle too long, though the TTL has hours left


def test_use_keeps_a_session_alive():
    clock = [0.0]
    store = auth.SessionStore(ttl=10_000, idle_timeout=100, clock=lambda: clock[0])
    token = store.create()
    for _ in range(5):
        clock[0] += 80
        assert store.valid(token), "activity should slide the idle window"


def test_the_absolute_ttl_still_wins_over_activity():
    """Otherwise a session used every minute would live forever."""
    clock = [0.0]
    store = auth.SessionStore(ttl=300, idle_timeout=1000, clock=lambda: clock[0])
    token = store.create()
    clock[0] = 299
    assert store.valid(token)
    clock[0] = 301
    assert not store.valid(token)


def test_backup_codes_are_unguessable():
    """Ten fixed codes would be ten spare keys with the same cut."""
    batches = [auth.new_backup_codes() for _ in range(5)]
    everything = [code for batch in batches for code in batch]
    assert len(set(everything)) == len(everything)


# --- reaching the terminal by another name -----------------------------------

def _host_check(monkeypatch, allowed, db_path):
    monkeypatch.setenv("ROSWELL_ALLOWED_HOSTS", allowed)
    app = create_app(cfg=Config(db_path=db_path), start_poller=False)
    return TestClient(app, base_url="http://localhost")


def test_a_named_host_can_be_allowed(monkeypatch, db_path):
    with _host_check(monkeypatch, "mac.tail1234.ts.net", db_path) as c:
        assert c.get("/", headers={"Host": "mac.tail1234.ts.net"}).status_code == 200
        assert c.get("/", headers={"Host": "evil.example.com"}).status_code == 421


def test_a_leading_dot_allows_any_subdomain(monkeypatch, db_path):
    """A free tunnel hands out a fresh random subdomain on every run. Exact
    matching would mean editing the setting and restarting each time, which is
    how people end up turning the check off entirely."""
    with _host_check(monkeypatch, ".trycloudflare.com", db_path) as c:
        for name in ("fluffy-badger-1234.trycloudflare.com",
                     "quiet-river-9876.trycloudflare.com"):
            assert c.get("/", headers={"Host": name}).status_code == 200


def test_a_suffix_does_not_match_a_lookalike_domain(monkeypatch, db_path):
    """`.trycloudflare.com` must not admit `nottrycloudflare.com` — the dot is
    load-bearing."""
    with _host_check(monkeypatch, ".trycloudflare.com", db_path) as c:
        assert c.get(
            "/", headers={"Host": "eviltrycloudflare.com"}
        ).status_code == 421


def test_localhost_still_works_with_a_suffix_configured(monkeypatch, db_path):
    with _host_check(monkeypatch, ".trycloudflare.com", db_path) as c:
        assert c.get("/", headers={"Host": "localhost:8000"}).status_code == 200


def test_several_names_can_be_allowed_at_once(monkeypatch, db_path):
    with _host_check(monkeypatch, "mac.ts.net, .trycloudflare.com", db_path) as c:
        assert c.get("/", headers={"Host": "mac.ts.net"}).status_code == 200
        assert c.get("/", headers={"Host": "x.trycloudflare.com"}).status_code == 200
        assert c.get("/", headers={"Host": "nope.com"}).status_code == 421


def test_a_rebinding_host_is_refused_before_the_session_is_checked(client):
    """The host check must be the outermost gate.

    Starlette wraps each new middleware around the previous, so the last
    registered runs first. Registered before the session check, the host guard
    never saw an unauthenticated request: the reply was 401, which refuses it
    but means the rebinding check was doing nothing at all.

    The cookie clear is what makes this test work. Two earlier versions
    authenticated first, and then *both* orderings reached the guard and
    answered 421 — they passed against the bug they were written for.
    """
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()

    r = client.get("/api/watchlist", headers={"Host": "evil.example.com"})
    assert r.status_code == 421, (
        "a stranger on a rebinding host got "
        f"{r.status_code}; the host should be refused before the session is "
        "considered"
    )


def test_an_allowed_host_still_reaches_the_session_check(client):
    """The fix must not turn the host guard into the only gate."""
    client.post("/api/auth/setup", json={"password": "the-owners-passphrase"})
    client.cookies.clear()
    assert client.get(
        "/api/watchlist", headers={"Host": "localhost"}
    ).status_code == 401
