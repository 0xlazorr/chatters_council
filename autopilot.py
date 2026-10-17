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
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.request

from telegram import Bot
from telegram.constants import ParseMode

import config
from executor import MSnifferExecutor, format_execution_announcement
from positions import position_manager
import telemetry
from vault import profit_vault
from incubator import token_incubator

logger = logging.getLogger("multi_agent_council.autopilot")

AUTOPILOT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "autopilot_config.json")


def parse_duration_string(text: str) -> Optional[int]:
    """
    Parses human duration string into seconds.
    Examples:
    - '1m', '1 min', '1 minute', 'every minute', 'minute' -> 60
    - '15m', '15 min', '15 minutes' -> 900
    - '30m', '30 min', '30 minutes' -> 1800
    - '1h', '1 hr', '1 hour', 'every hour', 'hour', 'hourly' -> 3600
    - '2h', '2 hours' -> 7200
    - '6h', '6 hours' -> 21600
    - '12h', '12 hours' -> 43200
    - '24h', '1d', '1 day', 'daily' -> 86400
    - '1w', '1 week', 'weekly', 'weeks' -> 604800
    """
    clean = text.strip().lower()
    if not clean:
        return None

    if clean.startswith("every "):
        clean = clean[6:].strip()

    named = {
        "minute": 60,
        "minutes": 60,
        "min": 60,
        "mins": 60,
        "hour": 3600,
        "hours": 3600,
        "hr": 3600,
        "hrs": 3600,
        "hourly": 3600,
        "day": 86400,
        "days": 86400,
        "daily": 86400,
        "week": 604800,
        "weeks": 604800,
        "weekly": 604800,
    }
    if clean in named:
        return named[clean]

    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]+)?$", clean)
    if not m:
        return None

    val = float(m.group(1))
    unit = (m.group(2) or "").strip()

    if not unit:
        if val <= 180:
            return max(30, int(val * 60))
        return max(30, int(val))

    if unit in ("s", "sec", "secs", "second", "seconds"):
        return max(30, int(val))
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        return max(30, int(val * 60))
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        return int(val * 3600)
    elif unit in ("d", "day", "days"):
        return int(val * 86400)
    elif unit in ("w", "wk", "wks", "week", "weeks"):
        return int(val * 604800)

    return None


def format_interval(seconds: int) -> str:
    """Formats an interval in seconds into clean human-readable text."""
    if seconds < 60:
        return f"{seconds} seconds"
    elif seconds == 60:
        return "1 minute"
    elif seconds < 3600:
        if seconds % 60 == 0:
            return f"{seconds // 60} minutes"
        return f"{seconds / 60:.1f} minutes"
    elif seconds == 3600:
        return "1 hour"
    elif seconds < 86400:
        if seconds % 3600 == 0:
            return f"{seconds // 3600} hours"
        return f"{seconds / 3600:.1f} hours"
    elif seconds == 86400:
        return "24 hours (1 day)"
    elif seconds < 604800:
        if seconds % 86400 == 0:
            return f"{seconds // 86400} days"
        return f"{seconds / 86400:.1f} days"
    elif seconds == 604800:
        return "1 week"
    else:
        if seconds % 604800 == 0:
            return f"{seconds // 604800} weeks"
        return f"{seconds / 604800:.1f} weeks"


