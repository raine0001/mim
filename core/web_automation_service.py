import asyncio
import base64
import hashlib
import imaplib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import (
    AutomationAuthChallenge,
    AutomationCarrierRunStatus,
    AutomationEmailMessage,
    AutomationExecutionRun,
    AutomationFileArtifact,
    AutomationPlaybook,
    AutomationWebSession,
)

try:
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None


MFA_CODE_RE = re.compile(r"\b(\d{6})\b")


@dataclass
class RuntimeSession:
    browser: object | None = None
    context: object | None = None
    page: object | None = None


class WebAutomationService:
    def __init__(self) -> None:
        self._runtime_sessions: dict[int, RuntimeSession] = {}
        self._lock = asyncio.Lock()

    def _storage_dir(self) -> Path:
        base = Path(settings.automation_storage_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        (base / "downloads").mkdir(parents=True, exist_ok=True)
        (base / "state").mkdir(parents=True, exist_ok=True)
        return base

    async def create_session(
        self,
        db: AsyncSession,
        *,
        carrier_id: str,
        session_key: str,
        simulation_mode: bool,
        headless: bool,
        start_url: str,
        metadata_json: dict,
    ) -> AutomationWebSession:
        key = session_key.strip() or f"session-{uuid.uuid4().hex[:16]}"
        storage_path = str((self._storage_dir() / "state" / f"{key}.json").resolve())
        session = AutomationWebSession(
            session_key=key,
            carrier_id=carrier_id.strip(),
            simulation_mode=bool(simulation_mode),
            status="active",
            current_url=start_url.strip(),
            storage_state_path=storage_path,
            metadata_json={**metadata_json, "headless": bool(headless)},
        )
        db.add(session)
        await db.flush()

        if not session.simulation_mode:
            await self._open_runtime_session(
                session.id, storage_path=storage_path, headless=headless
            )
            if start_url.strip():
                await self.navigate(
                    db,
                    session,
                    url=start_url,
                    timeout_seconds=settings.automation_default_timeout_seconds,
                )

        return session

    async def _open_runtime_session(
        self, session_id: int, *, storage_path: str, headless: bool
    ) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright_not_installed")
        if not settings.automation_allow_live_browser:
            raise RuntimeError("live_browser_disabled")

        async with self._lock:
            if session_id in self._runtime_sessions:
                return
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=headless)
            if Path(storage_path).exists():
                context = await browser.new_context(storage_state=storage_path)
            else:
                context = await browser.new_context()
            page = await context.new_page()
            self._runtime_sessions[session_id] = RuntimeSession(
                browser=browser, context=context, page=page
            )

    async def close_session(
        self, db: AsyncSession, session: AutomationWebSession
    ) -> None:
        session.status = "closed"
        session.updated_at = datetime.now(timezone.utc)

        runtime = self._runtime_sessions.pop(session.id, None)
        if runtime:
            try:
                if runtime.context:
                    await runtime.context.storage_state(path=session.storage_state_path)
            except Exception:
                pass
            try:
                if runtime.context:
                    await runtime.context.close()
            except Exception:
                pass
            try:
                if runtime.browser:
                    await runtime.browser.close()
            except Exception:
                pass

    async def navigate(
        self,
        db: AsyncSession,
        session: AutomationWebSession,
        *,
        url: str,
        timeout_seconds: int,
    ) -> dict:
        target = url.strip()
        if not target:
            raise RuntimeError("url_required")

        if session.simulation_mode:
            session.current_url = target
            session.last_page_title = "Simulated Page"
            session.last_state_json = {
                "url": target,
                "title": "Simulated Page",
                "mode": "simulation",
                "navigated_at": datetime.now(timezone.utc).isoformat(),
            }
            return session.last_state_json

        runtime = self._runtime_sessions.get(session.id)
        if not runtime or not runtime.page:
            raise RuntimeError("session_runtime_missing")

        await runtime.page.goto(
            target,
            wait_until="domcontentloaded",
            timeout=max(1000, timeout_seconds * 1000),
        )
        session.current_url = str(runtime.page.url)
        try:
            session.last_page_title = await runtime.page.title()
        except Exception:
            session.last_page_title = ""
        session.last_state_json = {
            "url": session.current_url,
            "title": session.last_page_title,
            "mode": "live",
            "navigated_at": datetime.now(timezone.utc).isoformat(),
        }
        return session.last_state_json

    async def perform_action(
        self,
        session: AutomationWebSession,
        *,
        action: str,
        selector: str,
        text: str,
        key: str,
        value: str,
        timeout_seconds: int,
    ) -> dict:
        action_name = action.strip().lower()
        now_iso = datetime.now(timezone.utc).isoformat()

        if session.simulation_mode:
            history = list((session.last_state_json or {}).get("actions", []))
            history.append(
                {
                    "action": action_name,
                    "selector": selector,
                    "text": text,
                    "key": key,
                    "value": value,
                    "at": now_iso,
                }
            )
            session.last_state_json = {
                **(session.last_state_json or {}),
                "actions": history,
                "mode": "simulation",
            }
            return {
                "ok": True,
                "mode": "simulation",
                "action": action_name,
                "timestamp": now_iso,
            }

        runtime = self._runtime_sessions.get(session.id)
        if not runtime or not runtime.page:
            raise RuntimeError("session_runtime_missing")

        page = runtime.page
        timeout_ms = max(1000, timeout_seconds * 1000)
        if action_name == "click":
            await page.click(selector, timeout=timeout_ms)
        elif action_name == "type":
            await page.fill(selector, text or value, timeout=timeout_ms)
        elif action_name == "wait_for":
            if selector:
                await page.wait_for_selector(selector, timeout=timeout_ms)
            elif text:
                await page.get_by_text(text).first.wait_for(timeout=timeout_ms)
            else:
                await page.wait_for_timeout(min(timeout_ms, 5000))
        elif action_name == "press":
            await page.press(selector, key or "Enter", timeout=timeout_ms)
        elif action_name == "select":
            await page.select_option(selector, value=value, timeout=timeout_ms)
        elif action_name == "detect":
            found = False
            if selector:
                try:
                    await page.wait_for_selector(selector, timeout=1000)
                    found = True
                except Exception:
                    found = False
            elif text:
                content = (await page.content()).lower()
                found = text.lower() in content
            return {"ok": True, "mode": "live", "found": found, "action": "detect"}
        else:
            raise RuntimeError(f"unsupported_action:{action_name}")

        session.current_url = str(page.url)
        return {
            "ok": True,
            "mode": "live",
            "action": action_name,
            "url": session.current_url,
            "timestamp": now_iso,
        }

    async def session_state(self, session: AutomationWebSession) -> dict:
        return {
            "session_id": session.id,
            "session_key": session.session_key,
            "carrier_id": session.carrier_id,
            "status": session.status,
            "simulation_mode": session.simulation_mode,
            "current_url": session.current_url,
            "last_page_title": session.last_page_title,
            "last_state": session.last_state_json
            if isinstance(session.last_state_json, dict)
            else {},
            "updated_at": session.updated_at,
        }

    async def create_auth_challenge(
        self,
        db: AsyncSession,
        *,
        session_id: int | None,
        carrier_id: str,
        challenge_type: str,
        channel: str,
        prompt: str,
        metadata_json: dict,
    ) -> AutomationAuthChallenge:
        challenge = AutomationAuthChallenge(
            challenge_key=f"challenge-{uuid.uuid4().hex[:16]}",
            session_id=session_id,
            carrier_id=carrier_id,
            status="open",
            challenge_type=challenge_type,
            channel=channel,
            prompt=prompt,
            metadata_json=metadata_json,
        )
        db.add(challenge)
        await db.flush()
        return challenge

    async def apply_auth_resolution(
        self,
        db: AsyncSession,
        *,
        challenge: AutomationAuthChallenge,
        mfa_code: str,
        actor: str,
        reason: str,
        metadata_json: dict,
    ) -> AutomationAuthChallenge:
        challenge.status = "resolved"
        challenge.resolved_code = mfa_code.strip()
        challenge.resolved_at = datetime.now(timezone.utc)
        challenge.metadata_json = {
            **(
                challenge.metadata_json
                if isinstance(challenge.metadata_json, dict)
                else {}
            ),
            "resolved_by": actor,
            "reason": reason,
            **metadata_json,
        }
        await db.flush()
        return challenge

    async def detect_file(
        self,
        db: AsyncSession,
        *,
        session_id: int | None,
        run_id: int | None,
        carrier_id: str,
        selector: str,
        expected_name_pattern: str,
        source_url: str,
        metadata_json: dict,
    ) -> AutomationFileArtifact:
        suggested_name = (
            expected_name_pattern.strip() or f"{carrier_id or 'carrier'}-report.csv"
        )
        artifact = AutomationFileArtifact(
            run_id=run_id,
            session_id=session_id,
            carrier_id=carrier_id,
            status="detected",
            file_name=suggested_name,
            source_url=source_url,
            metadata_json={"selector": selector, **metadata_json},
        )
        db.add(artifact)
        await db.flush()
        return artifact

    async def store_download(
        self,
        db: AsyncSession,
        *,
        artifact: AutomationFileArtifact | None,
        run_id: int | None,
        session_id: int | None,
        carrier_id: str,
        file_name: str,
        source_url: str,
        content_base64: str,
        metadata_json: dict,
    ) -> AutomationFileArtifact:
        binary = (
            base64.b64decode(content_base64.encode("utf-8")) if content_base64 else b""
        )
        digest = hashlib.sha256(binary).hexdigest() if binary else ""

        target = artifact
        if target is None:
            target = AutomationFileArtifact(
                run_id=run_id,
                session_id=session_id,
                carrier_id=carrier_id,
                status="downloaded",
                file_name=file_name or "download.bin",
                source_url=source_url,
                metadata_json=metadata_json,
            )
            db.add(target)
            await db.flush()

        downloads_dir = self._storage_dir() / "downloads"
        safe_name = (
            (file_name or target.file_name or f"artifact-{target.id}.bin")
            .strip()
            .replace("/", "_")
        )
        file_path = downloads_dir / f"{target.id}-{safe_name}"
        if binary:
            file_path.write_bytes(binary)

        target.status = "downloaded"
        target.file_name = safe_name
        target.file_path = str(file_path)
        target.size_bytes = len(binary)
        target.file_sha256 = digest
        target.source_url = source_url or target.source_url
        target.metadata_json = {
            **(target.metadata_json if isinstance(target.metadata_json, dict) else {}),
            **metadata_json,
        }
        await db.flush()
        return target

    async def poll_email(
        self,
        db: AsyncSession,
        *,
        source: str,
        mailbox: str,
        limit: int,
        subject_contains: str,
        sender_contains: str,
        simulation_messages: list[dict],
        metadata_json: dict,
    ) -> list[AutomationEmailMessage]:
        messages: list[AutomationEmailMessage] = []

        if source == "simulation":
            for item in simulation_messages[:limit]:
                body = str(item.get("body") or "")
                msg = AutomationEmailMessage(
                    message_key=f"sim-{uuid.uuid4().hex[:12]}",
                    source="simulation",
                    sender=str(item.get("sender") or ""),
                    recipient=str(item.get("recipient") or ""),
                    subject=str(item.get("subject") or ""),
                    body_text=body,
                    received_at=datetime.now(timezone.utc),
                    extracted_codes_json=MFA_CODE_RE.findall(body),
                    metadata_json=metadata_json,
                )
                db.add(msg)
                messages.append(msg)
            await db.flush()
            return messages

        if not (
            settings.imap_host and settings.imap_username and settings.imap_password
        ):
            return messages

        conn = (
            imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
            if settings.imap_use_ssl
            else imaplib.IMAP4(settings.imap_host, settings.imap_port)
        )
        try:
            conn.login(settings.imap_username, settings.imap_password)
            conn.select(mailbox or settings.imap_inbox)
            status, data = conn.search(None, "ALL")
            if status != "OK":
                return messages
            ids = data[0].split()[-limit:]
            for msg_id in ids:
                status, payload = conn.fetch(msg_id, "(RFC822)")
                if status != "OK" or not payload:
                    continue
                raw = payload[0][1]
                parsed = message_from_bytes(raw)
                sender = str(parsed.get("From") or "")
                subject = str(parsed.get("Subject") or "")
                if sender_contains and sender_contains.lower() not in sender.lower():
                    continue
                if subject_contains and subject_contains.lower() not in subject.lower():
                    continue
                body = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        ctype = str(part.get_content_type() or "")
                        if ctype == "text/plain":
                            body = (part.get_payload(decode=True) or b"").decode(
                                errors="ignore"
                            )
                            break
                else:
                    body = (parsed.get_payload(decode=True) or b"").decode(
                        errors="ignore"
                    )
                msg = AutomationEmailMessage(
                    message_key=f"imap-{msg_id.decode(errors='ignore')}-{uuid.uuid4().hex[:6]}",
                    source="imap",
                    sender=sender,
                    recipient=str(parsed.get("To") or ""),
                    subject=subject,
                    body_text=body,
                    received_at=datetime.now(timezone.utc),
                    extracted_codes_json=MFA_CODE_RE.findall(body),
                    metadata_json=metadata_json,
                )
                db.add(msg)
                messages.append(msg)
            await db.flush()
            return messages
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                conn.logout()
            except Exception:
                pass

    async def extract_mfa(
        self,
        db: AsyncSession,
        *,
        lookback_minutes: int,
        sender_contains: str,
        subject_contains: str,
    ) -> dict:
        floor = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        rows = (
            (
                await db.execute(
                    select(AutomationEmailMessage)
                    .where(AutomationEmailMessage.created_at >= floor)
                    .order_by(AutomationEmailMessage.id.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )

        candidates = []
        for row in rows:
            if (
                sender_contains
                and sender_contains.lower() not in str(row.sender or "").lower()
            ):
                continue
            if (
                subject_contains
                and subject_contains.lower() not in str(row.subject or "").lower()
            ):
                continue
            codes = (
                row.extracted_codes_json
                if isinstance(row.extracted_codes_json, list)
                else []
            )
            if not codes:
                codes = MFA_CODE_RE.findall(str(row.body_text or ""))
            for code in codes:
                candidates.append(
                    {
                        "code": code,
                        "message_id": row.id,
                        "subject": row.subject,
                        "received_at": row.received_at,
                    }
                )

        best = candidates[0] if candidates else None
        return {
            "ok": bool(best),
            "latest_code": best["code"] if best else "",
            "candidates": candidates[:20],
        }

    def build_google_calendar_auth_url(
        self,
        *,
        state: str,
        scopes: list[str],
        prompt: str,
        access_type: str,
        include_granted_scopes: bool,
    ) -> str:
        if (
            not settings.google_calendar_client_id
            or not settings.google_calendar_redirect_uri
        ):
            raise RuntimeError("calendar_credentials_missing")

        cleaned_scopes = [s.strip() for s in scopes if str(s).strip()]
        if not cleaned_scopes:
            cleaned_scopes = ["https://www.googleapis.com/auth/calendar.events"]

        query = {
            "client_id": settings.google_calendar_client_id,
            "redirect_uri": settings.google_calendar_redirect_uri,
            "response_type": "code",
            "scope": " ".join(cleaned_scopes),
            "prompt": prompt or "consent",
            "access_type": access_type or "offline",
            "include_granted_scopes": "true" if include_granted_scopes else "false",
        }
        if state.strip():
            query["state"] = state.strip()

        return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
            query
        )

    async def exchange_google_calendar_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict:
        if (
            not settings.google_calendar_client_id
            or not settings.google_calendar_client_secret
        ):
            raise RuntimeError("calendar_credentials_missing")

        token_payload = {
            "code": code.strip(),
            "client_id": settings.google_calendar_client_id,
            "client_secret": settings.google_calendar_client_secret,
            "redirect_uri": (
                redirect_uri or settings.google_calendar_redirect_uri
            ).strip(),
            "grant_type": "authorization_code",
        }
        if code_verifier.strip():
            token_payload["code_verifier"] = code_verifier.strip()

        if not token_payload["redirect_uri"]:
            raise RuntimeError("calendar_redirect_uri_missing")

        return await asyncio.to_thread(
            self._post_form_json,
            "https://oauth2.googleapis.com/token",
            token_payload,
        )

    async def create_calendar_reminder(
        self,
        *,
        source: str,
        title: str,
        description: str,
        start_at: datetime,
        end_at: datetime | None,
        timezone_name: str,
        calendar_id: str,
        reminder_minutes: list[int],
        attendees: list[str],
        access_token: str,
        refresh_token: str,
        metadata_json: dict,
    ) -> dict:
        resolved_timezone = self._normalize_timezone_name(timezone_name)

        # Simulation mode is useful for local validation when provider auth is not ready.
        if source == "simulation":
            now_iso = datetime.now(timezone.utc).isoformat()
            effective_end = end_at or (start_at + timedelta(minutes=30))
            return {
                "ok": True,
                "provider": "simulation",
                "calendar_id": calendar_id,
                "event_id": f"sim-{uuid.uuid4().hex[:12]}",
                "html_link": "https://calendar.google.com/calendar",
                "start_at": start_at.isoformat(),
                "end_at": effective_end.isoformat(),
                "title": title,
                "description": description,
                "timezone": resolved_timezone,
                "reminders": sorted({max(0, int(v)) for v in reminder_minutes}) or [30],
                "attendees": [e.strip() for e in attendees if e.strip()],
                "metadata_json": metadata_json
                if isinstance(metadata_json, dict)
                else {},
                "created_at": now_iso,
            }

        token = access_token.strip()
        used_refresh_token = False
        refresh = (
            refresh_token.strip() or settings.google_calendar_refresh_token.strip()
        )
        if not token and refresh:
            token = await self._refresh_google_access_token(refresh)
            used_refresh_token = bool(token)
        if not token:
            raise RuntimeError("calendar_access_token_required")

        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=self._zoneinfo_or_utc(resolved_timezone))
        effective_end = end_at
        if effective_end is None:
            effective_end = start_at + timedelta(minutes=30)
        if effective_end.tzinfo is None:
            effective_end = effective_end.replace(
                tzinfo=self._zoneinfo_or_utc(resolved_timezone)
            )

        cleaned_attendees = [{"email": e.strip()} for e in attendees if e.strip()]
        reminder_overrides = [
            {"method": "popup", "minutes": max(0, int(v))}
            for v in reminder_minutes
            if str(v).strip()
        ]
        if not reminder_overrides:
            reminder_overrides = [{"method": "popup", "minutes": 30}]

        payload = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start_at.isoformat(),
                "timeZone": resolved_timezone,
            },
            "end": {
                "dateTime": effective_end.isoformat(),
                "timeZone": resolved_timezone,
            },
            "reminders": {
                "useDefault": False,
                "overrides": reminder_overrides,
            },
            "extendedProperties": {
                "private": {
                    "created_by": "mim.automation",
                    "metadata_json": json.dumps(
                        metadata_json if isinstance(metadata_json, dict) else {}
                    )[:1024],
                }
            },
        }
        if cleaned_attendees:
            payload["attendees"] = cleaned_attendees

        calendar_key = urllib.parse.quote(calendar_id.strip() or "primary", safe="")
        response = await asyncio.to_thread(
            self._post_json_with_bearer,
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_key}/events",
            payload,
            token,
        )

        return {
            "ok": True,
            "provider": "google",
            "calendar_id": calendar_id.strip() or "primary",
            "event_id": str(response.get("id") or ""),
            "html_link": str(response.get("htmlLink") or ""),
            "status": str(response.get("status") or "confirmed"),
            "used_refresh_token": used_refresh_token,
            "event": response,
        }

    def _normalize_timezone_name(self, timezone_name: str) -> str:
        candidate = (
            str(timezone_name or "").strip()
            or str(settings.automation_default_timezone or "").strip()
        )
        if not candidate:
            candidate = "America/Los_Angeles"
        try:
            ZoneInfo(candidate)
            return candidate
        except Exception:
            fallback = "America/Los_Angeles"
            try:
                ZoneInfo(fallback)
                return fallback
            except Exception:
                return "UTC"

    def _zoneinfo_or_utc(self, timezone_name: str):
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            return timezone.utc

    async def _refresh_google_access_token(self, refresh_token: str) -> str:
        if (
            not settings.google_calendar_client_id
            or not settings.google_calendar_client_secret
        ):
            raise RuntimeError("calendar_credentials_missing")

        response = await asyncio.to_thread(
            self._post_form_json,
            "https://oauth2.googleapis.com/token",
            {
                "client_id": settings.google_calendar_client_id,
                "client_secret": settings.google_calendar_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = str(response.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("calendar_token_refresh_failed")
        return token

    def _post_form_json(self, url: str, data: dict) -> dict:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"google_api_error:{detail or exc.reason}") from exc

    def _post_json_with_bearer(self, url: str, payload: dict, token: str) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"google_api_error:{detail or exc.reason}") from exc


web_automation_service = WebAutomationService()
