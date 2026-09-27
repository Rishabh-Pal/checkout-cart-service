"""Persistence and every transactional invariant.

Concurrency model (single instance): SQLite in WAL mode. Every mutating step
runs inside a `BEGIN IMMEDIATE` transaction, which takes the database write
lock up front, so writers serialise and readers never block. Correctness never
depends on that serialisation: each invariant is a conditional UPDATE whose
`rowcount` we assert, or a UNIQUE/CHECK constraint. The same statements are
correct on Postgres under READ COMMITTED with no application locking.

Checkout is two transactions, not one, because the payment provider is a
network call and a network call inside a database transaction is a lost-money
bug waiting to happen -- it can succeed at the provider while the local
transaction rolls back. So:

    txn 1  reserve   claim idempotency key, spend the cart, reserve stock,
                     claim the coupon, write the order as `pending_payment`
    ---    charge    provider call, outside any transaction, order id as the
                     provider's idempotency key
    txn 2  settle    `pending_payment` -> `paid`, record the reference
           or
           compensate `pending_payment` -> `payment_failed`, return the stock,
                     release the coupon, reopen the cart

Anything that interrupts the middle step leaves a durable `pending_payment`
row, which `reconcile_pending_orders` resolves. It resolves it by *voiding* the
charge at the provider, not by asking whether it was captured: an answer of
"not captured" describes a moment that has already passed, and writing an order
off on the strength of it is how a provider ends up holding money for an order
nobody will honour. A void is terminal, so once it succeeds compensation is
safe forever. If the void cannot be performed, nothing is decided and the order
stays reserved. Nothing is ever guessed.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import payments

DB_PATH = os.environ.get("CHECKOUT_DB", "checkout.db")
MILESTONE_EVERY_N = int(os.environ.get("MILESTONE_EVERY_N", "5"))   # n
COUPON_PERCENT_OFF = int(os.environ.get("COUPON_PERCENT_OFF", "10"))  # x

# How long a database writer waits for the lock. One value, two units.
BUSY_TIMEOUT_SECONDS = 30
# Default grace period before reconciliation will touch a pending order. The
# HTTP layer defaults to this too, so the two cannot drift.
STALE_AFTER_SECONDS = 30

SCHEMA = Path(__file__).with_name("schema.sql")


class ApiError(Exception):
    """Domain failure with a stable machine-readable code."""

    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------

def connect():
    conn = sqlite3.connect(DB_PATH, timeout=float(BUSY_TIMEOUT_SECONDS),
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_SECONDS * 1000}")
    return conn


def init_db(path=None, reset=False):
    """Create the database if absent. Idempotent; safe to call at every boot."""
    global DB_PATH
    if path:
        DB_PATH = path
    if reset:
        for suffix in ("", "-wal", "-shm"):
            Path(DB_PATH + suffix).unlink(missing_ok=True)
    fresh = not Path(DB_PATH).exists()
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        if fresh:
            conn.executescript(SCHEMA.read_text())
    finally:
        conn.close()


@contextmanager
def read_txn():
    """Deferred transaction: one consistent snapshot, mutates nothing."""
    conn = connect()
    try:
        conn.execute("BEGIN DEFERRED")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _safe_rollback(conn)
        raise
    finally:
        conn.close()


@contextmanager
def write_txn():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _safe_rollback(conn)
        raise
    finally:
        conn.close()


def _safe_rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------

def discount_cents_for(subtotal_cents, percent_off):
    """Integer minor units only; no float ever touches money.

    Floor division: the discount is rounded down, so it can never exceed the
    subtotal and the total can never go negative. Deterministic for a given
    (subtotal, percent) pair regardless of line ordering, because it is
    applied once to the order subtotal rather than per line.
    """
    return subtotal_cents * percent_off // 100


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------

def list_products():
    with read_txn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, name, price_cents, inventory FROM products ORDER BY id")]


# --------------------------------------------------------------------------
# carts
# --------------------------------------------------------------------------

def create_cart():
    cart_id = "cart_" + uuid.uuid4().hex[:16]
    with write_txn() as c:
        c.execute("INSERT INTO carts (id) VALUES (?)", (cart_id,))
    return get_cart(cart_id)


def _cart_row(c, cart_id):
    row = c.execute("SELECT id, status FROM carts WHERE id = ?", (cart_id,)).fetchone()
    if row is None:
        raise ApiError(404, "CART_NOT_FOUND", f"No cart with id {cart_id!r}.")
    return row


def _require_open_cart(c, cart_id):
    row = _cart_row(c, cart_id)
    if row["status"] != "open":
        raise ApiError(409, "CART_ALREADY_CHECKED_OUT",
                       "This cart has already been checked out and is immutable.",
                       order_id=_live_order_id_for_cart(c, cart_id))
    return row


def _live_order_id_for_cart(c, cart_id):
    """The order currently holding this cart. Compensated attempts do not count."""
    row = c.execute("SELECT id FROM orders WHERE cart_id = ? "
                    "AND status <> 'payment_failed'", (cart_id,)).fetchone()
    return row["id"] if row else None


def _cart_view(c, cart_id):
    """Cart lines are priced LIVE, from the product table, on every read.

    A cart stores only (product_id, quantity). It never snapshots price, so a
    repricing between add and checkout is visible immediately and the customer
    is charged the price shown at checkout time. `in_stock` is advisory: stock
    is only truly reserved inside checkout.
    """
    row = _cart_row(c, cart_id)
    items, subtotal = [], 0
    for r in c.execute(
        """SELECT i.product_id, i.quantity, p.name, p.price_cents, p.inventory
             FROM cart_items i JOIN products p ON p.id = i.product_id
            WHERE i.cart_id = ? ORDER BY i.product_id""", (cart_id,)):
        line_total = r["price_cents"] * r["quantity"]
        subtotal += line_total
        items.append({
            "product_id": r["product_id"],
            "product_name": r["name"],
            "quantity": r["quantity"],
            "unit_price_cents": r["price_cents"],
            "line_total_cents": line_total,
            "available_inventory": r["inventory"],
            "in_stock": r["inventory"] >= r["quantity"],
        })
    return {
        "id": row["id"],
        "status": row["status"],
        "items": items,
        "subtotal_cents": subtotal,
        "order_id": _live_order_id_for_cart(c, cart_id),
    }


def get_cart(cart_id):
    with read_txn() as c:
        return _cart_view(c, cart_id)


def _require_product(c, product_id):
    row = c.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise ApiError(404, "PRODUCT_NOT_FOUND", f"No product with id {product_id!r}.")


def add_item(cart_id, product_id, quantity):
    """Adding a product already in the cart increases its quantity."""
    with write_txn() as c:
        _require_open_cart(c, cart_id)
        _require_product(c, product_id)
        c.execute(
            """INSERT INTO cart_items (cart_id, product_id, quantity) VALUES (?, ?, ?)
               ON CONFLICT (cart_id, product_id)
               DO UPDATE SET quantity = quantity + excluded.quantity""",
            (cart_id, product_id, quantity))
        return _cart_view(c, cart_id)


def set_item_quantity(cart_id, product_id, quantity):
    with write_txn() as c:
        _require_open_cart(c, cart_id)
        _require_product(c, product_id)
        cur = c.execute(
            "UPDATE cart_items SET quantity = ? WHERE cart_id = ? AND product_id = ?",
            (quantity, cart_id, product_id))
        if cur.rowcount == 0:
            raise ApiError(404, "CART_ITEM_NOT_FOUND",
                           f"Product {product_id!r} is not in this cart.")
        return _cart_view(c, cart_id)


def remove_item(cart_id, product_id):
    with write_txn() as c:
        _require_open_cart(c, cart_id)
        cur = c.execute("DELETE FROM cart_items WHERE cart_id = ? AND product_id = ?",
                        (cart_id, product_id))
        if cur.rowcount == 0:
            raise ApiError(404, "CART_ITEM_NOT_FOUND",
                           f"Product {product_id!r} is not in this cart.")


# --------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------

def _order_views(c, where_sql="", args=()):
    """Build order payloads for every row matching `where_sql`, in 3 queries.

    Batched rather than per-order because `/admin/orders` grows by one row per
    checkout attempt and had no bound: the previous shape ran 2N+1 queries.
    `_order_view` delegates here so the single-order and list responses can
    never drift apart in shape.
    """
    rows = list(c.execute(
        f"""SELECT id, cart_id, status, subtotal_cents, discount_cents, total_cents,
                   coupon_code, payment_ref, resolution, created_at
              FROM orders {where_sql} ORDER BY created_at, id""", args))
    if not rows:
        return []

    order_ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(order_ids))
    items = {}
    for r in c.execute(
        f"""SELECT order_id, product_id, product_name, unit_price_cents,
                   quantity, line_total_cents
              FROM order_items WHERE order_id IN ({placeholders})
             ORDER BY order_id, product_id""", order_ids):
        items.setdefault(r["order_id"], []).append(
            {k: r[k] for k in r.keys() if k != "order_id"})

    codes = [r["coupon_code"] for r in rows if r["coupon_code"]]
    coupons = {}
    if codes:
        coupons = {r["code"]: {"code": r["code"], "percent_off": r["percent_off"]}
                   for r in c.execute(
                       "SELECT code, percent_off FROM coupons "
                       f"WHERE code IN ({','.join('?' * len(codes))})", codes)}

    return [{
        "id": r["id"],
        "cart_id": r["cart_id"],
        "status": r["status"],
        "items": items.get(r["id"], []),
        "subtotal_cents": r["subtotal_cents"],
        "coupon": coupons.get(r["coupon_code"]),
        "discount_cents": r["discount_cents"],
        "total_cents": r["total_cents"],
        "payment_ref": r["payment_ref"],
        "resolution": r["resolution"],
        "created_at": r["created_at"],
    } for r in rows]


def _order_view(c, order_id):
    found = _order_views(c, "WHERE id = ?", (order_id,))
    if not found:
        raise ApiError(404, "ORDER_NOT_FOUND", f"No order with id {order_id!r}.")
    return found[0]


def get_order(order_id):
    with read_txn() as c:
        return _order_view(c, order_id)


def _fingerprint(coupon_code):
    """What makes two uses of one key the *same* request. The cart is no longer
    part of it: the key is now scoped to its cart by the primary key, so the
    only way to reuse a key with different intent is to change the coupon."""
    return hashlib.sha256(f"{coupon_code or ''}".encode()).hexdigest()


# --------------------------------------------------------------------------
# checkout: reserve -> charge -> settle | compensate
# --------------------------------------------------------------------------

def checkout(cart_id, coupon_code=None, idempotency_key=None):
    """Place an order. Returns (order, replayed: bool).

    The provider call sits between two transactions and inside neither, which
    is the whole point: a captured charge can never be paired with a rolled
    back order, and an ambiguous result is never guessed at.
    """
    replay, order_id, amount_cents = _reserve(cart_id, coupon_code, idempotency_key)
    if replay is not None:
        return replay, True

    try:
        reference = payments.charge(order_id, amount_cents)
    except payments.PaymentDeclined as exc:
        # Definitive: no money moved and none ever will. Safe to undo at once.
        _compensate(order_id, "declined")
        raise ApiError(402, "PAYMENT_FAILED",
                       str(exc) or "The payment was declined.",
                       order_id=order_id) from exc
    except Exception as exc:
        # NOT definitive. The charge may have been captured. Leave the order
        # `pending_payment` and let reconciliation void it at the provider --
        # undoing here would risk releasing stock for an order that was paid.
        raise ApiError(503, "PAYMENT_RESULT_UNKNOWN",
                       "The payment result is unknown and is being reconciled. "
                       "Poll the order; do not retry the checkout.",
                       order_id=order_id) from exc

    order = _settle(order_id, reference)
    if order["status"] != "paid":
        # A concurrent reconciliation compensated this order first.
        raise ApiError(402, "PAYMENT_FAILED", "The payment did not complete.",
                       order_id=order_id)
    return order, False


def _reserve(cart_id, coupon_code, idempotency_key):
    """Transaction 1: claim everything, write the order as `pending_payment`.

    Returns (replayed_order_or_None, order_id, amount_cents).
    """
    fingerprint = _fingerprint(coupon_code)

    with write_txn() as c:
        # --- INV-6: retry replays, it does not re-execute -------------------
        if idempotency_key:
            prior = c.execute(
                "SELECT fingerprint, order_id, status_code, response_json "
                "FROM idempotency WHERE cart_id = ? AND key = ?",
                (cart_id, idempotency_key)).fetchone()
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise ApiError(
                        409, "IDEMPOTENCY_KEY_REUSED",
                        "This Idempotency-Key was already used on this cart "
                        "for a different request.",
                        idempotency_key=idempotency_key)
                if prior["status_code"]:
                    return json.loads(prior["response_json"]), None, None
                # The original attempt's charge is still in flight. We cannot
                # answer with the final result because it does not exist yet,
                # and we must not start a second charge.
                raise ApiError(
                    409, "CHECKOUT_IN_PROGRESS",
                    "The original request is still being processed. Poll the "
                    "order, or retry this key shortly.",
                    order_id=prior["order_id"])
            # Claim the key. UNIQUE on `key` is the backstop if a future
            # database lets two of these run concurrently.
            c.execute("INSERT INTO idempotency (cart_id, key, fingerprint, "
                      "status_code, response_json) VALUES (?, ?, ?, 0, '')",
                      (cart_id, idempotency_key, fingerprint))

        # --- INV-2: a cart can be spent exactly once ------------------------
        if c.execute("UPDATE carts SET status = 'checked_out' "
                     "WHERE id = ? AND status = 'open'", (cart_id,)).rowcount == 0:
            _cart_row(c, cart_id)  # raises CART_NOT_FOUND when it never existed
            raise ApiError(409, "CART_ALREADY_CHECKED_OUT",
                           "This cart has already been checked out.",
                           order_id=_live_order_id_for_cart(c, cart_id))

        lines = list(c.execute(
            """SELECT i.product_id, i.quantity, p.name, p.price_cents
                 FROM cart_items i JOIN products p ON p.id = i.product_id
                WHERE i.cart_id = ? ORDER BY i.product_id""", (cart_id,)))
        if not lines:
            raise ApiError(422, "CART_EMPTY", "Cannot check out an empty cart.")

        # --- INV-1: reserve stock with a conditional UPDATE -----------------
        # Ordered by product_id so multi-product checkouts always take rows in
        # the same order (irrelevant under SQLite's single writer, required to
        # avoid deadlocks once this runs on Postgres).
        for ln in lines:
            if c.execute("UPDATE products SET inventory = inventory - ? "
                         "WHERE id = ? AND inventory >= ?",
                         (ln["quantity"], ln["product_id"], ln["quantity"])).rowcount == 0:
                available = c.execute("SELECT inventory FROM products WHERE id = ?",
                                      (ln["product_id"],)).fetchone()["inventory"]
                raise ApiError(409, "OUT_OF_STOCK",
                               f"Only {available} of {ln['name']!r} left.",
                               product_id=ln["product_id"],
                               requested=ln["quantity"], available=available)

        subtotal = sum(ln["price_cents"] * ln["quantity"] for ln in lines)

        discount = 0
        if coupon_code:
            coupon = c.execute("SELECT percent_off, redeemed_order_id FROM coupons "
                               "WHERE code = ?", (coupon_code,)).fetchone()
            if coupon is None:
                raise ApiError(404, "COUPON_NOT_FOUND",
                               f"No coupon with code {coupon_code!r}.")
            if coupon["redeemed_order_id"] is not None:
                raise ApiError(409, "COUPON_ALREADY_REDEEMED",
                               "This coupon has already been redeemed.")
            discount = discount_cents_for(subtotal, coupon["percent_off"])

        order_id = "ord_" + uuid.uuid4().hex[:16]
        c.execute("""INSERT INTO orders (id, cart_id, status, subtotal_cents,
                                         discount_cents, total_cents, coupon_code)
                     VALUES (?, ?, 'pending_payment', ?, ?, ?, ?)""",
                  (order_id, cart_id, subtotal, discount, subtotal - discount,
                   coupon_code))
        c.executemany(
            """INSERT INTO order_items (order_id, product_id, product_name,
                                        unit_price_cents, quantity, line_total_cents)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(order_id, ln["product_id"], ln["name"], ln["price_cents"],
              ln["quantity"], ln["price_cents"] * ln["quantity"]) for ln in lines])

        # --- INV-4: the redemption itself is the race guard -----------------
        # The SELECT above computed the discount; THIS is what makes redemption
        # exclusive. rowcount 0 means someone else took it: roll everything back.
        if coupon_code and c.execute(
                "UPDATE coupons SET redeemed_order_id = ? "
                "WHERE code = ? AND redeemed_order_id IS NULL",
                (order_id, coupon_code)).rowcount == 0:
            raise ApiError(409, "COUPON_ALREADY_REDEEMED",
                           "This coupon has already been redeemed.")

        if idempotency_key:
            c.execute("UPDATE idempotency SET order_id = ? "
                      "WHERE cart_id = ? AND key = ?",
                      (order_id, cart_id, idempotency_key))
        return None, order_id, subtotal - discount