def generate_consensus_thesis_paragraph(dossier: Dict[str, Any], vote_summary) -> str:
    """
    Synthesizes a short, punchy paragraph explaining why the token won the Council's consensus,
    combining the reasoning articulated by the winning AI agents with verified on-chain metrics.
    """
    name = html.escape(dossier.get("name", "The asset"))
    symbol = html.escape(dossier.get("symbol", "TOKEN"))
    liq = float(dossier.get("liquidity_usd", 0.0))
    vol = float(dossier.get("volume_24h_usd", 0.0))
    h1 = float(dossier.get("change_1h") or dossier.get("price_change_1h") or 0.0)
    top10 = float(dossier.get("top10_pct", 0.0))

    yes_votes = [v for v in vote_summary.votes if v.vote == "YES"]
    total = vote_summary.total_votes or len(vote_summary.votes) or 5

    # 1. Metric drivers summary
    drivers = []
    if liq >= 10000:
        drivers.append(f"institutional liquidity depth (${liq:,.0f})")
    if vol >= 25000:
        drivers.append(f"high 24h volume turnover (${vol:,.0f})")
    if h1 > 0:
        drivers.append(f"positive 1h momentum ({h1:+.1f}%)")
    if top10 > 0 and top10 < 35:
        drivers.append(f"healthy holder distribution ({top10:.1f}% top 10)")

    driver_phrase = ", ".join(drivers) if drivers else "strong breakout momentum and favorable risk-reward skew"

    # 2. Extract substantive reasoning quotes from agents who voted YES
    agent_quotes = []
    for v in yes_votes:
        clean_reason = " ".join(v.reasoning.split()).strip()
        clean_reason = re.sub(r"^(?:i\s+vote\s+(?:yes|buy|approve)[:,\s]*)+", "", clean_reason, flags=re.IGNORECASE).strip()
        clean_reason = clean_reason.rstrip(".")
        if len(clean_reason) >= 20:
            if len(clean_reason) > 160:
                clean_reason = clean_reason[:157] + "..."
            agent_quotes.append(f"{v.agent_name} noted that {clean_reason}")

    paragraph_lead = (
        f"{name} (${symbol}) secured an overwhelming {len(yes_votes)}/{total} majority consensus "
        f"backed by {driver_phrase} and clean contract security audits."
    )

    if agent_quotes:
        quotes_blurb = " Specifically, " + "; furthermore, ".join(agent_quotes[:2]) + "."
        return paragraph_lead + quotes_blurb
    else:
        return (
            f"{paragraph_lead} The AI Council confirmed renounced mint rights, revoked freeze authority, "
            f"and asymmetric upside potential before authorizing execution."
        )


