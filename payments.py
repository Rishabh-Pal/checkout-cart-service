"""Stand-in payment provider.

This sits behind its own module for a reason that is structural, not
cosmetic: a real provider call is network I/O that must happen *outside* the
database transaction, and the recovery path needs a way to find out what
actually happened when we crashed or timed out before recording the result.

The important operation is `void`, not `charge`. Asking "did you capture?" can
only ever describe a moment that has already passed -- a charge that had not
been captured when we asked may be captured a millisecond later, and an order
compensated on the strength of that answer leaves the provider holding money
for an order we wrote off. So recovery does not ask. It *tells*: void the
charge, which is terminal at the provider, and only then is compensation safe.

`_charges` stands in for the provider's own ledger. It is deliberately not our
database: the whole problem this module exists to model is that the provider's
record and ours can disagree, and only the provider can resolve it.
"""

import uuid


class PaymentDeclined(Exception):
    """The provider refused. Definitive: no money moved, and none ever will."""


class PaymentUnavailable(Exception):
    """Timeout or transport failure. NOT definitive -- nothing is decided, and
    the caller must not infer an outcome from it."""


_charges: dict[str, str] = {}
_voided: set[str] = set()


def charge(order_id, amount_cents):
    """Capture `amount_cents` for `order_id`.

    `order_id` is the provider's idempotency key, so charging the same order
    twice returns the original reference instead of taking the money again.
    """
    if order_id in _voided:
        raise PaymentDeclined("This charge was voided and can never be captured.")
    if order_id in _charges:
        return _charges[order_id]
    reference = "pay_" + uuid.uuid4().hex[:16]
    _charges[order_id] = reference
    return reference


def void(order_id):
    """Guarantee this charge can never be captured, now or later.

    Returns None once the charge is permanently voided, or the capture
    reference if the provider had already captured it -- in which case the
    caller must settle, not compensate.

    Raises PaymentUnavailable if the provider could not be reached, or if the
    payment method cannot be voided because it is still in flight. Either way
    NOTHING has been decided and the caller must leave the order alone.

    The atomicity that matters here is the provider's, not this fake's: a real
    void either wins the race against a capture or reports that it lost.
    """
    if order_id in _charges:
        return _charges[order_id]
    _voided.add(order_id)
    return None


def _reset():
    """Test hook: forget the provider's ledger."""
    _charges.clear()
    _voided.clear()
