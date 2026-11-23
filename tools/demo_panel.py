"""디자인 확인용 데모 웹 패널.

실제 디스코드 봇/푸시불릿 없이 샘플 데이터로 웹 패널만 실행합니다.

    python tools/demo_panel.py            # http://127.0.0.1:8099
    python tools/demo_panel.py --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.config import load_settings  # noqa: E402
from app.plans import resolve_plan  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import ShopService  # noqa: E402
from app.web import WebServer  # noqa: E402

DEMO_GUILD = "111"
DEMO_USER = "999"


def seed(db: Database, service: ShopService, plan: str = "premium") -> None:
    db.ensure_guild(DEMO_GUILD, "VENDRIX 테스트 서버")
    spec = resolve_plan(plan)
    key = service.issue_licenses(1, 365, spec.key, DEMO_USER, "데모 발급")[0]
    service.redeem_license(DEMO_GUILD, DEMO_USER, key, "VENDRIX 테스트 서버")

    products = [
        ("넷플릭스 4K 계정", 12000, "content", "1개월 이용권 · 재고 자동 차감", ["NF-4K-0001", "NF-4K-0002"]),
        ("디즈니+ 1년 이용권", 25000, "content", "연간 이용권 · 구매 즉시 DM 발송", ["DP-1Y-0001"]),
        ("Discord Nitro 1개월", 5500, "content", "Nitro Gift 링크 자동 발송", ["NITRO-0001", "NITRO-0002", "NITRO-0003"]),
        ("커스텀 채널 권한", 30000, "manual", "관리자가 직접 권한을 지급합니다.", None),
    ]
    for name, price, delivery, description, stock in products:
        if not spec.allows_delivery(delivery):
            continue  # 플랜에서 허용되지 않는 전달 방식은 시드하지 않는다
        product = db.create_product(DEMO_GUILD, name, price, description, delivery_type=delivery)
        if stock:
            db.add_stock_lines(product["id"], stock)

    db.add_balance(DEMO_GUILD, DEMO_USER, 85000)
    db.audit(DEMO_GUILD, DEMO_USER, "demo.seed", "샘플 데이터 생성")


async def main() -> None:
    parser = argparse.ArgumentParser(description="VENDRIX 데모 웹 패널")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--db", default="/tmp/vendrix-demo.db")
    parser.add_argument("--plan", default="premium", choices=["basic", "standard", "premium", "platinum"])
    args = parser.parse_args()

    db_path = Path(args.db)
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db_path) + suffix)
        if target.exists():
            target.unlink()

    base_url = f"http://{args.host}:{args.port}"
    settings = replace(
        load_settings(),
        web_host=args.host,
        web_port=args.port,
        public_base_url=base_url,
        cookie_secure=False,
        owner_ids=frozenset({int(DEMO_USER)}),
        auto_charge_enabled=True,
    )
    db = Database(db_path)
    service = ShopService(db, settings)
    seed(db, service, args.plan)

    web = WebServer(db, settings, service)
    await web.start()
    login_url = service.issue_login_token(DEMO_GUILD, DEMO_USER, True)
    print(f"데모 플랜  : {resolve_plan(args.plan).name}", flush=True)
    print(f"데모 패널  : {base_url}/", flush=True)
    print(f"관리자 로그인: {login_url}", flush=True)
    print("종료: Ctrl+C", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await web.stop()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
