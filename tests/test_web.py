from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import aiohttp
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import ShopService  # noqa: E402
from app.web import WebServer  # noqa: E402

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


async def scenario(tmp_path: Path) -> None:
    settings = replace(
        load_settings(),
        web_host="127.0.0.1",
        web_port=PORT,
        public_base_url=BASE,
        cookie_secure=False,
        owner_ids=frozenset({999}),
        license_enforce=True,
        auto_charge_enabled=True,
        login_token_minutes=15,
    )
    db = Database(tmp_path / "web.db")
    service = ShopService(db, settings)
    key = service.issue_licenses(1, 30, "standard", "1")[0]
    service.redeem_license("111", "999", key, "테스트 서버")
    product = db.create_product("111", "테스트 상품", 1000, "설명입니다", delivery_type="content")
    db.add_stock_lines(product["id"], ["CODE-X", "CODE-Y"])
    xss = db.create_product("111", "XSS</script><img src=x onerror=alert(1)>", 500, "", delivery_type="manual")
    assert xss["id"] > 0
    await service.charge("111", "999", 5000, "admin")

    web = WebServer(db, settings, service)
    await web.start()
    try:
        # 테스트 서버가 127.0.0.1 이므로 IP 호스트 쿠키를 허용해야 한다.
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            response = await client.get(f"{BASE}/")
            assert response.status == 200
            landing = await response.text()
            assert "VENDRIX" in landing

            health = await client.get(f"{BASE}/healthz")
            assert health.status == 200
            data = await health.json()
            assert data["ok"] is True and data["bot"] is False

            login_url = service.issue_login_token("111", "999", True)
            token = login_url.rsplit("/", 1)[-1]
            response = await client.get(f"{BASE}/entry/{token}", allow_redirects=False)
            assert response.status == 302
            assert "vdx_session" in response.cookies
            assert "vdx_session" in client.cookie_jar.filter_cookies(URL(BASE))

            # 1회용 링크 재사용 불가
            again = await client.get(f"{BASE}/entry/{token}", allow_redirects=False)
            assert again.status == 410

            panel = await client.get(f"{BASE}/panel")
            assert panel.status == 200
            html = await panel.text()
            assert 'id="boot"' in html
            assert "테스트 상품" in html
            assert "</script><img" not in html

            admin_page = await client.get(f"{BASE}/admin")
            assert admin_page.status == 200
            admin_html = await admin_page.text()
            assert "제품 관리" in admin_html

            api_panel = await client.get(f"{BASE}/api/panel")
            assert api_panel.status == 200
            payload = await api_panel.json()
            assert payload["guild"]["name"] == "테스트 서버"
            assert any(item["name"] == "테스트 상품" for item in payload["products"])

            csrf_blocked = await client.post(f"{BASE}/api/purchase", json={"product_id": product["id"], "quantity": 1})
            assert csrf_blocked.status == 403

            bad_origin = await client.post(
                f"{BASE}/api/purchase",
                json={"product_id": product["id"], "quantity": 1},
                headers={"X-VDX-CSRF": "1", "Origin": "https://evil.example"},
            )
            assert bad_origin.status == 403

            # 관리자(999)가 아닌 세션으로 구매하려면 별도 링크 필요 → 여기서는 관리자 본인 구매로 검증
            purchase = await client.post(
                f"{BASE}/api/purchase",
                json={"product_id": product["id"], "quantity": 1},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert purchase.status == 200, await purchase.text()
            purchase_data = await purchase.json()
            assert purchase_data["ok"] is True
            assert purchase_data["total"] == 1000

            admin_api = await client.get(f"{BASE}/api/admin")
            assert admin_api.status == 200
            admin_data = await admin_api.json()
            assert admin_data["stats"]["products"] >= 2

            create = await client.post(
                f"{BASE}/api/admin/product",
                json={"name": "새 상품", "price": 2000, "delivery_type": "manual", "description": "테스트"},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert create.status == 200, await create.text()

            deposit = await client.post(
                f"{BASE}/api/deposit",
                json={"amount": 5000, "depositor": "홍길동"},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert deposit.status == 200, await deposit.text()
            deposit_data = await deposit.json()
            assert deposit_data["pending"]["code"].startswith("W")

            # 서버 설정: 입금 한도 변경 → 상점 API에 반영
            guild_settings = await client.get(f"{BASE}/api/admin")
            admin_payload = await guild_settings.json()
            assert "settings" in admin_payload and "guild_settings" in admin_payload
            assert admin_payload["settings"]["bank"]["name"]

            update = await client.post(
                f"{BASE}/api/admin/settings",
                json={"min_deposit": 5000, "max_deposit": 50000, "purchase_max_qty": 3},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert update.status == 200, await update.text()
            updated = await update.json()
            assert updated["settings"]["min_deposit"] == 5000

            too_small = await client.post(
                f"{BASE}/api/deposit",
                json={"amount": 1000, "depositor": "홍길동"},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert too_small.status == 400

            # 전역(오너) 설정: 브랜드/계좌/토큰
            global_get = await client.get(f"{BASE}/api/owner/settings")
            assert global_get.status == 200
            global_data = await global_get.json()
            assert global_data["settings"]["brand_name"]
            assert "value" not in global_data["settings"]["pushbullet_token"]

            global_set = await client.post(
                f"{BASE}/api/owner/settings",
                json={
                    "brand_name": "테스트브랜드",
                    "bank_account": "110-999-888888",
                    "pushbullet_token": {"value": "o.secret-token-1234"},
                    "max_deposit": 2000000,
                },
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert global_set.status == 200, await global_set.text()
            after = (await global_set.json())["settings"]
            assert after["brand_name"] == "테스트브랜드"
            assert after["pushbullet_token"]["configured"] is True
            assert "secret-token" not in after["pushbullet_token"]["hint"]

            panel_after = await (await client.get(f"{BASE}/api/panel")).json()
            assert panel_after["bank"]["account"] == "110-999-888888"
            assert panel_after["brand"] == "테스트브랜드"
            # 서버 재정의(50,000)가 전역 기본값(2,000,000)보다 우선한다
            assert panel_after["settings"]["max_deposit"] == 50000
            assert service.store.max_deposit() == 2000000

            bad_setting = await client.post(
                f"{BASE}/api/owner/settings",
                json={"bank_account": "!!!"},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert bad_setting.status == 400

            owner = await client.get(f"{BASE}/api/owner/summary")
            assert owner.status == 200
            owner_data = await owner.json()
            assert owner_data["summary"]["total"] >= 1

            issue = await client.post(
                f"{BASE}/api/owner/licenses",
                json={"count": 2, "days": 30, "plan": "pro"},
                headers={"X-VDX-CSRF": "1", "Origin": BASE},
            )
            assert issue.status == 200
            keys = (await issue.json())["keys"]
            assert len(keys) == 2 and all(key.startswith("VDX-") for key in keys)

            logout = await client.get(f"{BASE}/logout", allow_redirects=False)
            assert logout.status == 302
            after = await client.get(f"{BASE}/panel", allow_redirects=False)
            assert after.status == 302 and after.headers["Location"] == "/"
    finally:
        await web.stop()
        db.close()


def test_web_panel(tmp_path):
    asyncio.run(scenario(tmp_path))
