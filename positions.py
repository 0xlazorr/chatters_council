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

import html
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
    exit_strategy: str = "DUAL_SPLIT"  # "DUAL_SPLIT", "50PCT_TAKE_PROFIT", "2X_TAKE_PROFIT", "TRAILING_RUNNER", "HOLD_INDEFINITE"
    pnl_pct: float = 0.0
    buy_tx_hash: str = ""
    exit_tx_hash: str = ""
    exit_price_usd: float = 0.0
    exit_reason: str = ""
    exit_timestamp: float = 0.0
    scalp_sold: bool = False
    moonbag_retained: bool = False
    wallet_type: str = "HOT_TRADING"


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
        if strat in ("DUAL", "DUAL_SPLIT", "SPLIT", "MOONBAG", "MOONBAG_SPLIT"):
            strat = "DUAL_SPLIT"
        elif strat in ("50", "50%", "50PCT", "SCALP", "SCALP_50", "50PCT_TAKE_PROFIT"):
            strat = "50PCT_TAKE_PROFIT"
        elif strat in ("2X", "2X_TAKE_PROFIT", "AUTO_EXIT", "100", "100%"):
            strat = "2X_TAKE_PROFIT"
        elif strat in ("TRAILING_RUNNER", "RUNNER", "TRAIL", "LONG"):
            strat = "TRAILING_RUNNER"
        elif strat in ("HOLD_INDEFINITE", "HOLD", "DIAMOND"):
            strat = "HOLD_INDEFINITE"
        else:
            strat = "DUAL_SPLIT"

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
            scalp_sold=False,
            moonbag_retained=False,
            wallet_type="HOT_TRADING",
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
        if strat in ("DUAL", "DUAL_SPLIT", "SPLIT", "MOONBAG", "MOONBAG_SPLIT"):
            strat = "DUAL_SPLIT"
            desc = "⚡ SCALP + 💎 MOONBAG (Sell 50% at profit, hold 50% indefinitely)"
        elif strat in ("50", "50%", "50PCT", "SCALP", "SCALP_50", "50PCT_TAKE_PROFIT"):
            strat = "50PCT_TAKE_PROFIT"
            desc = "⚡ QUICK 50% SCALP (+50.0% Take-Profit)"
        elif strat in ("2X", "2X_TAKE_PROFIT", "AUTO_EXIT", "100", "100%"):
            strat = "2X_TAKE_PROFIT"
            desc = "🎯 FIXED 2X TAKE-PROFIT (+100.0% / 2x Return)"
        elif strat in ("TRAILING_RUNNER", "RUNNER", "TRAIL", "LONG"):
            strat = "TRAILING_RUNNER"
            desc = f"🚀 LONG RUNNER (Trail peak by -{config.TRAILING_STOP_PCT:.0f}% after +50%)"
        elif strat in ("HOLD_INDEFINITE", "HOLD", "DIAMOND"):
            strat = "HOLD_INDEFINITE"
            desc = "💎 HOLD INDEFINITELY"
        else:
            return False, "Strategy must be DUAL_SPLIT (Scalp+Moonbag), 50PCT_TAKE_PROFIT, 2X_TAKE_PROFIT, TRAILING_RUNNER, or HOLD_INDEFINITE"

        pos.exit_strategy = strat
        self._save()
        safe_name = html.escape(pos.name)
        safe_sym = html.escape(pos.symbol)
        return True, f"Updated strategy for <b>{safe_name}</b> (<code>${safe_sym}</code>) to <b>{desc}</b>"

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

    def check_exit_triggers(self, mint: str, current_price_usd: float) -> Tuple[bool, str, float]:
        """
        Evaluates whether a position should be sold to take profit or prevent losses.
        Returns: (should_exit, reason_description, sell_pct)
        sell_pct: 50.0 for dual split scalp sale; 100.0 for full exit.
        """
        pos = self.update_price(mint, current_price_usd)
        if not pos or pos.status != "OPEN":
            return False, "", 0.0

        # Option: HOLD_INDEFINITE (Diamond Hands)
        # Never triggers automatic sell; position is held until Admin manually exits with /sell
        if pos.exit_strategy == "HOLD_INDEFINITE":
            return False, "", 0.0

        pnl = pos.pnl_pct

        # Strategy 0: DUAL_SPLIT (⚡ Scalp 50% for profit + 💎 Hold 50% as Forever Moonbag)
        if pos.exit_strategy in ("DUAL_SPLIT", "SPLIT", "MOONBAG_SPLIT"):
            if not pos.scalp_sold:
                # 1. Scalp Profit Target: Sells 50% of bag at +50% or +100% gain!
                # Default is +50.0% gain: recovers full original SOL capital + 25% profit, making moonbag 100% risk-free!
                if pnl >= 50.0 or current_price_usd >= pos.entry_price_usd * 1.50:
                    return (
                        True,
                        f"⚡ Scalp Target Hit (+{pnl:.1f}%). Sold 50% bag to bank profit & secure SOL. Remaining 50% permanently held as FREE MOONBAG! 💎🚀",
                        50.0,
                    )

                # 2. Capital Protection Stop-Loss before scalp is banked (-25%)
                if pnl <= -config.STOP_LOSS_PCT:
                    return True, f"Stop-Loss Cut ({pnl:.1f}% loss limit)", 100.0

                return False, "", 0.0
            else:
                # Scalp already banked! The remaining 50% is a diamond hands moonbag held indefinitely!
                return False, "", 0.0

        # Strategy 1: TRAILING_RUNNER (Long Runner Mode: Lets winners run to 5x-100x; locks in profit once peak pulls back 25%)
        if pos.exit_strategy == "TRAILING_RUNNER":
            # 1. Hard Stop-Loss Trigger (default -25% loss limit)
            if pnl <= -config.STOP_LOSS_PCT:
                return True, f"Stop-Loss Cut ({pnl:.1f}% loss limit)", 100.0

            # 2. Once in profit (peak >= entry * 1.50, i.e. +50% or higher), trail peak price!
            # With TRAILING_STOP_PCT = 25%, it gives plenty of breathing room for memecoin volatility
            if pos.peak_price_usd >= pos.entry_price_usd * 1.50:
                drawdown_from_peak = ((pos.peak_price_usd - current_price_usd) / pos.peak_price_usd) * 100.0
                if drawdown_from_peak >= config.TRAILING_STOP_PCT:
                    return True, f"Trailing Profit Lock Triggered (-{drawdown_from_peak:.1f}% from peak, locked in +{pnl:.1f}% gain)", 100.0

            return False, "", 0.0

        # Strategy 2: 2X_TAKE_PROFIT / AUTO_EXIT (Fixed 2x Mode: Sells once it doubles / reaches +100% gain)
        if pos.exit_strategy in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
            # 1. 2X Target Reached (Price doubled or pnl >= 100%)
            if pnl >= 100.0 or current_price_usd >= pos.entry_price_usd * 2.0:
                return True, f"2X Take-Profit Target Reached (+{pnl:.1f}% gain / 2x return)", 100.0

            # 2. Hard Stop-Loss Trigger (default -25%)
            if pnl <= -config.STOP_LOSS_PCT:
                return True, f"Stop-Loss Cut ({pnl:.1f}% loss limit)", 100.0

            return False, "", 0.0

        # Strategy 3: 50PCT_TAKE_PROFIT / SCALP_50 (Quick 50% Scalp: Sells once it reaches +50% gain)
        if pos.exit_strategy in ("50PCT_TAKE_PROFIT", "SCALP_50"):
            # 1. 50% Target Reached (Price >= entry * 1.50 or pnl >= 50%)
            if pnl >= 50.0 or current_price_usd >= pos.entry_price_usd * 1.50:
                return True, f"50% Take-Profit Target Reached (+{pnl:.1f}% gain)", 100.0

            # 2. Hard Stop-Loss Trigger (default -25%)
            if pnl <= -config.STOP_LOSS_PCT:
                return True, f"Stop-Loss Cut ({pnl:.1f}% loss limit)", 100.0

            return False, "", 0.0

        return False, "", 0.0

    def record_partial_exit(
        self,
        mint: str,
        sold_pct: float,
        exit_price_usd: float,
        exit_reason: str,
        exit_tx_hash: str = "",
    ) -> Optional[Position]:
        """
        Records a partial exit (e.g. 50% scalp sale in DUAL_SPLIT mode).
        Locks in realized gains for the scalp portion, updates remaining position size,
        and converts the remaining bag into a permanent HOLD_INDEFINITE moonbag!
        """
        pos = self.positions.get(mint)
        if not pos or pos.status != "OPEN":
            return None

        pos.scalp_sold = True
        pos.moonbag_retained = True
        pos.exit_reason = exit_reason
        pos.exit_tx_hash = exit_tx_hash
        pos.amount_sol = round(pos.amount_sol * (1.0 - sold_pct / 100.0), 4)
        pos.exit_strategy = "HOLD_INDEFINITE"
        self._save()
        logger.info(
            f"💎 DUAL_SPLIT executed for {pos.symbol}: Sold {sold_pct:.0f}% at ${exit_price_usd:.6f}. "
            f"Remaining {100.0 - sold_pct:.0f}% permanently converted to FREE MOONBAG held indefinitely!"
        )
        return pos

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
            if p.exit_strategy == "HOLD_INDEFINITE":
                strat_badge = "💎 HOLD"
            elif p.exit_strategy == "TRAILING_RUNNER":
                strat_badge = f"🚀 RUNNER (-{config.TRAILING_STOP_PCT:.0f}% peak trail)"
            elif p.exit_strategy in ("50PCT_TAKE_PROFIT", "SCALP_50"):
                strat_badge = "⚡ 50% SCALP (+50%)"
            else:
                strat_badge = "🎯 FIXED 2X (+100%)"
            safe_name = html.escape(p.name)
            safe_sym = html.escape(p.symbol)
            lines.append(
                f"🪙 <b>{safe_name}</b> (<code>${safe_sym}</code>)\n"
                f"  • Strategy: <b>{strat_badge}</b> | PnL: <b>{pnl_badge}</b>\n"
                f"  • Entry: <code>${p.entry_price_usd:.6f}</code> | Current: <code>${p.current_price_usd:.6f}</code>\n"
                f"  • Size: <code>{p.amount_sol:.2f} SOL</code> | Chart: <a href='{dex_url}'>DexScreener</a>\n"
            )
        lines.append("───────────────────────────────")
        lines.append("<i>Controls: /sell &lt;CA&gt; [pct] to exit anytime | /exitmode to change default strategy</i>")
        return "\n".join(lines)


# Global singleton instance
position_manager = PositionManager()
