"""Unit tests for ``ads_engine.api.security``: argon2id passwords, sessions, CSRF, login limit.

Each test asserts behaviour the module's docstrings document:

* passwords: :func:`hash_password` writes argon2id with the library defaults;
  :func:`verify_password` matches, mismatches, flags outdated parameters for a rehash, and
  turns an unreadable or non-ASCII stored hash into a logged mismatch rather than an
  exception; a lone surrogate in the candidate never raises; :func:`verify_dummy` spends one
  verification against a current-parameter hash that can never match;
* sessions: :func:`start_session`, :func:`read_session` and :func:`end_session` over a
  hand-built ASGI scope and through Starlette's ``SessionMiddleware``, covering the absolute
  12 h lifetime, malformed claims and naive datetimes;
* :func:`csrf_ok` on safe and mutating methods, and :func:`client_ip`, which reads only the
  ASGI peer;
* :class:`LoginRateLimiter` on an injected clock: the sliding window, ``retry_after`` bounds,
  resets, idle pruning, the key ceiling with its once-per-episode warning, and concurrent use.

No test reads settings, the database or the network. Every argon2 hash is computed at run
time, so no PHC hash string appears in this file, and every address is from a documentation
range (RFC 5737, RFC 3849).
"""

import math
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import httpx
import pytest
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError
from argon2.low_level import hash_secret
from argon2.profiles import get_default_parameters
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import Session, SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import ads_engine.api.security as security
from ads_engine.api.security import (
    CSRF_HEADER,
    CSRF_VALUE,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    MUTATING_METHODS,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    LoginRateLimiter,
    PasswordCheck,
    RateDecision,
    SessionClaims,
    client_ip,
    csrf_ok,
    end_session,
    hash_password,
    read_session,
    start_session,
    verify_dummy,
    verify_password,
)

ADMIN_PASSWORD = "test-admin-password"
ADMIN_EMAIL = "admin@example.com"
SIGNING_KEY = "test-signing-key"

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
T0_EPOCH = int(T0.timestamp())
NAIVE_T0 = T0.replace(tzinfo=None)

PEER = "203.0.113.7"
PEER_V6 = "2001:db8::7"
FORWARDED = "198.51.100.9"

VALID_CLAIMS: dict[str, object] = {"uid": 7, "email": ADMIN_EMAIL, "iat": T0_EPOCH}

MISMATCH = PasswordCheck(ok=False, needs_rehash=False)

EXPIRED_COOKIE = "expires=Thu, 01 Jan 1970 00:00:00 GMT"


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _request(
    *,
    method: str = "GET",
    headers: Sequence[tuple[bytes, bytes]] = (),
    client: tuple[str, int] | None = (PEER, 51234),
    session: dict[str, Any] | None = None,
) -> Request:
    """Build a Starlette request from a hand-made ASGI HTTP scope.

    ``client=None`` leaves the peer out of the scope; ``session`` stands in for the dict
    ``SessionMiddleware`` would place there. Header names are lowercase, as ASGI servers
    deliver them.
    """
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": list(headers),
    }
    if client is not None:
        scope["client"] = client
    if session is not None:
        scope["session"] = session
    return Request(scope)


def _session_request(claims: dict[str, object] | None = None) -> tuple[Request, Session]:
    """Return a request whose scope carries a Starlette ``Session`` holding ``claims``.

    A ``Session`` records whether it was modified, which is how ``SessionMiddleware``
    decides to rewrite or expire the cookie.
    """
    session = Session(claims or {})
    return _request(session=session), session


class _WarningRecorder:
    """Stand-in for the module's structlog logger that keeps every warning it receives."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        """Record one warning event with its structured fields."""
        self.warnings.append((event, fields))

    def text(self) -> str:
        """Return every recorded event and field as one string, for leak checks."""
        return " ".join(f"{event} {fields!r}" for event, fields in self.warnings)


@pytest.fixture
def log_warnings(monkeypatch: pytest.MonkeyPatch) -> _WarningRecorder:
    """Replace the module logger with a recorder for the duration of one test."""
    recorder = _WarningRecorder()
    monkeypatch.setattr(security, "_log", recorder)
    return recorder


@pytest.fixture(scope="module")
def admin_hash() -> str:
    """One argon2id hash of the admin password, shared because each hash takes ~60 ms."""
    return hash_password(ADMIN_PASSWORD)


@pytest.fixture
def anyio_backend() -> str:
    """Run this module's async tests on asyncio only."""
    return "asyncio"


