"""HTTP surface. All business rules live in store.py; this file only maps
JSON <-> domain calls and turns domain failures into a stable error envelope.

Route handlers are deliberately `def`, not `async def`: FastAPI then runs them
in a worker threadpool, so concurrent requests really do hit the database at
the same time (and the concurrency tests exercise the real thing).
"""

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import store


@asynccontextmanager
async def lifespan(_app):
    store.init_db()
    yield


app = FastAPI(
    title="Checkout & Rewards Service",
    version="1.0.0",
    summary="Carts, checkout with inventory + idempotency guarantees, and "
            "milestone reward coupons.",
    lifespan=lifespan,
)


def _error(status, code, message, **details):
    # `details` is always present, even when empty: the README promises one
    # error shape, and a client should never need to branch on whether the key
    # exists before reading it.
    return JSONResponse(status_code=status, content={
        "error": {"code": code, "message": message, "details": details}})


@app.exception_handler(store.ApiError)
def _api_error(_request, exc):
    return _error(exc.status, exc.code, exc.message, **exc.details)


@app.exception_handler(RequestValidationError)
def _validation_error(_request, exc):
    return _error(422, "VALIDATION_ERROR", "The request body or path is invalid.",
                  violations=[{"field": ".".join(str(p) for p in e["loc"][1:]),
                               "problem": e["msg"]} for e in exc.errors()])


@app.exception_handler(sqlite3.IntegrityError)
def _integrity_error(_request, exc):
    # A database constraint caught something the application logic missed.
    # Surface it as a conflict rather than a 500 -- the invariant held.
    # The driver's message names tables, columns and verbatim CHECK expressions
    # and changes between SQLite versions, so it is logged, never returned.
    logging.getLogger(__name__).warning("constraint violation: %s", exc)
    return _error(409, "CONSTRAINT_VIOLATION",
                  "The request conflicts with a database constraint.")


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------

class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable code. This is the "
                                  "contract; `message` is for humans.",
                      examples=["OUT_OF_STOCK"])
    message: str
    details: dict = Field(default_factory=dict,
                          description="Always present, `{}` when empty.")


class ErrorEnvelope(BaseModel):
    """The single error shape. Declared so /openapi.json documents the error
    contract too -- FastAPI otherwise emits only success responses, and types
    the 422 as its own HTTPValidationError, which this service does not use."""
    error: ErrorBody


def _errs(mapping):
    return {status: {"model": ErrorEnvelope, "description": codes}
            for status, codes in mapping.items()}


MAX_LINE_QUANTITY = 1_000_000


class AddItem(BaseModel):
    product_id: str = Field(min_length=1)
    # Upper bound is not decoration: an unbounded int reaches SQLite's INTEGER
    # column and raises OverflowError, which surfaces as a bare 500.
    quantity: int = Field(ge=1, le=MAX_LINE_QUANTITY,
                          description="Added to any quantity already in the cart.")


class SetQuantity(BaseModel):
    quantity: int = Field(ge=1, le=MAX_LINE_QUANTITY,
                          description="Use DELETE to remove a line.")


class Checkout(BaseModel):
    coupon_code: str | None = None


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

@app.get("/products", tags=["catalogue"])
def get_products():
    return {"products": store.list_products()}


# --------------------------------------------------------------------------
# carts
# --------------------------------------------------------------------------

@app.post("/carts", status_code=201, tags=["carts"])
def post_cart():
    return store.create_cart()


@app.get("/carts/{cart_id}", tags=["carts"],
         responses=_errs({404: "CART_NOT_FOUND",
                          422: "VALIDATION_ERROR"}))
def get_cart(cart_id: str):
    return store.get_cart(cart_id)


@app.post("/carts/{cart_id}/items", status_code=201, tags=["carts"],
          responses=_errs({404: "PRODUCT_NOT_FOUND, CART_NOT_FOUND",
                           409: "CART_ALREADY_CHECKED_OUT",
                           422: "VALIDATION_ERROR"}))
def post_cart_item(cart_id: str, body: AddItem):
    return store.add_item(cart_id, body.product_id, body.quantity)


@app.patch("/carts/{cart_id}/items/{product_id}", tags=["carts"],
           responses=_errs({404: "CART_ITEM_NOT_FOUND, PRODUCT_NOT_FOUND, "
                                 "CART_NOT_FOUND",
                            409: "CART_ALREADY_CHECKED_OUT",
                            422: "VALIDATION_ERROR"}))
