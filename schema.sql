-- Every invariant that can be a constraint IS a constraint.
-- Application code only ever asserts on `rowcount` of conditional UPDATEs.

CREATE TABLE products (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents >= 0),
    inventory   INTEGER NOT NULL CHECK (inventory >= 0)   -- INV-1: never oversell
);

CREATE TABLE carts (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'checked_out')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE cart_items (
    cart_id    TEXT    NOT NULL REFERENCES carts(id),
    product_id TEXT    NOT NULL REFERENCES products(id),  -- INV-7: no phantom products
    quantity   INTEGER NOT NULL CHECK (quantity > 0),     -- INV-7: no zero/negative qty
    PRIMARY KEY (cart_id, product_id)
);

CREATE TABLE coupons (
    code              TEXT    PRIMARY KEY,
    percent_off       INTEGER NOT NULL CHECK (percent_off > 0 AND percent_off <= 100),
    milestone         INTEGER NOT NULL UNIQUE,  -- INV-3: one coupon per milestone, ever
    redeemed_order_id TEXT    UNIQUE REFERENCES orders(id),  -- INV-4: redeemed once;
                                                             -- UNIQUE also caps orders at 1 coupon
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- An order is a two-phase state machine, because the payment provider is a
-- network call that must NOT happen inside a database transaction:
--
--   pending_payment --(provider captured)------------------> paid
--                   \--(provider declined, or voided it)---> payment_failed
--
-- Inventory and the coupon are claimed on entry to `pending_payment`, so
-- nothing can be oversold or double-redeemed while a charge is in flight.
-- Only `paid` counts as a successfully placed order.
--
-- `resolution` records WHY the order left `pending_payment`, and is what makes
-- INV-9 below enforceable: we may only write off an order once the provider has
-- told us the money can never arrive.
CREATE TABLE orders (
    id             TEXT    PRIMARY KEY,
    cart_id        TEXT    NOT NULL REFERENCES carts(id),
    status         TEXT    NOT NULL
                    CHECK (status IN ('pending_payment', 'paid', 'payment_failed')),
    subtotal_cents INTEGER NOT NULL CHECK (subtotal_cents >= 0),
    discount_cents INTEGER NOT NULL CHECK (discount_cents >= 0),
    total_cents    INTEGER NOT NULL CHECK (total_cents >= 0),     -- INV-8: never negative
    coupon_code    TEXT    REFERENCES coupons(code),
    payment_ref    TEXT,
    resolution     TEXT    CHECK (resolution IN ('captured', 'declined', 'voided')),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (total_cents = subtotal_cents - discount_cents),        -- INV-8: arithmetic holds
    -- INV-9: the whole payment state machine as one constraint. An order is
    -- `paid` exactly when we hold a provider reference, and may only be written
    -- off once the provider has DECLINED or VOIDED the charge -- never because
    -- a status check happened to come back empty. Compensating on a guess is
    -- how a provider ends up holding money for an order nobody will honour,
    -- so it is not merely discouraged here, it is unrepresentable.
    --
    -- COALESCE is load-bearing, not defensive noise: a CHECK is satisfied when
    -- its expression is NULL, not only when it is true. Written as
    -- `resolution IN ('declined','voided')`, a NULL resolution makes the whole
    -- disjunction NULL and the constraint silently passes -- which is exactly
    -- the row it exists to reject.
    CHECK (
        (status = 'pending_payment'
             AND resolution IS NULL AND payment_ref IS NULL)
     OR (status = 'paid'
             AND COALESCE(resolution, '') = 'captured' AND payment_ref IS NOT NULL)
     OR (status = 'payment_failed'
             AND COALESCE(resolution, '') IN ('declined', 'voided')
             AND payment_ref IS NULL)
    )
);

-- INV-2: a cart yields at most one LIVE order. A compensated attempt stays on
-- the books as an audit record and does not block a legitimate retry.
CREATE UNIQUE INDEX one_live_order_per_cart
    ON orders (cart_id) WHERE status <> 'payment_failed';

CREATE INDEX orders_pending ON orders (created_at) WHERE status = 'pending_payment';

-- INV-10: an order explains itself forever. Snapshot with deliberately no FK
-- to products, so it survives the product being repriced, renamed or deleted.
CREATE TABLE order_items (
    order_id         TEXT    NOT NULL REFERENCES orders(id),
    product_id       TEXT    NOT NULL,
    product_name     TEXT    NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents INTEGER NOT NULL,
    PRIMARY KEY (order_id, product_id),
    CHECK (line_total_cents = unit_price_cents * quantity)
);

-- INV-6: a retried checkout replays, it does not re-execute.
-- The key is scoped to its cart, not global. A global namespace let any caller
-- permanently claim an obvious key ("1", "retry") and make every later honest
-- use of it fail; it also meant one client reusing a key across two of its own
-- carts -- two genuinely different operations -- was rejected rather than
-- honoured. Scoping fixes both, and `fingerprint` still catches the case that
-- matters: the same key replayed against the same cart with a different coupon.
CREATE TABLE idempotency (
    cart_id       TEXT    NOT NULL REFERENCES carts(id),
    key           TEXT    NOT NULL,
    fingerprint   TEXT    NOT NULL,
    order_id      TEXT,
    status_code   INTEGER NOT NULL,   -- 0 while the charge is still in flight
    response_json TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (cart_id, key)
);

INSERT INTO products (id, name, price_cents, inventory) VALUES
    ('p_keyboard', 'Mechanical Keyboard',    8999,  25),
    ('p_mouse',    'Wireless Mouse',         3450,  40),
    ('p_monitor',  '27-inch 4K Monitor',    32999,   7),
    ('p_cable',    'USB-C Cable',             999, 100),
    ('p_dock',     'Limited Edition Dock',  14999,   1);  -- limited stock, on purpose
