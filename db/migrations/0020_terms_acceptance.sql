-- Track which terms version each user has accepted
CREATE TABLE IF NOT EXISTS terms_acceptances (
    user_id BIGINT NOT NULL,
    terms_version VARCHAR(20) NOT NULL,
    accepted_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, terms_version)
);

-- Temporary holding area for OAuth data while user reviews terms
CREATE TABLE IF NOT EXISTS pending_logins (
    token VARCHAR(64) PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username VARCHAR(255) NOT NULL,
    discriminator VARCHAR(10) NOT NULL DEFAULT '0',
    avatar VARCHAR(255),
    access_token TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '15 minutes'
);
