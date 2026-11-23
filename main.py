#!/usr/bin/env python3
"""VENDRIX 단일 실행 파일.

    python main.py                 # 디스코드 봇 + 웹 패널 + Pushbullet 리스너
    python main.py --check         # 설정 점검 (DB에 저장된 실제 값 포함)
    python main.py --web-only      # 웹 패널만 실행 (디스코드 봇 없이 UI 확인)
    python main.py --host 0.0.0.0 --port 8080

자판기 설정(계좌/입금 한도/자동충전/브랜드 등)은 이 파일이 아니라
웹 패널의 설정 탭이나 디스코드 `/설정` 명령어에서 바꿉니다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.bot import DiscordNotifier, WiaBot  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.pushbullet import PushbulletListener  # noqa: E402
from app.service import ShopService  # noqa: E402
from app.settings_store import SettingsStore  # noqa: E402
from app.web import WebServer  # noqa: E402

log = logging.getLogger("vendrix")


def check_config(db: Database | None = None) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    if not settings.discord_token:
        problems.append("DISCORD_TOKEN 이 비어 있습니다.")
    if not settings.owner_ids:
        problems.append("OWNER_IDS 가 비어 있습니다. 오너 디스코드 ID를 지정하세요.")
    if settings.cookie_secure and not settings.base_url.startswith("https://"):
        problems.append("COOKIE_SECURE=true 인데 PUBLIC_BASE_URL 이 https 가 아닙니다.")
    if not settings.base_url.startswith(("http://", "https://")):
        problems.append("PUBLIC_BASE_URL 형식이 올바르지 않습니다.")

    print(f"owner ids      : {', '.join(str(i) for i in settings.owner_ids) or '(없음)'}")
    print(f"database       : {settings.db_path}")
    print(f"web            : {settings.web_host}:{settings.web_port}  (public: {settings.base_url})")
    print(f"log level      : {settings.log_level}")

    if db is not None:
        store = SettingsStore(db, settings)
        bank = store.bank()
        print("-- DB에 저장된 자판기 설정 (웹 패널/`/설정`에서 변경) --")
        print(f"brand          : {store.brand()}")
        print(f"bank           : {bank['name']} {bank['account']} / {bank['holder']}")
        print(f"deposit        : {store.min_deposit():,} ~ {store.max_deposit():,}원 · {store.deposit_ttl()}시간")
        print(f"purchase qty   : 최대 {store.purchase_max_qty()}개")
        print(f"auto charge    : {'ON' if store.auto_charge_master() else 'OFF'} (마스터)")
        print(f"license enforce: {'ON' if store.license_enforce() else 'OFF'}")
        token = store.pushbullet_token()
        print(f"pushbullet     : {'설정됨 (' + token[:4] + '...)' if token else '없음 (자동충전 비활성)'}")
        if not token:
            warnings.append("Pushbullet 토큰이 없습니다. 웹 패널 → 시스템 설정에서 입력하세요.")
        if not bank["account"]:
            warnings.append("입금 계좌가 비어 있습니다. 웹 패널 → 시스템 설정에서 입력하세요.")

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


async def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    db = Database(settings.db_path)
    service = ShopService(db, settings)
    store = service.store

    if not args.web_only and not settings.discord_token:
        db.close()
        raise SystemExit("DISCORD_TOKEN 이 없습니다. .env 를 확인하세요. (python main.py --check)")

    web = WebServer(db, settings, service)
    listener = PushbulletListener(
        store.pushbullet_token(),
        service.processor,
        poll_seconds=settings.pushbullet_poll_seconds,
    )
    web.pushbullet = listener
    service.pushbullet = listener

    bot: WiaBot | None = None
    if not args.web_only:
        bot = WiaBot(db, settings, service)
        service.bot = bot
        service.attach_notifier(DiscordNotifier(bot))

    await web.start()
    await listener.start()
    cleanup_task = asyncio.create_task(cleanup_loop(db), name="vendrix-cleanup")

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        if bot is not None:
            asyncio.create_task(bot.close())
        else:
            for task in asyncio.all_tasks(loop):
                if task is not asyncio.current_task():
                    task.cancel()

    if bot is not None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except NotImplementedError:
                pass

    try:
        if bot is not None:
            await bot.start(settings.discord_token)
        else:
            log.info("web-only mode: http://%s:%s", settings.web_host, settings.web_port)
            await asyncio.Event().wait()
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VENDRIX · 디스코드 자판기 + 웹 패널")
    parser.add_argument("--check", action="store_true", help="설정 점검 후 종료")
    parser.add_argument("--web-only", action="store_true", help="웹 패널만 실행")
    parser.add_argument("--host", default=None, help="웹 바인딩 주소 (기본: .env WEB_HOST)")
    parser.add_argument("--port", type=int, default=None, help="웹 포트 (기본: .env WEB_PORT)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # --host/--port 는 실행 중에만 덮어쓴다.
    if args.host or args.port:
        object.__setattr__(settings, "web_host", args.host or settings.web_host)
        object.__setattr__(settings, "web_port", args.port or settings.web_port)

    if args.check:
        db = Database(settings.db_path)
        try:
            raise SystemExit(check_config(db))
        finally:
            db.close()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
