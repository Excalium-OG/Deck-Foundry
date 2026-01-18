-- Migration: General inventory system (deck-scoped)
-- Replaces user_packs with a flexible, deck-scoped inventory

CREATE TABLE IF NOT EXISTS user_inventory (
    user_id BIGINT NOT NULL,
    deck_id INT NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    quantity INT NOT NULL DEFAULT 0,
    metadata JSONB DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, deck_id, item_type, item_key),
    CONSTRAINT positive_quantity CHECK (quantity >= 0)
);

CREATE INDEX IF NOT EXISTS idx_user_inventory_user_deck ON user_inventory(user_id, deck_id);
CREATE INDEX IF NOT EXISTS idx_user_inventory_type ON user_inventory(user_id, deck_id, item_type);
