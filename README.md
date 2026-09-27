# Checkout & Rewards Service

A cart/checkout/orders backend with milestone reward coupons. The interesting
part is not the CRUD — it is that retries, concurrent checkouts, inventory
drift and coupon races all have defined, tested outcomes.

Read [DECISIONS.md](DECISIONS.md) for the reasoning; this file is how to run it.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --reload
```

The database (`checkout.db`, SQLite) is created and seeded on first boot from
[`schema.sql`](schema.sql) — no migration step. Delete the file to reset.
Interactive docs: <http://127.0.0.1:8000/docs>.

```bash
.venv/bin/pytest -q          # 28 tests, ~7s
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CHECKOUT_DB` | `checkout.db` | SQLite file path |
| `MILESTONE_EVERY_N` | `5` | *n* — a coupon is earned every *n*th placed order |
| `COUPON_PERCENT_OFF` | `10` | *x* — percent off, stamped onto each coupon at mint time |

## Seed data

| id | name | price | inventory |
|---|---|---:|---:|
| `p_keyboard` | Mechanical Keyboard | 89.99 | 25 |
| `p_mouse` | Wireless Mouse | 34.50 | 40 |
| `p_monitor` | 27-inch 4K Monitor | 329.99 | 7 |
| `p_cable` | USB-C Cable | 9.99 | 100 |
| `p_dock` | Limited Edition Dock | 149.99 | **1** |

All money in the API is an integer count of **cents**, in fields suffixed
`_cents`. There are no decimal money values anywhere in the wire format.

## Order lifecycle

Checkout is **two** transactions with the payment provider's call between them
and inside neither, because a network call inside a database transaction can be
captured remotely while the transaction rolls back locally — the customer is
charged and no order exists.

```
                   reserve (txn 1)          charge            settle (txn 2)
  cart: open ──────────────────────> pending_payment ────────────────────────> paid
             stock reserved                            provider captured
             coupon claimed                  │         resolution = captured
             cart spent                      │
                                             └── declined, or charge voided
                                                        │
                                                        v  compensate (txn 2)
                                                  payment_failed
                                             stock returned, coupon released,
                                             cart reopened, resolution records
                                             which verdict it was
