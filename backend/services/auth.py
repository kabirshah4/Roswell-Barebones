"""Local authentication: a password, optionally a platform biometric.

The terminal binds to loopback, which stops the network reaching it but not
anyone sitting at the machine. It holds API keys, a Discord webhook that can
post to a channel, and a watchlist — enough that an unlocked screen is worth a
lock.

Nothing here invents cryptography. Passwords go through PBKDF2-HMAC-SHA256 with
a per-password random salt; comparisons are constant-time; sessions are random
tokens with an expiry rather than anything derived from the password.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from dataclasses import dataclass

import argon2
from argon2 import PasswordHasher

# Argon2id, at the RFC 9106 second-recommended profile (64 MiB, t=3, p=4).
#
# PBKDF2-HMAC-SHA256 at 600k is not a weak hash — it is still OWASP's floor —
# but it is only CPU-hard, so a GPU chews through it in parallel. Argon2id is
# memory-hard: each guess needs 64 MiB held for the duration, which is what
# makes racks of GPUs stop being the cheap way to attack it. Roughly 40ms per
# login here, which nobody notices once.
_hasher = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16
)

# Kept only to verify hashes written before the switch. Nothing new is stored
# in this format; a correct login rewrites it as Argon2id.
ITERATIONS = 600_000
SALT_BYTES = 16
TOKEN_BYTES = 32

# A session outlives a page reload but not a week away from the desk.
SESSION_TTL_SECONDS = 12 * 60 * 60

# Brute force is the realistic attack on a short password typed at a desk.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 30
# Each further lockout doubles. A flat 60s lockout that reset its counter
# afterwards allowed 8 guesses a minute forever, which is ~11,000 a day — fine
# against a 20-character passphrase and not fine against anything a person
# actually types. Doubling turns a sustained attack into hours of waiting.
LOCKOUT_MAX_SECONDS = 60 * 60

# A session outlives making a coffee, not leaving for the afternoon. This is in
# addition to the absolute TTL above: the absolute one bounds a stolen token,
# the idle one bounds an unattended desk.
IDLE_TIMEOUT_SECONDS = 60 * 60

MIN_PASSWORD_LENGTH = 8

# Not a dictionary — a dictionary check belongs to a service with a breach
# corpus. This is the short list that a person guessing at a keyboard would
# actually try first.
_OBVIOUS = frozenset({
    "password", "password1", "passw0rd", "12345678", "123456789", "1234567890",
    "qwertyui", "qwerty123", "letmein1", "trustno1", "iloveyou", "abc12345",
    "obelisk", "obelisk1", "roswell", "roswell1", "roswell123", "terminal",
    "bloomberg", "changeme",
    "admin123", "welcome1", "monkey12", "football", "baseball", "sunshine",
})


def password_problem(password: str) -> str | None:
    """Why this password is refused, or None if it is fine.

    Deliberately not a strength meter. A score out of four teaches people to
    add an exclamation mark; these are the three failures that actually matter
    for a secret typed at a desk.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"At least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in _OBVIOUS:
        return "That is one of the first passwords anyone would try."
    if len(set(password)) < 4:
        return "Too few distinct characters."
    return None


def hash_password(password: str) -> str:
    """Argon2id. The output is self-describing — algorithm, parameters, salt
    and digest — which is what lets the cost be raised later without
    invalidating every existing password."""
    return _hasher.hash(password)


