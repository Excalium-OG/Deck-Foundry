-- Migration 0023: Per-template mission acceptance cost configuration
ALTER TABLE mission_templates
    ADD COLUMN IF NOT EXISTS has_acceptance_cost BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS acceptance_cost_multiplier FLOAT NOT NULL DEFAULT 0.05;
