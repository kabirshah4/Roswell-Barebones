"""Login, logout, password management, and platform biometrics.

WebAuthn note: a browser will only accept an RP ID that is a registrable suffix
of the page's origin, and an IP address is not one. Touch ID therefore works at
http://localhost:8000 and not at http://127.0.0.1:8000 — the same server, a
different host string. Rather than fail cryptically, the status route reports
whether the current origin can support it.
"""

import base64
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from backend.db import database
from backend.routes import get_db_path
from backend.services import auth as auth_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

PASSWORD_KEY = "auth_password_hash"
CREDENTIALS_KEY = "auth_webauthn_credentials"
CHALLENGE_KEY = "auth_webauthn_challenge"

SESSION_COOKIE = "roswell_session"


TOTP_SECRET_KEY = "auth_totp_secret"
TOTP_BACKUP_KEY = "auth_totp_backup"
TOTP_LAST_KEY = "auth_totp_last_counter"
PENDING_COOKIE = "roswell_pending"


class TotpIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordIn(BaseModel):
    current: str = Field(default="", max_length=256)
    password: str = Field(min_length=auth_service.MIN_PASSWORD_LENGTH, max_length=256)


def _stored_hash(db_path: Path) -> str | None:
    with database.get_conn(db_path) as conn:
        return database.get_setting(conn, PASSWORD_KEY)


def _credentials(db_path: Path) -> list[dict]:
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, CREDENTIALS_KEY)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _save_credentials(db_path: Path, creds: list[dict]) -> None:
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, CREDENTIALS_KEY, json.dumps(creds))


def require_session(request: Request) -> None:
    """Refuse unless the caller already holds a session.

    Every /api/auth/ path is exempt from the global gate, because the lock
    screen has to be able to reach login. That exemption was applied to the
    whole prefix, which left passkey enrolment open: anyone who could reach the
    port could register their own authenticator and then log in as the owner,
    or delete the owner's and lock them out of Touch ID. Enrolment is an
    account change, not a way in, so it belongs behind the session.
    """
    configured = bool(_stored_hash(request.app.state.cfg.db_path)) \
        if hasattr(request.app.state, "cfg") else True
    if not configured:
        # First run: no password exists yet, so there is nothing to protect and
        # no session to hold.
        return
    if not request.app.state.sessions.valid(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="Not authenticated")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True,      # unreachable from JS, so an XSS cannot lift it
        samesite="strict",  # no cross-site request carries it
        max_age=auth_service.SESSION_TTL_SECONDS,
        path="/",
    )


def _rp_id(request: Request) -> str:
    """The WebAuthn Relying Party ID for this origin.

    Browsers reject an IP address here, so 127.0.0.1 cannot register a
    credential at all. Returning the raw host lets the browser produce its own
    (accurate) error rather than us guessing one.
    """
    return (request.url.hostname or "localhost")


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def _webauthn_possible(request: Request) -> bool:
    host = _rp_id(request)
    # An all-numeric-and-dots host is an IPv4 literal; ':' means IPv6.
    return not (host.replace(".", "").isdigit() or ":" in host)


@router.get("/status")
async def status_(request: Request, db_path: Path = Depends(get_db_path)) -> dict:
    limiter = request.app.state.attempt_limiter
    sessions = request.app.state.sessions
    return {
        "configured": bool(_stored_hash(db_path)),
        "authenticated": sessions.valid(request.cookies.get(SESSION_COOKIE)),
        "biometric_registered": len(_credentials(db_path)) > 0,
        "biometric_possible": _webauthn_possible(request),
        "biometric_hint": (
            "" if _webauthn_possible(request)
            else "Touch ID needs a hostname, not an IP. Open http://localhost:"
                 f"{request.url.port or 8000} instead of 127.0.0.1."
        ),
        "locked": limiter.locked,
        "seconds_remaining": limiter.seconds_remaining,
        "min_password_length": auth_service.MIN_PASSWORD_LENGTH,
        "totp_enabled": bool(_totp_secret(db_path)),
        "backup_codes_left": len(_backup_hashes(db_path)),
    }


