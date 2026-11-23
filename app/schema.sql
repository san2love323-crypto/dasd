PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guilds (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL DEFAULT '',
    registered_at       INTEGER NOT NULL,
    license_key         TEXT,
    license_expires_at  INTEGER,
    panel_channel_id    TEXT,
    log_channel_id      TEXT,
    admin_role_id       TEXT,
    notice              TEXT NOT NULL DEFAULT '',
    event_text          TEXT NOT NULL DEFAULT '',
    auto_charge         INTEGER NOT NULL DEFAULT 1,
    buy_enabled         INTEGER NOT NULL DEFAULT 1,
    created_at          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    guild_id        TEXT NOT NULL,
    discord_id      TEXT NOT NULL,
    balance         INTEGER NOT NULL DEFAULT 0,
    total_spent     INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    price           INTEGER NOT NULL,
    delivery_type   TEXT NOT NULL DEFAULT 'manual',
    role_id         TEXT,
    stock           INTEGER NOT NULL DEFAULT -1,
    active          INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS product_stock (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL,
    content         TEXT NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0,
    used_by         TEXT,
    purchase_id     INTEGER,
    used_at         INTEGER,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS purchases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    discord_id      TEXT NOT NULL,
    product_id      INTEGER,
    product_name    TEXT NOT NULL,
    unit_price      INTEGER NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 1,
    total           INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed',
    delivery_type   TEXT NOT NULL DEFAULT 'manual',
    delivery        TEXT,
    channel         TEXT NOT NULL DEFAULT 'discord',
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_deposits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    discord_id      TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    depositor       TEXT NOT NULL,
    code            TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    matched_deposit_id INTEGER
);

CREATE TABLE IF NOT EXISTS deposits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    push_iden       TEXT UNIQUE,
    source          TEXT NOT NULL DEFAULT 'pushbullet',
    application     TEXT,
    title           TEXT,
    body            TEXT,
    amount          INTEGER,
    depositor       TEXT,
    guild_id        TEXT,
    discord_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'unmatched',
    pending_id      INTEGER,
    note            TEXT,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS licenses (
    key                 TEXT PRIMARY KEY,
    plan                TEXT NOT NULL DEFAULT 'standard',
    duration_days       INTEGER NOT NULL DEFAULT 30,
    max_guilds          INTEGER NOT NULL DEFAULT 1,
    created_by          TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    redeemed_by         TEXT,
    redeemed_by_user    TEXT,
    redeemed_at         INTEGER,
    expires_at          INTEGER,
    revoked             INTEGER NOT NULL DEFAULT 0,
    note                TEXT
);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash      TEXT PRIMARY KEY,
    guild_id        TEXT NOT NULL,
    discord_id      TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    is_owner        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash      TEXT PRIMARY KEY,
    guild_id        TEXT NOT NULL,
    discord_id      TEXT NOT NULL,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT,
    actor       TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE INDEX IF NOT EXISTS idx_products_guild ON products(guild_id, active, sort_order);
CREATE INDEX IF NOT EXISTS idx_stock_product ON product_stock(product_id, used);
CREATE INDEX IF NOT EXISTS idx_purchases_guild ON purchases(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(guild_id, discord_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_deposits(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_amount ON pending_deposits(amount, status);
CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_licenses_redeemed ON licenses(redeemed_by);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON web_sessions(expires_at);
