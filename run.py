#!/usr/bin/env python3
"""VENDRIX 실행 엔트리포인트.

    python run.py            # 봇 + 웹 패널 + Pushbullet 리스너
    python run.py --check    # 설정 점검만
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.bot import DiscordNotifier, WiaBot
from app.config import settings
from app.db import Database
from app.pushbullet import PushbulletListener
from app.service import ShopService
from app.web import WebServer

log = logging.getLogger("vendrix")


def check_config() -> int:
    problems = []
    warnings = []
    if not settings.discord_token:
        problems.append("DISCORD_TOKEN 이 비어 있습니다.")
    if not settings.owner_ids:
        problems.append("OWNER_IDS 가 비어 있습니다. 오너 디스코드 ID를 지정하세요.")
    if not settings.pushbullet_token:
        warnings.append("PUSHBULLET_TOKEN 이 비어 있습니다. (자동충전 비활성)")
    if settings.cookie_secure and not settings.base_url.startswith("https://"):
        problems.append("COOKIE_SECURE=true 인데 PUBLIC_BASE_URL 이 https 가 아닙니다.")
    if not settings.base_url.startswith(("http://", "https://")):
        problems.append("PUBLIC_BASE_URL 형식이 올바르지 않습니다.")

    print(f"brand          : {settings.brand_name}")
    print(f"owner ids      : {', '.join(str(i) for i in settings.owner_ids) or '(없음)'}")
    print(f"database       : {settings.db_path}")
    print(f"web            : {settings.web_host}:{settings.web_port}  (public: {settings.base_url})")
    print(f"bank           : {settings.bank_name} {settings.bank_account} / {settings.bank_holder}")
    print(f"pushbullet     : {'설정됨' if settings.pushbullet_token else '없음'}")
    print(f"auto charge    : {settings.auto_charge_enabled}")
    print(f"license enforce: {settings.license_enforce}")
    for problem in problems:
        print(f"! {problem}")
    for warning in warnings:
        print(f"[경고] {warning}")
    return 0 if not problems else 1


async def cleanup_loop(db: Database) -> None:
    while True:
        try:
            await asyncio.sleep(600)
            await asyncio.to_thread(db.cleanup)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cleanup failed")


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    if not settings.discord_token:
        raise SystemExit("DISCORD_TOKEN 이 없습니다. .env 를 확인하세요. (python run.py --check 로 점검)")

    db = Database(settings.db_path)
    service = ShopService(db, settings)
    bot = WiaBot(db, settings, service)
    service.bot = bot
    notifier = DiscordNotifier(bot)
    service.attach_notifier(notifier)

    web = WebServer(db, settings, service)
    listener = PushbulletListener(
        settings.pushbullet_token, service.processor, poll_seconds=settings.pushbullet_poll_seconds
    )
    web.pushbullet = listener

    await web.start()
    await listener.start()
    cleanup_task = asyncio.create_task(cleanup_loop(db), name="vendrix-cleanup")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError:
            pass

    try:
        await bot.start(settings.discord_token)
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await listener.stop()
        await web.stop()
        db.close()
        log.info("shutdown complete")


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(check_config())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
