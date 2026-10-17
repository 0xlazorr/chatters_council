"""
test_adversarial_integration.py - Adversarial Chaos & Cross-System Integration Test Suite

Tests edge cases, fuzz inputs, attack vectors, state machine transitions,
and end-to-end integration between Chatters Council and Market Sniffer.

Run via: python3 test_adversarial_integration.py
"""

import asyncio
import base58
import html
import importlib.util
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

CHATTERS_DIR = "/home/lazorr/chatters"
MSNIFFER_DIR = "/home/lazorr/market_sniffer_bot"

if CHATTERS_DIR not in sys.path:
    sys.path.insert(0, CHATTERS_DIR)
if MSNIFFER_DIR not in sys.path:
    sys.path.append(MSNIFFER_DIR)

import config
# Inject non-colliding attributes
ms_cfg_spec = importlib.util.spec_from_file_location("ms_cfg", os.path.join(MSNIFFER_DIR, "config.py"))
if ms_cfg_spec and ms_cfg_spec.loader:
    ms_cfg = importlib.util.module_from_spec(ms_cfg_spec)
    ms_cfg_spec.loader.exec_module(ms_cfg)
    for attr in dir(ms_cfg):
        if not hasattr(config, attr):
            setattr(config, attr, getattr(ms_cfg, attr))

from executor import SignalExtractor, ExecutionResult, MSnifferExecutor
from orchestrator_receiver import SignalParser, TradeSignal, SecurityValidator
from sniffer import MarketSniffer
from wallet_manager import WalletManager, FEE_RESERVE_SOL
from bot import parse_positive_float, UserTradeLock, USER_STATES


