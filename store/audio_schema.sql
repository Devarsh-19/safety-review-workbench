-- SQLite schema for the audio review database (separate from the chat DB).
-- Mirrors the chat workbench model: sessions -> segments (like turns) -> flags.

CREATE TABLE IF NOT EXISTS audio_sessions (
    s_id                INTEGER PRIMARY KEY,
    lang                TEXT,
    pauses              TEXT,                               -- JSON array, stored as delivered
    needs_review        INTEGER DEFAULT 0,                  -- the 'review' boolean from the LLM output
    speaker1_role       TEXT,                               -- 'ASTROLOGER' | 'USER' — assigned by reviewer, NULL until set
    speaker2_role       TEXT,
    overall_verdict     TEXT,                               -- 'CLEAN', 'FLAGGED'
    review_status       TEXT    DEFAULT 'PENDING',          -- 'PENDING', 'SUBMITTED_FOR_REVIEW', 'LOCKED'
    reviewer_id         TEXT,
    reviewer_note       TEXT,
    reviewed_at         TEXT,
    assigned_to         TEXT    DEFAULT NULL,
    submitted_by        TEXT,
    submitted_at        TEXT,
    locked_by           TEXT,
    locked_at           TEXT,
    created_at          TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audio_segments (
    s_id                INTEGER,
    seg_id              INTEGER,
    ts_start            REAL,                               -- seconds from session start
    ts_end              REAL,
    speaker             TEXT,                               -- raw diarization label, e.g. 'SPEAKER_1' — never rewritten
    tone                TEXT,
    PRIMARY KEY (s_id, seg_id),
    FOREIGN KEY (s_id) REFERENCES audio_sessions(s_id)
);

CREATE TABLE IF NOT EXISTS audio_flags (
    flag_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    s_id                INTEGER,
    seg_id              INTEGER,
    intent              TEXT,                               -- violation category
    severity            TEXT,                               -- the 's' field of the LLM output
    conf                REAL,
    transcript          TEXT,
    source              TEXT    DEFAULT 'LLM',              -- 'LLM' | 'MANUAL'
    status              TEXT    DEFAULT 'ACTIVE',           -- 'ACTIVE' | 'CONFIRMED'
    parent_flag_id      INTEGER,                            -- set on amendment rows
    created_at          TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (s_id) REFERENCES audio_sessions(s_id)
);

CREATE INDEX IF NOT EXISTS idx_audio_sessions_status ON audio_sessions(review_status);
CREATE INDEX IF NOT EXISTS idx_audio_segments_s_id   ON audio_segments(s_id);
CREATE INDEX IF NOT EXISTS idx_audio_flags_s_id      ON audio_flags(s_id);
CREATE INDEX IF NOT EXISTS idx_audio_flags_intent    ON audio_flags(intent);
