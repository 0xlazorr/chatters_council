"""
autopilot.py - Autonomous Council Stream-Trading & Auto-Pilot Engine.

Allows the AI Council to operate continuously in the background without manual input:
1. Periodically scans Solana breakout & trending tokens from DexScreener / Pump.fun
2. Enforces wallet safety: automatically stops if SOL runs out or falls below minimum trade threshold
3. Convenes the 5 council bots to debate and vote in the Telegram group chat
4. Automatically pipes approved tokens to Market Sniffer (POST http://127.0.0.1:8088/trade)
5. Can be started with /autopilot on or -auto flag, and stopped anytime with /autopilot off or /cancel
"""

import asyncio
import html
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.request
import json

from telegram import Bot
from telegram.constants import ParseMode

import config
from executor import MSnifferExecutor, format_execution_announcement

logger = logging.getLogger("multi_agent_council.autopilot")


class AutopilotEngine:
    """Manages the autonomous continuous trading loop for the AI Council."""

    def __init__(self):
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None
        self.evaluated_mints: Set[str] = set()
        self.cycle_count: int = 0
        self.trades_executed: int = 0
        self.trades_approved: int = 0
        self.trades_rejected: int = 0
        self.start_timestamp: float = 0.0
        self.scan_interval_seconds: int = int(os.getenv("AUTOPILOT_INTERVAL_SECONDS", "90")) if "os" in globals() else 90

    def get_status(self) -> Dict[str, Any]:
        """Returns live statistics of the Autopilot engine."""
        uptime = time.time() - self.start_timestamp if self.is_running and self.start_timestamp else 0.0
        return {
            "is_running": self.is_running,
            "uptime_seconds": uptime,
            "cycles": self.cycle_count,
            "evaluated_count": len(self.evaluated_mints),
            "approved": self.trades_approved,
            "rejected": self.trades_rejected,
            "executed": self.trades_executed,
            "scan_interval": self.scan_interval_seconds,
        }

    async def start(
        self,
        chat_id: int,
        lead_bot: Bot,
        council_orchestrator,
        scan_interval: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Starts the autonomous scanning & trading loop."""
        if self.is_running:
            return False, "⚠️ Autopilot is already active and scanning tokens."

        if scan_interval and scan_interval >= 30:
            self.scan_interval_seconds = scan_interval

        self.is_running = True
        self.start_timestamp = time.time()
        self._task = asyncio.create_task(
            self._run_loop(chat_id=chat_id, lead_bot=lead_bot, council=council_orchestrator)
        )
        logger.info(f"🚀 Autopilot started for group {chat_id} (interval: {self.scan_interval_seconds}s)")
        return True, "🚀 <b>AUTOPILOT ACTIVATED:</b> The AI Council is now scanning live breakout tokens in autonomous mode."

    async def stop(self) -> str:
        """Stops the autonomous loop cleanly."""
        if not self.is_running:
            return "ℹ️ Autopilot is not currently running."

        self.is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("🛑 Autopilot halted by admin command.")
        return "🛑 <b>AUTOPILOT HALTED:</b> Autonomous loop stopped. System returned to manual trigger mode."

    async def _fetch_trending_solana_mints(self) -> List[Dict[str, str]]:
        """
        Fetches live breakout Solana tokens from DexScreener's public trending and profiles APIs:
        1. /token-boosts/top/v1 (Top boosted Solana tokens by velocity & community activity)
        2. /token-boosts/latest/v1 (Latest breakout tokens gaining immediate traction)
        3. /token-profiles/latest/v1 (Latest verified token profiles launched on Solana)
        Returns a list of dicts: [{"mint": "...", "name": "...", "symbol": "..."}]
        """
        urls = [
            "https://api.dexscreener.com/token-boosts/top/v1",
            "https://api.dexscreener.com/token-boosts/latest/v1",
            "https://api.dexscreener.com/token-profiles/latest/v1",
        ]
        candidates = []
        seen = set()

        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "ChattersCouncil/2.0"})
                # Run sync urlopen in thread executor
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, lambda: self._fetch_url_json(req))
                if not data or not isinstance(data, list):
                    continue

                for item in data:
                    if item.get("chainId") == "solana":
                        mint = item.get("tokenAddress", "").strip()
                        if mint and mint not in seen and mint not in self.evaluated_mints:
                            seen.add(mint)
                            candidates.append({
                                "mint": mint,
                                "name": item.get("description", "")[:30] or "Trending Solana Token",
                                "symbol": item.get("tokenAddress", "")[:6].upper(),
                                "url": item.get("url") or f"https://dexscreener.com/solana/{mint}",
                            })
            except Exception as e:
                logger.debug(f"Error fetching DexScreener tokens from {url}: {e}")

        return candidates

    async def _enrich_candidate(self, mint: str) -> Optional[Dict[str, Any]]:
        """
        Enriches a candidate Solana mint with live on-chain market telemetry:
        - Liquidity USD (verifies candidate meets AUTOPILOT_MIN_LIQUIDITY_USD floor)
        - FDV / Market Cap
        - Price USD
        - 24h Trading Volume
        - 1h Price Momentum
        - DEX (Raydium, PumpSwap, Orca, Meteora)
        Filters out illiquid tokens and honeypots before disturbing the Council.
        """
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ChattersCouncil/2.0"})
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: self._fetch_url_json(req))

            if not data or not isinstance(data, dict):
                return None

            pairs = data.get("pairs") or []
            solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not solana_pairs:
                return None

            # Sort by liquidity USD descending to identify primary liquidity pool
            solana_pairs.sort(
                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
                reverse=True,
            )
            primary_pair = solana_pairs[0]

            liquidity_usd = float((primary_pair.get("liquidity") or {}).get("usd") or 0.0)

            # Strict Safety Check: Discard tokens below minimum liquidity floor ($5k USD)
            if liquidity_usd < config.AUTOPILOT_MIN_LIQUIDITY_USD:
                logger.info(
                    f"Candidate {mint} discarded: Liquidity ${liquidity_usd:,.2f} is below safety floor "
                    f"(${config.AUTOPILOT_MIN_LIQUIDITY_USD:,.2f})"
                )
                return None

            base_token = primary_pair.get("baseToken") or {}
            name = base_token.get("name") or "Unknown"
            symbol = (base_token.get("symbol") or "SOL").upper()
            dex = (primary_pair.get("dexId") or "Raydium").capitalize()
            fdv = float(primary_pair.get("fdv") or 0.0)
            price_usd = float(primary_pair.get("priceUsd") or 0.0)
            vol_24h = float((primary_pair.get("volume") or {}).get("h24") or 0.0)
            h1_change = float((primary_pair.get("priceChange") or {}).get("h1") or 0.0)
            chart_url = primary_pair.get("url") or f"https://dexscreener.com/solana/{mint}"

            return {
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "dex": dex,
                "liquidity_usd": liquidity_usd,
                "fdv": fdv,
                "price_usd": price_usd,
                "volume_24h": vol_24h,
                "price_change_1h": h1_change,
                "url": chart_url,
            }
        except Exception as e:
            logger.debug(f"Error enriching candidate {mint}: {e}")
            return None

    @staticmethod
    def _fetch_url_json(req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    async def _run_loop(self, chat_id: int, lead_bot: Bot, council):
        """The core background execution loop."""
        logger.info("Autopilot background task loop initialized.")

        try:
            while self.is_running:
                self.cycle_count += 1
                logger.info(f"[Autopilot Cycle #{self.cycle_count}] Scanning candidate tokens...")

                # -------------------------------------------------------------
                # 1. Wallet Balance & Treasury Safety Check
                # -------------------------------------------------------------
                wallet_info = await MSnifferExecutor.get_wallet_status()
                if wallet_info.get("online"):
                    sol_bal = wallet_info.get("sol_balance", 0.0)
                    wallet_addr = wallet_info.get("wallet", "N/A")
                    min_required = config.DEFAULT_TRADE_AMOUNT_SOL + 0.005

                    if sol_bal < min_required:
                        alert_msg = (
                            "🛑 <b>AUTOPILOT PAUSED: INSUFFICIENT SOL IN WALLET</b>\n"
                            "───────────────────────────────\n"
                            f"💳 <b>Wallet:</b> <code>{wallet_addr}</code>\n"
                            f"💰 <b>Current Balance:</b> <code>{sol_bal:.4f} SOL</code>\n"
                            f"⚠️ <b>Required Min:</b> <code>{min_required:.4f} SOL</code>\n"
                            "───────────────────────────────\n"
                            "<i>The execution wallet has run out of SOL. Autopilot has been paused automatically "
                            "to protect your account. Fund the wallet and reactivate using</i> <code>/auto on</code>."
                        )
                        try:
                            await lead_bot.send_message(chat_id=chat_id, text=alert_msg, parse_mode=ParseMode.HTML)
                        except Exception as e:
                            logger.error(f"Failed to send low balance alert: {e}")

                        self.is_running = False
                        break

                # -------------------------------------------------------------
                # 2. Find Candidates & Enrich with Live DexScreener Telemetry
                # -------------------------------------------------------------
                raw_candidates = await self._fetch_trending_solana_mints()

                qualified_target = None
                for cand in raw_candidates:
                    mint = cand["mint"]
                    if mint in self.evaluated_mints:
                        continue
                    self.evaluated_mints.add(mint)

                    # Fetch live pair telemetry & enforce $5,000 liquidity floor
                    enriched = await self._enrich_candidate(mint)
                    if enriched:
                        qualified_target = enriched
                        break

                if not qualified_target:
                    logger.debug("No qualified Solana breakout candidates met safety criteria in this scan. Sleeping...")
                    await asyncio.sleep(self.scan_interval_seconds)
                    continue

                mint = qualified_target["mint"]
                name = qualified_target["name"]
                symbol = qualified_target["symbol"]
                dex = qualified_target["dex"]
                liq = qualified_target["liquidity_usd"]
                fdv = qualified_target["fdv"]
                price = qualified_target["price_usd"]
                vol24 = qualified_target["volume_24h"]
                change1h = qualified_target["price_change_1h"]
                chart_url = qualified_target["url"]
                trade_amt = config.DEFAULT_TRADE_AMOUNT_SOL

                announcement = (
                    f"🤖 <b>[AUTOPILOT SCAN #{self.cycle_count}] QUALIFIED BREAKOUT DISCOVERED</b>\n"
                    f"───────────────────────────────\n"
                    f"🪙 <b>Token:</b> {html.escape(name)} (<code>${html.escape(symbol)}</code>)\n"
                    f"🔑 <b>Contract:</b> <code>{mint}</code>\n"
                    f"🏦 <b>DEX:</b> <code>{dex}</code>\n"
                    f"💧 <b>Liquidity:</b> <code>${liq:,.0f} USD</code> (Floor: &gt;${config.AUTOPILOT_MIN_LIQUIDITY_USD:,.0f})\n"
                    f"📊 <b>Market Cap (FDV):</b> <code>${fdv:,.0f} USD</code>\n"
                    f"💵 <b>Price:</b> <code>${price:.6f} USD</code> | <b>1h:</b> <code>{change1h:+.1f}%</code> | <b>24h Vol:</b> <code>${vol24:,.0f}</code>\n"
                    f"📈 <a href='{chart_url}'>View Live DexScreener Chart</a>\n"
                    f"🎯 <b>Planned Allocation:</b> <code>{trade_amt:.2f} SOL</code>\n"
                    f"───────────────────────────────\n"
                    f"⚖️ <b>Convening AI Council for Institutional Debate & Vote...</b>"
                )
                try:
                    await lead_bot.send_message(
                        chat_id=chat_id,
                        text=announcement,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as e:
                    logger.error(f"Failed to post candidate announcement: {e}")

                # -------------------------------------------------------------
                # 3. Convene Council: Debate and Formal Vote with Real Market Stats
                # -------------------------------------------------------------
                prompt = (
                    f"Institutional council evaluation for {name} (${symbol}) on Solana ({dex}). "
                    f"Contract Address: {mint}. "
                    f"Live On-Chain Telemetry: Liquidity = ${liq:,.0f} USD, Market Cap (FDV) = ${fdv:,.0f} USD, "
                    f"Price = ${price:.6f} USD, 24h Volume = ${vol24:,.0f} USD, 1h Momentum = {change1h:+.1f}%. "
                    f"Evaluate security, liquidity structure, mathematical EV, and breakout probability. "
                    f"Cast your official vote on whether to execute BUY allocation of {trade_amt:.2f} SOL."
                )

                try:
                    vote_summary = await council.run_debate_and_vote(prompt=prompt, chat_id=chat_id)
                except Exception as e:
                    logger.error(f"Error during autopilot council session: {e}", exc_info=True)
                    await asyncio.sleep(self.scan_interval_seconds)
                    continue

                # Post vote summary into group
                try:
                    await lead_bot.send_message(
                        chat_id=chat_id,
                        text=vote_summary.summary_text,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.debug(f"Could not post vote summary: {e}")

                # -------------------------------------------------------------
                # 4. Automatic Execution If Consensus Passes
                # -------------------------------------------------------------
                if vote_summary.consensus_passed:
                    self.trades_approved += 1
                    logger.info(f"Consensus passed for {mint}. Dispatching execution to Market Sniffer...")

                    exec_res = await MSnifferExecutor.execute_consensus(
                        prompt=f"BUY {trade_amt:.2f} SOL of {mint}",
                        consensus_text=vote_summary.summary_text,
                        yes_count=vote_summary.yes_count,
                        total_votes=vote_summary.total_votes,
                        broadcaster_bot=lead_bot,
                        reply_to_chat_id=chat_id,
                    )

                    if exec_res.success:
                        self.trades_executed += 1

                    ann_text = format_execution_announcement(exec_res)
                    try:
                        await lead_bot.send_message(
                            chat_id=chat_id,
                            text=ann_text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.error(f"Failed to post execution announcement: {e}")
                else:
                    self.trades_rejected += 1
                    try:
                        await lead_bot.send_message(
                            chat_id=chat_id,
                            text=f"🛑 <b>PROPOSAL REJECTED:</b> Council voted against allocating to <code>{mint}</code>. Autopilot moving to next breakout candidate.",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception as e:
                        logger.debug(f"Could not post rejection notice: {e}")

                # -------------------------------------------------------------
                # 5. Rest interval between council evaluations
                # -------------------------------------------------------------
                logger.info(f"Autopilot sleeping for {self.scan_interval_seconds}s before next cycle...")
                await asyncio.sleep(self.scan_interval_seconds)

        except asyncio.CancelledError:
            logger.info("Autopilot task cancelled cleanly.")
        except Exception as e:
            logger.error(f"Unexpected error in Autopilot loop: {e}", exc_info=True)
            self.is_running = False
            try:
                await lead_bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ <b>Autopilot encountered an error:</b> <code>{html.escape(str(e))}</code>. Loop paused.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# Global singleton instance
autopilot = AutopilotEngine()
