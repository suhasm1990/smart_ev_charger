import asyncio
import unittest

import services.chargepoint as cp
from core import config


class TestChargePointHelpers(unittest.TestCase):
    def test_cloudflare_html_errors_are_summarised(self):
        self.assertEqual(
            cp._clean_error(Exception("<html><div>502 Bad Gateway</div></html>")),
            "ChargePoint API Bad Gateway (Cloudflare 502)",
        )
        self.assertEqual(
            cp._clean_error(Exception("<!DOCTYPE html><div>503 unavailable</div>")),
            "ChargePoint API Service Unavailable (Cloudflare 503)",
        )

    def test_plain_errors_are_passed_through_truncated(self):
        self.assertEqual(cp._clean_error(Exception("Failed to start charging: 422")),
                         "Failed to start charging: 422")
        self.assertLessEqual(len(cp._clean_error(Exception("x" * 500))), 150)

    def test_missing_credentials_raise_a_clear_error(self):
        original = config.CHARGEPOINT_USERNAME, config.CHARGEPOINT_COULOMB_TOKEN
        config.CHARGEPOINT_USERNAME = config.CHARGEPOINT_COULOMB_TOKEN = ""
        try:
            cp._client = None
            with self.assertRaises(RuntimeError):
                asyncio.run(cp._get_client())
        finally:
            config.CHARGEPOINT_USERNAME, config.CHARGEPOINT_COULOMB_TOKEN = original

    def test_start_charger_defaults_to_configured_amperage(self):
        """Auto mode must charge at DEFAULT_CHARGER_AMPERAGE, not a hardcoded 20A."""
        sent = []
        original = cp._run
        cp._run = lambda coro, timeout=60.0: (coro.close(), sent.append("called"))
        try:
            import inspect
            self.assertIsNone(inspect.signature(cp.start_charger).parameters["amperage_limit"].default)
            cp.start_charger()
            self.assertEqual(sent, ["called"])
        finally:
            cp._run = original


class TestChargePointLive(unittest.TestCase):
    """Opt-in live check; skipped unless real credentials are configured."""

    @unittest.skipUnless(config.CHARGEPOINT_USERNAME and config.CHARGEPOINT_COULOMB_TOKEN,
                         "ChargePoint credentials not configured")
    def test_live_status_fetch(self):
        status = cp.get_charger_status()
        self.assertIn("charging_status", status)
        self.assertIn("is_plugged_in", status)


if __name__ == "__main__":
    unittest.main()