class FakeClock:
    """A settable replacement for :func:`time.monotonic`."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.start = start
        self.now = start

    def __call__(self) -> float:
        """Return the current fake time in seconds."""
        return self.now

    def at(self, offset: float) -> None:
        """Move the clock to ``offset`` seconds after its start."""
        self.now = self.start + offset


class _UnparseableParametersHasher(PasswordHasher):
    """A hasher whose parameter parser rejects every hash, although libargon2 verifies it."""

    def check_needs_rehash(self, hash: str | bytes) -> bool:
        """Fail as argon2-cffi does on an encoding its parameter parser cannot read."""
        raise InvalidHashError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants_match_the_security_contract() -> None:
    """The 12 h session, the CSRF header rule and 5 login attempts per 60 s are the contract."""
    assert SESSION_MAX_AGE == 12 * 3600
    assert CSRF_HEADER == "X-Requested-With"
    assert CSRF_VALUE == "ads-engine"
    assert sorted(MUTATING_METHODS) == ["DELETE", "PATCH", "POST", "PUT"]
    assert LOGIN_MAX_ATTEMPTS == 5
    assert LOGIN_WINDOW_SECONDS == 60


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def test_hash_password_writes_argon2id_with_the_library_defaults(admin_hash: str) -> None:
    """The hash is PHC-format argon2id with argon2-cffi's default parameters."""
    assert admin_hash.startswith("$argon2id$")
    assert extract_parameters(admin_hash) == get_default_parameters()
    assert ADMIN_PASSWORD not in admin_hash


def test_verify_password_accepts_the_matching_password(
    admin_hash: str, log_warnings: _WarningRecorder
) -> None:
    """A matching password with current parameters needs no rehash and logs nothing."""
    assert verify_password(admin_hash, ADMIN_PASSWORD) == PasswordCheck(ok=True, needs_rehash=False)
    assert log_warnings.warnings == []


@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("test-admin-passwordX", id="suffix"),
        pytest.param("Test-admin-password", id="case"),
        pytest.param("", id="empty"),
    ],
)
def test_verify_password_rejects_a_wrong_password(admin_hash: str, candidate: str) -> None:
    """A wrong password is a mismatch, and a mismatch never asks for a rehash."""
    assert verify_password(admin_hash, candidate) == MISMATCH


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("pässwörd-☃-𝄞", id="non-ascii"),
        pytest.param("test-" + "x" * 1024, id="long"),
    ],
)
def test_verify_password_uses_the_full_utf8_encoding_of_the_password(password: str) -> None:
    """Hashing and verifying agree on UTF-8 bytes, and every character counts."""
    stored = hash_password(password)
    assert verify_password(stored, password).ok is True
    assert verify_password(stored, password[:-1]) == MISMATCH


@pytest.mark.parametrize(
    "password",
    ["", "test-admin-password", "pässwörd", "☃ snow", "𝄞 clef"],
)
def test_password_bytes_equals_utf8_for_every_valid_string(password: str) -> None:
    """For a valid string the bytes are exactly ``password.encode("utf-8")``."""
    assert security._password_bytes(password) == password.encode("utf-8")


def test_password_bytes_maps_a_lone_surrogate_to_bytes_no_valid_string_encodes_to() -> None:
    """A lone surrogate is encoded without raising, into bytes that are not valid UTF-8."""
    encoded = security._password_bytes("abc\ud800")
    with pytest.raises(UnicodeDecodeError, match="can't decode"):
        encoded.decode("utf-8")


def test_verify_password_mismatches_a_lone_surrogate_without_raising(
    admin_hash: str, log_warnings: _WarningRecorder
) -> None:
    """A JSON body's lone surrogate is an ordinary mismatch, not an error or a log line."""
    assert verify_password(admin_hash, "test-admin-password\ud800") == MISMATCH
    assert log_warnings.warnings == []


def test_hash_password_rejects_a_lone_surrogate() -> None:
    """No stored password can hold a lone surrogate, so hashing one raises."""
    with pytest.raises(UnicodeEncodeError, match="surrogates not allowed"):
        hash_password("test-admin-password\ud800")