@router.post("/setup")
async def setup(
    body: ChangePasswordIn, request: Request, response: Response,
    db_path: Path = Depends(get_db_path),
) -> dict:
    """Set the first password. Refused once one exists."""
    if _stored_hash(db_path):
        raise HTTPException(status_code=409, detail="A password is already set")
    problem = auth_service.password_problem(body.password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, PASSWORD_KEY,
                             auth_service.hash_password(body.password))
    token = request.app.state.sessions.create("password")
    _set_session_cookie(response, token)
    return {"authenticated": True}


@router.post("/login")
async def login(
    body: PasswordIn, request: Request, response: Response,
    db_path: Path = Depends(get_db_path),
) -> dict:
    limiter = request.app.state.attempt_limiter
    if limiter.locked:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {limiter.seconds_remaining}s.",
        )

    if not auth_service.verify_password(body.password, _stored_hash(db_path)):
        limiter.record_failure()
        # One message for both "no password set" and "wrong password": telling
        # them apart is free information for whoever is guessing.
        raise HTTPException(status_code=401, detail="Incorrect password")

    limiter.record_success()

    # The password was right, so we hold it in the clear for exactly as long as
    # it takes to re-store it in the current format. This is what moves the
    # existing PBKDF2 hashes to Argon2id without ever asking anyone to reset.
    stored = _stored_hash(db_path)
    if auth_service.needs_rehash(stored):
        with database.get_conn(db_path) as conn:
            database.set_setting(conn, PASSWORD_KEY,
                                 auth_service.hash_password(body.password))

    if _totp_secret(db_path):
        # Password alone must not open the terminal once a second factor is
        # on. This token proves only that step one passed, lives two minutes,
        # and is not a session.
        _set_pending_cookie(response, request.app.state.pending.create("password"))
        return {"authenticated": False, "totp_required": True}

    _set_session_cookie(response, request.app.state.sessions.create("password"))
    return {"authenticated": True}


def _totp_secret(db_path: Path) -> str | None:
    with database.get_conn(db_path) as conn:
        return database.get_setting(conn, TOTP_SECRET_KEY)


def _backup_hashes(db_path: Path) -> list[str]:
    with database.get_conn(db_path) as conn:
        raw = database.get_setting(conn, TOTP_BACKUP_KEY)
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _set_pending_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        PENDING_COOKIE, token, httponly=True, samesite="strict",
        max_age=120, path="/",
    )


@router.post("/login/totp")
async def login_totp(
    body: TotpIn, request: Request, response: Response,
    db_path: Path = Depends(get_db_path),
) -> dict:
    """Step two. Accepts an authenticator code or a single-use backup code."""
    limiter = request.app.state.attempt_limiter
    if limiter.locked:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {limiter.seconds_remaining}s.",
        )
    pending = request.app.state.pending
    token = request.cookies.get(PENDING_COOKIE)
    if not pending.valid(token):
        raise HTTPException(status_code=401, detail="Start again from the password.")

    secret = _totp_secret(db_path)
    if not secret:
        raise HTTPException(status_code=400, detail="No second factor configured")

    counter = auth_service.verify_totp(secret, body.code)
    if counter is not None:
        with database.get_conn(db_path) as conn:
            last = database.get_setting(conn, TOTP_LAST_KEY)
            # A code stays valid for its whole 30-second step, so without this
            # one read over a shoulder is still usable. Each counter is spent
            # once.
            if last is not None and str(counter) == last:
                raise HTTPException(
                    status_code=401, detail="That code has already been used."
                )
            database.set_setting(conn, TOTP_LAST_KEY, str(counter))
    else:
        used = auth_service.check_backup_code(body.code, _backup_hashes(db_path))
        if used is None:
            limiter.record_failure()
            raise HTTPException(status_code=401, detail="Incorrect code")
        remaining = [h for h in _backup_hashes(db_path) if h != used]
        with database.get_conn(db_path) as conn:
            database.set_setting(conn, TOTP_BACKUP_KEY, json.dumps(remaining))

    pending.revoke(token)
    response.delete_cookie(PENDING_COOKIE, path="/")
    limiter.record_success()
    _set_session_cookie(response, request.app.state.sessions.create("password+totp"))
    return {"authenticated": True}


