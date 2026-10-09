-- Taltempla spend ledger (.frank/ledger.db). Written only by the meter. USD.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,              -- UTC request start, ISO 8601
    session_id  TEXT NOT NULL,
    run_id      TEXT,
    build_id    TEXT,
    grant_kind  TEXT,                       -- session | run | build | tool_run
    role        TEXT,                       -- main | plan | tests | code | security | tool
    tool        TEXT,
    model       TEXT,
    hit         INTEGER NOT NULL DEFAULT 0,
    miss        INTEGER NOT NULL DEFAULT 0,
    out         INTEGER NOT NULL DEFAULT 0,
    reasoning   INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'error', 'refused')),
    latency_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    started        TEXT NOT NULL,
    balance_start  REAL,
    balance_end    REAL
);

CREATE TABLE IF NOT EXISTS savings (
    session_id  TEXT NOT NULL,
    tool        TEXT NOT NULL,
    version     INTEGER,
    saved_usd   REAL NOT NULL,
    ts          TEXT NOT NULL
);

-- The ledger is append-only. `make demo-reset` moves the file away; it does not edit rows.
CREATE TRIGGER IF NOT EXISTS calls_no_update BEFORE UPDATE ON calls BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS calls_no_delete BEFORE DELETE ON calls BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS savings_no_update BEFORE UPDATE ON savings BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS savings_no_delete BEFORE DELETE ON savings BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
