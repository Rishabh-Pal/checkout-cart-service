"""The tests that actually matter: overlapping and repeated requests.

Each one is written so that a plausible-looking but wrong implementation
fails it -- read-then-write instead of a conditional UPDATE, per-request
transactions instead of one, or an idempotency record written after commit.
"""

import time

import payments
import store
from conftest import code_of, new_cart, place_order


def test_concurrent_checkouts_cannot_oversell_the_last_unit(client, concurrently):
    """p_dock is seeded with inventory = 1. Eight carts race for it."""
    carts = [new_cart(client, "p_dock", 1) for _ in range(8)]

    results = concurrently(
        lambda i: client.post(f"/carts/{carts[i]}/checkout"), len(carts))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201] + [409] * 7, statuses
    assert {code_of(r) for r in results if r.status_code == 409} == {"OUT_OF_STOCK"}

    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_dock"] == 0
    assert client.get("/admin/report").json()["orders_placed"] == 1

    # The seven losers keep open carts -- a failed checkout consumes nothing.
    assert sum(client.get(f"/carts/{c}").json()["status"] == "open" for c in carts) == 7


def test_concurrent_retries_of_one_request_place_exactly_one_order(client, concurrently):
    """The timed-out client hammers retry. Eight in flight, same key."""
    cart_id = new_cart(client, "p_keyboard", 2)
    headers = {"Idempotency-Key": "retry-storm-1"}

    results = concurrently(
        lambda _: client.post(f"/carts/{cart_id}/checkout", headers=headers), 8)

    # Two legal outcomes per caller, and which one you get is a matter of
    # timing: 201 with the order (the winner, or a replay once it settled), or
    # 409 CHECKOUT_IN_PROGRESS if the original charge had not resolved yet.
    # Asserting a fixed split here would be asserting a race.
    assert {r.status_code for r in results} <= {201, 409}
    for r in results:
        if r.status_code == 409:
            assert code_of(r) == "CHECKOUT_IN_PROGRESS"

    order_ids = {r.json()["id"] for r in results if r.status_code == 201}
    assert len(order_ids) == 1, "a retry created a second order"

    # The invariant, which is not timing-dependent: one order, charged once.
    orders = client.get("/admin/orders").json()["orders"]
    assert len(orders) == 1 and orders[0]["status"] == "paid"
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_keyboard"] == 23
    assert len(payments._charges) == 1, "the provider was charged more than once"


def test_concurrent_checkouts_cannot_redeem_one_coupon_twice(client, concurrently,
                                                             monkeypatch):
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 1)
    place_order(client)
    coupon = client.post("/admin/coupons").json()["code"]

    carts = [new_cart(client, "p_mouse", 1) for _ in range(6)]
    results = concurrently(
        lambda i: client.post(f"/carts/{carts[i]}/checkout",
                              json={"coupon_code": coupon}), len(carts))

    winners = [r for r in results if r.status_code == 201]
    losers = [r for r in results if r.status_code != 201]
    assert len(winners) == 1, [r.json() for r in results]
    assert {code_of(r) for r in losers} == {"COUPON_ALREADY_REDEEMED"}
    assert winners[0].json()["discount_cents"] == 345  # 10% of 3450

    report = client.get("/admin/report").json()
    assert report["coupons"] == {"generated": 1, "available": 0, "redeemed": 1}
    assert report["total_discounts_cents"] == 345


def test_concurrent_generation_mints_one_coupon_per_milestone(client, concurrently,
                                                              monkeypatch):
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 5)
    for _ in range(5):
        place_order(client)  # exactly one milestone reached

    results = concurrently(lambda _: client.post("/admin/coupons"), 6)

    # Exactly one winner is the invariant. WHICH error the losers get is not:
    # the read-then-check in _milestone_state yields NO_ELIGIBLE_MILESTONE, and
    # the UNIQUE(milestone) backstop yields CONSTRAINT_VIOLATION. SQLite's single
    # writer makes the former exclusive today; Postgres under READ COMMITTED
    # would let the latter fire. Pinning one code would be asserting the race.
    losers = [r for r in results if r.status_code != 201]
    assert len(results) - len(losers) == 1, [r.status_code for r in results]
    assert {code_of(r) for r in losers} <= {"NO_ELIGIBLE_MILESTONE",
                                           "CONSTRAINT_VIOLATION"}

    coupons = client.get("/admin/coupons").json()["coupons"]
    assert [c["milestone"] for c in coupons] == [1]


def test_rolled_back_checkout_leaves_cart_stock_and_coupon_untouched(client, monkeypatch):
    """Payment fails after inventory and the coupon have already been claimed."""
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 1)
    place_order(client)
    coupon = client.post("/admin/coupons").json()["code"]

    cart_id = new_cart(client, "p_monitor", 2)

    def decline(order_id, amount_cents):
        raise payments.PaymentDeclined("The payment was declined.")

    monkeypatch.setattr(payments, "charge", decline)
    r = client.post(f"/carts/{cart_id}/checkout",
                    json={"coupon_code": coupon},
                    headers={"Idempotency-Key": "will-fail"})
    assert r.status_code == 402 and code_of(r) == "PAYMENT_FAILED"

    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_monitor"] == 7
    assert client.get("/admin/report").json()["coupons"]["available"] == 1

    # The failure was not memoised: the same key may legitimately retry.
    monkeypatch.setattr(payments, "charge", lambda o, a: "pay_recovered")
    retry = client.post(f"/carts/{cart_id}/checkout",
                        json={"coupon_code": coupon},
                        headers={"Idempotency-Key": "will-fail"})
    assert retry.status_code == 201
    assert retry.json()["discount_cents"] == 6599  # floor(65998 * 10 / 100)
    assert client.get("/admin/report").json()["coupons"]["redeemed"] == 1


def test_reconciliation_racing_a_settle_resolves_the_order_once(client, concurrently,
                                                               monkeypatch):
    """A recovery sweep and the original checkout resolve the same order.

    Both paths end at `_settle`/`_compensate`, which are guarded by the order's
    status, so exactly one of them may act. If that guard were missing, stock
    would be restored for an order that was in fact paid.
    """
    import threading

    started = threading.Event()

    def capture_then_stall(order_id, amount_cents):
        payments._charges[order_id] = "pay_slow_" + order_id[-6:]
        started.set()
        time.sleep(0.4)          # the window a crash or deploy would land in
        return payments._charges[order_id]

    monkeypatch.setattr(payments, "charge", capture_then_stall)
    cart_id = new_cart(client, "p_monitor", 3)

    def checkout_or_reconcile(i):
        if i == 0:
            return client.post(f"/carts/{cart_id}/checkout")
        started.wait(5)
        return client.post("/admin/orders/reconcile?stale_after_seconds=0")

    checkout_result, sweep = concurrently(checkout_or_reconcile, 2)

    assert checkout_result.status_code == 201
    order = checkout_result.json()
    assert order["status"] == "paid"
    assert sweep.status_code == 200

    # Settled exactly once, whichever path got there first.
    assert client.get(f"/orders/{order['id']}").json()["payment_ref"] is not None
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_monitor"] == 4, "stock restored for an order that was paid"

    report = client.get("/admin/report").json()
    assert report["orders_placed"] == 1
    assert report["unsettled"] == {"orders_awaiting_payment": 0, "value_cents": 0,
                                  "orders_payment_failed": 0}
