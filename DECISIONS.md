# Decisions

## 1. Invariants

These are the statements that must be true after every request, whatever
overlaps or repeats. Each one is enforced in exactly one place, and that place
is a database constraint or a conditional `UPDATE` whose `rowcount` is checked
— never a Python-level lock, never a read-then-write.

| # | Invariant | Enforced by | Where |
|---|---|---|---|
| 1 | Inventory never goes negative; we never sell what we do not have | `UPDATE products SET inventory = inventory - :q WHERE id = :id AND inventory >= :q`, plus `CHECK (inventory >= 0)` | `store._reserve` |
| 2 | A cart yields at most one *live* order | `UPDATE carts SET status='checked_out' WHERE id=:id AND status='open'`, plus the partial index `one_live_order_per_cart` | `store._reserve`, `schema.sql` |
| 3 | At most one coupon exists per milestone, ever | `coupons.milestone UNIQUE` | `store.generate_coupon` |
| 4 | A coupon is redeemed at most once, and an order carries at most one coupon | `UPDATE coupons SET redeemed_order_id=:o WHERE code=:c AND redeemed_order_id IS NULL`, plus `redeemed_order_id UNIQUE` | `store._reserve` |
| 5 | A coupon is never consumed by a checkout that does not complete | compensation releases it, guarded by `UPDATE orders SET status='payment_failed' WHERE id=:o AND status='pending_payment'` so it runs exactly once | `store._compensate` |
| 5b | An order is only ever written off on the provider's verdict, never on a guess | `resolution` must be `declined` or `voided`; the state-machine `CHECK` makes any other write-off unrepresentable | `schema.sql`, `store._compensate` |
| 6 | A retried checkout replays and places no second order | `idempotency PRIMARY KEY (cart_id, key)`, claimed in the reserve transaction; a retry arriving mid-charge gets `CHECKOUT_IN_PROGRESS` rather than a second charge | `store._reserve` |
| 7 | Only real products, in positive quantities, enter a cart | `cart_items.product_id` FK, `CHECK (quantity > 0)`, Pydantic `ge=1` | `schema.sql`, `app.AddItem` |
| 8 | An order total is never negative and always equals subtotal − discount | `CHECK (total_cents >= 0)` and `CHECK (total_cents = subtotal_cents - discount_cents)` | `schema.sql` |
| 9 | The payment state machine holds: `pending_payment` has no verdict and no reference, `paid` has `captured` and a reference, `payment_failed` has `declined` or `voided` and no reference | one three-branch `CHECK` on `orders` | `schema.sql` |
| 10 | An order explains itself forever, regardless of later product edits | `order_items` snapshots name, unit price, quantity, line total, and has **no** FK to `products` | `schema.sql` |
| 11 | Reporting mutates nothing and reconciles with orders and coupons | `report()` runs only `SELECT`s in one deferred transaction, counting `paid` orders | `store.report` |