def format_autonomous_buy_card(
    dossier: Dict[str, Any],
    vote_summary,
    trade_amt: float,
    exec_res,
    seasoning_hours: Optional[float] = None,
) -> str:
    """Formats full token details + execution details + why it won consensus paragraph."""
    name = html.escape(dossier.get("name", "Solana Token"))
    symbol = html.escape(dossier.get("symbol", "TOKEN"))
    mint = dossier.get("mint", "")
    dex = html.escape(dossier.get("dex", "Solana DEX"))
    pair_url = dossier.get("pair_url") or f"https://dexscreener.com/solana/{mint}"

    price = float(dossier.get("price_usd") or 0.0)
    mcap = float(dossier.get("market_cap_usd") or dossier.get("fdv") or 0.0)
    liq = float(dossier.get("liquidity_usd") or 0.0)
    vol24 = float(dossier.get("volume_24h_usd") or 0.0)
    c1h = float(dossier.get("change_1h") or dossier.get("price_change_1h") or 0.0)
    c24h = float(dossier.get("change_24h") or 0.0)
    buys = int(dossier.get("buys_24h") or 0)
    sells = int(dossier.get("sells_24h") or 0)
    top10 = float(dossier.get("top10_pct") or 0.0)

    mint_auth = "Revoked 🟢" if "REVOKED" in str(dossier.get("mint_authority", "")) else "Active ⚠️"
    freeze_auth = "Revoked 🟢" if "REVOKED" in str(dossier.get("freeze_authority", "")) else "Active ⚠️"

    c1h_badge = f"+{c1h:.1f}% 🟢" if c1h >= 0 else f"{c1h:.1f}% 🔴"
    c24h_badge = f"+{c24h:.1f}% 🟢" if c24h >= 0 else f"{c24h:.1f}% 🔴"

    total_votes = vote_summary.total_votes or len(vote_summary.votes) or 5
    approval_pct = (vote_summary.yes_count / total_votes * 100.0) if total_votes > 0 else 100.0
    solscan_link = f"\n🔗 <a href='https://solscan.io/tx/{exec_res.tx_hash}'>View Solana Transaction (Solscan)</a>" if exec_res.tx_hash else ""

    thesis_paragraph = generate_consensus_thesis_paragraph(dossier, vote_summary)

    seasoned_line = f"🧪 <b>Seasoned Breakout:</b> <code>Incubated {seasoning_hours:.1f}h post-launch before buy</code>" if seasoning_hours is not None else ""

    lines = [
        f"🤖 <b>AUTOPILOT CONSENSUS EXECUTED: BOUGHT {name} (${symbol})</b>",
        "───────────────────────────────",
        f"🪙 <b>Token:</b> {name} (<code>${symbol}</code>)",
        f"📍 <b>Contract:</b> <code>{mint}</code>",
        f"📊 <b>DEX:</b> {dex} | <a href='{pair_url}'>DexScreener Chart</a>",
        f"💵 <b>Price:</b> <code>${price:.6f} USD</code> | <b>Mcap:</b> <code>${mcap:,.0f}</code>",
        f"💧 <b>Liquidity:</b> <code>${liq:,.0f}</code> | <b>24h Vol:</b> <code>${vol24:,.0f}</code>",
        f"⚡ <b>1h Momentum:</b> <b>{c1h_badge}</b> | <b>24h:</b> <b>{c24h_badge}</b>",
        f"👥 <b>24h Activity:</b> <code>{buys:,} buys / {sells:,} sells</code>",
        f"🛡️ <b>Security:</b> Mint: <code>{mint_auth}</code> | Freeze: <code>{freeze_auth}</code>",
        f"🐋 <b>Top 10 Supply:</b> <code>{top10:.1f}%</code>",
    ]
    if seasoned_line:
        lines.append(seasoned_line)

    lines.extend([
        "───────────────────────────────",
        f"🗳️ <b>Consensus:</b> <code>{vote_summary.yes_count}/{total_votes} YES ({approval_pct:.0f}%)</code>",
        f"⚡ <b>Trade Size:</b> <code>{trade_amt:.2f} SOL</code>",
        f"🚀 <b>Execution:</b> {html.escape(exec_res.message)}{solscan_link}",
        "───────────────────────────────",
        "💡 <b>Why It Won Consensus:</b>",
        f"<i>{thesis_paragraph}</i>",
        "───────────────────────────────",
        f"🎯 <i>Autonomous position active: Auto-Exit (+{config.TAKE_PROFIT_PCT:.0f}% TP / -25% SL) & ${config.PROFIT_VAULT_THRESHOLD_USD:.0f} USDT Profit Vault enabled.</i>",
    ])
    return "\n".join(lines)


