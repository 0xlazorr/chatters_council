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
# 2. LLM BACKEND & API KEYS (Unified OpenRouter or Native Providers)
# =============================================================================

# OpenRouter Unified API Key (Recommended: one key powers all 5-15 AI models)
OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()

# Native Provider Keys (Used if OPENROUTER_API_KEY is not set or overridden per agent)
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
GROK_API_KEY: str = os.getenv("GROK_API_KEY", os.getenv("XAI_API_KEY", "")).strip()
MISTRAL_API_KEY: str = os.getenv("MISTRAL_API_KEY", "").strip()


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

# Default trade amount in SOL when executing BUY consensus
DEFAULT_TRADE_AMOUNT_SOL: float = float(os.getenv("DEFAULT_TRADE_AMOUNT_SOL", "0.25"))

# Default slippage in basis points (300 bps = 3.0%)
DEFAULT_SLIPPAGE_BPS: int = int(os.getenv("DEFAULT_SLIPPAGE_BPS", "300"))


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
# Additional bots 6 through 15 are defined and can be enabled by setting their token in .env.

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
        model=os.getenv("MODEL_CLAUDE", "anthropic/claude-3.5-sonnet").strip(),
        provider="openrouter" if OPENROUTER_API_KEY else "anthropic",
        role_description="Security & Architecture Auditor",
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
        provider="openrouter" if OPENROUTER_API_KEY else "openai",
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
        provider="openrouter" if OPENROUTER_API_KEY else "deepseek",
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
        provider="openrouter" if OPENROUTER_API_KEY else "gemini",
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
        model=os.getenv("MODEL_GROK", "x-ai/grok-2").strip(),
        provider="openrouter" if OPENROUTER_API_KEY else "grok",
        role_description="PvP Momentum & Contrarian Alpha Hunter",
        personality_prompt=(
            "You are Grok (display name Groktalks). You are the High-Momentum Alpha & Degen Sentiment Hunter "
            "in the AI Council. You have a witty, unvarnished, sharp edge. You read market psychology, meme viral velocity, "
            "and PvP trader positioning. You speak truth without corporate filter. Keep replies punchy and bold (2-4 sentences)."
        ),
        enabled=True,
    ),

    # =========================================================================
    # Bots 6-15: Expandable Slot Lineup (Set BOT_TOKEN_* in .env to activate)
    # =========================================================================
    AgentConfig(
        id="mistral",
        name="Mistral",
        display_name="Mistraltalks",
        username="mistraltalkbbot",
        token=os.getenv("BOT_TOKEN_MISTRAL", "").strip(),
        model=os.getenv("MODEL_MISTRAL", "mistralai/mistral-large-2411").strip(),
        provider="openrouter" if OPENROUTER_API_KEY else "mistral",
        role_description="Decentralized Protocol & Game Theory Specialist",
        personality_prompt="You are Mistral (display name Mistraltalks). You specialize in European market flows, open-weights protocol architectures, and game theory.",
        enabled=bool(os.getenv("BOT_TOKEN_MISTRAL", "").strip()),
    ),
    AgentConfig(
        id="llama",
        name="Llama",
        display_name="Llamatalks",
        username="llamatalkbbot",
        token=os.getenv("BOT_TOKEN_LLAMA", "").strip(),
        model=os.getenv("MODEL_LLAMA", "meta-llama/llama-3.3-70b-instruct").strip(),
        provider="openrouter",
        role_description="Open-Source On-Chain Auditor",
        personality_prompt="You are Llama (display name Llamatalks). You specialize in decentralized computing, open systems, and transparent on-chain mechanics.",
        enabled=bool(os.getenv("BOT_TOKEN_LLAMA", "").strip()),
    ),
    AgentConfig(
        id="qwen",
        name="Qwen",
        display_name="Qwentalks",
        username="qwentalkbbot",
        token=os.getenv("BOT_TOKEN_QWEN", "").strip(),
        model=os.getenv("MODEL_QWEN", "qwen/qwen-2.5-72b-instruct").strip(),
        provider="openrouter",
        role_description="Global Arbitrage & Market Flow Specialist",
        personality_prompt="You are Qwen (display name Qwentalks). You specialize in global liquidity distribution, cross-border capital flows, and algorithmic arbitrage.",
        enabled=bool(os.getenv("BOT_TOKEN_QWEN", "").strip()),
    ),
    AgentConfig(
        id="cohere",
        name="Cohere",
        display_name="Coheretalks",
        username="coheretalkbbot",
        token=os.getenv("BOT_TOKEN_COHERE", "").strip(),
        model=os.getenv("MODEL_COHERE", "cohere/command-r-plus").strip(),
        provider="openrouter",
        role_description="Enterprise & Settlement Mechanics",
        personality_prompt="You are Cohere (display name Coheretalks). You specialize in institutional order execution, slippage reduction, and liquidity settlement.",
        enabled=bool(os.getenv("BOT_TOKEN_COHERE", "").strip()),
    ),
    AgentConfig(
        id="perplexity",
        name="Perplexity",
        display_name="Perplexitytalks",
        username="perplexitytalkbbot",
        token=os.getenv("BOT_TOKEN_PERPLEXITY", "").strip(),
        model=os.getenv("MODEL_PERPLEXITY", "perplexity/sonar-reasoning").strip(),
        provider="openrouter",
        role_description="Real-Time Web & News Intelligence",
        personality_prompt="You are Perplexity (display name Perplexitytalks). You specialize in real-time news aggregation, event-driven trading, and social catalyst verification.",
        enabled=bool(os.getenv("BOT_TOKEN_PERPLEXITY", "").strip()),
    ),
    AgentConfig(
        id="dbrx",
        name="DBRX",
        display_name="DBRXtalks",
        username="dbrxtalkbbot",
        token=os.getenv("BOT_TOKEN_DBRX", "").strip(),
        model=os.getenv("MODEL_DBRX", "databricks/dbrx-instruct").strip(),
        provider="openrouter",
        role_description="High-Throughput Data Pipeline Specialist",
        personality_prompt="You are DBRX (display name DBRXtalks). You analyze high-frequency DEX volume logs and identify anomalous buyer clustering.",
        enabled=bool(os.getenv("BOT_TOKEN_DBRX", "").strip()),
    ),
    AgentConfig(
        id="haiku",
        name="Haiku",
        display_name="Haikutalks",
        username="haikutalkbbot",
        token=os.getenv("BOT_TOKEN_HAIKU", "").strip(),
        model=os.getenv("MODEL_HAIKU", "anthropic/claude-3.5-haiku").strip(),
        provider="openrouter",
        role_description="High-Speed Flash Scout",
        personality_prompt="You are Haiku (display name Haikutalks). You are a rapid-response flash scout delivering immediate 1-sentence evaluations.",
        enabled=bool(os.getenv("BOT_TOKEN_HAIKU", "").strip()),
    ),
    AgentConfig(
        id="minigpt",
        name="MiniGPT",
        display_name="MiniGPTtalks",
        username="minigpttalkbbot",
        token=os.getenv("BOT_TOKEN_MINIGPT", "").strip(),
        model=os.getenv("MODEL_MINIGPT", "openai/gpt-4o-mini").strip(),
        provider="openrouter",
        role_description="Micro-Cap Rapid Evaluator",
        personality_prompt="You are MiniGPT (display name MiniGPTtalks). You specialize in low-latency micro-cap vetting and fast execution parameters.",
        enabled=bool(os.getenv("BOT_TOKEN_MINIGPT", "").strip()),
    ),
    AgentConfig(
        id="grokmini",
        name="GrokMini",
        display_name="GrokMinitalks",
        username="grokminitalkbbot",
        token=os.getenv("BOT_TOKEN_GROKMINI", "").strip(),
        model=os.getenv("MODEL_GROKMINI", "x-ai/grok-2-mini").strip(),
        provider="openrouter",
        role_description="Micro-Trend Sentiment Scalper",
        personality_prompt="You are GrokMini (display name GrokMinitalks). You track viral subculture micro-trends and early stage pump.fun token launches.",
        enabled=bool(os.getenv("BOT_TOKEN_GROKMINI", "").strip()),
    ),
    AgentConfig(
        id="mixtral",
        name="Mixtral",
        display_name="Mixtraltalks",
        username="mixtraltalkbbot",
        token=os.getenv("BOT_TOKEN_MIXTRAL", "").strip(),
        model=os.getenv("MODEL_MIXTRAL", "mistralai/mixtral-8x22b-instruct").strip(),
        provider="openrouter",
        role_description="Mixture-of-Experts Arbitrageur",
        personality_prompt="You are Mixtral (display name Mixtraltalks). You leverage multi-expert consensus heuristics for volatile trading environments.",
        enabled=bool(os.getenv("BOT_TOKEN_MIXTRAL", "").strip()),
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
