from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.pushbullet import PushbulletListener  # noqa: E402
from app.service import ShopService  # noqa: E402

GUILD = "111"
USER = "999"


def mirrored_push(iden: str, title: str, body: str, package: str = "com.kb.star") -> dict:
    return {
        "type": "mirror",
        "iden": iden,
        "package_name": package,
        "application_name": title,
        "title": title,
        "body": body,
        "notification_id": f"n-{iden}",
        "dismissed": False,
    }


async def scenario(tmp_path: Path) -> None:
    settings = replace(
        load_settings(),
        owner_ids=frozenset({int(USER)}),
        auto_charge_enabled=True,
        license_enforce=True,
        pushbullet_allowed_packages=(),
        pushbullet_blocked_packages=("com.pushbullet.android", "com.android.systemui"),
    )
    db = Database(tmp_path / "push.db")
    service = ShopService(db, settings)
    key = service.issue_licenses(1, 30, "standard", USER)[0]
    service.redeem_license(GUILD, USER, key, "테스트 서버")
    db.update_guild(GUILD, auto_charge=1)
    pending = db.create_pending_deposit(GUILD, USER, 10_000, "홍길동", ttl_hours=6)
    listener = PushbulletListener("dummy-token", service.processor)

    alert = mirrored_push(
        "push-1",
        "KB국민은행",
        "12/22 15:04 123456-78-901234 입금 10,000원 홍길동 잔액 1,234,567원",
    )
    await listener.handle_push(alert, source="websocket")

    user = db.get_user(GUILD, USER)
    assert user is not None and int(user["balance"]) == 10_000
    assert listener.processed == 1
    assert listener.last_event_at is not None
    deposit = db.list_deposits(GUILD, limit=1)[0]
    assert deposit["status"] == "matched" and int(deposit["pending_id"] or 0) == int(pending["id"])

    # 동일 알림 재수신(웹소켓 + 폴링 중복)은 중복 처리되지 않아야 한다.
    await listener.handle_push(alert, source="poll")
    assert int(db.get_user(GUILD, USER)["balance"]) == 10_000
    assert listener.processed == 1

    # 은행 입금 알림이 아닌 일반 알림은 무시한다.
    chat = mirrored_push("push-2", "카카오톡", "홍길동: 안녕하세요", package="com.kakao.talk")
    await listener.handle_push(chat, source="websocket")
    assert listener.processed == 1
    assert len(db.list_deposits(GUILD, limit=10)) == 1

    # SMS 로 전달된 입금 알림(sms_changed)도 동일하게 처리한다.
    db.create_pending_deposit(GUILD, USER, 5_000, "김철수", ttl_hours=6)
    sms = {
        "type": "sms_changed",
        "iden": "sms-1",
        "notifications": [
            {
                "notification_id": "sms-1-1",
                "package_name": "com.android.mms",
                "application_name": "메시지",
                "title": "신한은행",
                "body": "110-123-456789\n5,000원 입금\n김철수",
            }
        ],
    }
    await listener.handle_push(sms, source="websocket")
    assert int(db.get_user(GUILD, USER)["balance"]) == 15_000
    db.close()


def test_pushbullet_auto_charge(tmp_path):
    asyncio.run(scenario(tmp_path))
