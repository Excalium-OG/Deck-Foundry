-- DeckForge Migration v0018
-- Mission Points (MP) system for leaderboard rewards

-- Add mission_points column to player_deck_state
ALTER TABLE player_deck_state 
ADD COLUMN IF NOT EXISTS mission_points BIGINT DEFAULT 0;

-- Track monthly reward distributions
CREATE TABLE IF NOT EXISTS monthly_rewards_log (
    id SERIAL PRIMARY KEY,
    deck_id INT NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    month_year VARCHAR(7) NOT NULL,  -- Format: 'YYYY-MM'
    distributed_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    first_place_user_id BIGINT,
    first_place_mp BIGINT,
    second_place_user_id BIGINT,
    second_place_mp BIGINT,
    third_place_user_id BIGINT,
    third_place_mp BIGINT,
    UNIQUE(deck_id, month_year)
);

-- Index for looking up leaderboard
CREATE INDEX IF NOT EXISTS idx_player_deck_state_mp ON player_deck_state(deck_id, mission_points DESC);