@pytest.mark.parametrize(
    "variant", [Type.ID, Type.I, Type.D], ids=["argon2id", "argon2i", "argon2d"]
)
def test_verify_password_flags_outdated_parameters_for_rehash(variant: Type) -> None:
    """Any argon2 variant verifies; parameters other than the defaults ask for a rehash."""
    stored = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=variant).hash(
        ADMIN_PASSWORD
    )
    assert verify_password(stored, ADMIN_PASSWORD) == PasswordCheck(ok=True, needs_rehash=True)
    assert verify_password(stored, "test-wrong-password") == MISMATCH


def test_verify_password_flags_a_hash_without_a_version_field_for_rehash() -> None:
    """A legacy hash without ``v=`` (argon2 1.0) verifies and is due for a rehash."""
    legacy = hash_secret(
        ADMIN_PASSWORD.encode("utf-8"),
        b"test-salt-bytes",
        time_cost=1,
        memory_cost=8,
        parallelism=1,
        hash_len=32,
        type=Type.ID,
        version=0x10,
    ).decode("ascii")
    unversioned = legacy.replace("$v=16", "", 1)
    assert "$v=" not in unversioned
    assert verify_password(unversioned, ADMIN_PASSWORD) == PasswordCheck(ok=True, needs_rehash=True)


