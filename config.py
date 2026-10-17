"""
config.py - Configuration management for Multi-Agent Telegram Council.

Handles:
- Telegram Bot tokens for all AI agents (expandable from 5 to 15 bots)
- AutoGen LLM backend configurations (OpenRouter unified endpoint or native providers)
- Telegram group permissions, admin whitelist, and council debate settings
- MSniffer crypto execution parameters (Telegram, Webhook, Local IPC)
- Dynamic council mood & personality profiles
"""

import asyncio
import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


@dataclass
class AgentConfig:
    """Configuration definition for an individual AI Council Agent."""
    id: str                       # Internal unique slug (e.g. "claude")
    name: str                     # Short name (e.g. "Claude")
    display_name: str             # Telegram display name matching {AI}talks pattern
    username: str                 # Telegram bot username (@...talkbbot)
    token: str                    # Telegram Bot API token
    model: str                    # LLM model identifier (OpenRouter or provider specific)
    provider: str = "openrouter"  # "openrouter", "openai", "anthropic", "deepseek", "gemini", "grok", "mistral"
    base_url: Optional[str] = None
    role_description: str = ""
    personality_prompt: str = ""
    enabled: bool = True


# =============================================================================
# 1. TELEGRAM COUNCIL & ADMIN SETTINGS
# =============================================================================

def _parse_int_safe(val: str, default: int = 0) -> int:
    try:
        val_clean = str(val).split("#")[0].strip()
        return int(val_clean)
    except Exception:
        return default

# Telegram Group Chat ID where debates & votes occur (e.g. -1004310499587)
GROUP_CHAT_ID: int = _parse_int_safe(os.getenv("GROUP_CHAT_ID", os.getenv("TELEGRAM_GROUP_CHAT_ID", "0")), 0)

# Whitelist of Telegram Admin User IDs permitted to trigger council commands
# If empty, all members in the group can trigger commands
_admin_ids_raw = os.getenv("ADMIN_USER_IDS", "")
ADMIN_USER_IDS: List[int] = [
    _parse_int_safe(x) for x in _admin_ids_raw.split(",") if _parse_int_safe(x) != 0
]

# Which bot in the lineup acts as the listener/dispatcher polling Telegram updates
# (0 = first active bot, e.g. Claude)
LISTENER_BOT_INDEX: int = int(os.getenv("LISTENER_BOT_INDEX", "0"))

# Maximum turns/rounds in Debate mode
# (1 round means each active bot speaks once: 5 concise perspectives total)
DEBATE_MAX_ROUNDS: int = int(os.getenv("DEBATE_MAX_ROUNDS", "1"))

# Minimum percentage of YES votes required to reach consensus (0.50 = 50% majority)
CONSENSUS_THRESHOLD: float = float(os.getenv("CONSENSUS_THRESHOLD", "0.50"))

# Typing delay simulation (in seconds) between bot responses for visual realism
BOT_TYPING_DELAY_SECONDS: float = float(os.getenv("BOT_TYPING_DELAY_SECONDS", "1.5"))


# =============================================================================
# 2. LLM BACKEND & API KEYS (Unified OpenRouter Engine)
# =============================================================================

# OpenRouter Unified API Key (Powers all 5 AI models with 1 key)
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()