def _settle(order_id, reference):
    """Transaction 2a: `pending_payment` -> `paid`. Exactly once."""
    with write_txn() as c:
        if c.execute("UPDATE orders SET status = 'paid', payment_ref = ?, "
                     "resolution = 'captured' "
                     "WHERE id = ? AND status = 'pending_payment'",
                     (reference, order_id)).rowcount == 0:
            # Already resolved -- by a concurrent settle or by reconciliation.
            return _order_view(c, order_id)
        order = _order_view(c, order_id)
        # Keyed on order_id, not on the request's key, so reconciliation fills
        # this in too and a later retry still gets a proper replay.
        c.execute("UPDATE idempotency SET status_code = 201, response_json = ? "
                  "WHERE order_id = ?", (json.dumps(order), order_id))
        return order


def _compensate(order_id, resolution):
    """Transaction 2b: undo a reservation whose payment can never succeed.

    `resolution` is the provider's verdict and is mandatory: `declined` (it
    refused) or `voided` (we cancelled the charge and it is now terminal). There
    is deliberately no value meaning "a status check came back empty" -- see
    INV-9 in schema.sql. Callers without a verdict must leave the order alone.

    INV-5 lives here: the coupon goes back, so it is never consumed by a
    checkout that does not complete.

    Returns True if this call is the one that performed the compensation. The
    status guard makes it exactly-once, so a concurrent settle and a
    reconciliation sweep cannot both act on the same order.
    """
    if resolution not in ("declined", "voided"):
        raise ValueError(f"refusing to write off an order on {resolution!r}")
    with write_txn() as c:
        if c.execute("UPDATE orders SET status = 'payment_failed', resolution = ? "
                     "WHERE id = ? AND status = 'pending_payment'",
                     (resolution, order_id)).rowcount == 0:
            return False

        for r in c.execute("SELECT product_id, quantity FROM order_items "
                           "WHERE order_id = ?", (order_id,)):
            c.execute("UPDATE products SET inventory = inventory + ? WHERE id = ?",
                      (r["quantity"], r["product_id"]))
        c.execute("UPDATE coupons SET redeemed_order_id = NULL "
                  "WHERE redeemed_order_id = ?", (order_id,))
        cart_id = c.execute("SELECT cart_id FROM orders WHERE id = ?",
                            (order_id,)).fetchone()["cart_id"]
        c.execute("UPDATE carts SET status = 'open' WHERE id = ?", (cart_id,))
        # The failure is not memoised: the same key may legitimately try again.
        c.execute("DELETE FROM idempotency WHERE order_id = ?", (order_id,))
        return True


