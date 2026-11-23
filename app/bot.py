"""VENDRIX 디스코드 봇."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from . import utils
from .config import Settings
from .db import Database
from .deposits import MatchOutcome
from .plans import plan_choices, resolve_plan
from .service import PURPLE, ShopError, ShopService

log = logging.getLogger("vendrix.bot")

BRAND_COLOR = discord.Color(0x7C5CFF)
GREEN = discord.Color(0x2ECC71)
ORANGE = discord.Color(0xF39C12)
RED = discord.Color(0xE74C3C)


# --------------------------------------------------------------------- embeds
def logo_url(settings: Settings) -> str:
    return f"{settings.base_url}/static/logo.png"


def banner_url(settings: Settings) -> str:
    return settings.banner_url or f"{settings.base_url}/static/banner.png"


def panel_embed(bot: "WiaBot", guild_row: dict) -> discord.Embed:
    settings = bot.settings
    notice = (guild_row.get("notice") or "").strip() or "공지사항이 등록되지 않았습니다"
    event = (guild_row.get("event_text") or "").strip() or "진행 중인 이벤트가 없습니다"
    products = bot.db.list_products(guild_row["id"], active_only=True)
    embed = discord.Embed(
        title="VENDRIX Home",
        description=(
            "**공지사항**\n"
            f"▎ {notice}\n\n"
            "**이벤트**\n"
            f"▎ {event}"
        ),
        color=BRAND_COLOR,
    )
    embed.set_author(name=f"{settings.brand_name}  |  {guild_row.get('name') or ''}", icon_url=logo_url(settings))
    embed.set_image(url=banner_url(settings))
    plan = bot.service.plan_for(guild_row["id"])
    embed.set_footer(
        text=f"{settings.brand_name}™ · {plan.name} | {utils.fmt_dt(utils.now())} | 상품 {len(products)}개"
    )
    return embed


def money_line(label: str, value: str) -> str:
    return f"**{label}** · {value}"


def base_embed(title: str, description: str = "", color: discord.Color = BRAND_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description or None, color=color)
    embed.timestamp = discord.utils.utcnow()
    return embed


# --------------------------------------------------------------------- modals
class QuantityModal(discord.ui.Modal, title="구매 수량 선택"):
    quantity = discord.ui.TextInput(label="수량", placeholder="1 ~ 10", default="1", max_length=2)

    def __init__(self, bot: "WiaBot", product: dict):
        super().__init__()
        self.bot = bot
        self.product = product

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            quantity = int(str(self.quantity.value).strip())
        except ValueError:
            await interaction.response.send_message("수량은 숫자로 입력해 주세요.", ephemeral=True)
            return
        if not 1 <= quantity <= 10:
            await interaction.response.send_message("수량은 1~10 사이로 입력해 주세요.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.bot.service.purchase(
                interaction.guild_id, interaction.user.id, self.product["id"], quantity, channel="discord"
            )
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        embed = base_embed(
            "구매 완료",
            f"{money_line('상품', result['product']['name'])}\n"
            f"{money_line('수량', str(result['quantity']))}\n"
            f"{money_line('결제 금액', utils.fmt_won(result['total']))}\n"
            f"{money_line('남은 잔액', utils.fmt_won(result['balance']))}\n"
            f"{money_line('전달', result['delivery'])}",
            GREEN,
        )
        embed.set_footer(text=f"주문번호 #{result['purchase_id']}")
        await interaction.followup.send(embed=embed, ephemeral=True)


class BuySelectView(discord.ui.View):
    def __init__(self, bot: "WiaBot", products: list[dict]):
        super().__init__(timeout=120)
        self.bot = bot
        self.products = {str(p["id"]): p for p in products}
        options = []
        for product in products[:25]:
            label = f"{product['name']} · {utils.fmt_won(product['price'])}"
            description = product.get("description") or ""
            if product["sold_out"]:
                label = f"[품절] {label}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(product["id"]),
                    description=(description[:100] or None),
                    emoji="📦",
                )
            )
        self.select = discord.ui.Select(placeholder="구매할 상품을 선택하세요", options=options, min_values=1, max_values=1)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        product = self.products.get(self.select.values[0])
        if product is None:
            await interaction.response.send_message("상품을 찾을 수 없습니다.", ephemeral=True)
            return
        if product["sold_out"]:
            await interaction.response.send_message("품절된 상품입니다.", ephemeral=True)
            return
        await interaction.response.send_modal(QuantityModal(self.bot, product))


class DepositModal(discord.ui.Modal, title="입금 요청"):
    amount = discord.ui.TextInput(label="입금 금액 (원)", placeholder="예: 10000", max_length=12)
    depositor = discord.ui.TextInput(
        label="입금자명", placeholder="은행에 표시될 이름 (예: 홍길동)", max_length=20
    )

    def __init__(self, bot: "WiaBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        amount = utils.parse_amount(str(self.amount.value))
        if amount is None:
            await interaction.response.send_message("금액을 숫자로 입력해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            pending = self.bot.service.create_deposit_request(
                interaction.guild_id, interaction.user.id, amount, str(self.depositor.value)
            )
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        embed = base_embed("입금 안내", self.bot.service.deposit_guide(pending))
        embed.set_footer(text="입금자명이 다르면 자동 충전이 되지 않을 수 있습니다.")
        await interaction.followup.send(embed=embed, ephemeral=True)


class ChargeModal(discord.ui.Modal, title="충전 / 차감"):
    target = discord.ui.TextInput(label="대상 유저 ID", placeholder="디스코드 유저 ID (숫자)", max_length=25)
    amount = discord.ui.TextInput(label="금액 (원)", placeholder="10000 / 차감은 -10000", max_length=12)

    def __init__(self, bot: "WiaBot", guild_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        target = str(self.target.value).strip()
        if not target.isdigit():
            await interaction.response.send_message("유저 ID는 숫자로 입력해 주세요.", ephemeral=True)
            return
        amount = utils.parse_amount(str(self.amount.value))
        if amount is None:
            await interaction.response.send_message("금액을 숫자로 입력해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            balance = await self.bot.service.charge(self.guild_id, target, amount, actor=str(interaction.user.id))
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ <@{target}> 잔액이 **{utils.fmt_won(balance)}** 이 되었습니다.\n"
            f"처리 금액: {utils.fmt_won(amount)}",
            ephemeral=True,
        )


class ProductModal(discord.ui.Modal, title="상품 추가"):
    name = discord.ui.TextInput(label="상품명", max_length=80)
    price = discord.ui.TextInput(label="가격 (원)", placeholder="5000", max_length=12)
    description = discord.ui.TextInput(
        label="설명", required=False, max_length=200, style=discord.TextStyle.paragraph
    )
    delivery = discord.ui.TextInput(
        label="전달 방식", placeholder="role / content / manual", default="manual", max_length=10
    )
    role_id = discord.ui.TextInput(label="역할 ID (role 전용)", required=False, max_length=25)

    def __init__(self, bot: "WiaBot", guild_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        price = utils.parse_amount(str(self.price.value))
        if price is None or price < 0:
            await interaction.response.send_message("가격을 올바르게 입력해 주세요.", ephemeral=True)
            return
        delivery = str(self.delivery.value).strip().lower()
        if delivery not in {"role", "content", "manual"}:
            await interaction.response.send_message("전달 방식은 role / content / manual 중 하나입니다.", ephemeral=True)
            return
        role_id = str(self.role_id.value).strip() or None
        if delivery == "role":
            if not role_id or not role_id.isdigit():
                await interaction.response.send_message("role 방식은 역할 ID가 필요합니다.", ephemeral=True)
                return
            role = interaction.guild.get_role(int(role_id))
            if role is None:
                await interaction.response.send_message("서버에서 해당 역할을 찾을 수 없습니다.", ephemeral=True)
                return
        try:
            product = self.bot.service.create_product(
                self.guild_id,
                name=str(self.name.value).strip()[:80],
                price=price,
                description=str(self.description.value or "").strip()[:200],
                delivery_type=delivery,
                role_id=role_id,
            )
        except ShopError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        note = "\n재고(상품 코드)는 웹 패널에서 등록하세요." if delivery == "content" else ""
        await interaction.response.send_message(
            f"✅ 상품 **{product['name']}** 이 추가되었습니다. (ID {product['id']})\n"
            f"가격 {utils.fmt_won(product['price'])} · 전달 {delivery}{note}",
            ephemeral=True,
        )


class NoticeModal(discord.ui.Modal, title="공지 / 이벤트 수정"):
    notice = discord.ui.TextInput(
        label="공지사항", required=False, max_length=500, style=discord.TextStyle.paragraph
    )
    event = discord.ui.TextInput(
        label="이벤트", required=False, max_length=500, style=discord.TextStyle.paragraph
    )

    def __init__(self, bot: "WiaBot", guild_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.bot.db.update_guild(
            self.guild_id,
            notice=str(self.notice.value or "").strip()[:500],
            event_text=str(self.event.value or "").strip()[:500],
        )
        await interaction.response.send_message("✅ 공지/이벤트가 수정되었습니다. `/패널` 로 패널을 다시 게시하세요.", ephemeral=True)


# ---------------------------------------------------------------------- views
class ProductListView(discord.ui.View):
    PER_PAGE = 8

    def __init__(self, bot: "WiaBot", guild_id: int, products: list[dict], page: int = 0):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.products = products
        self.page = max(0, page)
        self.max_page = max(0, (len(products) - 1) // self.PER_PAGE)
        if self.page > 0:
            self.add_item(self._nav("이전", "◀", -1, discord.ButtonStyle.secondary))
        if self.page < self.max_page:
            self.add_item(self._nav("다음", "▶", 1, discord.ButtonStyle.secondary))

    def _nav(self, label: str, emoji: str, delta: int, style: discord.ButtonStyle) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=0)

        async def callback(interaction: discord.Interaction) -> None:
            view = ProductListView(self.bot, self.guild_id, self.products, self.page + delta)
            await interaction.response.edit_message(embed=view.embed(), view=view)

        button.callback = callback
        return button

    def embed(self) -> discord.Embed:
        start = self.page * self.PER_PAGE
        chunk = self.products[start : start + self.PER_PAGE]
        lines = []
        for product in chunk:
            stock = "무제한" if product["unlimited"] else (f"{product['stock']}개" if product["stock"] > 0 else "품절")
            lines.append(
                f"**{product['name']}** · {utils.fmt_won(product['price'])}\n"
                f"▎ {product['description'] or '설명 없음'}  `재고 {stock}`"
            )
        embed = base_embed(
            "제품목록",
            "\n\n".join(lines) if lines else "등록된 상품이 없습니다.",
        )
        embed.set_footer(text=f"{self.page + 1} / {self.max_page + 1} 페이지 · 총 {len(self.products)}개")
        return embed


class AdminPanelView(discord.ui.View):
    def __init__(self, bot: "WiaBot"):
        super().__init__(timeout=None)
        self.bot = bot

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return False
        if not (
            interaction.user.id in self.bot.settings.owner_ids
            or self.bot.service.is_admin_member(interaction.guild_id, interaction.user.id)
        ):
            await interaction.response.send_message("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="웹 관리자", emoji="🌐", style=discord.ButtonStyle.primary, custom_id="vdx:admin:web")
    async def web(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        url = self.bot.service.issue_login_token(interaction.guild_id, interaction.user.id, True)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="웹 관리자 열기", url=url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(
            "1회용 관리자 링크입니다. 15분 안에 접속하세요.", view=view, ephemeral=True
        )

    @discord.ui.button(label="제품 추가", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="vdx:admin:product")
    async def product(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(ProductModal(self.bot, interaction.guild_id))

    @discord.ui.button(label="충전/차감", emoji="💰", style=discord.ButtonStyle.secondary, custom_id="vdx:admin:charge")
    async def charge(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(ChargeModal(self.bot, interaction.guild_id))

    @discord.ui.button(label="공지 수정", emoji="📢", style=discord.ButtonStyle.secondary, custom_id="vdx:admin:notice")
    async def notice(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        await interaction.response.send_modal(NoticeModal(self.bot, interaction.guild_id))

    @discord.ui.button(label="자동충전 토글", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="vdx:admin:auto")
    async def toggle_auto(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        guild = self.bot.db.get_guild(interaction.guild_id)
        new_value = 0 if (guild and guild.get("auto_charge")) else 1
        self.bot.db.update_guild(interaction.guild_id, auto_charge=new_value)
        await interaction.response.send_message(
            f"자동충전이 **{'켜짐' if new_value else '꺼짐'}** 으로 변경되었습니다.", ephemeral=True
        )

    @discord.ui.button(label="구매 토글", emoji="🛒", style=discord.ButtonStyle.secondary, custom_id="vdx:admin:buy")
    async def toggle_buy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._guard(interaction):
            return
        guild = self.bot.db.get_guild(interaction.guild_id)
        new_value = 0 if (guild and guild.get("buy_enabled")) else 1
        self.bot.db.update_guild(interaction.guild_id, buy_enabled=new_value)
        await interaction.response.send_message(
            f"구매 기능이 **{'켜짐' if new_value else '꺼짐'}** 으로 변경되었습니다.", ephemeral=True
        )


class ShopPanelView(discord.ui.View):
    def __init__(self, bot: "WiaBot"):
        super().__init__(timeout=None)
        self.bot = bot

    async def _products(self, interaction: discord.Interaction, *, for_buy: bool = False) -> list[dict] | None:
        try:
            self.bot.service.require_shop(interaction.guild_id, for_buy=for_buy)
        except ShopError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return None
        return self.bot.db.list_products(interaction.guild_id, active_only=True)

    @discord.ui.button(label="제품목록", emoji="🛒", style=discord.ButtonStyle.secondary, custom_id="vdx:panel:products")
    async def products(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        items = await self._products(interaction)
        if items is None:
            return
        if not items:
            await interaction.response.send_message("등록된 상품이 없습니다.", ephemeral=True)
            return
        view = ProductListView(self.bot, interaction.guild_id, items)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="구매하기", emoji="💳", style=discord.ButtonStyle.primary, custom_id="vdx:panel:buy")
    async def buy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        items = await self._products(interaction, for_buy=True)
        if items is None:
            return
        available = [p for p in items if not p["sold_out"]]
        if not available:
            await interaction.response.send_message("현재 구매 가능한 상품이 없습니다.", ephemeral=True)
            return
        embed = base_embed("상품 구매", "아래에서 상품을 선택하면 수량 입력 창이 열립니다.")
        await interaction.response.send_message(embed=embed, view=BuySelectView(self.bot, available), ephemeral=True)

    @discord.ui.button(label="유저정보", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="vdx:panel:me")
    async def me(self, interaction: discord.Interaction, _: discord.UI.Button) -> None:
        if interaction.guild is None:
            return
        user = self.bot.db.ensure_user(interaction.guild_id, interaction.user.id)
        pending = self.bot.db.list_pending(interaction.guild_id)
        purchases = self.bot.db.list_purchases(interaction.guild_id, interaction.user.id, limit=5)
        lines = [
            money_line("잔액", utils.fmt_won(user["balance"])),
            money_line("누적 사용", utils.fmt_won(user["total_spent"])),
            "",
            "**최근 구매**",
        ]
        if purchases:
            for purchase in purchases:
                lines.append(
                    f"· {purchase['product_name']} × {purchase['quantity']} · {utils.fmt_won(purchase['total'])} "
                    f"({utils.fmt_dt(purchase['created_at'], '%m/%d %H:%M')})"
                )
        else:
            lines.append("· 구매 내역이 없습니다.")
        if pending:
            lines.append("")
            lines.append("**진행 중인 입금 요청**")
            for item in pending[:3]:
                lines.append(
                    f"· {utils.fmt_won(item['amount'])} · `{item['code']}` · {utils.fmt_dt(item['expires_at'], '%m/%d %H:%M')} 까지"
                )
        embed = base_embed("유저정보", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="문의하기", emoji="💬", style=discord.ButtonStyle.secondary, custom_id="vdx:panel:support")
    async def support(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        url = self.bot.settings.support_url
        if url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="문의 열기", url=url, style=discord.ButtonStyle.link))
            await interaction.response.send_message("문의 채널로 이동하세요.", view=view, ephemeral=True)
        else:
            await interaction.response.send_message(
                "문의는 서버 관리자에게 DM으로 보내 주세요.", ephemeral=True
            )

    @discord.ui.button(
        label="충전하기", emoji="➕", style=discord.ButtonStyle.success, custom_id="vdx:panel:deposit", row=1
    )
    async def deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            self.bot.service.require_shop(interaction.guild_id, for_buy=True)
        except ShopError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_modal(DepositModal(self.bot))

    @discord.ui.button(
        label="관리", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="vdx:panel:admin", row=1
    )
    async def admin(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not (
            interaction.user.id in self.bot.settings.owner_ids
            or self.bot.service.is_admin_member(interaction.guild_id, interaction.user.id)
        ):
            await interaction.response.send_message("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        embed = admin_embed(self.bot, interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=AdminPanelView(self.bot), ephemeral=True)


def admin_embed(bot: "WiaBot", guild_id: int) -> discord.Embed:
    stats = bot.db.guild_stats(guild_id)
    guild = bot.db.get_guild(guild_id) or {}
    plan = bot.service.plan_for(guild_id)
    products = bot.db.list_products(guild_id, active_only=False)
    embed = base_embed(
        "VENDRIX · 관리자",
        f"**플랜** · {plan.name} ({plan.tagline})\n"
        f"**상품** · {len(products)}/{plan.limit_text(plan.max_products)}\n"
        f"**자동충전** · {'가능' if plan.auto_charge else '불가 (스탠다드부터)'}\n"
        f"**라이센스** · {utils.fmt_dt(guild.get('license_expires_at'), '%Y.%m.%d')} 까지\n\n"
        f"**오늘 매출** · {utils.fmt_won(stats['today_sales'])}\n"
        f"**오늘 주문** · {stats['today_orders']}건\n"
        f"**회원** · {stats['members']}명\n"
        f"**대기 입금** · {stats['pending_deposits']}건\n"
        f"**보유 잔액 합계** · {utils.fmt_won(stats['total_balance'])}",
    )
    embed.set_footer(text="자동충전 / 구매 토글은 아래 버튼으로 변경할 수 있습니다.")
    return embed


# ------------------------------------------------------------------- notifier
class DiscordNotifier:
    def __init__(self, bot: "WiaBot"):
        self.bot = bot
        self.settings = bot.settings

    async def _dm(self, guild_id: str, user_id: str, text: str) -> None:
        try:
            await self.bot.service._dm(guild_id, user_id, text)
        except Exception:
            log.debug("dm failed", exc_info=True)

    async def deposit_matched(self, outcome: MatchOutcome) -> None:
        if outcome.guild_id is None or outcome.user_id is None:
            return
        await self._dm(
            outcome.guild_id,
            outcome.user_id,
            f"**자동 충전 완료**\n"
            f"입금 금액: {utils.fmt_won(outcome.amount)}\n"
            f"입금자명: {outcome.depositor or '-'}\n"
            f"현재 잔액: {utils.fmt_won(outcome.balance)}\n\n"
            f"{self.settings.brand_name} 이용해 주셔서 감사합니다.",
        )
        embed = base_embed(
            "자동 충전 완료",
            f"{money_line('입금자', outcome.depositor or '-')}\n"
            f"{money_line('금액', utils.fmt_won(outcome.amount))}\n"
            f"{money_line('대상', f'<@{outcome.user_id}>')}\n"
            f"{money_line('충전 후 잔액', utils.fmt_won(outcome.balance))}",
            GREEN,
        )
        deposit = outcome.deposit or {}
        if deposit.get("body"):
            embed.add_field(name="알림 원문", value=f"```{utils.truncate(deposit['body'], 300)}```", inline=False)
        await self.bot.service.notify_log(outcome.guild_id, embed)

    async def deposit_review(self, outcome: MatchOutcome) -> None:
        if outcome.guild_id and outcome.user_id:
            await self._dm(
                outcome.guild_id,
                outcome.user_id,
                f"입금 **{utils.fmt_won(outcome.amount)}** 이 접수되었습니다.\n"
                "자동 매칭 조건이 맞지 않아 관리자 확인 후 충전됩니다.",
            )
        embed = base_embed(
            "입금 확인 필요",
            f"{money_line('입금자', outcome.depositor or '-')}\n"
            f"{money_line('금액', utils.fmt_won(outcome.amount))}\n"
            f"{money_line('사유', outcome.message)}\n\n"
            "웹 패널 → 입금 탭에서 승인할 수 있습니다.",
            ORANGE,
        )
        if outcome.candidates:
            lines = [
                f"· <@{candidate['discord_id']}> · {candidate['depositor']} · `{candidate['code']}`"
                for candidate in outcome.candidates[:5]
            ]
            embed.add_field(name="후보 입금 요청", value="\n".join(lines), inline=False)
        deposit = outcome.deposit or {}
        if deposit.get("body"):
            embed.add_field(name="알림 원문", value=f"```{utils.truncate(deposit['body'], 300)}```", inline=False)
        await self.bot.service.notify_log(outcome.guild_id or (outcome.candidates[0]["guild_id"] if outcome.candidates else None), embed)

    async def deposit_unmatched(self, outcome: MatchOutcome) -> None:
        embed = base_embed(
            "미매칭 입금",
            f"{money_line('입금자', outcome.depositor or '-')}\n"
            f"{money_line('금액', utils.fmt_won(outcome.amount))}\n\n"
            "일치하는 입금 요청이 없습니다. 웹 패널에서 직접 회원에게 귀속할 수 있습니다.",
            RED,
        )
        deposit = outcome.deposit or {}
        if deposit.get("body"):
            embed.add_field(name="알림 원문", value=f"```{utils.truncate(deposit['body'], 300)}```", inline=False)
        if outcome.guild_id:
            await self.bot.service.notify_log(outcome.guild_id, embed)
        else:
            for guild in self.bot.db.list_guilds():
                if guild.get("log_channel_id"):
                    await self.bot.service.notify_log(guild["id"], embed)

    async def purchase_completed(self, guild: dict, result: dict, discord_id: str) -> None:
        if result.get("delivery_type") == "manual":
            return
        product_name = result["product"]["name"]
        item_text = f"{product_name} × {result['quantity']}"
        embed = base_embed(
            "구매 발생",
            f"{money_line('구매자', f'<@{discord_id}>')}\n"
            f"{money_line('상품', item_text)}\n"
            f"{money_line('금액', utils.fmt_won(result['total']))}\n"
            f"{money_line('전달', result.get('delivery', '-'))}\n"
            f"{money_line('채널', result.get('channel', 'discord'))}",
            PURPLE,
        )
        await self.bot.service.notify_log(guild["id"], embed)


# ----------------------------------------------------------------------- bot
class WiaBot(discord.Client):
    def __init__(self, db: Database, settings: Settings, service: ShopService):
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        super().__init__(intents=intents, activity=discord.CustomActivity(name="VENDRIX · /패널"))
        self.tree = app_commands.CommandTree(self)
        self.db = db
        self.settings = settings
        self.service = service

    async def setup_hook(self) -> None:
        self.add_view(ShopPanelView(self))
        self.add_view(AdminPanelView(self))
        register_commands(self)
        try:
            if self.settings.guild_id:
                self.tree.copy_global_to(guild=discord.Object(id=self.settings.guild_id))
                synced = await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
            else:
                synced = await self.tree.sync()
            log.info("synced %s application commands", len(synced))
        except Exception:
            log.exception("command sync failed")

        @self.tree.error
        async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            original = getattr(error, "original", error)
            if isinstance(error, app_commands.CheckFailure):
                message = str(error) or "이 명령어를 사용할 권한이 없습니다."
            elif isinstance(error, app_commands.CommandOnCooldown):
                message = f"잠시 후 다시 시도해 주세요. ({error.retry_after:.0f}초)"
            elif isinstance(original, ShopError):
                message = str(original)
            else:
                log.exception("command error", exc_info=error)
                message = "명령어 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ {message}", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            except discord.HTTPException:
                pass

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s guilds)", self.user, len(self.guilds))


# ------------------------------------------------------------------ commands
def register_commands(bot: WiaBot) -> None:
    tree = bot.tree
    settings = bot.settings
    db = bot.db

    async def owner_predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id in settings.owner_ids:
            return True
        raise app_commands.CheckFailure("이 명령어는 봇 오너만 사용할 수 있습니다.")

    async def admin_predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.CheckFailure("서버에서만 사용할 수 있습니다.")
        if interaction.user.id in settings.owner_ids:
            return True
        if isinstance(interaction.user, discord.Member):
            if interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild:
                return True
        row = db.get_guild(interaction.guild.id)
        role_id = (row or {}).get("admin_role_id")
        if role_id and isinstance(interaction.user, discord.Member):
            if any(str(role.id) == str(role_id) for role in interaction.user.roles):
                return True
        raise app_commands.CheckFailure("서버 관리자만 사용할 수 있습니다.")

    async def product_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        products = db.list_products(interaction.guild_id, active_only=True)
        current = (current or "").lower()
        return [
            app_commands.Choice(
                name=f"{p['name']} · {utils.fmt_won(p['price'])}"[:100], value=str(p["id"])
            )
            for p in products
            if current in p["name"].lower()
        ][:25]

    async def plan_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()
        return [
            app_commands.Choice(name=label[:100], value=key)
            for label, key in plan_choices()
            if current in label.lower() or current in key
        ][:25]

    # ------------------------------------------------------------------ /패널
    @app_commands.command(name="패널", description="상점 패널을 현재 채널에 게시합니다.")
    @app_commands.check(admin_predicate)
    async def panel_command(interaction: discord.Interaction) -> None:
        try:
            guild_row = bot.service.require_shop(interaction.guild_id)
        except ShopError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        embed = panel_embed(bot, guild_row)
        await interaction.response.send_message(embed=embed, view=ShopPanelView(bot))
        message = await interaction.original_response()
        db.update_guild(interaction.guild_id, panel_channel_id=str(message.channel.id))
        db.audit(interaction.guild_id, str(interaction.user.id), "panel.post", f"message={message.id}")

    # ------------------------------------------------------------------ /관리
    @app_commands.command(name="관리", description="상점 관리자 패널을 엽니다.")
    @app_commands.check(admin_predicate)
    async def admin_command(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=admin_embed(bot, interaction.guild_id), view=AdminPanelView(bot), ephemeral=True
        )

    # ------------------------------------------------------------------ /등록
    @app_commands.command(name="등록", description="이 서버에 상점을 등록합니다. (서버 관리자 전용)")
    @app_commands.describe(라이센스="발급받은 라이센스 키 (오너는 생략 가능)")
    @app_commands.check(admin_predicate)
    async def register_command(interaction: discord.Interaction, 라이센스: str | None = None) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if 라이센스:
                result = bot.service.redeem_license(guild.id, interaction.user.id, 라이센스, guild.name)
                spec = resolve_plan(result.get("plan"))
                features = " · ".join(spec.summary_lines())
                message = (
                    f"✅ **{guild.name}** 상점이 등록되었습니다.\n"
                    f"플랜: **{spec.name}** — {spec.tagline}\n"
                    f"{features}\n"
                    f"만료: **{utils.fmt_dt(result['expires_at'], '%Y.%m.%d %H:%M')}**"
                )
            elif interaction.user.id in settings.owner_ids:
                expires = utils.now() + 3650 * 86400
                db.register_guild(guild.id, guild.name, "OWNER", expires)
                message = (
                    f"✅ 오너 권한으로 **{guild.name}** 상점을 등록했습니다.\n"
                    f"만료: **{utils.fmt_dt(expires, '%Y.%m.%d %H:%M')}** (10년)"
                )
            else:
                await interaction.followup.send(
                    "❌ 라이센스 키가 필요합니다. 오너에게 키를 요청한 뒤 `/등록 라이센스:VDX-....` 로 등록하세요.",
                    ephemeral=True,
                )
                return
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(message + "\n이제 `/패널` 로 상점 패널을 게시하세요.", ephemeral=True)

    # --------------------------------------------------------------- /웹패널
    @app_commands.command(name="웹패널", description="웹 상점/관리 패널에 접속할 1회용 링크를 받습니다.")
    async def webpanel_command(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        try:
            bot.service.require_shop(interaction.guild_id)
        except ShopError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        is_admin = interaction.user.id in settings.owner_ids or bot.service.is_admin_member(
            interaction.guild_id, interaction.user.id
        )
        url = bot.service.issue_login_token(interaction.guild_id, interaction.user.id, is_admin)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="웹 패널 열기", url=url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(
            "🔗 **1회용 웹 패널 링크**\n"
            f"{settings.login_token_minutes}분 안에 접속하세요. 링크는 한 번만 사용할 수 있습니다.\n"
            f"권한: **{'관리자' if is_admin else '일반 회원'}**",
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------- /제품전송
    @app_commands.command(name="제품전송", description="회원에게 상품을 지급합니다.")
    @app_commands.describe(상품="지급할 상품", 대상="받을 회원", 수량="수량 (기본 1)")
    @app_commands.autocomplete(상품=product_autocomplete)
    @app_commands.check(admin_predicate)
    async def send_command(
        interaction: discord.Interaction,
        상품: str,
        대상: discord.Member,
        수량: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        if not str(상품).isdigit():
            await interaction.response.send_message("상품을 목록에서 선택해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.service.send_product(
                interaction.guild_id, 대상.id, int(상품), int(수량), actor=str(interaction.user.id)
            )
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ **{대상}** 님에게 **{result['product']['name']} × {result['quantity']}** 지급 완료\n"
            f"전달: {result['delivery']}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /충전
    @app_commands.command(name="충전", description="회원의 잔액을 충전합니다. 금액 앞에 - 를 붙이면 차감합니다.")
    @app_commands.describe(대상="대상 회원", 금액="충전 금액 (차감은 음수)")
    @app_commands.check(admin_predicate)
    async def charge_command(
        interaction: discord.Interaction,
        대상: discord.Member,
        금액: app_commands.Range[int, -10_000_000, 10_000_000],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            balance = await bot.service.charge(
                interaction.guild_id, 대상.id, int(금액), actor=str(interaction.user.id)
            )
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ **{대상}** 잔액: **{utils.fmt_won(balance)}** (처리 {utils.fmt_won(금액)})", ephemeral=True
        )

    # ------------------------------------------------------------------ /잔액
    @app_commands.command(name="잔액", description="내 잔액과 입금 요청을 확인합니다.")
    async def balance_command(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        user = db.ensure_user(interaction.guild_id, interaction.user.id)
        pending = db.list_pending(interaction.guild_id)
        lines = [money_line("잔액", utils.fmt_won(user["balance"])), money_line("누적 사용", utils.fmt_won(user["total_spent"]))]
        if pending:
            lines.append("\n**진행 중인 입금 요청**")
            for item in pending[:5]:
                lines.append(
                    f"· {utils.fmt_won(item['amount'])} · `{item['code']}` · {utils.fmt_dt(item['expires_at'], '%m/%d %H:%M')} 까지"
                )
        await interaction.response.send_message(embed=base_embed("내 지갑", "\n".join(lines)), ephemeral=True)

    # ------------------------------------------------------------------ /입금
    @app_commands.command(name="입금", description="입금 요청을 만들고 계좌를 안내받습니다.")
    @app_commands.describe(금액="입금할 금액", 입금자명="은행에 표시될 이름")
    async def deposit_command(
        interaction: discord.Interaction,
        금액: app_commands.Range[int, 100, 10_000_000],
        입금자명: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            pending = bot.service.create_deposit_request(
                interaction.guild_id, interaction.user.id, int(금액), 입금자명
            )
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        await interaction.followup.send(embed=base_embed("입금 안내", bot.service.deposit_guide(pending)), ephemeral=True)

    # ------------------------------------------------------------------ /구매
    @app_commands.command(name="구매", description="잔액으로 상품을 구매합니다.")
    @app_commands.describe(상품="구매할 상품", 수량="수량 (기본 1)")
    @app_commands.autocomplete(상품=product_autocomplete)
    async def buy_command(
        interaction: discord.Interaction,
        상품: str,
        수량: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        if not str(상품).isdigit():
            await interaction.response.send_message("상품을 목록에서 선택해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await bot.service.purchase(
                interaction.guild_id, interaction.user.id, int(상품), int(수량), channel="discord"
            )
        except ShopError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        embed = base_embed(
            "구매 완료",
            f"{money_line('상품', result['product']['name'])}\n"
            f"{money_line('수량', str(result['quantity']))}\n"
            f"{money_line('결제 금액', utils.fmt_won(result['total']))}\n"
            f"{money_line('남은 잔액', utils.fmt_won(result['balance']))}\n"
            f"{money_line('전달', result['delivery'])}",
            GREEN,
        )
        embed.set_footer(text=f"주문번호 #{result['purchase_id']}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ /공지
    @app_commands.command(name="공지", description="패널의 공지사항/이벤트 문구를 수정합니다.")
    @app_commands.describe(공지="공지사항 내용", 이벤트="이벤트 내용")
    @app_commands.check(admin_predicate)
    async def notice_command(
        interaction: discord.Interaction,
        공지: str | None = None,
        이벤트: str | None = None,
    ) -> None:
        if not 공지 and not 이벤트:
            await interaction.response.send_message("공지 또는 이벤트 중 하나는 입력해 주세요.", ephemeral=True)
            return
        fields: dict[str, Any] = {}
        if 공지 is not None:
            fields["notice"] = 공지[:500]
        if 이벤트 is not None:
            fields["event_text"] = 이벤트[:500]
        db.update_guild(interaction.guild_id, **fields)
        await interaction.response.send_message("✅ 수정되었습니다. `/패널` 로 패널을 다시 게시하세요.", ephemeral=True)

    # ------------------------------------------------------------------ /플랜
    @app_commands.command(name="플랜", description="현재 서버 플랜과 등급별 기능 제한을 확인합니다.")
    async def plan_command(interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        try:
            current = bot.service.plan_for(interaction.guild_id)
        except ShopError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        lines = []
        for _label, key in plan_choices():
            spec = resolve_plan(key)
            bullet = "▶" if spec.key == current.key else "•"
            lines.append(
                f"{bullet} **{spec.name}** — {spec.tagline}\n"
                f"　{' · '.join(spec.summary_lines())}"
            )
        embed = base_embed("플랜 안내", "\n\n".join(lines))
        embed.set_footer(text=f"현재 플랜: {current.name} · 자동충전은 스탠다드부터 사용할 수 있습니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------------------------------------------------------------- /라이센스
    license_group = app_commands.Group(name="라이센스", description="오너 전용 라이센스 관리")

    @license_group.command(name="발급", description="라이센스 키를 발급합니다. (오너 전용)")
    @app_commands.describe(수량="발급 수량", 기간="유효 기간(일)", 플랜="플랜 등급", 메모="관리용 메모")
    @app_commands.autocomplete(플랜=plan_autocomplete)
    @app_commands.check(owner_predicate)
    async def license_issue(
        interaction: discord.Interaction,
        수량: app_commands.Range[int, 1, 50] = 1,
        기간: app_commands.Range[int, 1, 3650] = 30,
        플랜: str = "standard",
        메모: str = "",
    ) -> None:
        keys = bot.service.issue_licenses(int(수량), int(기간), 플랜, str(interaction.user.id), 메모)
        if not keys:
            await interaction.response.send_message("❌ 키 발급에 실패했습니다.", ephemeral=True)
            return
        spec = resolve_plan(플랜)
        await interaction.response.send_message(
            f"🔑 **라이센스 {len(keys)}개 발급**\n"
            f"플랜: **{spec.name}** — {spec.tagline}\n"
            f"기간: {기간}일 · {' · '.join(spec.summary_lines())}\n"
            "```\n" + "\n".join(keys) + "\n```",
            ephemeral=True,
        )

    @license_group.command(name="목록", description="최근 발급된 라이센스를 확인합니다. (오너 전용)")
    @app_commands.check(owner_predicate)
    async def license_list(interaction: discord.Interaction) -> None:
        rows = db.list_licenses(limit=15)
        if not rows:
            await interaction.response.send_message("발급된 라이센스가 없습니다.", ephemeral=True)
            return
        lines = []
        for row in rows:
            state = "폐기" if row["revoked"] else ("사용" if row["redeemed_by"] else "미사용")
            plan_name = resolve_plan(row.get("plan")).name
            lines.append(
                f"`{row['key']}` · {plan_name} · {row['duration_days']}일 · {state} · {utils.fmt_date(row['created_at'])}"
            )
        await interaction.response.send_message("**최근 라이센스 15개**\n" + "\n".join(lines), ephemeral=True)

    @license_group.command(name="폐기", description="라이센스 키를 폐기합니다. (오너 전용)")
    @app_commands.describe(키="폐기할 라이센스 키")
    @app_commands.check(owner_predicate)
    async def license_revoke(interaction: discord.Interaction, 키: str) -> None:
        ok = db.revoke_license(키)
        await interaction.response.send_message(
            "✅ 폐기되었습니다." if ok else "❌ 해당 키를 찾을 수 없습니다.", ephemeral=True
        )

    @license_group.command(name="정보", description="라이센스 현황을 확인합니다. (오너 전용)")
    @app_commands.check(owner_predicate)
    async def license_info(interaction: discord.Interaction) -> None:
        summary = db.license_summary()
        embed = base_embed(
            "라이센스 현황",
            f"**전체 발급** · {summary['total']}개\n"
            f"**사용됨** · {summary['used']}개\n"
            f"**폐기** · {summary['revoked']}개\n"
            f"**활성 서버** · {summary['active_guilds']}개",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    for command in (panel_command, admin_command, register_command, webpanel_command, send_command, charge_command, balance_command, deposit_command, buy_command, notice_command, plan_command):
        tree.add_command(command)
    tree.add_command(license_group)
