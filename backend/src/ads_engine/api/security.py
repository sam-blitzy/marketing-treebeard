"""Security primitives of the API: argon2id passwords, session claims, CSRF, login limit.

This module holds every security decision the API makes about a request, and nothing
else. It performs no database access, reads no settings and raises no HTTP errors: each
helper returns a decision, and ``api/deps.py`` and ``api/routes/auth.py`` turn a negative
decision into an ``ApiError`` (401, 403 or 429). It never imports ``ads_engine.api.deps``,
``ads_engine.api.routes``, ``connectors``, ``sync`` or ``simulation``, so ``deps.py`` can
import it without a cycle.

Contents:

* **Constants** shared with ``api/app.py``, ``api/deps.py`` and the tests: the session
  cookie name and its 12 h lifetime, the CSRF header and value, the mutating methods and
  the login limit of 5 attempts per 60 s per client IP.
* **Passwords**: :func:`hash_password`, :func:`verify_password` and :func:`verify_dummy`
  over one argon2id :class:`argon2.PasswordHasher` with the library defaults, the same
  parameters ``ads-engine hash-password`` uses.
* **Sessions**: :func:`start_session`, :func:`read_session`, :func:`end_session` and
  :func:`session_cookie_unverified` (a cookie the middleware refused) over
  Starlette's ``request.session``, a signed (not encrypted) cookie written by
  ``SessionMiddleware``, which ``api/app.py`` installs with :data:`SESSION_COOKIE` and
  :data:`SESSION_MAX_AGE`. The session holds only the user id, the email, the issue time
  and the session generation (``app_user.session_generation`` at sign-in, which sign-out
  increments so ``api/deps.py`` revokes every earlier cookie); nothing secret is ever
  stored in it.
* **CSRF**: :func:`csrf_ok` requires ``X-Requested-With: ads-engine`` on mutating methods.
* **Client identity and brute-force protection**: :func:`client_ip` and
  :class:`LoginRateLimiter`.

Nothing here logs a password, a password hash or an email address, and no password is returned
or stored. :func:`hash_password` returns a hash and :func:`read_session` returns the session's
email, each only to the login route or the session dependency, which never log them.

Example:
    The login route combines the helpers in this order::

        decision = limiter.check(client_ip(request))
        if not decision.allowed:
            ...  # 429 with Retry-After: decision.retry_after
        check = verify_password(user.password_hash, password)
        if check.ok:
            start_session(
                request,
                user_id=user.id,
                email=user.email,
                generation=user.session_generation,
                now=now,
            )
"""

import functools
import math
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, TypeGuard

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from starlette.requests import Request

from ads_engine.logging import get_logger

__all__ = [
    "CSRF_HEADER",
    "CSRF_VALUE",
    "LOGIN_MAX_ATTEMPTS",
    "LOGIN_WINDOW_SECONDS",
    "MUTATING_METHODS",
    "SESSION_COOKIE",
    "SESSION_MAX_AGE",
    "LoginRateLimiter",
    "PasswordCheck",
    "RateDecision",
    "SessionClaims",
    "client_ip",
    "csrf_ok",
    "end_session",
    "hash_password",
    "read_session",
    "session_cookie_unverified",
    "start_session",
    "verify_dummy",
    "verify_password",
]

# ---------------------------------------------------------------------------
# Constants (the single source for app.py, deps.py and the tests)
# ---------------------------------------------------------------------------

SESSION_COOKIE: Final = "ads_engine_session"
"""Name of the signed session cookie set by Starlette's ``SessionMiddleware``."""

SESSION_MAX_AGE: Final = 12 * 3600
"""Absolute session lifetime in seconds (12 h), measured from sign-in.

``api/app.py`` passes it to ``SessionMiddleware`` as the cookie ``Max-Age`` and the
signature age limit; :func:`read_session` enforces it again on the session's own issue
time, so the lifetime stays absolute however often the cookie is rewritten.
"""

