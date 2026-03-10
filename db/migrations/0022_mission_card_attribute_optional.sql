-- Migration 0022: Make card attribute requirement optional for missions
ALTER TABLE mission_templates
    ADD COLUMN IF NOT EXISTS require_card_attribute BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE mission_templates
    ALTER COLUMN requirement_field DROP NOT NULL;
