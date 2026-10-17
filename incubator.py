"""
incubator.py - Token Incubator & 2-Hour Seasoning Recheck Engine.

Solana tokens often launch with low initial liquidity, high dev concentration, or volatile noise.
The Incubator catches promising tokens early, registers them in a persistent queue,
and waits ~2 hours (configurable) for them to season:
1. Filters out immediate rug pulls and abandoned dev dumps during the first 2 hours.
2. Identifies tokens where liquidity migrated/deepened, holder distribution stabilized, and volume held.
3. Automatically triggers an internal council re-evaluation after 2 hours to buy mature breakouts.
"""

import html
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger("multi_agent_council.incubator")

INCUBATOR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "incubator.json")


@dataclass
class IncubatedToken:
    """A Solana token caught early and tracked for post-launch seasoning & maturity."""
    mint: str
    symbol: str
    name: str
    first_seen_timestamp: float
    initial_price_usd: float = 0.0
    initial_liquidity_usd: float = 0.0
    initial_fdv_usd: float = 0.0
    last_checked_timestamp: float = 0.0
    recheck_count: int = 0
    status: str = "INCUBATING"  # "INCUBATING", "BOUGHT", "DEAD", "EXPIRED"
    notes: str = ""

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.first_seen_timestamp)

    @property
    def age_hours(self) -> float:
        return self.age_seconds / 3600.0

    @property
    def is_seasoned(self) -> bool:
        """True if token has matured past the minimum seasoning window (default 2 hours)."""
        return self.age_seconds >= config.INCUBATOR_MIN_AGE_SECONDS

    @property
    def is_due_for_recheck(self) -> bool:
        """True if token is seasoned and ready for its next internal check."""
        if self.status != "INCUBATING":
            return False
        if not self.is_seasoned:
            return False
        # Recheck schedule: at 2 hours, then every INCUBATOR_RECHECK_INTERVAL_SECONDS thereafter
        time_since_last = time.time() - self.last_checked_timestamp
        return time_since_last >= config.INCUBATOR_RECHECK_INTERVAL_SECONDS


