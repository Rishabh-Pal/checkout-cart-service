"""Business rules and failure modes: validation, state transitions, money,
snapshotting, and report reconciliation."""

import sqlite3

import pytest

import payments
import store
from conftest import code_of, new_cart, place_order


# --------------------------------------------------------------------------
# cart validation
# --------------------------------------------------------------------------

def test_invalid_products_and_quantities_never_enter_a_cart(client):
    cart_id = client.post("/carts").json()["id"]

    r = client.post(f"/carts/{cart_id}/items", json={"product_id": "ghost", "quantity": 1})
    assert (r.status_code, code_of(r)) == (404, "PRODUCT_NOT_FOUND")

    for bad in (0, -3):
        r = client.post(f"/carts/{cart_id}/items",
                        json={"product_id": "p_cable", "quantity": bad})
        assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR")

    r = client.post(f"/carts/{cart_id}/items",
                    json={"product_id": "p_cable", "quantity": "two"})
    assert r.status_code == 422

    assert client.get(f"/carts/{cart_id}").json()["items"] == []


def test_adding_the_same_product_twice_accumulates_and_patch_replaces(client):
    cart_id = new_cart(client, "p_cable", 2)
    client.post(f"/carts/{cart_id}/items", json={"product_id": "p_cable", "quantity": 3})
    assert client.get(f"/carts/{cart_id}").json()["items"][0]["quantity"] == 5

    client.patch(f"/carts/{cart_id}/items/p_cable", json={"quantity": 2})
    cart = client.get(f"/carts/{cart_id}").json()
    assert cart["items"][0]["quantity"] == 2
    assert cart["subtotal_cents"] == 1998

    assert client.delete(f"/carts/{cart_id}/items/p_cable").status_code == 204
    assert client.get(f"/carts/{cart_id}").json()["subtotal_cents"] == 0

    r = client.delete(f"/carts/{cart_id}/items/p_cable")
    assert (r.status_code, code_of(r)) == (404, "CART_ITEM_NOT_FOUND")


def test_unknown_cart_and_order_are_404(client):
    assert code_of(client.get("/carts/cart_nope")) == "CART_NOT_FOUND"
    assert code_of(client.get("/orders/ord_nope")) == "ORDER_NOT_FOUND"


# --------------------------------------------------------------------------
# price and availability drift
# --------------------------------------------------------------------------

def _set_product(price_cents=None, inventory=None, name=None, product_id="p_monitor"):
    sets, args = [], []
    for column, value in (("price_cents", price_cents), ("inventory", inventory),
                          ("name", name)):
        if value is not None:
            sets.append(f"{column} = ?")
            args.append(value)
    with store.write_txn() as c:
        c.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?",
                  (*args, product_id))


def test_cart_reprices_live_and_flags_unavailable_lines(client):
    cart_id = new_cart(client, "p_monitor", 3)
    assert client.get(f"/carts/{cart_id}").json()["subtotal_cents"] == 98997

    _set_product(price_cents=20000, inventory=1)
    cart = client.get(f"/carts/{cart_id}").json()
    assert cart["subtotal_cents"] == 60000, "cart must show the current price"
    assert cart["items"][0]["in_stock"] is False
    assert cart["items"][0]["available_inventory"] == 1

    # in_stock is advisory; the hard stop is checkout.
    r = client.post(f"/carts/{cart_id}/checkout")
    assert (r.status_code, code_of(r)) == (409, "OUT_OF_STOCK")
    assert r.json()["error"]["details"] == {
        "product_id": "p_monitor", "requested": 3, "available": 1}


def test_order_is_a_snapshot_and_survives_product_changes(client):
    order = place_order(client, "p_monitor", 2)
    assert order["subtotal_cents"] == 65998

    _set_product(price_cents=1, name="Renamed Monitor")

    fetched = client.get(f"/orders/{order['id']}").json()
    assert fetched == order
    assert fetched["items"][0]["product_name"] == "27-inch 4K Monitor"
    assert fetched["items"][0]["unit_price_cents"] == 32999


# --------------------------------------------------------------------------
# checkout state machine
# --------------------------------------------------------------------------

