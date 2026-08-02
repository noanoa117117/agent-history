PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_session_id TEXT,
    model TEXT,

    started_at TEXT NOT NULL,
    ended_at TEXT,

    initial_cwd TEXT,
    host_name TEXT,
    title TEXT,

    capture_status TEXT NOT NULL DEFAULT 'capturing',
    metadata_json TEXT,

    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,

    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,

    content TEXT,
    content_json TEXT,

    source_event_id TEXT,
    payload_size INTEGER,
    truncated INTEGER NOT NULL DEFAULT 0,
    dedup_key TEXT,

    cwd TEXT,
    exit_code INTEGER,
    occurred_at TEXT NOT NULL,

    sensitivity TEXT NOT NULL DEFAULT 'unchecked',

    created_at TEXT NOT NULL,

    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE (session_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    target_type TEXT NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,

    locator TEXT,
    metadata_json TEXT,

    created_at TEXT NOT NULL,

    UNIQUE (target_type, slug)
);

CREATE TABLE IF NOT EXISTS session_targets (
    session_id TEXT NOT NULL,
    target_id INTEGER NOT NULL,

    relation_type TEXT NOT NULL DEFAULT 'related',
    confidence REAL,
    assigned_by TEXT NOT NULL DEFAULT 'manual',

    created_at TEXT NOT NULL,

    PRIMARY KEY (session_id, target_id),

    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES targets(id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    event_id UNINDEXED,
    session_id UNINDEXED,
    content
);

CREATE INDEX IF NOT EXISTS idx_events_session_id ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_targets_slug ON targets(slug);
CREATE INDEX IF NOT EXISTS idx_session_targets_target_id ON session_targets(target_id);

CREATE TRIGGER IF NOT EXISTS events_fts_ai
AFTER INSERT ON events
BEGIN
    INSERT INTO events_fts(event_id, session_id, content)
    VALUES (new.id, new.session_id, coalesce(new.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS events_fts_ad
AFTER DELETE ON events
BEGIN
    DELETE FROM events_fts WHERE event_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS events_fts_au
AFTER UPDATE OF session_id, content ON events
BEGIN
    DELETE FROM events_fts WHERE event_id = old.id;
    INSERT INTO events_fts(event_id, session_id, content)
    VALUES (new.id, new.session_id, coalesce(new.content, ''));
END;
