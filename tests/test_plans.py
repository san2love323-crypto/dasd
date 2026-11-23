from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.plans import public_catalog, resolve_plan  # noqa: E402
from app.service import ShopError, ShopService  # noqa: E402

OWNER = "1"


def make(tmp_path: Path, plan: str = "standard", guild: str = "111"):
    settings = replace(
        load_settings(),
        owner_ids=frozenset({int(OWNER)}),
        license_enforce=True,
        auto_charge_enabled=True,
    )
    db = Database(tmp_path / f"plan-{plan}-{guild}.db")
    service = ShopService(db, settings)
    key = service.issue_licenses(1, 30, plan, OWNER)[0]
    service.redeem_license(guild, OWNER, key, "테스트 서버")
    return db, service


def test_catalog_order_and_auto_charge_entry():
    catalog = public_catalog()
    assert [item["key"] for item in catalog] == ["basic", "standard", "premium", "platinum"]
    assert catalog[0]["auto_charge"] is False
    # 중간 등급(스탠다드)부터 자동충전 허용
    assert catalog[1]["auto_charge"] is True
    assert catalog[2]["auto_charge"] is True
    assert catalog[3]["auto_charge"] is True
    assert resolve_plan("pro").key == "premium"
    assert resolve_plan("없는플랜").key == "standard"


def test_basic_plan_restrictions(tmp_path):
    db, service = make(tmp_path, "basic")
    assert service.plan_for("111").key == "basic"

    # 자동 충전 불가
    with pytest.raises(ShopError) as exc:
        service.create_deposit_request("111", "222", 10_000, "홍길동")
    assert "스탠다드" in str(exc.value)

    # 상품 코드/역할 전달 불가
    with pytest.raises(ShopError) as exc:
        service.create_product("111", "코드 상품", 1000, delivery_type="content")
    assert "베이직" in str(exc.value)

    # 수동 상품은 10개까지
    for index in range(10):
        service.create_product("111", f"상품 {index}", 1000, delivery_type="manual")
    with pytest.raises(ShopError) as exc:
        service.create_product("111", "초과 상품", 1000, delivery_type="manual")
    assert "한도" in str(exc.value)

    # 상품 코드 한도는 100개
    manual = db.list_products("111", active_only=False)[0]
    service.add_stock(manual["id"], "111", [f"CODE-{i}" for i in range(100)])
    with pytest.raises(ShopError) as exc:
        service.add_stock(manual["id"], "111", ["OVER-1"])
    assert "100" in str(exc.value)


def test_basic_cannot_auto_charge_even_with_matching_push(tmp_path):
    db, service = make(tmp_path, "basic")
    # 플랜을 우회해 DB에 직접 입금 요청을 만든 뒤 알림을 넣어도 자동충전되지 않는다
    db.update_guild("111", auto_charge=1)
    db.create_pending_deposit("111", "222", 10_000, "홍길동", ttl_hours=6)
    push = {
        "type": "mirror",
        "iden": "plan-push-1",
        "package_name": "com.kbstar.kbbank",
        "application_name": "KB국민은행",
        "title": "KB국민은행",
        "body": "입금 10,000원 홍길동 잔액 1,000원",
    }
    outcome = asyncio.run(service.processor.process_push(push))
    assert outcome.status == "review"
    assert "베이직" in outcome.message
    assert (db.get_user("111", "222") or {}).get("balance", 0) == 0


def test_standard_and_premium_features(tmp_path):
    db, service = make(tmp_path, "standard")
    service.create_product("111", "코드 상품", 1000, delivery_type="content")
    # 역할 지급은 프리미엄부터
    with pytest.raises(ShopError):
        service.create_product("111", "역할 상품", 1000, delivery_type="role", role_id="123")
    # 자동충전 가능
    assert service.require_auto_charge("111").key == "standard"

    db2, service2 = make(tmp_path, "premium", guild="112")
    service2.create_product("112", "역할 상품", 1000, delivery_type="role", role_id="123")
    assert service2.plan_for("112").max_products == 100


def test_license_issue_stores_plan_and_limits(tmp_path):
    db, service = make(tmp_path, "standard")
    key = service.issue_licenses(1, 10, "pro", OWNER)[0]
    row = db.get_license(key)
    assert row["plan"] == "premium"
    assert int(row["max_guilds"]) == 2

    result = service.redeem_license("999", OWNER, key, "프리미엄 서버")
    assert result["plan_name"] == "프리미엄"
    assert result["plan_features"]["role_delivery"] is True
    assert service.plan_for("999").key == "premium"


def test_owner_guild_gets_platinum(tmp_path):
    db = Database(tmp_path / "owner.db")
    settings = replace(load_settings(), owner_ids=frozenset({1}))
    service = ShopService(db, settings)
    db.register_guild("777", "오너 서버", "OWNER", 99_999_999_999)
    assert service.plan_for("777").key == "platinum"
    assert service.plan_for("777").max_products == -1