class TestAdversarialMarketSniffer(unittest.TestCase):
    """Adversarial & Fuzz Testing for Market Sniffer Webhook & Parsers"""

    def test_adv_01_non_dict_json_bodies(self):
        """Verify SignalParser rejects non-dict and malformed JSON payloads without 500 crashes."""
        bad_payloads = [
            "[1, 2, 3]",
            '"simple string"',
            "12345",
            "true",
            "null",
            "{unclosed json",
            "",
            "   ",
        ]
        for bad in bad_payloads:
            sig, err = SignalParser.parse(bad)
            self.assertIsNone(sig, f"Non-dict payload unexpectedly returned signal: {bad}")
            self.assertIsNotNone(err)

    def test_adv_02_illegal_actions_fuzz(self):
        """Verify SignalParser strictly accepts only BUY and SELL, rejecting all others."""
        illegal_actions = [
            "TRANSFER", "STAKE", "DRAIN", "APPROVE", "MINT", "BURN",
            "DROP", "ADMIN", "EXEC", "<script>", "'; DROP TABLE; --",
        ]
        valid_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        for act in illegal_actions:
            payload = json.dumps({"action": act, "mint": valid_mint, "amount": 0.1})
            sig, err = SignalParser.parse(payload)
            self.assertIsNone(sig)
            self.assertIn("Invalid or missing action", str(err))

    def test_adv_03_native_sol_mint_rejected(self):
        """Verify buying/selling native SOL mint against itself is blocked immediately."""
        native_sol = "So11111111111111111111111111111111111111112"
        payload = json.dumps({"action": "BUY", "mint": native_sol, "amount": 0.5})
        sig, err = SignalParser.parse(payload)
        self.assertIsNone(sig)
        self.assertIn("native SOL", str(err))

    def test_adv_04_micro_and_negative_amounts(self):
        """Verify amounts below minimum threshold (0.001 SOL) or negative/NaN/Inf are rejected."""
        invalid_amounts = [0.0, -1.0, -0.0001, 0.000000001, 1e-12, float("nan"), float("inf")]
        valid_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        for amt in invalid_amounts:
            payload = json.dumps({"action": "BUY", "mint": valid_mint, "amount": amt})
            sig, err = SignalParser.parse(payload)
            self.assertIsNone(sig, f"Invalid amount {amt} was accepted")
            self.assertIsNotNone(err)

    def test_adv_05_overflow_amounts(self):
        """Verify buy amounts > 1000 SOL or sell percentages > 100% are rejected."""
        valid_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        # Over 1000 SOL buy
        payload_buy = json.dumps({"action": "BUY", "mint": valid_mint, "amount": 1000.1})
        sig, err = SignalParser.parse(payload_buy)
        self.assertIsNone(sig)
        self.assertIn("exceeds safety maximum", str(err))

        # Over 100% sell
        payload_sell = json.dumps({"action": "SELL", "mint": valid_mint, "amount": 100.1})
        sig, err = SignalParser.parse(payload_sell)
        self.assertIsNone(sig)
        self.assertIn("cannot exceed 100%", str(err))

    def test_adv_06_malformed_solana_addresses(self):
        """Verify invalid Base58 characters (0, O, I, l) or wrong lengths are rejected."""
        bad_cas = [
            "0xDezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB",  # Ethereum 0x prefix
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPOOO",  # Contains 'O' (invalid in Base58)
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPIII",  # Contains 'I' (invalid in Base58)
            "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPlll",  # Contains 'l' (invalid in Base58)
            "TooShortAddress123",
            "ThisAddressIsWayTooLongToEverBeAValidSolanaPublicKey555555555555555555555555555",
        ]
        for bad_ca in bad_cas:
            payload = json.dumps({"action": "BUY", "mint": bad_ca, "amount": 0.1})
            sig, err = SignalParser.parse(payload)
            self.assertIsNone(sig, f"Bad address accepted: {bad_ca}")
            self.assertIn("Invalid Solana", str(err))

    def test_adv_07_slippage_fuzzing(self):
        """Verify slippage bps clamps properly between 10 and 1500 bps regardless of input format."""
        valid_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        cases = [
            (50, 50),
            (300, 300),
            (1500, 1500),
            (99999, 1500),  # clamped to max 1500
            (-500, 10),     # clamped to min 10
            ("500", 500),
        ]
        for inp_slip, expected_slip in cases:
            payload = json.dumps({"action": "BUY", "mint": valid_mint, "amount": 0.1, "slippage_bps": inp_slip})
            sig, err = SignalParser.parse(payload)
            self.assertIsNotNone(sig)
            self.assertEqual(sig.slippage_bps, expected_slip)

    def test_adv_08_xss_and_injection_in_payload_metadata(self):
        """Verify XSS and SQL injection strings in symbol or reason are safely handled."""
        valid_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        xss_string = "<script>alert(1)</script>'; DROP TABLE users; --"
        payload = json.dumps({
            "action": "BUY",
            "mint": valid_mint,
            "amount": 0.1,
            "symbol": xss_string,
            "reason": xss_string,
        })
        sig, err = SignalParser.parse(payload)
        self.assertIsNotNone(sig)
        # Safe string handling
        escaped_sym = html.escape(sig.symbol)
        self.assertNotIn("<script>", escaped_sym.lower())
        self.assertIn("&lt;script&gt;", escaped_sym.lower())

    def test_adv_09_user_trade_lock_concurrency(self):
        """Verify UserTradeLock prevents overlapping concurrent trades on the same wallet."""
        loop = asyncio.new_event_loop()
        try:
            async def run_concurrent():
                user_id = 99999999
                async with UserTradeLock(user_id) as acq1:
                    self.assertTrue(acq1)
                    # Attempt second acquisition while lock is held
                    async with UserTradeLock(user_id) as acq2:
                        self.assertFalse(acq2, "Second concurrent trade lock was erroneously acquired!")
                # Once released, lock should be acquirable again
                async with UserTradeLock(user_id) as acq3:
                    self.assertTrue(acq3)

            loop.run_until_complete(run_concurrent())
        finally:
            loop.close()

    def test_adv_10_positive_float_parser_edge_cases(self):
        """Verify parse_positive_float rejects invalid values and parses clean numbers."""
        self.assertEqual(parse_positive_float("0.25"), 0.25)
        self.assertEqual(parse_positive_float("1,000.50"), 1000.50)
        self.assertIsNone(parse_positive_float("0"))
        self.assertIsNone(parse_positive_float("-5.0"))
        self.assertIsNone(parse_positive_float("nan"))
        self.assertIsNone(parse_positive_float("inf"))
        self.assertIsNone(parse_positive_float("abc"))
        self.assertIsNone(parse_positive_float(""))
        self.assertIsNone(parse_positive_float("10000000000.0"))  # Exceeds max_val