def test_verify_password_rehashes_when_the_parameter_parser_cannot_read_the_hash(
    admin_hash: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash libargon2 verifies but argon2-cffi cannot parse is replaced by a rehash."""
    monkeypatch.setattr(security, "_HASHER", _UnparseableParametersHasher())
    assert verify_password(admin_hash, ADMIN_PASSWORD) == PasswordCheck(ok=True, needs_rehash=True)


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda _stored: "not-a-hash", id="garbage"),
        pytest.param(lambda _stored: "", id="empty"),
        pytest.param(lambda stored: stored.replace("$argon2id$", "$argon2x$", 1), id="variant"),
        pytest.param(lambda stored: stored[:-5], id="truncated"),
        pytest.param(lambda stored: stored.replace("m=65536,", "m=1,", 1), id="parameters"),
        pytest.param(lambda stored: stored[:-1] + "é", id="non-ascii"),
    ],
)
def test_verify_password_treats_an_unreadable_hash_as_a_logged_mismatch(
    admin_hash: str, log_warnings: _WarningRecorder, corrupt: Callable[[str], str]
) -> None:
    """An unreadable stored hash mismatches and logs one warning naming only the error type."""
    stored = corrupt(admin_hash)
    assert verify_password(stored, ADMIN_PASSWORD) == MISMATCH
    assert len(log_warnings.warnings) == 1
    _event, fields = log_warnings.warnings[0]
    assert set(fields) == {"error"}
    logged = log_warnings.text()
    assert ADMIN_PASSWORD not in logged
    assert admin_hash.rsplit("$", 1)[-1][:-5] not in logged
    if stored:
        assert stored not in logged


def test_verify_dummy_spends_one_verification_against_a_current_hash(
    monkeypatch: pytest.MonkeyPatch, log_warnings: _WarningRecorder
) -> None:
    """``verify_dummy`` verifies once against a cached current-parameter hash that never matches."""
    real_verify = security.verify_password
    calls: list[tuple[str, str]] = []

    def spy(stored_hash: str, password: str) -> PasswordCheck:
        """Record the call, then verify for real."""
        calls.append((stored_hash, password))
        return real_verify(stored_hash, password)

    monkeypatch.setattr(security, "verify_password", spy)
    verify_dummy(ADMIN_PASSWORD)
    assert len(calls) == 1
    dummy, password = calls[0]
    assert password == ADMIN_PASSWORD
    assert dummy is security._dummy_hash()
    assert extract_parameters(dummy) == get_default_parameters()
    assert real_verify(dummy, ADMIN_PASSWORD) == MISMATCH
    assert log_warnings.warnings == []


def test_verify_dummy_never_raises_on_a_lone_surrogate(log_warnings: _WarningRecorder) -> None:
    """The timing equaliser accepts any submitted string."""
    verify_dummy("test-admin-password\ud800")
    assert log_warnings.warnings == []


# ---------------------------------------------------------------------------
# Sessions on a hand-built scope
# ---------------------------------------------------------------------------


def test_start_session_replaces_the_session_with_fresh_claims() -> None:
    """Signing in drops everything an earlier session held and stores uid, email and iat."""
    request, session = _session_request(
        {"uid": 99, "email": "old@example.com", "iat": 1, "stale": "x"}
    )
    start_session(request, user_id=7, email=ADMIN_EMAIL, now=T0)
    assert dict(session) == VALID_CLAIMS
    assert session.modified is True
    assert read_session(request, now=T0) == SessionClaims(
        user_id=7, email=ADMIN_EMAIL, issued_at=T0_EPOCH
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(T0 + timedelta(microseconds=750_000), T0_EPOCH, id="fraction-dropped"),
        pytest.param(T0.astimezone(timezone(timedelta(hours=-4))), T0_EPOCH, id="other-offset"),
        pytest.param(datetime(1969, 12, 31, 23, 59, 59, 500_000, tzinfo=UTC), -1, id="pre-epoch"),
    ],
)
def test_start_session_stores_whole_epoch_seconds_truncated_toward_the_past(
    now: datetime, expected: int
) -> None:
    """The issue time is the instant in whole epoch seconds, rounded down."""
    request, session = _session_request()
    start_session(request, user_id=7, email=ADMIN_EMAIL, now=now)
    assert session["iat"] == expected


@pytest.mark.parametrize(
    ("user_id", "email", "now", "message"),
    [
        pytest.param(0, ADMIN_EMAIL, T0, "user_id must be a positive integer", id="uid-zero"),
        pytest.param(-3, ADMIN_EMAIL, T0, "user_id must be a positive integer", id="uid-neg"),
        pytest.param(True, ADMIN_EMAIL, T0, "user_id must be a positive integer", id="uid-bool"),
        pytest.param("7", ADMIN_EMAIL, T0, "user_id must be a positive integer", id="uid-str"),
        pytest.param(7, "", T0, "email must not be empty", id="email-empty"),
        pytest.param(7, ADMIN_EMAIL, NAIVE_T0, "now must be a timezone-aware datetime", id="naive"),
    ],
)
def test_start_session_rejects_invalid_arguments(
    user_id: object, email: str, now: datetime, message: str
) -> None:
    """A non-positive or non-int user id, an empty email or a naive instant raises."""
    request, _session = _session_request()
    with pytest.raises(ValueError, match=message):
        start_session(request, user_id=cast("int", user_id), email=email, now=now)


def test_read_session_returns_none_for_an_empty_session_and_leaves_it_untouched() -> None:
    """No session means no claims, and the empty session is not marked for rewriting."""
    request, session = _session_request()
    assert read_session(request, now=T0) is None
    assert dict(session) == {}
    assert session.modified is False


def test_read_session_enforces_the_absolute_lifetime_from_sign_in() -> None:
    """Claims are valid for fewer than SESSION_MAX_AGE seconds after iat, then cleared."""
    request, session = _session_request(VALID_CLAIMS)
    last_valid = T0 + timedelta(seconds=SESSION_MAX_AGE - 1)
    assert read_session(request, now=last_valid) == SessionClaims(
        user_id=7, email=ADMIN_EMAIL, issued_at=T0_EPOCH
    )
    assert read_session(request, now=T0 + timedelta(seconds=SESSION_MAX_AGE)) is None
    assert dict(session) == {}
    assert session.modified is True


def test_read_session_accepts_an_issue_time_slightly_ahead_of_the_clock() -> None:
    """Only elapsed time is bounded: an iat a few seconds ahead (replica clock skew) is valid."""
    request, _session = _session_request({**VALID_CLAIMS, "iat": T0_EPOCH + 5})
    assert read_session(request, now=T0) == SessionClaims(
        user_id=7, email=ADMIN_EMAIL, issued_at=T0_EPOCH + 5
    )


def _without(key: str) -> dict[str, object]:
    """Return the valid claims with ``key`` removed."""
    return {name: value for name, value in VALID_CLAIMS.items() if name != key}


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({**VALID_CLAIMS, "uid": "7"}, id="uid-str"),
        pytest.param({**VALID_CLAIMS, "uid": True}, id="uid-bool"),
        pytest.param({**VALID_CLAIMS, "uid": 7.0}, id="uid-float"),
        pytest.param({**VALID_CLAIMS, "uid": 0}, id="uid-zero"),
        pytest.param({**VALID_CLAIMS, "uid": -1}, id="uid-negative"),
        pytest.param(_without("uid"), id="uid-missing"),
        pytest.param({**VALID_CLAIMS, "email": ""}, id="email-empty"),
        pytest.param({**VALID_CLAIMS, "email": 7}, id="email-int"),
        pytest.param({**VALID_CLAIMS, "email": None}, id="email-null"),
        pytest.param(_without("email"), id="email-missing"),
        pytest.param({**VALID_CLAIMS, "iat": str(T0_EPOCH)}, id="iat-str"),
        pytest.param({**VALID_CLAIMS, "iat": True}, id="iat-bool"),
        pytest.param({**VALID_CLAIMS, "iat": T0_EPOCH + 0.5}, id="iat-float"),
        pytest.param({**VALID_CLAIMS, "iat": -1}, id="iat-negative"),
        pytest.param(_without("iat"), id="iat-missing"),
        pytest.param({"unrelated": "x"}, id="unrelated-only"),
    ],
)
def test_read_session_clears_malformed_claims(claims: dict[str, object]) -> None:
    """Claims of the wrong type, sign or shape yield None and clear the session."""
    request, session = _session_request(claims)
    assert read_session(request, now=T0) is None
    assert dict(session) == {}
    assert session.modified is True


@pytest.mark.parametrize(
    "claims",
    [pytest.param({}, id="empty-session"), pytest.param(VALID_CLAIMS, id="valid-session")],
)
def test_read_session_rejects_a_naive_clock(claims: dict[str, object]) -> None:
    """A naive ``now`` raises whatever the session holds."""
    request, _session = _session_request(claims)
    with pytest.raises(ValueError, match="now must be a timezone-aware datetime"):
        read_session(request, now=NAIVE_T0)


def test_end_session_clears_the_session() -> None:
    """Signing out empties the session and marks it for the middleware to expire."""
    request, session = _session_request(VALID_CLAIMS)
    end_session(request)
    assert dict(session) == {}
    assert session.modified is True
    assert read_session(request, now=T0) is None


# ---------------------------------------------------------------------------
# Sessions and CSRF through Starlette's SessionMiddleware
# ---------------------------------------------------------------------------


def _app(clock: list[datetime]) -> Starlette:
    """Build a minimal app that runs this module's helpers behind ``SessionMiddleware``."""

    async def login(request: Request) -> JSONResponse:
        """Sign the admin in at the current fake instant."""
        start_session(request, user_id=7, email=ADMIN_EMAIL, now=clock[0])
        return JSONResponse({"signed_in": True})

    async def me(request: Request) -> JSONResponse:
        """Return the session claims, or null."""
        claims = read_session(request, now=clock[0])
        if claims is None:
            return JSONResponse(None)
        return JSONResponse(
            {"user_id": claims.user_id, "email": claims.email, "issued_at": claims.issued_at}
        )

    async def logout(request: Request) -> JSONResponse:
        """Sign out."""
        end_session(request)
        return JSONResponse({"signed_in": False})

    async def mutate(request: Request) -> JSONResponse:
        """Report the CSRF decision for this request."""
        return JSONResponse({"csrf_ok": csrf_ok(request)})

    return Starlette(
        routes=[
            Route("/login", login, methods=["POST"]),
            Route("/me", me, methods=["GET"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/mutate", mutate, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key=SIGNING_KEY,
                session_cookie=SESSION_COOKIE,
                max_age=SESSION_MAX_AGE,
            )
        ],
    )


def _client(clock: list[datetime]) -> httpx.AsyncClient:
    """Return an in-process HTTP client for :func:`_app`; it keeps cookies between requests."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(clock)), base_url="http://testserver"
    )


@pytest.mark.anyio
async def test_session_claims_survive_the_signed_cookie_until_they_expire() -> None:
    """Claims round-trip through the signed JSON cookie; expiry makes the middleware drop it."""
    clock = [T0]
    async with _client(clock) as client:
        assert (await client.post("/login")).status_code == 200
        assert client.cookies.get(SESSION_COOKIE)

        clock[0] = T0 + timedelta(seconds=SESSION_MAX_AGE - 1)
        assert (await client.get("/me")).json() == {
            "user_id": 7,
            "email": ADMIN_EMAIL,
            "issued_at": T0_EPOCH,
        }

        clock[0] = T0 + timedelta(seconds=SESSION_MAX_AGE)
        expired = await client.get("/me")
        assert expired.json() is None
        assert expired.headers["set-cookie"].startswith(f"{SESSION_COOKIE}=")
        assert EXPIRED_COOKIE in expired.headers["set-cookie"]
        assert client.cookies.get(SESSION_COOKIE) is None

        after = await client.get("/me")
        assert after.json() is None
        assert "set-cookie" not in after.headers


@pytest.mark.anyio
async def test_end_session_makes_the_middleware_expire_the_cookie() -> None:
    """After sign-out the cookie is expired and the next request has no session."""
    async with _client([T0]) as client:
        assert (await client.post("/login")).status_code == 200
        logout = await client.post("/logout")
        assert EXPIRED_COOKIE in logout.headers["set-cookie"]
        assert (await client.get("/me")).json() is None


@pytest.mark.anyio
async def test_csrf_header_name_is_case_insensitive_end_to_end() -> None:
    """The header name may arrive in any case; the value must be exactly ``ads-engine``."""
    async with _client([T0]) as client:
        upper = await client.post("/mutate", headers={"X-REQUESTED-WITH": CSRF_VALUE})
        assert upper.json() == {"csrf_ok": True}
        assert (await client.post("/mutate")).json() == {"csrf_ok": False}


# ---------------------------------------------------------------------------
# CSRF and client identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_csrf_ok_passes_safe_methods_without_the_header(method: str) -> None:
    """Safe methods always pass."""
    assert csrf_ok(_request(method=method)) is True


@pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
def test_csrf_ok_requires_the_header_on_mutating_methods(method: str) -> None:
    """A mutating method passes only with ``X-Requested-With: ads-engine``."""
    assert csrf_ok(_request(method=method)) is False
    header = (b"x-requested-with", CSRF_VALUE.encode("ascii"))
    assert csrf_ok(_request(method=method, headers=[header])) is True


@pytest.mark.parametrize(
    "value",
    [b"ADS-ENGINE", b"XMLHttpRequest", b"ads-engine ", b"", b"ads-engine-x"],
)
def test_csrf_ok_compares_the_header_value_exactly(value: bytes) -> None:
    """Any value other than the exact, case-sensitive ``ads-engine`` fails."""
    request = _request(method="POST", headers=[(b"x-requested-with", value)])
    assert csrf_ok(request) is False


@pytest.mark.parametrize("host", [PEER, PEER_V6])
def test_client_ip_returns_the_asgi_peer(host: str) -> None:
    """The rate-limit key is the peer host the server supplied."""
    assert client_ip(_request(client=(host, 51234))) == host


def test_client_ip_never_reads_forwarding_headers() -> None:
    """Client-supplied forwarding headers never choose the rate-limit key."""
    headers = [(b"x-forwarded-for", FORWARDED.encode("ascii")), (b"x-real-ip", b"192.0.2.1")]
    assert client_ip(_request(headers=headers)) == PEER
    assert client_ip(_request(headers=headers, client=None)) == "unknown"


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(None, id="absent"),
        pytest.param(("", 0), id="empty-host"),
    ],
)
def test_client_ip_falls_back_to_one_shared_key(client: tuple[str, int] | None) -> None:
    """Without a peer address every request shares the key ``unknown``."""
    assert client_ip(_request(client=client)) == "unknown"


def test_client_ip_treats_a_null_client_as_absent() -> None:
    """An ASGI scope may carry ``client: None``; it is treated like a missing peer."""
    request = Request({"type": "http", "method": "GET", "headers": [], "client": None})
    assert client_ip(request) == "unknown"


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------


def test_limiter_allows_the_limit_then_denies_with_retry_after() -> None:
    """With the defaults, five attempts pass and the 6th within 60 s is denied for 60 s."""
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert limiter.check(PEER) == RateDecision(allowed=True, retry_after=0)
    assert limiter.check(PEER) == RateDecision(allowed=False, retry_after=LOGIN_WINDOW_SECONDS)
    clock.at(LOGIN_WINDOW_SECONDS)
    assert limiter.check(PEER).allowed is True


def test_limiter_window_slides_from_the_oldest_counted_attempt() -> None:
    """Admission returns exactly one window after the oldest counted attempt."""
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for offset in (0, 10, 20, 30, 40):
        clock.at(offset)
        assert limiter.check(PEER).allowed is True
    clock.at(50)
    assert limiter.check(PEER) == RateDecision(allowed=False, retry_after=10)
    clock.at(59.5)
    assert limiter.check(PEER) == RateDecision(allowed=False, retry_after=1)
    clock.at(60)
    assert limiter.check(PEER).allowed is True
    clock.at(61)
    assert limiter.check(PEER) == RateDecision(allowed=False, retry_after=9)


def test_limiter_does_not_count_denied_attempts() -> None:
    """A client that keeps retrying is admitted once its oldest counted attempt expires."""
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        limiter.check(PEER)
    for second in range(1, LOGIN_WINDOW_SECONDS):
        clock.at(second)
        assert limiter.check(PEER).allowed is False
    clock.at(LOGIN_WINDOW_SECONDS)
    assert limiter.check(PEER).allowed is True


def test_limiter_keys_are_independent() -> None:
    """One address at its limit does not affect another."""
    limiter = LoginRateLimiter(clock=FakeClock())
    for _ in range(LOGIN_MAX_ATTEMPTS + 1):
        limiter.check(PEER)
    assert limiter.check(PEER).allowed is False
    assert limiter.check(PEER_V6).allowed is True


def test_limiter_retry_after_stays_between_one_second_and_one_window() -> None:
    """``retry_after`` is at least 1 and at most the window, even for a clock that steps back."""
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    clock.at(100)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        limiter.check(PEER)
    clock.at(10)
    assert limiter.check(PEER) == RateDecision(allowed=False, retry_after=LOGIN_WINDOW_SECONDS)

    short_clock = FakeClock()
    short = LoginRateLimiter(max_attempts=1, window_seconds=0.5, clock=short_clock)
    assert short.check(PEER).allowed is True
    short_clock.at(0.25)
    assert short.check(PEER) == RateDecision(allowed=False, retry_after=1)


def test_limiter_tracks_and_resets_keys() -> None:
    """``tracked_keys`` counts keys with attempts; ``reset`` forgets one key or every key."""
    limiter = LoginRateLimiter(clock=FakeClock())
    assert limiter.tracked_keys == 0
    for _ in range(LOGIN_MAX_ATTEMPTS):
        limiter.check(PEER)
        limiter.check(PEER_V6)
    assert limiter.tracked_keys == 2

    limiter.reset(PEER)
    limiter.reset(FORWARDED)
    assert limiter.tracked_keys == 1
    assert limiter.check(PEER).allowed is True
    assert limiter.check(PEER_V6).allowed is False

    limiter.reset()
    assert limiter.tracked_keys == 0
    assert limiter.check(PEER_V6).allowed is True


def test_limiter_prunes_keys_whose_attempts_have_all_expired() -> None:
    """Every call drops keys whose latest attempt left the window, and keeps active ones."""
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        limiter.check(PEER)
    clock.at(30)
    limiter.check(PEER_V6)
    assert limiter.tracked_keys == 2
    clock.at(61)
    limiter.check(FORWARDED)
    assert limiter.tracked_keys == 2
    assert limiter.check(PEER).allowed is True


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"max_attempts": 0}, id="max-attempts-zero"),
        pytest.param({"window_seconds": 0}, id="window-zero"),
        pytest.param({"window_seconds": -1}, id="window-negative"),
        pytest.param({"window_seconds": math.inf}, id="window-infinite"),
        pytest.param({"window_seconds": math.nan}, id="window-nan"),
        pytest.param({"max_keys": 0}, id="max-keys-zero"),
    ],
)
def test_limiter_rejects_out_of_range_configuration(kwargs: dict[str, Any]) -> None:
    """Each constructor argument outside its documented range raises."""
    with pytest.raises(ValueError, match="must be"):
        LoginRateLimiter(**kwargs)


def test_limiter_evicts_the_key_with_the_oldest_latest_attempt(
    log_warnings: _WarningRecorder,
) -> None:
    """Beyond ``max_keys`` the least recently active key is evicted and its counter restarts."""
    clock = FakeClock()
    limiter = LoginRateLimiter(max_attempts=2, clock=clock, max_keys=2)
    first, second, third = "198.51.100.1", "198.51.100.2", "198.51.100.3"
    for offset, key in ((0, first), (1, second), (1.5, second), (2, first)):
        clock.at(offset)
        assert limiter.check(key).allowed is True
    clock.at(3)
    assert limiter.check(third).allowed is True
    assert limiter.tracked_keys == 2
    clock.at(3.5)
    assert limiter.check(first).allowed is False
    clock.at(4)
    assert limiter.check(second).allowed is True
    assert limiter.tracked_keys == 2
    assert len(log_warnings.warnings) == 1


def test_limiter_capacity_warning_is_logged_once_per_episode_without_addresses(
    log_warnings: _WarningRecorder,
) -> None:
    """One warning per episode; it re-arms below 90% of ``max_keys`` or on a full reset."""
    assert security._CAPACITY_REARM_SHARE == 0.9
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock, max_keys=10)
    addresses = [f"198.51.100.{index}" for index in range(1, 40)]
    pending = iter(addresses)

    def attempt() -> None:
        """Count one attempt from the next unused address, one second later."""
        clock.now += 1
        assert limiter.check(next(pending)).allowed is True

    for _ in range(11):
        attempt()
    assert limiter.tracked_keys == 10
    assert len(log_warnings.warnings) == 1
    _event, fields = log_warnings.warnings[0]
    assert fields["max_keys"] == 10
    assert fields["evicted"] == 1

    # Back down to 10 of 10, then over again: still the same episode.
    limiter.reset(addresses[1])
    attempt()
    attempt()
    assert limiter.tracked_keys == 10
    assert len(log_warnings.warnings) == 1

    # 7 tracked, then 8 after one attempt: below 9 (90% of 10), so the warning re-arms.
    for address in addresses[3:6]:
        limiter.reset(address)
    attempt()
    assert limiter.tracked_keys == 8
    for _ in range(3):
        attempt()
    assert limiter.tracked_keys == 10
    assert len(log_warnings.warnings) == 2

    limiter.reset()
    for _ in range(11):
        attempt()
    assert len(log_warnings.warnings) == 3

    logged = log_warnings.text()
    assert all(address not in logged for address in addresses)


def test_limiter_defaults_to_the_monotonic_clock() -> None:
    """Without an injected clock the limiter runs on real monotonic time."""
    limiter = LoginRateLimiter()
    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert limiter.check(PEER).allowed is True
    denied = limiter.check(PEER)
    assert denied.allowed is False
    assert 1 <= denied.retry_after <= LOGIN_WINDOW_SECONDS


def _run_concurrently(workers: int, task: Callable[[int], int]) -> int:
    """Run ``task(worker_index)`` on ``workers`` threads released together; sum the results."""
    barrier = threading.Barrier(workers)

    def released(index: int) -> int:
        """Wait until every worker is ready, then run the task."""
        barrier.wait(timeout=30)
        return task(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(released, range(workers)))


def test_limiter_counts_concurrent_attempts_on_one_key_exactly() -> None:
    """Under contention from many threads exactly ``max_attempts`` attempts are allowed."""
    limiter = LoginRateLimiter(clock=FakeClock())

    def hammer(_index: int) -> int:
        """Attempt 50 logins from one address and count the allowed ones."""
        return sum(limiter.check(PEER).allowed for _ in range(50))

    assert _run_concurrently(8, hammer) == LOGIN_MAX_ATTEMPTS
    assert limiter.tracked_keys == 1


def test_limiter_holds_its_key_ceiling_under_concurrent_eviction(
    log_warnings: _WarningRecorder,
) -> None:
    """Concurrent attempts from many addresses never leave more than ``max_keys`` tracked."""
    limiter = LoginRateLimiter(clock=FakeClock(), max_keys=50)

    def spray(index: int) -> int:
        """Attempt one login from each of 100 distinct addresses."""
        return sum(limiter.check(f"10.{index}.0.{host}").allowed for host in range(100))

    assert _run_concurrently(8, spray) == 800
    assert limiter.tracked_keys == 50
    assert len(log_warnings.warnings) == 1
