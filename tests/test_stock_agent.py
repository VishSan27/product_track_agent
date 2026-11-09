import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from stock_agent import (
    AgentConfig,
    ConfigurationError,
    is_within_order_limit,
    parse_inr_amount,
)


class ParseAmountTests(unittest.TestCase):
    def test_parses_common_rupee_formats(self) -> None:
        self.assertEqual(parse_inr_amount("₹4,799.00"), Decimal("4799.00"))
        self.assertEqual(parse_inr_amount("Rs. 4,800"), Decimal("4800"))
        self.assertEqual(parse_inr_amount("4800"), Decimal("4800"))

    def test_rejects_ambiguous_or_invalid_prices(self) -> None:
        for value in ("₹4,500 ₹4,000", "Price unavailable", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_inr_amount(value)

    def test_order_limit_includes_4800_but_rejects_any_higher_total(self) -> None:
        self.assertTrue(is_within_order_limit(Decimal("4800")))
        self.assertFalse(is_within_order_limit(Decimal("4800.01")))


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.raw = {
            "product_url": "https://shop.example/item",
            "product_name": "Test item",
            "in_stock_selector": ".buy",
            "poll_interval_seconds": 300,
        }

    def load(self) -> AgentConfig:
        self.config_path.write_text(json.dumps(self.raw), encoding="utf-8")
        return AgentConfig.from_file(self.config_path)

    def test_accepts_valid_monitor_configuration(self) -> None:
        config = self.load()
        self.assertEqual(config.product_name, "Test item")
        self.assertEqual(config.poll_interval_seconds, 300)

    def test_rejects_http_and_too_frequent_polling(self) -> None:
        self.raw["product_url"] = "http://shop.example/item"
        with self.assertRaises(ConfigurationError):
            self.load()
        self.raw["product_url"] = "https://shop.example/item"
        self.raw["poll_interval_seconds"] = 1
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_rejects_cross_origin_cart_url(self) -> None:
        self.raw["cart_url"] = "https://other.example/cart"
        with self.assertRaises(ConfigurationError):
            self.load()

    def test_cart_staging_requires_all_safety_selectors(self) -> None:
        config = self.load()
        with self.assertRaises(ConfigurationError):
            config.validate_staging()


if __name__ == "__main__":
    unittest.main()
