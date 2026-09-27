"""The payment lifecycle: reserve -> charge -> settle | compensate, and the
recovery path when the middle step is interrupted.

These exist because the provider call happens outside the database
transaction. That is the only correct place for it -- a network call inside a
transaction can be captured remotely while the transaction rolls back locally
-- but it creates a window in which our record and the provider's can disagree.
Everything below pins down what happens in that window.
"""

import sqlite3
import threading
import time

import pytest

import payments
import store
from conftest import code_of, new_cart, place_order


def _coupon(client, monkeypatch):
    monkeypatch.setattr("store.MILESTONE_EVERY_N", 1)
    place_order(client)
    return client.post("/admin/coupons").json()["code"]


def _capture_then_lose_the_connection(order_id, amount_cents):
    """The provider took the money; we never found out. The dangerous case."""
    payments._charges[order_id] = "pay_ghost_" + order_id[-6:]
    raise payments.PaymentUnavailable("connection reset by peer")


def _die_before_capturing(order_id, amount_cents):
    """The provider never saw the request."""
    raise payments.PaymentUnavailable("timed out connecting")


def leave_order_pending(client, cart_id, monkeypatch):
    """Strand an order in `pending_payment`, as a crash mid-charge would."""
    monkeypatch.setattr(payments, "charge", _die_before_capturing)
    order_id = client.post(f"/carts/{cart_id}/checkout"
                           ).json()["error"]["details"]["order_id"]
    monkeypatch.setattr(payments, "charge", _real_charge)
    return order_id


_real_charge = payments.charge


# --------------------------------------------------------------------------
# the ambiguous window
# --------------------------------------------------------------------------

def test_an_ambiguous_payment_keeps_the_reservation_instead_of_guessing(client,
                                                                       monkeypatch):
    """We do not know if the money moved, so we release nothing.

    Compensating here would be a real bug: it would hand the stock and the
    coupon to someone else for an order the provider had in fact captured.
    """
    coupon = _coupon(client, monkeypatch)
    monkeypatch.setattr(payments, "charge", _capture_then_lose_the_connection)

    cart_id = new_cart(client, "p_monitor", 2)
    r = client.post(f"/carts/{cart_id}/checkout",
                    json={"coupon_code": coupon},
                    headers={"Idempotency-Key": "ambiguous-1"})
    assert (r.status_code, code_of(r)) == (503, "PAYMENT_RESULT_UNKNOWN")
    order_id = r.json()["error"]["details"]["order_id"]

    order = client.get(f"/orders/{order_id}").json()
    assert order["status"] == "pending_payment"
    assert order["payment_ref"] is None

    # Still reserved: nothing was handed back to anyone else.
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_monitor"] == 5
    assert client.get(f"/carts/{cart_id}").json()["status"] == "checked_out"
    assert client.get("/admin/coupons").json()["coupons"][0]["redeemed_order_id"] == order_id

    # ... and not counted as revenue, but visible rather than missing.
    report = client.get("/admin/report").json()
    assert report["orders_placed"] == 1          # only the setup order
    assert report["unsettled"]["orders_awaiting_payment"] == 1
    assert report["unsettled"]["value_cents"] == 59399    # 65998 less 10%
    assert order_id not in [o["id"] for o in
                            client.get("/admin/orders?status=paid").json()["orders"]]


def test_a_pending_order_does_not_earn_a_milestone(client, monkeypatch):
    """Only a paid order is a successfully placed order."""
    monkeypatch.setattr("store.MILESTONE_EVERY_N", 2)
    place_order(client)
    monkeypatch.setattr(payments, "charge", _capture_then_lose_the_connection)
    client.post(f"/carts/{new_cart(client)}/checkout")

    r = client.post("/admin/coupons")
    assert (r.status_code, code_of(r)) == (409, "NO_ELIGIBLE_MILESTONE")
    assert r.json()["error"]["details"]["orders_placed"] == 1


# --------------------------------------------------------------------------
# recovery
# --------------------------------------------------------------------------

