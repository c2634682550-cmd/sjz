-- 卡密下载站数据库（SQLite 起步；字段与索引刻意保持 Postgres 可平移）
-- 所有时间戳 = Unix 秒（INTEGER），跨库最省事。

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS admins (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'owner',     -- owner | staff
  totp_secret   TEXT,                              -- base32；NULL = 未启用
  totp_enabled  INTEGER NOT NULL DEFAULT 0,
  failed_count  INTEGER NOT NULL DEFAULT 0,
  locked_until  INTEGER,                           -- Unix 秒
  created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS login_events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  username TEXT,
  ip       TEXT,
  ua       TEXT,
  ok       INTEGER NOT NULL,
  reason   TEXT
);
CREATE INDEX IF NOT EXISTS ix_login_events_ts ON login_events(ts);

CREATE TABLE IF NOT EXISTS batches (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      INTEGER NOT NULL,
  count           INTEGER NOT NULL,
  days            INTEGER NOT NULL,              -- 0 = 永久
  tier            TEXT NOT NULL,
  note            TEXT,
  max_per_day     INTEGER NOT NULL,
  bind_device     INTEGER NOT NULL DEFAULT 0,
  manifest_file   TEXT NOT NULL,                 -- 相对 storage 的路径
  manifest_sha256 TEXT NOT NULL,
  sig_blob        TEXT NOT NULL,                 -- P-256 签名的批次文档
  created_by      INTEGER
);

CREATE TABLE IF NOT EXISTS cards (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  code_hash      TEXT NOT NULL UNIQUE,           -- HMAC-SHA256(pepper, code)
  code_mask      TEXT NOT NULL,                  -- SJZ-AB12-****-****-WXYZ
  batch_id       INTEGER NOT NULL REFERENCES batches(id),
  tier           TEXT NOT NULL,
  days           INTEGER NOT NULL,
  note           TEXT,
  created_at     INTEGER NOT NULL,
  expires_at     INTEGER,                        -- NULL = 永久
  revoked        INTEGER NOT NULL DEFAULT 0,
  revoked_at     INTEGER,
  revoke_reason  TEXT,
  changed_at     INTEGER,                        -- 最近一次续期/改绑
  amend_blob     TEXT,                           -- 续期/改绑的 P-256 签名文档
  amend_gen      INTEGER NOT NULL DEFAULT 0,
  bound_device   TEXT,                           -- NULL = 未绑定
  bind_device    INTEGER NOT NULL DEFAULT 0,
  max_per_day    INTEGER NOT NULL DEFAULT 5,
  day_key        TEXT,                           -- 时区内的 YYYY-MM-DD
  day_count      INTEGER NOT NULL DEFAULT 0,
  total_downloads INTEGER NOT NULL DEFAULT 0,
  first_used_at  INTEGER,
  last_used_at   INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cards_batch ON cards(batch_id);
CREATE INDEX IF NOT EXISTS ix_cards_mask ON cards(code_mask);

CREATE TABLE IF NOT EXISTS versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  version      TEXT NOT NULL UNIQUE,
  notes        TEXT,
  force_update INTEGER NOT NULL DEFAULT 0,
  enabled      INTEGER NOT NULL DEFAULT 1,
  sha256       TEXT NOT NULL,
  size         INTEGER NOT NULL,
  filename     TEXT NOT NULL,
  stored_path  TEXT NOT NULL,                    -- 相对 storage 的路径
  uploaded_at  INTEGER NOT NULL,
  downloads    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash   TEXT NOT NULL UNIQUE,             -- sha256(token)
  card_id      INTEGER NOT NULL REFERENCES cards(id),
  version_id   INTEGER NOT NULL REFERENCES versions(id),
  session_id   TEXT NOT NULL,
  ip           TEXT,
  created_at   INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL,
  consumed_at  INTEGER,
  consumed_ip  TEXT
);
CREATE INDEX IF NOT EXISTS ix_tokens_expires ON tokens(expires_at);

CREATE TABLE IF NOT EXISTS downloads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  card_id    INTEGER,
  card_mask  TEXT,
  ip         TEXT,
  ua         TEXT,
  version_id INTEGER,
  version    TEXT
);
CREATE INDEX IF NOT EXISTS ix_downloads_ts ON downloads(ts);

CREATE TABLE IF NOT EXISTS attempts (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  ip        TEXT,
  card_hash TEXT,
  ok        INTEGER NOT NULL,
  reason    TEXT,
  ua        TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempts_ip_ts ON attempts(ip, ts);
CREATE INDEX IF NOT EXISTS ix_attempts_card_ts ON attempts(card_hash, ts);

CREATE TABLE IF NOT EXISTS audit (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  admin_id INTEGER,
  action   TEXT NOT NULL,
  target   TEXT,
  detail   TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts);

-- 一次性 CSV 导出令牌（批量发卡后的明文 CSV 只落盘短时间）
CREATE TABLE IF NOT EXISTS export_tokens (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash  TEXT NOT NULL UNIQUE,
  batch_id    INTEGER NOT NULL,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  consumed_at INTEGER
);