def reconcile_pending_orders(stale_after_seconds=STALE_AFTER_SECONDS):
    """Resolve orders we lost track of, and never by guessing.

    This is the recovery path for a crash, timeout or deploy between the charge
    and the settle. For each stale `pending_payment` order it asks the provider
    to *void* the charge:

      - the provider reports it had already captured -> settle, the order is paid;
      - the void succeeds -> the charge is terminal and can never be captured,
        so compensation is now safe forever;
      - the provider cannot be reached, or the payment method cannot be voided
        while it is still in flight -> nothing is decided. The order stays
        `pending_payment` with its stock and coupon still held, and the next
        sweep tries again.

    The third branch is the whole point. An earlier version of this function
    asked "did you capture?" and compensated on a no. That answer describes a
    moment that has already passed: a charge not yet captured when we asked can
    be captured immediately afterwards, leaving the provider holding money for
    an order we had written off -- which then needs a refund to put right. A
    void cannot go stale, so that case no longer exists.

    Holding a reservation indefinitely is the correct failure mode here: it
    costs one row and some stock, and it is visible in the report under
    `unsettled`. Releasing it early can cost real money.

    Idempotent, and safe to run beside live traffic: `_settle` and `_compensate`
    are both guarded by the order's status, so only one of them can act.
    """
    with read_txn() as c:
        stale = [r["id"] for r in c.execute(
            "SELECT id FROM orders WHERE status = 'pending_payment' "
            "AND created_at <= datetime('now', ?) ORDER BY created_at, id",
            (f"-{int(stale_after_seconds)} seconds",))]

    settled, compensated, unresolved = [], [], []
    for order_id in stale:
        try:
            reference = payments.void(order_id)
        except payments.PaymentUnavailable:
            unresolved.append(order_id)
            continue
        if reference is not None:
            if _settle(order_id, reference)["status"] == "paid":
                settled.append(order_id)
        elif _compensate(order_id, "voided"):
            compensated.append(order_id)
    return {"examined": len(stale), "settled": settled,
            "compensated": compensated, "unresolved": unresolved}


