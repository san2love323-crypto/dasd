# -*- coding: utf-8 -*-
"""
임대형 디스코드 자판기 봇 (봇 + 웹패널 + Pushbullet 자동충전) - 단일 파일
필요: pip install -U "discord.py>=2.6" aiohttp
실행: python3 main.py
"""
import asyncio, io, json, re, secrets, sqlite3, time

import aiohttp
import discord
from aiohttp import web
from discord import app_commands, ui

# ───────────────────────── 설정 (여기만 고치면 됨) ─────────────────────────
TOKEN = "여기에_봇_토큰"
OWNER_ID = 123456789012345678          # 내 디스코드 유저 ID (라이센스 발급은 이 사람만 가능)
WEB_PORT = 8080                        # 웹패널 포트
WEB_BASE_URL = "http://내서버IP:8080"  # 웹패널 주소 (끝에 / 없이)
DB_PATH = "vending.db"
CHARGE_EXPIRE = 600                    # 충전 신청 유효시간(초)
MIN_CHARGE, MAX_CHARGE = 1000, 1000000
# 자동충전으로 인정할 은행 앱(패키지명). 웹패널에서 서버별로 추가/삭제 가능.
DEFAULT_PACKAGES = [
    "com.kakaobank.channel", "viva.republica.toss", "com.kbstar.kbbank",
    "com.shinhan.sbanking", "com.wooribank.smart.npib", "com.kbankwith.smartbank",
    "nh.smart.banking", "com.kebhana.hanapush",
]
# ──────────────────────────────────────────────────────────────────────────