The one that ties the rest together, and the only one no single statement can
enforce: **money is never captured without a `paid` order, and a `paid` order
always has captured money.** Half of it is invariant 9. The other half — an
order whose charge succeeded but whose result we never saw — cannot be a
constraint, because the fact lives at the payment provider. It is held by
leaving such an order durably `pending_payment`, and by
`reconcile_pending_orders` *voiding* the charge at the provider before writing
anything off, so a write-off can never be overtaken by a capture. See
[the payment decision](#decision-payment-happens-between-two-transactions-not-inside-one).

The schema is the specification. If every line of Python were deleted, a
direct `INSERT` still could not oversell, double-reward a milestone, mark an
order paid without a payment reference, or write an order whose arithmetic
does not add up.

That is a claim worth checking rather than asserting, so it is checked: every
`CHECK` in the schema was driven with the forbidden row it exists to reject,
in raw SQL with the application bypassed entirely, and all of them rejected it.
Two of those cases are also pinned as tests
(`test_the_schema_refuses_to_write_off_an_order_without_a_verdict`,
`test_the_schema_refuses_a_second_coupon_for_one_milestone_or_one_order`).

The audit was not ceremonial. **Invariant 9 was decorative when first written**
— see [the NULL trap](#the-null-trap-a-constraint-that-was-not-constraining)
below.

---

## 2. Ambiguities found, and the semantics chosen

| Ambiguity | Chosen semantics | Rationale |
|---|---|---|
| Price changes between add-to-cart and checkout | The cart holds only `(product_id, quantity)` and is **re-priced live on every read**. The customer pays the price in effect at checkout. | One source of truth for price. A snapshot at add-time would need an expiry policy, a "price changed, confirm?" flow, and a rule for how stale is too stale — none of which was asked for. |
| Availability changes after add-to-cart | Adding is allowed beyond current stock. `GET /carts/{id}` marks the line `in_stock: false`. Checkout hard-fails with `OUT_OF_STOCK`. | Stock at add-time is advice, not a reservation — it can be gone a millisecond later. Rejecting at add-time would imply a guarantee we do not make. One enforcement point, at the only moment it is true. |
| Adding a product already in the cart | `POST /items` **accumulates**; `PATCH` **replaces**. | Matches how the two verbs read, and gives the client both behaviours without a mode flag. |
| Which order "counts" toward a milestone | Every successfully placed order, including discounted ones. Milestone *k* is reached at order *k·n*. | Simple, monotonic, and easy to reconcile: `reached = total_orders // n`. |
| What if the admin never generates a coupon and milestones pile up | Milestones queue. Each `POST /admin/coupons` mints the **oldest unrewarded** one; call it repeatedly to catch up. | Never silently drops an earned reward, and keeps generation explicitly administrator-triggered as specified. |
| Coupon ownership | Coupons are **bearer** tokens: whoever presents the code first gets it. | There is no authentication and no customer entity in scope, so there is nobody to own one. Stated plainly rather than faked. |
| Coupon scope and stacking | One coupon per order, percent-off applied to the whole order subtotal, no minimum spend, no expiry. | Smallest defensible rule set. Each of the missing ones is a product decision, not an engineering one. |
| Percent stamped per coupon or read from config | Stamped onto the coupon row at mint time. | Changing `COUPON_PERCENT_OFF` must not silently revalue coupons already in customers' hands. |
| Is an `Idempotency-Key` global or per-cart | **Per-cart**: the primary key is `(cart_id, key)`. The fingerprint then only has to cover `coupon_code`. | A global namespace was wrong in both directions. Any caller could permanently claim an obvious string like `retry` and make every later honest use of it fail; and one client reusing a key across two of its *own* carts — two genuinely different operations — was rejected rather than honoured. Changed during review; the original global key is the kind of choice that looks safe until you ask who else shares the namespace. |
| Is `Idempotency-Key` required | Optional. Without it a retry gets `409 CART_ALREADY_CHECKED_OUT` **carrying the winning `order_id`**, so the client can still recover. | The cart state machine is the real duplicate-order backstop; the key only upgrades the outcome from "recoverable error" to "identical response". |
| Does a *failed* checkout memoise under its key | No. The reserve transaction's row is rolled back, and compensation deletes it, so the same key may genuinely retry. | A declined payment should be retryable. Caching the failure would strand the client on a key it can never reuse. |
| What counts as a "successfully placed order" | Only a `paid` order. Milestones, revenue and purchased quantities all filter on it. | A reservation whose payment has not resolved is not a sale. Counting it would let a coupon be earned by money that never arrived. |
| What to do when the payment result is **unknown** | Nothing. Return `503 PAYMENT_RESULT_UNKNOWN`, leave the order `pending_payment` with stock and coupon still claimed, and let reconciliation void the charge at the provider. | The only alternatives are guessing it failed (releasing stock for an order that may be paid) or guessing it succeeded (shipping goods nobody paid for). Neither is acceptable, so the system declines to guess. |
| A retry arriving while the first charge is in flight | `409 CHECKOUT_IN_PROGRESS` with the order id, rather than blocking until the charge resolves. | The final result does not exist yet. Waiting would hold a request open on someone else's network latency; starting a second charge would be worse. Fail fast and name the order so the client can poll it. |
| Is a compensated order deleted or kept | Kept, as `payment_failed`. | Deleting financial records is how audit trails disappear. `one_live_order_per_cart` is a *partial* index so the record can be kept without blocking a retry. |

---

## 3. Material design decisions

### Decision: Invariants live in the schema, not in application code

**Context.** Every requirement in this brief — no overselling, one order per
cart, one coupon per milestone, single redemption, no duplicate on retry — is
a uniqueness or conditional-write problem. Each could be implemented by
reading, deciding in Python, then writing.

**Options considered.** (a) Read-check-write in the service layer, guarded by
a process-wide `threading.Lock`. (b) Optimistic concurrency with a `version`
column and retry loops. (c) Push every invariant into constraints and
conditional `UPDATE`s, and assert on `rowcount`.

**Choice.** (c).

**Why.** (a) is the trap: it looks correct, passes single-process tests, and
becomes a silent lie the moment a second worker or a second instance exists —
false confidence is worse than no guard. (b) is correct but needs a retry loop
around every mutation and a conflict-resolution rule per entity, which is a
lot of machinery for invariants that are all really "this row may only be
claimed once". (c) makes the claim atomic by construction: `UPDATE ... WHERE
inventory >= :q` either claims stock or reports 0 rows, and there is no window
between the check and the write because there is no separate check.

**Consequences.** The service layer is small and has no locking code. The same
SQL is correct on Postgres under `READ COMMITTED` with no changes. The cost is
that reading the code requires reading SQL, and that invariants are expressed
in two languages; the invariant table above exists to pay that back.

There is a sharper cost, and it bit. Pushing invariants into the database moves
them somewhere that is **harder to test and easier to get subtly wrong**, and a
constraint that does not constrain looks exactly like one that does:

#### The NULL trap: a constraint that was not constraining

Invariant 9 — an order may only be written off once the provider has declined
or voided the charge — was first written as:

```sql
CHECK (   (status = 'pending_payment' AND resolution IS NULL AND payment_ref IS NULL)
       OR (status = 'paid'            AND resolution = 'captured' AND payment_ref IS NOT NULL)
       OR (status = 'payment_failed'  AND resolution IN ('declined','voided') AND payment_ref IS NULL) )
```

This reads correctly and is wrong. **A `CHECK` is satisfied when its expression
evaluates to `NULL`, not only when it is true.** With a `NULL` `resolution`,
`resolution IN ('declined','voided')` is `NULL`, the third disjunct is `NULL`,
the other two are false, and `false OR false OR NULL` is `NULL` — so the
constraint passed and admitted precisely the row it was written to reject. The
fix is `COALESCE(resolution, '')`, which makes the disjunct false rather than
unknown, and the comment in `schema.sql` says why it is there so nobody tidies
it away.

What makes this worth recording is how it surfaced. It was not caught by
reading the SQL, by reasoning about it, or by any application-level test — the
application never writes that row, so every one of them stayed green. It was
caught by a test that **bypasses the application and writes the forbidden row
in raw SQL**, which failed with `DID NOT RAISE`. The lesson generalises past
this bug: if the schema is the specification, then the specification needs
tests of its own, aimed underneath the code that normally protects it.

### Decision: SQLite in WAL mode with `BEGIN IMMEDIATE`, not an in-memory store

**Context.** Persistence was free choice; an in-memory implementation was
allowed if it demonstrated the invariants under overlap.

**Options considered.** (a) In-memory dicts with an `RLock`. (b) SQLite. (c)
Postgres via Docker.

**Choice.** (b), file-backed, WAL journal, `BEGIN IMMEDIATE` for every write,
`busy_timeout = 30s`.

**Why.** (a) would have made every invariant a hand-rolled critical section,
which is precisely the reasoning the brief wants to see externalised, and it
throws away `CHECK`/`UNIQUE` for free. (c) is the production answer but adds a
dependency an evaluator has to install and run. (b) is in the standard
library, needs no service, and still gives real transactions, real
constraints, and real write contention. `BEGIN IMMEDIATE` takes the write lock
at transaction start rather than on first write, which converts SQLite's
"deferred transaction upgrade fails with `SQLITE_BUSY`" failure mode into a
simple queue.

**Consequences.** Writers serialise database-wide — correct, and fine at this
scale, but it *is* the throughput ceiling. Readers never block (WAL). The
design does not depend on that serialisation for correctness, which is what
makes the Postgres migration a configuration change rather than a rewrite.

### Decision: Idempotency by claimed key inside the same transaction

**Context.** A client that times out will retry. That retry must not place a
second order or charge inventory twice.

**Options considered.** (a) Deduplicate on cart state alone — the second
checkout of a spent cart is already a 409. (b) A separate idempotency record
written after the order commits. (c) Claim the key as the first statement of
the same transaction that places the order.

**Choice.** (c), with (a) retained underneath as a backstop.

**Why.** (a) alone is correct but user-hostile: the retrying client gets an
error for an operation that in fact succeeded, and must go and fetch the order
to find out. (b) has a window — if the process dies between commit and
recording the key, the retry re-executes and the guarantee evaporates. (c) has
no window: the key and the order commit or roll back together. Storing the
serialised response, rather than just the order id, means the replay is
byte-identical and needs no re-derivation.

**Consequences.** Retries return `201` with the original body and
`Idempotent-Replay: true`. Reusing a key for a *different* cart or coupon is a
`409` rather than a wrong replay, because the stored fingerprint is compared.
The `idempotency` table grows without bound and needs a TTL sweep — deferred,
noted below.

### Decision: Money as integer cents with floored percentage discounts

**Context.** "Calculate money without floating-point rounding errors," and
discounts must be deterministic and never make a total negative.

**Options considered.** (a) `float`. (b) `decimal.Decimal`. (c) integer minor
units.

**Choice.** (c) throughout — storage, arithmetic, and the wire format, where
every money field is named `*_cents`.

**Why.** (a) is disqualified. (b) is correct in Python but SQLite has no
decimal type, so a `Decimal` is stored as `TEXT` or `REAL` and the problem
returns at the storage boundary, along with a serialisation question at the
API boundary. (c) has no representation gap anywhere in the stack, and `CHECK`
constraints on integers actually mean something. The discount uses floor
division (`subtotal * percent // 100`), so it can never exceed the subtotal —
the total is structurally incapable of going negative, independent of the
`CHECK` that also asserts it. Applying the percentage once to the order
subtotal rather than per line makes the result independent of line ordering
and of how a quantity is split across lines.

**Consequences.** Rounding is always down, so the store rounds in the
customer's favour by at most one cent per order; that is a stated policy, not
an accident. Sub-cent pricing and multi-currency are not supported. Per-product
revenue in the report is gross (pre-discount), because an order-level discount
has no non-arbitrary allocation across lines — the report states this by
naming the field `gross_revenue_cents`, and it reconciles exactly against the
order subtotals.

### Decision: Orders snapshot; carts reference

**Context.** An order must stay explainable after products are repriced,
renamed or deleted.

**Options considered.** (a) Orders join to `products` at read time. (b) Orders
store product ids plus a copy of the price. (c) `order_items` is a full
snapshot with no foreign key to `products` at all.

**Choice.** (c) for orders, and the mirror image — pure reference, no snapshot
— for carts.

**Why.** (a) silently rewrites history. (b) still breaks if the product row is
deleted, and leaves the name to drift. (c) makes an order a self-contained
document: product id, name as it was, unit price as it was, quantity, and line
total, with `CHECK (line_total_cents = unit_price_cents * quantity)` so the
arithmetic cannot rot. Deliberately omitting the foreign key is the point —
the snapshot must outlive its referent. Carts take the opposite choice for the
opposite reason: a cart is a live intention, so it should show today's price.

**Consequences.** `order_items.product_name` denormalises. Reporting groups by
`product_id` and uses `MAX(product_name)`, so a renamed product reports under
whatever name its most recent order recorded — acceptable, and better than
joining to a table that may no longer contain the row.

### Decision: Payment happens between two transactions, not inside one

**Context.** Checkout has to reserve stock, claim a coupon, create an order and
take money. The obvious implementation puts all four in one transaction, and
that is what this service did first. It is wrong, and it is wrong in the worst
possible direction: a payment provider is a network call, so it can be captured
remotely while the local transaction rolls back. The customer is charged and no
order exists. No amount of local transactional rigour detects that, because the
authoritative record is in someone else's database.

**Options considered.**

(a) Charge inside the transaction. Simple, and silently loses money on any
error after the provider captured — including a timeout where the capture
actually succeeded.
(b) Charge *before* opening the transaction. Inverts the problem: the money is
taken and then stock may turn out to be unavailable, so now every failure needs
a refund.
(c) Reserve in one transaction, charge outside any transaction, then settle or
compensate in a second transaction, with a durable intermediate state and a
reconciliation path that asks the provider what happened.
(d) A transactional outbox: commit the order plus an "initiate payment" row,
and have a worker drain it.

**Choice.** (c). The order becomes a small state machine —
`pending_payment → paid | payment_failed` — with inventory and the coupon
claimed on entry to `pending_payment`, and `reconcile_pending_orders` as the
recovery path.

**Why.** (c) is the smallest design in which no outcome requires a guess. The
three things that can happen to a charge map onto three distinct behaviours:

- **declined** — definitive, no money moved, so compensate immediately: stock
  back, coupon released, cart reopened, order `payment_failed`, `402`.
- **captured** — settle: `paid`, record the reference, `201`.
- **unknown** (timeout, connection reset, our process dying) — *do nothing*.
  Return `503 PAYMENT_RESULT_UNKNOWN` and leave the order `pending_payment`.
  Compensating here would be the actual bug: it would hand the stock and the
  coupon to another customer for an order the provider had in fact captured.

That third branch is the whole reason this decision exists. Reconciliation is
the only code allowed to resolve a `pending_payment` order, and it resolves it
against the provider — see
[the next decision](#decision-recovery-voids-the-charge-instead-of-asking-whether-it-was-captured)
for why it does so by voiding the charge rather than by asking about it.

(d) is the better answer at scale and is a strict superset of this — the outbox
makes the charge survive a process death without needing a sweep to notice.
It needs a worker, a queue and at-least-once delivery semantics to be worth
anything, which is more machinery than this service can justify; (c) gets the
correctness with a periodic sweep instead of a worker, and upgrades to (d)
without changing the state machine.

**Consequences.**

- Only `paid` counts as a successfully placed order. Milestones, revenue and
  purchased quantities all filter on it — a reservation in flight is not a sale.
  Money in flight is reported separately under `unsettled` rather than folded
  in or omitted, so it is visible instead of missing.
- `one_live_order_per_cart` had to become a **partial** unique index
  (`WHERE status <> 'payment_failed'`). A plain `UNIQUE` on `cart_id` would
  keep the invariant but let one failed attempt poison a cart forever.
- `_settle` and `_compensate` are both guarded by the order's current status,
  so a reconciliation sweep racing the original request cannot double-apply.
  Without that guard, a sweep could restore stock for an order that was paid.
- A retry that arrives while the first charge is still in flight cannot be
  given the final result, because it does not exist yet. It gets
  `409 CHECKOUT_IN_PROGRESS` with the order id — the same answer Stripe gives
  for a key whose original request is still running.
- Recovery must *void* rather than poll. See
  [the next decision](#decision-recovery-voids-the-charge-instead-of-asking-whether-it-was-captured).
- Checkout is no longer atomic end to end, and saying otherwise would be the
  comfortable lie. It is atomic in each phase, with a bounded, durable,
  self-healing window between them. That is the strongest property available
  once real money is involved.

### Decision: Recovery voids the charge instead of asking whether it was captured

**Context.** Reconciliation has to decide the fate of an order left
`pending_payment`. The obvious way is to ask the provider "did you capture this?"
and act on the answer. That is what this service did first, and it has a hole:
**a "no" describes a moment that has already passed.** A charge that had not been
captured when we asked can be captured immediately afterwards. Write the order
off on the strength of that answer and the provider is holding money for an order
nobody will honour — which then needs a refund to put right, and a refund path
means a `refund_pending` state, a sweep that watches written-off orders for late
captures, and an unbounded set of orders to keep watching. All of it machinery to
clean up after a decision that should never have been made.

**Options considered.**

(a) Poll `status()`, compensate on "not captured", and build the refund path to
mop up the race. Correct eventually, but the window is real and the cleanup is
larger than the original feature.
(b) Poll `status()` but only after a long grace period, and accept the residual
risk. Shrinks the window without closing it, and "unlikely" is a poor property
for money.
(c) Don't ask — **tell**. Void the charge at the provider. A void is terminal:
once it succeeds the charge can never be captured, so compensation is safe
forever rather than safe-for-now. If the provider reports it had already
captured, settle instead. If the void cannot be performed, decide nothing.

**Choice.** (c). `payments.void(order_id)` returns the capture reference if the
provider had already captured, `None` once the charge is permanently voided, and
raises `PaymentUnavailable` if nothing could be decided.

**Why.** It converts an unbounded eventual-consistency problem into a single
atomic decision taken at the only place that can take it. The stale-answer race
does not get smaller, it stops existing — and with it the entire refund path,
the extra order state, and the set of written-off orders that would have needed
watching forever. Closing a hole is cheaper than building the apparatus to
survive it.

It also subsumes the read: `void` answers "was it captured?" as a side effect of
making the answer permanent, so `status()` became dead code and was deleted
rather than kept for symmetry.

The third branch is the one that matters. When the provider is unreachable, or
the payment method cannot be voided because it is still in flight — a real
constraint for bank debits and other asynchronous methods — the sweep records the
order as `unresolved` and changes nothing. The reservation is held indefinitely.
That is the correct trade: holding stock costs a row and some inventory, and it
is visible in the report under `unsettled`, whereas releasing it early costs
money and trust.

**Consequences.**

- `orders.resolution` records *why* an order left `pending_payment` —
  `captured`, `declined` or `voided`. There is deliberately no value meaning "a
  status check came back empty", so the old behaviour is not merely discouraged,
  it is unrepresentable. `_compensate` takes the verdict as a required argument
  and rejects anything else.
- The invariant is enforced by a three-branch `CHECK` on `orders`, so it holds
  against direct SQL and not just against this codebase. There is a test that
  bypasses the application entirely and asserts the database refuses the write.
- Voiding on the first sweep can cancel a charge that was merely slow and would
  have succeeded. The customer's checkout fails and they retry; nobody is
  charged twice. That is a far better failure than the alternative, and
  `stale_after_seconds` keeps it rare.
- If the provider is down for a long time, orders accumulate in
  `pending_payment` holding stock. Fail-safe, visible, and the thing an operator
  should be paged about — not something to paper over by releasing inventory.
- Customer-initiated refunds and cancellations are still not implemented. That
  is a product feature with its own rules, not a hole in this design.

### Decision: A fake provider whose ledger is deliberately not ours

**Context.** No real payment integration was required, but the design above only
works if the provider can void a charge and report an already-captured one.

**Options considered.** (a) Treat checkout success as payment success and write
no payment code. (b) A provider interface with an abstract base class and a fake
implementation. (c) A module with two functions and a dict standing in for the
provider's ledger.

**Choice.** (c) — `payments.charge(order_id, amount_cents)` and
`payments.void(order_id)`, in their own module.

**Why.** (a) leaves the two most important requirements untestable: a coupon must
survive a checkout that fails, and money must not be capturable without an order.
Both need something that can fail *after* the claim. (b) is an abstract base
class with one implementation — ceremony with nothing to abstract over; the module
boundary already gives the seam and `monkeypatch` already gives the substitution.
(c) is about sixty lines and buys three things: `charge` is idempotent on
`order_id` (charging the same order twice returns the first reference, which is
how a real provider's idempotency key behaves), `void` is terminal so a voided
charge can never be captured afterwards — there is a test asserting exactly that,
because the whole design rests on it — and the separate module makes it obvious at
a glance that these calls are not inside a transaction.

`_charges` deliberately models the *provider's* ledger rather than ours. The
entire problem being solved is that their record and ours can disagree, and only
they can settle it — a fake that shared our database would quietly assume the
problem away.

**Consequences.** Tests can inject each outcome precisely, including the nasty
one — capture the charge, then raise a transport error — and assert that
reconciliation recovers it. The ledger is process-local, so a real restart loses
it; that is a property of the fake, not of the design.

### Decision: Milestone queue computed from `COUNT(orders)`, not a counter column

**Context.** Coupons are earned every *n*th order and minted on request.

**Options considered.** (a) A mutable `rewards_issued` counter row. (b) A
`sequence_no` column on orders. (c) Derive from `COUNT(*) FROM orders` and the
set of milestones already granted.

**Choice.** (c).

**Why.** (a) is a second source of truth that can drift from the orders table
— exactly the class of bug the reconciliation requirement is probing for. (b)
adds a column whose only consumer is this calculation. (c) cannot drift by
construction, because orders are insert-only and never deleted, and the
`UNIQUE` on `coupons.milestone` means even a badly-isolated concurrent read
cannot produce two coupons for one milestone. The report's milestone section
is computed by the same function the generator uses, so the two can never
disagree.

**Consequences.** `COUNT(*)` is O(n) and will need an index or a materialised
counter eventually; at this scale it is free. Orders must never be hard-deleted
— a refund has to be a new compensating record, not a deletion. That is the
right constraint for financial data anyway, but it is now load-bearing.

---

## 4. Transactions, concurrency and idempotency

**One transaction per mutating step.** Every mutating endpoint opens exactly one
`BEGIN IMMEDIATE`. Checkout is the exception, and deliberately so: it is two
transactions with the payment provider's network call between them and inside
neither. See [the payment decision](#decision-payment-happens-between-two-transactions-not-inside-one)
for why; the shape is:

**Transaction 1 — reserve.** Ordering is deliberate:

1. Claim the idempotency key, so a concurrent retry cannot get past here.
2. Transition the cart `open → checked_out` — the duplicate-order backstop.
3. Read the lines; an empty cart fails here.
4. Reserve stock, one conditional `UPDATE` per line, **ordered by
   `product_id`** — irrelevant under SQLite's single writer, required to avoid
   lock-ordering deadlocks once this is Postgres.
5. Price the coupon, insert the order as `pending_payment` and its line
   snapshots.
6. Redeem the coupon with the conditional `UPDATE` — *this*, not the earlier
   `SELECT`, is what makes redemption exclusive.

Any raise in 1–6 rolls back all of it, and nothing has been charged.

**Charge** — `payments.charge(order_id, amount_cents)`, outside any transaction,
with the order id as the provider's idempotency key.

**Transaction 2 — settle or compensate.** `pending_payment → paid` with the
reference, or `pending_payment → payment_failed` with stock returned, coupon
released and cart reopened. Both are guarded by the order's current status, so
each runs exactly once even if the original request and a reconciliation sweep
arrive together.

**Recovery.** An interrupted charge leaves a durable `pending_payment` row.
`reconcile_pending_orders` resolves it by *voiding* the charge at the provider:
if the provider reports it had already captured, the order settles; if the void
succeeds the charge is terminal and compensation is safe forever; if nothing
could be decided the order is reported `unresolved` and left exactly as it was.
It never writes an order off on a status answer that could go stale between the
read and the write. Idempotent, safe to run beside live traffic, and it skips
orders younger than `stale_after_seconds` so a merely slow charge is usually
left alone.

**Concurrency.** Correctness never rests on SQLite serialising writers. Every
step above is a claim that either succeeds atomically or reports zero rows
affected. The write lock is a throughput property; the `rowcount` checks and
constraints are the correctness property.

**Idempotency.** Keyed on the `Idempotency-Key` header **scoped to its cart**:
the primary key is `(cart_id, key)`, not `key`. A global namespace was worse in
both directions — any caller could permanently claim an obvious string like
`retry` and make every later honest use of it fail, and one client reusing a key
across two of its own carts (two genuinely different operations) was rejected
rather than honoured. With the cart in the primary key, the fingerprint only has
to cover `coupon_code`, which is the one remaining way to replay a key with
different intent. The stored response is the serialised order, so
a replay is byte-identical; it is written keyed on `order_id`, which means
reconciliation fills it in too and a retry arriving long after recovery still
replays correctly. A retry arriving *during* the charge gets
`409 CHECKOUT_IN_PROGRESS`, because the final result does not exist yet and
starting a second charge would be worse than saying so. Failures are not
memoised — compensation deletes the key — so a declined payment may genuinely
be retried.

**How this is tested.** `test_concurrency.py` and `test_payment_recovery.py`
run against a real uvicorn server — not Starlette's `TestClient` — and the
racing tests release their threads through a `threading.Barrier`, because a
thread pool will otherwise happily finish task 0 before task 1 starts and turn
a race test into a sequential one that passes for the wrong reason. Measured:
six handlers genuinely in flight at once.

Races:

- eight carts racing for the one unit of `p_dock` → exactly one `201`, seven
  `OUT_OF_STOCK`, final inventory `0`, seven carts still open;
- eight concurrent retries of one `Idempotency-Key` → one order, one inventory
  decrement, the provider charged exactly once;
- six carts racing for one coupon → one discounted order, five
  `COUPON_ALREADY_REDEEMED`, report shows `redeemed: 1`;
- six admins racing to mint the same milestone → exactly one coupon;
- a reconciliation sweep racing the original settle → resolved once, stock not
  restored for an order that was paid.

Payment lifecycle:

- declined after stock and coupon were claimed → cart reopened, stock restored,
  coupon still available, same key retries successfully;
- **captured but the connection died** → `503`, order stays `pending_payment`,
  stock stays reserved, coupon stays redeemed, and reconciliation settles it
  from the provider's ledger;
- never captured → reconciliation voids and compensates it, and the same cart can
  check out again because `one_live_order_per_cart` is partial;
- **provider unreachable, so the charge cannot be voided** → nothing is decided:
  the order stays `pending_payment` with stock and coupon still held, is reported
  `unresolved`, and a later sweep resolves it once the provider returns;
- a voided charge can never be captured afterwards — asserted directly against
  the provider, because the safety of every compensation depends on it;
- the database itself refuses to mark an order `payment_failed` without a
  provider verdict, tested by bypassing the application and writing raw SQL;
- a `pending_payment` order does not earn a milestone;
- a retry during the charge gets `CHECKOUT_IN_PROGRESS` and the provider is
  charged once;
- reconciliation is idempotent and ignores orders inside the staleness window;
- a charge that lands *after* a sweep compensated the order returns `402`, not a
  `201` carrying a `payment_failed` order whose stock has been released.

Constraints, tested underneath the application:

- the database refuses to mark an order `payment_failed` without a provider
  verdict, and refuses a `paid` order with no payment reference;
- `UNIQUE(milestone)` refuses a second coupon for one milestone, and
  `UNIQUE(redeemed_order_id)` refuses a second coupon against one order. Both
  are called backstops in the table above; dropping either left the whole suite
  green, because SQLite's single writer makes the application-level guard
  accidentally sufficient. They are now exercised directly in raw SQL.

Boundary validation:

- an unbounded `quantity` is a `422`, not the `500` it used to produce;
- an unknown or empty `?status=` is a `422`, not a silently empty list;
- a negative staleness window is a `422`, not a sweep that examines nothing and
  reports all clear;
- a malformed `Idempotency-Key` is rejected at the boundary, and the same key on
  two different carts is honoured rather than rejected;
- the report on an empty database returns zeroes, not nulls — which pins the
  `COALESCE` guards on the money aggregates.

One note on what these tests do *not* prove. The retry-storm test asserts a set
of legal outcomes (`201` or `CHECKOUT_IN_PROGRESS`) rather than a fixed split,
because which one a given caller gets depends on where it lands relative to the
charge. Asserting an exact split there would be asserting a race. What it does
assert is timing-independent: one order, one inventory decrement, one charge.

The milestone-generation test had the same flaw and kept it longer. It asserted
that every loser gets `NO_ELIGIBLE_MILESTONE` — but this document names
`UNIQUE(milestone)` as the actual exclusion guard, and when *that* guard fires
the client gets `CONSTRAINT_VIOLATION` instead. The test passed only because
SQLite's single writer makes the read-then-check in `_milestone_state`
accidentally exclusive, so the constraint never fired. In other words **the test
was pinned to the one property this design explicitly claims not to depend on**,
and it would have failed on the Postgres migration argued for in section 8. It
now asserts the invariant — exactly one winner, exactly one coupon row — and
accepts either loser code.

That is the third instance of the same mistake in this project, after the
`TestClient` episode and the retry storm. The pattern is consistent enough to
state as a rule: **in a concurrency test, assert what must be true afterwards,
never which competitor lost or how it was told.**

---

## 5. Money and rounding rules

1. **Integer cents everywhere** — storage, arithmetic, and the wire format,
   where every monetary field is suffixed `_cents`. No `float`, no `Decimal`,
   no decimal string crosses any boundary. Rationale and the alternatives
   rejected: [Decision: Money as integer cents](#decision-money-as-integer-cents-with-floored-percentage-discounts).
2. **Line total** = `unit_price_cents × quantity`. Exact, and asserted by
   `CHECK (line_total_cents = unit_price_cents * quantity)`.
3. **Subtotal** = sum of line totals.
4. **Discount** = `subtotal_cents × percent_off // 100`. Floor division,
   applied **once to the order subtotal**, never per line.
5. **Total** = `subtotal − discount`, asserted by
   `CHECK (total_cents = subtotal_cents - discount_cents)` and
   `CHECK (total_cents >= 0)`.

**Rounding direction is always down.** Not half-up, not banker's — one
direction, so a total is reproducible from its stored inputs by anyone. The
consequence is that the store rounds in the customer's favour by at most one
cent per order, which is a stated policy rather than an accident. It also
means the discount can never exceed the subtotal, so a negative total is
structurally impossible before the `CHECK` constraint even gets a say.

**Determinism.** Because the percentage is applied once to the subtotal rather
than per line, the result is independent of line ordering and of how a
quantity is split across lines: three cables as one line of 3 and as three
lines of 1 produce the same discount. Worked example — 10% off a 8999-cent
keyboard is 899.9 cents, floored to **899**, total **8100**.

**Out of scope.** Multi-currency, sub-cent unit prices, tax and shipping. All
prices are in one implicit currency.

---

## 6. Error model

One envelope for every failure: `{"error": {"code", "message", "details"}}`.
`code` is a stable machine-readable string and is the contract; `message` is
for humans and may change. `details` carries the facts a client needs to
decide what to do — `available` on `OUT_OF_STOCK`, the winning `order_id` on
`CART_ALREADY_CHECKED_OUT`, `next_milestone_at_order` on
`NO_ELIGIBLE_MILESTONE`, per-field `violations` on `VALIDATION_ERROR`.

`details` is **always present**, `{}` when there is nothing to add. It was
originally omitted when empty, which meant roughly half the documented codes
returned a two-key object and a client had to check for the key before reading
it — a promise of "one error shape" that the wire format did not keep.

Status codes are chosen so a client can triage without parsing the code: 404
means *it does not exist*, 409 means *it exists but its state forbids this*,
422 means *your request was malformed or semantically impossible*, 402 means
*the payment was refused*, 503 means *we do not yet know the outcome*.

That last one earns its own code and status rather than being folded into 402.
`PAYMENT_FAILED` and `PAYMENT_RESULT_UNKNOWN` demand opposite client
behaviour: the first is final and safe to retry as a fresh checkout, the second
means an order exists and may be paid, so the client must poll it and must
*not* retry. Collapsing them would be the API-level version of the same
guessing the service refuses to do internally. `CHECKOUT_IN_PROGRESS` is
likewise distinct from `CART_ALREADY_CHECKED_OUT`: both are 409, but one means
wait and retry, the other means stop.

Pydantic's default validation response is remapped into the same
envelope so there is exactly one error shape on the wire. An unexpected
`IntegrityError` is surfaced as `409 CONSTRAINT_VIOLATION` rather than a 500 —
if a constraint fires, the invariant held and the request genuinely conflicted.
The driver's message is **logged, never returned**: it names tables, columns and
verbatim `CHECK` expressions, and it changes between SQLite versions, so echoing
it both leaks the schema and makes an unstable string part of the contract.

**Validation is a boundary, not a suggestion.** Three inputs reached storage or
SQL with only half a bound on them, and each failed in a way that was worse than
an error:

- `quantity` had a lower bound but no upper one, so `2**63` reached SQLite's
  `INTEGER` column and raised `OverflowError` — a bare `500` with an empty body.
- `stale_after_seconds` had no floor, and a negative value produced the invalid
  SQLite modifier `'--5 seconds'`, which makes `datetime()` return `NULL`, so no
  row matched. The sweep answered `{"examined": 0}`: a clean all-clear while
  orders sat stuck holding stock. **Silent wrong answers are worse than loud
  failures**, and this one would have been believed by a monitoring script.
- `?status=` on `/admin/orders` was a bare `str`, so a typo returned `200` with
  an empty list and an empty string dropped the filter and returned everything.
  Indistinguishable from a real answer.

All three are now bounded at the handler and return `422`. The general shape of
the bug is the same each time: a value validated on one side only, whose
out-of-range case produced *plausible output* rather than an error.

**The OpenAPI document is part of the contract.** The README points clients at
`/openapi.json`, but FastAPI generates only success responses by default, and
types its automatic `422` as `HTTPValidationError` (`{"detail": [...]}`) — which
this service does not use. A generated client would have parsed the wrong field
for the one error shape that *was* documented. Every route now declares its
statuses against a shared `ErrorEnvelope` model, so the spec and the table below
say the same thing.

Full table in [README.md](README.md#errors).

---

## 7. Implemented vs. deferred

**Implemented.** Everything in the brief: products and seeded inventory, cart
CRUD with validation, live cart pricing, single-use carts, checkout with
inventory reservation and idempotent retry, order snapshots, milestone coupon
generation and single-use redemption, admin reporting that reconciles, a
distinguishable error model, and 37 tests: six races, seven covering the
payment lifecycle, two driving the schema constraints in raw SQL underneath the
application, and the rest business rules and boundary validation.

Beyond the brief: the two-phase payment state machine and its reconciliation
path. This started out as a single transaction with the charge inside it, which
is the standard shape and is quietly wrong once the provider is a real network
call — so it was worth building properly rather than documenting as a known
defect.

Also beyond the brief, and added during review rather than planned: bounded
input validation at every handler, `ErrorEnvelope` declared per route so
`/openapi.json` documents the failure contract, per-cart idempotency scoping,
batched order reads, and `secrets`-based coupon codes. None of these were
features. Each closed a way the service could give a **plausible wrong answer**
— a `500` with an empty body, an empty list that means "you typo'd", a sweep
that reports all clear while orders sit stuck.

**Deferred, deliberately.**

| Deferred | Why it was safe to skip | What it would cost |
|---|---|---|
| Authentication / authorisation | Explicitly out of scope; admin routes are identified instead | A scope check dependency on the four `/admin` routes |
| Customer-initiated refunds and cancellation | A product feature with its own rules (window, partial refunds, restocking), not a correctness hole. Note this is *not* the refund path an earlier draft needed: voiding the charge before compensating removed that obligation entirely | A `refund` provider operation, a `refunded` order state, and a decision about whether a refund restores inventory |
| Provider webhooks | Reconciliation is pull-based, so recovery is bounded by how often the sweep runs rather than being immediate | An endpoint, signature verification, and replay protection — the sweep stays as the backstop |
| Reconciliation as a scheduled job | It exists as an admin endpoint, which is testable and observable but has to be invoked | A cron or a worker calling `reconcile_pending_orders`; the function does not change |
| Transactional outbox for the charge | The sweep covers the same failures with less machinery at this scale | A queue, a worker, and at-least-once semantics |
| Idempotency key TTL and sweep | The table only grows; nothing is incorrect | A `created_at` index and a periodic `DELETE`; the column is already there |
| Cart expiry / abandoned cart cleanup | No reservation is held, so an abandoned cart costs nothing but a row | A sweeper on `carts.created_at` |
| Coupon expiry, minimum spend, stacking, per-customer ownership | Product decisions, not engineering ones; each needs a rule nobody has stated | Columns and predicate checks in `checkout` |
| Refunds / cancellation | Would make orders mutable, which breaks the derived milestone count | Compensating records, never deletion |
| Pagination on `/admin/orders`, `/products` | Dataset is tiny, and the N+1 behind `/admin/orders` is fixed so the remaining cost is response size, not query count | Cursor pagination and a `total` alongside the array |
| Rate limiting on coupon redemption | Coupon codes are now 128-bit `secrets` tokens, so the enumeration oracle (`COUPON_NOT_FOUND` vs `COUPON_ALREADY_REDEEMED` are distinguishable, and there is no auth) is no longer practically exploitable | A counter per client, which needs a client identity this service does not have |
| Idempotency keys scoped to a client rather than a cart | Per-cart scoping removes the cross-client collision. Per-client would be stricter but needs authentication, which is out of scope | A principal on the request, then `(client_id, key)` |
| Structured logging, metrics, tracing | Nothing to observe yet | Middleware |
| Rate limiting | No auth, so no principal to limit | Gateway concern |

---

## 8. How this evolves for multiple instances and production scale

The application code does not change. That was the design goal, and it is
worth being concrete about why.

**Database.** Swap SQLite for Postgres and point `CHECKOUT_DB` at a DSN. Every
statement that carries an invariant is already a conditional `UPDATE` or a
constraint, and both are atomic under `READ COMMITTED` — a row-level write
lock is taken for the duration of the `UPDATE`, so `UPDATE products SET
inventory = inventory - 2 WHERE id = 'p_dock' AND inventory >= 2` cannot
interleave with a competing one. No `SELECT ... FOR UPDATE` is needed anywhere,
because nothing reads a value and then writes a decision based on it. The
stock reservation already iterates lines in `product_id` order, which is what
stops two multi-product checkouts deadlocking on each other's rows.

**What genuinely changes.** `BEGIN IMMEDIATE` becomes a plain `BEGIN`;
database-wide write serialisation becomes row-level contention, so throughput
stops being bounded by a single writer. A connection pool replaces
connect-per-request. `COUNT(*) FROM orders` on the milestone path becomes the
first thing to hurt and wants either a partial index or a small
`order_counters` row updated in the same transaction — at which point the
`UNIQUE` on `coupons.milestone` is what still guarantees the invariant even if
the counter drifts, which is exactly why it is there. The idempotency sweep and
`reconcile_pending_orders` both become scheduled jobs rather than endpoints —
with more than one instance they want a leader lock or an advisory lock so
several replicas do not sweep the same orders at once, though the status guards
on `_settle` and `_compensate` mean the worst case is wasted work rather than
double-applied compensation.

`one_live_order_per_cart` is a partial unique index, which Postgres supports
with identical syntax; it is worth checking rather than assuming, because that
index is the only thing standing between a compensated attempt and a cart that
can never be retried.

`/admin/orders` was the first thing that would actually have hurt, and it is
fixed: it built each order through a per-order helper, so listing N orders ran
2N+1 queries on the one endpoint that grows by a row per checkout attempt
forever. It now assembles every order from three queries — measured at 4 SQL
statements for 12 orders, previously 25 — and `_order_view` delegates to the
same batched builder so the single-order and list shapes cannot drift apart.

**What stays hard.** Hot inventory rows serialise on the popular product; if
that becomes the bottleneck the answer is to shard stock into reservation
buckets, not to weaken the constraint.

The payment path is the part that genuinely changes shape rather than scaling.
Pull-based reconciliation is bounded by sweep frequency, which is fine at low
volume and not fine when a customer is watching a spinner: production wants
provider webhooks for the fast path with the sweep kept as the backstop, and a
transactional outbox so the charge survives a process death without waiting to
be noticed. Both slot in behind the same `pending_payment → paid | failed`
state machine, which is the point of having built it.

---

## 9. How AI was used

I used Claude Code for essentially all of the typing: the schema, both modules,
the tests, and these documents. What I did not delegate was the design. I went
in with the shape already decided — invariants as constraints, one transaction
per request, `rowcount` assertions instead of locks, integer cents — and the
value of the tool was that it wrote a lot of consistent code against those
constraints quickly, not that it chose them.

Three defaults I steered away from before generating anything, because each is
what an unconstrained model reaches for first: a module-level `threading.Lock`
around checkout (correct-looking, invisible to a second process, and therefore
worse than no guard); `decimal.Decimal` for money (correct in Python, but
SQLite has no decimal type so it reintroduces the problem at the storage
boundary); and snapshotting price into `cart_items` at add-time (creates a
second source of truth and a staleness policy nobody asked for).

**The second correction, and the more expensive one.** Checkout was originally
one transaction with the payment call inside it. That is what I designed, what
the model wrote without objection, and what twenty-something green tests
endorsed — because every test was checking local transactional behaviour, and
locally the design is impeccable. The flaw is only visible if you ask what the
provider's database looks like when ours rolls back: the charge can be captured
remotely while the order disappears locally. No amount of test coverage against
our own database surfaces that, and no linter or review pass on the diff would
either, because every individual line is correct.

I flagged it in this document as a known ceiling before rebuilding it. That was
the right call to make explicit and the wrong place to leave it, and it is worth
noting which kind of error it was: not a bug the model introduced, but a design
I specified and it implemented faithfully. Reviewing generated code line by line
would never have caught it. The rebuild — order state machine, compensation with
exactly-once status guards, a reconciliation sweep, a partial unique index so a
failed attempt does not poison the cart — is about a hundred and fifty lines and
six new tests, and none of it was hard once the shape was right. Getting the
shape right was the work, and that part does not delegate.

The same pattern repeated once more, which is the reason I am labouring it. My
first reconciliation implementation polled the provider for a capture and
compensated on a "no" — a textbook-shaped recovery loop that reads perfectly and
loses money on a race, because the answer is stale the instant it arrives. I
wrote it, documented the resulting refund obligation as a known hole, and only
on reconsidering it saw that voiding the charge deletes the hole instead of
requiring apparatus to survive it. Three times now the failure mode has been the
same: code that is locally correct, plausibly shaped, and wrong about something
only visible from outside the diff. That is the class of error to look for in
generated work, and the reason a design has to be argued through rather than
reviewed line by line.

**A related catch, smaller but sharper.** The constraint enforcing that state
machine was written as `resolution IN ('declined','voided')`. A `CHECK` in SQL
is satisfied when its expression evaluates to NULL, not only when it is true —
so with a NULL `resolution` the whole disjunction went NULL and the constraint
passed, permitting precisely the row it was written to reject. The test asserting
the database refuses that write is what surfaced it; it failed with `DID NOT
RAISE`, which is a much better outcome than the alternative, where the invariant
table would have claimed an enforcement that did not exist. `COALESCE(resolution,
'')` fixes it, and the comment in `schema.sql` says why it is there so nobody
tidies it away.

**The correction worth reporting.** The concurrency tests initially ran through
Starlette's `TestClient`, and they passed immediately. Tests that pass on the
first run are exactly the tests to distrust, so before believing them I wrote a
throwaway probe that counted how many request handlers were inside the server
at once. It reported **1**. I read that as "`TestClient` serialises requests,
so these tests are sequential and prove nothing", rewrote the fixture to launch
a real uvicorn server, and re-ran the probe. Still 1.

The probe was instrumenting the charge function (then `store.charge`, now
`payments.charge`), which at that point ran *inside* the
`BEGIN IMMEDIATE` transaction. Of course only one handler was ever in there —
that is the write lock doing its job. Moving the probe to `store.connect`,
before the transaction begins, showed **six** handlers concurrently in flight,
under both clients. My diagnosis had been wrong; the instinct to verify had
not. I kept the live server for a different and stated reason (its parallelism
is a property of sockets and a threadpool rather than of `TestClient`'s
internal portal, which is mid-deprecation) and rewrote the fixture's docstring,
which had confidently asserted the thing I had just disproved.

The general lesson, and the reason I am writing it down: a passing concurrency
test is evidence of nothing until you have measured that the concurrency
actually happened. Both the original code and my first fix would have shipped a
test suite that looked rigorous and tested a sequence.

### The review round, and what a fresh reader found

The last thing I did was review the finished service with five independent
reviewers, each starting from an empty context and each given one lens —
security, testing, API contract, maintainability, and an anti-over-engineering
pass. Separating the lenses matters more than the number: a single reviewer
asked for "anything wrong" produces a list weighted toward whatever it noticed
first, while a reviewer told to think only about the error contract reads the
README against the code line by line and finds that the documented `?status=`
values were never enforced.

It found **four bugs I would have shipped**, all of them the same shape — an
input validated on one side only, whose out-of-range case produced plausible
output instead of an error. The worst was the negative staleness window, which
answers `{"examined": 0}`: a monitoring script would have read that as healthy
forever while orders sat stuck holding inventory.

The technique that produced the sharpest findings was **mutation testing**, and
it is the part I would repeat first. Rather than reasoning about coverage, the
testing reviewer broke a line, ran the suite, and recorded whether anything
failed. Six mutants survived. One of them was the `_compensate` verdict guard —
the application half of invariant 9, which I had written *in the previous
round* and considered the centrepiece of the design. Replacing its condition
with `if False:` left all 28 tests green. I had tested the schema half and
assumed the pair.

That is the honest summary of AI's role here. It did not design this service
and it did not catch these things because it is clever; it caught them because
five readers with fresh context and narrow briefs will out-read one author who
knows what the code is supposed to do. The author's knowledge is the problem —
it is exactly what makes a decorative constraint and an untested guard invisible.
What I contributed was the decision to distrust a green suite, the mutation
technique, and the judgment about which findings to act on: I applied fifteen
mechanically, took six after weighing them, and **declined seven with stated
reasons** — including a proposal to derive test expectations from `/products`,
which would have made the assertions pass even if the prices were wrong. A
reviewer that produces thirty-three findings is only useful if you are willing
to argue with a third of them.

One more small thing, recorded because it is the same failure a fourth time. My
own verification script reported `FAIL  retry replays same order`. The replay
was fine; I had read the response header case-sensitively and it arrives
lowercased. I checked before reporting it. The through-line across all four —
the probe inside the lock, payment inside the transaction, the race assertions,
this — is that **the code was never the unreliable part. The measurement was.**

---

## 10. What I would look at first with another two hours

1. **Prove the invariants against an adversarial harness, not a test.** Run a
   few thousand randomised operations across many threads — add, remove,
   checkout, redeem, generate, report — and assert the global invariants after
   every single one: `sold + on_hand == seeded` per product, coupons redeemed
   ≤ coupons generated, report totals equal to the sum over orders. Property
   testing finds the interleaving I did not think to write down, and this
   domain is unusually well suited to it because the invariants are cheap to
   check.
2. **Run the same suite against Postgres.** The whole scaling argument above
   rests on the claim that these statements are correct under `READ COMMITTED`
   with row locks. I believe it, I have reasoned it through, and I have not
   executed it. That is exactly the kind of belief that should be a CI job.
3. **Finish turning the invariant table into an executable suite.** The
   `CHECK` audit prompted by the NULL trap is done — every constraint was
   driven with the row it forbids and all of them rejected it — but only two of
   those cases are pinned as regression tests; the rest was a one-off audit I
   ran by hand. I would generate one test per row of the invariant table, so a
   constraint that stops constraining fails CI instead of waiting for the next
   manual pass. This is the single highest-value thing left, because the whole
   design rests on the schema being the specification.
4. **Reconcile the report against an independent implementation.** Recompute
   every reported figure in Python from `/admin/orders` and `/admin/coupons`
   and assert equality. The current test does this partially; making it total
   would turn "the report reconciles" from a claim into a check.
5. **Fault-inject the gap between the two transactions.** The recovery path is
   tested by simulating each provider outcome, but not by actually killing the
   process between `_reserve` and `_settle`. A test that starts the service as
   a subprocess, `SIGKILL`s it mid-charge, restarts it and runs the sweep would
   exercise the real failure this design exists for, rather than a
   monkeypatched approximation of it.
