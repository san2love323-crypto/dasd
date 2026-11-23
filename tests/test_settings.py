from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import ShopError, ShopService  # noqa: E402
from app.settings_store import SettingsError, SettingsStore, mask_secret  # noqa: E402

OWNER = "1"


def make(tmp_path: Path, plan: str = "premium"):
    settings = replace(
        load_settings(),
        owner_ids=frozenset({int(OWNER)}),
        license_enforce=True,
        auto_charge_enabled=True,
        bank_name="기본은행",
        bank_account="111-222-333333",
        bank_holder="기본예금주",
    )
    db = Database(tmp_path / "settings.db")
    service = ShopService(db, settings)
    key = service.issue_licenses(1, 30, plan, OWNER)[0]
    service.redeem_license("111", OWNER, key, "테스트 서버")
    return db, service


def test_global_defaults_and_persist(tmp_path):
    db, service = make(tmp_path)
    store = service.store
    assert store.bank()["name"] == "기본은행"
    assert store.min_deposit() == 100

    store.set_global("bank_name", "신한은행", actor=OWNER)
    store.set_global("bank_account", "110-123-456789", actor=OWNER)
    store.set_global("min_deposit", 5000, actor=OWNER)
    store.set_global("max_deposit", 300000, actor=OWNER)

    # 새 스토어 인스턴스(=DB에서 다시 읽기)에서도 유지
    fresh = SettingsStore(db, service.settings)
    assert fresh.bank()["name"] == "신한은행"
    assert fresh.min_deposit() == 5000
    assert fresh.max_deposit() == 300000


def test_validation_rejects_bad_values(tmp_path):
    _, service = make(tmp_path)
    store = service.store
    with pytest.raises(SettingsError):
        store.set_global("bank_account", "abcdef!")
    with pytest.raises(SettingsError):
        store.set_global("min_deposit", 10)
    with pytest.raises(SettingsError):
        store.set_global("max_deposit", 100)
    with pytest.raises(SettingsError):
        store.set_global("banner_url", "javascript:alert(1)")
    with pytest.raises(SettingsError):
        store.set_global("purchase_max_qty", 50)


def test_secret_masking(tmp_path):
    _, service = make(tmp_path)
    store = service.store
    store.set_global("pushbullet_token", "o.abcdefghijklmnop", actor=OWNER)
    view = store.global_view()
    assert view["pushbullet_token"]["configured"] is True
    assert "abcdefghij" not in view["pushbullet_token"]["hint"]
    assert mask_secret("o.abcdefghijklmnop").startswith("o.ab")
    # 감사 로그에는 원문이 남지 않는다
    audit = service.db.list_audit("111", limit=50)
    assert all("abcdefghij" not in (row["detail"] or "") for row in audit)


def test_guild_override_and_reset(tmp_path):
    _, service = make(tmp_path)
    store = service.store
    store.set_global("min_deposit", 1000, actor=OWNER)
    store.set_guild("111", "min_deposit", 5000, actor=OWNER)
    assert store.min_deposit("111") == 5000
    assert store.min_deposit("999") == 1000  # 다른 서버는 전역값

    view = store.guild_view("111")
    assert view["min_deposit"]["override"] is True

    store.clear_guild("111", "min_deposit", actor=OWNER)
    assert store.min_deposit("111") == 1000
    assert store.guild_view("111")["min_deposit"]["override"] is False


def test_deposit_uses_configured_limits(tmp_path):
    _, service = make(tmp_path)
    store = service.store
    store.set_guild("111", "min_deposit", 5000, actor=OWNER)
    store.set_guild("111", "max_deposit", 50000, actor=OWNER)

    with pytest.raises(ShopError) as exc:
        service.create_deposit_request("111", "222", 1000, "홍길동")
    assert "5,000" in str(exc.value)

    with pytest.raises(ShopError) as exc:
        service.create_deposit_request("111", "222", 60000, "홍길동")
    assert "50,000" in str(exc.value)

    pending = service.create_deposit_request("111", "222", 10000, "홍길동")
    assert pending["amount"] == 10000
    # 만료 시간도 설정값을 따른다
    ttl = store.deposit_ttl("111")
    assert pending["expires_at"] - pending["created_at"] == ttl * 3600


def test_purchase_quantity_limit(tmp_path):
    _, service = make(tmp_path)
    store = service.store
    store.set_guild("111", "purchase_max_qty", 2, actor=OWNER)
    product = service.create_product("111", "테스트", 1000, delivery_type="manual")
    asyncio.run(service.charge("111", "222", 100000, OWNER))

    with pytest.raises(ShopError) as exc:
        asyncio.run(service.purchase("111", "222", product["id"], 3))
    assert "최대 2개" in str(exc.value)

    result = asyncio.run(service.purchase("111", "222", product["id"], 2))
    assert result["quantity"] == 2


def test_auto_charge_master_blocks_deposit(tmp_path):
    _, service = make(tmp_path)
    service.store.set_global("auto_charge_master", False, actor=OWNER)
    with pytest.raises(ShopError):
        service.create_deposit_request("111", "222", 10000, "홍길동")


def test_main_entrypoint_exists():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    assert (root / "main.py").exists()
    assert not (root / "run.py").exists()
    spec = importlib.util.spec_from_file_location("vendrix_main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    assert callable(module.main)
    assert callable(module.check_config)
