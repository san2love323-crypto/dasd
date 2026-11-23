from __future__ import annotations

import hashlib
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 혼동하기 쉬운 문자(0/O, 1/I) 제외
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def now() -> int:
    return int(time.time())


def kst_now() -> datetime:
    return datetime.now(KST)


def fmt_dt(ts: int | float | None, fmt: str = "%Y년 %m월 %d일 %H:%M") -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), KST).strftime(fmt)


def fmt_date(ts: int | float | None) -> str:
    return fmt_dt(ts, "%Y.%m.%d")


def start_of_today_kst() -> int:
    midnight = kst_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def fmt_won(amount: int | float | None) -> str:
    try:
        value = int(amount or 0)
    except (TypeError, ValueError):
        value = 0
    sign = "-" if value < 0 else ""
    return f"{sign}{abs(value):,}원"


def parse_amount(raw: str | int | None) -> int | None:
    """'10,000원' / '10000' / '1만원' 형태를 정수로 변환."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().replace(" ", "")
    if not text:
        return None
    text = text.replace("원", "").replace(",", "")
    match = re.fullmatch(r"(-?)(\d+(?:\.\d+)?)(만|천)?", text)
    if not match:
        return None
    sign, number, unit = match.groups()
    value = float(number)
    if unit == "만":
        value *= 10000
    elif unit == "천":
        value *= 1000
    result = int(value)
    return -result if sign else result


def gen_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def gen_license_key() -> str:
    groups = ["".join(secrets.choice(_KEY_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "VDX-" + "-".join(groups)


def gen_deposit_code() -> str:
    return "W" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(5))


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"\s+", "", str(value))
    text = text.replace("님", "")
    text = re.sub(r"[^0-9A-Za-z가-힣]", "", text)
    return text.lower()


def truncate(value: str, limit: int = 120) -> str:
    value = (value or "").replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def mask_account(account: str) -> str:
    account = account or ""
    if len(account) <= 6:
        return account
    return account[:-4] + "****"
