CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  shortcode TEXT NOT NULL UNIQUE,
  kind TEXT,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | done | failed
  added_at TEXT NOT NULL,
  claimed_at TEXT,
  done_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  title TEXT,
  note_md TEXT,
  note_path TEXT,
  topic TEXT
);

CREATE INDEX IF NOT EXISTS idx_links_status ON links (status, added_at);
