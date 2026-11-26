-- Storage of the external payment gateway simulator (separate schema).
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE IF NOT EXISTS sim.provider_effects (
    provider_key text PRIMARY KEY,
    status       text NOT NULL CHECK (status IN ('succeeded', 'declined')),
    response     jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