```

Inventory and the coupon are claimed on entry to `pending_payment`, so nothing
can be oversold or double-redeemed while a charge is in flight.

**If the charge outcome is unknown** (timeout, connection reset, the process
dying), the order stays `pending_payment` and nothing is released — releasing
would hand the stock and coupon to someone else for an order that may in fact
be paid. Checkout returns `503 PAYMENT_RESULT_UNKNOWN` with the order id.

`POST /admin/orders/reconcile` then resolves it, and the way it does so is the
important part: it **voids** the charge at the provider rather than asking
whether it was captured. A "not captured" answer is stale the moment it arrives —
the charge can be captured a millisecond later — so an order written off on that
basis can leave the provider holding money for an order nobody will honour. A
void is terminal, so once it succeeds compensation is safe forever. If the
provider reports it had already captured, the order settles instead. If the void
cannot be performed at all, **nothing is decided**: the order keeps its
reservation, is reported as `unresolved`, and the next sweep tries again.

An order therefore only ever leaves `pending_payment` on an explicit provider
verdict, recorded in `resolution` as `captured`, `declined` or `voided`. A
three-branch `CHECK` on `orders` enforces that, so it holds against direct SQL
and not only against this codebase.

In production the sweep is a scheduled job; it is exposed as an endpoint here so
it is observable and testable.

**Only `paid` counts as a successfully placed order** — for milestones, for
revenue, and for purchased quantities. Orders awaiting payment are reported
separately under `unsettled` so money in flight is visible rather than missing.

## Endpoints

Administrative operations are the five under `/admin`. Authentication is out of
scope, so they are unauthenticated — in production they would sit behind an
admin scope, not a different transport.

| Method | Path | Success | Notes |
|---|---|---|---|
| `GET` | `/products` | 200 | catalogue with live inventory |
| `POST` | `/carts` | 201 | empty cart |
| `GET` | `/carts/{cart_id}` | 200 | live prices, totals, per-line `in_stock` |
| `POST` | `/carts/{cart_id}/items` | 201 | `{product_id, quantity}`; **adds to** any existing quantity |
| `PATCH` | `/carts/{cart_id}/items/{product_id}` | 200 | `{quantity}`; **replaces** the quantity |
| `DELETE` | `/carts/{cart_id}/items/{product_id}` | 204 | |
| `POST` | `/carts/{cart_id}/checkout` | 201 | `{coupon_code?}`; send `Idempotency-Key` |
| `GET` | `/orders/{order_id}` | 200 | immutable snapshot |
| `POST` | `/admin/coupons` | 201 | *admin* — mint the coupon for the oldest unrewarded milestone |
| `GET` | `/admin/coupons` | 200 | *admin* |
| `GET` | `/admin/orders` | 200 | *admin*; optional `?status=paid\|pending_payment\|payment_failed` |
| `POST` | `/admin/orders/reconcile` | 200 | *admin* — resolve stuck payments; optional `?stale_after_seconds=30` |
| `GET` | `/admin/report` | 200 | *admin*, read-only |

### Full reference

Two response shapes recur, so they are given once here: **Cart** is returned by
every cart endpoint, **Order** by checkout and by `GET /orders/{id}`.

<details open><summary><b>Cart</b></summary>

```json
{
  "id": "cart_45f4e9e91d774bb5",
  "status": "open",
  "items": [
    {
      "product_id": "p_keyboard",
      "product_name": "Mechanical Keyboard",
      "quantity": 2,
      "unit_price_cents": 8999,
      "line_total_cents": 17998,
      "available_inventory": 25,
      "in_stock": true
    }
  ],
  "subtotal_cents": 17998,
  "order_id": null
}
```

`product_name` is spelled the same here as in **Order** items, so one client
code path renders a line from either shape.
`unit_price_cents` is the product's price **right now**, not when it was added.
`in_stock` is advisory — stock is only reserved at checkout. `order_id` is
`null` until the cart is checked out, then it points at the resulting order.
</details>

<details open><summary><b>Order</b></summary>

```json
{
  "id": "ord_2531af2e31c34b50",
  "cart_id": "cart_522b6701da95435c",
  "status": "paid",
  "items": [
    {
      "product_id": "p_monitor",
      "product_name": "27-inch 4K Monitor",
      "unit_price_cents": 32999,
      "quantity": 1,
      "line_total_cents": 32999
    }
  ],
  "subtotal_cents": 32999,
  "coupon": {"code": "REWARD-LyEgcihpwoIWh3nq8WJ5TQ", "percent_off": 10},
  "discount_cents": 3299,
  "total_cents": 29700,
  "payment_ref": "pay_d026364d1e574003",
  "resolution": "captured",
  "created_at": "2026-09-26 22:54:10"
}
```

`items` is a snapshot taken at checkout — renaming, repricing or deleting the
product afterwards does not change it. `coupon` is `null` when none was used.
`total_cents == subtotal_cents - discount_cents`, always. `status` is
`pending_payment`, `paid` or `payment_failed` (see
[Order lifecycle](#order-lifecycle)), and `resolution` says why it got there —
`null` while pending, then `captured`, `declined` or `voided`. `payment_ref` is
non-null exactly when `status` is `paid`.
</details>

---

#### `GET /products`

No request body. **200** — `{"products": [{"id", "name", "price_cents", "inventory"}]}`,
ordered by id. Inventory is live.

#### `POST /carts`

No request body. **201** — a **Cart** with no items.

#### `GET /carts/{cart_id}`

No request body. **200** — a **Cart**, re-priced at read time.
**404** `CART_NOT_FOUND`.

#### `POST /carts/{cart_id}/items`

```json
{"product_id": "p_keyboard", "quantity": 2}
```

`quantity` must be an integer ≥ 1. If the product is already in the cart the
quantity is **added** to what is there.

**201** — the updated **Cart**.
**404** `PRODUCT_NOT_FOUND`, `CART_NOT_FOUND` ·
**409** `CART_ALREADY_CHECKED_OUT` ·
**422** `VALIDATION_ERROR` (quantity ≤ 0, non-integer, or missing field).

#### `PATCH /carts/{cart_id}/items/{product_id}`

```json
{"quantity": 3}
```

**Replaces** the quantity (contrast with `POST`, which accumulates). Integer ≥ 1;
use `DELETE` to remove a line rather than setting 0.

**200** — the updated **Cart**.
**404** `CART_ITEM_NOT_FOUND`, `PRODUCT_NOT_FOUND`, `CART_NOT_FOUND` ·
**409** `CART_ALREADY_CHECKED_OUT` · **422** `VALIDATION_ERROR`.

#### `DELETE /carts/{cart_id}/items/{product_id}`

No request body. **204** — no content.
**404** `CART_ITEM_NOT_FOUND`, `CART_NOT_FOUND` ·
**409** `CART_ALREADY_CHECKED_OUT`.

#### `POST /carts/{cart_id}/checkout`

```json
{"coupon_code": "REWARD-LyEgcihpwoIWh3nq8WJ5TQ"}
```

Body is optional; so is `coupon_code` (send `{}`, `{"coupon_code": null}`, or
no body at all for an undiscounted checkout). Header `Idempotency-Key` is
optional but recommended — see [Checkout](#checkout) below. At most 200
characters from `[A-Za-z0-9_.:-]`, and **scoped to this cart**: two clients may
use the same key on different carts without colliding.

**201** — an **Order**. Response header `Idempotent-Replay: true|false`
distinguishes a replay from a fresh order; the body is identical either way.

**402** `PAYMENT_FAILED` — declined, definitively. Fully compensated: the cart
is `open` again, inventory is restored, any coupon is available again, and the
order is on the books as `payment_failed`. Safe to retry as a fresh checkout.

**503** `PAYMENT_RESULT_UNKNOWN` — the provider call timed out or the
connection dropped, so we do not know whether the money moved. An order exists
in `pending_payment` with stock and coupon still claimed; `details.order_id`
names it. **Poll the order, do not retry the checkout** — reconciliation will
resolve it from the provider's ledger.

**404** `CART_NOT_FOUND`, `COUPON_NOT_FOUND`.
**409** `CART_ALREADY_CHECKED_OUT` (`details.order_id` names the winner),
`CHECKOUT_IN_PROGRESS` (the original request's charge is still running;
`details.order_id` names the order to poll), `OUT_OF_STOCK` (`details` carries
`product_id`, `requested`, `available`), `COUPON_ALREADY_REDEEMED`,
`IDEMPOTENCY_KEY_REUSED`.
**422** `CART_EMPTY` — the cart stays `open`.

#### `GET /orders/{order_id}`

No request body. **200** — an **Order**. **404** `ORDER_NOT_FOUND`.

#### `POST /admin/coupons` — administrative

No request body. Mints the coupon for the oldest milestone that has been
reached but not yet rewarded.

**201**

```json
{
  "code": "REWARD-LyEgcihpwoIWh3nq8WJ5TQ",
  "percent_off": 10,
  "milestone": 1,
  "earned_at_order_number": 5,
  "redeemed_order_id": null
}
```

**409** `NO_ELIGIBLE_MILESTONE` — either no milestone is reached yet or every
reached one is already rewarded; `details` carries `orders_placed`,
`milestone_every_n` and `next_milestone_at_order`.

#### `GET /admin/coupons` — administrative

No request body. **200** — `{"coupons": [...]}`, ordered by milestone. Each
entry is the mint response plus `created_at`; `redeemed_order_id` is non-null
once spent.

#### `GET /admin/orders` — administrative

No request body. Optional `?status=paid|pending_payment|payment_failed`; any
other value is a **422** `VALIDATION_ERROR`, never a silently empty list.
**200** — `{"orders": [...]}`, full **Order** objects oldest first. Provided so
the report can be reconciled against its source data; pass `?status=paid` to
compare against the report's revenue figures, which count paid orders only.

#### `POST /admin/orders/reconcile` — administrative

No request body. Optional `?stale_after_seconds=30` (0–86400; out of range is a
**422**). For every order stuck in
`pending_payment` for longer than that, asks the provider to void the charge and
then settles or compensates accordingly. Idempotent, and safe to run alongside
live traffic.

**200**

```json
{"examined": 3,
 "settled": ["ord_9a1c..."],
 "compensated": ["ord_44f0..."],
 "unresolved": ["ord_7b22..."]}
