import asyncio
import hmac
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pydantic import BaseModel, Field

from mensa.config import Settings
from mensa.discord_api import DiscordAPI, DiscordError
from mensa.storage import Store, parse_paid_csv

log = logging.getLogger(__name__)


def private_message(content: str):
    return {"type": 4, "data": {"content": content, "flags": 64, "allowed_mentions": {"parse": []}}}


def registration_form():
    return {
        "type": 9,
        "data": {
            "custom_id": "mensa:link",
            "title": "Link Mensa membership",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 4,
                            "custom_id": "member_number",
                            "label": "Your Mensa member number",
                            "style": 1,
                            "min_length": 1,
                            "max_length": 20,
                            "required": True,
                            "placeholder": "For example: 10001",
                        }
                    ],
                }
            ],
        },
    }


class PaidImport(BaseModel):
    csv: str = Field(max_length=1_000_000)
    confirmation: str | None = None
    allow_empty: bool = False


class MembershipService:
    def __init__(self, settings, store, discord):
        self.settings = settings
        self.store = store
        self.discord = discord
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()

    async def reconcile(self, all_links=False):
        async with self.lock:
            if not self.store.initialized():
                return {"updated": 0, "failed": 0, "waiting_for_paid_list": True}
            updated = failed = 0
            for target in self.store.targets(all_links):
                row = self.store.get_link(target["user_id"])
                try:
                    await self.discord.set_paid_role(
                        self.settings.guild_id,
                        row["user_id"],
                        self.settings.paid_role_id,
                        bool(row["paid"]),
                    )
                except (DiscordError, httpx.HTTPError) as exc:
                    # Do not log HTTP exception text: it may contain credentials in URLs.
                    error = str(exc) if isinstance(exc, DiscordError) else "Discord network error"
                    self.store.mark_synced(row["user_id"], row["revision"], error)
                    failed += 1
                else:
                    self.store.mark_synced(row["user_id"], row["revision"])
                    updated += 1
            return {"updated": updated, "failed": failed}

    async def worker(self):
        next_full = 0.0
        while True:
            self.wake.clear()
            full = time.monotonic() >= next_full
            try:
                await self.reconcile(all_links=full)
            except sqlite3.Error:
                log.error("Membership reconciliation failed; will retry", exc_info=False)
            if full:
                next_full = time.monotonic() + self.settings.reconcile_seconds
            try:
                await asyncio.wait_for(
                    self.wake.wait(), timeout=min(30, self.settings.reconcile_seconds)
                )
            except TimeoutError:
                pass


def create_app(settings: Settings | None = None, *, discord=None, start_worker=True):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)
    api = discord or DiscordAPI(settings.bot_token)
    service = MembershipService(settings, store, api)
    verify_key = VerifyKey(bytes.fromhex(settings.public_key))

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(service.worker()) if start_worker else None
        app.state.worker = task
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await api.close()

    app = FastAPI(
        title="Mensa Membership", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.service = service

    @app.middleware("http")
    async def limit_body(request, call_next):
        # Bound streaming bodies as well as bodies with Content-Length.
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1_100_000:
                from starlette.responses import Response

                return Response(status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def admin(request: Request):
        expected = "Bearer " + settings.admin_token
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), expected.encode()
        ):
            raise HTTPException(401, "Authentication required")

    @app.get("/healthz")
    async def health():
        worker = getattr(app.state, "worker", None)
        if worker is not None and worker.done():
            raise HTTPException(503, "Membership worker is not running")
        store.summary()
        return {"status": "ok"}

    def status_message(user_id):
        row = store.get_link(user_id)
        if not row or not row["member_number"]:
            return private_message(
                "Your account is not linked. Use /membership to enter your member number."
            )
        content = f"Linked to member number **{row['member_number']}**. "
        if not store.initialized():
            content += "Waiting for the administrator to upload a paid-member list."
        elif row["last_error"]:
            content += "Your access update needs attention; it will be retried. Contact an administrator if it persists."
        elif row["pending"]:
            content += "Your access update is queued. Check /membership-status again shortly."
        elif row["paid"]:
            content += "You are on the current paid-member list. Paid member access is active."
        else:
            content += "You are not on the current paid-member list. Contact an administrator if you have paid."
        return private_message(content)

    @app.post("/interactions")
    async def interactions(request: Request):
        body = await request.body()
        if len(body) > 65536:
            raise HTTPException(413, "Interaction too large")
        timestamp = request.headers.get("x-signature-timestamp", "")
        signature = request.headers.get("x-signature-ed25519", "")
        try:
            if abs(time.time() - int(timestamp)) > 300:
                raise ValueError("expired")
            verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature))
        except (ValueError, BadSignatureError):
            raise HTTPException(401, "Invalid request signature") from None
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise TypeError("not an object")
        except (ValueError, TypeError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        if payload.get("type") == 1:
            return {"type": 1}
        if payload.get("application_id") != settings.application_id:
            raise HTTPException(403, "Wrong application")
        if payload.get("guild_id") != settings.guild_id:
            return private_message("This app is only available in its configured server.")
        user_id = payload.get("member", {}).get("user", {}).get("id")
        interaction_id = payload.get("id")
        if not isinstance(user_id, str) or not user_id.isascii() or not user_id.isdigit():
            raise HTTPException(400, "Missing Discord user ID")
        if not isinstance(interaction_id, str) or not interaction_id.isdigit():
            raise HTTPException(400, "Missing interaction ID")
        cached = store.cached_response(interaction_id)
        if cached:
            return cached
        data = payload.get("data", {})
        kind = payload.get("type")
        if (kind == 2 and data.get("name") == "membership") or (
            kind == 3 and data.get("custom_id") == "mensa:register"
        ):
            row = store.get_link(user_id)
            response = (
                status_message(user_id) if row and row["member_number"] else registration_form()
            )
        elif kind == 2 and data.get("name") == "membership-status":
            response = status_message(user_id)
        elif kind == 5 and data.get("custom_id") == "mensa:link":
            try:
                fields = [
                    field
                    for group in data["components"]
                    for field in group["components"]
                    if field.get("custom_id") == "member_number"
                ]
                if len(fields) != 1 or not isinstance(fields[0].get("value"), str):
                    raise ValueError("Enter a valid member number.")
                store.link(user_id, fields[0]["value"])
                service.wake.set()
                response = status_message(user_id)
            except (KeyError, TypeError):
                response = private_message("Enter a valid member number.")
            except ValueError as exc:
                response = private_message(str(exc))
        else:
            response = private_message("Unknown action. Use /membership.")
        store.save_response(interaction_id, response)
        return response

    @app.get("/admin/status", dependencies=[Depends(admin)])
    async def admin_status():
        return store.summary()

    @app.post("/admin/paid-members", dependencies=[Depends(admin)])
    async def paid_members(document: PaidImport):
        try:
            numbers = parse_paid_csv(document.csv)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        preview = store.import_preview(numbers)
        if document.confirmation is None:
            return {**preview, "applied": False}
        if document.confirmation != preview["confirmation"]:
            raise HTTPException(409, "Paid list changed. Preview this import again.")
        if not numbers and not document.allow_empty:
            raise HTTPException(400, "Empty paid list requires allow_empty=true.")
        store.replace_paid(numbers)
        service.wake.set()
        return {**preview, "applied": True}

    @app.post("/admin/sync", dependencies=[Depends(admin)])
    async def sync():
        return await service.reconcile(all_links=True)

    @app.post("/admin/unlink/{user_id}", dependencies=[Depends(admin)])
    async def unlink(user_id: str):
        store.unlink(user_id)
        service.wake.set()

        return {"queued": True}

    return app
