from __future__ import annotations

import base64
import binascii
import hmac
import time
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from core.config import PROJECT_ROOT, settings


MIMTOD_AUTH_COOKIE_NAME = "mimtod_operator_session"


@lru_cache(maxsize=1)
def _dotenv_overrides() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (PROJECT_ROOT / ".env", PROJECT_ROOT / "env" / ".env"):
        if not path.exists() or not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            value = raw_value.strip()
            if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {'"', "'"}:
                value = value[1:-1]
            values[key] = value
    return values


def _configured_username() -> str:
    return str(settings.mimtod_user or _dotenv_overrides().get("MIMTOD_USER") or "").strip()


def _configured_password() -> str:
    return str(settings.mimtod_password or _dotenv_overrides().get("MIMTOD_PASSWORD") or "")


def _normalize_host(value: object) -> str:
    host = str(value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")].strip().lower()
    if ":" in host:
        return host.rsplit(":", 1)[0].strip().lower()
    return host


def _request_host_name(request: Request) -> str:
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
    if forwarded_host:
        return _normalize_host(forwarded_host.split(",", 1)[0])
    return _normalize_host(request.headers.get("host") or request.url.netloc or request.url.hostname)


def _effective_request_scheme(request: Request) -> str:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip()
    if forwarded_proto:
        return forwarded_proto.split(",", 1)[0].strip().lower()
    return str(request.url.scheme or "http").strip().lower() or "http"


def _is_loopback_host(host: str) -> bool:
    return host in {"", "localhost", "127.0.0.1", "::1"}


def _configured_public_hosts() -> set[str]:
    hosts: set[str] = set()
    for value in (
        settings.remote_shell_hostname,
        settings.remote_shell_domain,
        settings.remote_shell_zone,
    ):
        text = str(value or "").strip()
        if not text:
            continue
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlparse(candidate)
        host = _normalize_host(parsed.hostname or text)
        if host:
            hosts.add(host)
    zone = _normalize_host(settings.remote_shell_zone)
    if zone:
        hosts.add(zone)
        hosts.add(f"www.{zone}")
    return hosts


def mimtod_login_configured() -> bool:
    return bool(
        settings.mimtod_login_enabled
        and _configured_username()
        and _configured_password()
    )


def mimtod_auth_required(request: Request) -> bool:
    if not mimtod_login_configured():
        return False
    request_host = _request_host_name(request)
    protected_hosts = _configured_public_hosts()
    if protected_hosts:
        return request_host in protected_hosts
    return not _is_loopback_host(request_host)


def normalize_next_path(next_path: object, *, default: str = "/mim") -> str:
    text = str(next_path or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        return default
    return text


def login_redirect_url(next_path: object = "/mim") -> str:
    normalized = normalize_next_path(next_path)
    return f"/mim/login?next={quote(normalized, safe='/?:&=%')}"


def _session_secret() -> bytes:
    seed = f"{_configured_username()}:{_configured_password()}".encode("utf-8")
    return sha256(seed).digest()


def _urlsafe_b64decode(value: str) -> bytes:
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def build_session_token(*, username: str) -> str:
    expires_at = int(time.time()) + max(300, int(settings.mimtod_session_hours) * 3600)
    payload = f"{username}\n{expires_at}".encode("utf-8")
    signature = hmac.new(_session_secret(), payload, sha256).hexdigest().encode("ascii")
    token = base64.urlsafe_b64encode(payload + b"." + signature).decode("ascii")
    return token.rstrip("=")


def credentials_match(*, username: str, password: str) -> bool:
    return hmac.compare_digest(username, _configured_username()) and hmac.compare_digest(
        password,
        _configured_password(),
    )


def _basic_auth_credentials(request: Request) -> tuple[str, str] | None:
    authorization = str(request.headers.get("authorization") or "").strip()
    if not authorization.lower().startswith("basic "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        decoded = base64.b64decode(token.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if ":" not in decoded:
        return None
    return decoded.split(":", 1)


def request_has_valid_mimtod_auth(request: Request) -> bool:
    if not mimtod_auth_required(request):
        return True
    basic_credentials = _basic_auth_credentials(request)
    if basic_credentials is not None:
        username, password = basic_credentials
        if credentials_match(username=username, password=password):
            return True
    token = str(request.cookies.get(MIMTOD_AUTH_COOKIE_NAME) or "").strip()
    if not token:
        return False
    try:
        decoded = _urlsafe_b64decode(token)
    except (ValueError, binascii.Error):
        return False
    try:
        payload, signature = decoded.rsplit(b".", 1)
    except ValueError:
        return False
    expected_signature = hmac.new(_session_secret(), payload, sha256).hexdigest().encode("ascii")
    if not hmac.compare_digest(signature, expected_signature):
        return False
    try:
        username, expires_at_text = payload.decode("utf-8").split("\n", 1)
        expires_at = int(expires_at_text)
    except (UnicodeDecodeError, ValueError):
        return False
    if username != _configured_username():
        return False
    return expires_at >= int(time.time())


def ensure_authenticated_mimtod_api_request(request: Request) -> None:
    if request_has_valid_mimtod_auth(request):
        return
    raise HTTPException(status_code=401, detail="mimtod_login_required")


def maybe_require_mimtod_page_login(request: Request, *, next_path: str) -> RedirectResponse | None:
    if request_has_valid_mimtod_auth(request):
        return None
    return RedirectResponse(url=login_redirect_url(next_path), status_code=303)


def set_authenticated_mimtod_cookie(response: RedirectResponse, request: Request, *, username: str) -> None:
    secure_cookie = _effective_request_scheme(request) == "https" and not _is_loopback_host(
        _request_host_name(request)
    )
    response.set_cookie(
        key=MIMTOD_AUTH_COOKIE_NAME,
        value=build_session_token(username=username),
        max_age=max(300, int(settings.mimtod_session_hours) * 3600),
        httponly=True,
        samesite="lax",
        secure=secure_cookie,
        path="/",
    )


def clear_authenticated_mimtod_cookie(response: RedirectResponse, request: Request) -> None:
    secure_cookie = _effective_request_scheme(request) == "https" and not _is_loopback_host(
        _request_host_name(request)
    )
    response.delete_cookie(
        key=MIMTOD_AUTH_COOKIE_NAME,
        path="/",
        samesite="lax",
        secure=secure_cookie,
    )