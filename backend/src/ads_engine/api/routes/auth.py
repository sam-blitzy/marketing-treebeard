"""Session management routes: ``POST /auth/login``, ``POST /auth/logout``, ``GET /auth/me``.

The product has a single admin login (AAP 0.3.2, 0.5.3). Paths are relative to ``/api/v1``;
``ads_engine.api.app.create_app`` mounts :data:`router` with the CSRF dependency, so login and
logout already require ``X-Requested-With: ads-engine`` (403 ``csrf_header_missing``), and
installs Starlette's ``SessionMiddleware``, which writes the signed, HttpOnly, SameSite=Lax
session cookie (``ads_engine_session``, 12 h, Secure for an ``https://`` ``APP_BASE_URL``).

This module holds no security primitive of its own. Password hashing and verification, the
session claims, the client-IP key and the 5-per-minute login limiter all live in
:mod:`ads_engine.api.security`; the routes only sequence them and turn a negative decision
into an :class:`~ads_engine.api.deps.ApiError`:

* 429 ``rate_limited`` with ``Retry-After`` on the 6th login attempt within 60 s from one
  client IP. Every attempt counts, successful or not, and the check runs before any lookup.
* 401 ``invalid_credentials`` with one message for an unknown email and a wrong password,
  so the response never reveals which part failed. An unknown email still pays for one
  argon2 verification (:func:`~ads_engine.api.security.verify_dummy`), so the response time
  does not reveal it either.
* 401 ``unauthenticated`` on logout and ``/auth/me`` without a valid session, from
  :func:`~ads_engine.api.deps.require_user`.

Logging (AAP 0.11.1, "authentication failures"): events carry a reason and, where known, the
user id. No email, password, hash, cookie or client IP is ever logged or placed in an error
message.

Example:
    The SPA signs in and reads the session like this::

        POST /api/v1/auth/login
        X-Requested-With: ads-engine
        {"email": "admin@example.com", "password": "..."}
        -> 200 {"email": "admin@example.com"}, Set-Cookie: ads_engine_session=...

        GET /api/v1/auth/me  -> 200 {"email": "admin@example.com"} (401 when signed out)
"""

from typing import Final

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from ads_engine.api import schemas, security
from ads_engine.api.deps import ApiError, LimiterDep, NowDep, SessionDep, UserDep
from ads_engine.db.models.ops import AppUser
from ads_engine.logging import get_logger

__all__ = ["login", "logout", "me", "router"]

router = APIRouter(tags=["auth"])
"""The auth routes; ``create_app`` includes it under ``/api/v1`` with the CSRF guard."""

_log = get_logger(__name__)

_INVALID_CREDENTIALS_CODE: Final = "invalid_credentials"
# One message for an unknown email and a wrong password, so neither is distinguishable.
_INVALID_CREDENTIALS_MESSAGE: Final = "Invalid email or password"
_RATE_LIMITED_CODE: Final = "rate_limited"
_RATE_LIMITED_MESSAGE: Final = "Too many login attempts; try again later"


def _invalid_credentials() -> ApiError:
    """Build the single 401 every credential failure returns, whatever its cause."""
    return ApiError(401, _INVALID_CREDENTIALS_CODE, _INVALID_CREDENTIALS_MESSAGE)


async def _store_rehash(session: AsyncSession, user: AppUser, password: str, user_id: int) -> None:
    """Replace an outdated password hash with one using the current argon2 parameters.

    The rehash is opportunistic: the password has already been verified, so a database
    failure while storing the new hash is rolled back and logged by error type, and the
    sign-in proceeds with the old hash still stored (the next sign-in retries). Hashing runs
    in the thread pool, because argon2 is CPU- and memory-bound.

    Args:
        session: The request's database session, which loaded ``user``.
        user: The authenticated user row.
        password: The verified plaintext password; never logged or stored.
        user_id: ``user.id``, read before any rollback could expire the row.

    Raises:
        argon2.exceptions.HashingError: When the argon2 library itself fails to hash.
    """
    user.password_hash = await run_in_threadpool(security.hash_password, password)
    try:
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        _log.warning("auth.password_rehash_failed", user_id=user_id, error=type(exc).__name__)
        return
    _log.info("auth.password_rehashed", user_id=user_id)