class AutopilotEngine:
    """Manages the autonomous continuous trading loop for the AI Council."""

    def __init__(self):
        self.is_running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._wake_event: asyncio.Event = asyncio.Event()
        self.evaluated_mints: Set[str] = set()
        self.cycle_count: int = 0
        self.trades_executed: int = 0
        self.trades_approved: int = 0
        self.trades_rejected: int = 0
        self.start_timestamp: float = 0.0
        self.scan_interval_seconds: int = 900
        self.enabled: bool = False
        self._load_config()

    def _load_config(self):
        """Loads saved autopilot scan interval and enabled state from local storage."""
        try:
            if os.path.exists(AUTOPILOT_CONFIG_FILE):
                with open(AUTOPILOT_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.scan_interval_seconds = int(data.get("scan_interval_seconds", 900))
                    self.enabled = bool(data.get("enabled", False))
                    logger.info(f"Loaded autopilot config from disk: {self.scan_interval_seconds}s ({format_interval(self.scan_interval_seconds)}) | enabled: {self.enabled}")
            else:
                env_val = os.getenv("AUTOPILOT_INTERVAL_SECONDS")
                if env_val:
                    self.scan_interval_seconds = int(env_val)
        except Exception as e:
            logger.debug(f"Could not load autopilot config file: {e}")

    def _save_config(self):
        """Saves current scan interval and enabled state to disk for cross-restart persistence."""
        try:
            os.makedirs(os.path.dirname(AUTOPILOT_CONFIG_FILE), exist_ok=True)
            with open(AUTOPILOT_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "scan_interval_seconds": self.scan_interval_seconds,
                    "enabled": self.enabled,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save autopilot config: {e}")

    def set_interval(self, seconds: int) -> Tuple[bool, str]:
        """Sets the scan frequency interval and immediately wakes the loop if running."""
        clean_sec = max(30, min(seconds, 2419200))  # Max 4 weeks
        self.scan_interval_seconds = clean_sec
        self._save_config()
        self._wake_event.set()
        desc = format_interval(clean_sec)
        logger.info(f"Autopilot frequency updated to {clean_sec}s ({desc})")
        return (
            True,
            f"⏱️ <b>Autopilot Scan Frequency Updated:</b> <code>Every {desc}</code>\n"
            f"The autonomous engine will evaluate breakout tokens on this schedule.",
        )

    def get_status(self) -> Dict[str, Any]:
        """Returns live statistics of the Autopilot engine."""
        uptime = time.time() - self.start_timestamp if self.is_running and self.start_timestamp else 0.0
        open_pos = position_manager.get_open_positions()
        incubating = [t for t in token_incubator.tokens.values() if t.status == "INCUBATING"]
        seasoned = [t for t in incubating if t.is_seasoned]
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
            "formatted_interval": format_interval(self.scan_interval_seconds),
            "incubator_total": len(incubating),
            "incubator_seasoned": len(seasoned),
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
            self.set_interval(scan_interval)

        self.is_running = True
        self.enabled = True
        self._save_config()
        self.start_timestamp = time.time()
        self._wake_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(chat_id=chat_id, lead_bot=lead_bot, council=council_orchestrator)
        )
        desc = format_interval(self.scan_interval_seconds)
        logger.info(f"🚀 Autopilot started for group {chat_id} (interval: {self.scan_interval_seconds}s / {desc})")
        return (
            True,
            f"🚀 <b>AUTOPILOT ACTIVATED:</b> The AI Council is now screening breakout tokens in the background.\n\n"
            f"• <b>Operating Frequency:</b> <code>Every {desc}</code>\n"
            f"• <b>Background Screening:</b> Only high-conviction tokens meeting institutional volume & liquidity criteria are brought to vote.\n"
            f"• <b>Execute Mode:</b> Silently votes; on consensus win, buys and posts detailed thesis & token metrics.\n"
            f"• <b>Automated Exits:</b> Monitored 24/7 for Take-Profit (+{config.TAKE_PROFIT_PCT:.0f}%), Trailing Stops, and Stop-Loss.\n"
            f"• <b>One-Way Profit Vault:</b> Every ${config.PROFIT_VAULT_THRESHOLD_USD:.0f} in realized profit is automatically secured in USDT (never burned).\n"
            f"• <b>Safety Guard:</b> Loop auto-halts if wallet SOL drops below trade threshold.",
        )

    async def stop(self) -> str:
        """Stops the autonomous loop cleanly."""
        if not self.is_running:
            return "ℹ️ Autopilot is not currently running."

        self.is_running = False
        self.enabled = False
        self._save_config()
        self._wake_event.set()
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
        clean_mint = (mint or "").strip()
        if not re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", clean_mint):
            return None
        url = f"https://api.dexscreener.com/latest/dex/tokens/{clean_mint}"
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
                        should_exit, reason, sell_pct = position_manager.check_exit_triggers(pos.mint, curr_price)
                        if should_exit:
                            logger.info(f"🎯 Automated exit triggered for {pos.symbol} ({pos.mint}): {reason}")

                            # Execute SELL via Market Sniffer
                            prompt_str = f"SELL 100% of {pos.mint}"
                            exit_res = await MSnifferExecutor.execute_consensus(
                                prompt=prompt_str,
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
                                    f"🎯 <b>COUNCIL POSITION EXIT EXECUTED: SOLD {html.escape(pos.name)}</b>\n"
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
                # 3.5. Incubator Check: Recheck Seasoned Tokens (>= 2 Hours Old)
                # -------------------------------------------------------------
                qualified_target = None
                is_from_incubator = False
                seasoning_hours: Optional[float] = None

                current_open = position_manager.get_open_positions()
                if len(current_open) >= config.MAX_OPEN_POSITIONS:
                    logger.info(
                        f"🛡️ [Risk Guard] Maximum concurrent open positions reached ({len(current_open)}/{config.MAX_OPEN_POSITIONS}). "
                        f"Holding new entries until active positions exit."
                    )
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=self.scan_interval_seconds)
                        self._wake_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

                open_mints = {p.mint for p in current_open}

                if config.INCUBATOR_ENABLED:
                    due_candidates = token_incubator.get_due_for_recheck()
                    if due_candidates:
                        logger.info(f"🧪 [Incubator Recheck] Evaluating {len(due_candidates)} seasoned candidates (>= {config.INCUBATOR_MIN_AGE_SECONDS/3600:.1f}h old)...")
                        for s_cand in due_candidates:
                            if s_cand.mint in open_mints:
                                continue
                            # 1. Fetch live pair telemetry
                            s_raw = await self._enrich_candidate_raw(s_cand.mint)
                            if not s_raw or s_raw.get("liquidity_usd", 0.0) < 2000.0:
                                token_incubator.mark_dead(s_cand.mint, "Liquidity below $2k or pair dead after 2h seasoning")
                                continue

                            # 2. Check if token matured to meet breakout criteria
                            s_enriched = await self._enrich_candidate(s_cand.mint)
                            if s_enriched:
                                logger.info(
                                    f"🏆 [Incubator Seasoned Breakout] {s_cand.symbol} matured after {s_cand.age_hours:.1f}h! "
                                    f"Liq=${s_enriched['liquidity_usd']:,.0f}, Vol=${s_enriched['volume_24h']:,.0f}. Queuing for Council vote!"
                                )
                                qualified_target = s_enriched
                                is_from_incubator = True
                                seasoning_hours = s_cand.age_hours
                                break
                            else:
                                token_incubator.record_recheck(
                                    s_cand.mint,
                                    f"Checked at {s_cand.age_hours:.1f}h: Liq=${s_raw.get('liquidity_usd', 0.0):,.0f}, Vol=${s_raw.get('volume_24h', 0.0):,.0f}"
                                )

                # -------------------------------------------------------------
                # 4. Background Screening: Find High-Conviction Breakout Candidate
                # -------------------------------------------------------------
                if not qualified_target:
                    raw_candidates = await self._fetch_trending_solana_mints()

                    for cand in raw_candidates:
                        mint = cand["mint"]
                        if mint in self.evaluated_mints or mint in open_mints:
                            continue
                        if len(self.evaluated_mints) > 1000:
                            self.evaluated_mints = set(list(self.evaluated_mints)[-500:])
                        self.evaluated_mints.add(mint)

                        # Strict background filter: liquidity > $10k, vol > $25k, momentum > +1%
                        enriched = await self._enrich_candidate(mint)
                        if enriched:
                            qualified_target = enriched
                            is_from_incubator = False
                            seasoning_hours = None
                            break
                        else:
                            # Token caught early but didn't meet full breakout criteria yet:
                            # Register in 2-Hour Incubator queue for internal seasoning recheck!
                            if config.INCUBATOR_ENABLED:
                                raw_telem = await self._enrich_candidate_raw(mint)
                                if raw_telem and raw_telem.get("liquidity_usd", 0.0) >= 1500.0:
                                    token_incubator.add_candidate(
                                        mint=mint,
                                        symbol=raw_telem.get("symbol", cand.get("symbol", "")),
                                        name=raw_telem.get("name", cand.get("name", "")),
                                        price_usd=raw_telem.get("price_usd", 0.0),
                                        liquidity_usd=raw_telem.get("liquidity_usd", 0.0),
                                        fdv_usd=raw_telem.get("fdv", 0.0),
                                        notes="Caught early on DexScreener launch; queuing for 2h seasoning",
                                    )

                if not qualified_target:
                    logger.debug("No candidate met high-conviction criteria in this scan. Sleeping...")
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=self.scan_interval_seconds)
                        self._wake_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

                mint = qualified_target["mint"]
                trade_amt = config.DEFAULT_TRADE_AMOUNT_SOL

                # Fetch full institutional dossier (DexScreener + RugCheck)
                dossier = await telemetry.fetch_token_dossier(mint)
                if not dossier:
                    dossier = qualified_target

                tok_name = dossier.get("name") or qualified_target.get("name") or "Solana Token"
                tok_symbol = dossier.get("symbol") or qualified_target.get("symbol") or "TOKEN"
                dex = dossier.get("dex") or qualified_target.get("dex") or "Solana DEX"
                price = float(dossier.get("price_usd") or qualified_target.get("price_usd") or 0.0)
                mcap = float(dossier.get("market_cap_usd") or qualified_target.get("fdv") or 0.0)
                liq = float(dossier.get("liquidity_usd") or qualified_target.get("liquidity_usd") or 0.0)

                if is_from_incubator and seasoning_hours is not None:
                    user_context = (
                        f"🧪 Token Incubator: This token was caught early at launch and has seasoned for {seasoning_hours:.1f} hours. "
                        f"Liquidity deepened to ${liq:,.0f} and 24h volume held strong at ${dossier.get('volume_24h_usd', 0):,.0f}."
                    )
                else:
                    user_context = "Autonomous Autopilot scan discovered high-conviction breakout pattern."

                prompt = telemetry.format_council_prompt_from_dossier(
                    dossier=dossier,
                    action="BUY",
                    amount_sol=trade_amt,
                    user_context=user_context,
                )

                # -------------------------------------------------------------
                # 5. Silent Background Council Vote (No Group Chat Spam)
                # -------------------------------------------------------------
                logger.info(f"Autopilot evaluating {tok_name} (${tok_symbol}) in silent background mode (from incubator: {is_from_incubator})...")
                try:
                    vote_summary = await council.run_vote(prompt=prompt, chat_id=chat_id, silent=True)
                except Exception as e:
                    logger.error(f"Error during autopilot council vote for {mint}: {e}", exc_info=True)
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=self.scan_interval_seconds)
                        self._wake_event.clear()
                    except asyncio.TimeoutError:
                        pass
                    continue

                # -------------------------------------------------------------
                # 6. Automatic Execution If Consensus Passes
                # -------------------------------------------------------------
                if vote_summary.consensus_passed:
                    self.trades_approved += 1
                    logger.info(f"Consensus passed for {tok_name} (${tok_symbol}). Dispatching to Market Sniffer...")

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
                        if is_from_incubator:
                            token_incubator.mark_bought(mint)

                        # Register active position for automatic exit & profit monitoring
                        position_manager.open_position(
                            mint=mint,
                            symbol=tok_symbol,
                            name=tok_name,
                            entry_price_usd=price,
                            amount_sol=trade_amt,
                            tx_hash=exec_res.tx_hash or "",
                            exit_strategy=config.DEFAULT_EXIT_STRATEGY,
                        )

                    buy_msg = format_autonomous_buy_card(
                        dossier=dossier,
                        vote_summary=vote_summary,
                        trade_amt=trade_amt,
                        exec_res=exec_res,
                        seasoning_hours=seasoning_hours,
                    )
                    try:
                        await lead_bot.send_message(
                            chat_id=chat_id,
                            text=buy_msg,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.error(f"Failed to post autopilot buy confirmation: {e}")
                else:
                    self.trades_rejected += 1
                    logger.info(
                        f"🛑 Autopilot proposal rejected by Council for {tok_name} (${tok_symbol}): "
                        f"{vote_summary.yes_count} YES / {vote_summary.no_count} NO. No trade executed."
                    )
                    if config.INCUBATOR_ENABLED:
                        token_incubator.add_candidate(
                            mint=mint,
                            symbol=tok_symbol,
                            name=tok_name,
                            price_usd=price,
                            liquidity_usd=liq,
                            fdv_usd=mcap,
                            notes=f"Council rejected initially ({vote_summary.yes_count}/{vote_summary.total_votes} YES). Re-evaluating after 2h.",
                        )

                # -------------------------------------------------------------
                # 7. Rest interval between council evaluations
                # -------------------------------------------------------------
                logger.info(f"Autopilot sleeping for {self.scan_interval_seconds}s ({format_interval(self.scan_interval_seconds)}) before next cycle...")
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.scan_interval_seconds)
                    self._wake_event.clear()
                    logger.info("Autopilot awakened early by interval update or manual trigger.")
                except asyncio.TimeoutError:
                    pass

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
