from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        return False

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, default)).strip()))
    except (TypeError, ValueError):
        return default


def _id_set(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    values: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            values.add(int(chunk))
    return frozenset(values)


def _pkg_tuple(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(p.strip() for p in raw.replace(";", ",").split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    discord_token: str
    owner_ids: frozenset[int]
    guild_id: int | None
    db_path: Path

    web_host: str
    web_port: int
    public_base_url: str
    cookie_secure: bool
    session_days: int
    login_token_minutes: int

    pushbullet_token: str
    pushbullet_allowed_packages: tuple[str, ...]
    pushbullet_blocked_packages: tuple[str, ...]
    pushbullet_poll_seconds: int
    auto_charge_enabled: bool
    deposit_ttl_hours: int

    bank_name: str
    bank_account: str
    bank_holder: str

    brand_name: str
    support_url: str
    banner_url: str
    license_enforce: bool
    log_level: str

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def origin(self) -> str:
        parsed = urlparse(self.base_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return self.base_url

    @property
    def web_dir(self) -> Path:
        return Path(__file__).resolve().parent


def load_settings() -> Settings:
    guild_raw = _env_str("GUILD_ID")
    guild_id = int(guild_raw) if guild_raw.isdigit() else None
    public_base_url = _env_str("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
    return Settings(
        discord_token=_env_str("DISCORD_TOKEN"),
        owner_ids=_id_set("OWNER_IDS"),
        guild_id=guild_id,
        db_path=Path(_env_str("DB_PATH", str(BASE_DIR / "data" / "vendrix.db"))).expanduser(),
        web_host=_env_str("WEB_HOST", "0.0.0.0"),
        web_port=_env_int("WEB_PORT", 8080),
        public_base_url=public_base_url,
        cookie_secure=_env_bool("COOKIE_SECURE", public_base_url.startswith("https://")),
        session_days=max(1, _env_int("SESSION_DAYS", 7)),
        login_token_minutes=max(1, _env_int("LOGIN_TOKEN_MINUTES", 15)),
        pushbullet_token=_env_str("PUSHBULLET_TOKEN"),
        pushbullet_allowed_packages=_pkg_tuple("PUSHBULLET_ALLOWED_PACKAGES"),
        pushbullet_blocked_packages=_pkg_tuple(
            "PUSHBULLET_BLOCKED_PACKAGES", "com.pushbullet.android,com.android.systemui"
        )
        or ("com.pushbullet.android", "com.android.systemui"),
        pushbullet_poll_seconds=max(15, _env_int("PUSHBULLET_POLL_SECONDS", 60)),
        auto_charge_enabled=_env_bool("AUTO_CHARGE_ENABLED", True),
        deposit_ttl_hours=max(1, _env_int("DEPOSIT_TTL_HOURS", 6)),
        bank_name=_env_str("BANK_NAME", "KB국민은행"),
        bank_account=_env_str("BANK_ACCOUNT", "123456-78-901234"),
        bank_holder=_env_str("BANK_HOLDER", "홍길동"),
        brand_name=_env_str("BRAND_NAME", "VENDRIX"),
        support_url=_env_str("SUPPORT_URL"),
        banner_url=_env_str("BANNER_URL"),
        license_enforce=_env_bool("LICENSE_ENFORCE", True),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )


settings = load_settings()
