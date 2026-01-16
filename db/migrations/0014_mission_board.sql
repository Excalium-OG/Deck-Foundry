-- DeckForge Mission Board Migration v0014
-- Replaces hourly server-based spawning with deck-based mission boards

-- Mission board slots: 10 missions per deck (3 visible + 7 backlog)
CREATE TABLE IF NOT EXISTS mission_board_slots (
    slot_id SERIAL PRIMARY KEY,
    deck_id INTEGER NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    mission_template_id INTEGER NOT NULL REFERENCES mission_templates(mission_template_id) ON DELETE CASCADE,
    slot_position INTEGER NOT NULL CHECK (slot_position >= 1 AND slot_position <= 10),
    rarity_rolled VARCHAR(20) NOT NULL,
    requirement_rolled FLOAT NOT NULL,
    reward_rolled INTEGER NOT NULL,
    duration_rolled_hours INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(deck_id, slot_position)
);

-- Track the last mission board message per guild for a deck
CREATE TABLE IF NOT EXISTS mission_board_messages (
    guild_id BIGINT NOT NULL,
    deck_id INTEGER NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (guild_id, deck_id)
);

-- Track last backlog refill time per deck
ALTER TABLE decks ADD COLUMN IF NOT EXISTS last_mission_refill TIMESTAMP WITH TIME ZONE;

-- Index for efficient slot queries
CREATE INDEX IF NOT EXISTS idx_mission_board_slots_deck ON mission_board_slots(deck_id, slot_position);

-- Update active_missions to track deck-based missions (guild_id becomes optional for tracking)
-- We keep guild_id for knowing which server the user accepted from
ALTER TABLE active_missions ALTER COLUMN channel_id DROP NOT NULL;
ALTER TABLE active_missions ALTER COLUMN message_id DROP NOT NULL;

-- Add slot_id reference to track which board slot this came from
ALTER TABLE active_missions ADD COLUMN IF NOT EXISTS board_slot_id INTEGER REFERENCES mission_board_slots(slot_id) ON DELETE SET NULL;

-- Update user_missions to track player's mission slots (max 3)
-- The existing table already supports this, we just need to enforce the limit in code
