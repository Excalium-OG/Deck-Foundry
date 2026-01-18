-- Migration: Add credits to trades
-- Allows players to offer credits as part of trades

ALTER TABLE trades ADD COLUMN IF NOT EXISTS credits_initiator INT DEFAULT 0;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS credits_responder INT DEFAULT 0;

COMMENT ON COLUMN trades.credits_initiator IS 'Credits offered by the trade initiator';
COMMENT ON COLUMN trades.credits_responder IS 'Credits offered by the trade responder';