# --------------------------------------------------------------------------
# coupons (administrative)
# --------------------------------------------------------------------------

def _milestone_state(c):
    # Only a paid order is a successfully placed order.
    orders_placed = c.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE status = 'paid'").fetchone()["n"]
    n = MILESTONE_EVERY_N
    reached = orders_placed // n           # milestone k is earned at order k*n
    granted = {r["milestone"] for r in c.execute("SELECT milestone FROM coupons")}
    pending = next((k for k in range(1, reached + 1) if k not in granted), None)
    return orders_placed, reached, pending


def generate_coupon():
    """Mint the coupon for the lowest reached-but-unrewarded milestone."""
    with write_txn() as c:
        orders_placed, reached, pending = _milestone_state(c)
        if pending is None:
            raise ApiError(
                409, "NO_ELIGIBLE_MILESTONE",
                "Every reached milestone has already been rewarded."
                if reached else "No order milestone has been reached yet.",
                orders_placed=orders_placed,
                milestone_every_n=MILESTONE_EVERY_N,
                next_milestone_at_order=(reached + 1) * MILESTONE_EVERY_N)

        # A coupon is a bearer credential and there is no authentication, so
        # guessing one is worth real money. secrets, not uuid4()[:8] (32 bits),
        # and no milestone number in the visible code.
        code = "REWARD-" + secrets.token_urlsafe(16)
        # INV-3: `milestone` is UNIQUE. Two admins racing cannot both mint for
        # the same milestone even if the read above were to run concurrently.
        c.execute("INSERT INTO coupons (code, percent_off, milestone) VALUES (?, ?, ?)",
                  (code, COUPON_PERCENT_OFF, pending))
        return {
            "code": code,
            "percent_off": COUPON_PERCENT_OFF,
            "milestone": pending,
            "earned_at_order_number": pending * MILESTONE_EVERY_N,
            "redeemed_order_id": None,
        }


