-- Active provider defaults for new calls. Existing provider values remain historical.
ALTER TABLE calls ALTER COLUMN provider SET DEFAULT 'twilio';
ALTER TABLE calls ADD CONSTRAINT calls_provider_check CHECK (provider IN ('twilio', 'asterisk'));
