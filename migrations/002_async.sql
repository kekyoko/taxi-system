-- LR4: asynchronous operations, transactional outbox, deduplication, history.
ALTER TABLE rides ADD COLUMN payment_status text NOT NULL DEFAULT 'unpaid'
    CHECK (payment_status IN ('unpaid', 'pending', 'paid', 'failed'));

CREATE TABLE operations (
    id           text PRIMARY KEY,
    user_id      text NOT NULL REFERENCES users(id),
    ride_id      text NOT NULL REFERENCES rides(id),
    type         text NOT NULL,
    status       text NOT NULL CHECK (status IN
                    ('pending', 'running', 'succeeded', 'failed')),
    version      integer NOT NULL DEFAULT 1 CHECK (version > 0),
    provider_key text NOT NULL,
    request      jsonb NOT NULL,
    result       jsonb,
    error        jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operations_user_idx ON operations(user_id, created_at DESC);

CREATE TABLE outbox_events (
    id             text PRIMARY KEY,
    channel        text NOT NULL CHECK (channel IN ('domain-events', 'notifications')),
    event_type     text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id   text NOT NULL,
    version        integer NOT NULL DEFAULT 1,
    payload        jsonb NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz
);
CREATE INDEX outbox_unpublished_idx ON outbox_events(channel, published_at, created_at)
    WHERE published_at IS NULL;

CREATE TABLE processed_messages (
    handler      text NOT NULL,
    event_id     text NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (handler, event_id)
);

CREATE TABLE notifications (
    seq         bigserial PRIMARY KEY,
    id          text NOT NULL UNIQUE,
    user_id     text NOT NULL REFERENCES users(id),
    event_type  text NOT NULL,
    payload     jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX notifications_user_seq_idx ON notifications(user_id, seq);

CREATE TABLE ws_tickets (
    ticket      text PRIMARY KEY,
    user_id     text NOT NULL REFERENCES users(id),
    token_hash  text NOT NULL,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);

CREATE TABLE car_positions (
    id          bigserial PRIMARY KEY,
    ride_id     text NOT NULL REFERENCES rides(id),
    lat         numeric(9,6) NOT NULL,
    lon         numeric(9,6) NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fault_hits (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    instance    text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
