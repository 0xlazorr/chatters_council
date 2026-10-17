"""
context_manager.py - Dynamic Voting Instructions & Context Engine for Chatters AI Council.

Allows the admin to customize and edit voting instructions for the 5 council bots:
- Saved persistently to data/council_context.json across bot restarts
- Viewable and editable at any time via /context <instructions>
- Includes 1-click presets (Default, Conservative, Aggressive, Memecoin)
- Dynamically injected into all AI voting prompts (/exe, /vot, /dve, and /auto)
"""

import json
import logging
import os
import time
from typing import Dict, Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("multi_agent_council.context")

CONTEXT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "council_context.json")

PRESETS: Dict[str, str] = {
    "DEFAULT": "Balanced evaluation. Vote YES on secure tokens with verified liquidity, low rug risk, and positive volume.",
    "RUNNER": "Breakout runner hunter. Mandatory: mint and freeze authorities must be REVOKED. Allow early supply concentration up to 85% if buy volume is accelerating and liquidity > $6,000. Ride early runners like TRUMP while strictly rejecting honeypots.",
    "CONSERVATIVE": "Capital preservation. Strict NO on any token with rug score > 150, top 10 holders > 25%, or liquidity < $30,000. Prioritize safety over hype.",
    "AGGRESSIVE": "Momentum scalping. Allow early tokens with liquidity down to $5,000 if 5m/1h volume is surging and buy ratio > 65%. Accept volatility for high breakout velocity.",
    "MEMECOIN": "Viral narrative hunter. Prioritize breakout velocity and community hype. Ensure mint/freeze authority is revoked, but allow high price swings.",
}


class ContextManager:
    """Manages dynamic user-customized voting instructions for the 5 AI council bots."""

    def __init__(self, filepath: str = CONTEXT_FILE):
        self.filepath = filepath
        self.active_context = PRESETS["DEFAULT"]
        self.preset_name = "DEFAULT"
        self.updated_at = time.time()
        self.updated_by = "System"
        self._load()

    def _load(self):
        """Loads saved context from disk if exists."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.active_context = data.get("active_context", PRESETS["DEFAULT"])
                    self.preset_name = data.get("preset_name", "CUSTOM")
                    self.updated_at = data.get("updated_at", time.time())
                    self.updated_by = data.get("updated_by", "Admin")
                    logger.info(f"Loaded Council Context: [{self.preset_name}] {self.active_context[:50]}...")
            except Exception as e:
                logger.error(f"Error loading council context file: {e}")

    def _save(self):
        """Persists current context to disk."""
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "active_context": self.active_context,
                    "preset_name": self.preset_name,
                    "updated_at": self.updated_at,
                    "updated_by": self.updated_by,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving council context file: {e}")

    def get_active_context(self) -> str:
        """Returns the active voting instruction string."""
        return self.active_context

    def get_preset_name(self) -> str:
        """Returns the name of the active preset or 'CUSTOM'."""
        return self.preset_name

    def set_context(self, instruction: str, preset_name: str = "CUSTOM", updated_by: str = "Admin") -> Tuple[bool, str]:
        """Updates the active voting instruction and persists to disk."""
        cleaned = instruction.strip()
        if not cleaned:
            return False, "⚠️ Context instruction cannot be empty."

        self.active_context = cleaned
        self.preset_name = preset_name.upper()
        self.updated_at = time.time()
        self.updated_by = updated_by
        self._save()
        logger.info(f"Updated Council Context to [{self.preset_name}]: {cleaned}")
        return True, f"✅ <b>Council Voting Context Updated [{self.preset_name}]</b>\n\n<i>\"{cleaned}\"</i>"

    def apply_preset(self, preset_key: str, updated_by: str = "Admin") -> Tuple[bool, str]:
        """Applies a built-in strategy preset."""
        key = preset_key.upper().strip()
        if key not in PRESETS:
            return False, f"⚠️ Unknown preset '{key}'. Available: {', '.join(PRESETS.keys())}"

        return self.set_context(instruction=PRESETS[key], preset_name=key, updated_by=updated_by)

    def reset_to_default(self, updated_by: str = "Admin") -> str:
        """Resets the voting instruction back to DEFAULT."""
        _, msg = self.apply_preset("DEFAULT", updated_by=updated_by)
        return msg

    def format_status_dashboard(self) -> str:
        """Formats the /context command UI dashboard."""
        elapsed = int(time.time() - self.updated_at)
        if elapsed < 60:
            time_ago = f"{elapsed}s ago"
        elif elapsed < 3600:
            time_ago = f"{elapsed // 60}m ago"
        elif elapsed < 86400:
            time_ago = f"{elapsed // 3600}h {(elapsed % 3600) // 60}m ago"
        else:
            time_ago = f"{elapsed // 86400}d ago"

        badge = f"🏷️ <b>Preset:</b> <code>{self.preset_name}</code>"
        return (
            f"🧠 <b>AI COUNCIL VOTING CONTEXT & DIRECTIVES</b>\n"
            f"───────────────────────────────\n"
            f"{badge}\n"
            f"🕒 <b>Last Updated:</b> <code>{time_ago}</code> (by {self.updated_by})\n\n"
            f"📋 <b>Active Voting Instructions:</b>\n"
            f"<i>\"{self.active_context}\"</i>\n"
            f"───────────────────────────────\n"
            f"<b>How to Customize:</b>\n"
            f"• Type <code>/context &lt;instructions&gt;</code> to set any custom rules.\n"
            f"  <i>Example: <code>/context Market is dumping. Strict NO if liquidity &lt; $50k or top 10 &gt; 20%.</code></i>\n"
            f"• Tap a preset button below for 1-click strategy switching:\n"
            f"  - <b>Runner:</b> Catch 5x-50x breakouts like TRUMP, allow early distribution if authorities revoked\n"
            f"  - <b>Conservative:</b> Strict capital defense, low rug score, low concentration\n"
            f"  - <b>Aggressive:</b> Scalp early momentum, accept higher risk for speed\n"
            f"  - <b>Memecoin:</b> Focus on viral hype, volume velocity, and breakout momentum\n"
            f"  - <b>Default:</b> Balanced institutional criteria\n"
            f"• <code>/context reset</code> - Revert back to Default"
        )

    def build_keyboard(self) -> InlineKeyboardMarkup:
        """Builds interactive inline buttons for switching context presets."""
        curr = self.preset_name
        def mark(name: str, label: str) -> str:
            return f"{label} ✅" if curr == name else label

        row1 = [
            InlineKeyboardButton(mark("RUNNER", "🏃 Runner (TRUMP)"), callback_data="ctx:runner"),
            InlineKeyboardButton(mark("CONSERVATIVE", "🛡️ Conservative"), callback_data="ctx:conservative"),
        ]
        row2 = [
            InlineKeyboardButton(mark("AGGRESSIVE", "⚡ Aggressive"), callback_data="ctx:aggressive"),
            InlineKeyboardButton(mark("MEMECOIN", "🚀 Memecoin"), callback_data="ctx:memecoin"),
        ]
        row3 = [
            InlineKeyboardButton(mark("DEFAULT", "🔄 Default"), callback_data="ctx:default"),
            InlineKeyboardButton("🔄 Refresh Context", callback_data="ctx:refresh"),
        ]
        return InlineKeyboardMarkup([row1, row2, row3])


# Global singleton instance
context_manager = ContextManager()