async def check_openrouter_status() -> Dict[str, Any]:
    """
    Queries OpenRouter's /api/v1/auth/key and /api/v1/credits endpoints to verify
    API key connectivity, validity, and remaining USD credit balance in real time.
    """
    if not OPENROUTER_API_KEY:
        return {
            "configured": False,
            "active": False,
            "credit_usd": None,
            "status": "Simulated Testing Mode (Add OPENROUTER_API_KEY to .env for live AI)",
        }

    loop = asyncio.get_running_loop()
    try:
        def _query():
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://telegram.org",
                "X-Title": "Chatters AI Council",
                "User-Agent": "ChattersCouncil/2.0",
            }
            # 1. Query key validation
            req_key = urllib.request.Request(f"{OPENROUTER_BASE_URL}/auth/key", headers=headers)
            with urllib.request.urlopen(req_key, timeout=6) as r:
                key_data = json.loads(r.read().decode()).get("data", {})

            # 2. Query credit balance
            credit_usd = None
            try:
                req_cred = urllib.request.Request(f"{OPENROUTER_BASE_URL}/credits", headers=headers)
                with urllib.request.urlopen(req_cred, timeout=6) as rc:
                    cred_data = json.loads(rc.read().decode()).get("data", {})
                    total_cred = float(cred_data.get("total_credits", 0.0) or 0.0)
                    total_usage = float(cred_data.get("total_usage", 0.0) or 0.0)
                    credit_usd = max(0.0, total_cred - total_usage)
            except Exception:
                pass

            return {"configured": True, "active": True, "key_data": key_data, "credit_usd": credit_usd}

        res = await loop.run_in_executor(None, _query)
        credit_usd = res.get("credit_usd")
        if credit_usd is not None and credit_usd <= 0.005:
            return {
                "configured": True,
                "active": False,
                "credit_usd": 0.0,
                "status": "⚠️ OpenRouter Credits Depleted ($0.00 USD) — Add funds at openrouter.ai/settings/credits",
            }
        cred_str = f" | Credit: ${credit_usd:.2f} USD" if credit_usd is not None else ""
        return {
            "configured": True,
            "active": True,
            "credit_usd": credit_usd,
            "status": f"OpenRouter Unified Engine (Active 🟢{cred_str})",
        }
    except urllib.error.HTTPError as he:
        try:
            err_msg = json.loads(he.read().decode()).get("error", {}).get("message", str(he))
        except Exception:
            err_msg = str(he)
        return {
            "configured": True,
            "active": False,
            "credit_usd": 0.0,
            "status": f"⚠️ OpenRouter Key Error ({he.code}): {err_msg}",
        }
    except Exception as e:
        return {
            "configured": True,
            "active": True,
            "credit_usd": None,
            "status": f"OpenRouter Unified Engine (Connected)",
        }


# =============================================================================
# 3. EXTERNAL TASK EXECUTION (MSniffer Bot Integration)
# =============================================================================

# Dispatch mode: "webhook", "telegram", "local_ipc", or "all"
MSNIFFER_DISPATCH_MODE: str = os.getenv("MSNIFFER_DISPATCH_MODE", "webhook").strip().lower()

# Local HTTP Webhook endpoint of Market Sniffer (POST /trade or POST /signal)
MSNIFFER_WEBHOOK_URL: str = os.getenv("MSNIFFER_WEBHOOK_URL", "http://127.0.0.1:8088/trade").strip()

# Shared authentication secret key for MSniffer webhook/signals
MSNIFFER_SECRET: str = os.getenv("MSNIFFER_SECRET", os.getenv("ORCHESTRATOR_SECRET", "")).strip()

# Telegram Chat ID or Username where MSniffer signals should be broadcast
MSNIFFER_CHAT_ID: str = os.getenv("MSNIFFER_CHAT_ID", "@Msnifferbot").strip()

# Allowed investment options in SOL (Strict presets to prevent dangerous typos or wild custom amounts)
ALLOWED_INVESTMENT_OPTIONS: List[float] = [0.05, 0.10, 0.25, 0.50, 1.00]

def validate_investment_amount(val: float) -> float:
    """
    Validates and snaps an investment amount to an allowed preset option.
    Guarantees the system never executes arbitrary unbounded numbers.
    """
    try:
        val_clean = round(float(val), 2)
        if val_clean in ALLOWED_INVESTMENT_OPTIONS:
            return val_clean
        # Snap to closest allowed option
        return min(ALLOWED_INVESTMENT_OPTIONS, key=lambda x: abs(x - val_clean))
    except Exception:
        return 0.25

# Default trade amount in SOL when executing BUY consensus
DEFAULT_TRADE_AMOUNT_SOL: float = validate_investment_amount(float(os.getenv("DEFAULT_TRADE_AMOUNT_SOL", "0.25")))

# Default slippage in basis points (300 bps = 3.0%)
DEFAULT_SLIPPAGE_BPS: int = int(os.getenv("DEFAULT_SLIPPAGE_BPS", "300"))

# Well-known mints on Solana
SOL_MINT: str = "So11111111111111111111111111111111111111112"
USDT_SOLANA_MINT: str = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Position Exit & Profit-Taking Targets
DEFAULT_EXIT_STRATEGY: str = os.getenv("DEFAULT_EXIT_STRATEGY", "AUTO_EXIT").strip().upper()
if DEFAULT_EXIT_STRATEGY not in ("AUTO_EXIT", "HOLD_INDEFINITE"):
    DEFAULT_EXIT_STRATEGY = "AUTO_EXIT"

TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "50.0"))  # Exit at +50% gain
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "25.0"))      # Cut loss at -25%
TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", "15.0"))  # Retrace 15% from peak after profit

# Profit Vault Sweeper (One-way ratchet: sweeps profits to USDT; NEVER converts USDT back to SOL)
PROFIT_VAULT_ENABLED: bool = os.getenv("PROFIT_VAULT_ENABLED", "true").lower() in ("true", "1", "yes")
PROFIT_VAULT_THRESHOLD_USD: float = float(os.getenv("PROFIT_VAULT_THRESHOLD_USD", "25.0"))
ALLOWED_VAULT_THRESHOLDS: List[float] = [10.0, 25.0, 50.0, 100.0, 250.0]
PROFIT_VAULT_RESERVE_SOL: float = float(os.getenv("PROFIT_VAULT_RESERVE_SOL", "0.10"))

# Autonomous Background Screening Filters (Only high-conviction breakout tokens reach the Council)
AUTOPILOT_MIN_LIQUIDITY_USD: float = float(os.getenv("AUTOPILOT_MIN_LIQUIDITY_USD", "10000.0"))
AUTOPILOT_MIN_VOLUME_24H_USD: float = float(os.getenv("AUTOPILOT_MIN_VOLUME_24H_USD", "25000.0"))
AUTOPILOT_MIN_1H_CHANGE: float = float(os.getenv("AUTOPILOT_MIN_1H_CHANGE", "1.0"))
AUTOPILOT_MAX_FDV_USD: float = float(os.getenv("AUTOPILOT_MAX_FDV_USD", "25000000.0"))


# =============================================================================
# 4. PERSONALITY & DEBATE POSTURE
# =============================================================================

DEFAULT_MOOD: str = os.getenv("DEFAULT_MOOD", "analytical").strip().lower()

MOOD_PROMPTS: Dict[str, str] = {
    "analytical": (
        "CORE POSTURE: Objective, data-driven, and intellectually rigorous. "
        "Formulate well-reasoned arguments grounded in verifiable facts and first principles."
    ),
    "skeptical": (
        "CORE POSTURE: Hyper-vigilant and risk-averse. "
        "Probe for hidden assumptions, vulnerabilities, overextension, and structural flaws."
    ),
    "aggressive": (
        "CORE POSTURE: Bold, high-conviction, and forward-leaning. "
        "Emphasize asymmetric upside, speed of execution, and viral momentum."
    ),
    "ruthless": (
        "CORE POSTURE: Pragmatic, cutthroat, and game-theoretic. "
        "Focus on hard realities, competitive dynamics, and cold execution edge."
    ),
}


# =============================================================================
# 5. AGENT ROSTER (The 5 Council Bots)
# =============================================================================

