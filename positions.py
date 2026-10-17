"""
positions.py - Active Token Position Tracking & Automated Exit / Profit-Taking Engine.

Monitors tokens purchased by the Council:
1. Tracks entry price, current price, peak price, and unrealized PnL %
2. Evaluates exit triggers:
   - Take-Profit Target (default: +50% or +100%)
   - Trailing Stop-Loss (default: 15% retrace from peak after +20% gain)
   - Stop-Loss Protection (default: -25%)
3. Executes automated SELL (100% of tokens to SOL) via Market Sniffer
4. Persists state to data/positions.json
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger("multi_agent_council.positions")

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "positions.json")


@dataclass
class Position:
    mint: str
    symbol: str
    name: str
    entry_price_usd: float
    current_price_usd: float
    peak_price_usd: float
    amount_sol: float
    entry_timestamp: float
    status: str = "OPEN"  # "OPEN" or "CLOSED"
    exit_strategy: str = "AUTO_EXIT"  # "AUTO_EXIT" or "HOLD_INDEFINITE"
    pnl_pct: float = 0.0
    buy_tx_hash: str = ""
    exit_tx_hash: str = ""
    exit_price_usd: float = 0.0
    exit_reason: str = ""
    exit_timestamp: float = 0.0


class PositionManager:
    """Manages active trading positions and exit condition evaluation."""

    def __init__(self, filepath: str = POSITIONS_FILE):
        self.filepath = filepath
        self.positions: Dict[str, Position] = {}
        self._load()

    def _load(self):
        """Loads saved positions from local JSON storage."""
        if not os.path.exists(self.filepath):
            return
        try:
            valid_keys = {f.name for f in fields(Position)}
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    filtered = {k: v for k, v in item.items() if k in valid_keys}
                    pos = Position(**filtered)
                    self.positions[pos.mint] = pos
            logger.info(f"Loaded {len(self.positions)} positions from {self.filepath}")
        except Exception as e:
            logger.error(f"Error loading positions from {self.filepath}: {e}")

    def _save(self):
        """Persists positions atomically to disk."""
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            data = [asdict(p) for p in self.positions.values()]
            tmp_path = self.filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.filepath)
        except Exception as e:
            logger.error(f"Error saving positions to {self.filepath}: {e}")

    def open_position(
        self,
        mint: str,
        symbol: str,
        name: str,
        entry_price_usd: float,
        amount_sol: float,
        tx_hash: str = "",
        exit_strategy: Optional[str] = None,
    ) -> Position:
        """Registers a newly purchased token position with designated exit strategy."""
        strat = exit_strategy.upper() if exit_strategy else config.DEFAULT_EXIT_STRATEGY
        if strat not in ("AUTO_EXIT", "HOLD_INDEFINITE"):
            strat = "AUTO_EXIT"

        pos = Position(
            mint=mint,
            symbol=symbol or "UNKNOWN",
            name=name or "Unknown Token",
            entry_price_usd=entry_price_usd if entry_price_usd > 0 else 0.0001,
            current_price_usd=entry_price_usd,
            peak_price_usd=entry_price_usd,
            amount_sol=amount_sol,
            entry_timestamp=time.time(),
            status="OPEN",
            exit_strategy=strat,
            pnl_pct=0.0,
            buy_tx_hash=tx_hash,
        )
        self.positions[mint] = pos
        self._save()
        logger.info(
            f"📈 Opened position: {symbol} ({mint}) at ${entry_price_usd:.6f} "
            f"with {amount_sol} SOL | Strategy: {strat}"
        )
        return pos

    def get_open_positions(self) -> List[Position]:
        """Returns all currently active positions."""
        return [p for p in self.positions.values() if p.status == "OPEN"]

    def set_position_strategy(self, mint: str, strategy: str) -> Tuple[bool, str]:
        """Updates the exit strategy for an existing open position."""
        pos = self.positions.get(mint)
        if not pos or pos.status != "OPEN":
            return False, f"No open position found for token <code>{mint}</code>"

        strat = strategy.upper()
        if strat not in ("AUTO_EXIT", "HOLD_INDEFINITE"):
            return False, "Strategy must be AUTO_EXIT or HOLD_INDEFINITE"

        pos.exit_strategy = strat
        self._save()
        desc = "💎 HOLD INDEFINITELY" if strat == "HOLD_INDEFINITE" else f"🎯 AUTO-EXIT (+{config.TAKE_PROFIT_PCT:.0f}%)"
        return True, f"Updated strategy for <b>{pos.name}</b> (<code>${pos.symbol}</code>) to <b>{desc}</b>"

    def update_price(self, mint: str, current_price_usd: float) -> Optional[Position]:
        """Updates live price, peak price, and PnL for a position."""
        pos = self.positions.get(mint)
        if not pos or pos.status != "OPEN":
            return None

        if current_price_usd > 0:
            pos.current_price_usd = current_price_usd
            if current_price_usd > pos.peak_price_usd:
                pos.peak_price_usd = current_price_usd

            if pos.entry_price_usd > 0:
                pos.pnl_pct = round(((current_price_usd - pos.entry_price_usd) / pos.entry_price_usd) * 100.0, 2)

            self._save()
        return pos

    def check_exit_triggers(self, mint: str, current_price_usd: float) -> Tuple[bool, str]:
        """
        Evaluates whether a position should be sold to take profit or prevent losses.
        Returns: (should_exit, reason_description)
        """
        pos = self.update_price(mint, current_price_usd)
        if not pos or pos.status != "OPEN":
            return False, ""

        # Option: HOLD_INDEFINITE (Diamond Hands)
        # Never triggers automatic sell; position is held until Admin manually exits with /sell
        if pos.exit_strategy == "HOLD_INDEFINITE":
            return False, ""

        pnl = pos.pnl_pct

        # 1. Take-Profit Target Trigger (default +50% gain)
        if pnl >= config.TAKE_PROFIT_PCT:
            return True, f"Take-Profit Target Reached (+{pnl:.1f}% gain)"

        # 2. Trailing Stop Trigger (If reached +20% profit, and retraced by TRAILING_STOP_PCT from peak)
        if pos.peak_price_usd >= pos.entry_price_usd * 1.20:
            drawdown_from_peak = ((pos.peak_price_usd - current_price_usd) / pos.peak_price_usd) * 100.0
            if drawdown_from_peak >= config.TRAILING_STOP_PCT:
                return True, f"Trailing Stop Triggered (-{drawdown_from_peak:.1f}% from peak, locked in +{pnl:.1f}%)"

        # 3. Stop-Loss Trigger (default -25%)
        if pnl <= -config.STOP_LOSS_PCT:
            return True, f"Stop-Loss Triggered ({pnl:.1f}% loss limit)"

        return False, ""

    def close_position(
        self,
        mint: str,
        exit_price_usd: float,
        exit_reason: str,
        exit_tx_hash: str = "",
    ) -> Optional[Position]:
        """Marks a position as CLOSED with realized performance."""
        pos = self.positions.get(mint)
        if not pos:
            return None

        pos.status = "CLOSED"
        pos.exit_price_usd = exit_price_usd
        pos.exit_reason = exit_reason
        pos.exit_tx_hash = exit_tx_hash
        pos.exit_timestamp = time.time()
        if pos.entry_price_usd > 0 and exit_price_usd > 0:
            pos.pnl_pct = round(((exit_price_usd - pos.entry_price_usd) / pos.entry_price_usd) * 100.0, 2)

        self._save()
        logger.info(
            f"🎯 Closed position: {pos.symbol} ({mint}) at ${exit_price_usd:.6f} "
            f"| PnL: {pos.pnl_pct:+.1f}% | Reason: {exit_reason}"
        )
        return pos

    def get_summary_text(self) -> str:
        """Formats active positions into a clean Telegram Markdown overview."""
        open_pos = self.get_open_positions()
        if not open_pos:
            return "📊 <b>Active Positions:</b> None. AI Council is screening for new breakout entries."

        lines = [
            "📈 <b>ACTIVE COUNCIL POSITIONS PORTFOLIO</b>",
            "───────────────────────────────",
        ]
        for p in open_pos:
            pnl_badge = f"+{p.pnl_pct:.1f}% 🟢" if p.pnl_pct >= 0 else f"{p.pnl_pct:.1f}% 🔴"
            dex_url = f"https://dexscreener.com/solana/{p.mint}"
            strat_badge = "💎 HOLD" if p.exit_strategy == "HOLD_INDEFINITE" else f"🎯 AUTO-EXIT (+{config.TAKE_PROFIT_PCT:.0f}%)"
            lines.append(
                f"🪙 <b>{p.name}</b> (<code>${p.symbol}</code>)\n"
                f"  • Strategy: <b>{strat_badge}</b> | PnL: <b>{pnl_badge}</b>\n"
                f"  • Entry: <code>${p.entry_price_usd:.6f}</code> | Current: <code>${p.current_price_usd:.6f}</code>\n"
                f"  • Size: <code>{p.amount_sol:.2f} SOL</code> | Chart: <a href='{dex_url}'>DexScreener</a>\n"
            )
        lines.append("───────────────────────────────")
        lines.append("<i>Controls: /sell &lt;CA&gt; [pct] to exit anytime | /exitmode to change default strategy</i>")
        return "\n".join(lines)


# Global singleton instance
position_manager = PositionManager()