@router.post("/totp/begin")
async def totp_begin(
    request: Request, db_path: Path = Depends(get_db_path),
    _: None = Depends(require_session),
) -> dict:
    """Hand out a candidate secret. Nothing is switched on until a code from it
    verifies, so a half-finished setup cannot lock anyone out."""
    if _totp_secret(db_path):
        raise HTTPException(status_code=409, detail="A second factor is already on")
    secret = auth_service.new_totp_secret()
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, "auth_totp_pending", secret)
    return {
        "secret": secret,
        # Grouped for typing by hand. No QR: rendering one would mean another
        # dependency, and every authenticator app takes a typed key.
        "formatted": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "uri": auth_service.provisioning_uri(secret),
    }


@router.post("/totp/enable")
async def totp_enable(
    body: TotpIn, request: Request, db_path: Path = Depends(get_db_path),
    _: None = Depends(require_session),
) -> dict:
    with database.get_conn(db_path) as conn:
        secret = database.get_setting(conn, "auth_totp_pending")
    if not secret:
        raise HTTPException(status_code=400, detail="No setup in progress")
    if auth_service.verify_totp(secret, body.code) is None:
        raise HTTPException(status_code=401, detail="That code does not match")

    codes = auth_service.new_backup_codes()
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, TOTP_SECRET_KEY, secret)
        database.set_setting(conn, TOTP_BACKUP_KEY,
                             json.dumps([auth_service.hash_backup_code(c)
                                         for c in codes]))
        database.delete_setting(conn, "auth_totp_pending")
    # Returned once and never again — only their hashes are kept.
    return {"enabled": True, "backup_codes": codes}


@router.delete("/totp")
async def totp_disable(
    body: PasswordIn, request: Request, db_path: Path = Depends(get_db_path),
    _: None = Depends(require_session),
) -> dict:
    """Turning a factor off is exactly what an attacker on an unattended
    session would do, so it costs the password."""
    if not auth_service.verify_password(body.password, _stored_hash(db_path)):
        raise HTTPException(status_code=401, detail="Incorrect password")
    with database.get_conn(db_path) as conn:
        for key in (TOTP_SECRET_KEY, TOTP_BACKUP_KEY, TOTP_LAST_KEY,
                    "auth_totp_pending"):
            database.delete_setting(conn, key)
    return {"enabled": False}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> Response:
    request.app.state.sessions.revoke(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/password")
async def change_password(
    body: ChangePasswordIn, request: Request, response: Response,
    db_path: Path = Depends(get_db_path),
) -> dict:
    """Change it. Requires the current one, and ends every other session."""
    stored = _stored_hash(db_path)
    if stored and not auth_service.verify_password(body.current, stored):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    problem = auth_service.password_problem(body.password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    with database.get_conn(db_path) as conn:
        database.set_setting(conn, PASSWORD_KEY,
                             auth_service.hash_password(body.password))

    # A password change should log out anyone holding an old session — that is
    # usually the reason for changing it.
    request.app.state.sessions.revoke_all()
    _set_session_cookie(response, request.app.state.sessions.create("password"))
    return {"changed": True}


# --- WebAuthn ----------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@router.post("/biometric/register/begin")
async def biometric_register_begin(
    request: Request, db_path: Path = Depends(get_db_path),
    _: None = Depends(require_session),
) -> dict:
    if not _webauthn_possible(request):
        raise HTTPException(
            status_code=400,
            detail="Touch ID needs a hostname. Open the app at localhost, not an IP.",
        )
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorAttachment, AuthenticatorSelectionCriteria,
        ResidentKeyRequirement, UserVerificationRequirement,
    )

    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name="Roswell",
        user_id=b"roswell-local-user",
        user_name="roswell",
        user_display_name="Roswell",
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Platform only: the point is Touch ID, not a roaming key.
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, CHALLENGE_KEY, _b64(options.challenge))
    return json.loads(options_to_json(options))


