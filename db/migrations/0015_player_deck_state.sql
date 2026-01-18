-- DeckForge Migration v0015
-- Per-deck credits and free pack cooldowns

-- Player deck-specific state (credits and cooldowns per deck)
CREATE TABLE IF NOT EXISTS player_deck_state (
    user_id BIGINT NOT NULL,
    deck_id INT NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    credits BIGINT DEFAULT 0,
    last_drop_ts TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    PRIMARY KEY (user_id, deck_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_player_deck_state_user ON player_deck_state(user_id);
CREATE INDEX IF NOT EXISTS idx_player_deck_state_deck ON player_deck_state(deck_id);

-- Migrate existing player data to deck-scoped state
-- For each user with activity, create deck state records with their global credits/cooldown
INSERT INTO player_deck_state (user_id, deck_id, credits, last_drop_ts)
SELECT DISTINCT 
    p.user_id,
    activity.deck_id,
    p.credits,
    p.last_drop_ts
FROM players p
JOIN (
    -- Get all decks each user has interacted with
    SELECT DISTINCT uc.user_id, c.deck_id
    FROM user_cards uc
    JOIN cards c ON uc.card_id = c.card_id
    WHERE c.deck_id IS NOT NULL
    
    UNION
    
    SELECT DISTINCT am.accepted_by as user_id, am.deck_id
    FROM active_missions am
    WHERE am.accepted_by IS NOT NULL AND am.deck_id IS NOT NULL
    
    UNION
    
    SELECT DISTINCT ufn.user_id, ufn.deck_id
    FROM user_freepack_notifications ufn
) activity ON p.user_id = activity.user_id
ON CONFLICT (user_id, deck_id) DO NOTHING;
