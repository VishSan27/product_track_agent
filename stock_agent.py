"""Stock monitoring with optional, approval-only cart preparation."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import BrowserContext, Page, Route, async_playwright

LOGGER = logging.getLogger("stock_agent")
MAX_ORDER_TOTAL_INR = Decimal("4800")
MIN_POLL_INTERVAL_SECONDS = 30


class ConfigurationError(ValueError):
    """Raised when the agent configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class AgentConfig:
    product_url: str
    product_name: str
    in_stock_selector: str
    price_selector: str | None
    out_of_stock_selector: str | None
    poll_interval_seconds: int
    profile_dir: Path
    cart_url: str | None
    add_to_cart_selector: str | None
    cart_empty_selector: str | None
    cart_item_selector: str | None
    cart_total_selector: str | None

    @classmethod
    def from_file(cls, path: Path) -> AgentConfig:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("Configuration must be a JSON object.")

        product_url = _required_string(raw, "product_url")
        _validate_web_url(product_url, "product_url")
        interval = raw.get("poll_interval_seconds", 300)
        if isinstance(interval, bool) or not isinstance(interval, int):
            raise ConfigurationError("poll_interval_seconds must be an integer.")
        if interval < MIN_POLL_INTERVAL_SECONDS:
            raise ConfigurationError(
                f"poll_interval_seconds must be at least {MIN_POLL_INTERVAL_SECONDS}."
            )

        profile_dir_value = raw.get("profile_dir", "session_data")
        if not isinstance(profile_dir_value, str) or not profile_dir_value.strip():
            raise ConfigurationError("profile_dir must be a non-empty path.")

        config = cls(
            product_url=product_url,
            product_name=_required_string(raw, "product_name"),
            in_stock_selector=_required_string(raw, "in_stock_selector"),
            price_selector=_optional_string(raw, "price_selector"),
            out_of_stock_selector=_optional_string(raw, "out_of_stock_selector"),
            poll_interval_seconds=interval,
            profile_dir=Path(profile_dir_value),
            cart_url=_optional_string(raw, "cart_url"),
            add_to_cart_selector=_optional_string(raw, "add_to_cart_selector"),
            cart_empty_selector=_optional_string(raw, "cart_empty_selector"),
            cart_item_selector=_optional_string(raw, "cart_item_selector"),
            cart_total_selector=_optional_string(raw, "cart_total_selector"),
        )
        if config.cart_url is not None:
            _validate_web_url(config.cart_url, "cart_url")
            if _origin(config.cart_url) != _origin(config.product_url):
                raise ConfigurationError("cart_url must have the same origin as product_url.")
        return config

    def validate_staging(self) -> None:
        required = {
            "price_selector": self.price_selector,
            "cart_url": self.cart_url,
            "add_to_cart_selector": self.add_to_cart_selector,
            "cart_empty_selector": self.cart_empty_selector,
            "cart_item_selector": self.cart_item_selector,
            "cart_total_selector": self.cart_total_selector,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ConfigurationError(
                "Cart preparation requires these config fields: " + ", ".join(missing)
            )


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string.")
    return value.strip()


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string when provided.")
    return value.strip()


def _validate_web_url(value: str, field: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError(f"{field} must be an HTTPS URL without embedded credentials.")


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port


def parse_inr_amount(text: str) -> Decimal:
    """Parse one rupee amount from text such as '₹4,799.00' or 'Rs. 4,799'."""
    if re.search(r"-\s*(?:(?:₹|Rs\.?)\s*)?\d|(?:₹|Rs\.?)\s*-\s*\d", text, re.I):
        raise ValueError(f"Negative rupee amounts are not valid: {text!r}")
    matches = re.findall(r"(?<!\d)(?:₹|Rs\.?\s*)?\s*(\d[\d,]*(?:\.\d{1,2})?)", text, re.I)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one price in text, found {len(matches)}.")
    try:
        amount = Decimal(matches[0].replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid rupee amount: {text!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Invalid rupee amount: {text!r}")
    return amount


def is_within_order_limit(amount: Decimal) -> bool:
    return amount <= MAX_ORDER_TOTAL_INR


class Notifier:
    def __init__(self) -> None:
        self._token = os.getenv("TELEGRAM_BOT_TOKEN")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if bool(self._token) != bool(self._chat_id):
            raise ConfigurationError(
                "Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither."
            )

    def send(self, message: str, link: str | None = None) -> None:
        text = f"{message}\n{link}" if link else message
        if not self._token or not self._chat_id:
            print(text, flush=True)
            return

        endpoint = f"https://api.telegram.org/bot{self._token}/sendMessage"
        body = urlencode({"chat_id": self._chat_id, "text": text}).encode("utf-8")
        request = Request(endpoint, data=body, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telegram notification failed: {exc}") from exc
        if not payload.get("ok"):
            raise RuntimeError("Telegram rejected the notification.")


async def _is_visible(page: Page, selector: str) -> bool:
    locator = page.locator(selector).first
    try:
        return await locator.is_visible()
    except PlaywrightError as exc:
        raise RuntimeError(f"Could not inspect selector {selector!r}: {exc}") from exc


async def _text_for(page: Page, selector: str) -> str:
    locator = page.locator(selector).first
    try:
        await locator.wait_for(state="visible", timeout=15_000)
        text = (await locator.inner_text()).strip()
    except PlaywrightError as exc:
        raise RuntimeError(f"Could not read selector {selector!r}: {exc}") from exc
    if not text:
        raise RuntimeError(f"Selector {selector!r} contained no text.")
    return text


async def check_stock(page: Page, config: AgentConfig) -> bool | None:
    await page.goto(config.product_url, wait_until="domcontentloaded", timeout=45_000)
    if config.out_of_stock_selector and await _is_visible(page, config.out_of_stock_selector):
        return False
    if await _is_visible(page, config.in_stock_selector):
        locator = page.locator(config.in_stock_selector).first
        if await locator.is_enabled():
            return True
        return False
    return None


async def _guard_navigation(route: Route, page: Page, product_url: str) -> None:
    request = route.request
    if request.is_navigation_request() and request.frame == page.main_frame:
        target = urlsplit(request.url)
        path = target.path.lower()
        blocked_path_terms = ("checkout", "payment", "place-order", "order-confirm", "pay-now")
        if _origin(request.url) != _origin(product_url) or any(
            term in path for term in blocked_path_terms
        ):
            LOGGER.error("Blocked unsafe or cross-origin navigation: %s", request.url)
            await route.abort()
            return
    await route.continue_()


async def _prepare_cart(page: Page, config: AgentConfig) -> tuple[Decimal, str] | None:
    config.validate_staging()
    assert config.price_selector
    assert config.cart_url
    assert config.add_to_cart_selector
    assert config.cart_empty_selector
    assert config.cart_item_selector
    assert config.cart_total_selector

    displayed_price = parse_inr_amount(await _text_for(page, config.price_selector))
    if not is_within_order_limit(displayed_price):
        return None

    await page.goto(config.cart_url, wait_until="domcontentloaded", timeout=45_000)
    if not await _is_visible(page, config.cart_empty_selector):
        raise RuntimeError("Cart is not confirmed empty; refusing to modify it.")

    await page.goto(config.product_url, wait_until="domcontentloaded", timeout=45_000)
    add_button = page.locator(config.add_to_cart_selector).first
    try:
        await add_button.wait_for(state="visible", timeout=15_000)
        if not await add_button.is_enabled():
            raise RuntimeError("Add-to-cart control is disabled.")
        await add_button.click()
    except PlaywrightError as exc:
        raise RuntimeError(f"Could not add the configured item to the cart: {exc}") from exc

    await page.goto(config.cart_url, wait_until="domcontentloaded", timeout=45_000)
    item_count = await page.locator(config.cart_item_selector).count()
    if item_count != 1:
        raise RuntimeError(
            f"Expected exactly one cart item after staging; found {item_count}. "
            "No checkout link was sent."
        )
    cart_total = parse_inr_amount(await _text_for(page, config.cart_total_selector))
    if not is_within_order_limit(cart_total):
        return None
    if not await _is_visible(page, config.cart_item_selector):
        raise RuntimeError("Staged cart item is no longer visible.")
    return cart_total, page.url


async def run_agent(config: AgentConfig, prepare_checkout: bool, once: bool) -> None:
    notifier = Notifier()
    if prepare_checkout:
        config.validate_staging()

    config.profile_dir.mkdir(parents=True, exist_ok=True)
    last_stock: bool | None = None
    staged = False
    async with async_playwright() as playwright:
        context: BrowserContext = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.profile_dir),
            headless=False,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.route(
                "**/*",
                lambda route: _guard_navigation(route, page, config.product_url),
            )
            while True:
                try:
                    in_stock = await check_stock(page, config)
                    if in_stock is None:
                        LOGGER.warning("Stock status is unknown; check the configured selectors.")
                    elif in_stock != last_stock:
                        status = "IN STOCK" if in_stock else "out of stock"
                        notifier.send(f"{config.product_name}: {status}\n{config.product_url}")
                        last_stock = in_stock

                    if in_stock and prepare_checkout and not staged:
                        result = await _prepare_cart(page, config)
                        if result is None:
                            notifier.send(
                                f"{config.product_name} is in stock, but its displayed or "
                                f"cart total exceeds ₹{MAX_ORDER_TOTAL_INR} (or cannot be "
                                "confirmed within the cap). No cart link was sent. If the "
                                "item was already added, remove it from the cart manually."
                            )
                        else:
                            total, cart_link = result
                            notifier.send(
                                f"{config.product_name} is staged in your cart. "
                                f"Verified cart total: ₹{total:.2f} "
                                f"(limit ₹{MAX_ORDER_TOTAL_INR:.2f}). "
                                "Review the item, delivery, taxes, and final total yourself. "
                                "No order or payment was submitted.",
                                cart_link,
                            )
                        staged = True
                except (RuntimeError, ValueError, PlaywrightError):
                    LOGGER.exception("Monitoring check failed; will retry next interval.")

                if once:
                    return
                await asyncio.sleep(config.poll_interval_seconds)
        finally:
            await context.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument(
        "--prepare-checkout",
        action="store_true",
        help="Opt in to safe cart staging; this never places an order or pays.",
    )
    parser.add_argument("--once", action="store_true", help="Run one check, then exit.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        config = AgentConfig.from_file(args.config)
        asyncio.run(run_agent(config, args.prepare_checkout, args.once))
    except (ConfigurationError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
