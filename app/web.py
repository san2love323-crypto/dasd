"""VENDRIX 웹 패널 (aiohttp)."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any

import aiohttp_jinja2
import jinja2
from aiohttp import web

from . import utils
from .config import Settings
from .db import Database, DatabaseError
from .plans import public_catalog, resolve_plan
from .service import ShopError, ShopService, product_json

log = logging.getLogger("vendrix.web")

COOKIE_NAME = "vdx_session"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def json_safe(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(self._hits) > 5000:
            for stale in [k for k, v in self._hits.items() if not v][:1000]:
                self._hits.pop(stale, None)
        return True


class WebServer:
    def __init__(self, db: Database, settings: Settings, service: ShopService):
        self.db = db
        self.settings = settings
        self.service = service
        self.pushbullet: Any | None = None
        self.limiter = RateLimiter()
        self.app = web.Application(
            middlewares=[self._error_middleware, self._security_middleware, self._rate_middleware],
            client_max_size=1024 * 1024,
        )
        loader = jinja2.FileSystemLoader(str(settings.web_dir / "templates"))
        aiohttp_jinja2.setup(self.app, loader=loader, autoescape=True)
        self._routes()
        self._runner: web.AppRunner | None = None

    # -------------------------------------------------------------- middleware
    @web.middleware
    async def _error_middleware(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=exc.status)
        except ShopError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except DatabaseError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            log.exception("unhandled error on %s %s", request.method, request.path)
            if request.path.startswith("/api/"):
                return web.json_response({"ok": False, "error": "서버 오류가 발생했습니다."}, status=500)
            raise

    @web.middleware
    async def _security_middleware(self, request: web.Request, handler):
        if request.path.startswith("/api/") and request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if request.headers.get("X-VDX-CSRF") != "1":
                return web.json_response({"ok": False, "error": "CSRF 검증에 실패했습니다."}, status=403)
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != self.settings.origin:
                return web.json_response({"ok": False, "error": "허용되지 않은 Origin 입니다."}, status=403)

        response = await handler(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.path.startswith("/api/") or request.path in {"/panel", "/admin"}:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @web.middleware
    async def _rate_middleware(self, request: web.Request, handler):
        ip = request.remote or "?"
        if request.path.startswith("/entry/"):
            if not self.limiter.allow(f"entry:{ip}", 30, 60):
                return web.Response(text="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.", status=429)
        elif request.path.startswith("/api/"):
            if not self.limiter.allow(f"api:{ip}", 240, 60):
                return web.json_response({"ok": False, "error": "요청이 너무 많습니다."}, status=429)
        return await handler(request)

    # ------------------------------------------------------------------ routes
    def _routes(self) -> None:
        router = self.app.router
        router.add_get("/", self.landing)
        router.add_get("/entry/{token}", self.entry)
        router.add_get("/panel", self.panel_page)
        router.add_get("/admin", self.admin_page)
        router.add_get("/logout", self.logout)
        router.add_get("/healthz", self.health)

        router.add_get("/api/panel", self.api_panel)
        router.add_post("/api/purchase", self.api_purchase)
        router.add_post("/api/deposit", self.api_deposit)

        router.add_get("/api/admin", self.api_admin)
        router.add_post("/api/admin/product", self.api_product_create)
        router.add_patch("/api/admin/product/{product_id}", self.api_product_update)
        router.add_delete("/api/admin/product/{product_id}", self.api_product_delete)
        router.add_post("/api/admin/product/{product_id}/stock", self.api_stock_add)
        router.add_get("/api/admin/product/{product_id}/stock", self.api_stock_list)
        router.add_post("/api/admin/charge", self.api_charge)
        router.add_post("/api/admin/deposit/{deposit_id}/approve", self.api_deposit_approve)
        router.add_post("/api/admin/deposit/{deposit_id}/ignore", self.api_deposit_ignore)
        router.add_post("/api/admin/purchase/{purchase_id}/redeliver", self.api_redeliver)
        router.add_post("/api/admin/settings", self.api_settings)
        router.add_get("/api/admin/users", self.api_users)

        router.add_get("/api/owner/summary", self.api_owner_summary)
        router.add_get("/api/owner/licenses", self.api_owner_licenses)
        router.add_post("/api/owner/licenses", self.api_owner_issue)
        router.add_post("/api/owner/licenses/revoke", self.api_owner_revoke)

        router.add_static("/static", self.settings.web_dir / "static", name="static")

    # ------------------------------------------------------------------ helpers
    def _session(self, request: web.Request) -> dict | None:
        raw = request.cookies.get(COOKIE_NAME)
        if not raw:
            return None
        return self.db.get_session(utils.sha256(raw))

    def _require_session(self, request: web.Request) -> dict:
        session = self._session(request)
        if session is None:
            raise ApiError("로그인이 필요합니다. 디스코드에서 /웹패널 을 다시 입력해 주세요.", 401)
        return session

    def _is_admin(self, session: dict) -> bool:
        if session.get("is_owner") or int(session["discord_id"]) in self.settings.owner_ids:
            return True
        bot = self.service.bot
        if bot is not None and bot.is_ready():
            try:
                return self.service.is_admin_member(session["guild_id"], session["discord_id"])
            except Exception:
                pass
        return bool(session.get("is_admin"))

    def _require_admin(self, request: web.Request) -> dict:
        session = self._require_session(request)
        if not self._is_admin(session):
            raise ApiError("서버 관리자만 사용할 수 있습니다.", 403)
        return session

    def _require_owner(self, request: web.Request) -> dict:
        session = self._require_session(request)
        if session.get("is_owner") or int(session["discord_id"]) in self.settings.owner_ids:
            return session
        raise ApiError("봇 오너만 사용할 수 있습니다.", 403)

    def _render(
        self, request: web.Request, template: str, context: dict | None = None, status: int = 200
    ) -> web.Response:
        ctx = {
            "brand": self.settings.brand_name,
            "year": utils.kst_now().year,
            "support_url": self.settings.support_url,
        }
        ctx.update(context or {})
        return aiohttp_jinja2.render_template(template, request, ctx, status=status)

    async def _json_body(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise ApiError("JSON 본문이 필요합니다.")
        if not isinstance(data, dict):
            raise ApiError("JSON 객체가 필요합니다.")
        return data

    def _int(self, data: dict, key: str, default: int | None = None, low: int = -10**9, high: int = 10**9) -> int:
        raw = data.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ApiError(f"{key} 값이 올바르지 않습니다.")
        if value < low or value > high:
            raise ApiError(f"{key} 값이 범위를 벗어났습니다.")
        return value

    # ------------------------------------------------------------------- pages
    async def landing(self, request: web.Request) -> web.StreamResponse:
        session = self._session(request)
        if session:
            raise web.HTTPFound("/admin" if self._is_admin(session) else "/panel")
        return self._render(request, "landing.html")

    async def entry(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info.get("token", "")
        if not token or len(token) > 200:
            return self._render(request, "error.html", {"message": "잘못된 접근입니다."}, status=404)
        row = self.db.consume_login_token(utils.sha256(token))
        if not row:
            return self._render(
                request,
                "error.html",
                {"message": "링크가 만료되었거나 이미 사용되었습니다. 디스코드에서 /웹패널 을 다시 입력해 주세요."},
                status=410,
            )
        is_owner = int(row["discord_id"]) in self.settings.owner_ids
        session_token = utils.gen_token(32)
        self.db.create_session(
            utils.sha256(session_token),
            row["guild_id"],
            row["discord_id"],
            bool(row["is_admin"]),
            is_owner,
            self.settings.session_days * 86400,
        )
        self.db.audit(row["guild_id"], row["discord_id"], "web.login", f"admin={bool(row['is_admin'])}")
        target = "/admin" if (row["is_admin"] or is_owner) else "/panel"
        response = web.HTTPFound(target)
        response.set_cookie(
            COOKIE_NAME,
            session_token,
            max_age=self.settings.session_days * 86400,
            httponly=True,
            samesite="Lax",
            secure=self.settings.cookie_secure,
            path="/",
        )
        raise response

    async def panel_page(self, request: web.Request) -> web.StreamResponse:
        session = self._session(request)
        if not session:
            raise web.HTTPFound("/")
        try:
            payload = self.service.shop_payload(session["guild_id"], session["discord_id"])
        except ShopError as exc:
            return self._render(request, "error.html", {"message": str(exc)}, status=400)
        boot = {
            "page": "shop",
            "payload": payload,
            "session": {
                "discord_id": session["discord_id"],
                "guild_id": session["guild_id"],
                "is_admin": self._is_admin(session),
                "is_owner": bool(session.get("is_owner")),
            },
        }
        return self._render(request, "panel.html", {"boot": json_safe(boot), "page": "shop"})

    async def admin_page(self, request: web.Request) -> web.StreamResponse:
        session = self._session(request)
        if not session:
            raise web.HTTPFound("/")
        if not self._is_admin(session):
            return self._render(request, "error.html", {"message": "서버 관리자만 접근할 수 있습니다."}, status=403)
        try:
            payload = self.service.admin_payload(session["guild_id"])
        except ShopError as exc:
            return self._render(request, "error.html", {"message": str(exc)}, status=400)
        boot = {
            "page": "admin",
            "payload": payload,
            "session": {
                "discord_id": session["discord_id"],
                "guild_id": session["guild_id"],
                "is_admin": True,
                "is_owner": bool(session.get("is_owner") or int(session["discord_id"]) in self.settings.owner_ids),
            },
        }
        return self._render(request, "admin.html", {"boot": json_safe(boot), "page": "admin"})

    async def logout(self, request: web.Request) -> web.StreamResponse:
        raw = request.cookies.get(COOKIE_NAME)
        if raw:
            self.db.delete_session(utils.sha256(raw))
        response = web.HTTPFound("/")
        response.del_cookie(COOKIE_NAME, path="/")
        raise response

    async def health(self, request: web.Request) -> web.Response:
        bot = self.service.bot
        return web.json_response(
            {
                "ok": True,
                "bot": bool(bot is not None and bot.is_ready()),
                "pushbullet": self.pushbullet.status() if self.pushbullet else {"enabled": False},
                "time": utils.now(),
            }
        )

    # -------------------------------------------------------------------- api
    async def api_panel(self, request: web.Request) -> web.Response:
        session = self._require_session(request)
        payload = self.service.shop_payload(session["guild_id"], session["discord_id"])
        return web.json_response({"ok": True, **payload})

    async def api_purchase(self, request: web.Request) -> web.Response:
        session = self._require_session(request)
        data = await self._json_body(request)
        product_id = self._int(data, "product_id", low=1)
        quantity = self._int(data, "quantity", 1, low=1, high=10)
        result = await self.service.purchase(
            session["guild_id"], session["discord_id"], product_id, quantity, channel="web"
        )
        return web.json_response(
            {
                "ok": True,
                "purchase_id": result["purchase_id"],
                "product": result["product"]["name"],
                "quantity": result["quantity"],
                "total": result["total"],
                "total_text": utils.fmt_won(result["total"]),
                "balance": result["balance"],
                "balance_text": utils.fmt_won(result["balance"]),
                "delivery": result["delivery"],
            }
        )

    async def api_deposit(self, request: web.Request) -> web.Response:
        session = self._require_session(request)
        data = await self._json_body(request)
        amount = self._int(data, "amount", low=1, high=10_000_000)
        depositor = str(data.get("depositor") or "").strip()[:20]
        pending = self.service.create_deposit_request(
            session["guild_id"], session["discord_id"], amount, depositor
        )
        return web.json_response(
            {
                "ok": True,
                "pending": {
                    "id": pending["id"],
                    "amount": pending["amount"],
                    "amount_text": utils.fmt_won(pending["amount"]),
                    "depositor": pending["depositor"],
                    "code": pending["code"],
                    "expires_text": utils.fmt_dt(pending["expires_at"]),
                },
                "guide": self.service.deposit_guide(pending),
            }
        )

    async def api_admin(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        return web.json_response({"ok": True, **self.service.admin_payload(session["guild_id"])})

    async def api_product_create(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        data = await self._json_body(request)
        name = str(data.get("name") or "").strip()[:80]
        if not name:
            raise ApiError("상품명을 입력해 주세요.")
        price = self._int(data, "price", low=0, high=100_000_000)
        delivery_type = str(data.get("delivery_type") or "manual").strip()
        if delivery_type not in {"role", "content", "manual"}:
            raise ApiError("전달 방식은 role / content / manual 중 하나입니다.")
        role_id = str(data.get("role_id") or "").strip() or None
        if delivery_type == "role":
            if not role_id or not role_id.isdigit():
                raise ApiError("역할 상품은 역할 ID가 필요합니다.")
        else:
            role_id = None
        stock = self._int(data, "stock", -1, low=-1, high=1_000_000)
        product = self.service.create_product(
            session["guild_id"],
            name,
            price,
            str(data.get("description") or "").strip()[:200],
            delivery_type=delivery_type,
            role_id=role_id,
            stock=stock,
            sort_order=self._int(data, "sort_order", 0, low=0, high=1000),
        )
        self.db.audit(session["guild_id"], session["discord_id"], "product.create", f"{name} price={price}")
        return web.json_response({"ok": True, "product": product_json(product)})

    def _product_for_guild(self, product_id: int, guild_id: str) -> dict:
        product = self.db.get_product(product_id, guild_id)
        if product is None:
            raise ApiError("상품을 찾을 수 없습니다.", 404)
        return product

    async def api_product_update(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        product_id = int(request.match_info["product_id"])
        product = self._product_for_guild(product_id, session["guild_id"])
        data = await self._json_body(request)
        fields: dict[str, Any] = {}
        if "name" in data:
            name = str(data["name"]).strip()[:80]
            if not name:
                raise ApiError("상품명을 입력해 주세요.")
            fields["name"] = name
        if "description" in data:
            fields["description"] = str(data["description"]).strip()[:200]
        if "price" in data:
            fields["price"] = self._int(data, "price", low=0, high=100_000_000)
        if "delivery_type" in data:
            delivery_type = str(data["delivery_type"]).strip()
            if delivery_type not in {"role", "content", "manual"}:
                raise ApiError("전달 방식이 올바르지 않습니다.")
            fields["delivery_type"] = delivery_type
        if "role_id" in data:
            role_id = str(data.get("role_id") or "").strip() or None
            fields["role_id"] = role_id if (role_id and role_id.isdigit()) else None
        if "stock" in data:
            fields["stock"] = self._int(data, "stock", low=-1, high=1_000_000)
        if "active" in data:
            fields["active"] = 1 if data["active"] else 0
        if "sort_order" in data:
            fields["sort_order"] = self._int(data, "sort_order", low=0, high=1000)
        updated = self.service.update_product(product_id, session["guild_id"], **fields)
        self.db.audit(session["guild_id"], session["discord_id"], "product.update", f"#{product_id} {fields}")
        return web.json_response({"ok": True, "product": product_json(updated or product)})

    async def api_product_delete(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        product_id = int(request.match_info["product_id"])
        self._product_for_guild(product_id, session["guild_id"])
        self.db.delete_product(product_id, session["guild_id"])
        self.db.audit(session["guild_id"], session["discord_id"], "product.delete", f"#{product_id}")
        return web.json_response({"ok": True})

    async def api_stock_add(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        product_id = int(request.match_info["product_id"])
        self._product_for_guild(product_id, session["guild_id"])
        data = await self._json_body(request)
        raw = data.get("lines")
        if isinstance(raw, list):
            lines = [str(item) for item in raw]
        else:
            lines = str(raw or "").splitlines()
        lines = [line.strip()[:200] for line in lines if line.strip()]
        if not lines:
            raise ApiError("추가할 상품 코드를 입력해 주세요.")
        if len(lines) > 2000:
            raise ApiError("한 번에 최대 2,000개까지 등록할 수 있습니다.")
        added = self.service.add_stock(product_id, session["guild_id"], lines)
        self.db.audit(session["guild_id"], session["discord_id"], "stock.add", f"product={product_id} count={added}")
        product = self.db.get_product(product_id, session["guild_id"])
        return web.json_response(
            {"ok": True, "added": added, "stock": int(product["stock_left"] if product else 0)}
        )

    async def api_stock_list(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        product_id = int(request.match_info["product_id"])
        self._product_for_guild(product_id, session["guild_id"])
        rows = self.db.list_stock(product_id)
        return web.json_response(
            {
                "ok": True,
                "stock": [
                    {"id": row["id"], "content": row["content"], "created_at": row["created_at"]} for row in rows
                ],
            }
        )

    async def api_charge(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        data = await self._json_body(request)
        user_id = str(data.get("user_id") or "").strip()
        if not user_id.isdigit():
            raise ApiError("유저 ID가 올바르지 않습니다.")
        amount = self._int(data, "amount", low=-10_000_000, high=10_000_000)
        if amount == 0:
            raise ApiError("0원은 처리할 수 없습니다.")
        balance = await self.service.charge(
            session["guild_id"], user_id, amount, actor=str(session["discord_id"])
        )
        return web.json_response({"ok": True, "balance": balance, "balance_text": utils.fmt_won(balance)})

    async def api_deposit_approve(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        deposit_id = int(request.match_info["deposit_id"])
        deposit = self.db.get_deposit(deposit_id)
        if deposit is None:
            raise ApiError("입금 내역을 찾을 수 없습니다.", 404)
        if deposit.get("guild_id") and str(deposit["guild_id"]) != session["guild_id"]:
            raise ApiError("다른 서버의 입금 내역입니다.", 403)
        data = await self._json_body(request)
        user_id = str(data.get("user_id") or deposit.get("discord_id") or "").strip()
        if not user_id.isdigit():
            raise ApiError("지급할 회원의 디스코드 ID를 입력해 주세요.")
        try:
            result = await self.service.processor.approve_manually(
                deposit_id, session["guild_id"], user_id, str(session["discord_id"])
            )
        except DatabaseError as exc:
            raise ApiError(str(exc)) from exc
        amount = int(result["deposit"].get("amount") or 0)
        return web.json_response(
            {
                "ok": True,
                "balance": result["deposit"].get("balance"),
                "balance_text": utils.fmt_won(result["deposit"].get("balance") or 0),
                "amount_text": utils.fmt_won(amount),
                "already": result.get("already", False),
            }
        )

    async def api_deposit_ignore(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        deposit_id = int(request.match_info["deposit_id"])
        deposit = self.db.get_deposit(deposit_id)
        if deposit is None:
            raise ApiError("입금 내역을 찾을 수 없습니다.", 404)
        if deposit.get("guild_id") and str(deposit["guild_id"]) != session["guild_id"]:
            raise ApiError("다른 서버의 입금 내역입니다.", 403)
        self.db.mark_deposit(deposit_id, "ignored", f"무시 by {session['discord_id']}")
        self.db.audit(session["guild_id"], session["discord_id"], "deposit.ignore", f"deposit={deposit_id}")
        return web.json_response({"ok": True})

    async def api_redeliver(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        purchase_id = int(request.match_info["purchase_id"])
        purchase = self.db.get_purchase(purchase_id)
        if purchase is None or str(purchase["guild_id"]) != session["guild_id"]:
            raise ApiError("구매 내역을 찾을 수 없습니다.", 404)
        delivery = await self.service.redeliver(purchase_id, str(session["discord_id"]))
        return web.json_response({"ok": True, "delivery": delivery})

    async def api_settings(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        data = await self._json_body(request)
        fields: dict[str, Any] = {}
        for key, limit in (("notice", 500), ("event_text", 500)):
            if key in data:
                fields[key] = str(data.get(key) or "").strip()[:limit]
        for key in ("log_channel_id", "admin_role_id"):
            if key in data:
                value = str(data.get(key) or "").strip()
                if value and not value.isdigit():
                    raise ApiError(f"{key}는 숫자(채널/역할 ID)여야 합니다.")
                fields[key] = value
        for key in ("auto_charge", "buy_enabled"):
            if key in data:
                fields[key] = 1 if data.get(key) else 0
        self.db.update_guild(session["guild_id"], **fields)
        self.db.audit(session["guild_id"], session["discord_id"], "settings.update", str(fields))
        return web.json_response({"ok": True})

    async def api_users(self, request: web.Request) -> web.Response:
        session = self._require_admin(request)
        search = str(request.query.get("search") or "").strip()[:25]
        if search and not search.isdigit():
            search = ""
        users = self.db.list_users(session["guild_id"], limit=100, search=search)
        return web.json_response(
            {
                "ok": True,
                "users": [
                    {
                        "id": user["discord_id"],
                        "balance": user["balance"],
                        "balance_text": utils.fmt_won(user["balance"]),
                        "total_spent": user["total_spent"],
                        "total_spent_text": utils.fmt_won(user["total_spent"]),
                    }
                    for user in users
                ],
            }
        )

    # ------------------------------------------------------------------ owner
    async def api_owner_summary(self, request: web.Request) -> web.Response:
        self._require_owner(request)
        summary = self.service.license_summary()
        guilds = self.db.list_guilds()
        return web.json_response(
            {
                "ok": True,
                "summary": summary,
                "plans": public_catalog(),
                "guilds": [
                    {
                        "id": guild["id"],
                        "name": guild.get("name") or guild["id"],
                        "license_valid": self.db.license_valid(guild),
                        "license_text": utils.fmt_dt(guild.get("license_expires_at"), "%Y.%m.%d"),
                    }
                    for guild in guilds
                ],
            }
        )

    async def api_owner_licenses(self, request: web.Request) -> web.Response:
        self._require_owner(request)
        search = str(request.query.get("search") or "").strip()[:40]
        rows = self.db.list_licenses(limit=200, search=search)
        return web.json_response(
            {
                "ok": True,
                "licenses": [
                    {
                        "key": row["key"],
                        "plan": row["plan"],
                        "plan_name": resolve_plan(row.get("plan")).name,
                        "duration_days": row["duration_days"],
                        "created_at": utils.fmt_dt(row["created_at"], "%Y.%m.%d %H:%M"),
                        "redeemed_by": row.get("redeemed_by") or "",
                        "expires_at": utils.fmt_dt(row.get("expires_at"), "%Y.%m.%d %H:%M"),
                        "revoked": bool(row["revoked"]),
                        "status": "폐기" if row["revoked"] else ("사용" if row.get("redeemed_by") else "미사용"),
                        "note": row.get("note") or "",
                    }
                    for row in rows
                ],
            }
        )

    async def api_owner_issue(self, request: web.Request) -> web.Response:
        session = self._require_owner(request)
        data = await self._json_body(request)
        count = self._int(data, "count", 1, low=1, high=50)
        days = self._int(data, "days", 30, low=1, high=3650)
        spec = resolve_plan(str(data.get("plan") or "standard"))
        note = str(data.get("note") or "").strip()[:100]
        keys = self.service.issue_licenses(count, days, spec.key, str(session["discord_id"]), note)
        return web.json_response(
            {
                "ok": True,
                "keys": keys,
                "plan": spec.key,
                "plan_name": spec.name,
                "plan_features": spec.features(),
            }
        )

    async def api_owner_revoke(self, request: web.Request) -> web.Response:
        session = self._require_owner(request)
        data = await self._json_body(request)
        key = str(data.get("key") or "").strip().upper()
        if not key.startswith("VDX-"):
            raise ApiError("라이센스 키 형식이 올바르지 않습니다.")
        if not self.db.revoke_license(key):
            raise ApiError("해당 키를 찾을 수 없습니다.", 404)
        self.db.audit(None, session["discord_id"], "license.revoke", key)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------- life
    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.settings.web_host, self.settings.web_port)
        await site.start()
        log.info("web panel listening on http://%s:%s", self.settings.web_host, self.settings.web_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