def list_coupons():
    with read_txn() as c:
        return [{
            "code": r["code"],
            "percent_off": r["percent_off"],
            "milestone": r["milestone"],
            "earned_at_order_number": r["milestone"] * MILESTONE_EVERY_N,
            "redeemed_order_id": r["redeemed_order_id"],
            "created_at": r["created_at"],
        } for r in c.execute("SELECT * FROM coupons ORDER BY milestone")]


def list_orders(status=None):
    with read_txn() as c:
        if status:
            return _order_views(c, "WHERE status = ?", (status,))
        return _order_views(c)


# --------------------------------------------------------------------------
# reporting (administrative, read-only)
# --------------------------------------------------------------------------

def report():
    """INV-11: one consistent snapshot, no UPDATE, so repeated calls are identical.

    Every revenue figure counts `paid` orders only. Reserved-but-unpaid orders
    are surfaced separately rather than folded in or hidden, so money in flight
    is visible instead of silently missing.
    """
    with read_txn() as c:
        totals = c.execute(
            """SELECT COUNT(*)                          AS orders_placed,
                      COALESCE(SUM(subtotal_cents), 0)  AS gross,
                      COALESCE(SUM(discount_cents), 0)  AS discounts,
                      COALESCE(SUM(total_cents), 0)     AS net
                 FROM orders WHERE status = 'paid'""").fetchone()
        unsettled = c.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(total_cents), 0) AS cents
                 FROM orders WHERE status = 'pending_payment'""").fetchone()
        failed = c.execute("SELECT COUNT(*) AS n FROM orders "
                           "WHERE status = 'payment_failed'").fetchone()["n"]
        coupons = c.execute(
            """SELECT COUNT(*) AS generated,
                      COALESCE(SUM(redeemed_order_id IS NOT NULL), 0) AS redeemed
                 FROM coupons""").fetchone()
        by_product = [{
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "quantity": r["quantity"],
            "gross_revenue_cents": r["gross"],
        } for r in c.execute(
            """SELECT i.product_id, MAX(i.product_name) AS product_name,
                      SUM(i.quantity) AS quantity, SUM(i.line_total_cents) AS gross
                 FROM order_items i JOIN orders o ON o.id = i.order_id
                WHERE o.status = 'paid'
                GROUP BY i.product_id ORDER BY i.product_id""")]
        _, reached, pending = _milestone_state(c)

        return {
            "orders_placed": totals["orders_placed"],
            "purchased_quantity_by_product": by_product,
            "gross_revenue_cents": totals["gross"],
            "total_discounts_cents": totals["discounts"],
            "net_revenue_cents": totals["net"],
            "coupons": {
                "generated": coupons["generated"],
                "redeemed": coupons["redeemed"],
                "available": coupons["generated"] - coupons["redeemed"],
            },
            "unsettled": {
                "orders_awaiting_payment": unsettled["n"],
                "value_cents": unsettled["cents"],
                "orders_payment_failed": failed,
            },
            "milestones": {
                "every_n_orders": MILESTONE_EVERY_N,
                "percent_off": COUPON_PERCENT_OFF,
                "reached": reached,
                "awaiting_generation": pending is not None,
                "next_milestone_at_order": (reached + 1) * MILESTONE_EVERY_N,
            },
        }
