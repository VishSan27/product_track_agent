"""Local SQLite-backed product monitor with best-effort page detection."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route, sync_playwright

from stock_agent import Notifier, parse_inr_amount

LOGGER = logging.getLogger("product_monitor")
MIN_POLL_INTERVAL_SECONDS = 60
DEFAULT_POLL_INTERVAL_SECONDS = 300
MAX_ORDER_TOTAL_INR = 4800
PURCHASE_PATH_PATTERN = re.compile(
    r"(?:^|/)(?:cart|checkout|payment|place-order|order-confirm|pay-now)(?:/|$)",
    re.IGNORECASE,
)

PAGE_INSPECTION_SCRIPT = r"""
(options) => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const textFor = (selector) => {
    if (!selector) return null;
    try {
      const el = document.querySelector(selector);
      if (!visible(el)) return null;
      return (el.innerText || el.getAttribute("content") || el.textContent || "").trim();
    } catch (_) {
      return null;
    }
  };
  const readJsonLd = (value, products) => {
    if (!value || typeof value !== "object") return;
    if (Array.isArray(value)) {
      value.forEach((entry) => readJsonLd(entry, products));
      return;
    }
    const types = value["@type"];
    const isProduct = types === "Product" ||
      (Array.isArray(types) && types.includes("Product"));
    if (isProduct) products.push(value);
    Object.values(value).forEach((entry) => readJsonLd(entry, products));
  };
  const products = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach((script) => {
    try { readJsonLd(JSON.parse(script.textContent), products); } catch (_) {}
  });
  const selectedIn = textFor(options.inStockSelector);
  const selectedOut = textFor(options.outOfStockSelector);
  let stock = selectedOut ? "out_of_stock" : null;
  let source = selectedOut ? "your out-of-stock selector" : null;
  if (!stock && selectedIn) {
    const el = document.querySelector(options.inStockSelector);
    if (el && !el.disabled && el.getAttribute("aria-disabled") !== "true") {
      stock = "in_stock";
      source = "your in-stock selector";
    } else {
      stock = "unknown";
      source = "in-stock selector is disabled";
    }
  }
  const pageTitle = document.querySelector("h1")?.innerText?.trim() || document.title || "";
  const normalize = (value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;
  const normalizeUrl = (value) => {
    try {
      const url = new URL(value, location.href);
      url.search = "";
      url.hash = "";
      return url.href.replace(/\/+$/, "").toLowerCase();
    } catch (_) {
      return "";
    }
  };
  const canonicalProduct = products.find((product) => {
    const urls = Array.isArray(product.url) ? product.url : [product.url];
    return urls.some((url) => url && (
      normalizeUrl(url) === normalizeUrl(location.href) ||
      normalizeUrl(url) === normalizeUrl(canonical)
    ));
  });
  const namedProduct = products.find((product) => {
    const productName = normalize(product.name);
    const visibleName = normalize(pageTitle);
    return productName && visibleName &&
      (productName === visibleName || productName.includes(visibleName) || visibleName.includes(productName));
  });
  const mainProduct = canonicalProduct || namedProduct || (products.length === 1 ? products[0] : null);
  let title = mainProduct?.name || pageTitle;
  let priceText = textFor(options.priceSelector);
  if (mainProduct) {
    const offers = Array.isArray(mainProduct.offers) ? mainProduct.offers : [mainProduct.offers];
    for (const offer of offers) {
      if (!offer || typeof offer !== "object") continue;
      if (!priceText && offer.price !== undefined) {
        priceText = String(offer.priceCurrency || "") + " " + String(offer.price);
      }
      if (!stock && offer.availability) {
        const availability = String(offer.availability).toLowerCase();
        if (availability.includes("outofstock") || availability.includes("soldout")) {
          stock = "out_of_stock";
          source = "structured product data";
        } else if (availability.includes("instock") || availability.includes("limitedavailability")) {
          stock = "in_stock";
          source = "structured product data";
        }
      }
    }
  }
  if (!priceText) {
    const metaPrice = document.querySelector(
      'meta[property="product:price:amount"], meta[itemprop="price"]'
    );
    if (metaPrice) priceText = metaPrice.getAttribute("content") || metaPrice.innerText || "";
  }
  if (!stock) {
    const buttons = Array.from(document.querySelectorAll("button, [role=button], input[type=submit]"))
      .filter(visible)
      .map((el) => ({
        text: (el.innerText || el.value || el.getAttribute("aria-label") || "").toLowerCase(),
        disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true"
      }));
    const stockMessages = Array.from(
      document.querySelectorAll(
        '[role="alert"], [role="status"], [class*="stock"], [class*="availability"], [id*="stock"], [id*="availability"]'
      )
    ).filter(visible).map((el) => (el.innerText || el.textContent || "").toLowerCase());
    if (stockMessages.some((text) => /\b(sold out|out of stock|currently unavailable|not available)\b/.test(text))) {
      stock = "out_of_stock";
      source = "stock message";
    } else {
      const buyButton = buttons.find((button) => /\b(add to cart|add to bag|buy now)\b/.test(button.text));
      if (buyButton && !buyButton.disabled) {
        stock = "in_stock";
        source = "purchase button";
      }
    }
  }
  return {title, priceText, stock, source};
}
"""


class ProductInputError(ValueError):
    """Raised when a product URL or tracking option is unsafe or invalid."""


@dataclass(frozen=True)
class CheckResult:
    status: str
    title: str
    price: str | None
    source: str | None
    diagnostic: str | None = None


def validate_product_url(value: str) -> str:
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProductInputError("Enter a full HTTPS product link.")
    if parsed.username or parsed.password:
        raise ProductInputError("Product links must not contain embedded credentials.")
    if parsed.port not in (None, 443):
        raise ProductInputError("Product links must use the standard HTTPS port.")
    host = parsed.hostname.lower().rstrip(".")
    if (
        host in {"localhost", "localhost.localdomain"}
        or host.endswith(".localhost")
        or host.endswith(".local")
    ):
        raise ProductInputError("Local network addresses cannot be tracked.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (not address.is_global):
        raise ProductInputError("Private and reserved network addresses cannot be tracked.")
    if PURCHASE_PATH_PATTERN.search(parsed.path):
        raise ProductInputError("Use a product page link, not a checkout or payment link.")
    return url


def inspect_snapshot(
    snapshot: dict[str, Any], diagnostic: str | None = None
) -> CheckResult:
    raw_status = snapshot.get("stock")
    status = raw_status if raw_status in {"in_stock", "out_of_stock"} else "unknown"
    title = str(snapshot.get("title") or "").strip()[:300] or "Product"
    price_text = str(snapshot.get("priceText") or "").strip()
    price: str | None = None
    if price_text:
        try:
            price = str(parse_inr_amount(price_text))
        except ValueError:
            diagnostic = diagnostic or "Detected price text was ambiguous; verify the price selector."
            LOGGER.info(
                "Could not unambiguously parse detected price text: %r",
                price_text[:160],
            )
    return CheckResult(
        status=status,
        title=title,
        price=price,
        source=str(snapshot.get("source") or "") or None,
        diagnostic=diagnostic,
    )


class ProductStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    price TEXT,
                    source TEXT,
                    in_stock_selector TEXT,
                    out_of_stock_selector TEXT,
                    price_selector TEXT,
                    interval_seconds INTEGER NOT NULL DEFAULT 300,
                    last_checked REAL,
                    next_check REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _as_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["max_order_total_inr"] = MAX_ORDER_TOTAL_INR
        return value

    def list_products(self) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM products ORDER BY created_at DESC"
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def add_product(self, data: dict[str, Any]) -> dict[str, Any]:
        url_value = data.get("url")
        if not isinstance(url_value, str):
            raise ProductInputError("Product URL is required.")
        url = validate_product_url(url_value)
        interval = data.get("interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise ProductInputError("Check interval must be a whole number of seconds.")
        if interval < MIN_POLL_INTERVAL_SECONDS or interval > 86_400:
            raise ProductInputError("Check interval must be between 60 seconds and 24 hours.")

        selectors: dict[str, str | None] = {}
        for field in ("in_stock_selector", "out_of_stock_selector", "price_selector"):
            raw = data.get(field)
            if raw is not None and not isinstance(raw, str):
                raise ProductInputError(f"{field} must be a CSS selector.")
            value = raw.strip() if raw else None
            if value and len(value) > 500:
                raise ProductInputError(f"{field} is too long.")
            selectors[field] = value or None

        now = time.time()
        title = urlsplit(url).hostname or "Product"
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO products (
                    url, title, in_stock_selector, out_of_stock_selector, price_selector,
                    interval_seconds, next_check, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    in_stock_selector = excluded.in_stock_selector,
                    out_of_stock_selector = excluded.out_of_stock_selector,
                    price_selector = excluded.price_selector,
                    interval_seconds = excluded.interval_seconds,
                    next_check = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    url,
                    title,
                    selectors["in_stock_selector"],
                    selectors["out_of_stock_selector"],
                    selectors["price_selector"],
                    interval,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM products WHERE url = ?", (url,)).fetchone()
        if row is None:
            raise RuntimeError("Product could not be saved.")
        return self._as_dict(row)

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM products WHERE id = ?", (product_id,)
            ).fetchone()
        return self._as_dict(row) if row else None

    def due_products(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM products WHERE next_check <= ? ORDER BY next_check, id",
                (current,),
            ).fetchall()
        return [self._as_dict(row) for row in rows]

    def request_check(self, product_id: int) -> bool:
        with self._session() as connection:
            result = connection.execute(
                "UPDATE products SET next_check = 0, updated_at = ? WHERE id = ?",
                (time.time(), product_id),
            )
        return result.rowcount > 0

    def save_result(self, product_id: int, result: CheckResult) -> bool:
        now = time.time()
        with self._session() as connection:
            row = connection.execute(
                "SELECT status, interval_seconds FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            if row is None:
                return False
            if result.status == "unknown":
                connection.execute(
                    """
                    UPDATE products SET title = ?, last_checked = ?, next_check = ?,
                        price = COALESCE(?, price), source = COALESCE(?, source),
                        last_error = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        result.title,
                        now,
                        now + row["interval_seconds"],
                        result.price,
                        result.source,
                        "Could not determine stock; verify this product page or add selectors.",
                        now,
                        product_id,
                    ),
                )
                return False
            changed = result.status != row["status"]
            connection.execute(
                """
                UPDATE products SET
                    title = ?, status = ?, price = ?, source = ?, last_checked = ?,
                    next_check = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    result.title,
                    result.status,
                    result.price,
                    result.source,
                    now,
                    now + row["interval_seconds"],
                    result.diagnostic,
                    now,
                    product_id,
                ),
            )
        return changed

    def save_error(self, product_id: int, error: str) -> None:
        now = time.time()
        with self._session() as connection:
            row = connection.execute(
                "SELECT interval_seconds FROM products WHERE id = ?", (product_id,)
            ).fetchone()
            if row:
                connection.execute(
                    """
                    UPDATE products SET last_checked = ?, next_check = ?,
                        last_error = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        now,
                        now + row["interval_seconds"],
                        error[:1000],
                        now,
                        product_id,
                    ),
                )

    def save_notification_error(self, product_id: int, error: str) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE products SET last_error = ?, updated_at = ? WHERE id = ?",
                (f"Notification failed: {error}"[:1000], time.time(), product_id),
            )

    def remove_product(self, product_id: int) -> bool:
        with self._session() as connection:
            result = connection.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return result.rowcount > 0