@router.post(
    "/auth/login",
    response_model=schemas.Me,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def login(
    body: schemas.LoginRequest,
    request: Request,
    session: SessionDep,
    limiter: LimiterDep,
    now: NowDep,
) -> schemas.Me:
    """Authenticate the single admin and set the session cookie.

    Login is limited to 5 attempts per minute per client IP; every attempt counts, so the
    6th within 60 s gets 429 before any account is looked up. The email arrives trimmed and
    lowercased by :class:`~ads_engine.api.schemas.LoginRequest`, the form ``ads-engine
    ensure-admin`` stores. An unknown email and a wrong password return the same 401 after
    the same argon2 work. On success the session holds the user id, the email and the
    sign-in instant (``now``), and ``SessionMiddleware`` writes the cookie.

    Args:
        body: The submitted ``{email, password}``.
        request: The current request; carries the session and the client address.
        session: The request's database session.
        limiter: The application's login rate limiter.
        now: The sign-in instant, the absolute start of the 12 h session lifetime.

    Returns:
        The signed-in admin's ``{email}``.

    Raises:
        ApiError: 429 ``rate_limited`` with ``Retry-After`` when the client IP has used its
            5 attempts in the last minute; 401 ``invalid_credentials`` when the email is
            unknown or the password is wrong.
    """
    decision = limiter.check(security.client_ip(request))
    if not decision.allowed:
        # The client IP is deliberately not logged: only the wait the client was given.
        _log.warning("auth.rate_limited", retry_after=decision.retry_after)
        raise ApiError(
            429,
            _RATE_LIMITED_CODE,
            _RATE_LIMITED_MESSAGE,
            headers={"Retry-After": str(decision.retry_after)},
        )

    user = await session.scalar(select(AppUser).where(AppUser.email == body.email))
    if user is None:
        # Same argon2 cost as a wrong password, so timing does not reveal unknown emails.
        await run_in_threadpool(security.verify_dummy, body.password)
        _log.warning("auth.login_failed", reason="unknown_user")
        raise _invalid_credentials()

    # Read before any rollback in the rehash step could expire the row's attributes.
    user_id = user.id
    email = user.email

    check = await run_in_threadpool(security.verify_password, user.password_hash, body.password)
    if not check.ok:
        _log.warning("auth.login_failed", reason="bad_password", user_id=user_id)
        raise _invalid_credentials()

    if check.needs_rehash:
        await _store_rehash(session, user, body.password, user_id)

    security.start_session(request, user_id=user_id, email=email, now=now)
    _log.info("auth.login_succeeded", user_id=user_id)
    return schemas.Me(email=email)


@router.post(
    "/auth/logout",
    response_class=Response,
    status_code=204,
    responses=schemas.ERROR_RESPONSES,
)
async def logout(request: Request, user: UserDep) -> Response:
    """Sign out by clearing the session cookie.

    Requires a valid session. The session is cleared, so ``SessionMiddleware`` sends the
    cookie back already expired, which removes it from the browser.

    Args:
        request: The current request; carries the session.
        user: The signed-in user, verified by ``require_user``.

    Returns:
        An empty 204 response.

    Raises:
        ApiError: 401 ``unauthenticated`` without a valid session (from ``require_user``).
    """
    security.end_session(request)
    _log.info("auth.logged_out", user_id=user.id)
    return Response(status_code=204)


@router.get(
    "/auth/me",
    response_model=schemas.Me,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def me(user: UserDep) -> schemas.Me:
    """Return the signed-in admin.

    The SPA calls it on load to decide between ``/login`` and ``/actions``.

    Args:
        user: The signed-in user, verified by ``require_user``.

    Returns:
        The signed-in admin's ``{email}``.

    Raises:
        ApiError: 401 ``unauthenticated`` without a valid session (from ``require_user``).
    """
    return schemas.Me(email=user.email)