CSRF_HEADER: Final = "X-Requested-With"
"""Header every mutating request must carry (HTTP header names are case-insensitive)."""

CSRF_VALUE: Final = "ads-engine"
"""The exact, case-sensitive value :data:`CSRF_HEADER` must have."""

MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""HTTP methods that change state and therefore require the CSRF header."""

LOGIN_MAX_ATTEMPTS: Final = 5
"""Login attempts allowed per client IP within :data:`LOGIN_WINDOW_SECONDS`."""

LOGIN_WINDOW_SECONDS: Final = 60
"""Length of the sliding login rate-limit window, in seconds. The 6th attempt gets 429."""

# Session keys. Short names keep the signed cookie small; it is readable by the client,
# which is why it carries only the user's own id and email, the issue time and the
# session generation.
_SESSION_UID: Final = "uid"
_SESSION_EMAIL: Final = "email"
_SESSION_IAT: Final = "iat"
_SESSION_GEN: Final = "gen"

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Passwords (argon2id)
# ---------------------------------------------------------------------------

# One hasher for the process. argon2-cffi's defaults are argon2id (RFC 9106 low-memory
# profile: t=3, m=64 MiB, p=4), identical to the `ads-engine hash-password` output that
# `make init` writes, so a freshly initialised admin never needs a rehash. PasswordHasher
# is stateless after construction and safe to share between threads.
_HASHER: Final = PasswordHasher()


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    """Outcome of :func:`verify_password`.

    Attributes:
        ok: Whether the password matches the stored hash.
        needs_rehash: Whether the stored hash uses parameters other than the current
            defaults, so the caller should store :func:`hash_password` of the same
            password. Always ``False`` when ``ok`` is ``False``.
    """

    ok: bool
    needs_rehash: bool


_MISMATCH: Final = PasswordCheck(ok=False, needs_rehash=False)


def _password_bytes(password: str) -> bytes:
    r"""Encode a password as UTF-8 for argon2, without ever raising.

    A JSON body can carry a lone surrogate (the escape ``\ud800``), which strict UTF-8
    cannot encode. ``surrogatepass`` maps it to bytes that no valid string encodes to, so the
    verification still runs at full cost and simply mismatches, instead of failing with
    an exception or returning early (which would leak timing). For every valid string the
    result equals ``password.encode("utf-8")``, the encoding argon2-cffi uses when hashing.

    Args:
        password: The candidate password.

    Returns:
        The bytes passed to argon2.
    """
    return password.encode("utf-8", "surrogatepass")


def hash_password(password: str) -> str:
    """Hash a password with argon2id and the current default parameters.

    The login route calls it when :attr:`PasswordCheck.needs_rehash` is set. Hashing is
    CPU- and memory-bound (tens of milliseconds, 64 MiB); async callers that care about
    event-loop latency run it through ``starlette.concurrency.run_in_threadpool``.

    Args:
        password: The plaintext password. It is never logged or stored by this function.

    Returns:
        The PHC-format hash, starting with ``$argon2id$``.

    Raises:
        argon2.exceptions.HashingError: When the argon2 library fails to hash.
        UnicodeEncodeError: When ``password`` holds a lone surrogate, which no stored
            password can, since it cannot be encoded as UTF-8.
    """
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> PasswordCheck:
    """Check a password against a stored argon2 hash.

    A wrong password and an unreadable stored hash both yield ``ok=False``; neither
    raises, so a corrupt ``app_user.password_hash`` produces a 401 rather than a 500. An
    unreadable hash is logged as a warning naming only the error type, never the hash or
    the password, because it means the admin cannot sign in until ``ads-engine
    ensure-admin`` stores a valid hash.

    Every call first obtains the process-wide dummy hash that :func:`verify_dummy` checks
    against. The first call in a process therefore builds it, one hash on top of the
    verification, whichever login branch made that call, so a known email's wrong
    password performs the same argon2 operations as an unknown email, cold or warm. Like
    hashing, verification is CPU- and memory-bound; async callers that care about
    event-loop latency run it through ``starlette.concurrency.run_in_threadpool`` too.

    Args:
        stored_hash: The stored PHC-format hash (argon2id, argon2i or argon2d).
        password: The candidate plaintext password.

    Returns:
        The match result, with ``needs_rehash`` set when the hash parameters are outdated.
    """
    # Timing equaliser: build the cached dummy hash here, before any branch, so its one-time
    # cost never falls on the unknown-email branch alone. The value is used by verify_dummy.
    _dummy_hash()
    # A stored hash is ASCII by construction (PHC string format). argon2-cffi encodes it
    # as ASCII before verifying and raises UnicodeEncodeError for anything else, so a
    # non-ASCII value is rejected up front as unreadable instead of crashing the login.
    if not stored_hash.isascii():
        _log.warning("auth.password_hash_invalid", error="NonAsciiHash")
        return _MISMATCH
    try:
        _HASHER.verify(stored_hash, _password_bytes(password))
    except VerifyMismatchError:
        return _MISMATCH
    except InvalidHashError, VerificationError:
        # VerifyMismatchError subclasses VerificationError and is handled above, so this
        # branch sees only hashes argon2 cannot parse or decode.
        _log.warning("auth.password_hash_invalid", error="UnreadableHash")
        return _MISMATCH
    try:
        needs_rehash = _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        # libargon2 verified the hash, but argon2-cffi's parameter parser cannot read its
        # encoding (for example a legacy hash without the `v=` field); replacing it with a
        # current-format hash is exactly what a rehash does.
        needs_rehash = True
    return PasswordCheck(ok=True, needs_rehash=needs_rehash)


@functools.cache
def _dummy_hash() -> str:
    """Return a process-wide hash of a random secret, computed on first use.

    :func:`verify_password` calls it before any branch, so the first verification in a
    process pays for building it, whether that verification checks a real account or
    comes from :func:`verify_dummy`. It is built with the same hasher, so verifying
    against it costs the same as verifying a real, current-parameter hash.

    ``functools.cache`` takes no lock: threads racing on the first call may each compute
    a hash, so each pays the same one-time cost, and the cache keeps the last one stored.
    Every such hash is of a random secret that never matches, so which one a caller
    receives is harmless.

    Returns:
        The PHC-format argon2id hash of a random secret that never leaves this process.
    """
    return _HASHER.hash(secrets.token_urlsafe(32))


def verify_dummy(password: str) -> None:
    """Spend the cost of one password verification without checking any account.

    The login route calls it when no user has the submitted email. It runs
    :func:`verify_password` against the cached dummy hash, so it performs the same argon2
    operations as a known email's wrong password: on the first failure in a process both
    build the dummy hash once and verify, and afterwards both only verify. The response
    time therefore does not reveal which emails exist. The result is discarded: it can
    never match, because the dummy secret is a random token that never leaves this
    process.

    Args:
        password: The submitted password.
    """
    verify_password(_dummy_hash(), password)


# ---------------------------------------------------------------------------
# Sessions (Starlette request.session, a signed cookie)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionClaims:
    """The validated contents of a session cookie.

    Attributes:
        user_id: ``app_user.id`` of the signed-in user.
        email: The user's email at sign-in; ``deps.require_user`` compares it with the
            current row, so replacing the admin invalidates older sessions.
        issued_at: Sign-in instant, in epoch seconds.
        generation: ``app_user.session_generation`` at sign-in, a non-negative integer;
            ``deps.require_user`` compares it with the current row, so signing out, which
            increments the column, revokes every session issued before it.
    """

    user_id: int
    email: str
    issued_at: int
    generation: int


def _epoch_seconds(now: datetime, parameter: str) -> int:
    """Convert an aware ``datetime`` to whole epoch seconds.

    Args:
        now: The instant; it must carry a time zone.
        parameter: Parameter name used in the error message.

    Returns:
        ``now`` as epoch seconds, truncated toward the past.

    Raises:
        ValueError: When ``now`` is naive, whose epoch value would depend on the host's
            local time zone.
    """
    if now.utcoffset() is None:
        raise ValueError(f"{parameter} must be a timezone-aware datetime")
    return math.floor(now.timestamp())


def _is_int(value: object) -> TypeGuard[int]:
    """Whether ``value`` is a genuine ``int``; JSON ``true`` decodes to ``bool``, a subclass."""
    return isinstance(value, int) and not isinstance(value, bool)


def start_session(
    request: Request, *, user_id: int, email: str, generation: int, now: datetime
) -> None:
    """Sign a user in by replacing the session's contents with fresh claims.

    The session is cleared first, so nothing from an earlier session carries over. A
    Starlette session lives entirely in the signed cookie, with no server-side id, so the
    rewritten cookie is a new session and fixation is not possible. ``SessionMiddleware``
    writes the cookie (HttpOnly, SameSite=Lax, ``Max-Age`` :data:`SESSION_MAX_AGE`,
    Secure for an ``https://`` ``APP_BASE_URL``) when the response starts.

    The session holds the user id, the email, the issue time and the session generation.
    The generation is the user's ``app_user.session_generation`` at sign-in: sign-out
    increments the column, and ``deps.require_user`` refuses a cookie whose generation no
    longer matches it, so a copy of the cookie saved before the sign-out stops working.

    Args:
        request: The current request; ``SessionMiddleware`` must be installed.
        user_id: ``app_user.id`` of the authenticated user; a positive integer.
        email: The user's stored email.
        generation: The user's stored ``session_generation``; a non-negative integer.
        now: The sign-in instant, timezone-aware (the route's ``NowDep``).

    Raises:
        ValueError: When ``user_id`` is not a positive integer, ``email`` is empty,
            ``generation`` is not a non-negative integer or ``now`` is naive.
    """
    if not _is_int(user_id) or user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    if not email:
        raise ValueError("email must not be empty")
    if not _is_int(generation) or generation < 0:
        raise ValueError("generation must be a non-negative integer")
    issued_at = _epoch_seconds(now, "now")
    session = request.session
    session.clear()
    session[_SESSION_UID] = user_id
    session[_SESSION_EMAIL] = email
    session[_SESSION_IAT] = issued_at
    session[_SESSION_GEN] = generation


def read_session(request: Request, *, now: datetime) -> SessionClaims | None:
    """Return the signed-in user's claims, or ``None`` when there is no valid session.

    A session is valid when it holds a positive integer ``uid``, a non-empty string
    ``email``, a non-negative integer ``iat`` that is not later than ``now`` and a
    non-negative integer ``gen`` (the session generation), and fewer than
    :data:`SESSION_MAX_AGE` seconds have passed since ``iat``. Whether ``gen`` still
    equals the user's stored generation is checked by ``deps.require_user``, which reads
    the row; this function reads no database. The cookie's signature already proves the
    data was written by this application; its timestamp only says when the cookie was
    last written, so checking ``iat`` keeps the 12 h lifetime absolute from sign-in, and
    makes it testable through the injected clock.

    An ``iat`` later than ``now`` is rejected with no clock-skew allowance.
    ``SessionMiddleware`` verifies the cookie with itsdangerous'
    ``TimestampSigner.unsign(max_age=...)``, which already rejects a signature timestamp
    ahead of the receiving clock, so a cookie written on a node whose clock runs ahead
    fails at the signature layer. Holding ``iat`` to the same zero tolerance keeps the
    12 h lifetime absolute under a clock rollback or replica skew, where a negative
    elapsed time would otherwise extend it. API replicas therefore need synchronised
    clocks.

    An expired, future-issued or malformed session is cleared, so ``SessionMiddleware``
    expires the cookie on the response. A session without a valid ``gen``, such as one
    written before sessions carried a generation, is malformed. An empty session is left
    untouched.

    Args:
        request: The current request; ``SessionMiddleware`` must be installed.
        now: The current instant, timezone-aware (the route's ``NowDep``).

    Returns:
        The claims, or ``None`` when the session is absent, malformed, issued after ``now``
        or expired.

    Raises:
        ValueError: When ``now`` is naive.
    """
    current = _epoch_seconds(now, "now")
    session = request.session
    if not session:
        return None
    uid = session.get(_SESSION_UID)
    email = session.get(_SESSION_EMAIL)
    iat = session.get(_SESSION_IAT)
    gen = session.get(_SESSION_GEN)
    if (
        not _is_int(uid)
        or not isinstance(email, str)
        or not email
        or not _is_int(iat)
        or not _is_int(gen)
        or uid <= 0
        or iat < 0
        or gen < 0
        or iat > current
        or current - iat >= SESSION_MAX_AGE
    ):
        session.clear()
        return None
    return SessionClaims(user_id=uid, email=email, issued_at=iat, generation=gen)


def session_cookie_unverified(request: Request) -> bool:
    """Whether the request presented a session cookie that ``SessionMiddleware`` refused.

    Starlette's ``SessionMiddleware`` verifies the cookie's signature and age with
    itsdangerous before the application runs. When that fails (a tampered or forged
    value, a cookie signed with another ``SESSION_SECRET``, or a signature older than
    :data:`SESSION_MAX_AGE`) it silently installs an empty session, so the request looks
    exactly like one that sent no cookie. A :data:`SESSION_COOKIE` that is present while
    the session is empty is therefore one the middleware could not verify. The
    application never signs an empty session, so a cookie it wrote never decodes to one.

    The decision is made here and logged by ``deps.require_user``; this function logs
    nothing and leaves the session untouched. Call it before :func:`read_session`, which
    clears a verified but malformed session and would make that session look refused.

    Args:
        request: The current request; ``SessionMiddleware`` must be installed.

    Returns:
        ``True`` when the session cookie is present and the session is empty; ``False``
        when no session cookie was sent or its contents were verified.
    """
    return SESSION_COOKIE in request.cookies and not request.session


def end_session(request: Request) -> None:
    """Sign out by clearing the session.

    ``SessionMiddleware`` then sends the cookie back with an expiry date in the past,
    which removes it from the browser. Clearing the cookie does not revoke a copy of it
    kept elsewhere; ``POST /auth/logout`` first increments ``app_user.session_generation``,
    which does.

    Args:
        request: The current request; ``SessionMiddleware`` must be installed.
    """
    request.session.clear()


# ---------------------------------------------------------------------------
# CSRF and client identity
# ---------------------------------------------------------------------------


def csrf_ok(request: Request) -> bool:
    """Whether the request satisfies the CSRF header rule.

    Safe methods (``GET``, ``HEAD``, ``OPTIONS``) always pass. A method in
    :data:`MUTATING_METHODS` passes only with ``X-Requested-With: ads-engine``, compared
    exactly. A cross-site form or navigation cannot set a custom header, and a
    cross-origin ``fetch`` that sets one needs a CORS preflight the API never grants, so
    together with the SameSite=Lax cookie this blocks cross-site request forgery.

    Args:
        request: The current request.

    Returns:
        ``True`` when the request may proceed; ``deps.require_csrf`` answers ``False``
        with 403 ``csrf_header_missing``.
    """
    if request.method.upper() not in MUTATING_METHODS:
        return True
    return request.headers.get(CSRF_HEADER) == CSRF_VALUE


def client_ip(request: Request) -> str:
    """Return the client IP address used as the login rate-limit key.

    The value is the ASGI peer address. uvicorn (``--proxy-headers``) rewrites that peer
    from ``X-Forwarded-For`` before the app sees the request, but only when the connecting
    peer is listed in ``FORWARDED_ALLOW_IPS``, which the backend image sets to loopback
    only; it then takes the rightmost entry that is not itself listed, and from any other
    peer it ignores the header. Behind a reverse proxy, the real client address therefore
    arrives here only when ``FORWARDED_ALLOW_IPS`` names the proxy's address or network and
    the proxy overwrites ``X-Forwarded-For`` with the address of the peer connected to it.
    ``FORWARDED_ALLOW_IPS=*`` would trust every peer and take the leftmost, client-written
    entry. This function never parses ``X-Forwarded-For`` itself, because a client-supplied
    header would let an attacker pick a fresh rate-limit key on every attempt.

    Args:
        request: The current request.

    Returns:
        The peer host, or ``"unknown"`` when the server supplied no client address (all
        such requests then share one rate-limit key).
    """
    client = request.client
    if client is None or not client.host:
        return "unknown"
    return client.host


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateDecision:
    """Outcome of :meth:`LoginRateLimiter.check`.

    Attributes:
        allowed: Whether the attempt may proceed.
        retry_after: Whole seconds until the next attempt from the same key can be
            allowed: at least 1 when denied (the ``Retry-After`` header value), 0 when
            allowed.
    """

    allowed: bool
    retry_after: int


_ALLOWED: Final = RateDecision(allowed=True, retry_after=0)

# Capacity warnings are logged once per episode: after the first refused untracked key,
# the next warning needs the tracked-key count to fall below this share of max_keys first,
# so a sustained many-address attack produces one line rather than one per attempt.
_CAPACITY_REARM_SHARE: Final = 0.9


class LoginRateLimiter:
    """Sliding-window limit on login attempts per key (the client IP).

    Every attempt counts, successful or not: at most ``max_attempts`` are allowed within
    any ``window_seconds`` span, so with the defaults the 6th attempt within 60 s is
    denied (429). A denied attempt is not recorded, so a client that keeps retrying is
    admitted again exactly ``window_seconds`` after its oldest counted attempt.

    Scope and memory:

    * The state lives in this process's memory, as the plan specifies. Each API replica
      enforces its own window; a restart starts every key afresh. ``api/app.py`` creates
      one instance per application (``app.state.login_limiter``), so every
      ``create_app()``, including each test app, starts clean.
    * Memory is bounded: at most ``max_keys`` keys, each holding at most ``max_attempts``
      timestamps. Keys are ordered by their latest counted attempt, so keys whose
      attempts have all expired are pruned from the front on every call at amortised
      constant cost, and every key left is active. Capacity fails closed: while
      ``max_keys`` keys are active, an attempt from an untracked key is refused without
      being recorded, with ``retry_after`` set to when the least recently active key
      expires and frees a slot. Active counters are never dropped to make room, so no
      key is ever allowed more than ``max_attempts`` attempts in any window; the price of
      the memory ceiling is that new addresses wait during a many-address attack, which
      is logged as a warning without any address.

    Concurrency: :meth:`check` never awaits, so on the asyncio event loop it is atomic.
    It also takes an internal lock, because FastAPI runs synchronous (``def``)
    dependencies and routes in a thread pool; an uncontended lock costs well under a
    microsecond.

    Example:
        A fake clock makes the window deterministic in tests::

            now = [0.0]
            limiter = LoginRateLimiter(clock=lambda: now[0])
            assert all(limiter.check("1.2.3.4").allowed for _ in range(5))
            assert limiter.check("1.2.3.4").retry_after == 60
            now[0] = 60.0
            assert limiter.check("1.2.3.4").allowed

    Args:
        max_attempts: Attempts allowed per key within the window; at least 1.
        window_seconds: Length of the sliding window in seconds; positive and finite.
        clock: Source of non-decreasing seconds; :func:`time.monotonic` by default, so
            wall-clock adjustments never shorten or extend a window.
        max_keys: Most keys tracked at once; at least 1.

    Raises:
        ValueError: When an argument is outside the ranges above.
    """

    __slots__ = (
        "_at_capacity",
        "_attempts",
        "_clock",
        "_lock",
        "_max_attempts",
        "_max_keys",
        "_window",
    )

    def __init__(
        self,
        *,
        max_attempts: int = LOGIN_MAX_ATTEMPTS,
        window_seconds: float = LOGIN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not (math.isfinite(window_seconds) and window_seconds > 0):
            raise ValueError("window_seconds must be a positive, finite number of seconds")
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self._max_attempts = max_attempts
        self._window = float(window_seconds)
        self._clock = clock
        self._max_keys = max_keys
        # Key -> timestamps of its counted attempts, oldest first. Insertion order is
        # kept equal to the order of each key's latest attempt (move_to_end on record).
        self._attempts: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._at_capacity = False

    @property
    def tracked_keys(self) -> int:
        """Number of keys currently holding at least one counted attempt."""
        with self._lock:
            return len(self._attempts)

    def check(self, key: str) -> RateDecision:
        """Count one attempt for ``key`` and decide whether it may proceed.

        Args:
            key: The rate-limit key, normally :func:`client_ip` of the request.

        Returns:
            ``allowed=True`` (and the attempt recorded) when fewer than ``max_attempts``
            attempts fall within the window and the key is tracked or a slot is free.
            Otherwise ``allowed=False`` and nothing is recorded, with ``retry_after``
            between 1 and the window length: ceil(oldest counted attempt + window - now)
            for a key at its limit, or, for an untracked key while ``max_keys`` keys are
            active, ceil(latest attempt of the least recently active key + window - now),
            when that key expires and frees a slot.
        """
        with self._lock:
            now = self._clock()
            self._prune_idle(now)
            tracked = len(self._attempts)
            if self._at_capacity and tracked < self._max_keys * _CAPACITY_REARM_SHARE:
                self._at_capacity = False
            attempts = self._attempts.get(key)
            if attempts is None:
                if tracked >= self._max_keys:
                    return self._refuse_untracked(now)
                attempts = deque(maxlen=self._max_attempts)
                self._attempts[key] = attempts
            else:
                while attempts and now - attempts[0] >= self._window:
                    attempts.popleft()
                if len(attempts) >= self._max_attempts:
                    return self._deny_until_expiry(attempts[0], now)
            attempts.append(now)
            self._attempts.move_to_end(key)
            return _ALLOWED

    def reset(self, key: str | None = None) -> None:
        """Forget recorded attempts.

        Args:
            key: The key to forget; every key when ``None``.
        """
        with self._lock:
            if key is None:
                self._attempts.clear()
                self._at_capacity = False
            else:
                self._attempts.pop(key, None)

    def _prune_idle(self, now: float) -> None:
        """Drop keys whose latest attempt has left the window (caller holds the lock).

        Keys are ordered by latest attempt, so pruning stops at the first key that is
        still active; every key after it is newer.
        """
        while self._attempts:
            key, attempts = next(iter(self._attempts.items()))
            if attempts and now - attempts[-1] < self._window:
                return
            del self._attempts[key]

    def _refuse_untracked(self, now: float) -> RateDecision:
        """Refuse an untracked key while ``max_keys`` keys are active (caller holds the lock).

        Nothing is recorded and no counter is dropped. After pruning, the front key has the
        oldest latest attempt among active keys, so it is the first to expire and free a
        slot. The first refusal of a capacity episode is logged, without any address.
        """
        if not self._at_capacity:
            self._at_capacity = True
            _log.warning("auth.login_limiter_at_capacity", max_keys=self._max_keys)
        front = next(iter(self._attempts.values()))
        return self._deny_until_expiry(front[-1], now)

    def _deny_until_expiry(self, counted_at: float, now: float) -> RateDecision:
        """Deny until the attempt counted at ``counted_at`` leaves the window."""
        wait = math.ceil(counted_at + self._window - now)
        # The bounds hold for any clock: at least 1 s, never beyond one window.
        return RateDecision(allowed=False, retry_after=min(max(wait, 1), math.ceil(self._window)))
