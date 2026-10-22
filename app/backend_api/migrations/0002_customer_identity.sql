-- Link customers to their Cognito identity, move KYC to the customer, and
-- support several linked accounts per customer.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS cognito_sub VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS customers_cognito_sub_key ON customers (cognito_sub);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS kyc_status VARCHAR(32) NOT NULL DEFAULT 'PENDING';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS kyc_document_key TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS kyc_verified_at TIMESTAMPTZ;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS alert_subscription_arn TEXT;

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS account_type VARCHAR(16) NOT NULL DEFAULT 'EVERYDAY';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS nickname VARCHAR(60);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS currency CHAR(3) NOT NULL DEFAULT 'SGD';
CREATE INDEX IF NOT EXISTS accounts_user_id_idx ON accounts (user_id);
