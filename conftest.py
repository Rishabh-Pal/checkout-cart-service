import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import uvicorn

import app
import payments
import store


@pytest.fixture(autouse=True)
def clean_provider_ledger():
    """`payments` keeps its fake ledger in module state, so it has to be reset
    for EVERY test, not just the ones that happen to request `client`. Teardown
    too, so a failing test cannot leak a capture into the next one."""
    payments._reset()
    yield
    payments._reset()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A fresh seeded database behind a real uvicorn server on a random port.

    Deliberately a live server rather than Starlette's TestClient. Both do in
    fact overlap requests today (measured: 6 handlers in flight at once), but
    TestClient's parallelism is a property of its internal portal rather than
    a documented guarantee, and it is mid-deprecation. A real socket and a
    real threadpool leave nothing to interpret -- and if these requests ever
    stopped overlapping, every concurrency test below would silently start
    passing for the wrong reason.
    """
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db(db, reset=True)

    server = uvicorn.Server(uvicorn.Config(
        app.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60.0) as c:
            yield c
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def concurrently():
    """Run `fn(i)` on `n` threads released simultaneously by a barrier.

    The barrier matters: without it the pool tends to finish task 0 before
    task 1 starts and the test silently degrades into a sequential one.
    """
    def run(fn, n):
        barrier = threading.Barrier(n)

        def wrapped(i):
            barrier.wait()
            return fn(i)

        with ThreadPoolExecutor(max_workers=n) as pool:
            return [f.result() for f in [pool.submit(wrapped, i) for i in range(n)]]

    return run


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def new_cart(client, product_id="p_cable", quantity=1):
    cart_id = client.post("/carts").json()["id"]
    r = client.post(f"/carts/{cart_id}/items",
                    json={"product_id": product_id, "quantity": quantity})
    assert r.status_code == 201, r.text
    return cart_id


def place_order(client, product_id="p_cable", quantity=1, coupon_code=None):
    cart_id = new_cart(client, product_id, quantity)
    r = client.post(f"/carts/{cart_id}/checkout", json={"coupon_code": coupon_code})
    assert r.status_code == 201, r.text
    return r.json()


def code_of(response):
    return response.json()["error"]["code"]