```

- `settled` — the provider had already captured; the order is now `paid`.
- `compensated` — the charge is now permanently voided, so stock and coupon were
  returned and the cart reopened.
- `unresolved` — the provider could not be reached, or the payment method cannot
  be voided while it is still in flight. **Nothing was changed.** These orders
  keep their reservations and are retried on the next sweep; they are also
  counted in the report under `unsettled`.

The staleness window exists because voiding a charge that was merely slow
cancels a payment that would have succeeded. That is a recoverable annoyance —
the customer retries and nobody is charged twice — but it is worth avoiding.

#### `GET /admin/report` — administrative, read-only

No request body. **200**

```json
{
  "orders_placed": 6,
  "purchased_quantity_by_product": [
    {"product_id": "p_cable", "product_name": "USB-C Cable",
     "quantity": 3, "gross_revenue_cents": 2997},
    {"product_id": "p_keyboard", "product_name": "Mechanical Keyboard",
     "quantity": 3, "gross_revenue_cents": 26997},
    {"product_id": "p_monitor", "product_name": "27-inch 4K Monitor",
     "quantity": 1, "gross_revenue_cents": 32999},
    {"product_id": "p_mouse", "product_name": "Wireless Mouse",
     "quantity": 3, "gross_revenue_cents": 10350}
  ],
  "gross_revenue_cents": 73343,
  "total_discounts_cents": 3299,
  "net_revenue_cents": 70044,
  "coupons": {"generated": 1, "redeemed": 1, "available": 0},
  "unsettled": {"orders_awaiting_payment": 0, "value_cents": 0,
                "orders_payment_failed": 0},
  "milestones": {
    "every_n_orders": 5, "percent_off": 10, "reached": 1,
    "awaiting_generation": false, "next_milestone_at_order": 10
  }
}
```

`gross_revenue_cents` is pre-discount and equals the sum of order subtotals
*and* the sum of `purchased_quantity_by_product[].gross_revenue_cents`
(2997 + 26997 + 32999 + 10350 = 73343 above — the example is complete so the
identity can be checked against it).
`net_revenue_cents == gross_revenue_cents - total_discounts_cents`.
Per-product revenue is gross because an order-level discount has no
non-arbitrary allocation across lines. Runs only `SELECT`s in one snapshot, so
repeated calls are byte-identical and mutate nothing.

Every revenue figure counts **`paid` orders only**. `unsettled` surfaces what is
excluded — reservations awaiting a payment result, and how much they are worth —
so money in flight is visible rather than silently missing from the totals.

An OpenAPI document generated from the running service is available at
`/openapi.json`, and Swagger UI at `/docs`. It declares the error responses per
route against the shared `ErrorEnvelope` schema, not only the success ones, so a
generated client handles the failure cases in this table.

---

### Checkout

```bash
CART=$(curl -sX POST localhost:8000/carts | jq -r .id)
curl -sX POST localhost:8000/carts/$CART/items \
     -H 'content-type: application/json' \
     -d '{"product_id":"p_keyboard","quantity":2}'

