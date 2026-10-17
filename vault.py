"""
vault.py - Automated Profit Vault Sweeper (One-Way Ratchet into USDT).

Implements the user's core profit security rule:
1. Tracks realized profits and SOL balance growth against an established baseline.
2. Every time the bot generates $25 or more in profit above the baseline:
   - Automatically swaps $25 worth of SOL into USDT on Solana.
   - Preserves a dedicated SOL trading reserve so upcoming trades are never starved.
3. CRITICAL ONE-WAY SAFETY INVARIANT:
   - The bot ONLY swaps SOL -> USDT to lock in profits.
   - The bot has ZERO capability or permission to convert USDT -> SOL automatically.
   - Only the Admin can manually access or convert the accumulated USDT.
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

import config
from executor import MSnifferExecutor, format_execution_announcement

logger = logging.getLogger("multi_agent_council.vault")

VAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vault_state.json")


class ProfitVaultManager:
    """Manages automatic $25 profit harvesting into USDT on Solana."""

    def __init__(self, filepath: str = VAULT_FILE):
        self.filepath = filepath
        self.threshold_usd: float = config.PROFIT_VAULT_THRESHOLD_USD
        self.baseline_sol_balance: float = 0.0
        self.total_usdt_harvested: float = 0.0
        self.total_sweeps_count: int = 0
        self.last_sweep_timestamp: float = 0.0
        self.last_sol_price_usd: float = 105.0
        self._load()

    def _load(self):
        """Loads state from local JSON storage."""
        if not os.path.exists(self.filepath):
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.threshold_usd = float(data.get("threshold_usd", config.PROFIT_VAULT_THRESHOLD_USD))
                self.baseline_sol_balance = float(data.get("baseline_sol_balance", 0.0))
                self.total_usdt_harvested = float(data.get("total_usdt_harvested", 0.0))
                self.total_sweeps_count = int(data.get("total_sweeps_count", 0))
                self.last_sweep_timestamp = float(data.get("last_sweep_timestamp", 0.0))
                self.last_sol_price_usd = float(data.get("last_sol_price_usd", 105.0))
            logger.info(
                f"🏦 Loaded Profit Vault: Threshold=${self.threshold_usd:.2f}, "
                f"Baseline={self.baseline_sol_balance:.4f} SOL, Total Swept=${self.total_usdt_harvested:.2f} USDT"
            )
        except Exception as e:
            logger.error(f"Error loading vault state from {self.filepath}: {e}")

    def _save(self):
        """Persists vault state to disk."""
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            data = {
                "threshold_usd": self.threshold_usd,
                "baseline_sol_balance": self.baseline_sol_balance,
                "total_usdt_harvested": self.total_usdt_harvested,
                "total_sweeps_count": self.total_sweeps_count,
                "last_sweep_timestamp": self.last_sweep_timestamp,
                "last_sol_price_usd": self.last_sol_price_usd,
            }
            tmp_path = self.filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.filepath)
        except Exception as e:
            logger.error(f"Error saving vault state to {self.filepath}: {e}")

    def set_threshold(self, new_amount: float) -> Tuple[bool, str]:
        """Customizes the dollar profit threshold that triggers an automated USDT sweep."""
        if new_amount < 5.0:
            return False, "Minimum profit vault threshold is $5.00 USD."
        self.threshold_usd = round(float(new_amount), 2)
        self._save()
        logger.info(f"Updated Profit Vault threshold to ${self.threshold_usd:.2f} USD")
        return True, f"✅ <b>Profit Vault Threshold Updated:</b> <code>${self.threshold_usd:.2f} USD</code>"

    def reset_baseline(self, new_balance: float):
        """Resets the baseline SOL balance (e.g. after manual deposits or withdrawals)."""
        self.baseline_sol_balance = round(float(new_balance), 4)
        self._save()
        logger.info(f"Reset Profit Vault baseline to {self.baseline_sol_balance:.4f} SOL")

    async def get_sol_price_usd(self) -> float:
        """Fetches live SOL/USD price from DexScreener's primary liquid pool."""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{config.SOL_MINT}"
        loop = asyncio.get_running_loop()

        def _fetch():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ChattersCouncil/2.0"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read())
                    pairs = data.get("pairs") or []
                    sol_pairs = [
                        p for p in pairs if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")
                    ]
                    sol_pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0), reverse=True)
                    if sol_pairs and sol_pairs[0].get("priceUsd"):
                        return float(sol_pairs[0]["priceUsd"])
            except Exception as e:
                logger.debug(f"Could not fetch SOL price from DexScreener: {e}")
            return None

        price = await loop.run_in_executor(None, _fetch)
        if price and price > 0:
            self.last_sol_price_usd = price
            return price
        return self.last_sol_price_usd

    async def check_and_sweep(
        self,
        current_sol_balance: float,
        broadcaster_bot,
        reply_to_chat_id: int,
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluates whether accumulated profits have reached threshold_usd above baseline.
        If yes:
        1. Calculates SOL required for threshold_usd.
        2. Verifies remaining balance preserves the trading reserve.
        3. Swaps SOL -> USDT via Market Sniffer.
        4. Adjusts baseline and posts confirmation to Telegram.
        """
        if not config.PROFIT_VAULT_ENABLED:
            return False, None

        if current_sol_balance <= 0:
            return False, None

        # Initialize baseline on first observation
        if self.baseline_sol_balance <= 0:
            self.baseline_sol_balance = current_sol_balance
            self._save()
            logger.info(f"Initialized Profit Vault baseline at {self.baseline_sol_balance:.4f} SOL")
            return False, None

        sol_gain = current_sol_balance - self.baseline_sol_balance
        if sol_gain <= 0:
            return False, None

        sol_price = await self.get_sol_price_usd()
        profit_usd = sol_gain * sol_price

        # Check if customizable profit threshold has been achieved
        if profit_usd < self.threshold_usd:
            return False, None

        logger.info(
            f"🎉 Profit threshold reached! Gain: +{sol_gain:.4f} SOL (~${profit_usd:.2f} USD). "
            f"Preparing ${self.threshold_usd:.2f} USDT sweep..."
        )

        sol_to_sweep = round(self.threshold_usd / sol_price, 4) if sol_price > 0 else 0.0
        if sol_to_sweep <= 0:
            return False, None

        # Safety Reserve Check: Ensure trading wallet remains sufficiently funded
        required_reserve = config.DEFAULT_TRADE_AMOUNT_SOL + config.PROFIT_VAULT_RESERVE_SOL
        if current_sol_balance - sol_to_sweep < required_reserve:
            logger.info(
                f"Holding vault sweep: remaining balance ({current_sol_balance - sol_to_sweep:.4f} SOL) "
                f"would be below operational trading reserve ({required_reserve:.4f} SOL)."
            )
            return False, None

        # ---------------------------------------------------------------------
        # EXECUTE SWAP: SOL -> USDT (Action: BUY USDT)
        # CRITICAL SAFETY: Only BUY USDT is permitted. ZERO SELL USDT allowed!
        # ---------------------------------------------------------------------
        exec_res = await MSnifferExecutor.execute_consensus(
            prompt=f"BUY {sol_to_sweep:.4f} SOL of {config.USDT_SOLANA_MINT}",
            consensus_text=f"Profit Vault Sweep: ${self.threshold_usd:.2f} USD profit locked into safe USDT reserve.",
            yes_count=5,
            total_votes=5,
            broadcaster_bot=broadcaster_bot,
            reply_to_chat_id=reply_to_chat_id,
        )

        if not exec_res.success:
            logger.warning(f"Profit vault swap to USDT failed: {exec_res.message}")
            return False, None

        # Sweep succeeded
        self.total_usdt_harvested += self.threshold_usd
        self.total_sweeps_count += 1
        self.last_sweep_timestamp = time.time()
        # Move baseline up so next sweep triggers on the NEXT threshold_usd of profit
        self.baseline_sol_balance = round(current_sol_balance - sol_to_sweep, 4)
        self._save()

        announcement = (
            f"🏦 <b>PROFIT VAULT HARVEST SECURED (${self.threshold_usd:.2f} USD)</b>\n"
            "───────────────────────────────\n"
            f"💰 <b>Realized Gain:</b> <code>+${profit_usd:.2f} USD</code>\n"
            f"🔒 <b>Converted to Vault:</b> <code>{sol_to_sweep:.4f} SOL</code> ➔ <b>~${self.threshold_usd:.2f} USDT</b>\n"
            f"🪙 <b>Safe USDT Mint:</b> <code>{config.USDT_SOLANA_MINT}</code>\n"
            f"🛡️ <b>Trading Reserve Remaining:</b> <code>{self.baseline_sol_balance:.4f} SOL</code>\n"
            f"📊 <b>Total USDT Harvested:</b> <code>${self.total_usdt_harvested:.2f} USDT</code>\n"
            "───────────────────────────────\n"
            "🛡️ <b>One-Way Safety Guarantee:</b> <i>USDT is permanently locked. "
            "The AI bots have ZERO permission to convert USDT back to SOL. "
            "Only the Admin can manually transfer or convert your USDT.</i>"
        )

        try:
            await broadcaster_bot.send_message(
                chat_id=reply_to_chat_id,
                text=announcement,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Failed to post profit vault harvest message: {e}")

        return True, announcement

    def get_status_text(self, current_sol_balance: float = 0.0) -> str:
        """Formats Profit Vault statistics for Telegram display."""
        sol_price = self.last_sol_price_usd
        sol_gain = max(0.0, current_sol_balance - self.baseline_sol_balance) if self.baseline_sol_balance > 0 else 0.0
        profit_usd = sol_gain * sol_price
        progress_pct = min(100.0, (profit_usd / self.threshold_usd) * 100.0) if self.threshold_usd > 0 else 0.0

        lines = [
            "🏦 <b>AI COUNCIL PROFIT VAULT STATUS</b>",
            "───────────────────────────────",
            f"💵 <b>Total USDT Harvested:</b> <code>${self.total_usdt_harvested:.2f} USDT</code>",
            f"🎯 <b>Harvest Threshold:</b> <code>${self.threshold_usd:.2f} USD</code>",
            f"🔄 <b>Total Sweeps Completed:</b> <code>{self.total_sweeps_count}</code>",
            f"📈 <b>Current Unswept Profit:</b> <code>+${profit_usd:.2f} USD</code> ({progress_pct:.0f}% to next sweep)",
            f"💳 <b>Active Baseline:</b> <code>{self.baseline_sol_balance:.4f} SOL</code>",
            f"☀️ <b>SOL Price:</b> <code>${sol_price:.2f} USD</code>",
            "───────────────────────────────",
            "🛡️ <i>One-Way Ratchet Active: Profits are swept into USDT and permanently protected from bot trading.</i>\n"
            "<i>Commands: /vault &lt;amount&gt; to change threshold | /vault reset to sync baseline</i>",
        ]
        return "\n".join(lines)


# Global singleton instance
profit_vault = ProfitVaultManager()