def test_a_cart_checks_out_at_most_once_and_then_freezes(client):
    cart_id = new_cart(client, "p_mouse", 1)
    order_id = client.post(f"/carts/{cart_id}/checkout").json()["id"]

    r = client.post(f"/carts/{cart_id}/checkout")
    assert (r.status_code, code_of(r)) == (409, "CART_ALREADY_CHECKED_OUT")
    assert r.json()["error"]["details"]["order_id"] == order_id

    for response in (
        client.post(f"/carts/{cart_id}/items", json={"product_id": "p_cable", "quantity": 1}),
        client.patch(f"/carts/{cart_id}/items/p_mouse", json={"quantity": 9}),
        client.delete(f"/carts/{cart_id}/items/p_mouse"),
    ):
        assert (response.status_code, code_of(response)) == (409, "CART_ALREADY_CHECKED_OUT")


def test_empty_cart_cannot_check_out_and_stays_open(client):
    cart_id = client.post("/carts").json()["id"]
    r = client.post(f"/carts/{cart_id}/checkout")
    assert (r.status_code, code_of(r)) == (422, "CART_EMPTY")
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"


def test_an_idempotency_key_is_scoped_to_its_cart(client, monkeypatch):
    """Two clients may use the same key on different carts; one client may not
    reuse it on the same cart with different intent."""
    first = new_cart(client, "p_cable", 1)
    second = new_cart(client, "p_cable", 1)
    headers = {"Idempotency-Key": "retry"}

    a = client.post(f"/carts/{first}/checkout", headers=headers)
    b = client.post(f"/carts/{second}/checkout", headers=headers)
    assert (a.status_code, b.status_code) == (201, 201), b.text
    assert a.json()["id"] != b.json()["id"], "one client's key blocked another's"

    # Same key, same cart, different coupon -> different intent, so rejected.
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 1)
    coupon = client.post("/admin/coupons").json()["code"]
    third = new_cart(client, "p_cable", 1)
    assert client.post(f"/carts/{third}/checkout", headers=headers).status_code == 201
    r = client.post(f"/carts/{third}/checkout", json={"coupon_code": coupon},
                    headers=headers)
    assert (r.status_code, code_of(r)) == (409, "IDEMPOTENCY_KEY_REUSED")


def test_a_malformed_idempotency_key_is_rejected_at_the_boundary(client):
    cart_id = new_cart(client, "p_cable", 1)
    # A length cap and a charset, not an arbitrary minimum: the squatting
    # problem is solved by scoping the key to its cart, not by length.
    # (a newline is rejected by httpx before it reaches us, so not tested here)
    for bad in ("has spaces", "a" * 300, "semi;colon"):
        r = client.post(f"/carts/{cart_id}/checkout",
                        headers={"Idempotency-Key": bad})
        assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR"), bad
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"


# --------------------------------------------------------------------------
# coupons
# --------------------------------------------------------------------------

def test_coupons_are_minted_once_per_reached_milestone(client, monkeypatch):
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 2)

    r = client.post("/admin/coupons")
    assert (r.status_code, code_of(r)) == (409, "NO_ELIGIBLE_MILESTONE")
    assert r.json()["error"]["details"]["next_milestone_at_order"] == 2

    place_order(client)
    assert client.post("/admin/coupons").status_code == 409

    place_order(client)  # 2nd order -> milestone 1
    first = client.post("/admin/coupons").json()
    assert (first["milestone"], first["earned_at_order_number"]) == (1, 2)
    assert first["percent_off"] == 10
    assert client.post("/admin/coupons").status_code == 409, "no double reward"

    place_order(client)
    place_order(client)  # 4th order -> milestone 2
    assert client.post("/admin/coupons").json()["milestone"] == 2
    assert [c["milestone"] for c in client.get("/admin/coupons").json()["coupons"]] == [1, 2]


