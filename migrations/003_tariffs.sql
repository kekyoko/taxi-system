-- LR6: planned change "economy"/"comfort" tariffs. Expand, then enable by flag.
-- Existing rides stay on the default tariff. Old clients omit the field safely.
ALTER TABLE rides ADD COLUMN tariff text NOT NULL DEFAULT 'standard'
    CHECK (tariff IN ('standard', 'economy', 'comfort'));
ALTER TABLE rides ADD COLUMN distance_km numeric(6,2) NOT NULL DEFAULT 2.50
    CHECK (distance_km >= 0);
ALTER TABLE rides ADD COLUMN started_at timestamptz;
ALTER TABLE rides ADD COLUMN finished_at timestamptz;

CREATE TABLE feature_flags (
    name        text PRIMARY KEY,
    enabled     boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO feature_flags(name, enabled) VALUES ('tariffs_v2', false)
    ON CONFLICT (name) DO NOTHING;
