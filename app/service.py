"""봇과 웹 패널이 공유하는 상점 서비스 계층."""

from __future__ import annotations

import logging
from typing import Any

import discord

from . import utils
from .config import Settings
from .db import Database, DatabaseError, InsufficientBalance, LicenseError
from .deposits import DepositProcessor
from .plans import Plan, resolve_plan
from .settings_store import SettingsError, SettingsStore

log = logging.getLogger("vendrix.service")

MIN_DEPOSIT = 100
PURPLE = 0x7C5CFF


class ShopError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""


def product_json(product: dict) -> dict:
    delivery_type = product.get("delivery_type") or "manual"
    if delivery_type == "content":
        stock = int(product.get("stock_left") or 0)
    else:
        stock = int(product.get("stock") if product.get("stock") is not None else -1)
    return {
        "id": int(product["id"]),
        "name": product["name"],
        "description": product.get("description") or "",
        "price": int(product["price"]),
        "price_text": utils.fmt_won(int(product["price"])),
        "delivery_type": delivery_type,
        "stock": stock,
        "unlimited": stock < 0,
        "sold_out": stock == 0,
        "active": bool(product.get("active", 1)),
        "role_id": product.get("role_id"),
        "sort_order": int(product.get("sort_order") or 0),
    }


class ShopService:
    def __init__(self, db: Database, settings: Settings, bot: discord.Client | None = None):
        self.db = db
        self.settings = settings
        self.bot = bot
        self.notifier: Any | None = None
        self.store = SettingsStore(db, settings)
        self.processor = DepositProcessor(db, self.store)
        self.pushbullet: Any | None = None

    def attach_notifier(self, notifier: Any) -> None:
        self.notifier = notifier
        self.processor.notifier = notifier

    async def restart_pushbullet(self) -> bool:
        listener = self.pushbullet
        if listener is None:
            return False
        return await listener.reconfigure(self.store.pushbullet_token())

    # ------------------------------------------------------------------ gates
    def require_shop(self, guild_id: str | int, *, for_buy: bool = False) -> dict:
        guild = self.db.get_guild(guild_id)
        if guild is None or not int(guild.get("registered_at") or 0):
            raise ShopError("이 서버는 아직 등록되지 않았습니다. `/등록` 으로 상점을 등록해 주세요.")
        if self.store.license_enforce() and not self.db.license_valid(guild):
            raise ShopError(
                f"라이센스가 만료되었습니다. (만료일 {utils.fmt_dt(guild.get('license_expires_at'))}) "
                "오너에게 새 라이센스 키를 요청하세요."
            )
        if for_buy and not int(guild.get("buy_enabled") or 0):
            raise ShopError("현재 상점 구매가 비활성화되어 있습니다. 관리자에게 문의하세요.")
        return guild

    # ------------------------------------------------------------------ plans
    def plan_for(self, guild_id: str | int) -> Plan:
        guild = self.db.get_guild(guild_id)
        return resolve_plan(self.db.plan_key_for_guild(guild))

    def plan_payload(self, guild_id: str | int) -> dict:
        return self.plan_for(guild_id).payload()

    def require_auto_charge(self, guild_id: str | int) -> Plan:
        plan = self.plan_for(guild_id)
        if not plan.auto_charge:
            raise ShopError(
                f"자동충전은 **스탠다드** 플랜부터 사용할 수 있습니다. (현재 플랜: {plan.name})"
            )
        return plan

    def create_product(
        self,
        guild_id: str | int,
        name: str,
        price: int,
        description: str = "",
        delivery_type: str = "manual",
        role_id: str | None = None,
        stock: int = -1,
        sort_order: int = 0,
    ) -> dict:
        self.require_shop(guild_id)
        plan = self.plan_for(guild_id)
        if delivery_type not in {"role", "content", "manual"}:
            raise ShopError("전달 방식은 role / content / manual 중 하나입니다.")
        if not plan.allows_delivery(delivery_type):
            labels = {"manual": "수동 전달", "content": "상품 코드", "role": "역할 지급"}
            allowed = " · ".join(labels[t] for t in plan.delivery_types)
            raise ShopError(
                f"{plan.name} 플랜에서는 {labels[delivery_type]}을 사용할 수 없습니다. (가능: {allowed})"
            )
        if not plan.unlimited_products:
            used = len(self.db.list_products(guild_id, active_only=False))
            if used >= plan.max_products:
                raise ShopError(
                    f"상품 등록 한도에 도달했습니다. ({used}/{plan.max_products}) "
                    "더 높은 플랜으로 업그레이드하세요."
                )
        return self.db.create_product(
            guild_id,
            name,
            price,
            description,
            delivery_type=delivery_type,
            role_id=role_id,
            stock=stock,
            sort_order=sort_order,
        )

    def update_product(self, product_id: int, guild_id: str | int, **fields: Any) -> dict:
        plan = self.plan_for(guild_id)
        delivery_type = fields.get("delivery_type")
        if delivery_type and not plan.allows_delivery(str(delivery_type)):
            raise ShopError(f"{plan.name} 플랜에서는 해당 전달 방식을 사용할 수 없습니다.")
        updated = self.db.update_product(product_id, guild_id, **fields)
        if updated is None:
            raise ShopError("상품을 찾을 수 없습니다.")
        return updated

    def add_stock(self, product_id: int, guild_id: str | int, lines: list[str]) -> int:
        product = self.db.get_product(product_id, guild_id)
        if product is None:
            raise ShopError("상품을 찾을 수 없습니다.")
        plan = self.plan_for(guild_id)
        cleaned = [line.strip()[:200] for line in lines if str(line).strip()]
        if not plan.unlimited_stock:
            current = len(self.db.list_stock(product_id))
            if current + len(cleaned) > plan.max_stock_per_product:
                raise ShopError(
                    f"상품 코드는 최대 {plan.max_stock_per_product:,}개까지 등록할 수 있습니다. "
                    f"(현재 {current:,}개 / 추가 {len(cleaned):,}개)"
                )
        return self.db.add_stock_lines(product_id, cleaned)

    def is_admin_member(self, guild_id: str | int, discord_id: str | int) -> bool:
        guild = self.bot.get_guild(int(guild_id)) if self.bot else None
        if guild is None:
            return False
        member = guild.get_member(int(discord_id))
        if member is None:
            return False
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            return True
        row = self.db.get_guild(guild_id)
        role_id = (row or {}).get("admin_role_id")
        if role_id and any(str(role.id) == str(role_id) for role in member.roles):
            return True
        return False

    # --------------------------------------------------------------- deposits
    def create_deposit_request(self, guild_id: str | int, discord_id: str | int, amount: int, depositor: str) -> dict:
        guild = self.require_shop(guild_id)
        amount = int(amount)
        min_amount = self.store.min_deposit(guild_id)
        max_amount = self.store.max_deposit(guild_id)
        if amount < min_amount:
            raise ShopError(f"최소 입금 금액은 {utils.fmt_won(min_amount)}입니다.")
        if amount > max_amount:
            raise ShopError(f"1회 입금 요청 한도는 {utils.fmt_won(max_amount)}입니다.")
        depositor = (depositor or "").strip()[:20]
        if len(depositor) < 2:
            raise ShopError("은행에 표시될 입금자명을 2자 이상 입력해 주세요.")
        if not self.store.auto_charge_master():
            raise ShopError("자동충전이 전역에서 비활성화되어 있습니다. 오너에게 문의하세요.")
        if not guild.get("auto_charge"):
            raise ShopError("이 서버는 자동충전이 꺼져 있습니다. 관리자에게 문의하세요.")
        self.require_auto_charge(guild_id)

        pending = self.db.create_pending_deposit(
            guild_id, discord_id, amount, depositor, ttl_hours=self.store.deposit_ttl(guild_id)
        )
        self.db.audit(guild_id, str(discord_id), "deposit.request", f"amount={amount} depositor={depositor} code={pending['code']}")
        return pending

    def deposit_guide(self, pending: dict) -> str:
        bank = self.store.bank()
        return (
            f"**{utils.fmt_won(pending['amount'])}** 입금 요청이 생성되었습니다.\n\n"
            f"**은행** · {bank['name']}\n"
            f"**계좌** · `{bank['account']}`\n"
            f"**예금주** · {bank['holder']}\n"
            f"**입금자명** · `{pending['depositor']}`\n"
            f"**메모** · `{pending['code']}` (선택)\n\n"
            f"위 계좌로 **{utils.fmt_won(pending['amount'])}** 을 입금하면 알림 확인 후 자동으로 충전됩니다.\n"
            f"요청 만료: {utils.fmt_dt(pending['expires_at'])}"
        )

    # --------------------------------------------------------------- purchase
    async def purchase(
        self,
        guild_id: str | int,
        discord_id: str | int,
        product_id: int,
        quantity: int = 1,
        channel: str = "discord",
    ) -> dict:
        guild = self.require_shop(guild_id, for_buy=True)
        limit = self.store.purchase_max_qty(guild_id)
        quantity = max(1, int(quantity))
        if quantity > limit:
            raise ShopError(f"1회 구매 수량은 최대 {limit}개입니다.")
        plan = self.plan_for(guild_id)
        product = self.db.get_product(product_id, guild_id)
        if product is not None and not plan.allows_delivery(product["delivery_type"]):
            raise ShopError(f"현재 플랜({plan.name})에서는 이 상품을 구매할 수 없습니다. 관리자에게 문의하세요.")
        try:
            result = self.db.purchase(guild_id, discord_id, product_id, quantity, channel=channel)
        except DatabaseError as exc:
            raise ShopError(str(exc)) from exc

        delivery = await self._deliver(guild, result, discord_id)
        self.db.mark_purchase_delivered(result["purchase_id"], delivery)
        result["delivery"] = delivery
        result["channel"] = channel
        await self._log_purchase(guild, result, discord_id)
        return result

    async def send_product(
        self, guild_id: str | int, discord_id: str | int, product_id: int, quantity: int, actor: str
    ) -> dict:
        guild = self.require_shop(guild_id)
        plan = self.plan_for(guild_id)
        product = self.db.get_product(product_id, guild_id)
        if product is not None and not plan.allows_delivery(product["delivery_type"]):
            raise ShopError(f"현재 플랜({plan.name})에서는 이 전달 방식을 사용할 수 없습니다.")
        try:
            result = self.db.grant_purchase(guild_id, discord_id, product_id, quantity, actor=actor)
        except DatabaseError as exc:
            raise ShopError(str(exc)) from exc
        delivery = await self._deliver(guild, result, discord_id)
        self.db.mark_purchase_delivered(result["purchase_id"], delivery)
        result["delivery"] = delivery
        return result

    async def redeliver(self, purchase_id: int, actor: str) -> str:
        purchase = self.db.get_purchase(purchase_id)
        if purchase is None:
            raise ShopError("구매 내역을 찾을 수 없습니다.")
        guild = self.db.get_guild(purchase["guild_id"])
        if guild is None:
            raise ShopError("서버 정보를 찾을 수 없습니다.")
        contents = self.db.stock_for_purchase(purchase_id)
        result = {
            "purchase_id": purchase_id,
            "product": {
                "name": purchase["product_name"],
                "id": purchase["product_id"],
                "price": purchase["unit_price"],
                "delivery_type": purchase["delivery_type"],
                "role_id": None,
            },
            "quantity": purchase["quantity"],
            "contents": contents,
            "delivery_type": purchase["delivery_type"],
            "role_id": None,
        }
        product = self.db.get_product(int(purchase["product_id"])) if purchase["product_id"] else None
        if product:
            result["role_id"] = product.get("role_id")
        delivery = await self._deliver(guild, result, purchase["discord_id"])
        self.db.mark_purchase_delivered(purchase_id, delivery)
        self.db.audit(purchase["guild_id"], actor, "purchase.redeliver", f"purchase={purchase_id} result={delivery}")
        return delivery

    # ---------------------------------------------------------------- charges
    async def charge(self, guild_id: str | int, target_id: str | int, amount: int, actor: str) -> int:
        amount = int(amount)
        if amount == 0:
            raise ShopError("0원은 처리할 수 없습니다.")
        guild = self.db.get_guild(guild_id)
        if guild is None or not int(guild.get("registered_at") or 0):
            raise ShopError("등록된 서버가 아닙니다.")
        try:
            balance = self.db.add_balance(guild_id, target_id, amount)
        except InsufficientBalance as exc:
            raise ShopError("차감할 잔액이 부족합니다.") from exc
        action = "충전" if amount > 0 else "차감"
        self.db.audit(guild_id, actor, "balance.adjust", f"user={target_id} amount={amount} balance={balance}")
        await self._dm(
            guild_id,
            target_id,
            f"**{action} 안내**\n{utils.fmt_won(abs(amount))}이 {action}되었습니다.\n현재 잔액: **{utils.fmt_won(balance)}**",
        )
        return balance

    # ------------------------------------------------------------------ admin
    async def notify_log(self, guild_id: str | int, embed: discord.Embed) -> None:
        if self.bot is None:
            return
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return
        row = self.db.get_guild(guild_id)
        channel_id = (row or {}).get("log_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.warning("failed to send log to guild %s", guild_id)

    async def _log_purchase(self, guild: dict, result: dict, discord_id: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.purchase_completed(guild, result, discord_id)
        except Exception:
            log.exception("purchase notification failed")

    # --------------------------------------------------------------- delivery
    async def _member(self, guild_id: str | int, discord_id: str | int) -> discord.Member | None:
        if self.bot is None:
            return None
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return None
        member = guild.get_member(int(discord_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(discord_id))
        except discord.HTTPException:
            return None

    async def _dm(self, guild_id: str | int, discord_id: str | int, message: str) -> bool:
        member = await self._member(guild_id, discord_id)
        if member is None:
            return False
        try:
            await member.send(message[:1900])
            return True
        except discord.HTTPException:
            return False

    async def _deliver(self, guild: dict, result: dict, discord_id: str) -> str:
        product = result["product"]
        delivery_type = result["delivery_type"]
        quantity = result["quantity"]
        member = await self._member(guild["id"], discord_id)

        if delivery_type == "role" and result.get("role_id"):
            discord_guild = self.bot.get_guild(int(guild["id"])) if self.bot else None
            role = discord_guild.get_role(int(result["role_id"])) if discord_guild else None
            if member is None or role is None:
                return "역할 지급 대기 (멤버/역할 확인 필요)"
            try:
                await member.add_roles(role, reason=f"VENDRIX 구매 #{result['purchase_id']}")
            except discord.HTTPException as exc:
                log.warning("role grant failed: %s", exc)
                return "역할 지급 실패 (권한/역할 순서 확인 필요)"
            return f"역할 지급 완료: {role.name}"

        if delivery_type == "content":
            contents = result.get("contents") or []
            if not contents:
                return "상품 코드 없음 (관리자 확인 필요)"
            body = "\n".join(contents)
            message = (
                f"**{self.store.brand()}** · 구매하신 상품이 도착했습니다.\n\n"
                f"**상품** · {product['name']} × {quantity}\n"
                f"**주문번호** · #{result['purchase_id']}\n\n"
                f"```\n{body[:1500]}\n```\n"
                "상품 코드는 타인과 공유하지 마세요."
            )
            sent = await self._dm(guild["id"], discord_id, message)
            if sent:
                return f"상품 코드 {len(contents)}건 DM 전송"
            embed = discord.Embed(
                title="수동 전달 필요 · DM 실패",
                description=f"<@{discord_id}> 님에게 상품 코드를 전달해 주세요.\n```\n{body[:1200]}\n```",
                color=0xE74C3C,
            )
            embed.set_footer(text=f"구매 #{result['purchase_id']} · {product['name']}")
            await self.notify_log(guild["id"], embed)
            return "DM 실패 · 관리자 수동 전달 대기"

        if delivery_type == "manual":
            embed = discord.Embed(
                title="수동 상품 주문",
                description=(
                    f"**구매자** · <@{discord_id}>\n"
                    f"**상품** · {product['name']} × {quantity}\n"
                    f"**금액** · {utils.fmt_won(result.get('total', 0))}\n"
                    f"**주문번호** · #{result['purchase_id']}"
                ),
                color=PURPLE,
            )
            await self.notify_log(guild["id"], embed)
            await self._dm(
                guild["id"],
                discord_id,
                f"**주문 접수 완료**\n상품: {product['name']} × {quantity}\n주문번호: #{result['purchase_id']}\n관리자 확인 후 전달됩니다.",
            )
            return "수동 전달 대기"

        return "전달 완료"

    # ------------------------------------------------------------- web / auth
    def issue_login_token(self, guild_id: str | int, discord_id: str | int, is_admin: bool) -> str:
        raw = utils.gen_token(24)
        self.db.create_login_token(
            utils.sha256(raw), guild_id, discord_id, is_admin, self.settings.login_token_minutes * 60
        )
        return f"{self.settings.base_url}/entry/{raw}"

    def shop_payload(self, guild_id: str | int, discord_id: str | int) -> dict:
        guild = self.db.get_guild(guild_id)
        if guild is None:
            raise ShopError("서버 정보를 찾을 수 없습니다.")
        user = self.db.ensure_user(guild_id, discord_id)
        plan = self.plan_for(guild_id)
        products = [product_json(p) for p in self.db.list_products(guild_id, active_only=True)]
        purchases = self.db.list_purchases(guild_id, discord_id, limit=30)
        pending = self.db.list_pending(guild_id, include_all=False)
        bank = self.store.bank()
        return {
            "plan": plan.payload(),
            "auto_charge_available": plan.auto_charge,
            "settings": {
                "min_deposit": self.store.min_deposit(guild_id),
                "min_deposit_text": utils.fmt_won(self.store.min_deposit(guild_id)),
                "max_deposit": self.store.max_deposit(guild_id),
                "max_deposit_text": utils.fmt_won(self.store.max_deposit(guild_id)),
                "deposit_ttl_hours": self.store.deposit_ttl(guild_id),
                "purchase_max_qty": self.store.purchase_max_qty(guild_id),
                "auto_charge_master": self.store.auto_charge_master(),
            },
            "guild": {
                "id": guild["id"],
                "name": guild["name"],
                "notice": guild.get("notice") or "",
                "event": guild.get("event_text") or "",
                "buy_enabled": bool(guild.get("buy_enabled")),
                "auto_charge": bool(guild.get("auto_charge")),
                "registered": bool(guild.get("registered_at")),
                "license_expires_at": guild.get("license_expires_at"),
                "license_text": utils.fmt_dt(guild.get("license_expires_at"), "%Y.%m.%d"),
                "license_valid": self.db.license_valid(guild),
                "plan_key": plan.key,
                "plan_name": plan.name,
            },
            "user": {
                "id": user["discord_id"],
                "balance": int(user["balance"]),
                "balance_text": utils.fmt_won(user["balance"]),
                "total_spent": int(user["total_spent"]),
                "total_spent_text": utils.fmt_won(user["total_spent"]),
            },
            "products": products,
            "purchases": [
                {
                    "id": p["id"],
                    "name": p["product_name"],
                    "quantity": p["quantity"],
                    "total": p["total"],
                    "total_text": utils.fmt_won(p["total"]),
                    "status": p["status"],
                    "delivery": p["delivery"],
                    "created_at": p["created_at"],
                    "created_text": utils.fmt_dt(p["created_at"]),
                }
                for p in purchases
            ],
            "pending": [
                {
                    "id": p["id"],
                    "amount": p["amount"],
                    "amount_text": utils.fmt_won(p["amount"]),
                    "depositor": p["depositor"],
                    "code": p["code"],
                    "expires_at": p["expires_at"],
                    "expires_text": utils.fmt_dt(p["expires_at"]),
                }
                for p in pending
            ],
            "bank": bank,
            "brand": self.store.brand(),
            "support_url": self.store.support_url(),
            "min_deposit": self.store.min_deposit(guild_id),
        }

    def admin_payload(self, guild_id: str | int) -> dict:
        guild = self.db.get_guild(guild_id)
        if guild is None:
            raise ShopError("서버 정보를 찾을 수 없습니다.")
        plan = self.plan_for(guild_id)
        products = [product_json(p) for p in self.db.list_products(guild_id, active_only=False)]
        deposits = self.db.list_deposits(guild_id, limit=100)
        return {
            "plan": plan.payload(),
            "limits": {
                "products_used": len(products),
                "products_max": plan.max_products,
                "products_text": plan.limit_text(plan.max_products),
                "stock_max": plan.max_stock_per_product,
                "stock_text": plan.limit_text(plan.max_stock_per_product),
            },
            "guild": {
                "id": guild["id"],
                "name": guild["name"],
                "notice": guild.get("notice") or "",
                "event": guild.get("event_text") or "",
                "log_channel_id": guild.get("log_channel_id") or "",
                "admin_role_id": guild.get("admin_role_id") or "",
                "auto_charge": bool(guild.get("auto_charge")),
                "buy_enabled": bool(guild.get("buy_enabled")),
                "license_key": guild.get("license_key") or "",
                "license_expires_at": guild.get("license_expires_at"),
                "license_text": utils.fmt_dt(guild.get("license_expires_at"), "%Y.%m.%d %H:%M"),
                "license_valid": self.db.license_valid(guild),
                "plan_key": plan.key,
                "plan_name": plan.name,
            },
            "stats": self.db.guild_stats(guild_id),
            "products": products,
            "deposits": [
                {
                    "id": d["id"],
                    "amount": d["amount"],
                    "amount_text": utils.fmt_won(d.get("amount")),
                    "depositor": d.get("depositor") or "",
                    "status": d["status"],
                    "application": d.get("application") or "",
                    "title": d.get("title") or "",
                    "body": d.get("body") or "",
                    "user_id": d.get("discord_id"),
                    "created_at": d["created_at"],
                    "created_text": utils.fmt_dt(d["created_at"]),
                    "note": d.get("note") or "",
                }
                for d in deposits
            ],
            "pending": [
                {
                    "id": p["id"],
                    "amount": p["amount"],
                    "amount_text": utils.fmt_won(p["amount"]),
                    "depositor": p["depositor"],
                    "code": p["code"],
                    "user_id": p["discord_id"],
                    "expires_text": utils.fmt_dt(p["expires_at"]),
                }
                for p in self.db.list_pending(guild_id, include_all=True)[:50]
            ],
            "users": [
                {
                    "id": u["discord_id"],
                    "balance": u["balance"],
                    "balance_text": utils.fmt_won(u["balance"]),
                    "total_spent": u["total_spent"],
                    "total_spent_text": utils.fmt_won(u["total_spent"]),
                }
                for u in self.db.list_users(guild_id, limit=100)
            ],
            "audit": self.db.list_audit(guild_id, limit=40),
            "bank": self.store.bank(),
            "settings": self.store.summary(guild_id),
            "guild_settings": self.store.guild_view(guild_id),
        }

    # --------------------------------------------------------------- licenses
    def issue_licenses(
        self, count: int, days: int, plan: str, actor: str, note: str = ""
    ) -> list[str]:
        count = utils.clamp(int(count), 1, 50)
        days = utils.clamp(int(days), 1, 3650)
        spec = resolve_plan(plan)
        plan = spec.key
        keys: list[str] = []
        for _ in range(count):
            for _attempt in range(8):
                key = utils.gen_license_key()
                if self.db.get_license(key):
                    continue
                self.db.create_license(key, plan, days, spec.max_guilds, actor, note)
                keys.append(key)
                break
        if keys:
            self.db.audit(None, actor, "license.issue", f"count={len(keys)} days={days} plan={plan}")
        return keys

    def redeem_license(self, guild_id: str | int, user_id: str | int, key: str, guild_name: str) -> dict:
        try:
            result = self.db.redeem_license(key, guild_id, user_id)
        except LicenseError as exc:
            raise ShopError(str(exc)) from exc
        if guild_name:
            self.db.ensure_guild(guild_id, guild_name)
        spec = resolve_plan(result.get("plan"))
        result["plan_key"] = spec.key
        result["plan_name"] = spec.name
        result["plan_features"] = spec.features()
        return result

    def license_summary(self) -> dict:
        return self.db.license_summary()
