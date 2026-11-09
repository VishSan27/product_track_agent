import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from product_monitor import (
    CheckResult,
    ProductInputError,
    ProductStore,
    inspect_snapshot,
    validate_product_url,
)
from web_app import create_handler


class ProductValidationTests(unittest.TestCase):
    def test_accepts_https_product_url(self) -> None:
        self.assertEqual(
            validate_product_url(" https://shop.example/products/item "),
            "https://shop.example/products/item",
        )

    def test_rejects_non_https_private_and_checkout_urls(self) -> None:
        for value in (
            "http://shop.example/item",
            "https://127.0.0.1/item",
            "https://localhost/item",
            "https://shop.example/checkout",
            "https://shop.example/cart",
            "https://user:pass@shop.example/item",
        ):
            with self.subTest(value=value), self.assertRaises(ProductInputError):
                validate_product_url(value)

    def test_snapshot_maps_availability_and_parses_price(self) -> None:
        result = inspect_snapshot(
            {
                "title": "Sample item",
                "stock": "in_stock",
                "priceText": "₹4,500",
                "source": "structured product data",
            }
        )
        self.assertEqual(result.status, "in_stock")
        self.assertEqual(result.title, "Sample item")
        self.assertEqual(result.price, "4500")
        self.assertEqual(inspect_snapshot({"stock": None}).status, "unknown")


class ProductStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = ProductStore(Path(self.temp_dir.name) / "products.sqlite3")

    def test_product_lifecycle_and_notification_change(self) -> None:
        product = self.store.add_product(
            {
                "url": "https://shop.example/products/item",
                "in_stock_selector": "button.add",
                "interval_seconds": 300,
            }
        )
        self.assertEqual(product["status"], "unknown")
        self.assertEqual(len(self.store.due_products()), 1)

        result = CheckResult("in_stock", "Sample item", "4500", "structured product data")
        self.assertTrue(self.store.save_result(product["id"], result))
        self.assertFalse(self.store.save_result(product["id"], result))
        saved = self.store.get_product(product["id"])
        self.assertEqual(saved["status"], "in_stock")
        self.assertEqual(saved["price"], "4500")
        self.assertEqual(saved["last_error"], None)

        self.store.request_check(product["id"])
        self.assertEqual(len(self.store.due_products()), 1)
        self.assertTrue(self.store.remove_product(product["id"]))
        self.assertIsNone(self.store.get_product(product["id"]))

    def test_unknown_result_does_not_erase_last_known_status(self) -> None:
        product = self.store.add_product({"url": "https://shop.example/item"})
        self.store.save_result(
            product["id"],
            CheckResult("in_stock", "Sample item", None, "purchase button"),
        )
        self.store.save_result(product["id"], CheckResult("unknown", "Sample item", None, None))
        saved = self.store.get_product(product["id"])
        self.assertEqual(saved["status"], "in_stock")
        self.assertIsNotNone(saved["last_error"])

    def test_duplicate_link_does_not_create_duplicate_rows(self) -> None:
        first = self.store.add_product({"url": "https://shop.example/item"})
        second = self.store.add_product(
            {
                "url": "https://shop.example/item",
                "price_selector": ".price",
            }
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_products()), 1)
        self.assertEqual(self.store.get_product(first["id"])["price_selector"], ".price")


class DashboardApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = ProductStore(Path(self.temp_dir.name) / "products.sqlite3")
        self.token = "test-token"

        class FakeMonitor:
            def __init__(self) -> None:
                self.wake_count = 0

            def wake(self) -> None:
                self.wake_count += 1

        self.monitor = FakeMonitor()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            create_handler(self.store, self.monitor, self.token),
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self._close_server)
        self.host, self.port = self.server.server_address

    def _close_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if token is not None:
            headers["X-App-Token"] = token
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body)
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_serves_tokenized_frontend_and_protects_api(self) -> None:
        status, page = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.token.encode(), page)
        self.assertEqual(self._request("GET", "/api/products")[0], 403)
        self.assertEqual(self._request("GET", "/api/products", token=self.token)[0], 200)

    def test_add_product_wakes_monitor_and_rejects_invalid_url(self) -> None:
        status, body = self._request(
            "POST",
            "/api/products",
            {"url": "https://shop.example/item"},
            self.token,
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["product"]["status"], "unknown")
        self.assertEqual(self.monitor.wake_count, 1)

        status, body = self._request(
            "POST",
            "/api/products",
            {"url": "http://shop.example/item"},
            self.token,
        )
        self.assertEqual(status, 400)
        self.assertIn("HTTPS", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
