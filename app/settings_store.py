"""DB 기반 자판기 설정.

`.env` 는 최초 기본값(부트스트랩)으로만 쓰고, 실제 운영 값은 SQLite `settings` 테이블에
저장합니다. 웹 패널과 `/설정` 명령어가 이 저장소를 통해 값을 읽고 씁니다.

키 구조
    global:<key>            오너가 관리하는 전역 설정 (계좌, 브랜드, 자동충전 마스터 등)
    guild:<guild_id>:<key>  서버 관리자가 관리하는 서버별 재정의 값 (입금 한도 등)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import utils

MAX_DEPOSIT_CEILING = 50_000_000


class SettingsError(Exception):
    """설정 값이 올바르지 않을 때."""


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    kind: str  # str | int | bool | csv | secret
    minimum: int | None = None
    maximum: int | None = None
    max_len: int = 200


GLOBAL_SPECS: dict[str, Spec] = {
    "brand_name": Spec("brand_name", "브랜드명", "str", max_len=40),
    "bank_name": Spec("bank_name", "은행", "str", max_len=40),
    "bank_account": Spec("bank_account", "계좌번호", "str", max_len=60),
    "bank_holder": Spec("bank_holder", "예금주", "str", max_len=40),
    "support_url": Spec("support_url", "문의 URL", "str", max_len=300),
    "banner_url": Spec("banner_url", "배너 이미지 URL", "str", max_len=500),
    "min_deposit": Spec("min_deposit", "최소 입금", "int", 100, 1_000_000),
    "max_deposit": Spec("max_deposit", "최대 입금", "int", 1_000, MAX_DEPOSIT_CEILING),
    "deposit_ttl_hours": Spec("deposit_ttl_hours", "입금 요청 만료(시간)", "int", 1, 72),
    "purchase_max_qty": Spec("purchase_max_qty", "1회 구매 수량", "int", 1, 20),
    "auto_charge_master": Spec("auto_charge_master", "자동충전 마스터", "bool"),
    "license_enforce": Spec("license_enforce", "라이센스 강제", "bool"),
    "pushbullet_token": Spec("pushbullet_token", "Pushbullet 토큰", "secret", max_len=200),
    "pushbullet_allowed_packages": Spec(
        "pushbullet_allowed_packages", "허용 앱 패키지", "csv", max_len=1000
    ),
    "pushbullet_blocked_packages": Spec(
        "pushbullet_blocked_packages", "차단 앱 패키지", "csv", max_len=1000
    ),
}

GUILD_SPECS: dict[str, Spec] = {
    "min_deposit": Spec("min_deposit", "최소 입금", "int", 100, 1_000_000),
    "max_deposit": Spec("max_deposit", "최대 입금", "int", 1_000, MAX_DEPOSIT_CEILING),
    "deposit_ttl_hours": Spec("deposit_ttl_hours", "입금 요청 만료(시간)", "int", 1, 72),
    "purchase_max_qty": Spec("purchase_max_qty", "1회 구매 수량", "int", 1, 20),
}

SECRET_KEYS = {"pushbullet_token"}

_ACCOUNT_RE = re.compile(r"^[\d\- ]{6,40}$")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.replace(";", ",").split(",") if part.strip())


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


class SettingsStore:
    def __init__(self, db, base):
        self.db = db
        self.base = base

    # ------------------------------------------------------------------ global
    def _default(self, key: str) -> Any:
        base = self.base
        return {
            "brand_name": base.brand_name,
            "bank_name": base.bank_name,
            "bank_account": base.bank_account,
            "bank_holder": base.bank_holder,
            "support_url": base.support_url,
            "banner_url": base.banner_url,
            "min_deposit": 100,
            "max_deposit": 10_000_000,
            "deposit_ttl_hours": base.deposit_ttl_hours,
            "purchase_max_qty": 10,
            "auto_charge_master": base.auto_charge_enabled,
            "license_enforce": base.license_enforce,
            "pushbullet_token": base.pushbullet_token,
            "pushbullet_allowed_packages": ",".join(base.pushbullet_allowed_packages),
            "pushbullet_blocked_packages": ",".join(base.pushbullet_blocked_packages),
        }.get(key, "")

    def _coerce(self, spec: Spec, raw: Any) -> Any:
        if spec.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}
        if spec.kind == "int":
            if isinstance(raw, bool):
                raise SettingsError(f"{spec.label} 값이 올바르지 않습니다.")
            try:
                value = int(str(raw).replace(",", "").strip())
            except (TypeError, ValueError):
                raise SettingsError(f"{spec.label} 은(는) 숫자여야 합니다.")
            if spec.minimum is not None and value < spec.minimum:
                raise SettingsError(f"{spec.label} 은(는) {spec.minimum:,} 이상이어야 합니다.")
            if spec.maximum is not None and value > spec.maximum:
                raise SettingsError(f"{spec.label} 은(는) {spec.maximum:,} 이하여야 합니다.")
            return value
        text = "" if raw is None else str(raw).strip()
        if spec.kind == "csv":
            text = ",".join(_csv(text))
        if len(text) > spec.max_len:
            raise SettingsError(f"{spec.label} 은(는) {spec.max_len}자 이하여야 합니다.")
        if spec.key == "bank_account" and text and not _ACCOUNT_RE.match(text):
            raise SettingsError("계좌번호는 숫자, -, 공백만 사용할 수 있습니다.")
        if spec.key in {"support_url", "banner_url"} and text:
            if not text.startswith(("http://", "https://")):
                raise SettingsError(f"{spec.label} 은(는) http(s):// 로 시작해야 합니다.")
        return text

    def get_global(self, key: str) -> Any:
        spec = GLOBAL_SPECS.get(key)
        if spec is None:
            raise KeyError(key)
        raw = self.db.get_setting(f"global:{key}")
        if raw is None:
            return self._default(key)
        try:
            return self._coerce(spec, raw)
        except SettingsError:
            return self._default(key)

    def set_global(self, key: str, value: Any, actor: str = "system") -> Any:
        spec = GLOBAL_SPECS.get(key)
        if spec is None:
            raise SettingsError(f"알 수 없는 설정입니다: {key}")
        coerced = self._coerce(spec, value)
        if spec.kind == "int":
            stored = str(coerced)
        elif spec.kind == "bool":
            stored = "1" if coerced else "0"
        else:
            stored = coerced
        self.db.set_setting(f"global:{key}", stored)
        self.db.audit(
            None,
            actor,
            "settings.global",
            f"{key}={'***' if key in SECRET_KEYS and stored else stored}",
        )
        return coerced

    def global_view(self, redact: bool = True) -> dict:
        view: dict[str, Any] = {}
        for key, spec in GLOBAL_SPECS.items():
            value = self.get_global(key)
            if key in SECRET_KEYS:
                view[key] = {
                    "configured": bool(value),
                    "hint": mask_secret(str(value)) if redact else str(value),
                }
            elif spec.kind == "csv":
                view[key] = list(_csv(str(value)))
            else:
                view[key] = value
        return view

    # ------------------------------------------------------------------- guild
    def get_guild(self, guild_id: str | int, key: str, fallback: Any = None) -> Any:
        spec = GUILD_SPECS.get(key)
        if spec is None:
            raise KeyError(key)
        raw = self.db.get_setting(f"guild:{guild_id}:{key}")
        if raw is None:
            return self.get_global(key) if fallback is None else fallback
        try:
            return self._coerce(spec, raw)
        except SettingsError:
            return self.get_global(key)

    def set_guild(self, guild_id: str | int, key: str, value: Any, actor: str = "system") -> Any:
        spec = GUILD_SPECS.get(key)
        if spec is None:
            raise SettingsError(f"서버에서 바꿀 수 없는 설정입니다: {key}")
        if value is None or str(value).strip() == "":
            self.db._exec("DELETE FROM settings WHERE key = ?", (f"guild:{guild_id}:{key}",))
            self.db.audit(guild_id, actor, "settings.guild", f"{key}=기본값")
            return self.get_global(key)
        coerced = self._coerce(spec, value)
        self.db.set_setting(f"guild:{guild_id}:{key}", str(coerced))
        self.db.audit(guild_id, actor, "settings.guild", f"{key}={coerced}")
        return coerced

    def clear_guild(self, guild_id: str | int, key: str, actor: str = "system") -> None:
        self.set_guild(guild_id, key, "", actor)

    def guild_view(self, guild_id: str | int) -> dict:
        view: dict[str, Any] = {}
        for key in GUILD_SPECS:
            view[key] = {
                "value": self.get_guild(guild_id, key),
                "override": self.db.get_setting(f"guild:{guild_id}:{key}") is not None,
            }
        return view

    # -------------------------------------------------------------- typed reads
    def brand(self) -> str:
        return str(self.get_global("brand_name") or self.base.brand_name)

    def bank(self) -> dict:
        return {
            "name": str(self.get_global("bank_name")),
            "account": str(self.get_global("bank_account")),
            "holder": str(self.get_global("bank_holder")),
        }

    def support_url(self) -> str:
        return str(self.get_global("support_url"))

    def banner(self) -> str:
        return str(self.get_global("banner_url")) or f"{self.base.base_url}/static/banner.png"

    def min_deposit(self, guild_id: str | int | None = None) -> int:
        if guild_id is None:
            return int(self.get_global("min_deposit"))
        return int(self.get_guild(guild_id, "min_deposit"))

    def max_deposit(self, guild_id: str | int | None = None) -> int:
        if guild_id is None:
            return int(self.get_global("max_deposit"))
        return int(self.get_guild(guild_id, "max_deposit"))

    def deposit_ttl(self, guild_id: str | int | None = None) -> int:
        if guild_id is None:
            return int(self.get_global("deposit_ttl_hours"))
        return int(self.get_guild(guild_id, "deposit_ttl_hours"))

    def purchase_max_qty(self, guild_id: str | int | None = None) -> int:
        if guild_id is None:
            return int(self.get_global("purchase_max_qty"))
        return int(self.get_guild(guild_id, "purchase_max_qty"))

    def auto_charge_master(self) -> bool:
        return bool(self.get_global("auto_charge_master"))

    def license_enforce(self) -> bool:
        return bool(self.get_global("license_enforce"))

    def pushbullet_token(self) -> str:
        return str(self.get_global("pushbullet_token"))

    def allowed_packages(self) -> tuple[str, ...]:
        return _csv(str(self.get_global("pushbullet_allowed_packages")))

    def blocked_packages(self) -> tuple[str, ...]:
        return _csv(str(self.get_global("pushbullet_blocked_packages")))

    # ------------------------------------------------------------------ display
    def summary(self, guild_id: str | int) -> dict:
        limits = self.guild_view(guild_id)
        return {
            "bank": self.bank(),
            "brand": self.brand(),
            "support_url": self.support_url(),
            "min_deposit": self.min_deposit(guild_id),
            "max_deposit": self.max_deposit(guild_id),
            "deposit_ttl_hours": self.deposit_ttl(guild_id),
            "purchase_max_qty": self.purchase_max_qty(guild_id),
            "auto_charge_master": self.auto_charge_master(),
            "license_enforce": self.license_enforce(),
            "overrides": {key: value["override"] for key, value in limits.items()},
        }

    def describe(self, guild_id: str | int) -> str:
        bank = self.bank()
        return (
            f"**브랜드** · {self.brand()}\n"
            f"**계좌** · {bank['name']} `{utils.mask_account(bank['account'])}` ({bank['holder']})\n"
            f"**입금** · {utils.fmt_won(self.min_deposit(guild_id))} ~ {utils.fmt_won(self.max_deposit(guild_id))}"
            f" · 요청 {self.deposit_ttl(guild_id)}시간 유지\n"
            f"**1회 구매** · 최대 {self.purchase_max_qty(guild_id)}개\n"
            f"**자동충전 마스터** · {'켜짐' if self.auto_charge_master() else '꺼짐'}\n"
            f"**라이센스 강제** · {'켜짐' if self.license_enforce() else '꺼짐'}"
        )