def test_a_coupon_redeems_once_and_only_against_a_successful_order(client, monkeypatch):
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 1)
    place_order(client)
    coupon = client.post("/admin/coupons").json()["code"]

    # An unknown code is a 404 and leaves the cart open.
    cart_id = new_cart(client, "p_keyboard", 1)
    r = client.post(f"/carts/{cart_id}/checkout", json={"coupon_code": "NOPE"})
    assert (r.status_code, code_of(r)) == (404, "COUPON_NOT_FOUND")
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"

    order = client.post(f"/carts/{cart_id}/checkout",
                        json={"coupon_code": coupon}).json()
    assert order["discount_cents"] == 899          # floor(8999 * 10 / 100)
    assert order["total_cents"] == 8100
    assert order["coupon"] == {"code": coupon, "percent_off": 10}

    again = client.post(f"/carts/{new_cart(client)}/checkout",
                        json={"coupon_code": coupon})
    assert (again.status_code, code_of(again)) == (409, "COUPON_ALREADY_REDEEMED")


def test_discount_floors_and_a_total_never_goes_negative(client, monkeypatch):
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 1)
    monkeypatch.setattr(store, "COUPON_PERCENT_OFF", 100)
    place_order(client)
    coupon = client.post("/admin/coupons").json()["code"]

    order = client.post(f"/carts/{new_cart(client, 'p_cable', 3)}/checkout",
                        json={"coupon_code": coupon}).json()
    assert (order["subtotal_cents"], order["discount_cents"], order["total_cents"]) \
        == (2997, 2997, 0)

    # 10% of 999 is 99.9 -> floors to 99, never 100.
    assert store.discount_cents_for(999, 10) == 99
    assert store.discount_cents_for(1, 10) == 0


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def test_report_reconciles_with_orders_and_coupons_and_never_mutates(client, monkeypatch):
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 2)
    place_order(client, "p_cable", 4)
    place_order(client, "p_mouse", 1)
    coupon = client.post("/admin/coupons").json()["code"]
    place_order(client, "p_keyboard", 2, coupon_code=coupon)
    client.post(f"/carts/{new_cart(client, 'p_dock', 5)}/checkout")  # fails: out of stock

    # A payment-failed order exists too, and must be excluded from every figure.
    def decline(order_id, amount_cents):
        raise payments.PaymentDeclined("declined")

    monkeypatch.setattr(payments, "charge", decline)
    declined = client.post(f"/carts/{new_cart(client, 'p_mouse', 3)}/checkout")
    assert declined.status_code == 402

    report = client.get("/admin/report").json()
    orders = client.get("/admin/orders?status=paid").json()["orders"]
    assert len(client.get("/admin/orders").json()["orders"]) == 4, "audit trail kept"
    coupons = client.get("/admin/coupons").json()["coupons"]

    assert report["orders_placed"] == len(orders) == 3
    assert report["gross_revenue_cents"] == sum(o["subtotal_cents"] for o in orders)
    assert report["total_discounts_cents"] == sum(o["discount_cents"] for o in orders)
    assert report["net_revenue_cents"] == sum(o["total_cents"] for o in orders)
    assert report["net_revenue_cents"] == (report["gross_revenue_cents"]
                                           - report["total_discounts_cents"])

    expected = {}
    for o in orders:
        for item in o["items"]:
            expected[item["product_id"]] = expected.get(item["product_id"], 0) + item["quantity"]
    assert {p["product_id"]: p["quantity"]
            for p in report["purchased_quantity_by_product"]} == expected
    assert sum(p["gross_revenue_cents"] for p in report["purchased_quantity_by_product"]) \
        == report["gross_revenue_cents"]

    assert report["coupons"] == {
        "generated": len(coupons),
        "redeemed": sum(c["redeemed_order_id"] is not None for c in coupons),
        "available": sum(c["redeemed_order_id"] is None for c in coupons),
    }

    assert report["unsettled"] == {"orders_awaiting_payment": 0, "value_cents": 0,
                                   "orders_payment_failed": 1}

    assert client.get("/admin/report").json() == report, "reporting must be read-only"


# --------------------------------------------------------------------------
# invariants enforced below the application
# --------------------------------------------------------------------------