@router.post("/biometric/register/finish")
async def biometric_register_finish(
    request: Request, db_path: Path = Depends(get_db_path),
    _: None = Depends(require_session),
) -> dict:
    from webauthn import verify_registration_response

    body = await request.json()
    with database.get_conn(db_path) as conn:
        challenge = database.get_setting(conn, CHALLENGE_KEY)
        database.delete_setting(conn, CHALLENGE_KEY)
    if not challenge:
        raise HTTPException(status_code=400, detail="No registration in progress")

    try:
        verified = verify_registration_response(
            credential=body,
            expected_challenge=base64.urlsafe_b64decode(challenge + "=="),
            expected_origin=_origin(request),
            expected_rp_id=_rp_id(request),
        )
    except Exception:
        # The library's message can quote raw client data; ours does not.
        raise HTTPException(status_code=400, detail="Registration failed")

    creds = _credentials(db_path)
    creds.append({
        "id": _b64(verified.credential_id),
        "public_key": _b64(verified.credential_public_key),
        "sign_count": verified.sign_count,
    })
    _save_credentials(db_path, creds)
    return {"registered": True, "count": len(creds)}


@router.post("/biometric/login/begin")
async def biometric_login_begin(
    request: Request, db_path: Path = Depends(get_db_path)
) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor, UserVerificationRequirement,
    )

    creds = _credentials(db_path)
    if not creds:
        raise HTTPException(status_code=400, detail="No biometric registered")

    options = generate_authentication_options(
        rp_id=_rp_id(request),
        allow_credentials=[
            PublicKeyCredentialDescriptor(
                id=base64.urlsafe_b64decode(c["id"] + "==")
            )
            for c in creds
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    with database.get_conn(db_path) as conn:
        database.set_setting(conn, CHALLENGE_KEY, _b64(options.challenge))
    return json.loads(options_to_json(options))


@router.post("/biometric/login/finish")
async def biometric_login_finish(
    request: Request, response: Response, db_path: Path = Depends(get_db_path)
) -> dict:
    from webauthn import verify_authentication_response

    limiter = request.app.state.attempt_limiter
    if limiter.locked:
        raise HTTPException(status_code=429, detail="Too many attempts")

    body = await request.json()
    with database.get_conn(db_path) as conn:
        challenge = database.get_setting(conn, CHALLENGE_KEY)
        database.delete_setting(conn, CHALLENGE_KEY)
    if not challenge:
        raise HTTPException(status_code=400, detail="No login in progress")

    creds = _credentials(db_path)
    match = next((c for c in creds if c["id"] == body.get("id")), None)
    if match is None:
        limiter.record_failure()
        raise HTTPException(status_code=401, detail="Unknown credential")

    try:
        verified = verify_authentication_response(
            credential=body,
            expected_challenge=base64.urlsafe_b64decode(challenge + "=="),
            expected_origin=_origin(request),
            expected_rp_id=_rp_id(request),
            credential_public_key=base64.urlsafe_b64decode(match["public_key"] + "=="),
            credential_current_sign_count=match["sign_count"],
            require_user_verification=True,
        )
    except Exception:
        limiter.record_failure()
        raise HTTPException(status_code=401, detail="Biometric check failed")

    # A counter that fails to advance is the signal for a cloned authenticator;
    # the library raises on it, and persisting the new value is what keeps that
    # check meaningful next time.
    match["sign_count"] = verified.new_sign_count
    _save_credentials(db_path, creds)

    limiter.record_success()
    _set_session_cookie(response, request.app.state.sessions.create("biometric"))
    return {"authenticated": True}


@router.delete("/biometric", status_code=status.HTTP_204_NO_CONTENT)
async def forget_biometric(
    db_path: Path = Depends(get_db_path),
    _: None = Depends(require_session),
) -> Response:
    with database.get_conn(db_path) as conn:
        database.delete_setting(conn, CREDENTIALS_KEY)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
