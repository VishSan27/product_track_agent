"""Run the local product-tracking dashboard."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from product_monitor import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    MAX_ORDER_TOTAL_INR,
    MIN_POLL_INTERVAL_SECONDS,
    MonitorService,
    ProductInputError,
    ProductStore,
)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 16_384
MAX_TRACKED_PRODUCTS = 100
LOGGER = logging.getLogger("web_app")


def create_handler(store: ProductStore, monitor: MonitorService, token: str):
    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "LocalStockMonitor/1.0"

        def log_message(self, format_string: str, *args: Any) -> None:
            LOGGER.info("%s - %s", self.address_string(), format_string % args)

        def _valid_host(self) -> bool:
            return self.headers.get("Host", "").lower() in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def _send_json(self, status: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, filename: str, content_type: str) -> None:
            path = STATIC_DIR / filename
            try:
                body = path.read_bytes()
            except OSError:
                self._send_json(500, {"error": "Dashboard asset is missing."})
                return
            if filename == "index.html":
                body = body.replace(b"__APP_TOKEN__", token.encode("ascii"))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not self._valid_host():
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                expected_host = self.headers.get("Host", "").lower()
                if parsed.scheme != "http" or parsed.netloc.lower() != expected_host:
                    return False
            return secrets.compare_digest(self.headers.get("X-App-Token", ""), token)

        def do_GET(self) -> None:
            if not self._valid_host():
                self._send_json(403, {"error": "Local dashboard access only."})
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._send_static("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._send_static("app.js", "text/javascript; charset=utf-8")
            elif path == "/app.css":
                self._send_static("app.css", "text/css; charset=utf-8")
            elif path == "/api/products":
                if not self._authorized():
                    self._send_json(403, {"error": "Dashboard token missing or invalid."})
                    return
                self._send_json(
                    200,
                    {
                        "products": store.list_products(),
                        "settings": {
                            "minimum_interval_seconds": MIN_POLL_INTERVAL_SECONDS,
                            "default_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
                            "max_order_total_inr": MAX_ORDER_TOTAL_INR,
                        },
                    },
                )
            elif path == "/api/health":
                self._send_json(200, {"ok": True})
            else:
                self._send_json(404, {"error": "Not found."})

        def do_POST(self) -> None:
            if not self._authorized():
                self._send_json(403, {"error": "Dashboard token or local origin invalid."})
                return
            path = urlsplit(self.path).path
            if path == "/api/products":
                try:
                    data = self._read_json()
                    url = data.get("url")
                    existing_urls = {product["url"] for product in store.list_products()}
                    if (
                        len(existing_urls) >= MAX_TRACKED_PRODUCTS
                        and isinstance(url, str)
                        and url.strip() not in existing_urls
                    ):
                        self._send_json(
                            409,
                            {"error": f"Limit is {MAX_TRACKED_PRODUCTS} tracked products."},
                        )
                        return
                    product = store.add_product(data)
                except ProductInputError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                except (ValueError, json.JSONDecodeError) as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                monitor.wake()
                self._send_json(201, {"product": product})
                return

            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0:2] == ["api", "products"] and parts[3] == "check":
                product_id = self._parse_product_id(parts[2])
                if product_id is None:
                    return
                if not store.request_check(product_id):
                    self._send_json(404, {"error": "Product not found."})
                    return
                monitor.wake()
                self._send_json(202, {"ok": True})
                return
            self._send_json(404, {"error": "Not found."})

        def do_DELETE(self) -> None:
            if not self._authorized():
                self._send_json(403, {"error": "Dashboard token or local origin invalid."})
                return
            parts = urlsplit(self.path).path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["api", "products"]:
                self._send_json(404, {"error": "Not found."})
                return
            product_id = self._parse_product_id(parts[2])
            if product_id is None:
                return
            if not store.remove_product(product_id):
                self._send_json(404, {"error": "Product not found."})
                return
            self._send_json(200, {"ok": True})

        def _parse_product_id(self, value: str) -> int | None:
            try:
                product_id = int(value)
                if product_id < 1:
                    raise ValueError
                return product_id
            except ValueError:
                self._send_json(400, {"error": "Invalid product ID."})
                return None

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if content_type != "application/json":
                raise ValueError("Expected application/json.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid request size.") from exc
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise ValueError("Request is empty or too large.")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("Request body must be a JSON object.")
            return value

    return RequestHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "data" / "products.sqlite3",
        help="SQLite database path.",
    )
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    store = ProductStore(args.database)
    profile_dir = ROOT / "session_data"
    monitor = MonitorService(store, profile_dir)
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        create_handler(store, monitor, token),
    )
    server.daemon_threads = True
    monitor.start()
    print(f"Product tracker is running at http://127.0.0.1:{args.port}", flush=True)
    print("Keep this terminal open while you want products to be monitored.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping product tracker.", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        monitor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