def test_reconciliation_settles_an_order_the_provider_did_capture(client, monkeypatch):
    coupon = _coupon(client, monkeypatch)
    monkeypatch.setattr(payments, "charge", _capture_then_lose_the_connection)
    cart_id = new_cart(client, "p_monitor", 2)
    order_id = client.post(f"/carts/{cart_id}/checkout",
                           json={"coupon_code": coupon},
                           headers={"Idempotency-Key": "ambiguous-2"}
                           ).json()["error"]["details"]["order_id"]

    sweep = client.post("/admin/orders/reconcile?stale_after_seconds=0").json()
    assert sweep == {"examined": 1, "settled": [order_id], "compensated": [],
                     "unresolved": []}

    order = client.get(f"/orders/{order_id}").json()
    assert order["status"] == "paid"
    assert order["resolution"] == "captured"
    assert order["payment_ref"].startswith("pay_ghost_")
    assert order["total_cents"] == 59399

    report = client.get("/admin/report").json()
    assert report["orders_placed"] == 2
    assert report["unsettled"]["orders_awaiting_payment"] == 0
    assert report["coupons"]["redeemed"] == 1

    # The recovered order is now replayable under its original key.
    replay = client.post(f"/carts/{cart_id}/checkout",
                         json={"coupon_code": coupon},
                         headers={"Idempotency-Key": "ambiguous-2"})
    assert replay.status_code == 201
    assert replay.json()["id"] == order_id
    assert replay.headers["Idempotent-Replay"] == "true"


def test_reconciliation_compensates_an_order_the_provider_never_captured(client,
                                                                        monkeypatch):
    coupon = _coupon(client, monkeypatch)
    monkeypatch.setattr(payments, "charge", _die_before_capturing)
    cart_id = new_cart(client, "p_monitor", 2)
    order_id = client.post(f"/carts/{cart_id}/checkout",
                           json={"coupon_code": coupon},
                           headers={"Idempotency-Key": "lost-1"}
                           ).json()["error"]["details"]["order_id"]

    sweep = client.post("/admin/orders/reconcile?stale_after_seconds=0").json()
    assert sweep == {"examined": 1, "settled": [], "compensated": [order_id],
                     "unresolved": []}

    failed = client.get(f"/orders/{order_id}").json()
    assert failed["status"] == "payment_failed"
    assert failed["resolution"] == "voided", "written off without a provider verdict"
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_monitor"] == 7, "reservation not returned"
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"
    assert client.get("/admin/coupons").json()["coupons"][0]["redeemed_order_id"] is None

    report = client.get("/admin/report").json()
    assert report["orders_placed"] == 1
    assert report["unsettled"] == {"orders_awaiting_payment": 0, "value_cents": 0,
                                   "orders_payment_failed": 1}


def test_a_compensated_order_does_not_block_a_retry_of_the_same_cart(client,
                                                                    monkeypatch):
    """`one_live_order_per_cart` is a PARTIAL unique index for exactly this.

    A plain UNIQUE on cart_id would keep the invariant but make the failed
    attempt permanently poison the cart.
    """
    coupon = _coupon(client, monkeypatch)
    monkeypatch.setattr(payments, "charge", _die_before_capturing)
    cart_id = new_cart(client, "p_monitor", 2)
    failed_id = client.post(f"/carts/{cart_id}/checkout",
                            json={"coupon_code": coupon},
                            headers={"Idempotency-Key": "lost-2"}
                            ).json()["error"]["details"]["order_id"]
    client.post("/admin/orders/reconcile?stale_after_seconds=0")

    # The provider recovers. (Not monkeypatch.undo(): that would also revert
    # the fixture's own database patch.)
    monkeypatch.setattr(payments, "charge", lambda order_id, amount_cents:
                        payments._charges.setdefault(order_id, "pay_recovered"))
    retry = client.post(f"/carts/{cart_id}/checkout",
                        json={"coupon_code": coupon},
                        headers={"Idempotency-Key": "lost-2"})
    assert retry.status_code == 201
    new_id = retry.json()["id"]
    assert new_id != failed_id

    # The failed attempt stays on the books as an audit record.
    statuses = {o["id"]: o["status"] for o in client.get("/admin/orders").json()["orders"]}
    assert statuses[failed_id] == "payment_failed"
    assert statuses[new_id] == "paid"
    assert client.get("/admin/report").json()["orders_placed"] == 2


