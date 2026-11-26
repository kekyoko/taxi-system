-- LR1: domain model, types, keys and constraints.
CREATE TABLE users (
    id          text PRIMARY KEY,
    login       text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role        text NOT NULL CHECK (role IN ('user', 'staff')),
    district    text NOT NULL DEFAULT 'central',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    token_hash  text PRIMARY KEY,
    user_id     text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_idx ON sessions(user_id);

CREATE TABLE drivers (
    id          text PRIMARY KEY,
    district    text NOT NULL,
    status      text NOT NULL CHECK (status IN ('online', 'busy', 'offline')),
    version     integer NOT NULL DEFAULT 1 CHECK (version > 0),
    lat         numeric(9,6),
    lon         numeric(9,6),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX drivers_district_status_idx ON drivers(district, status, id);

CREATE TABLE rides (
    id              text PRIMARY KEY,
    user_id         text NOT NULL REFERENCES users(id),
    district        text NOT NULL,
    pickup          text NOT NULL,
    driver_id       text REFERENCES drivers(id),
    status          text NOT NULL CHECK (status IN
                        ('requested', 'assigned', 'riding', 'finished', 'cancelled')),
    fare_minor      bigint NOT NULL CHECK (fare_minor >= 0),
    currency        char(3) NOT NULL DEFAULT 'UAH',
    version         integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    accept_deadline timestamptz
);
CREATE INDEX rides_user_created_idx ON rides(user_id, created_at DESC, id DESC);
CREATE INDEX rides_status_idx ON rides(status);

CREATE TABLE idempotency_records (
    user_id        text NOT NULL REFERENCES users(id),
    operation      text NOT NULL,
    key            text NOT NULL,
    request_hash   text NOT NULL,
    status         text NOT NULL CHECK (status IN ('processing', 'done')),
    response_code  integer,
    response_body  jsonb,
    ride_id        text,
    expires_at     timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, operation, key)
);
CREATE INDEX idempotency_expiry_idx ON idempotency_records(expires_at);
