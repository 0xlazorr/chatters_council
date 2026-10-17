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
from positions import position_manager
from vault import profit_vault

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
        open_pos = position_manager.get_open_positions()
        return {
            "is_running": self.is_running,
            "uptime_seconds": uptime,
            "cycles": self.cycle_count,
            "evaluated_count": len(self.evaluated_mints),
            "approved": self.trades_approved,
            "rejected": self.trades_rejected,
            "executed": self.trades_executed,
            "open_positions_count": len(open_pos),
            "total_usdt_vaulted": profit_vault.total_usdt_harvested,
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
        return (
            True,
            "🚀 <b>AUTOPILOT ACTIVATED:</b> The AI Council is now screening breakout tokens in the background.\n\n"
            "• <b>Background Screening:</b> Only high-conviction tokens meeting institutional volume & liquidity criteria are brought to vote.\n"
            "• <b>Automated Exits:</b> Monitored 24/7 for Take-Profit (+50%), Trailing Stops, and Stop-Loss.\n"
            "• <b>One-Way Profit Vault:</b> Every $25 in realized profit is automatically secured in USDT (never burned).\n"
            "• <b>Safety Guard:</b> Loop auto-halts if wallet SOL drops below trade threshold.",
        )

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

    async def _enrich_candidate_raw(self, mint: str) -> Optional[Dict[str, Any]]:
        """Fetches raw on-chain pair telemetry for any token without applying screening filters."""
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

            solana_pairs.sort(
                key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
                reverse=True,
            )
            primary = solana_pairs[0]
            base_tok = primary.get("baseToken") or {}

            return {
                "mint": mint,
                "name": base_tok.get("name") or "Unknown",
                "symbol": (base_tok.get("symbol") or "SOL").upper(),
                "dex": (primary.get("dexId") or "Raydium").capitalize(),
                "liquidity_usd": float((primary.get("liquidity") or {}).get("usd") or 0.0),
                "fdv": float(primary.get("fdv") or 0.0),
                "price_usd": float(primary.get("priceUsd") or 0.0),
                "volume_24h": float((primary.get("volume") or {}).get("h24") or 0.0),
                "price_change_1h": float((primary.get("priceChange") or {}).get("h1") or 0.0),
                "url": primary.get("url") or f"https://dexscreener.com/solana/{mint}",
            }
        except Exception as e:
            logger.debug(f"Error fetching raw telemetry for {mint}: {e}")
            return None

    async def _enrich_candidate(self, mint: str) -> Optional[Dict[str, Any]]:
        """
        Background Institutional Screening Filter:
        Screens candidate tokens silently in the background.
        ONLY returns candidates meeting high-conviction breakout criteria:
        - Liquidity >= $10,000 USD (safe floor)
        - 24h Volume >= $25,000 USD (real active buyers)
        - 1h Momentum >= +1.0% (positive upward trajectory)
        - Market Cap between $20k and $25M (breakout potential sweet spot)
        - Volume-to-Liquidity ratio >= 0.5 (healthy turnover)
        """
        raw = await self._enrich_candidate_raw(mint)
        if not raw:
            return None

        liq = raw["liquidity_usd"]
        vol = raw["volume_24h"]
        h1 = raw["price_change_1h"]
        fdv = raw["fdv"]

        # 1. Liquidity Floor Check (Default $10,000 USD)
        if liq < config.AUTOPILOT_MIN_LIQUIDITY_USD:
            logger.debug(f"[Background Filter] {mint} skipped: Liquidity ${liq:,.0f} < ${config.AUTOPILOT_MIN_LIQUIDITY_USD:,.0f}")
            return None

        # 2. 24h Volume Check (Default $25,000 USD)
        if vol < config.AUTOPILOT_MIN_VOLUME_24H_USD:
            logger.debug(f"[Background Filter] {mint} skipped: 24h Vol ${vol:,.0f} < ${config.AUTOPILOT_MIN_VOLUME_24H_USD:,.0f}")
            return None

        # 3. 1h Momentum Check (Must be positive >= +1.0%)
        if h1 < config.AUTOPILOT_MIN_1H_CHANGE:
            logger.debug(f"[Background Filter] {mint} skipped: 1h Momentum {h1:+.1f}% < {config.AUTOPILOT_MIN_1H_CHANGE:+.1f}%")
            return None

        # 4. Market Cap Bounds Check ($20k to $25M)
        if fdv < 20000.0 or fdv > config.AUTOPILOT_MAX_FDV_USD:
            logger.debug(f"[Background Filter] {mint} skipped: FDV ${fdv:,.0f} outside target bounds")
            return None

        # 5. Volume-to-Liquidity Ratio Check (Turnover >= 0.5)
        ratio = vol / max(1.0, liq)
        if ratio < 0.5:
            logger.debug(f"[Background Filter] {mint} skipped: Vol-to-Liq ratio {ratio:.2f} < 0.50")
            return None

        logger.info(
            f"🎯 [HIGH-CONVICTION QUALIFIER] {raw['name']} (${raw['symbol']}) passed background screen: "
            f"Liq=${liq:,.0f}, Vol=${vol:,.0f}, 1h={h1:+.1f}%, FDV=${fdv:,.0f}"
        )
        return raw

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
                logger.info(f"[Autopilot Cycle #{self.cycle_count}] Running monitoring & screening cycle...")

                # -------------------------------------------------------------
                # 1. Active Positions Check: Take-Profit, Trailing Stop, Stop-Loss
                # -------------------------------------------------------------
                open_positions = position_manager.get_open_positions()
                if open_positions:
                    logger.info(f"Checking {len(open_positions)} open positions for profit-taking / exit triggers...")
                    for pos in open_positions:
                        live_pair = await self._enrich_candidate_raw(pos.mint)
                        if not live_pair:
                            continue

                        curr_price = live_pair.get("price_usd", 0.0)
                        should_exit, reason = position_manager.check_exit_triggers(pos.mint, curr_price)
                        if should_exit:
                            logger.info(f"🎯 Automated exit triggered for {pos.symbol} ({pos.mint}): {reason}")

                            # Execute SELL 100% via Market Sniffer
                            exit_res = await MSnifferExecutor.execute_consensus(
                                prompt=f"SELL 100% of {pos.mint}",
                                consensus_text=f"Council Automated Exit: {reason}",
                                yes_count=5,
                                total_votes=5,
                                broadcaster_bot=lead_bot,
                                reply_to_chat_id=chat_id,
                            )

                            if exit_res.success:
                                position_manager.close_position(
                                    mint=pos.mint,
                                    exit_price_usd=curr_price,
                                    exit_reason=reason,
                                    exit_tx_hash=exit_res.tx_hash or "",
                                )

                                pnl_badge = f"+{pos.pnl_pct:.1f}% 🟢" if pos.pnl_pct >= 0 else f"{pos.pnl_pct:.1f}% 🔴"
                                solscan_link = f"\n🔗 <a href='https://solscan.io/tx/{exit_res.tx_hash}'>View Solana Transaction</a>" if exit_res.tx_hash else ""
                                exit_msg = (
                                    f"🎯 <b>AUTOMATED POSITION EXIT EXECUTED!</b>\n"
                                    f"───────────────────────────────\n"
                                    f"🪙 <b>Token:</b> {html.escape(pos.name)} (<code>${html.escape(pos.symbol)}</code>)\n"
                                    f"📉 <b>Exit Trigger:</b> <code>{reason}</code>\n"
                                    f"💵 <b>Entry Price:</b> <code>${pos.entry_price_usd:.6f}</code> | <b>Exit:</b> <code>${curr_price:.6f}</code>\n"
                                    f"📊 <b>Realized Performance:</b> <b>{pnl_badge}</b>\n"
                                    f"⚡ <i>SOL proceeds returned to wallet for future trading & profit vault.</i>"
                                    f"{solscan_link}\n"
                                    f"───────────────────────────────"
                                )
                                try:
                                    await lead_bot.send_message(
                                        chat_id=chat_id,
                                        text=exit_msg,
                                        parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True,
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to post exit announcement: {e}")

                # -------------------------------------------------------------
                # 2. Profit Vault Check: Sweep $25 Realized Profit to USDT
                # -------------------------------------------------------------
                wallet_info = await MSnifferExecutor.get_wallet_status()
                if wallet_info.get("online"):
                    current_sol = wallet_info.get("sol_balance", 0.0)
                    await profit_vault.check_and_sweep(
                        current_sol_balance=current_sol,
                        broadcaster_bot=lead_bot,
                        reply_to_chat_id=chat_id,
                    )

                # -------------------------------------------------------------
                # 3. Wallet Balance & Treasury Safety Check
                # -------------------------------------------------------------
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
                # 4. Background Screening: Find High-Conviction Breakout Candidate
                # -------------------------------------------------------------
                raw_candidates = await self._fetch_trending_solana_mints()

                qualified_target = None
                for cand in raw_candidates:
                    mint = cand["mint"]
                    if mint in self.evaluated_mints:
                        continue
                    if len(self.evaluated_mints) > 1000:
                        self.evaluated_mints = set(list(self.evaluated_mints)[-500:])
                    self.evaluated_mints.add(mint)

                    # Strict background filter: liquidity > $10k, vol > $25k, momentum > +1%
                    enriched = await self._enrich_candidate(mint)
                    if enriched:
                        qualified_target = enriched
                        break

                if not qualified_target:
                    logger.debug("No candidate met high-conviction criteria in this scan. Sleeping...")
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
                # 5. Convene Council: Debate and Formal Vote with Real Market Stats
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
                # 6. Automatic Execution If Consensus Passes
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
                        # Register active position for automatic exit & profit monitoring
                        position_manager.open_position(
                            mint=mint,
                            symbol=symbol,
                            name=name,
                            entry_price_usd=price,
                            amount_sol=trade_amt,
                            tx_hash=exec_res.tx_hash or "",
                            exit_strategy=config.DEFAULT_EXIT_STRATEGY,
                        )

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
                # 7. Rest interval between council evaluations
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
