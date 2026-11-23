from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import utils


class DatabaseError(Exception):
    """Base error for shop operations."""


class InsufficientBalance(DatabaseError):
    pass


class OutOfStock(DatabaseError):
    pass


class ProductUnavailable(DatabaseError):
    pass


class LicenseError(DatabaseError):
    pass


SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_GUILD_FIELDS = {
    "name",
    "log_channel_id",
    "panel_channel_id",
    "admin_role_id",
    "notice",
    "event_text",
    "auto_charge",
    "buy_enabled",
    "license_key",
    "license_expires_at",
}

_PRODUCT_FIELDS = {
    "name",
    "description",
    "price",
    "delivery_type",
    "role_id",
    "stock",
    "active",
    "sort_order",
}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.init_schema()

    # ------------------------------------------------------------------ core
    def init_schema(self) -> None:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self.conn.executescript(sql)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ----------------------------------------------------------------- guilds
    def ensure_guild(self, guild_id: str | int, name: str = "") -> dict:
        guild_id = str(guild_id)
        ts = utils.now()
        self._exec(
            """INSERT INTO guilds (id, name, registered_at, created_at, auto_charge)
               VALUES (?, ?, 0, ?, 1)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name""",
            (guild_id, name, ts),
        )
        return self.get_guild(guild_id)  # type: ignore[return-value]

    def get_guild(self, guild_id: str | int) -> dict | None:
        return self._one("SELECT * FROM guilds WHERE id = ?", (str(guild_id),))

    def list_guilds(self) -> list[dict]:
        return self._all("SELECT * FROM guilds ORDER BY created_at ASC")

    def register_guild(self, guild_id: str | int, name: str, license_key: str | None, expires_at: int | None) -> dict:
        guild_id = str(guild_id)
        ts = utils.now()
        with self._tx() as conn:
            row = conn.execute("SELECT id, registered_at FROM guilds WHERE id = ?", (guild_id,)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO guilds (id, name, registered_at, license_key, license_expires_at, created_at, auto_charge)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (guild_id, name, ts, license_key, expires_at, ts),
                )
            else:
                conn.execute(
                    """UPDATE guilds SET name = ?, registered_at = CASE WHEN registered_at = 0 THEN ? ELSE registered_at END,
                       license_key = COALESCE(?, license_key), license_expires_at = COALESCE(?, license_expires_at) WHERE id = ?""",
                    (name, ts, license_key, expires_at, guild_id),
                )
            conn.execute(
                "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, "system", "guild.register", f"key={license_key or 'owner'}", ts),
            )
        return self.get_guild(guild_id)  # type: ignore[return-value]

    def update_guild(self, guild_id: str | int, **fields: Any) -> dict:
        clean = {k: v for k, v in fields.items() if k in _GUILD_FIELDS}
        if not clean:
            return self.get_guild(guild_id)  # type: ignore[return-value]
        assignments = ", ".join(f"{key} = ?" for key in clean)
        params = list(clean.values()) + [str(guild_id)]
        self._exec(f"UPDATE guilds SET {assignments} WHERE id = ?", params)
        return self.get_guild(guild_id)  # type: ignore[return-value]

    def license_valid(self, guild: dict | None) -> bool:
        if guild is None:
            return False
        if not guild.get("license_expires_at"):
            return False
        return int(guild["license_expires_at"]) > utils.now()

    def plan_key_for_guild(self, guild: dict | None) -> str:
        """서버에 적용된 라이센스의 플랜 키를 반환한다."""
        if guild is None:
            return "basic"
        key = (guild.get("license_key") or "").strip().upper()
        if not key:
            return "basic"
        if key == "OWNER":
            return "platinum"
        license_row = self.get_license(key)
        if license_row is None or int(license_row.get("revoked") or 0):
            return "basic"
        return str(license_row.get("plan") or "standard")

    # ------------------------------------------------------------------ users
    def ensure_user(self, guild_id: str | int, discord_id: str | int) -> dict:
        guild_id, discord_id = str(guild_id), str(discord_id)
        self._exec(
            "INSERT OR IGNORE INTO users (guild_id, discord_id, balance, total_spent, created_at) VALUES (?, ?, 0, 0, ?)",
            (guild_id, discord_id, utils.now()),
        )
        return self.get_user(guild_id, discord_id)  # type: ignore[return-value]

    def get_user(self, guild_id: str | int, discord_id: str | int) -> dict | None:
        return self._one(
            "SELECT * FROM users WHERE guild_id = ? AND discord_id = ?", (str(guild_id), str(discord_id))
        )

    def add_balance(self, guild_id: str | int, discord_id: str | int, delta: int) -> int:
        guild_id, discord_id, delta = str(guild_id), str(discord_id), int(delta)
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (guild_id, discord_id, balance, total_spent, created_at) VALUES (?, ?, 0, 0, ?)",
                (guild_id, discord_id, utils.now()),
            )
            cur = conn.execute(
                "UPDATE users SET balance = balance + ? WHERE guild_id = ? AND discord_id = ? AND balance + ? >= 0",
                (delta, guild_id, discord_id, delta),
            )
            if cur.rowcount == 0:
                raise InsufficientBalance("잔액이 부족합니다.")
            row = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
            ).fetchone()
        return int(row["balance"])

    def list_users(self, guild_id: str | int, limit: int = 100, offset: int = 0, search: str = "") -> list[dict]:
        params: list[Any] = [str(guild_id)]
        clause = ""
        if search:
            clause = " AND discord_id LIKE ?"
            params.append(f"%{search.strip()}%")
        params.extend([limit, offset])
        return self._all(
            f"SELECT * FROM users WHERE guild_id = ?{clause} ORDER BY balance DESC, created_at ASC LIMIT ? OFFSET ?",
            params,
        )

    # --------------------------------------------------------------- products
    def _product_select(self) -> str:
        return """SELECT p.*,
                  (SELECT COUNT(*) FROM product_stock s WHERE s.product_id = p.id AND s.used = 0) AS stock_left
                  FROM products p"""

    def list_products(self, guild_id: str | int, active_only: bool = True) -> list[dict]:
        clause = " AND p.active = 1" if active_only else ""
        return self._all(
            f"{self._product_select()} WHERE p.guild_id = ?{clause} ORDER BY p.sort_order ASC, p.id ASC",
            (str(guild_id),),
        )

    def get_product(self, product_id: int, guild_id: str | int | None = None) -> dict | None:
        if guild_id is None:
            return self._one(f"{self._product_select()} WHERE p.id = ?", (int(product_id),))
        return self._one(
            f"{self._product_select()} WHERE p.id = ? AND p.guild_id = ?", (int(product_id), str(guild_id))
        )

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
        if delivery_type not in {"role", "content", "manual"}:
            raise ProductUnavailable("delivery_type 은 role/content/manual 중 하나여야 합니다.")
        cur = self._exec(
            """INSERT INTO products (guild_id, name, description, price, delivery_type, role_id, stock, active, sort_order, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (str(guild_id), name, description, int(price), delivery_type, role_id, int(stock), int(sort_order), utils.now()),
        )
        return self.get_product(int(cur.lastrowid))  # type: ignore[return-value]

    def update_product(self, product_id: int, guild_id: str | int, **fields: Any) -> dict | None:
        clean = {k: v for k, v in fields.items() if k in _PRODUCT_FIELDS}
        if not clean:
            return self.get_product(product_id, guild_id)
        assignments = ", ".join(f"{key} = ?" for key in clean)
        self._exec(
            f"UPDATE products SET {assignments} WHERE id = ? AND guild_id = ?",
            list(clean.values()) + [int(product_id), str(guild_id)],
        )
        return self.get_product(product_id, guild_id)

    def delete_product(self, product_id: int, guild_id: str | int) -> bool:
        cur = self._exec("DELETE FROM products WHERE id = ? AND guild_id = ?", (int(product_id), str(guild_id)))
        return cur.rowcount > 0

    def add_stock_lines(self, product_id: int, lines: Iterable[str]) -> int:
        cleaned = [str(line).strip() for line in lines if str(line).strip()]
        if not cleaned:
            return 0
        ts = utils.now()
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO product_stock (product_id, content, used, created_at) VALUES (?, ?, 0, ?)",
                [(int(product_id), line, ts) for line in cleaned],
            )
        return len(cleaned)

    def list_stock(self, product_id: int, include_used: bool = False) -> list[dict]:
        clause = "" if include_used else " AND used = 0"
        return self._all(
            f"SELECT * FROM product_stock WHERE product_id = ?{clause} ORDER BY id ASC", (int(product_id),)
        )

    # -------------------------------------------------------------- purchases
    def purchase(
        self,
        guild_id: str | int,
        discord_id: str | int,
        product_id: int,
        quantity: int = 1,
        channel: str = "discord",
    ) -> dict:
        quantity = max(1, int(quantity))
        guild_id, discord_id = str(guild_id), str(discord_id)
        ts = utils.now()
        with self._tx() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id = ? AND guild_id = ? AND active = 1",
                (int(product_id), guild_id),
            ).fetchone()
            if product is None:
                raise ProductUnavailable("판매 중인 상품이 아닙니다.")

            if product["delivery_type"] == "role" and quantity != 1:
                raise ProductUnavailable("역할 상품은 1개만 구매할 수 있습니다.")

            stock_rows: list[sqlite3.Row] = []
            if product["delivery_type"] == "content":
                stock_rows = conn.execute(
                    "SELECT id, content FROM product_stock WHERE product_id = ? AND used = 0 ORDER BY id LIMIT ?",
                    (int(product_id), quantity),
                ).fetchall()
                if len(stock_rows) < quantity:
                    raise OutOfStock("재고가 부족합니다.")
            elif int(product["stock"]) >= 0 and int(product["stock"]) < quantity:
                raise OutOfStock("재고가 부족합니다.")

            user = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
            ).fetchone()
            if user is None:
                conn.execute(
                    "INSERT INTO users (guild_id, discord_id, balance, total_spent, created_at) VALUES (?, ?, 0, 0, ?)",
                    (guild_id, discord_id, ts),
                )
                balance = 0
            else:
                balance = int(user["balance"])

            total = int(product["price"]) * quantity
            if balance < total:
                raise InsufficientBalance(f"잔액이 부족합니다. (보유 {utils.fmt_won(balance)} / 필요 {utils.fmt_won(total)})")

            conn.execute(
                "UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE guild_id = ? AND discord_id = ?",
                (total, total, guild_id, discord_id),
            )
            if product["delivery_type"] != "content" and int(product["stock"]) >= 0:
                conn.execute(
                    "UPDATE products SET stock = stock - ? WHERE id = ?", (quantity, int(product_id))
                )

            cur = conn.execute(
                """INSERT INTO purchases
                   (guild_id, discord_id, product_id, product_name, unit_price, quantity, total, status, delivery_type, channel, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
                (
                    guild_id,
                    discord_id,
                    int(product_id),
                    product["name"],
                    int(product["price"]),
                    quantity,
                    total,
                    product["delivery_type"],
                    channel,
                    ts,
                ),
            )
            purchase_id = int(cur.lastrowid)

            contents: list[str] = []
            for row in stock_rows:
                conn.execute(
                    "UPDATE product_stock SET used = 1, used_by = ?, purchase_id = ?, used_at = ? WHERE id = ?",
                    (discord_id, purchase_id, ts, int(row["id"])),
                )
                contents.append(str(row["content"]))

            conn.execute(
                "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, discord_id, "purchase.create", f"product={product['name']} qty={quantity} total={total}", ts),
            )

        return {
            "purchase_id": purchase_id,
            "product": dict(product),
            "quantity": quantity,
            "total": total,
            "balance": balance - total,
            "contents": contents,
            "delivery_type": product["delivery_type"],
            "role_id": product["role_id"],
        }

    def grant_purchase(
        self, guild_id: str | int, discord_id: str | int, product_id: int, quantity: int = 1, actor: str = "admin"
    ) -> dict:
        """잔액 차감 없이 상품을 지급한다. (/제품전송)"""
        quantity = max(1, int(quantity))
        guild_id, discord_id = str(guild_id), str(discord_id)
        ts = utils.now()
        with self._tx() as conn:
            product = conn.execute(
                "SELECT * FROM products WHERE id = ? AND guild_id = ?", (int(product_id), guild_id)
            ).fetchone()
            if product is None:
                raise ProductUnavailable("상품을 찾을 수 없습니다.")
            if product["delivery_type"] == "role" and quantity != 1:
                raise ProductUnavailable("역할 상품은 1개만 지급할 수 있습니다.")
            stock_rows: list[sqlite3.Row] = []
            if product["delivery_type"] == "content":
                stock_rows = conn.execute(
                    "SELECT id, content FROM product_stock WHERE product_id = ? AND used = 0 ORDER BY id LIMIT ?",
                    (int(product_id), quantity),
                ).fetchall()
                if len(stock_rows) < quantity:
                    raise OutOfStock("재고가 부족합니다.")

            cur = conn.execute(
                """INSERT INTO purchases
                   (guild_id, discord_id, product_id, product_name, unit_price, quantity, total, status, delivery_type, channel, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, 'gift', ?, 'admin', ?)""",
                (guild_id, discord_id, int(product_id), product["name"], int(product["price"]), quantity, product["delivery_type"], ts),
            )
            purchase_id = int(cur.lastrowid)
            contents: list[str] = []
            for row in stock_rows:
                conn.execute(
                    "UPDATE product_stock SET used = 1, used_by = ?, purchase_id = ?, used_at = ? WHERE id = ?",
                    (discord_id, purchase_id, ts, int(row["id"])),
                )
                contents.append(str(row["content"]))
            conn.execute(
                "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, actor, "purchase.grant", f"product={product['name']} qty={quantity} user={discord_id}", ts),
            )
        return {
            "purchase_id": purchase_id,
            "product": dict(product),
            "quantity": quantity,
            "total": 0,
            "balance": int((self.get_user(guild_id, discord_id) or {}).get("balance") or 0),
            "contents": contents,
            "delivery_type": product["delivery_type"],
            "role_id": product["role_id"],
        }

    def mark_purchase_delivered(self, purchase_id: int, delivery: str) -> None:
        self._exec("UPDATE purchases SET delivery = ? WHERE id = ?", (delivery, int(purchase_id)))

    def refund_purchase(self, purchase_id: int, actor: str = "admin") -> dict:
        ts = utils.now()
        with self._tx() as conn:
            purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (int(purchase_id),)).fetchone()
            if purchase is None:
                raise DatabaseError("구매 내역을 찾을 수 없습니다.")
            if purchase["status"] != "completed":
                raise DatabaseError("이미 환불되었거나 완료되지 않은 구매입니다.")
            conn.execute(
                "UPDATE users SET balance = balance + ?, total_spent = MAX(0, total_spent - ?) WHERE guild_id = ? AND discord_id = ?",
                (purchase["total"], purchase["total"], purchase["guild_id"], purchase["discord_id"]),
            )
            if purchase["delivery_type"] == "content":
                conn.execute(
                    "UPDATE product_stock SET used = 0, used_by = NULL, purchase_id = NULL, used_at = NULL WHERE purchase_id = ?",
                    (int(purchase_id),),
                )
            elif purchase["delivery_type"] != "content" and purchase["product_id"]:
                conn.execute(
                    "UPDATE products SET stock = stock + ? WHERE id = ? AND stock >= 0",
                    (purchase["quantity"], purchase["product_id"]),
                )
            conn.execute("UPDATE purchases SET status = 'refunded' WHERE id = ?", (int(purchase_id),))
            conn.execute(
                "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (purchase["guild_id"], actor, "purchase.refund", f"purchase={purchase_id} total={purchase['total']}", ts),
            )
        return dict(purchase)

    def get_purchase(self, purchase_id: int) -> dict | None:
        return self._one("SELECT * FROM purchases WHERE id = ?", (int(purchase_id),))

    def stock_for_purchase(self, purchase_id: int) -> list[str]:
        rows = self._all(
            "SELECT content FROM product_stock WHERE purchase_id = ? ORDER BY id ASC", (int(purchase_id),)
        )
        return [str(row["content"]) for row in rows]

    def list_purchases(
        self, guild_id: str | int, discord_id: str | int | None = None, limit: int = 50
    ) -> list[dict]:
        params: list[Any] = [str(guild_id)]
        clause = ""
        if discord_id is not None:
            clause = " AND discord_id = ?"
            params.append(str(discord_id))
        params.append(limit)
        return self._all(
            f"SELECT * FROM purchases WHERE guild_id = ?{clause} ORDER BY created_at DESC LIMIT ?", params
        )

    # --------------------------------------------------------------- deposits
    def expire_pending(self) -> int:
        cur = self._exec(
            "UPDATE pending_deposits SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?",
            (utils.now(),),
        )
        return cur.rowcount

    def get_active_pending(self, guild_id: str | int, discord_id: str | int) -> dict | None:
        self.expire_pending()
        return self._one(
            """SELECT * FROM pending_deposits
               WHERE guild_id = ? AND discord_id = ? AND status = 'pending' AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (str(guild_id), str(discord_id), utils.now()),
        )

    def create_pending_deposit(
        self, guild_id: str | int, discord_id: str | int, amount: int, depositor: str, ttl_hours: int = 6
    ) -> dict:
        guild_id, discord_id = str(guild_id), str(discord_id)
        amount = int(amount)
        if amount <= 0:
            raise DatabaseError("입금 금액은 1원 이상이어야 합니다.")
        self.expire_pending()

        existing = self._one(
            """SELECT * FROM pending_deposits
               WHERE guild_id = ? AND discord_id = ? AND amount = ? AND depositor = ? AND status = 'pending'
                     AND expires_at > ? ORDER BY id DESC LIMIT 1""",
            (guild_id, discord_id, amount, depositor, utils.now()),
        )
        if existing:
            return existing

        ts = utils.now()
        expires = ts + max(1, int(ttl_hours)) * 3600
        for _ in range(10):
            code = utils.gen_deposit_code()
            try:
                cur = self._exec(
                    """INSERT INTO pending_deposits (guild_id, discord_id, amount, depositor, code, status, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (guild_id, discord_id, amount, depositor, code, ts, expires),
                )
                return self._one("SELECT * FROM pending_deposits WHERE id = ?", (int(cur.lastrowid),))  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                continue
        raise DatabaseError("입금 코드 생성에 실패했습니다. 다시 시도해 주세요.")

    def set_pending_ttl(self, pending_id: int, hours: int) -> None:
        self._exec(
            "UPDATE pending_deposits SET expires_at = created_at + ? WHERE id = ?", (int(hours) * 3600, int(pending_id))
        )

    def pending_for_amount(self, amount: int) -> list[dict]:
        self.expire_pending()
        return self._all(
            """SELECT * FROM pending_deposits
               WHERE amount = ? AND status = 'pending' AND expires_at > ? ORDER BY created_at ASC""",
            (int(amount), utils.now()),
        )

    def list_pending(self, guild_id: str | int, include_all: bool = False) -> list[dict]:
        self.expire_pending()
        clause = "" if include_all else " AND status = 'pending' AND expires_at > ?"
        params: list[Any] = [str(guild_id)]
        if not include_all:
            params.append(utils.now())
        return self._all(
            f"SELECT * FROM pending_deposits WHERE guild_id = ?{clause} ORDER BY created_at DESC LIMIT 100", params
        )

    def get_pending(self, pending_id: int) -> dict | None:
        return self._one("SELECT * FROM pending_deposits WHERE id = ?", (int(pending_id),))

    def cancel_pending(self, pending_id: int, guild_id: str | int) -> bool:
        cur = self._exec(
            "UPDATE pending_deposits SET status = 'cancelled' WHERE id = ? AND guild_id = ? AND status = 'pending'",
            (int(pending_id), str(guild_id)),
        )
        return cur.rowcount > 0

    def deposit_exists(self, push_iden: str) -> bool:
        return self._one("SELECT id FROM deposits WHERE push_iden = ?", (push_iden,)) is not None

    def record_deposit(
        self,
        *,
        push_iden: str | None,
        source: str,
        application: str | None,
        title: str | None,
        body: str | None,
        amount: int | None,
        depositor: str | None,
        status: str = "unmatched",
        note: str | None = None,
    ) -> dict | None:
        ts = utils.now()
        try:
            cur = self._exec(
                """INSERT INTO deposits
                   (push_iden, source, application, title, body, amount, depositor, status, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (push_iden, source, application, title, body, amount, depositor, status, note, ts),
            )
        except sqlite3.IntegrityError:
            return None
        return self._one("SELECT * FROM deposits WHERE id = ?", (int(cur.lastrowid),))

    def get_deposit(self, deposit_id: int) -> dict | None:
        return self._one("SELECT * FROM deposits WHERE id = ?", (int(deposit_id),))

    def list_deposits(self, guild_id: str | int | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        params: list[Any] = []
        clauses: list[str] = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(str(guild_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self._all(f"SELECT * FROM deposits{where} ORDER BY created_at DESC LIMIT ?", params)

    def mark_deposit(self, deposit_id: int, status: str, note: str | None = None) -> None:
        self._exec("UPDATE deposits SET status = ?, note = COALESCE(?, note) WHERE id = ?", (status, note, int(deposit_id)))

    def approve_deposit(
        self,
        deposit_id: int,
        guild_id: str | int,
        discord_id: str | int,
        actor: str = "admin",
        note: str | None = None,
    ) -> dict:
        guild_id, discord_id = str(guild_id), str(discord_id)
        ts = utils.now()
        with self._tx() as conn:
            deposit = conn.execute("SELECT * FROM deposits WHERE id = ?", (int(deposit_id),)).fetchone()
            if deposit is None:
                raise DatabaseError("입금 내역을 찾을 수 없습니다.")
            if deposit["status"] == "matched" and deposit["discord_id"]:
                return {"deposit": dict(deposit), "already": True}
            amount = int(deposit["amount"] or 0)
            if amount <= 0:
                raise DatabaseError("입금 금액이 올바르지 않습니다.")

            conn.execute(
                "INSERT OR IGNORE INTO users (guild_id, discord_id, balance, total_spent, created_at) VALUES (?, ?, 0, 0, ?)",
                (guild_id, discord_id, ts),
            )
            conn.execute(
                "UPDATE users SET balance = balance + ? WHERE guild_id = ? AND discord_id = ?",
                (amount, guild_id, discord_id),
            )
            conn.execute(
                """UPDATE deposits SET status = 'matched', guild_id = ?, discord_id = ?, note = COALESCE(?, note)
                   WHERE id = ?""",
                (guild_id, discord_id, note, int(deposit_id)),
            )
            if deposit["pending_id"]:
                conn.execute(
                    "UPDATE pending_deposits SET status = 'matched', matched_deposit_id = ? WHERE id = ?",
                    (int(deposit_id), int(deposit["pending_id"])),
                )
            conn.execute(
                "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, actor, "deposit.approve", f"deposit={deposit_id} amount={amount} user={discord_id}", ts),
            )
            balance = conn.execute(
                "SELECT balance FROM users WHERE guild_id = ? AND discord_id = ?", (guild_id, discord_id)
            ).fetchone()["balance"]

        result = dict(deposit)
        result.update({"status": "matched", "guild_id": guild_id, "discord_id": discord_id, "balance": int(balance)})
        return {"deposit": result, "already": False}

    def attach_pending(self, deposit_id: int, pending_id: int) -> None:
        self._exec("UPDATE deposits SET pending_id = ? WHERE id = ?", (int(pending_id), int(deposit_id)))

    # --------------------------------------------------------------- licenses
    def create_license(
        self,
        key: str,
        plan: str,
        duration_days: int,
        max_guilds: int,
        created_by: str,
        note: str | None = None,
    ) -> dict:
        self._exec(
            """INSERT INTO licenses (key, plan, duration_days, max_guilds, created_by, created_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, plan, int(duration_days), int(max_guilds), str(created_by), utils.now(), note),
        )
        return self.get_license(key)  # type: ignore[return-value]

    def get_license(self, key: str) -> dict | None:
        return self._one("SELECT * FROM licenses WHERE key = ?", (key.strip().upper(),))

    def redeem_license(self, key: str, guild_id: str | int, user_id: str | int) -> dict:
        key = key.strip().upper()
        guild_id = str(guild_id)
        ts = utils.now()
        with self._tx() as conn:
            lic = conn.execute("SELECT * FROM licenses WHERE key = ?", (key,)).fetchone()
            if lic is None:
                raise LicenseError("존재하지 않는 라이센스 키입니다.")
            if int(lic["revoked"]):
                raise LicenseError("폐기된 라이센스 키입니다.")
            if lic["redeemed_by"] and str(lic["redeemed_by"]) != guild_id:
                raise LicenseError("이미 다른 서버에서 사용된 라이센스 키입니다.")
            if lic["redeemed_by"] and str(lic["redeemed_by"]) == guild_id and lic["expires_at"]:
                raise LicenseError("이미 이 서버에 적용된 라이센스 키입니다.")

            guild = conn.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,)).fetchone()
            base = ts
            if guild and guild["license_expires_at"]:
                base = max(ts, int(guild["license_expires_at"]))
            expires = base + int(lic["duration_days"]) * 86400

            conn.execute(
                """UPDATE licenses SET redeemed_by = ?, redeemed_by_user = ?, redeemed_at = ?, expires_at = ?
                   WHERE key = ?""",
                (guild_id, str(user_id), ts, expires, key),
            )
            if guild is None:
                conn.execute(
                    """INSERT INTO guilds (id, name, registered_at, license_key, license_expires_at, created_at, auto_charge)
                       VALUES (?, '', ?, ?, ?, ?, 1)""",
                    (guild_id, ts, key, expires, ts),
                )
            else:
                conn.execute(
                    """UPDATE guilds SET registered_at = CASE WHEN registered_at = 0 THEN ? ELSE registered_at END,
                       license_key = ?, license_expires_at = ? WHERE id = ?""",
                    (ts, key, expires, guild_id),
                )
            conn.execute(
                "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, str(user_id), "license.redeem", f"key={key} expires={expires}", ts),
            )
        return {
            "key": key,
            "plan": lic["plan"],
            "duration_days": int(lic["duration_days"]),
            "expires_at": expires,
            "extended": base > ts,
        }

    def list_licenses(self, limit: int = 200, search: str = "") -> list[dict]:
        params: list[Any] = []
        clause = ""
        if search:
            clause = " WHERE key LIKE ? OR redeemed_by LIKE ? OR note LIKE ?"
            like = f"%{search.strip()}%"
            params = [like, like, like]
        params.append(limit)
        return self._all(f"SELECT * FROM licenses{clause} ORDER BY created_at DESC LIMIT ?", params)

    def revoke_license(self, key: str) -> bool:
        cur = self._exec("UPDATE licenses SET revoked = 1 WHERE key = ?", (key.strip().upper(),))
        return cur.rowcount > 0

    def license_summary(self) -> dict:
        row = self._one(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN redeemed_by IS NOT NULL THEN 1 ELSE 0 END) AS used,
                      SUM(CASE WHEN revoked = 1 THEN 1 ELSE 0 END) AS revoked
               FROM licenses"""
        ) or {}
        active = self._one(
            "SELECT COUNT(*) AS c FROM guilds WHERE license_expires_at > ?", (utils.now(),)
        )["c"]
        return {
            "total": int(row.get("total") or 0),
            "used": int(row.get("used") or 0),
            "revoked": int(row.get("revoked") or 0),
            "active_guilds": int(active or 0),
        }

    # --------------------------------------------------------------- sessions
    def create_login_token(
        self, token_hash: str, guild_id: str | int, discord_id: str | int, is_admin: bool, ttl_seconds: int
    ) -> None:
        ts = utils.now()
        self._exec(
            """INSERT INTO login_tokens (token_hash, guild_id, discord_id, is_admin, created_at, expires_at, used)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (token_hash, str(guild_id), str(discord_id), int(is_admin), ts, ts + int(ttl_seconds)),
        )

    def consume_login_token(self, token_hash: str) -> dict | None:
        ts = utils.now()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE login_tokens SET used = 1 WHERE token_hash = ? AND used = 0 AND expires_at > ?",
                (token_hash, ts),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM login_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def create_session(
        self,
        token_hash: str,
        guild_id: str | int,
        discord_id: str | int,
        is_admin: bool,
        is_owner: bool,
        ttl_seconds: int,
    ) -> None:
        ts = utils.now()
        self._exec(
            """INSERT OR REPLACE INTO web_sessions
               (token_hash, guild_id, discord_id, is_admin, is_owner, created_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (token_hash, str(guild_id), str(discord_id), int(is_admin), int(is_owner), ts, ts + int(ttl_seconds)),
        )

    def get_session(self, token_hash: str) -> dict | None:
        return self._one(
            """SELECT s.*, g.name AS guild_name FROM web_sessions s
               LEFT JOIN guilds g ON g.id = s.guild_id
               WHERE s.token_hash = ? AND s.expires_at > ?""",
            (token_hash, utils.now()),
        )

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def cleanup(self) -> None:
        ts = utils.now()
        self._exec("DELETE FROM web_sessions WHERE expires_at <= ?", (ts,))
        self._exec("DELETE FROM login_tokens WHERE expires_at <= ? OR used = 1", (ts,))
        self.expire_pending()

    # ------------------------------------------------------------------ audit
    def audit(self, guild_id: str | int | None, actor: str, action: str, detail: str = "") -> None:
        self._exec(
            "INSERT INTO audit_log (guild_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(guild_id) if guild_id is not None else None, str(actor), action, detail[:1000], utils.now()),
        )

    def list_audit(self, guild_id: str | int, limit: int = 50) -> list[dict]:
        return self._all(
            "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?", (str(guild_id), limit)
        )

    # ------------------------------------------------------------------ stats
    def guild_stats(self, guild_id: str | int) -> dict:
        guild_id = str(guild_id)
        start = utils.start_of_today_kst()
        row = self._one(
            """SELECT
                 (SELECT COUNT(*) FROM users WHERE guild_id = ?) AS members,
                 (SELECT COUNT(*) FROM products WHERE guild_id = ? AND active = 1) AS products,
                 (SELECT COALESCE(SUM(total), 0) FROM purchases WHERE guild_id = ? AND status = 'completed' AND created_at >= ?) AS today_sales,
                 (SELECT COUNT(*) FROM purchases WHERE guild_id = ? AND status = 'completed' AND created_at >= ?) AS today_orders,
                 (SELECT COALESCE(SUM(balance), 0) FROM users WHERE guild_id = ?) AS total_balance,
                 (SELECT COUNT(*) FROM pending_deposits WHERE guild_id = ? AND status = 'pending' AND expires_at > ?) AS pending_deposits,
                 (SELECT COALESCE(SUM(total), 0) FROM purchases WHERE guild_id = ? AND status = 'completed') AS total_sales
            """,
            (
                guild_id,
                guild_id,
                guild_id,
                start,
                guild_id,
                start,
                guild_id,
                guild_id,
                utils.now(),
                guild_id,
            ),
        )
        return {k: int(v or 0) for k, v in (row or {}).items()}

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


_db: Database | None = None


def get_db(path: str | Path) -> Database:
    global _db
    if _db is None:
        _db = Database(path)
    return _db