curl -sX POST localhost:8000/carts/$CART/checkout \
     -H 'content-type: application/json' \
     -H 'Idempotency-Key: 8f2c1a90-checkout-1' \
     -d '{"coupon_code": null}'
```

Repeat that last command verbatim and you get **the same order, the same 201,
the same body**, plus `Idempotent-Replay: true`. No second order, no second
inventory decrement.

The key is scoped to its cart, so `IDEMPOTENCY_KEY_REUSED` means the same key
was replayed against the same cart with a *different* coupon — genuinely
different intent — not that somebody else used the string first.

Without an `Idempotency-Key`, a retry gets `409 CART_ALREADY_CHECKED_OUT` with
the winning `order_id` in `details` — safe, just less pleasant. The header is
optional but recommended for every checkout.

### Errors

Every failure has the same shape and a stable machine-readable `code`:

```json
{"error": {"code": "OUT_OF_STOCK",
           "message": "Only 1 of 'Limited Edition Dock' left.",
           "details": {"product_id": "p_dock", "requested": 3, "available": 1}}}
```

`details` is always present, `{}` when there is nothing to add, so a client
never has to check whether the key exists before reading it.

| Code | Status | When |
|---|---|---|
| `VALIDATION_ERROR` | 422 | malformed body; `details.violations` lists each field |
| `PRODUCT_NOT_FOUND` | 404 | unknown `product_id` |
| `CART_NOT_FOUND` / `ORDER_NOT_FOUND` | 404 | unknown id |
| `CART_ITEM_NOT_FOUND` | 404 | patching/deleting a line that is not in the cart |
| `CART_EMPTY` | 422 | checkout with no items; the cart stays open |
| `CART_ALREADY_CHECKED_OUT` | 409 | second checkout, or mutating a spent cart; `details.order_id` |
| `OUT_OF_STOCK` | 409 | requested > available at checkout; `details.available` |
| `COUPON_NOT_FOUND` | 404 | unknown code |
| `COUPON_ALREADY_REDEEMED` | 409 | code already spent, including by a concurrent checkout |
| `IDEMPOTENCY_KEY_REUSED` | 409 | key replayed with a *different* cart or coupon |
| `NO_ELIGIBLE_MILESTONE` | 409 | no unrewarded milestone; `details.next_milestone_at_order` |
| `CHECKOUT_IN_PROGRESS` | 409 | a retry arrived while the original charge was still running; poll `details.order_id` |
| `PAYMENT_FAILED` | 402 | declined; fully compensated, safe to retry |
| `PAYMENT_RESULT_UNKNOWN` | 503 | charge outcome unknown; order left reserved for reconciliation, poll `details.order_id`, do **not** retry |
| `CONSTRAINT_VIOLATION` | 409 | a database constraint caught what the code missed (the driver message is logged, not returned) |

404 vs 409 is deliberate: 404 means *this does not exist*, 409 means *this
exists but the state forbids it* — so a client can decide whether retrying
could ever help.

`PAYMENT_FAILED` and `PAYMENT_RESULT_UNKNOWN` are separate for the same reason:
the first is final and safe to retry, the second means an order exists that may
already be paid, so the correct client behaviour is to poll it and never retry.
Collapsing them into one code would force the client to guess exactly where the
service refuses to.