def test_reconciliation_is_idempotent_and_ignores_fresh_orders(client, monkeypatch):
    monkeypatch.setattr(payments, "charge", _die_before_capturing)
    client.post(f"/carts/{new_cart(client)}/checkout")

    # The default staleness window protects an order whose charge may still be
    # running: reconciling too eagerly could compensate one about to be captured.
    assert client.post("/admin/orders/reconcile").json() == {
        "examined": 0, "settled": [], "compensated": [], "unresolved": []}

    first = client.post("/admin/orders/reconcile?stale_after_seconds=0").json()
    assert len(first["compensated"]) == 1
    assert client.post("/admin/orders/reconcile?stale_after_seconds=0").json() == {
        "examined": 0, "settled": [], "compensated": [], "unresolved": []}

    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_cable"] == 100, "stock restored twice"


# --------------------------------------------------------------------------
# the in-flight window
# --------------------------------------------------------------------------

def test_a_retry_during_the_charge_does_not_start_a_second_charge(client, monkeypatch):
    in_flight = threading.Event()

    def slow_charge(order_id, amount_cents):
        in_flight.set()
        time.sleep(0.5)
        return payments._charges.setdefault(order_id, "pay_slow_" + order_id[-6:])

    monkeypatch.setattr(payments, "charge", slow_charge)
    cart_id = new_cart(client, "p_keyboard", 1)
    headers = {"Idempotency-Key": "in-flight-1"}

    result = {}
    worker = threading.Thread(
        target=lambda: result.update(
            r=client.post(f"/carts/{cart_id}/checkout", headers=headers)))
    worker.start()
    assert in_flight.wait(5)

    retry = client.post(f"/carts/{cart_id}/checkout", headers=headers)
    assert (retry.status_code, code_of(retry)) == (409, "CHECKOUT_IN_PROGRESS")
    assert retry.json()["error"]["details"]["order_id"]

    worker.join(10)
    assert result["r"].status_code == 201
    assert len(payments._charges) == 1, "the provider was charged twice"
    assert client.get(f"/orders/{result['r'].json()['id']}").json()["status"] == "paid"


# --------------------------------------------------------------------------
# never write off an order on a guess
# --------------------------------------------------------------------------

def test_an_unvoidable_charge_is_left_reserved_rather_than_written_off(client,
                                                                      monkeypatch):
    """The case that used to owe a refund.

    The provider cannot be reached (or the payment method cannot be voided while
    it is in flight), so whether the money will move is genuinely unknown. The
    old implementation asked "did you capture?", got a no, and compensated --
    and if the capture landed a moment later the provider held money for an
    order we had written off. Now the sweep declines to decide.
    """
    coupon = _coupon(client, monkeypatch)
    monkeypatch.setattr(payments, "charge", _die_before_capturing)
    cart_id = new_cart(client, "p_monitor", 2)
    order_id = client.post(f"/carts/{cart_id}/checkout",
                           json={"coupon_code": coupon},
                           headers={"Idempotency-Key": "unvoidable-1"}
                           ).json()["error"]["details"]["order_id"]

    def cannot_void(order_id):
        raise payments.PaymentUnavailable("provider unreachable")

    monkeypatch.setattr(payments, "void", cannot_void)
    assert client.post("/admin/orders/reconcile?stale_after_seconds=0").json() == {
        "examined": 1, "settled": [], "compensated": [], "unresolved": [order_id]}

    order = client.get(f"/orders/{order_id}").json()
    assert order["status"] == "pending_payment"
    assert order["resolution"] is None

    # Everything stays claimed. Releasing it is what would cost money.
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_monitor"] == 5
    assert client.get(f"/carts/{cart_id}").json()["status"] == "checked_out"
    assert client.get("/admin/coupons").json()["coupons"][0]["redeemed_order_id"] == order_id
    assert client.get("/admin/report").json()["unsettled"][
        "orders_awaiting_payment"] == 1

    # Once the provider comes back, the next sweep resolves it for real.
    monkeypatch.setattr(payments, "void", lambda oid: None)
    assert client.post("/admin/orders/reconcile?stale_after_seconds=0"
                       ).json()["compensated"] == [order_id]
    assert client.get(f"/orders/{order_id}").json()["resolution"] == "voided"