class TokenIncubator:
    """Manages the persistent registry of incubating Solana tokens."""

    def __init__(self, filepath: str = INCUBATOR_FILE):
        self.filepath = filepath
        self.tokens: Dict[str, IncubatedToken] = {}
        self._load()

    def _load(self):
        """Loads incubating tokens from local JSON storage."""
        if not os.path.exists(self.filepath):
            return
        try:
            valid_keys = {f.name for f in fields(IncubatedToken)}
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    filtered = {k: v for k, v in item.items() if k in valid_keys}
                    tok = IncubatedToken(**filtered)
                    self.tokens[tok.mint] = tok
            logger.info(f"🧪 Loaded {len(self.tokens)} incubated tokens from {self.filepath}")
        except Exception as e:
            logger.error(f"Error loading incubator state from {self.filepath}: {e}")

    def _save(self):
        """Persists incubating tokens atomically to disk."""
        try:
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
            data = [asdict(t) for t in self.tokens.values()]
            tmp_path = self.filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.filepath)
        except Exception as e:
            logger.error(f"Error saving incubator state to {self.filepath}: {e}")

    def add_candidate(
        self,
        mint: str,
        symbol: str = "",
        name: str = "",
        price_usd: float = 0.0,
        liquidity_usd: float = 0.0,
        fdv_usd: float = 0.0,
        notes: str = "",
    ) -> bool:
        """
        Adds a newly discovered candidate token to the 2-hour incubation queue.
        If already tracked, updates metadata without resetting first_seen_timestamp.
        """
        clean_mint = (mint or "").strip()
        if not clean_mint or len(clean_mint) < 32:
            return False

        now = time.time()
        existing = self.tokens.get(clean_mint)
        if existing:
            # If already bought or dead, don't resurrect
            if existing.status in ("BOUGHT", "DEAD"):
                return False
            # Update latest price/liquidity snapshot
            if price_usd > 0:
                existing.initial_price_usd = existing.initial_price_usd or price_usd
            if liquidity_usd > 0:
                existing.initial_liquidity_usd = existing.initial_liquidity_usd or liquidity_usd
            if fdv_usd > 0:
                existing.initial_fdv_usd = existing.initial_fdv_usd or fdv_usd
            if notes and not existing.notes:
                existing.notes = notes
            self._save()
            return False

        # Enforce capacity limit: prune oldest expired/dead tokens if over limit
        self.cleanup_expired()
        if len(self.tokens) >= config.INCUBATOR_MAX_TRACKED:
            # Drop oldest inactive token
            inactives = [t for t in self.tokens.values() if t.status in ("DEAD", "EXPIRED")]
            if inactives:
                inactives.sort(key=lambda x: x.first_seen_timestamp)
                del self.tokens[inactives[0].mint]
            else:
                # If all incubating, drop oldest
                sorted_tokens = sorted(self.tokens.values(), key=lambda x: x.first_seen_timestamp)
                del self.tokens[sorted_tokens[0].mint]

        new_tok = IncubatedToken(
            mint=clean_mint,
            symbol=(symbol or "TOKEN")[:10].upper(),
            name=(name or "Solana Token")[:40],
            first_seen_timestamp=now,
            initial_price_usd=price_usd,
            initial_liquidity_usd=liquidity_usd,
            initial_fdv_usd=fdv_usd,
            last_checked_timestamp=now,
            recheck_count=0,
            status="INCUBATING",
            notes=notes or "Caught early at launch",
        )
        self.tokens[clean_mint] = new_tok
        self._save()
        logger.info(
            f"🧪 [Incubator Registered] {new_tok.name} (${new_tok.symbol}) entered 2-hour seasoning queue "
            f"(Liq: ${liquidity_usd:,.0f}, FDV: ${fdv_usd:,.0f})"
        )
        return True

    def get_due_for_recheck(self) -> List[IncubatedToken]:
        """Returns list of incubated tokens that have seasoned (>=2h old) and are ready for internal review."""
        now = time.time()
        due: List[IncubatedToken] = []
        for t in self.tokens.values():
            if t.status == "INCUBATING":
                # Check for expiration (older than max age, default 24h)
                if t.age_seconds >= config.INCUBATOR_MAX_AGE_SECONDS:
                    t.status = "EXPIRED"
                    continue

                if t.is_due_for_recheck:
                    due.append(t)

        if due:
            # Sort by age so oldest seasoned tokens get evaluated first
            due.sort(key=lambda x: x.first_seen_timestamp)
            self._save()

        return due

    def mark_bought(self, mint: str):
        """Marks an incubated token as successfully bought on consensus."""
        tok = self.tokens.get(mint)
        if tok:
            tok.status = "BOUGHT"
            tok.last_checked_timestamp = time.time()
            self._save()
            logger.info(f"🏆 [Incubator] {tok.symbol} marked as BOUGHT after {tok.age_hours:.1f}h seasoning.")

    def mark_dead(self, mint: str, reason: str = "Rugged / Liquidity Pulled"):
        """Marks a token as dead so we cease future checks."""
        tok = self.tokens.get(mint)
        if tok:
            tok.status = "DEAD"
            tok.notes = reason
            tok.last_checked_timestamp = time.time()
            self._save()
            logger.info(f"💀 [Incubator] {tok.symbol} marked DEAD: {reason}")

    def record_recheck(self, mint: str, notes: str = ""):
        """Records an evaluation attempt for a seasoned token."""
        tok = self.tokens.get(mint)
        if tok:
            tok.recheck_count += 1
            tok.last_checked_timestamp = time.time()
            if notes:
                tok.notes = notes
            self._save()

    def cleanup_expired(self):
        """Prunes tokens that have been dead or expired for over 48 hours."""
        now = time.time()
        to_delete = []
        for mint, t in self.tokens.items():
            if t.status in ("DEAD", "EXPIRED", "BOUGHT"):
                if now - t.last_checked_timestamp > 172800:  # 48 hours retention post completion
                    to_delete.append(mint)
        for mint in to_delete:
            del self.tokens[mint]
        if to_delete:
            self._save()

    def get_status_text(self) -> str:
        """Formats comprehensive live Incubator status for Telegram."""
        incubating = [t for t in self.tokens.values() if t.status == "INCUBATING"]
        seasoned = [t for t in incubating if t.is_seasoned]
        bought = [t for t in self.tokens.values() if t.status == "BOUGHT"]
        dead = [t for t in self.tokens.values() if t.status == "DEAD"]

        min_hours = config.INCUBATOR_MIN_AGE_SECONDS / 3600.0

        lines = [
            "🧪 <b>CHATTERS TOKEN INCUBATOR & SEASONING QUEUE</b>",
            "───────────────────────────────",
            f"🎯 <b>Seasoning Window:</b> <code>{min_hours:.1f} Hours Post-Launch</code>",
            f"📊 <b>Active Incubating:</b> <code>{len(incubating)} tokens</code>",
            f"⏰ <b>Seasoned &amp; Monitoring (≥{min_hours:.0f}h):</b> <code>{len(seasoned)} tokens</code>",
            f"🏆 <b>Seasoned Breakouts Bought:</b> <code>{len(bought)} tokens</code>",
            f"💀 <b>Rugged / Dead Filtered:</b> <code>{len(dead)} tokens</code>",
            "───────────────────────────────",
        ]

        if not incubating:
            lines.append("<i>Incubator is scanning. Early tokens will appear here to season for 2 hours before internal recheck.</i>")
        else:
            # Sort by proximity to / past 2-hour threshold
            sorted_inc = sorted(incubating, key=lambda x: x.first_seen_timestamp)
            lines.append("<b>Top Tokens in Seasoning Queue:</b>")
            for t in sorted_inc[:6]:
                age_h = t.age_hours
                if age_h < min_hours:
                    time_left = max(0, int(config.INCUBATOR_MIN_AGE_SECONDS - t.age_seconds))
                    mins_left = time_left // 60
                    status_badge = f"⏳ Seasons in {mins_left}m"
                else:
                    status_badge = f"🟢 Seasoned ({age_h:.1f}h) | {t.recheck_count} checks"

                dex_url = f"https://dexscreener.com/solana/{t.mint}"
                safe_name = html.escape(t.name)
                safe_sym = html.escape(t.symbol)
                lines.append(
                    f"• <b>{safe_name}</b> (<code>${safe_sym}</code>) — {status_badge}\n"
                    f"  Init Liq: <code>${t.initial_liquidity_usd:,.0f}</code> | "
                    f"<a href='{dex_url}'>Chart</a> | <code>{t.mint[:4]}...{t.mint[-4:]}</code>"
                )

        lines.append("───────────────────────────────")
        lines.append(
            f"<i>Tokens caught early incubate for {min_hours:.0f} hours. Once seasoned, the AI Council automatically "
            f"checks if liquidity deepened and momentum held before voting to invest!</i>"
        )
        return "\n".join(lines)


# Global singleton instance
token_incubator = TokenIncubator()