def _verify_pbkdf2(password: str, stored: str) -> bool:
    """Verify a hash written before the move to Argon2id."""
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex),
            int(iterations),
        )
        # compare_digest, not ==: an early-exit comparison leaks the length of
        # the matching prefix through timing.
        return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check against either format. Never raises."""
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        return _verify_pbkdf2(password, stored)
    try:
        return _hasher.verify(stored, password)
    except Exception:
        # VerifyMismatchError, InvalidHash, or anything else malformed — all of
        # them mean the same thing to the caller.
        return False


def _pbkdf2_for_test(password: str, iterations: int = 1000) -> str:
    """Produce a hash in the pre-Argon2 format.

    Only used to prove the upgrade path still opens hashes written before the
    switch — nothing in the app writes this format any more.
    """
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def needs_rehash(stored: str | None) -> bool:
    """True when a correct password should be re-stored in the current format.

    Covers both the old PBKDF2 hashes and Argon2 hashes written at parameters
    that have since been raised.
    """
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except Exception:
        return False


# --- second factor (TOTP, RFC 6238) ------------------------------------------

TOTP_STEP = 30
TOTP_DIGITS = 6
# One step either side, so a slightly wrong clock still works. Wider than this
# and a shoulder-surfed code stays usable for minutes.
TOTP_WINDOW = 1
BACKUP_CODE_COUNT = 10


def new_totp_secret() -> str:
    """160 bits, base32 — what every authenticator app expects."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret: str, at: float | None = None, step: int = TOTP_STEP) -> str:
    counter = int((at if at is not None else time.time()) // step)
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, at: float | None = None,
                window: int = TOTP_WINDOW) -> int | None:
    """Return the counter the code belongs to, or None.

    The counter comes back rather than a bare True so the caller can refuse to
    accept the same one twice — without that, a code read over a shoulder stays
    good for the rest of its 30 seconds.
    """
    cleaned = re.sub(r"\D", "", code or "")
    if len(cleaned) != TOTP_DIGITS:
        return None
    now = at if at is not None else time.time()
    for drift in range(-window, window + 1):
        moment = now + drift * TOTP_STEP
        if hmac.compare_digest(totp_code(secret, moment), cleaned):
            return int(moment // TOTP_STEP)
    return None


def provisioning_uri(secret: str, account: str = "roswell",
                     issuer: str = "Roswell") -> str:
    return (f"otpauth://totp/{issuer}:{account}?secret={secret}"
            f"&issuer={issuer}&algorithm=SHA1&digits={TOTP_DIGITS}"
            f"&period={TOTP_STEP}")


def new_backup_codes(count: int = BACKUP_CODE_COUNT) -> list[str]:
    """Single-use codes for the day the phone is lost.

    There is no password reset in this app by design, so turning on a second
    factor without these would mean a lost phone is a lost terminal.
    """
    return ["-".join(secrets.token_hex(2) for _ in range(2)) for _ in range(count)]


def hash_backup_code(code: str) -> str:
    """SHA-256, not Argon2, and deliberately.

    These are 32 random bits each, generated by the machine — there is no
    dictionary to run against them, so the slow hash buys nothing, and ten
    Argon2 verifications per login attempt would be a self-inflicted denial of
    service.
    """
    return hashlib.sha256(code.replace("-", "").lower().encode()).hexdigest()


def check_backup_code(code: str, hashes: list[str]) -> str | None:
    """Return the matching hash so the caller can burn it, or None."""
    candidate = hash_backup_code(code or "")
    for stored in hashes:
        if hmac.compare_digest(candidate, stored):
            return stored
    return None


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


@dataclass
class Session:
    token: str
    expires_at: float
    method: str          # password | biometric
    last_seen: float = 0.0


class SessionStore:
    """In-memory sessions, deliberately.

    Persisting them would mean a stolen database file is a valid login. Losing
    them on restart is the correct trade for a local app: the cost is typing a
    password again.
    """

    def __init__(self, ttl: float = SESSION_TTL_SECONDS,
                 idle_timeout: float = IDLE_TIMEOUT_SECONDS,
                 clock=time.monotonic) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl
        self._idle = idle_timeout
        self._clock = clock

    def create(self, method: str = "password") -> str:
        self._prune()
        token = new_token()
        now = self._clock()
        self._sessions[token] = Session(
            token=token, expires_at=now + self._ttl, method=method,
            last_seen=now,
        )
        return token

    def valid(self, token: str | None) -> bool:
        """Constant-time in the token, and sliding in the idle window.

        A dict lookup on a secret returns faster for a non-matching prefix. The
        margin is tiny and hard to exploit over loopback, but there are never
        more than a handful of sessions here, so scanning them costs nothing
        and removes the question.
        """
        if not token:
            return False
        # _prune is the single authority on expiry — both the absolute TTL and
        # the idle window. Re-checking them here as well left two mutations
        # alive, because the duplicated conditions were unreachable.
        self._prune()
        now = self._clock()
        for candidate, session in self._sessions.items():
            if hmac.compare_digest(candidate, token):
                session.last_seen = now
                return True
        return False

    def revoke(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        """Used when the password changes: old sessions must not survive it."""
        self._sessions.clear()

    def _prune(self) -> None:
        now = self._clock()
        dead = [
            t for t, s in self._sessions.items()
            if s.expires_at <= now or now - s.last_seen > self._idle
        ]
        for token in dead:
            self._sessions.pop(token, None)


class AttemptLimiter:
    """Locks out after repeated failures, for a while.

    Keyed by nothing: this is a single-user local app, so there is one counter
    and locking it locks the terminal. That is the intent — an attacker at the
    keyboard should not get unlimited guesses.
    """

    def __init__(self, max_attempts: int = MAX_ATTEMPTS,
                 lockout: float = LOCKOUT_SECONDS,
                 ceiling: float = LOCKOUT_MAX_SECONDS,
                 clock=time.monotonic) -> None:
        self._max = max_attempts
        self._lockout = lockout
        self._ceiling = ceiling
        self._clock = clock
        self._failures = 0
        self._lockouts = 0
        self._locked_until = 0.0

    @property
    def locked(self) -> bool:
        return self._clock() < self._locked_until

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self._locked_until - self._clock()))

    @property
    def attempts_left(self) -> int:
        return max(0, self._max - self._failures)

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._max:
            # Doubling, so a patient attacker waits 30s, then a minute, then
            # two... The counter survives the lockout expiring; only a correct
            # password clears it.
            self._lockouts += 1
            wait = min(self._lockout * (2 ** (self._lockouts - 1)), self._ceiling)
            self._locked_until = self._clock() + wait
            self._failures = 0

    def record_success(self) -> None:
        self._failures = 0
        self._lockouts = 0
        self._locked_until = 0.0