def test_the_schema_refuses_a_second_coupon_for_one_milestone_or_one_order(
        client, monkeypatch):
    """INV-3 and INV-4 are UNIQUE constraints, and DECISIONS calls them the
    backstops that hold even if the application read-then-check is not
    exclusive. Dropping either one left the whole suite green, so nothing
    exercised them. This does, in raw SQL, below every guard in store.py."""
    monkeypatch.setattr(store, "MILESTONE_EVERY_N", 1)
    order = place_order(client)
    first = client.post("/admin/coupons").json()["code"]

    with pytest.raises(sqlite3.IntegrityError):                       # INV-3
        with store.write_txn() as c:
            c.execute("INSERT INTO coupons (code, percent_off, milestone) "
                      "VALUES ('SECOND-FOR-MILESTONE-1', 10, 1)")

    with store.write_txn() as c:
        c.execute("UPDATE coupons SET redeemed_order_id = ? WHERE code = ?",
                  (order["id"], first))
        c.execute("INSERT INTO coupons (code, percent_off, milestone) "
                  "VALUES ('OTHER', 10, 2)")
    with pytest.raises(sqlite3.IntegrityError):                       # INV-4
        with store.write_txn() as c:
            c.execute("UPDATE coupons SET redeemed_order_id = ? WHERE code = 'OTHER'",
                      (order["id"],))


def test_compensate_refuses_to_write_off_an_order_without_a_verdict(client,
                                                                   monkeypatch):
    """The application half of INV-9. The schema half is tested in
    test_payment_recovery; replacing this guard with `if False:` left the whole
    suite green, so the two halves were not both pinned."""
    from test_payment_recovery import leave_order_pending
    order_id = leave_order_pending(client, new_cart(client, "p_monitor", 1),
                                   monkeypatch)
    for bad in (None, "", "unknown", "captured", "refunded"):
        with pytest.raises(ValueError):
            store._compensate(order_id, bad)
    assert client.get(f"/orders/{order_id}").json()["status"] == "pending_payment"


# --------------------------------------------------------------------------
# validation at the HTTP boundary
# --------------------------------------------------------------------------

def test_patching_a_line_that_is_absent_or_unknown_is_404(client):
    cart_id = new_cart(client, "p_cable", 1)
    r = client.patch(f"/carts/{cart_id}/items/p_mouse", json={"quantity": 2})
    assert (r.status_code, code_of(r)) == (404, "CART_ITEM_NOT_FOUND")
    r = client.patch(f"/carts/{cart_id}/items/ghost", json={"quantity": 2})
    assert (r.status_code, code_of(r)) == (404, "PRODUCT_NOT_FOUND")
    assert client.get(f"/carts/{cart_id}").json()["items"][0]["quantity"] == 1


def test_an_unbounded_quantity_is_rejected_not_a_500(client):
    """Without an upper bound this reached SQLite's INTEGER column and raised
    OverflowError, which surfaced as a bare 500 with an empty body."""
    cart_id = client.post("/carts").json()["id"]
    r = client.post(f"/carts/{cart_id}/items",
                    json={"product_id": "p_cable", "quantity": 2 ** 63})
    assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR")
    assert client.get(f"/carts/{cart_id}").json()["items"] == []


def test_an_unknown_order_status_filter_is_rejected_not_silently_empty(client):
    place_order(client)
    for bad in ("bogus", "PAID", "placed", ""):
        r = client.get("/admin/orders", params={"status": bad})
        assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR"), bad
    assert len(client.get("/admin/orders?status=paid").json()["orders"]) == 1


def test_a_negative_staleness_window_is_rejected_not_a_silent_no_op(client):
    """A negative value produced the invalid SQLite modifier '--5 seconds', so
    datetime() returned NULL, no row matched, and the sweep reported all clear
    while orders stayed stuck."""
    r = client.post("/admin/orders/reconcile?stale_after_seconds=-5")
    assert (r.status_code, code_of(r)) == (422, "VALIDATION_ERROR")
    assert client.post("/admin/orders/reconcile").json()["examined"] == 0


def test_the_report_on_an_empty_database_is_zeroes_not_nulls(client):
    """Pins the COALESCE guards on the money aggregates: without them these
    come back null with no paid orders."""
    report = client.get("/admin/report").json()
    assert report["orders_placed"] == 0
    assert report["purchased_quantity_by_product"] == []
    for key in ("gross_revenue_cents", "total_discounts_cents", "net_revenue_cents"):
        assert report[key] == 0, key
    assert report["coupons"] == {"generated": 0, "redeemed": 0, "available": 0}
    assert report["unsettled"] == {"orders_awaiting_payment": 0, "value_cents": 0,
                                   "orders_payment_failed": 0}
