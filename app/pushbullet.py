"""Pushbullet 실시간 알림 리스너.

안드로이드 Pushbullet 앱이 미러링한 알림을 웹소켓으로 수신하고,
끊김/누락에 대비해 REST 폴링을 병행한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger("vendrix.pushbullet")

API_BASE = "https://api.pushbullet.com/v2"
STREAM_URL = "wss://stream.pushbullet.com/websocket/{token}"


class PushbulletError(Exception):
    pass


class PushbulletListener:
    def __init__(self, token: str, processor: Any, poll_seconds: int = 60, backfill_seconds: int = 60):
        self.token = (token or "").strip()
        self.processor = processor
        self.poll_seconds = max(15, int(poll_seconds))
        self.backfill_seconds = max(0, int(backfill_seconds))
        self.connected = False
        self.last_event_at: int | None = None
        self.last_error: str | None = None
        self.processed = 0
        self._session: aiohttp.ClientSession | None = None
        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._last_modified = time.time() - self.backfill_seconds
        self._seen: set[str] = set()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "enabled": bool(self.token),
            "connected": self.connected,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "processed": self.processed,
        }

    # ------------------------------------------------------------------- http
    def _headers(self) -> dict[str, str]:
        return {"Access-Token": self.token, "Content-Type": "application/json"}

    async def verify_token(self) -> dict | None:
        if not self.token or self._session is None:
            return None
        async with self._session.get(f"{API_BASE}/users/me", headers=self._headers()) as response:
            if response.status == 401:
                raise PushbulletError("Pushbullet 토큰이 유효하지 않습니다. (401)")
            if response.status != 200:
                raise PushbulletError(f"Pushbullet 사용자 조회 실패 ({response.status})")
            data = await response.json()
        return {"name": data.get("name"), "created": data.get("created")}

    async def fetch_pushes(self, limit: int = 20, modified_after: float | None = None) -> list[dict]:
        if self._session is None:
            return []
        params = {
            "active": "true",
            "limit": str(limit),
            "modified_after": f"{modified_after if modified_after is not None else self._last_modified:.3f}",
        }
        for attempt in range(3):
            async with self._session.get(f"{API_BASE}/pushes", headers=self._headers(), params=params) as response:
                if response.status == 429:
                    retry = float(response.headers.get("Retry-After", "5") or 5)
                    log.warning("pushbullet rate limited, sleeping %.1fs", retry)
                    await asyncio.sleep(min(retry, 60))
                    continue
                if response.status == 401:
                    raise PushbulletError("Pushbullet 토큰이 유효하지 않습니다. (401)")
                if response.status != 200:
                    text = await response.text()
                    raise PushbulletError(f"Pushbullet 조회 실패 ({response.status}): {text[:200]}")
                data = await response.json()
                pushes = data.get("pushes") or []
                if pushes:
                    newest = max(float(p.get("modified") or 0) for p in pushes)
                    self._last_modified = max(self._last_modified, newest)
                return pushes
        raise PushbulletError("Pushbullet 요청이 제한되었습니다.")

    # --------------------------------------------------------------- dispatch
    async def handle_push(self, push: dict, source: str = "websocket") -> None:
        if not isinstance(push, dict):
            return
        if push.get("type") == "sms_changed":
            for notification in push.get("notifications") or []:
                item = dict(notification)
                item.setdefault("iden", f"{push.get('iden', 'sms')}:{item.get('notification_id', '')}")
                item["type"] = "mirror"
                item.setdefault("application_name", "SMS")
                await self._process(item, source)
            return
        await self._process(push, source)

    async def _process(self, push: dict, source: str) -> None:
        iden = push.get("iden")
        if iden and iden in self._seen:
            return
        try:
            outcome = await self.processor.process_push(push, source=source)
        except Exception:
            log.exception("failed to process push %s", iden)
            return
        if iden:
            self._seen.add(iden)
            if len(self._seen) > 5000:
                self._seen = set(list(self._seen)[-2500:])
        if outcome.status in {"matched", "review", "unmatched"}:
            self.last_event_at = int(time.time())
            self.processed += 1
            log.info("deposit %s: %s (%s)", outcome.status, outcome.amount, outcome.message)

    async def poll_once(self) -> int:
        pushes = await self.fetch_pushes(limit=20)
        count = 0
        for push in sorted(pushes, key=lambda item: float(item.get("modified") or 0)):
            await self.handle_push(push, source="poll")
            count += 1
        return count

    # ------------------------------------------------------------------ loops
    async def _poll_loop(self) -> None:
        while not self._closing:
            try:
                await self.poll_once()
                self.last_error = None
            except PushbulletError as exc:
                self.last_error = str(exc)
                log.warning("poll failed: %s", exc)
                if "401" in str(exc):
                    await asyncio.sleep(300)
                    continue
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("poll network error: %s", exc)
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("unexpected poll error")
            await asyncio.sleep(self.poll_seconds)

    async def _stream_loop(self) -> None:
        backoff = 3
        while not self._closing:
            try:
                assert self._session is not None
                async with self._session.ws_connect(
                    STREAM_URL.format(token=self.token), heartbeat=30, max_msg_size=8 * 1024 * 1024
                ) as ws:
                    self.connected = True
                    self.last_error = None
                    backoff = 3
                    log.info("pushbullet websocket connected")
                    # 연결 직후 누락분 보충
                    try:
                        await self.poll_once()
                    except Exception:
                        log.debug("initial catch-up failed", exc_info=True)
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(message.data)
                            except json.JSONDecodeError:
                                continue
                            kind = data.get("type")
                            if kind == "tickle" and data.get("subtype") == "push":
                                try:
                                    await self.poll_once()
                                except Exception as exc:
                                    log.warning("tickle poll failed: %s", exc)
                            elif kind == "push" and isinstance(data.get("push"), dict):
                                await self.handle_push(data["push"], source="websocket")
                        elif message.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("websocket error: %s (retry in %ss)", exc, backoff)
            finally:
                self.connected = False
            if self._closing:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------- life
    async def start(self) -> bool:
        if not self.token:
            log.warning("Pushbullet 토큰이 없어 자동충전 수신을 비활성화합니다.")
            return False
        self._closing = False
        self._last_modified = time.time() - self.backfill_seconds
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            await self.verify_token()
        except Exception as exc:
            self.last_error = str(exc)
            log.error("pushbullet token check failed: %s", exc)
        self._tasks = [
            asyncio.create_task(self._stream_loop(), name="pushbullet-stream"),
            asyncio.create_task(self._poll_loop(), name="pushbullet-poll"),
        ]
        log.info("pushbullet listener started (poll=%ss)", self.poll_seconds)
        return True

    async def reconfigure(self, token: str) -> bool:
        """토큰/설정 변경 시 리스너를 재시작한다."""
        token = (token or "").strip()
        if token == self.token and self._tasks:
            return True
        await self.stop()
        self.token = token
        self.connected = False
        self.last_error = None
        self._seen.clear()
        if not token:
            log.info("pushbullet token cleared; listener disabled")
            return False
        log.info("pushbullet listener reconfigured")
        return await self.start()

    async def stop(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.connected = False
