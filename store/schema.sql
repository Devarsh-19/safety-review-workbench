-- SQLite schema for AstroTalk content safety results store

CREATE TABLE IF NOT EXISTS sessions (
    session_id          TEXT    PRIMARY KEY,
    astrologer_id       TEXT,
    user_id             TEXT,
    session_start       TEXT,
    session_end         TEXT,
    duration_minutes    REAL,
    session_type        TEXT,                               -- 'chat' or 'voice'
    session_date        TEXT,
    month               TEXT,
    language_code       TEXT,
    language_detected   TEXT,
    overall_verdict     TEXT,                               -- 'CLEAN', 'FLAGGED', 'SEVERE'
    confidence_score    REAL,
    astrotalk_flagged   INTEGER,                            -- 0 or 1
    astrotalk_flag_category TEXT,
    astrotalk_severity  TEXT,
    review_status       TEXT    DEFAULT 'PENDING',          -- 'PENDING', 'SUBMITTED_FOR_REVIEW', 'NEEDS_FINAL_REVIEW', 'LOCKED', 'REVIEWED', 'CONFIRMED', 'OVERRIDDEN'
    reviewer_id         TEXT,
    reviewer_note       TEXT,
    reviewed_at         TEXT,
    session_note        TEXT,
    locked_by           TEXT,
    locked_at           TEXT,
    submitted_by        TEXT,                               -- L1 reviewer who submitted for final review
    submitted_at        TEXT,                               -- timestamp of submission
    needs_final_review  INTEGER DEFAULT 0,                  -- 1 if marked by L2 for final review
    assigned_to         TEXT    DEFAULT NULL,               -- L1 reviewer assigned to this session
    created_at          TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id             INTEGER,
    session_id          TEXT,
    speaker             TEXT,                               -- 'ASTROLOGER' or 'USER'
    message_text        TEXT,
    is_automated        INTEGER DEFAULT 0,
    timestamp           TEXT,
    language_detected   TEXT,
    has_link            INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, turn_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS flags (
    flag_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT,
    turn_id             INTEGER,
    category_code       TEXT,                               -- e.g. 'OFF_PLATFORM', 'NSFW', 'FEAR_MANIPULATION'
    detection_layer     TEXT,                               -- 'REGEX', 'LLM', 'MANUAL', 'AMENDED', 'DISMISSED'
    severity            TEXT,                               -- 'LOW', 'MEDIUM', 'HIGH'
    confidence_score    REAL,
    reasoning           TEXT,
    false_positive_risk TEXT,                               -- 'LOW', 'MEDIUM', 'HIGH'
    pattern_matched     TEXT,                               -- for regex/manual layer: the pattern or message text
    confirmed_by        TEXT,                               -- reviewer_id who confirmed this flag
    confirmed_at        TEXT,                               -- timestamp of confirmation
    is_confirmed        INTEGER DEFAULT 0,                  -- 1 if confirmed by a reviewer
    parent_flag_id      INTEGER DEFAULT NULL,               -- flag_id of the original parent for AMENDED/DISMISSED children; NULL for LLM/REGEX/MANUAL parents
    created_at          TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (session_id)    REFERENCES sessions(session_id),
    FOREIGN KEY (parent_flag_id) REFERENCES flags(flag_id)
);

CREATE TABLE IF NOT EXISTS review_log (
    log_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT,
    flag_id             INTEGER,
    action              TEXT,                               -- 'CONFIRM', 'FALSE_POSITIVE', 'CLEAR', 'SUBMIT', 'MANUAL_FLAG', 'FLAG_DISMISSED'
    reviewer_id         TEXT,
    note                TEXT,
    actioned_at         TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_sessions_verdict        ON sessions(overall_verdict);
CREATE INDEX IF NOT EXISTS idx_sessions_review_status  ON sessions(review_status);
CREATE INDEX IF NOT EXISTS idx_sessions_language       ON sessions(language_detected);
CREATE INDEX IF NOT EXISTS idx_flags_session_id        ON flags(session_id);
CREATE INDEX IF NOT EXISTS idx_flags_category_code     ON flags(category_code);
CREATE INDEX IF NOT EXISTS idx_flags_parent_flag_id    ON flags(parent_flag_id);
CREATE INDEX IF NOT EXISTS idx_sessions_assigned_to    ON sessions(assigned_to);
