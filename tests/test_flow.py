from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import ShopService  # noqa: E402
from app.utils import fmt_won  # noqa: E402


def make_shop(tmp_path: Path):
    settings = replace(
        load_settings(),
        license_enforce=True,
        auto_charge_enabled=True,
        deposit_ttl_hours=6,
        bank_name="KB국민은행",
        bank_account="123456-78-901234",
        bank_holder="홍길동",
        owner_ids=frozenset({1}),
    )
    db = Database(tmp_path / "test.db")
    service = ShopService(db, settings, bot=None)
    key = service.issue_licenses(1, 30, "standard", "1")[0]
    service.redeem_license("111", "999", key, "테스트 서버")
    return db, service, settings


def test_license_and_purchase(tmp_path):
    db, service, _ = make_shop(tmp_path)

    guild = db.get_guild("111")
    assert guild and db.license_valid(guild)

    product = db.create_product("111", "테스트 코드", 1000, "상품 설명", delivery_type="content")
    db.add_stock_lines(product["id"], ["CODE-AAA", "CODE-BBB"])

    balance = asyncio.run(service.charge("111", "222", 5000, "admin"))
    assert balance == 5000

    result = asyncio.run(service.purchase("111", "222", product["id"], 1))
    assert result["total"] == 1000
    assert result["balance"] == 4000
    assert result["contents"] == ["CODE-AAA"]
    assert db.get_user("111", "222")["balance"] == 4000

    remaining = db.list_stock(product["id"])
    assert len(remaining) == 1

    try:
        asyncio.run(service.purchase("111", "222", product["id"], 10))
    except Exception as exc:
        assert "잔액" in str(exc) or "재고" in str(exc)
    else:
        raise AssertionError("expected purchase failure")

    before = db.get_user("111", "222")["balance"]
    gift = asyncio.run(service.send_product("111", "333", product["id"], 1, actor="admin"))
    assert gift["purchase_id"] > 0
    assert db.get_user("111", "222")["balance"] == before


def test_deposit_auto_charge(tmp_path):
    db, service, settings = make_shop(tmp_path)
    asyncio.run(service.charge("111", "222", 4000, "admin"))
    pending = service.create_deposit_request("111", "222", 10000, "홍길동")
    assert pending["code"].startswith("W")

    push = {
        "iden": "push-test-1",
        "type": "mirror",
        "package_name": "com.kbstar.kbbank",
        "application_name": "KB국민은행",
        "title": "KB국민은행",
        "body": "12/22 15:04 123456-78-901234 입금 10,000원 홍길동 잔액 1,234,567원",
    }
    outcome = asyncio.run(service.processor.process_push(push))
    assert outcome.status == "matched", outcome.message
    assert outcome.amount == 10000
    assert db.get_user("111", "222")["balance"] == 14000

    duplicate = asyncio.run(service.processor.process_push(push))
    assert duplicate.status == "duplicate"
    assert db.get_user("111", "222")["balance"] == 14000

    pending2 = service.create_deposit_request("111", "333", 7000, "김철수")
    assert pending2["status"] == "pending"
    push2 = dict(push, iden="push-test-2", body="입금 7,000원 박영수 잔액 100,000원")
    outcome2 = asyncio.run(service.processor.process_push(push2))
    assert outcome2.status == "unmatched", outcome2.message
    assert (db.get_user("111", "333") or {}).get("balance", 0) == 0

    deposit_id = outcome2.deposit["id"]
    asyncio.run(service.processor.approve_manually(deposit_id, "111", "333", "admin"))
    assert db.get_user("111", "333")["balance"] == 7000


def test_license_enforcement(tmp_path):
    db, service, _ = make_shop(tmp_path)
    db.update_guild("111", license_expires_at=1)
    try:
        service.require_shop("111")
    except Exception as exc:
        assert "라이센스" in str(exc)
    else:
        raise AssertionError("license should be expired")

    key = service.issue_licenses(1, 1, "standard", "1")[0]
    result = service.redeem_license("112", "999", key, "두번째 서버")
    assert result["expires_at"] > 0

    try:
        service.redeem_license("113", "999", key, "세번째 서버")
    except Exception as exc:
        assert "다른 서버" in str(exc)
    else:
        raise AssertionError("license key must be single-use")


def test_money_format():
    assert fmt_won(10000) == "10,000원"
    assert fmt_won(-500) == "-500원"