def patch_cart_item(cart_id: str, product_id: str, body: SetQuantity):
    return store.set_item_quantity(cart_id, product_id, body.quantity)


@app.delete("/carts/{cart_id}/items/{product_id}", status_code=204,
            tags=["carts"],
            responses=_errs({404: "CART_ITEM_NOT_FOUND, CART_NOT_FOUND",
                             409: "CART_ALREADY_CHECKED_OUT",
                             422: "VALIDATION_ERROR"}))
def delete_cart_item(cart_id: str, product_id: str):
    store.remove_item(cart_id, product_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------
# checkout & orders
# --------------------------------------------------------------------------

@app.post("/carts/{cart_id}/checkout", status_code=201, tags=["checkout"],
          response_description="The placed order. Header "
                               "`Idempotent-Replay: true|false` says whether "
                               "this is a replay of an earlier identical request.",
          responses=_errs({402: "PAYMENT_FAILED",
                           404: "CART_NOT_FOUND, COUPON_NOT_FOUND",
                           409: "CART_ALREADY_CHECKED_OUT, CHECKOUT_IN_PROGRESS, "
                                "OUT_OF_STOCK, COUPON_ALREADY_REDEEMED, "
                                "IDEMPOTENCY_KEY_REUSED",
                           422: "CART_EMPTY, VALIDATION_ERROR",
                           503: "PAYMENT_RESULT_UNKNOWN"}))
def post_checkout(
    cart_id: str,
    response: Response,
    body: Checkout | None = None,
    idempotency_key: str | None = Header(
        None, alias="Idempotency-Key", max_length=200,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description="Send the same key when retrying. The original response is "
                    "replayed instead of a second order being placed. Scoped to "
                    "this cart, so it cannot collide with another client's key. "
                    "A retry arriving while the original charge is still in "
                    "flight gets 409 CHECKOUT_IN_PROGRESS."),
):
    order, replayed = store.checkout(
        cart_id,
        coupon_code=(body.coupon_code if body else None),
        idempotency_key=idempotency_key,
    )
    # A replay returns the original response verbatim; the header is the only
    # difference, so a client that ignores it still behaves correctly.
    response.headers["Idempotent-Replay"] = "true" if replayed else "false"
    return order


@app.get("/orders/{order_id}", tags=["checkout"],
         responses=_errs({404: "ORDER_NOT_FOUND",
                          422: "VALIDATION_ERROR"}))
def get_order(order_id: str):
    return store.get_order(order_id)


# --------------------------------------------------------------------------
# administrative
# --------------------------------------------------------------------------

@app.post("/admin/coupons", status_code=201, tags=["admin"],
          responses=_errs({409: "NO_ELIGIBLE_MILESTONE"}))
def post_admin_coupon():
    return store.generate_coupon()


@app.get("/admin/coupons", tags=["admin"])
def get_admin_coupons():
    return {"coupons": store.list_coupons()}


@app.get("/admin/orders", tags=["admin"],
         responses=_errs({422: "VALIDATION_ERROR"}))
def get_admin_orders(
    status: Literal["pending_payment", "paid", "payment_failed"] | None = None,
):
    """Pass `?status=paid` to reconcile against the report, which counts only
    paid orders. An unrecognised status is a 422, not an empty list."""
    return {"orders": store.list_orders(status)}


@app.post("/admin/orders/reconcile", tags=["admin"],
          responses=_errs({422: "VALIDATION_ERROR"}))
def post_admin_reconcile(
    stale_after_seconds: int = Query(store.STALE_AFTER_SECONDS, ge=0, le=86_400),
):
    """Resolve orders stuck in `pending_payment` by voiding the charge.

    This is the recovery path for a crash, timeout or deploy that landed
    between the charge and the settle. It does not ask the provider whether the
    charge was captured -- that answer is stale the moment it arrives -- it
    voids the charge, which is terminal, and only then compensates. In
    production it is a scheduled job; exposing it as an endpoint makes it
    observable and testable. Idempotent.

    `ge=0` matters: a negative window produced an invalid SQLite datetime
    modifier, so the sweep silently examined nothing and reported all clear.
    """
    return store.reconcile_pending_orders(stale_after_seconds)


@app.get("/admin/report", tags=["admin"])
def get_admin_report():
    return store.report()