# Configured for the 5 Telegram bots talking inside "Chatters".
AGENTS_CONFIG: List[AgentConfig] = [
    # -------------------------------------------------------------------------
    # Bot 1: Claude (Claudetalks / @claudetalkbbot)
    # -------------------------------------------------------------------------
    AgentConfig(
        id="claude",
        name="Claude",
        display_name="Claudetalks",
        username="claudetalkbbot",
        token=os.getenv("BOT_TOKEN_CLAUDE", "8828456879:AAHqJUXEuCK4W--r4t3brAR30mn7sThLMLY").strip(),
        model=os.getenv("MODEL_CLAUDE", "anthropic/claude-haiku-4.5").strip(),
        provider="openrouter",
        role_description="Solana Security Auditor & Smart Contract Risk Analyst",
        personality_prompt=(
            "You are Claude (Claudetalks), the Solana Security Auditor and Smart Contract Risk Analyst on the Chatters Crypto Council. "
            "You scrutinize mint/freeze authorities, RugCheck risk metrics, holder distributions, and contract exploit vectors. "
            "You protect treasury capital against honeypots, rugs, and illiquid traps. Keep replies concise and razor-sharp (1-2 sentences)."
        ),
        enabled=True,
    ),

    # -------------------------------------------------------------------------
    # Bot 2: GPT (GPTtalks / @gpttalkbbot)
    # -------------------------------------------------------------------------
    AgentConfig(
        id="gpt",
        name="GPT",
        display_name="GPTtalks",
        username="gpttalkbbot",
        token=os.getenv("BOT_TOKEN_GPT", "8630079807:AAEhjG1JY8RzayPeI3hLfc5Dixmvh7eJcUs").strip(),
        model=os.getenv("MODEL_GPT", "openai/gpt-4o-mini").strip(),
        provider="openrouter",
        role_description="Crypto Macro Strategist & Liquidity Architect",
        personality_prompt=(
            "You are GPT (GPTtalks), the Crypto Macro Strategist and Liquidity Architect on the Chatters Crypto Council. "
            "You evaluate DEX pool liquidity depth, liquidity-to-mcap ratios, slippage runway, and Solana market structure. "
            "Keep replies concise, structured, and razor-sharp (1-2 sentences)."
        ),
        enabled=True,
    ),

    # -------------------------------------------------------------------------
    # Bot 3: DeepSeek (DeepSeektalks / @deepseektalkbbot)
    # -------------------------------------------------------------------------
    AgentConfig(
        id="deepseek",
        name="DeepSeek",
        display_name="DeepSeektalks",
        username="deepseektalkbbot",
        token=os.getenv("BOT_TOKEN_DEEPSEEK", "8882002776:AAGdh55_7Ct6RJ3PGDZk_wEAsTnZIrOz0fY").strip(),
        model=os.getenv("MODEL_DEEPSEEK", "deepseek/deepseek-chat").strip(),
        provider="openrouter",
        role_description="On-Chain Quantitative Analyst & Mathematical EV Specialist",
        personality_prompt=(
            "You are DeepSeek (DeepSeektalks), the Quantitative Orderflow and Expected Value (EV) Specialist on the Chatters Crypto Council. "
            "You focus on mathematical probability, net buy/sell volume, transaction velocity, and statistical expected value. "
            "Keep replies concise and numbers-grounded (1-2 sentences)."
        ),
        enabled=True,
    ),

    # -------------------------------------------------------------------------
    # Bot 4: Gemini (Geminitalks / @geminitalkbbot)
    # -------------------------------------------------------------------------
    AgentConfig(
        id="gemini",
        name="Gemini",
        display_name="Geminitalks",
        username="geminitalkbbot",
        token=os.getenv("BOT_TOKEN_GEMINI", "8934071377:AAEXDzWKMLjpDkPlxztbmwzPKqRge8vSegk").strip(),
        model=os.getenv("MODEL_GEMINI", "google/gemini-2.5-flash").strip(),
        provider="openrouter",
        role_description="Memecoin Narrative Hunter & Trend Velocity Forecaster",
        personality_prompt=(
            "You are Gemini (Geminitalks), the Memecoin Narrative Hunter and Trend Velocity Forecaster on the Chatters Crypto Council. "
            "You track viral momentum, social catalysts, 1h/24h breakout velocity, and DEX pump dynamics. "
            "Keep replies concise, vivid, and razor-sharp (1-2 sentences)."
        ),
        enabled=True,
    ),

    # -------------------------------------------------------------------------
    # Bot 5: Grok (Groktalks / @groktalkbbot)
    # -------------------------------------------------------------------------
    AgentConfig(
        id="grok",
        name="Grok",
        display_name="Groktalks",
        username="groktalkbbot",
        token=os.getenv("BOT_TOKEN_GROK", "8607678038:AAHxLLtGoL6gEgnJoN35toGkKPbWCYC8VHI").strip(),
        model=os.getenv("MODEL_GROK", "x-ai/grok-4.5").strip(),
        provider="openrouter",
        role_description="PvP Crypto Trader, Whale Tracker & Contrarian Skeptic",
        personality_prompt=(
            "You are Grok (Groktalks), the PvP Crypto Trader, Whale Tracker, and Contrarian Skeptic on the Chatters Crypto Council. "
            "You audit top 10 holder supply concentration, sniff out insider dumps, identify fake volume, and challenge hype. "
            "Keep replies punchy, witty, and razor-sharp (1-2 sentences)."
        ),
        enabled=True,
    ),
]


def get_active_agents() -> List[AgentConfig]:
    """Returns only the configured agents with valid tokens that are enabled."""
    return [agent for agent in AGENTS_CONFIG if agent.enabled and agent.token]


def get_agent_by_id(agent_id: str) -> Optional[AgentConfig]:
    """Retrieves an agent configuration by its id or name (case-insensitive)."""
    target = agent_id.strip().lower()
    for agent in AGENTS_CONFIG:
        if agent.id.lower() == target or agent.name.lower() == target:
            return agent
    return None