now = lambda: int(time.time())
won = lambda n: f"{n:,}원"

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses(key TEXT PRIMARY KEY, days INTEGER, created_at INTEGER, used_guild INTEGER, used_at INTEGER);
CREATE TABLE IF NOT EXISTS guilds(guild_id INTEGER PRIMARY KEY, expires_at INTEGER, shop_name TEXT DEFAULT '자판기',
  notice TEXT DEFAULT '', event TEXT DEFAULT '', bank_info TEXT DEFAULT '', banner_url TEXT DEFAULT '',
  log_channel INTEGER DEFAULT 0, pb_token TEXT DEFAULT '', pb_packages TEXT DEFAULT '',
  panel_channel INTEGER DEFAULT 0, panel_message INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(guild_id INTEGER, user_id INTEGER, name TEXT DEFAULT '', balance INTEGER DEFAULT 0,
  charged INTEGER DEFAULT 0, spent INTEGER DEFAULT 0, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, price INTEGER, description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, content TEXT);
CREATE TABLE IF NOT EXISTS charges(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, depositor TEXT,
  amount INTEGER, status TEXT, method TEXT DEFAULT '', created_at INTEGER, done_at INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, product_name TEXT,
  qty INTEGER, total INTEGER, kind TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS notifs(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, package TEXT, app TEXT, text TEXT,
  result TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS web_tokens(token TEXT PRIMARY KEY, guild_id INTEGER, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, guild_id INTEGER, expires_at INTEGER);
"""


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    def q(self, sql, *a):
        return self.c.execute(sql, a).fetchall()

    def one(self, sql, *a):
        return self.c.execute(sql, a).fetchone()

    def x(self, sql, *a):
        with self.c:
            return self.c.execute(sql, a)

    # ── 라이센스 / 서버 ──
    def new_license(self, days):
        key = "VM-" + "-".join(secrets.token_hex(3).upper() for _ in range(3))
        self.x("INSERT INTO licenses VALUES(?,?,?,NULL,NULL)", key, days, now())
        return key

    def redeem(self, key, gid):
        """성공 시 새 만료시각, 실패 시 None"""
        with self.c:
            lic = self.one("SELECT * FROM licenses WHERE key=? AND used_guild IS NULL", key)
            if not lic:
                return None
            g = self.one("SELECT expires_at FROM guilds WHERE guild_id=?", gid)
            exp = max(g["expires_at"] if g else 0, now()) + lic["days"] * 86400
            if g:
                self.c.execute("UPDATE guilds SET expires_at=? WHERE guild_id=?", (exp, gid))
            else:
                self.c.execute("INSERT INTO guilds(guild_id, expires_at, pb_packages) VALUES(?,?,?)",
                               (gid, exp, "\n".join(DEFAULT_PACKAGES)))
            self.c.execute("UPDATE licenses SET used_guild=?, used_at=? WHERE key=?", (gid, now(), key))
            return exp

    def guild(self, gid):
        return self.one("SELECT * FROM guilds WHERE guild_id=?", gid)

    def active(self, gid):
        g = self.guild(gid)
        return g if g and g["expires_at"] > now() else None

    # ── 회원 ──
    def user(self, gid, uid, name=None):
        self.x("INSERT OR IGNORE INTO users(guild_id,user_id) VALUES(?,?)", gid, uid)
        if name:
            self.x("UPDATE users SET name=? WHERE guild_id=? AND user_id=?", name, gid, uid)
        return self.one("SELECT * FROM users WHERE guild_id=? AND user_id=?", gid, uid)

    def add_balance(self, gid, uid, delta, count_charge=True):
        self.user(gid, uid)
        self.x("UPDATE users SET balance=balance+?, charged=charged+? WHERE guild_id=? AND user_id=?",
               delta, delta if (count_charge and delta > 0) else 0, gid, uid)
        return self.user(gid, uid)["balance"]

    # ── 제품 ──
    def products(self, gid):
        return self.q("SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id) AS stock "
                      "FROM products p WHERE guild_id=? ORDER BY id", gid)

    def product(self, gid, pid):
        return self.one("SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id) AS stock "
                        "FROM products p WHERE guild_id=? AND id=?", gid, pid)

    def add_stock(self, pid, text):
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        with self.c:
            self.c.executemany("INSERT INTO stock(product_id, content) VALUES(?,?)", [(pid, l) for l in lines])
        return len(lines)

    def take(self, gid, uid, pid, qty, pay):
        """재고를 꺼냄. pay=True면 잔액 차감. (상태, 데이터) 반환 - 전부 한 트랜잭션"""
        with self.c:
            p = self.product(gid, pid)
            if not p:
                return "noproduct", None
            rows = self.q("SELECT id, content FROM stock WHERE product_id=? ORDER BY id LIMIT ?", pid, qty)
            if qty < 1 or len(rows) < qty:
                return "nostock", p
            total = p["price"] * qty if pay else 0
            if pay and self.user(gid, uid)["balance"] < total:
                return "nomoney", p
            self.c.execute(f"DELETE FROM stock WHERE id IN ({','.join('?' * qty)})", [r["id"] for r in rows])
            self.c.execute("UPDATE users SET balance=balance-?, spent=spent+? WHERE guild_id=? AND user_id=?",
                           (total, total, gid, uid))
            self.c.execute("INSERT INTO orders(guild_id,user_id,product_name,qty,total,kind,created_at) VALUES(?,?,?,?,?,?,?)",
                           (gid, uid, p["name"], qty, total, "구매" if pay else "전송", now()))
            return "ok", {"name": p["name"], "total": total, "items": [r["content"] for r in rows]}

    # ── 충전 ──
    def expire_charges(self):
        self.x("UPDATE charges SET status='expired' WHERE status='pending' AND created_at<?", now() - CHARGE_EXPIRE)

    def new_charge(self, gid, uid, depositor, amount):
        self.expire_charges()
        if self.one("SELECT 1 FROM charges WHERE guild_id=? AND status='pending' AND depositor=? AND amount=? AND user_id!=?",
                    gid, depositor, amount, uid):
            return None  # 같은 이름+금액 신청이 이미 있음
        self.x("UPDATE charges SET status='cancelled' WHERE guild_id=? AND user_id=? AND status='pending'", gid, uid)
        return self.x("INSERT INTO charges(guild_id,user_id,depositor,amount,status,created_at) VALUES(?,?,?,?,?,?)",
                      gid, uid, depositor, amount, "pending", now()).lastrowid

    def finish_charge(self, cid, method):
        """대기중인 신청을 완료 처리하고 잔액 지급. 이미 처리됐으면 None"""
        with self.c:
            ch = self.one("SELECT * FROM charges WHERE id=? AND status='pending'", cid)
            if not ch:
                return None
            self.c.execute("UPDATE charges SET status='done', method=?, done_at=? WHERE id=?", (method, now(), cid))
        return ch, self.add_balance(ch["guild_id"], ch["user_id"], ch["amount"])


db = DB(DB_PATH)

# ═════════════════════════ Pushbullet 자동충전 ═════════════════════════
AMOUNT_PATTERNS = [
    r"입금\s*(\d[\d,]*)\s*원",                        # 입금 10,000원
    r"(\d[\d,]*)\s*원\s*(?:이|을)?\s*입금",             # 10,000원 입금
    r"(\d[\d,]*)\s*원\s*(?:이|을)?\s*(?:보냈|받았)",     # 10,000원을 보냈어요
    r"입금\s*(\d{1,3}(?:,\d{3})+|\d+)(?![\d/:.\-*,])",  # 전자금융입금 10,000
]


def parse_deposit(text):
    """입금 알림이면 금액(int), 아니면 None"""
    if not any(k in text for k in ("입금", "보냈", "받았")):
        return None
    t = re.sub(r"잔액\s*\d[\d,]*\s*원?", " ", text)
    for pat in AMOUNT_PATTERNS:
        m = re.search(pat, t)
        if m:
            return int(m.group(1).replace(",", ""))
    c = re.findall(r"(\d[\d,]*)\s*원", t)
    return int(c[0].replace(",", "")) if len(c) == 1 else None


def handle_notification(gid, push):
    """미러된 알림 1건 처리. 충전이 성사되면 (charge_row, 새잔액) 반환"""
    g = db.active(gid)
    if not g:
        return None
    pkg = push.get("package_name") or ""
    text = f"{push.get('title') or ''} {push.get('body') or ''}".strip()
    amount = parse_deposit(text)
    if amount is None:
        return None  # 입금 알림이 아니면 저장도 안 함
    result, done = "대기중인 신청 없음", None
    if pkg not in g["pb_packages"].split():
        result = "허용되지 않은 앱"
    else:
        db.expire_charges()
        flat = text.replace(" ", "")
        for ch in db.q("SELECT * FROM charges WHERE guild_id=? AND status='pending' AND amount=? ORDER BY id", gid, amount):
            if ch["depositor"].replace(" ", "") in flat:
                done = db.finish_charge(ch["id"], "자동")
                if done:
                    result = f"충전 완료 ({ch['depositor']} {won(amount)})"
                    break
    db.x("INSERT INTO notifs(guild_id,package,app,text,result,created_at) VALUES(?,?,?,?,?,?)",
         gid, pkg, push.get("application_name") or "", text[:300], result, now())
    db.x("DELETE FROM notifs WHERE guild_id=? AND id NOT IN (SELECT id FROM notifs WHERE guild_id=? ORDER BY id DESC LIMIT 50)", gid, gid)
    return done


class PushbulletManager:
    def __init__(self, on_credit):
        self.tasks, self.tokens, self.status, self.on_credit = {}, {}, {}, on_credit

    def sync(self):
        """DB 설정에 맞춰 서버별 연결을 켜고 끔"""
        want = {g["guild_id"]: g["pb_token"] for g in db.q("SELECT * FROM guilds WHERE pb_token!='' AND expires_at>?", now())}
        for gid in list(self.tasks):
            if want.get(gid) != self.tokens.get(gid):
                self.tasks.pop(gid).cancel()
                self.tokens.pop(gid, None)
                self.status.pop(gid, None)
        for gid, tok in want.items():
            if gid not in self.tasks:
                self.tokens[gid] = tok
                self.tasks[gid] = asyncio.create_task(self._run(gid, tok))

    async def _run(self, gid, token):
        delay = 3
        while True:
            try:
                self.status[gid] = "연결 중"
                async with aiohttp.ClientSession() as s:
                    async with s.ws_connect("wss://stream.pushbullet.com/websocket/" + token, heartbeat=None) as ws:
                        self.status[gid], delay = "연결됨", 3
                        while True:
                            msg = await ws.receive(timeout=90)  # nop이 30초마다 옴
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                break
                            data = json.loads(msg.data)
                            push = data.get("push") or {}
                            if data.get("type") != "push":
                                continue
                            if push.get("encrypted"):
                                self.status[gid] = "오류: Pushbullet 종단간 암호화를 꺼주세요"
                            elif push.get("type") == "mirror":
                                done = handle_notification(gid, push)
                                if done:
                                    asyncio.create_task(self.on_credit(*done))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.status[gid] = f"재연결 대기 ({type(e).__name__})"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def check_pb_token(token):
    async with aiohttp.ClientSession() as s:
        async with s.get("https://api.pushbullet.com/v2/users/me", headers={"Access-Token": token}) as r:
            return r.status == 200


# ═════════════════════════ 디스코드 봇 ═════════════════════════
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.pb = PushbulletManager(self.on_credit)

    async def setup_hook(self):
        runner = web.AppRunner(make_web_app())
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
        self.pb.sync()
        self.loop.create_task(self.housekeeping())
        await self.tree.sync()

    async def housekeeping(self):
        while True:
            await asyncio.sleep(300)
            db.expire_charges()
            db.x("DELETE FROM web_tokens WHERE expires_at<?", now())
            db.x("DELETE FROM sessions WHERE expires_at<?", now())
            self.pb.sync()  # 라이센스 만료된 서버 연결 정리

    async def on_ready(self):
        print(f"로그인: {self.user} / 웹패널: {WEB_BASE_URL}")

    async def log(self, gid, title, desc, color=0x3B5BFF):
        g = db.guild(gid)
        if g and g["log_channel"]:
            try:
                await self.get_partial_messageable(g["log_channel"]).send(
                    embed=discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow()))
            except discord.HTTPException:
                pass

    async def dm(self, uid, **kw):
        try:
            user = self.get_user(uid) or await self.fetch_user(uid)
            await user.send(**kw)
            return True
        except discord.HTTPException:
            return False

    async def on_credit(self, ch, balance):
        await self.dm(ch["user_id"], embed=discord.Embed(
            title="✅ 자동충전 완료", color=0x2ECC71,
            description=f"**{won(ch['amount'])}** 충전되었습니다.\n현재 잔액: **{won(balance)}**"))
        await self.log(ch["guild_id"], "자동충전", f"<@{ch['user_id']}> · 입금자 `{ch['depositor']}` · {won(ch['amount'])}", 0x2ECC71)

    async def refresh_panel(self, gid):
        g = db.guild(gid)
        if g and g["panel_message"]:
            try:
                await self.get_partial_messageable(g["panel_channel"]).get_partial_message(g["panel_message"]).edit(view=panel_view(g))
            except discord.HTTPException:
                pass

    # 패널 버튼은 봇을 껐다 켜도 동작하도록 custom_id로 직접 처리
    async def on_interaction(self, it: discord.Interaction):
        if it.type != discord.InteractionType.component or not it.guild_id:
            return
        cid = (it.data or {}).get("custom_id", "")
        if not cid.startswith("vm:"):
            return
        g = db.active(it.guild_id)
        if not g:
            return await it.response.send_message("이 서버의 라이센스가 만료되었습니다. 관리자에게 문의하세요.", ephemeral=True)
        db.user(it.guild_id, it.user.id, str(it.user))
        await PANEL_ACTIONS[cid](it, g)


bot = Bot()
tree = bot.tree


def panel_view(g):
    v = ui.LayoutView(timeout=None)
    c = ui.Container(accent_colour=discord.Colour(0x3B5BFF))
    if g["banner_url"]:
        c.add_item(ui.MediaGallery(discord.MediaGalleryItem(g["banner_url"])))
    quote = lambda s, empty: "\n".join("> " + l for l in (s or empty).splitlines())
    c.add_item(ui.TextDisplay(f"## {g['shop_name']} Home"))
    c.add_item(ui.Separator())
    c.add_item(ui.TextDisplay("### 📢 공지사항\n" + quote(g["notice"], "공지사항이 등록되지 않았습니다")))
    c.add_item(ui.Separator())
    c.add_item(ui.TextDisplay("### 🗓️ 이벤트\n" + quote(g["event"], "진행 중인 이벤트가 없습니다")))
    c.add_item(ui.Separator())
    row = ui.ActionRow()
    for label, emoji, cid in (("제품목록", "🏷️", "vm:list"), ("구매하기", "💳", "vm:buy"),
                              ("유저정보", "👤", "vm:me"), ("문의하기", "💬", "vm:ask")):
        row.add_item(ui.Button(label=label, emoji=emoji, custom_id=cid, style=discord.ButtonStyle.secondary))
    c.add_item(row)
    c.add_item(ui.TextDisplay(f"-# {g['shop_name']} | <t:{now()}:f>"))
    v.add_item(c)
    return v


def deliver_kwargs(name, items):
    body = "\n".join(items)
    if len(body) <= 1800:
        return {"content": f"📦 **{name}** x{len(items)}\n```\n{body}\n```"}
    return {"content": f"📦 **{name}** x{len(items)}", "file": discord.File(io.BytesIO(body.encode()), "제품.txt")}


# ── 패널: 제품목록 ──
async def act_list(it, g):
    ps = db.products(it.guild_id)
    e = discord.Embed(title="🏷️ 제품목록", color=0x3B5BFF)
    for p in ps[:25]:
        e.add_field(name=p["name"], value=f"가격 **{won(p['price'])}** · 재고 **{p['stock']}개**\n{p['description']}"[:1024], inline=False)
    if not ps:
        e.description = "등록된 제품이 없습니다."
    await it.response.send_message(embed=e, ephemeral=True)


# ── 패널: 구매하기 ──
class QtyModal(ui.Modal, title="구매 수량"):
    qty = ui.TextInput(label="수량", default="1", max_length=3)

    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    async def on_submit(self, it):
        if not self.qty.value.isdigit() or not 1 <= int(self.qty.value) <= 100:
            return await it.response.send_message("수량은 1~100 사이 숫자로 입력해주세요.", ephemeral=True)
        st, d = db.take(it.guild_id, it.user.id, self.pid, int(self.qty.value), pay=True)
        if st != "ok":
            msg = {"noproduct": "없는 제품입니다.", "nostock": "재고가 부족합니다.",
                   "nomoney": "잔액이 부족합니다. 유저정보 → 충전하기로 충전해주세요."}[st]
            return await it.response.send_message("❌ " + msg, ephemeral=True)
        await it.response.send_message(**deliver_kwargs(d["name"], d["items"]), ephemeral=True)
        await bot.dm(it.user.id, **deliver_kwargs(d["name"], d["items"]))
        await bot.log(it.guild_id, "구매", f"{it.user.mention} · {d['name']} x{len(d['items'])} · {won(d['total'])}")


class BuySelect(ui.Select):
    def __init__(self, ps):
        super().__init__(placeholder="구매할 제품을 선택하세요", options=[
            discord.SelectOption(label=p["name"][:100], value=str(p["id"]),
                                 description=f"{won(p['price'])} · 재고 {p['stock']}개") for p in ps[:25]])

    async def callback(self, it):
        await it.response.send_modal(QtyModal(int(self.values[0])))


async def act_buy(it, g):
    ps = db.products(it.guild_id)
    if not ps:
        return await it.response.send_message("등록된 제품이 없습니다.", ephemeral=True)
    v = ui.View(timeout=180)
    v.add_item(BuySelect(ps))
    await it.response.send_message(view=v, ephemeral=True)


# ── 패널: 유저정보 / 충전 ──
class ChargeModal(ui.Modal, title="충전 신청"):
    depositor = ui.TextInput(label="입금자명 (실제 보내는 이름과 똑같이)", min_length=2, max_length=20)
    amount = ui.TextInput(label="충전 금액 (숫자만)", max_length=8)

    async def on_submit(self, it):
        g = db.active(it.guild_id)
        amt = self.amount.value.replace(",", "").strip()
        name = self.depositor.value.strip()
        if not g or not amt.isdigit() or not MIN_CHARGE <= int(amt) <= MAX_CHARGE:
            return await it.response.send_message(f"금액은 {won(MIN_CHARGE)} ~ {won(MAX_CHARGE)} 사이로 입력해주세요.", ephemeral=True)
        if not db.new_charge(it.guild_id, it.user.id, name, int(amt)):
            return await it.response.send_message("같은 입금자명·금액의 신청이 이미 있습니다. 금액을 조금 바꿔서 다시 신청해주세요.", ephemeral=True)
        auto = bool(g["pb_token"])
        e = discord.Embed(title="💰 충전 신청 완료", color=0x3B5BFF, description=(
            f"아래 계좌로 **{CHARGE_EXPIRE // 60}분 안에** 입금해주세요.\n\n{g['bank_info'] or '(계좌 안내 미등록 - 관리자 문의)'}\n\n"
            f"입금자명: **{name}**\n금액: **{won(int(amt))}** (정확히 이 금액)\n\n"
            + ("입금이 확인되면 자동으로 충전됩니다." if auto else "관리자가 확인 후 충전해 드립니다.")))
        await it.response.send_message(embed=e, ephemeral=True)
        await bot.log(it.guild_id, "충전 신청", f"{it.user.mention} · 입금자 `{name}` · {won(int(amt))}" + ("" if auto else "\n수동 확인 필요: `/충전`"), 0xF1C40F)


class MeView(ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @ui.button(label="충전하기", emoji="💰", style=discord.ButtonStyle.primary)
    async def charge(self, it, _):
        await it.response.send_modal(ChargeModal())

    @ui.button(label="구매내역", emoji="🧾", style=discord.ButtonStyle.secondary)
    async def history(self, it, _):
        rows = db.q("SELECT * FROM orders WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10", it.guild_id, it.user.id)
        text = "\n".join(f"<t:{r['created_at']}:d> {r['product_name']} x{r['qty']} · {won(r['total'])} ({r['kind']})" for r in rows)
        await it.response.send_message(text or "구매내역이 없습니다.", ephemeral=True)


async def act_me(it, g):
    u = db.user(it.guild_id, it.user.id)
    e = discord.Embed(title="👤 유저정보", color=0x3B5BFF)
    e.add_field(name="잔액", value=won(u["balance"]))
    e.add_field(name="누적 충전", value=won(u["charged"]))
    e.add_field(name="누적 구매", value=won(u["spent"]))
    await it.response.send_message(embed=e, view=MeView(), ephemeral=True)


# ── 패널: 문의하기 ──
class AskModal(ui.Modal, title="문의하기"):
    text = ui.TextInput(label="문의 내용", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, it):
        g = db.guild(it.guild_id)
        if not g["log_channel"]:
            return await it.response.send_message("문의 채널이 설정되지 않았습니다. 관리자에게 직접 문의해주세요.", ephemeral=True)
        await bot.log(it.guild_id, "📨 문의", f"{it.user.mention}\n{self.text.value}", 0xE67E22)
        await it.response.send_message("문의가 접수되었습니다.", ephemeral=True)


async def act_ask(it, g):
    await it.response.send_modal(AskModal())


PANEL_ACTIONS = {"vm:list": act_list, "vm:buy": act_buy, "vm:me": act_me, "vm:ask": act_ask}


# ── 명령어 공통 체크 ──
async def admin_guild(it, need_license=True):
    """서버 관리자 + 라이센스 확인. 통과하면 guild row(또는 True)"""
    if not it.guild or not it.user.guild_permissions.administrator:
        await it.response.send_message("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return None
    if not need_license:
        return True
    g = db.active(it.guild_id)
    if not g:
        await it.response.send_message("등록되지 않았거나 라이센스가 만료된 서버입니다. `/등록`으로 라이센스를 등록하세요.", ephemeral=True)
    return g


# ── 소유자 전용: 라이센스 ──
@tree.command(name="라이센스생성", description="[봇 소유자 전용] 라이센스 키를 만듭니다.")
@app_commands.rename(days="일수", count="개수")
async def license_create(it: discord.Interaction, days: app_commands.Range[int, 1, 3650], count: app_commands.Range[int, 1, 20] = 1):
    if it.user.id != OWNER_ID:
        return await it.response.send_message("봇 소유자만 사용할 수 있습니다.", ephemeral=True)
    keys = [db.new_license(days) for _ in range(count)]
    await it.response.send_message(f"🔑 {days}일 라이센스 {count}개\n```\n" + "\n".join(keys) + "\n```", ephemeral=True)


@tree.command(name="라이센스목록", description="[봇 소유자 전용] 발급한 라이센스를 봅니다.")
async def license_list(it: discord.Interaction):
    if it.user.id != OWNER_ID:
        return await it.response.send_message("봇 소유자만 사용할 수 있습니다.", ephemeral=True)
    rows = db.q("SELECT * FROM licenses ORDER BY created_at DESC LIMIT 30")
    text = "\n".join(f"`{r['key']}` {r['days']}일 · " + (f"사용됨(서버 {r['used_guild']})" if r["used_guild"] else "미사용") for r in rows)
    await it.response.send_message(text or "발급한 라이센스가 없습니다.", ephemeral=True)


# ── /등록 ──
@tree.command(name="등록", description="이 서버를 등록합니다. (서버 관리자 전용)")
@app_commands.rename(key="라이센스키")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def register(it: discord.Interaction, key: str):
    if not await admin_guild(it, need_license=False):
        return
    exp = db.redeem(key.strip(), it.guild_id)
    if not exp:
        return await it.response.send_message("❌ 없는 키이거나 이미 사용된 키입니다.", ephemeral=True)
    bot.pb.sync()
    await it.response.send_message(f"✅ 등록 완료! 만료일: <t:{exp}:f>\n`/관리`로 상점을 설정하고 `/패널`로 패널을 올리세요.", ephemeral=True)


# ── /관리 ──
class ShopModal(ui.Modal, title="상점 설정"):
    def __init__(self, g):
        super().__init__()
        mk = lambda label, key, long=False, n=1000: ui.TextInput(
            label=label, default=g[key], required=False, max_length=n,
            style=discord.TextStyle.paragraph if long else discord.TextStyle.short)
        self.f = {"shop_name": mk("상점 이름", "shop_name", n=40), "notice": mk("공지사항", "notice", True),
                  "event": mk("이벤트", "event", True), "bank_info": mk("입금 계좌 안내 (은행 계좌번호 예금주)", "bank_info", True, 300),
                  "banner_url": mk("배너 이미지 URL", "banner_url", n=300)}
        for i in self.f.values():
            self.add_item(i)

    async def on_submit(self, it):
        v = {k: i.value.strip() for k, i in self.f.items()}
        if v["banner_url"] and not v["banner_url"].startswith("https://"):
            v["banner_url"] = ""
        db.x("UPDATE guilds SET shop_name=?, notice=?, event=?, bank_info=?, banner_url=? WHERE guild_id=?",
             v["shop_name"] or "자판기", v["notice"], v["event"], v["bank_info"], v["banner_url"], it.guild_id)
        await it.response.send_message("✅ 저장했습니다.", ephemeral=True)
        await bot.refresh_panel(it.guild_id)


class ProductModal(ui.Modal, title="제품 추가"):
    name = ui.TextInput(label="제품 이름", max_length=80)
    price = ui.TextInput(label="가격 (숫자만)", max_length=8)
    desc = ui.TextInput(label="설명", required=False, max_length=200)

    async def on_submit(self, it):
        if not self.price.value.isdigit():
            return await it.response.send_message("가격은 숫자만 입력해주세요.", ephemeral=True)
        db.x("INSERT INTO products(guild_id,name,price,description) VALUES(?,?,?,?)",
             it.guild_id, self.name.value.strip(), int(self.price.value), self.desc.value.strip())
        await it.response.send_message(f"✅ **{self.name.value}** 추가됨. 재고는 '재고 추가'로 넣어주세요.", ephemeral=True)


class StockModal(ui.Modal, title="재고 추가"):
    items = ui.TextInput(label="재고 (한 줄에 1개씩)", style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    async def on_submit(self, it):
        if not db.product(it.guild_id, self.pid):
            return await it.response.send_message("없는 제품입니다.", ephemeral=True)
        await it.response.send_message(f"✅ 재고 {db.add_stock(self.pid, self.items.value)}개 추가됨.", ephemeral=True)


class StockSelect(ui.Select):
    def __init__(self, ps):
        super().__init__(placeholder="재고를 넣을 제품", options=[
            discord.SelectOption(label=p["name"][:100], value=str(p["id"]), description=f"재고 {p['stock']}개") for p in ps[:25]])

    async def callback(self, it):
        await it.response.send_modal(StockModal(int(self.values[0])))


class AdminView(ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @ui.button(label="상점 설정", emoji="⚙️")
    async def shop(self, it, _):
        await it.response.send_modal(ShopModal(db.guild(it.guild_id)))

    @ui.button(label="제품 추가", emoji="➕")
    async def product(self, it, _):
        await it.response.send_modal(ProductModal())

    @ui.button(label="재고 추가", emoji="📦")
    async def stock(self, it, _):
        ps = db.products(it.guild_id)
        if not ps:
            return await it.response.send_message("먼저 제품을 추가해주세요.", ephemeral=True)
        v = ui.View(timeout=180)
        v.add_item(StockSelect(ps))
        await it.response.send_message(view=v, ephemeral=True)

    @ui.button(label="이 채널을 로그/문의 채널로", emoji="📝")
    async def logch(self, it, _):
        db.x("UPDATE guilds SET log_channel=? WHERE guild_id=?", it.channel_id, it.guild_id)
        await it.response.send_message("✅ 이 채널에 구매·충전·문의 로그가 올라옵니다.", ephemeral=True)


@tree.command(name="관리", description="상점 관리자 패널을 엽니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def manage(it: discord.Interaction):
    g = await admin_guild(it)
    if not g:
        return
    ps = db.products(it.guild_id)
    e = discord.Embed(title=f"🛠️ {g['shop_name']} 관리", color=0x3B5BFF)
    e.add_field(name="라이센스 만료", value=f"<t:{g['expires_at']}:R>")
    e.add_field(name="제품 / 재고", value=f"{len(ps)}개 / {sum(p['stock'] for p in ps)}개")
    e.add_field(name="자동충전", value=bot.pb.status.get(it.guild_id, "꺼짐 (웹패널에서 설정)"))
    e.set_footer(text="제품 수정·삭제, 회원 관리, 자동충전 설정은 /웹패널")
    await it.response.send_message(embed=e, view=AdminView(), ephemeral=True)


# ── /웹패널 ──
@tree.command(name="웹패널", description="상점 웹 관리에 들어가는 1회용 링크를 받습니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def webpanel(it: discord.Interaction):
    if not await admin_guild(it):
        return
    tok = secrets.token_urlsafe(32)
    db.x("INSERT INTO web_tokens VALUES(?,?,?)", tok, it.guild_id, now() + 300)
    await it.response.send_message(f"🔗 [웹패널 열기]({WEB_BASE_URL}/login?t={tok})\n5분 안에 1번만 쓸 수 있는 링크입니다. 다른 사람에게 주지 마세요.", ephemeral=True)


# ── /제품전송 ──
async def product_autocomplete(it: discord.Interaction, cur: str):
    return [app_commands.Choice(name=f"{p['name']} (재고 {p['stock']})"[:100], value=str(p["id"]))
            for p in db.products(it.guild_id) if cur.lower() in p["name"].lower()][:25]


@tree.command(name="제품전송", description="회원에게 제품을 보냅니다.")
@app_commands.rename(member="대상유저", product="제품", qty="수량")
@app_commands.autocomplete(product=product_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def send_product(it: discord.Interaction, member: discord.Member, product: str, qty: app_commands.Range[int, 1, 100] = 1):
    if not await admin_guild(it):
        return
    db.user(it.guild_id, member.id, str(member))
    st, d = db.take(it.guild_id, member.id, int(product) if product.isdigit() else 0, qty, pay=False)
    if st != "ok":
        return await it.response.send_message("❌ " + ("재고가 부족합니다." if st == "nostock" else "제품을 목록에서 선택해주세요."), ephemeral=True)
    await it.response.defer(ephemeral=True)
    if await bot.dm(member.id, **deliver_kwargs(d["name"], d["items"])):
        await it.followup.send(f"✅ {member.mention}에게 **{d['name']}** x{qty} DM으로 전송했습니다.")
    else:
        await it.followup.send(f"⚠️ {member.mention}의 DM이 막혀 있어요. 아래 내용을 직접 전달해주세요.\n" + deliver_kwargs(d["name"], d["items"])["content"][:1800])
    await bot.log(it.guild_id, "제품전송", f"{it.user.mention} → {member.mention} · {d['name']} x{qty}")


# ── /충전 ──
@tree.command(name="충전", description="회원의 잔액을 충전합니다. 금액 앞에 -를 붙이면 차감합니다.")
@app_commands.rename(member="대상유저", amount="충전금액")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def charge(it: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, -10000000, 10000000]):
    if not await admin_guild(it):
        return
    db.user(it.guild_id, member.id, str(member))
    pend = db.one("SELECT id FROM charges WHERE guild_id=? AND user_id=? AND status='pending' AND amount=?", it.guild_id, member.id, amount)
    bal = db.finish_charge(pend["id"], "수동")[1] if pend else db.add_balance(it.guild_id, member.id, amount)
    word = "충전" if amount >= 0 else "차감"
    await it.response.send_message(f"✅ {member.mention} {won(abs(amount))} {word} → 잔액 **{won(bal)}**", ephemeral=True)
    await bot.log(it.guild_id, f"수동 {word}", f"{it.user.mention} → {member.mention} · {won(amount)} · 잔액 {won(bal)}")


# ── /패널 ──
@tree.command(name="패널", description="상점 패널을 이 채널에 올립니다.")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def panel(it: discord.Interaction):
    g = await admin_guild(it)
    if not g:
        return
    await it.response.defer(ephemeral=True)
    try:
        msg = await it.channel.send(view=panel_view(g))
    except discord.HTTPException as e:
        return await it.followup.send(f"❌ 패널을 올리지 못했습니다. 봇의 채널 권한과 배너 URL을 확인해주세요. ({e.text})")
    db.x("UPDATE guilds SET panel_channel=?, panel_message=? WHERE guild_id=?", it.channel_id, msg.id, it.guild_id)
    await it.followup.send("✅ 패널을 올렸습니다. 공지·이벤트를 바꾸면 이 패널이 자동으로 갱신됩니다.")


# ═════════════════════════ 웹패널 ═════════════════════════
def session_guild(req):
    s = db.one("SELECT guild_id FROM sessions WHERE sid=? AND expires_at>?", req.cookies.get("vm_sid", ""), now())
    if not s or not db.active(s["guild_id"]):
        raise web.HTTPUnauthorized(text="로그인이 필요합니다. 디스코드에서 /웹패널 을 다시 실행하세요.")
    return s["guild_id"]


async def web_login(req):
    tok = req.query.get("t", "")
    row = db.one("SELECT * FROM web_tokens WHERE token=? AND expires_at>?", tok, now())
    db.x("DELETE FROM web_tokens WHERE token=?", tok)  # 1회용
    if not row:
        return web.Response(text="만료되었거나 이미 사용한 링크입니다. /웹패널 을 다시 실행하세요.", status=403)
    sid = secrets.token_urlsafe(32)
    db.x("INSERT INTO sessions VALUES(?,?,?)", sid, row["guild_id"], now() + 43200)
    resp = web.HTTPFound("/")
    resp.set_cookie("vm_sid", sid, max_age=43200, httponly=True, samesite="Strict", secure=WEB_BASE_URL.startswith("https"))
    return resp


async def web_index(req):
    session_guild(req)
    return web.Response(text=PANEL_HTML, content_type="text/html")


async def web_state(req):
    gid = session_guild(req)
    g = dict(db.guild(gid))
    tok = g.pop("pb_token")
    g["pb_token_set"] = bool(tok)
    g["pb_status"] = bot.pb.status.get(gid, "꺼짐")
    rows = lambda sql: [dict(r) for r in db.q(sql, gid)]
    return web.json_response({
        "guild": {k: str(v) if k.endswith(("_id", "channel", "message")) else v for k, v in g.items()},
        "products": [dict(p) for p in db.products(gid)],
        "users": [dict(u, user_id=str(u["user_id"])) for u in db.q("SELECT * FROM users WHERE guild_id=? ORDER BY balance DESC LIMIT 300", gid)],
        "charges": [dict(c, user_id=str(c["user_id"])) for c in db.q("SELECT * FROM charges WHERE guild_id=? ORDER BY id DESC LIMIT 50", gid)],
        "orders": [dict(o, user_id=str(o["user_id"])) for o in db.q("SELECT * FROM orders WHERE guild_id=? ORDER BY id DESC LIMIT 50", gid)],
        "notifs": rows("SELECT * FROM notifs WHERE guild_id=? ORDER BY id DESC LIMIT 30"),
    })


async def web_action(req):
    gid = session_guild(req)
    d = await req.json()
    act = d.get("action")
    num = lambda k, lo=0, hi=10 ** 8: max(lo, min(hi, int(d.get(k) or 0)))
    s = lambda k, n: str(d.get(k) or "").strip()[:n]
    if act == "settings":
        banner = s("banner_url", 300)
        db.x("UPDATE guilds SET shop_name=?, notice=?, event=?, bank_info=?, banner_url=?, pb_packages=? WHERE guild_id=?",
             s("shop_name", 40) or "자판기", s("notice", 1000), s("event", 1000), s("bank_info", 300),
             banner if banner.startswith("https://") else "", "\n".join(s("pb_packages", 2000).split()), gid)
        await bot.refresh_panel(gid)
    elif act == "pb_token":
        tok = s("token", 100)
        if tok and not await check_pb_token(tok):
            return web.json_response({"error": "Pushbullet 토큰이 올바르지 않습니다."}, status=400)
        db.x("UPDATE guilds SET pb_token=? WHERE guild_id=?", tok, gid)
        bot.pb.sync()
    elif act == "allow_package":
        pk = set(db.guild(gid)["pb_packages"].split()) | {s("package", 150)}
        db.x("UPDATE guilds SET pb_packages=? WHERE guild_id=?", "\n".join(sorted(p for p in pk if p)), gid)
    elif act == "product_save":
        if d.get("id"):
            db.x("UPDATE products SET name=?, price=?, description=? WHERE id=? AND guild_id=?", s("name", 80), num("price"), s("description", 200), num("id"), gid)
        else:
            db.x("INSERT INTO products(guild_id,name,price,description) VALUES(?,?,?,?)", gid, s("name", 80), num("price"), s("description", 200))
    elif act in ("product_delete", "stock_add", "stock_clear"):
        if not db.product(gid, num("id")):
            return web.json_response({"error": "없는 제품입니다."}, status=400)
        if act == "stock_add":
            db.add_stock(num("id"), str(d.get("text") or ""))
        else:
            db.x("DELETE FROM stock WHERE product_id=?", num("id"))
            if act == "product_delete":
                db.x("DELETE FROM products WHERE id=?", num("id"))
    elif act == "balance":
        delta = num("delta", -10 ** 7, 10 ** 7)
        uid = num("user_id", 0, 2 ** 63)
        bal = db.add_balance(gid, uid, delta)
        await bot.log(gid, "웹패널 잔액 변경", f"<@{uid}> · {won(delta)} · 잔액 {won(bal)}")
    else:
        return web.json_response({"error": "알 수 없는 요청"}, status=400)
    return web.json_response({"ok": True})


def make_web_app():
    app = web.Application(client_max_size=1024 * 1024)
    app.add_routes([web.get("/login", web_login), web.get("/", web_index),
                    web.get("/api/state", web_state), web.post("/api/action", web_action)])
    return app


PANEL_HTML = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>자판기 웹패널</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,'Malgun Gothic',sans-serif;background:#0f1320;color:#e6e9f2}
header{padding:16px 20px;background:#161b2e;border-bottom:1px solid #262d47;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0;flex:1}nav{display:flex;gap:6px;flex-wrap:wrap}
button{background:#3b5bff;color:#fff;border:0;border-radius:8px;padding:8px 12px;cursor:pointer;font-size:14px}
button.g{background:#262d47}button.r{background:#c0392b}nav button{background:#262d47}nav button.on{background:#3b5bff}
main{max-width:980px;margin:0 auto;padding:20px}.card{background:#161b2e;border:1px solid #262d47;border-radius:12px;padding:16px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 12px}input,textarea{width:100%;background:#0f1320;color:#e6e9f2;border:1px solid #2f3757;border-radius:8px;padding:8px;font:inherit;margin:4px 0 10px}
textarea{min-height:70px}label{font-size:13px;color:#9aa3c0}table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:8px 6px;border-bottom:1px solid #262d47;text-align:left;vertical-align:top;word-break:break-all}th{color:#9aa3c0;font-weight:500}
.row{display:flex;gap:8px;flex-wrap:wrap}.row>*{flex:1;min-width:140px}.stat{font-size:22px;font-weight:700}.muted{color:#9aa3c0;font-size:13px}
.ok{color:#2ecc71}.warn{color:#f1c40f}td button{padding:5px 8px;font-size:12px;margin:2px}
</style></head><body><header><h1 id="title">자판기 웹패널</h1><nav id="nav"></nav></header><main id="main"></main><script>
const TABS={dash:'대시보드',products:'제품',users:'회원',charges:'충전·주문',auto:'자동충전',settings:'설정'};
let S=null,tab='dash';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const won=n=>Number(n).toLocaleString()+'원',dt=t=>t?new Date(t*1000).toLocaleString('ko-KR'):'-';
async function load(){const r=await fetch('/api/state');if(r.status==401){document.body.innerHTML='<main><div class="card">로그인이 만료됐습니다. 디스코드에서 /웹패널 을 다시 실행하세요.</div></main>';return}S=await r.json();render()}
async function act(action,data){const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...data})});
 const j=await r.json().catch(()=>({error:'오류'}));if(j.error){alert(j.error);return false}await load();return true}
const val=id=>document.getElementById(id).value;
function render(){const g=S.guild;document.getElementById('title').textContent=g.shop_name+' 웹패널';
 document.getElementById('nav').innerHTML=Object.entries(TABS).map(([k,v])=>`<button class="${k==tab?'on':''}" onclick="tab='${k}';render()">${v}</button>`).join('');
 document.getElementById('main').innerHTML=VIEWS[tab]()}
const VIEWS={
dash(){const g=S.guild,bal=S.users.reduce((a,u)=>a+u.balance,0),sales=S.users.reduce((a,u)=>a+u.spent,0);
 return `<div class="row">${[['회원',S.users.length+'명'],['제품',S.products.length+'개'],['총 재고',S.products.reduce((a,p)=>a+p.stock,0)+'개'],['누적 판매',won(sales)],['회원 잔액 합계',won(bal)]]
 .map(([a,b])=>`<div class="card"><div class="muted">${a}</div><div class="stat">${b}</div></div>`).join('')}</div>
 <div class="card"><h2>라이센스</h2>만료: ${dt(g.expires_at)}</div><div class="card"><h2>자동충전 상태</h2>${esc(g.pb_status)}</div>`},
products(){return `<div class="card"><h2>제품 추가</h2><div class="row"><input id="pn" placeholder="이름"><input id="pp" type="number" placeholder="가격"><input id="pd" placeholder="설명"></div>
 <button onclick="act('product_save',{name:val('pn'),price:val('pp'),description:val('pd')})">추가</button></div>
 <div class="card"><h2>제품 목록</h2><table><tr><th>이름</th><th>가격</th><th>재고</th><th></th></tr>${S.products.map(p=>`<tr><td>${esc(p.name)}<div class="muted">${esc(p.description)}</div></td><td>${won(p.price)}</td><td>${p.stock}</td>
 <td><button onclick="addStock(${p.id})">재고 추가</button><button class="g" onclick="editP(${p.id})">수정</button><button class="g" onclick="confirm('재고를 모두 비울까요?')&&act('stock_clear',{id:${p.id}})">재고 비우기</button><button class="r" onclick="confirm('제품을 삭제할까요?')&&act('product_delete',{id:${p.id}})">삭제</button></td></tr>`).join('')}</table>
 <div id="stockbox"></div></div>`},
users(){return `<div class="card"><h2>회원 (잔액순)</h2><table><tr><th>유저</th><th>잔액</th><th>누적 충전</th><th>누적 구매</th><th></th></tr>${S.users.map(u=>`<tr><td>${esc(u.name||'?')}<div class="muted">${u.user_id}</div></td><td>${won(u.balance)}</td><td>${won(u.charged)}</td><td>${won(u.spent)}</td>
 <td><button onclick="bal('${u.user_id}')">잔액 변경</button></td></tr>`).join('')}</table></div>`},
charges(){const st={pending:'<span class="warn">대기</span>',done:'<span class="ok">완료</span>',expired:'만료',cancelled:'취소'};
 return `<div class="card"><h2>충전 내역</h2><table><tr><th>시간</th><th>유저</th><th>입금자명</th><th>금액</th><th>상태</th></tr>${S.charges.map(c=>`<tr><td>${dt(c.created_at)}</td><td>${c.user_id}</td><td>${esc(c.depositor)}</td><td>${won(c.amount)}</td><td>${st[c.status]||''} ${esc(c.method)}</td></tr>`).join('')}</table></div>
 <div class="card"><h2>주문 내역</h2><table><tr><th>시간</th><th>유저</th><th>제품</th><th>수량</th><th>금액</th></tr>${S.orders.map(o=>`<tr><td>${dt(o.created_at)}</td><td>${o.user_id}</td><td>${esc(o.product_name)} (${esc(o.kind)})</td><td>${o.qty}</td><td>${won(o.total)}</td></tr>`).join('')}</table></div>`},
auto(){const g=S.guild,allowed=new Set(g.pb_packages.split(/\s+/));
 return `<div class="card"><h2>Pushbullet 연결</h2><p class="muted">상태: ${esc(g.pb_status)} · 토큰 ${g.pb_token_set?'등록됨':'미등록'}</p>
 <label>Access Token (pushbullet.com → Settings → Account → Create Access Token)</label><input id="tok" type="password" placeholder="o.xxxxxxxx" autocomplete="off">
 <button onclick="act('pb_token',{token:val('tok')})">저장 후 연결</button> <button class="r" onclick="act('pb_token',{token:''})">연결 끊기</button></div>
 <div class="card"><h2>최근 입금 알림</h2><p class="muted">입금으로 보이는 알림만 기록됩니다. 내 은행 앱이 '허용되지 않은 앱'으로 나오면 허용을 눌러주세요. 문자·카톡 같은 메신저 앱은 남이 가짜 입금 문구를 보낼 수 있으니 허용하지 마세요.</p>
 <table><tr><th>시간</th><th>앱</th><th>내용</th><th>결과</th></tr>${S.notifs.map(n=>`<tr><td>${dt(n.created_at)}</td><td>${esc(n.app)}<div class="muted">${esc(n.package)}</div></td><td>${esc(n.text)}</td>
 <td>${esc(n.result)}${allowed.has(n.package)?'':`<br><button onclick="act('allow_package',{package:'${esc(n.package)}'})">이 앱 허용</button>`}</td></tr>`).join('')}</table></div>`},
settings(){const g=S.guild;return `<div class="card"><h2>상점 설정</h2>
 <label>상점 이름</label><input id="s_name" value="${esc(g.shop_name)}"><label>공지사항</label><textarea id="s_notice">${esc(g.notice)}</textarea>
 <label>이벤트</label><textarea id="s_event">${esc(g.event)}</textarea><label>입금 계좌 안내</label><textarea id="s_bank">${esc(g.bank_info)}</textarea>
 <label>배너 이미지 URL (https)</label><input id="s_banner" value="${esc(g.banner_url)}">
 <label>자동충전 허용 앱 패키지 (한 줄에 1개)</label><textarea id="s_pk">${esc(g.pb_packages)}</textarea>
 <button onclick="act('settings',{shop_name:val('s_name'),notice:val('s_notice'),event:val('s_event'),bank_info:val('s_bank'),banner_url:val('s_banner'),pb_packages:val('s_pk')})">저장</button></div>`}};
function addStock(id){document.getElementById('stockbox').innerHTML=`<h2 style="margin-top:16px">재고 추가 (한 줄에 1개)</h2><textarea id="st"></textarea><button onclick="act('stock_add',{id:${id},text:val('st')})">넣기</button>`}
function editP(id){const p=S.products.find(x=>x.id==id),n=prompt('이름',p.name);if(n===null)return;const pr=prompt('가격',p.price);if(pr===null)return;const d=prompt('설명',p.description);if(d===null)return;act('product_save',{id,name:n,price:pr,description:d})}
function bal(uid){const d=prompt('더할 금액 (빼려면 앞에 -)');if(d&&!isNaN(parseInt(d)))act('balance',{user_id:uid,delta:parseInt(d)})}
load();
</script></body></html>"""

if __name__ == "__main__":
    bot.run(TOKEN)
