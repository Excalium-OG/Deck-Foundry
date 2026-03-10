ALTER TABLE decks ADD COLUMN IF NOT EXISTS disabled BOOLEAN DEFAULT FALSE;
ALTER TABLE decks ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;
ALTER TABLE decks ADD COLUMN IF NOT EXISTS disabled_by BIGINT;

CREATE TABLE IF NOT EXISTS admin_audit_log (
    audit_id SERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    deck_id INTEGER REFERENCES decks(deck_id),
    deck_name TEXT,
    performed_by BIGINT NOT NULL,
    performed_by_username TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