def _guard_route(route: Route, page: Page, product_url: str) -> None:
    request = route.request
    if request.is_navigation_request() and request.frame == page.main_frame:
        target = urlsplit(request.url)
        if (
            _origin(request.url) != _origin(product_url)
            or PURCHASE_PATH_PATTERN.search(target.path)
        ):
            LOGGER.warning("Blocked unsafe navigation to %s", request.url)
            route.abort()
            return
    route.continue_()


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port


class MonitorService:
    def __init__(self, store: ProductStore, profile_dir: Path) -> None:
        self.store = store
        self.profile_dir = profile_dir
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="product-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=True,
                )
                try:
                    while not self._stop.is_set():
                        due = self.store.due_products()
                        for product in due:
                            if self._stop.is_set():
                                break
                            self._check_product(context, product)
                        self._wake.wait(timeout=15)
                        self._wake.clear()
                finally:
                    context.close()
        except Exception:
            LOGGER.exception("The product monitor stopped unexpectedly.")

    def _check_product(self, context: Any, product: dict[str, Any]) -> None:
        page: Page | None = None
        try:
            page = context.new_page()
            page.route("**/*", lambda route: _guard_route(route, page, product["url"]))
            page.goto(product["url"], wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1_500)
            snapshot = page.evaluate(
                PAGE_INSPECTION_SCRIPT,
                {
                    "inStockSelector": product["in_stock_selector"],
                    "outOfStockSelector": product["out_of_stock_selector"],
                    "priceSelector": product["price_selector"],
                },
            )
            diagnostics = []
            for field in ("in_stock_selector", "out_of_stock_selector", "price_selector"):
                selector = product[field]
                if selector:
                    count = page.locator(selector).count()
                    if count == 0:
                        diagnostics.append(f"Configured {field} matched no elements.")
                    elif field == "price_selector" and count > 1:
                        diagnostics.append("Price selector matched multiple elements.")
            result = inspect_snapshot(snapshot, " ".join(diagnostics) or None)
            changed = self.store.save_result(product["id"], result)
            if result.status == "unknown":
                LOGGER.warning("Could not determine stock for %s", product["url"])
            elif changed:
                status_text = "IN STOCK" if result.status == "in_stock" else "OUT OF STOCK"
                price_text = f"\nDetected price: ₹{result.price}" if result.price else ""
                message = (
                    f"{result.title}: {status_text}{price_text}\n"
                    f"Detection: {result.source or 'page inspection'}\n{product['url']}"
                )
                try:
                    Notifier().send(message)
                except Exception as exc:
                    LOGGER.exception("Notification failed for product %s", product["id"])
                    self.store.save_notification_error(product["id"], str(exc))
        except (PlaywrightError, ValueError, RuntimeError) as exc:
            LOGGER.warning("Product check failed for %s: %s", product["url"], exc)
            self.store.save_error(product["id"], str(exc))
        except Exception as exc:
            LOGGER.exception("Unexpected product check failure for %s", product["url"])
            self.store.save_error(product["id"], str(exc))
        finally:
            if page is not None:
                page.close()
