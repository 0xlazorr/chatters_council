"""
config.py - Configuration management for Multi-Agent Telegram Council.

Handles:
- Telegram Bot tokens for all AI agents (expandable from 5 to 15 bots)
- AutoGen LLM backend configurations (OpenRouter unified endpoint or native providers)
- Telegram group permissions, admin whitelist, and council debate settings
- MSniffer crypto execution parameters (Telegram, Webhook, Local IPC)
- Dynamic council mood & personality profiles
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
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
# (e.g., 2 rounds means each active bot speaks twice during the debate)
DEBATE_MAX_ROUNDS: int = int(os.getenv("DEBATE_MAX_ROUNDS", "2"))

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
TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "50.0"))  # Exit at +50% gain
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "25.0"))      # Cut loss at -25%
TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", "15.0"))  # Retrace 15% from peak after profit

# Profit Vault Sweeper (One-way ratchet: sweeps $25 profits to USDT; NEVER converts USDT back to SOL)
PROFIT_VAULT_ENABLED: bool = os.getenv("PROFIT_VAULT_ENABLED", "true").lower() in ("true", "1", "yes")
PROFIT_VAULT_THRESHOLD_USD: float = float(os.getenv("PROFIT_VAULT_THRESHOLD_USD", "25.0"))
PROFIT_VAULT_RESERVE_SOL: float = float(os.getenv("PROFIT_VAULT_RESERVE_SOL", "0.10"))

# Autonomous Background Screening Filters (Only high-conviction breakout tokens reach the Council)
AUTOPILOT_MIN_LIQUIDITY_USD: float = float(os.getenv("AUTOPILOT_MIN_LIQUIDITY_USD", "10000.0"))
AUTOPILOT_MIN_VOLUME_24H_USD: float = float(os.getenv("AUTOPILOT_MIN_VOLUME_24H_USD", "25000.0"))
AUTOPILOT_MIN_1H_CHANGE: float = float(os.getenv("AUTOPILOT_MIN_1H_CHANGE", "1.0"))
AUTOPILOT_MAX_FDV_USD: float = float(os.getenv("AUTOPILOT_MAX_FDV_USD", "25000000.0"))


# =============================================================================
# 4. MOOD & PERSONALITY PROFILES
# =============================================================================

DEFAULT_MOOD: str = os.getenv("DEFAULT_MOOD", "analytical").strip().lower()

MOOD_PROMPTS: Dict[str, str] = {
    "analytical": (
        "CURRENT MOOD: ANALYTICAL (Objective, data-driven, balanced).\n"
        "Examine fundamental tokenomics, liquidity depth, on-chain volume, and risk-reward ratios. "
        "Formulate well-reasoned, dispassionate arguments grounded in quantifiable metrics."
    ),
    "skeptical": (
        "CURRENT MOOD: SKEPTICAL (Hyper-vigilant, paranoid, risk-averse).\n"
        "Actively look for honeypots, developer sell pressure, liquidity traps, artificial volume botting, "
        "and overextended tops. Presume every project is suspect until proven otherwise with verifiable proof."
    ),
    "aggressive": (
        "CURRENT MOOD: AGGRESSIVE (High-conviction, degen momentum, alpha-seeking).\n"
        "Focus on rapid breakout velocity, viral narrative momentum, smart money accumulation, and asymmetric upside. "
        "Emphasize the cost of hesitation and argue boldly for decisive action on high-velocity setups."
    ),
    "conservative": (
        "CURRENT MOOD: CONSERVATIVE (Capital preservation first, risk-intolerant).\n"
        "Strictly mandate verified safety metrics: revoked mint/freeze authorities, locked LP, $50K+ liquidity floor. "
        "Reject unproven or high-slippage plays; protect the treasury above all else."
    ),
    "ruthless": (
        "CURRENT MOOD: RUTHLESS (Cutthroat, game-theoretic, pragmatic).\n"
        "Analyze market PvP (player vs player) dynamics, insider wallet distribution, exit liquidity timing, "
        "and predatory setups. Strip out sentimentality and focus exclusively on cold execution edge."
    ),
    "contrarian": (
        "CURRENT MOOD: CONTRARIAN (Independent thinker, fade the crowd).\n"
        "Challenge popular consensus. When everyone is euphoric, look for the distribution trap. "
        "When everyone is fearful, look for overlooked accumulation patterns."
    ),
}


# =============================================================================
# 5. AGENT ROSTER (Initial 5 Bots, Expandable Up To 15)
# =============================================================================

# All 5 provided Telegram Bot tokens pre-configured matching their @*talkbbot usernames.
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
        model=os.getenv("MODEL_CLAUDE", "anthropic/claude-sonnet-4.5").strip(),
        provider="openrouter",
        role_description="Security & Smart Contract Auditor",
        personality_prompt=(
            "You are Claude (display name Claudetalks). You are the Senior Smart Contract & Security Architect "
            "in the AI Council. You are thoughtful, nuanced, and methodically scrutinize contract safety, "
            "code vulnerabilities, LP lock health, and long-term viability. Keep debate contributions concise, "
            "pointed, and punchy (2-4 sentences)."
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
        model=os.getenv("MODEL_GPT", "openai/gpt-4o").strip(),
        provider="openrouter",
        role_description="Macro Strategist & Liquidity Analyst",
        personality_prompt=(
            "You are GPT (display name GPTtalks). You are the Macro Strategist and Market Structure Analyst "
            "in the AI Council. You synthesize macroeconomic trends, liquidity flows, DEX market depth, "
            "and cross-asset correlations. You bring structured, balanced perspectives. Keep your replies concise "
            "and impactful (2-4 sentences)."
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
        role_description="Quantitative & Mathematical Strategist",
        personality_prompt=(
            "You are DeepSeek (display name DeepSeektalks). You are the Quantitative Data & Algorithm Specialist "
            "in the AI Council. You focus on mathematical probability, price slippage curves, buyer-to-seller ratios, "
            "and volume acceleration patterns. You are candid, numbers-oriented, and cut straight to expected value (EV). "
            "Keep replies concise and razor-sharp (2-4 sentences)."
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
        role_description="Trend Velocity & Ecosystem Synthesizer",
        personality_prompt=(
            "You are Gemini (display name Geminitalks). You are the Ecosystem & Real-Time Trend Synthesizer "
            "in the AI Council. You excel at rapid pattern recognition across Solana ecosystem narratives, "
            "social sentiment surges, and trending breakout tokens. Keep replies concise, vivid, and actionable (2-4 sentences)."
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
        role_description="PvP Momentum & Contrarian Alpha Hunter",
        personality_prompt=(
            "You are Grok (display name Groktalks). You are the High-Momentum Alpha & Degen Sentiment Hunter "
            "in the AI Council. You have a witty, unvarnished, sharp edge. You read market psychology, meme viral velocity, "
            "and PvP trader positioning. You speak truth without corporate filter. Keep replies punchy and bold (2-4 sentences)."
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
