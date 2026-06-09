PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  project     TEXT,
  started_at  INTEGER,
  last_seen   INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  seq         INTEGER NOT NULL,
  ts          INTEGER NOT NULL,
  role        TEXT NOT NULL,
  text        TEXT,
  tool_name   TEXT,
  tool_input  TEXT,
  tool_result TEXT,
  is_error    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='rowid', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS commands (
  id            INTEGER PRIMARY KEY,
  source        TEXT NOT NULL,
  external_id   TEXT,
  ts            INTEGER NOT NULL,
  shell         TEXT,
  cwd           TEXT,
  hostname      TEXT,
  command       TEXT NOT NULL,
  exit_status   INTEGER,
  duration_ms   INTEGER,
  author        TEXT,                 -- atuin author tag: '' / 'claude-code' / etc.
  intent        TEXT,                 -- atuin intent (the agent's description, when set)
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_commands_ts ON commands(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS commands_fts USING fts5(
  command, content='commands', content_rowid='id', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS commands_ai AFTER INSERT ON commands BEGIN
  INSERT INTO commands_fts(rowid, command) VALUES (new.id, new.command);
END;
CREATE TRIGGER IF NOT EXISTS commands_ad AFTER DELETE ON commands BEGIN
  INSERT INTO commands_fts(commands_fts, rowid, command) VALUES ('delete', old.id, old.command);
END;
CREATE TRIGGER IF NOT EXISTS commands_au AFTER UPDATE ON commands BEGIN
  INSERT INTO commands_fts(commands_fts, rowid, command) VALUES ('delete', old.id, old.command);
  INSERT INTO commands_fts(rowid, command) VALUES (new.id, new.command);
END;

CREATE TABLE IF NOT EXISTS recommendations (
  id           TEXT PRIMARY KEY,
  scope        TEXT NOT NULL,
  title        TEXT NOT NULL,
  signature    TEXT NOT NULL,
  body_path    TEXT NOT NULL,
  evidence     TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'logged',
  -- Free-text reason captured when a status flips away from 'logged'
  -- (e.g. why a rec was dismissed/rejected). Fed back into the analyze
  -- prompt so the LLM stops re-proposing variants of rejected ideas.
  reason       TEXT,
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recs_status ON recommendations(status, created_at DESC);

CREATE TABLE IF NOT EXISTS cursors (
  source       TEXT PRIMARY KEY,
  position     TEXT NOT NULL,
  updated_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS redaction_failures (
  id           INTEGER PRIMARY KEY,
  ts           INTEGER NOT NULL,
  source       TEXT NOT NULL,
  reason       TEXT NOT NULL
);
