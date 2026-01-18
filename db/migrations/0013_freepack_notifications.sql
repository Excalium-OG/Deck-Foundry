-- DeckForge Free Pack Notification System v0013
-- Track user preferences for free pack cooldown notifications

CREATE TABLE IF NOT EXISTS user_freepack_notifications (
    user_id BIGINT NOT NULL,
    deck_id INTEGER NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    enabled BOOLEAN DEFAULT TRUE,
    last_notified_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (user_id, deck_id)
);

CREATE INDEX IF NOT EXISTS idx_freepack_notifications_user ON user_freepack_notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_freepack_notifications_deck ON user_freepack_notifications(deck_id);