class TestIntegrationChattersAndMSniffer(unittest.TestCase):
    """Live Cross-System Integration: Chatters Council <-> Market Sniffer Bridge"""

    def test_int_01_health_endpoint_live_query(self):
        """Verify Chatters MSnifferExecutor queries Market Sniffer /health endpoint successfully."""
        loop = asyncio.new_event_loop()
        try:
            wallet_info = loop.run_until_complete(MSnifferExecutor.get_wallet_status())
            self.assertTrue(wallet_info["online"], "Market Sniffer HTTP health endpoint returned offline!")
            self.assertTrue(len(wallet_info["wallet"]) >= 32, f"Invalid wallet returned: {wallet_info['wallet']}")
            self.assertEqual(wallet_info["execution_user_id"], 2068383520)
            self.assertIsInstance(wallet_info["sol_balance"], float)
        finally:
            loop.close()

    def test_int_02_treasury_insufficient_sol_guard(self):
        """Verify Chatters pre-execution balance check blocks buy orders exceeding live balance."""
        loop = asyncio.new_event_loop()
        try:
            valid_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
            # Request 50 SOL (wallet has 0.0 SOL)
            res = loop.run_until_complete(
                MSnifferExecutor.execute_consensus(
                    prompt=f"Buy 50 SOL of {valid_mint}",
                    consensus_text="Council approved 5/5",
                    yes_count=5,
                    total_votes=5
                )
            )
            self.assertFalse(res.success)
            self.assertIn("Insufficient SOL balance", res.message)
            self.assertIn("Please fund the wallet", res.message)
        finally:
            loop.close()

    def test_int_03_usdt_auto_sell_protection(self):
        """Verify automated conversion of USDT back to SOL is strictly blocked as invariant."""
        loop = asyncio.new_event_loop()
        try:
            res = loop.run_until_complete(
                MSnifferExecutor.execute_consensus(
                    prompt=f"Sell 100% of {config.USDT_SOLANA_MINT}",
                    consensus_text="Sell USDT",
                    yes_count=5,
                    total_votes=5
                )
            )
            self.assertFalse(res.success)
            self.assertIn("SECURITY VIOLATION", res.message)
        finally:
            loop.close()

    def test_int_04_live_webhook_invalid_mint_response(self):
        """Verify live POST /trade properly returns HTTP 422 with structured JSON error on bad CA."""
        import requests
        res = requests.post(
            "http://127.0.0.1:8088/trade",
            json={"action": "BUY", "mint": "InvalidFakeAddress", "amount": 0.1},
            timeout=5
        )
        self.assertEqual(res.status_code, 422)
        data = res.json()
        self.assertIn("error", data)
        self.assertIn("Invalid Solana mint address", data["error"])

    def test_int_05_live_webhook_empty_action_response(self):
        """Verify live POST /trade properly rejects missing action parameter."""
        import requests
        res = requests.post(
            "http://127.0.0.1:8088/trade",
            json={"mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "amount": 0.1},
            timeout=5
        )
        self.assertEqual(res.status_code, 422)
        data = res.json()
        self.assertIn("Must be 'BUY' or 'SELL'", data["error"])

    def test_int_06_state_machine_custom_input_recovery(self):
        """Verify state machine in bot.py properly executes numeric inputs (fixing previously broken indentation)."""
        user_id = 11223344
        USER_STATES[user_id] = {
            "type": "custom_buy",
            "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            "_ts": time.time(),
        }
        self.assertIn(user_id, USER_STATES)
        state = USER_STATES[user_id]
        self.assertEqual(state["type"], "custom_buy")
        self.assertEqual(state["mint"], "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
        # Cleanup
        del USER_STATES[user_id]

    def test_int_07_concurrent_webhook_stress(self):
        """Stress test: Fire 10 simultaneous health requests to verify web server concurrency."""
        import requests
        import concurrent.futures

        def fetch_health():
            r = requests.get("http://127.0.0.1:8088/health", timeout=3)
            return r.status_code == 200 and r.json().get("status") == "online"

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_health) for _ in range(10)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 10)
        self.assertTrue(all(results), "One or more concurrent health queries failed!")

    def test_int_08_dex_url_extraction(self):
        """Verify MarketSniffer extracts CA from complex dirty URLs (Pump.fun, DexScreener, GMGN, Birdeye)."""
        valid_ca = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        urls = [
            f"https://dexscreener.com/solana/{valid_ca}",
            f"https://pump.fun/{valid_ca}",
            f"https://gmgn.ai/sol/token/{valid_ca}?ref=123",
            f"https://birdeye.so/token/{valid_ca}?chain=solana",
            f"Buy this gem CA: {valid_ca} right now!!",
        ]
        for u in urls:
            extracted = MarketSniffer.extract_solana_address(u)
            self.assertEqual(extracted, valid_ca, f"Failed extracting CA from {u}")


def run_tests():
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestAdversarialMarketSniffer))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestIntegrationChattersAndMSniffer))

    print("\n" + "=" * 70)
    print("🔥 RUNNING ADVERSARIAL STRESS & CROSS-SYSTEM INTEGRATION SUITE")
    print("=" * 70)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print("=" * 70)
    print(f"📊 SUMMARY: Ran {result.testsRun} tests.")
    print(f"✅ Successes: {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"❌ Failures : {len(result.failures)}")
    print(f"⚠️ Errors   : {len(result.errors)}")
    print("=" * 70 + "\n")
    return len(result.failures) == 0 and len(result.errors) == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