def test_a_voided_charge_can_never_be_captured_afterwards(client, monkeypatch):
    """The provider guarantee the whole fix rests on.

    If a void were not terminal, compensating after one would be just as unsafe
    as compensating on a stale status check, and the refund path would be back.
    """
    order_id = leave_order_pending(client, new_cart(client, "p_dock", 1), monkeypatch)
    client.post("/admin/orders/reconcile?stale_after_seconds=0")

    with pytest.raises(payments.PaymentDeclined):
        payments.charge(order_id, 14999)     # the real provider, not a stub
    assert order_id not in payments._charges

    # The stock really did come back, and is sellable by someone else.
    assert client.post(f"/carts/{new_cart(client, 'p_dock', 1)}/checkout"
                       ).status_code == 201


def test_the_schema_refuses_to_write_off_an_order_without_a_verdict(client,
                                                                   monkeypatch):
    """INV-9 is a constraint, not a convention.

    Bypassing every line of application code, it is still impossible to record
    a written-off order that no provider ever declined or voided.
    """
    order_id = leave_order_pending(client, new_cart(client, "p_monitor", 1),
                                   monkeypatch)

    with pytest.raises(sqlite3.IntegrityError):
        with store.write_txn() as c:
            c.execute("UPDATE orders SET status = 'payment_failed' WHERE id = ?",
                      (order_id,))

    with pytest.raises(sqlite3.IntegrityError):
        with store.write_txn() as c:
            c.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (order_id,))

    assert client.get(f"/orders/{order_id}").json()["status"] == "pending_payment"


def test_a_charge_that_lands_after_compensation_returns_402_not_a_failed_order(
        client, monkeypatch):
    """checkout's last guard. Replacing its condition with `if False:` left the
    whole suite green, so nothing caught a 201 carrying a payment_failed order
    whose stock and coupon had already been released.

    The sweep voids the charge while it is in flight, so the provider refuses
    the capture and the caller must be told the payment did not complete.
    """
    in_flight = threading.Event()

    def charge_across_the_sweep(order_id, amount_cents):
        in_flight.set()
        time.sleep(0.4)                       # the sweep runs inside this window
        return _real_charge(order_id, amount_cents)

    monkeypatch.setattr(payments, "charge", charge_across_the_sweep)
    cart_id = new_cart(client, "p_monitor", 2)

    result = {}
    worker = threading.Thread(
        target=lambda: result.update(r=client.post(f"/carts/{cart_id}/checkout")))
    worker.start()
    assert in_flight.wait(5)
    sweep = client.post("/admin/orders/reconcile?stale_after_seconds=0").json()
    assert len(sweep["compensated"]) == 1, sweep
    worker.join(10)

    r = result["r"]
    assert (r.status_code, code_of(r)) == (402, "PAYMENT_FAILED"), r.text
    order_id = r.json()["error"]["details"]["order_id"]
    assert client.get(f"/orders/{order_id}").json()["resolution"] == "voided"

    # The void held: nothing was captured, and the stock came back.
    assert order_id not in payments._charges
    stock = {p["id"]: p["inventory"] for p in client.get("/products").json()["products"]}
    assert stock["p_monitor"] == 7
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"
