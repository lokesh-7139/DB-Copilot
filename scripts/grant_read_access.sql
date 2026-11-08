-- Run as a database administrator in the database used by DATABASE_URL.
-- This user has no login; the app connects using its existing Windows identity.
IF DATABASE_PRINCIPAL_ID(N'copilot_reader') IS NULL
    CREATE USER [copilot_reader] WITHOUT LOGIN;
GRANT SELECT, VIEW DEFINITION TO [copilot_reader];
DENY INSERT, UPDATE, DELETE, EXECUTE TO [copilot_reader];
