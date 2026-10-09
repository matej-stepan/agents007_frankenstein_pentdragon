-- shedd registry (/data/shed.db). Append-only: versions, test_runs, reviews, approvals, events, builds_log.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS versions (
    name TEXT NOT NULL, version INTEGER NOT NULL, grade TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE, perm_hash TEXT NOT NULL,
    manifest TEXT NOT NULL, files TEXT NOT NULL, parent TEXT,
    origin TEXT NOT NULL DEFAULT 'chef', build_id TEXT, build_cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS test_runs (
    id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, kind TEXT NOT NULL,
    passed INTEGER NOT NULL, failed INTEGER NOT NULL, log TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS test_runs_hash ON test_runs(content_hash);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL, verdict TEXT NOT NULL, report TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS reviews_hash ON reviews(content_hash);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL, build_id TEXT, mode TEXT NOT NULL, by TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, tool TEXT, version INTEGER, data TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS events_tool ON events(tool, kind);
CREATE INDEX IF NOT EXISTS events_invoke ON events(json_extract(data, '$.invoke_id')) WHERE kind = 'invoked';
CREATE TABLE IF NOT EXISTS builds_log (
    id INTEGER PRIMARY KEY, build_id TEXT NOT NULL, event TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Mutable state.
CREATE TABLE IF NOT EXISTS active (name TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS drafts (
    content_hash TEXT PRIMARY KEY, build_id TEXT NOT NULL, name TEXT NOT NULL, perm_hash TEXT NOT NULL,
    manifest TEXT NOT NULL, files TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY, session_id TEXT, task TEXT, need TEXT, lookup_id TEXT, repair_of TEXT,
    status TEXT NOT NULL DEFAULT 'running', cost_usd REAL NOT NULL DEFAULT 0, handoff TEXT,
    started TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')), finished TEXT
);
CREATE TABLE IF NOT EXISTS lookups (
    id TEXT PRIMARY KEY, session_id TEXT, query TEXT NOT NULL, fit TEXT NOT NULL, rows TEXT NOT NULL,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS packages (
    name TEXT PRIMARY KEY, ok INTEGER NOT NULL, log TEXT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Full-text index over the ACTIVE version of each tool (rebuilt on register and rollback).
CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
    name, summary, keywords, description, skill, tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT, 'append-only: versions'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT, 'append-only: versions'); END;
CREATE TRIGGER IF NOT EXISTS test_runs_no_update BEFORE UPDATE ON test_runs BEGIN SELECT RAISE(ABORT, 'append-only: test_runs'); END;
CREATE TRIGGER IF NOT EXISTS test_runs_no_delete BEFORE DELETE ON test_runs BEGIN SELECT RAISE(ABORT, 'append-only: test_runs'); END;
CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT, 'append-only: reviews'); END;
CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT, 'append-only: reviews'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals BEGIN SELECT RAISE(ABORT, 'append-only: approvals'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals BEGIN SELECT RAISE(ABORT, 'append-only: approvals'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only: events'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append-only: events'); END;
CREATE TRIGGER IF NOT EXISTS builds_log_no_update BEFORE UPDATE ON builds_log BEGIN SELECT RAISE(ABORT, 'append-only: builds_log'); END;
CREATE TRIGGER IF NOT EXISTS builds_log_no_delete BEFORE DELETE ON builds_log BEGIN SELECT RAISE(ABORT, 'append-only: builds_log'); END;
