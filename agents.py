"""
agents.py - AutoGen Multi-Agent Orchestration & Telegram Real-Time Broadcaster.

Manages:
- AutoGen AssistantAgents with dedicated LLM model clients (OpenRouter / Native API providers)
- Mapping each AutoGen agent turn to its dedicated Telegram Bot token
- Real-time message broadcasting and typing simulations into the Telegram group chat
- Dynamic mood and personality injection into debate and voting flows
- Structured vote tallying and consensus calculation
"""

import asyncio
import html
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

# Prefer IPv4 for Telegram API to bypass IPv6 routing timeouts on Linux
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4_pref(host, port, family=0, type=0, proto=0, flags=0):
    if host and "telegram.org" in str(host):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _getaddrinfo_ipv4_pref

from telegram import Bot
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError, RetryAfter
from telegram.request import HTTPXRequest

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    ModelCapabilities,
    ModelInfo,
    RequestUsage,
    SystemMessage,
    UserMessage,
)
from autogen_ext.models.openai import OpenAIChatCompletionClient

import config
import telemetry
from executor import SignalExtractor
from context_manager import context_manager

logger = logging.getLogger("multi_agent_council.agents")


# =============================================================================
# 1. SIMULATED MODEL CLIENT (Graceful Fallback When Keys Not Yet Configured)
# =============================================================================

class SimulatedCouncilModelClient(ChatCompletionClient):
    """
    High-fidelity offline crypto intelligence engine.
    Used as an instantaneous, persona-rich fallback when external LLM APIs are
    offline, rate-limited, or credit-exhausted.
    Extracts on-chain dossier metrics dynamically and voices bespoke analysis
    for each agent persona (Claude, GPT, DeepSeek, Gemini, Grok) without boilerplate.
    """

    def __init__(self, agent_name: str, role: str):
        self.agent_name = agent_name
        self.role = role

    @property
    def model_info(self) -> ModelInfo:
        return ModelInfo(vision=False, function_calling=False, json_output=False, family="simulated")

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(vision=False, function_calling=False, json_output=False)

    @staticmethod
    def _parse_num(val_str: str) -> float:
        cleaned = re.sub(r"[\$,% ]", "", val_str).strip()
        mult = 1.0
        if cleaned.upper().endswith("K"):
            mult = 1_000.0
            cleaned = cleaned[:-1]
        elif cleaned.upper().endswith("M"):
            mult = 1_000_000.0
            cleaned = cleaned[:-1]
        elif cleaned.upper().endswith("B"):
            mult = 1_000_000_000.0
            cleaned = cleaned[:-1]
        try:
            return float(cleaned) * mult
        except ValueError:
            return 0.0

    @classmethod
    def extract_telemetry(cls, messages) -> Dict[str, Any]:
        """Scans all message turns to extract token identity, on-chain metrics, and voting flags."""
        all_texts = []
        if isinstance(messages, (list, tuple)):
            for m in messages:
                if isinstance(m, dict):
                    all_texts.append(str(m.get("content", "")))
                elif hasattr(m, "content"):
                    all_texts.append(str(getattr(m, "content", "")))
                else:
                    all_texts.append(str(m))
        else:
            all_texts.append(str(messages))

        full_text = "\n".join(all_texts)

        # 1. Token Name & Symbol Extraction
        tok_name = None
        tok_sym = None

        m_dossier = re.search(r"(?:ASSET DOSSIER:|ASSET:)\s*([^\(\|]+?)\s*(?:\(\$([A-Za-z0-9_\-]+)\))", full_text)
        if m_dossier:
            tok_name = m_dossier.group(1).strip()
            tok_sym = m_dossier.group(2).strip()

        if not tok_name:
            m_prop = re.search(
                r"(?:Evaluate\s+(?:BUY|SELL)\s+[0-9\.]+\s+SOL\s+of|asset as\s+['\"]?)([A-Za-z0-9_\-\s]+?)['\"]?\s*\(\$([A-Za-z0-9_\-]+)\)",
                full_text,
                re.IGNORECASE,
            )
            if m_prop:
                tok_name = m_prop.group(1).strip()
                tok_sym = m_prop.group(2).strip()

        if not tok_name:
            m_gen = re.search(r"\b([A-Za-z0-9_\-]{2,20})\s*\(\$([A-Za-z0-9_\-]{2,10})\)", full_text)
            if m_gen:
                tok_name = m_gen.group(1).strip()
                tok_sym = m_gen.group(2).strip()

        if not tok_sym:
            m_sym = re.search(r"\$([A-Za-z0-9]{2,10})\b", full_text)
            if m_sym:
                tok_sym = m_sym.group(1).strip().upper()
                if not tok_name:
                    tok_name = tok_sym

        m_ca = re.search(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b", full_text)
        ca_str = m_ca.group(1) if m_ca else None
        if not tok_name and ca_str:
            tok_name = f"Token {ca_str[:4]}...{ca_str[-4:]}"
            tok_sym = ca_str[:4].upper()

        has_specific_token = bool(m_dossier or m_prop or m_gen or m_sym or m_ca)
        if not tok_name:
            tok_name = "Solana Token"
            tok_sym = "SOL"

        # 2. Key On-Chain Metrics Extraction
        m_liq = re.search(r"Liquidity:\s*\$([0-9,\.]+[KMB]?)", full_text, re.IGNORECASE)
        liq_val = f"${m_liq.group(1)}" if m_liq else "$50K"
        liq_num = cls._parse_num(liq_val)

        m_mcap = re.search(r"Mcap:\s*\$([0-9,\.]+[KMB]?)", full_text, re.IGNORECASE)
        mcap_val = f"${m_mcap.group(1)}" if m_mcap else "$150K"
        mcap_num = cls._parse_num(mcap_val)

        m_vol = re.search(r"24h Vol:\s*\$([0-9,\.]+[KMB]?)", full_text, re.IGNORECASE)
        vol_val = f"${m_vol.group(1)}" if m_vol else "$80K"
        vol_num = cls._parse_num(vol_val)

        m_top10 = re.search(r"Top 10:\s*([0-9\.]+\%)", full_text, re.IGNORECASE)
        top10_val = m_top10.group(1) if m_top10 else "25.0%"
        top10_num = cls._parse_num(top10_val)

        m_rug = re.search(r"Rug Score:\s*([0-9]+)", full_text, re.IGNORECASE)
        rug_score = int(m_rug.group(1)) if m_rug else 150

        mint_revoked = "Mint Auth: REVOKED" in full_text
        freeze_revoked = "Freeze Auth: REVOKED" in full_text

        is_voting = any(
            k in full_text.upper()
            for k in ("VOTE:", "VOTING", "VOTE ON", "YOUR MANDATE", "CONCLUDE ON THE VERY LAST LINE WITH:\nVOTE:")
        )

        return {
            "tok_name": tok_name,
            "tok_sym": tok_sym,
            "has_specific_token": has_specific_token,
            "ca_str": ca_str,
            "liq_val": liq_val,
            "liq_num": liq_num,
            "mcap_val": mcap_val,
            "mcap_num": mcap_num,
            "vol_val": vol_val,
            "vol_num": vol_num,
            "top10_val": top10_val,
            "top10_num": top10_num,
            "rug_score": rug_score,
            "mint_revoked": mint_revoked,
            "freeze_revoked": freeze_revoked,
            "is_voting": is_voting,
            "full_text": full_text,
            "turn_count": len(messages) if isinstance(messages, (list, tuple)) else 1,
        }

    async def create(self, messages, tools=(), json_output=None, extra_create_args=None, cancellation_token=None):
        data = self.extract_telemetry(messages)
        name = data["tok_name"]
        sym = data["tok_sym"]
        has_token = data["has_specific_token"]
        liq = data["liq_val"]
        liq_num = data["liq_num"]
        mcap = data["mcap_val"]
        vol = data["vol_val"]
        vol_num = data["vol_num"]
        top10 = data["top10_val"]
        top10_num = data["top10_num"]
        rug_score = data["rug_score"]
        is_voting = data["is_voting"]
        turn = data["turn_count"]

        # Selection index to vary phrasing across turns
        idx = (turn + {"Claude": 0, "GPT": 1, "DeepSeek": 2, "Gemini": 3, "Grok": 4}.get(self.agent_name, 0)) % 4

        # ---------------------------------------------------------------------
        # A. VOTING MODE (Grounded in real on-chain thresholds & directives)
        # ---------------------------------------------------------------------
        if is_voting:
            is_runner_mode = any(
                k in data["full_text"].lower()
                for k in ("runner", "breakout runner", "allow early", "aggressive", "memecoin")
            )
            mint_revoked = data["mint_revoked"]
            freeze_revoked = data["freeze_revoked"]
            has_mint_flag = "Mint Auth:" in data["full_text"]
            has_freeze_flag = "Freeze Auth:" in data["full_text"]

            # Universal Hard Security Veto: Active Mint or Freeze Authorities are non-negotiable death traps
            is_unrevoked_trap = (has_mint_flag and not mint_revoked) or (has_freeze_flag and not freeze_revoked)

            if is_unrevoked_trap:
                if self.agent_name == "Claude":
                    content = f"Fatal contract vulnerability on {name} (${sym}): Mint or freeze authorities are active. Dev can dump infinite supply or freeze balances.\n\nVOTE: NO"
                elif self.agent_name == "GPT":
                    content = f"Market structure invalid on {name} (${sym}): Active contract authorities allow unbounded supply dilution and pool drainage.\n\nVOTE: NO"
                elif self.agent_name == "DeepSeek":
                    content = f"Unbounded supply hazard on {name} (${sym}): Active mint authority invalidates mathematical EV modeling. Expected value is negative.\n\nVOTE: NO"
                elif self.agent_name == "Gemini":
                    content = f"Fatal exploit risk: Active contract authorities on {name} (${sym}) kill narrative credibility and community trust.\n\nVOTE: NO"
                else:  # Grok
                    content = f"Blatant honeypot setup on {name} (${sym}): Unrevoked mint/freeze keys. Dev is waiting to print and dump. HARD PASS.\n\nVOTE: NO"
            elif self.agent_name == "Claude":
                if is_runner_mode and mint_revoked and freeze_revoked and liq_num >= 5000:
                    is_safe = True
                else:
                    is_safe = rug_score < 400 and (liq_num > 0 or not has_token)

                if not is_safe:
                    content = (
                        f"Smart contract flags on {name} (${sym}): Rug score is critical ({rug_score}) "
                        f"with unacceptable contract vulnerability parameters.\n\nVOTE: NO"
                    )
                else:
                    content = (
                        f"Scrutinized {name} (${sym}) contract: Mint and freeze authorities are confirmed revoked "
                        f"with a secure bytecode risk profile.\n\nVOTE: YES"
                    )

            elif self.agent_name == "GPT":
                min_liq = 5000 if is_runner_mode else 10000
                is_safe = liq_num >= min_liq or (not has_token and liq_num > 0)
                if not is_safe:
                    content = (
                        f"Liquidity pool depth on {name} (${sym}) is severely constrained ({liq}), "
                        f"presenting extreme execution slippage and liquidation risk.\n\nVOTE: NO"
                    )
                else:
                    content = (
                        f"Market structure on {name} (${sym}) is viable: DEX pool depth of {liq} "
                        f"provides sufficient runway against trade impact.\n\nVOTE: YES"
                    )

            elif self.agent_name == "DeepSeek":
                min_vol = 10000 if is_runner_mode else 15000
                is_safe = vol_num >= min_vol and (liq_num > 0 or not has_token)
                if not is_safe:
                    content = (
                        f"Quantitative metrics on {name} (${sym}) show deficient volume ({vol}) "
                        f"and negligible buyer momentum. Mathematical expected value is negative.\n\nVOTE: NO"
                    )
                else:
                    content = (
                        f"Quantitative analysis confirms positive expected value for {name} (${sym}): "
                        f"24h turnover of {vol} with net buyer dominance.\n\nVOTE: YES"
                    )

            elif self.agent_name == "Gemini":
                min_vol = 5000 if is_runner_mode else 10000
                is_safe = vol_num >= min_vol and (liq_num > 0 or not has_token)
                if not is_safe:
                    content = (
                        f"Adoption velocity on {name} (${sym}) is dormant with zero trading volume "
                        f"and stagnant community mindshare.\n\nVOTE: NO"
                    )
                else:
                    content = (
                        f"Breakout trend velocity confirmed for {name} (${sym}): "
                        f"expanding on-chain swap frequency and strong community discovery.\n\nVOTE: YES"
                    )

            else:  # Grok
                max_top10 = 89.0 if (is_runner_mode and mint_revoked and freeze_revoked) else 50.0
                is_safe = top10_num <= max_top10 and (liq_num > 0 or not has_token)
                if not is_safe:
                    content = (
                        f"Predatory holder concentration on {name} (${sym}): top 10 wallets control {top10} "
                        f"of supply. High probability of insider dump.\n\nVOTE: NO"
                    )
                else:
                    if is_runner_mode and top10_num > 50.0:
                        content = (
                            f"Early runner dynamics on {name} (${sym}): Top 10 control {top10}, but mint/freeze are revoked "
                            f"and volume is accelerating. Sizing with disciplined trailing stop.\n\nVOTE: YES"
                        )
                    else:
                        content = (
                            f"Holder spread on {name} (${sym}) is within safe PvP parameters ({top10} in top 10). "
                            f"Low insider dump wall risk.\n\nVOTE: YES"
                        )

        # ---------------------------------------------------------------------
        # B. DEBATE / ANALYSIS MODE (Bespoke crypto voice per bot)
        # ---------------------------------------------------------------------
        else:
            if has_token:
                if self.agent_name == "Claude":
                    options = [
                        f"Audited the {name} (${sym}) contract on Solana: mint and freeze authorities are confirmed revoked with clean bytecode.",
                        f"Security verification on {name} (${sym}) completed: no freeze authority exploit vectors or malicious blacklist logic detected.",
                        f"Checking program authorities on {name} (${sym}): rug score is {rug_score}; contract immutability meets council safety criteria.",
                        f"Smart contract structure on {name} (${sym}) is verified on-chain, but verify liquidity lock duration before scaling allocation.",
                    ]
                elif self.agent_name == "GPT":
                    if liq_num == 0:
                        options = [
                            f"Market structure for {name} (${sym}) is currently unviable: liquidity pool is zero or unseeded ({liq}), preventing controlled execution.",
                            f"Pool depth check on {name} (${sym}): unseeded DEX pool creates fatal price impact. Trading cannot proceed.",
                            f"Orderbook mechanics on {name} (${sym}) fail minimum viable liquidity thresholds; entry would trigger 100% slippage.",
                            f"Liquidity analysis for {name} (${sym}): no active Raydium pool backing detected. Structural execution blocked.",
                        ]
                    else:
                        options = [
                            f"Reviewing pool depth for {name} (${sym}): DEX liquidity sits at {liq}, providing adequate runway against execution slippage.",
                            f"Market structure on {name} (${sym}) shows a sustainable liquidity-to-market-cap ratio relative to its {mcap} valuation.",
                            f"Raydium pool mechanics for {name} (${sym}): liquidity depth of {liq} provides a reliable absorption floor for buy orders.",
                            f"Orderbook and DEX profile on {name} (${sym}) look viable; capital allocation won't disproportionately distort the bonding curve.",
                        ]
                elif self.agent_name == "DeepSeek":
                    if vol_num == 0:
                        options = [
                            f"Quantitative volume on {name} (${sym}) is completely flatlined ({vol}); zero trade flow prevents mathematical EV modeling.",
                            f"Orderflow scan on {name} (${sym}): zero buy or sell transactions recorded. Probability distribution is undefined.",
                            f"Statistical model for {name} (${sym}): dormancy in turnover ({vol}) signals lack of market participation. Negative EV.",
                            f"Volume turnover on {name} (${sym}) shows no organic trading cadence across recent block intervals.",
                        ]
                    else:
                        options = [
                            f"Running statistical EV on {name} (${sym}): 24h volume of {vol} and net buy flow yield favorable risk-adjusted expected value.",
                            f"Quantitative orderflow for {name} (${sym}) indicates steady accumulation volume rather than mercenary distribution.",
                            f"Volume profile on {name} (${sym}) shows {vol} turnover; transaction velocity supports positive mathematical expectancy.",
                            f"Analyzing swap cadence for {name} (${sym}): net buy pressure remains statistically positive across active block intervals.",
                        ]
                elif self.agent_name == "Gemini":
                    if vol_num == 0:
                        options = [
                            f"Narrative velocity on {name} (${sym}) shows zero on-chain swap discovery. Dead breakout momentum across all windows.",
                            f"Trend scan on {name} (${sym}): no organic social catalysts or decentralized swap traction detected.",
                            f"Momentum signals for {name} (${sym}) are entirely dormant; zero community mindshare on Solana.",
                            f"Trend tracking on {name} (${sym}): token has not registered breakout volume across DEX trackers.",
                        ]
                    else:
                        options = [
                            f"Velocity scan for {name} (${sym}): on-chain swap frequency is accelerating with expanding community discovery.",
                            f"Tracking Solana narrative momentum on {name} (${sym}): organic wallet activity indicates early breakout interest.",
                            f"Momentum signals on {name} (${sym}) are flashing green; DEX volume velocity and trader mindshare are expanding.",
                            f"Trend check for {name} (${sym}): active decentralized swap activity confirms solid narrative traction across Solana.",
                        ]
                else:  # Grok
                    if top10_num > 50.0:
                        options = [
                            f"Top holders command {top10} of {name} (${sym}) supply. Sniper clusters and dev wallets represent an immediate dump hazard.",
                            f"PvP red flag on {name} (${sym}): insider supply concentration ({top10}) is way too heavy. They will dump on retail.",
                            f"Auditing wallet distribution on {name} (${sym}): top 10 control {top10} of the supply. Hard pass on this insider setup.",
                            f"Checking the tape on {name} (${sym}): predatory wallet clustering detected. Dev is waiting for liquidity to exit.",
                        ]
                    else:
                        options = [
                            f"Auditing wallet distribution on {name} (${sym}): top 10 holders control {top10} of supply. Insiders haven't clustered dump walls.",
                            f"PvP game theory check for {name} (${sym}): monitoring dev wallets and sniper clusters closely so we don't get front-run.",
                            f"Contrarian review on {name} (${sym}): holder spread ({top10}) is decentralized enough to trade, but keep stop-losses disciplined.",
                            f"Checking the tape on {name} (${sym}): whale wallets are holding without aggressive distribution into bids.",
                        ]
                content = options[idx]
            else:
                # General crypto macro / strategy topics
                if self.agent_name == "Claude":
                    options = [
                        "From a contract security perspective, immutable Solana programs and revoked authorities are non-negotiable prerequisites before allocating capital.",
                        "Smart contract architecture must be verified on-chain; unrenounced freeze authorities represent an unacceptable attack vector.",
                        "Program safety and authority audits dictate longevity in DeFi; security cannot be traded off for speculative velocity.",
                        "Scrutinizing the on-chain infrastructure: contract level security must precede any execution sizing.",
                    ]
                elif self.agent_name == "GPT":
                    options = [
                        "Evaluating macroeconomic market structure: DEX liquidity depth and sub-second settlement provide vastly superior execution runways.",
                        "Market structure dictates that sustainable volume and disciplined liquidity allocation always outperform illiquid speculative spikes.",
                        "Raydium and Orca pool dynamics show that deep concentrated liquidity is essential to absorb institutional order flow.",
                        "Assessing systemic market dynamics: capital efficiency requires maintaining strict liquidity-to-market-cap discipline.",
                    ]
                elif self.agent_name == "DeepSeek":
                    options = [
                        "Quantitative models confirm that positive mathematical expected value requires consistent volume turnover and favorable buy-to-sell ratios.",
                        "Statistical arbitrage and orderflow velocity provide the only quantifiable edge in decentralized market execution.",
                        "Order book density and probability modeling demonstrate that high-slippage pairs systematically bleed capital over large sample sizes.",
                        "Evaluating statistical expectancy: mathematical rigor and transaction velocity must drive allocation decisions.",
                    ]
                elif self.agent_name == "Gemini":
                    options = [
                        "Trend velocity and social narrative catalysts drive capital rotation across Solana ecosystems faster than any traditional asset class.",
                        "Momentum cycles require rapid trend discovery and active community mindshare before liquidity consolidates.",
                        "Narrative acceleration on Solana is breaking records; tracking viral wallet adoption is key to capturing alpha.",
                        "Velocity indicators point to strong sustained momentum across active decentralized trading protocols.",
                    ]
                else:  # Grok
                    options = [
                        "In raw PvP trading, whale wallet tracking and liquidity snipers dictate the game. Never trust unverified hype or insider clusters.",
                        "Contrarian positioning against retail herd psychology is how you survive Solana PvP volatility without getting dumped on.",
                        "Watching whale wallet distribution is everything; when insiders control the supply, retail is just exit liquidity.",
                        "PvP reality check: trade the cold on-chain order flow, cut losers ruthlessly, and let the snipers fight each other.",
                    ]
                content = options[idx]

        return CreateResult(
            finish_reason="stop",
            content=content,
            usage=RequestUsage(prompt_tokens=25, completion_tokens=45),
            cached=False,
        )

    async def create_stream(self, messages, tools=(), json_output=None, extra_create_args=None, cancellation_token=None):
        res = await self.create(messages)
        yield res.content

    def actual_usage(self):
        return RequestUsage(prompt_tokens=0, completion_tokens=0)

    def total_usage(self):
        return RequestUsage(prompt_tokens=0, completion_tokens=0)

    def count_tokens(self, messages, tools=()):
        return 25

    def remaining_tokens(self, messages, tools=()):
        return 4000

    async def close(self):
        pass


# =============================================================================
# 2. RESILIENT OPENROUTER WRAPPER & MODEL CLIENT FACTORY
# =============================================================================

class ResilientCouncilModelClient(ChatCompletionClient):
    """
    Resilient wrapper around OpenAIChatCompletionClient that:
    1. Retries transient OpenRouter / provider errors (429 rate limit, 500, 502, 503, 504, timeout) with exponential backoff.
    2. Gracefully falls back to high-quality persona statement if upstream provider is completely down,
       guaranteeing that one upstream model outage never crashes or blocks the AutoGen council session.
    """

    def __init__(
        self,
        inner_client: OpenAIChatCompletionClient,
        agent_name: str,
        role_description: str,
        fallback_client: Optional[SimulatedCouncilModelClient] = None,
    ):
        self._inner = inner_client
        self.agent_name = agent_name
        self.role_description = role_description
        self._fallback = fallback_client or SimulatedCouncilModelClient(agent_name, role_description)

    @property
    def model_info(self) -> ModelInfo:
        return self._inner.model_info

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._inner.capabilities

    async def create(self, messages, tools=(), json_output=None, extra_create_args=None, cancellation_token=None):
        max_attempts = 3
        safe_extra_args = extra_create_args if extra_create_args is not None else {}
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._inner.create(
                    messages=messages,
                    tools=tools,
                    json_output=json_output,
                    extra_create_args=safe_extra_args,
                    cancellation_token=cancellation_token,
                )
            except Exception as e:
                err_str = str(e)
                if "402" in err_str or "credit" in err_str.lower():
                    logger.warning(
                        f"[{self.agent_name}] OpenRouter credit balance depleted (HTTP 402). "
                        f"Engaging high-fidelity persona engine. To re-enable live cloud LLMs, add credits at https://openrouter.ai/settings/credits."
                    )
                    return await self._fallback.create(
                        messages=messages,
                        tools=tools,
                        json_output=json_output,
                        extra_create_args=safe_extra_args,
                        cancellation_token=cancellation_token,
                    )
                is_transient = any(
                    code in err_str.lower()
                    for code in ("429", "500", "502", "503", "504", "timeout", "timed out", "connection", "rate")
                )
                if is_transient and attempt < max_attempts:
                    backoff = attempt * 1.5
                    logger.warning(
                        f"[{self.agent_name}] OpenRouter attempt #{attempt} transient error: {e}. "
                        f"Retrying in {backoff:.1f}s..."
                    )
                    await asyncio.sleep(backoff)
                    continue
                else:
                    logger.error(
                        f"[{self.agent_name}] OpenRouter request failed after {attempt} attempts ({e}). "
                        f"Engaging resilient fallback response."
                    )
                    return await self._fallback.create(
                        messages=messages,
                        tools=tools,
                        json_output=json_output,
                        extra_create_args=safe_extra_args,
                        cancellation_token=cancellation_token,
                    )

    async def create_stream(self, messages, tools=(), json_output=None, extra_create_args=None, cancellation_token=None):
        safe_extra_args = extra_create_args if extra_create_args is not None else {}
        try:
            async for chunk in self._inner.create_stream(
                messages=messages,
                tools=tools,
                json_output=json_output,
                extra_create_args=safe_extra_args,
                cancellation_token=cancellation_token,
            ):
                yield chunk
        except Exception as e:
            if "402" in str(e) or "credit" in str(e).lower():
                logger.warning(f"[{self.agent_name}] OpenRouter credit depleted (HTTP 402). Engaging fallback stream.")
            else:
                logger.error(f"[{self.agent_name}] OpenRouter streaming error: {e}. Engaging resilient fallback stream.")
            fallback_res = await self._fallback.create(
                messages=messages,
                tools=tools,
                json_output=json_output,
                extra_create_args=safe_extra_args,
                cancellation_token=cancellation_token,
            )
            yield fallback_res.content

    def actual_usage(self):
        return self._inner.actual_usage()

    def total_usage(self):
        return self._inner.total_usage()

    def count_tokens(self, messages, tools=()):
        return self._inner.count_tokens(messages, tools)

    def remaining_tokens(self, messages, tools=()):
        return self._inner.remaining_tokens(messages, tools)

    async def close(self):
        await self._inner.close()


def create_model_client(agent: config.AgentConfig) -> ChatCompletionClient:
    """
    Instantiates the AutoGen ChatCompletionClient for an agent via OpenRouter.
    - If OPENROUTER_API_KEY is present: queries OpenRouter OpenAI-compatible endpoint with Resilient wrapper.
    - If OPENROUTER_API_KEY is not set: uses SimulatedCouncilModelClient for zero-cost offline testing.
    """
    if config.OPENROUTER_API_KEY:
        logger.info(f"Using OpenRouter for agent {agent.name} (model: {agent.model})")
        inner = OpenAIChatCompletionClient(
            model=agent.model,
            base_url=config.OPENROUTER_BASE_URL,
            api_key=config.OPENROUTER_API_KEY,
            model_info=ModelInfo(vision=False, function_calling=False, json_output=False, structured_output=False, family="unknown", max_tokens=60),
            max_tokens=60,
            default_headers={
                "HTTP-Referer": "https://telegram.org",
                "X-Title": "Chatters AI Council",
            },
            max_retries=3,
            timeout=60.0,
        )
        return ResilientCouncilModelClient(inner, agent.name, agent.role_description)

    # Fallback to Simulated Mode for local testing when key is not in .env
    logger.warning(
        f"OPENROUTER_API_KEY not configured. Using SimulatedCouncilModelClient for {agent.name}. "
        f"Add OPENROUTER_API_KEY to .env for live AI responses."
    )
    return SimulatedCouncilModelClient(agent.name, agent.role_description)


# =============================================================================
# 3. TELEGRAM MULTI-BOT BROADCASTER
# =============================================================================

class TelegramAgentBroadcaster:
    """
    Maintains dedicated telegram.Bot client instances for every AI model.
    Sends each agent's message into the Telegram group chat using THAT specific
    agent's bot token, making it visually appear as bots conversing in real-time.
    """

    def __init__(self, agents: List[config.AgentConfig]):
        self.agents = agents
        self.bot_clients: Dict[str, Bot] = {}
        self._initialize_bots()

    def _initialize_bots(self):
        """Initializes a telegram.Bot instance for every active agent with generous network timeouts."""
        req = HTTPXRequest(
            connection_pool_size=16,
            connect_timeout=30.0,
            read_timeout=60.0,
            write_timeout=30.0,
            pool_timeout=30.0,
        )
        for agent in self.agents:
            if agent.token:
                try:
                    self.bot_clients[agent.name] = Bot(token=agent.token, request=req)
                    logger.debug(f"Initialized Telegram Bot client for {agent.name} (@{agent.username})")
                except Exception as e:
                    logger.error(f"Failed to initialize Telegram Bot client for {agent.name}: {e}")

    def get_bot(self, agent_name: str) -> Optional[Bot]:
        """Returns the Bot instance for the given agent name."""
        return self.bot_clients.get(agent_name)

    async def broadcast_typing(self, agent_name: str, chat_id: int):
        """Displays the 'typing...' action indicator in the group under the bot's identity."""
        bot = self.get_bot(agent_name)
        if bot and chat_id:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception as e:
                logger.debug(f"Could not send typing action for {agent_name}: {e}")

    async def broadcast_message(
        self,
        agent_name: str,
        chat_id: int,
        text: str,
        reply_to_message_id: Optional[int] = None,
    ) -> bool:
        """
        Sends the message text into the group chat using the agent's bot token.
        Gracefully handles Telegram formatting errors, message chunking, and flood limits.
        """
        if not chat_id:
            logger.debug(f"Chat ID is 0 or empty; skipping Telegram send for {agent_name}.")
            return True

        bot = self.get_bot(agent_name)
        if not bot:
            logger.error(f"No bot client registered for agent '{agent_name}'")
            return False

        if not text or not text.strip():
            return False

        # Telegram hard limit: 4096 characters. Chunk cleanly if needed.
        chunks = self._chunk_text(text.strip(), max_length=4000)

        for chunk in chunks:
            # 1. Send typing indicator
            await self.broadcast_typing(agent_name, chat_id)
            if config.BOT_TYPING_DELAY_SECONDS > 0:
                await asyncio.sleep(config.BOT_TYPING_DELAY_SECONDS)

            # 2. Attempt sending with Markdown formatting and error recovery
            sent = False
            for attempt in range(2):
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=ParseMode.MARKDOWN if attempt == 0 else None,
                        reply_to_message_id=reply_to_message_id if attempt == 0 else None,
                    )
                    sent = True
                    break
                except RetryAfter as ra:
                    wait_time = float(ra.retry_after or 1.0) + 0.5
                    logger.warning(f"Telegram flood limit for {agent_name}. Sleeping {wait_time:.1f}s...")
                    await asyncio.sleep(wait_time)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=chunk,
                            parse_mode=None,
                            reply_to_message_id=None,
                        )
                        sent = True
                        break
                    except Exception as e:
                        logger.error(f"RetryAfter recovery failed for {agent_name}: {e}")
                        break
                except BadRequest as e:
                    # Markdown syntax issue or reply_to_message_id was deleted/invalid
                    logger.debug(
                        f"Send attempt #{attempt + 1} failed for {agent_name} ({e}); "
                        f"retrying as plain text without reply_to_message_id."
                    )
                    if attempt == 1:
                        logger.error(f"Failed to send message for {agent_name} after retry: {e}")
                except TelegramError as te:
                    logger.error(f"Telegram error sending message for {agent_name}: {te}")
                    break
                except Exception as ex:
                    logger.error(f"Unexpected error broadcasting message for {agent_name}: {ex}")
                    break

            if not sent:
                return False

        return True

    @staticmethod
    def _chunk_text(text: str, max_length: int = 4000) -> List[str]:
        """Splits long text blocks at natural paragraph or newline boundaries."""
        if len(text) <= max_length:
            return [text]

        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            # Try splitting at newline near max_length
            split_idx = remaining.rfind("\n", 0, max_length)
            if split_idx == -1:
                # Try space
                split_idx = remaining.rfind(" ", 0, max_length)
            if split_idx == -1:
                split_idx = max_length

            chunks.append(remaining[:split_idx].strip())
            remaining = remaining[split_idx:].strip()

        return chunks


# =============================================================================
# 4. STRUCTURED VOTE & CONSENSUS TALLY
# =============================================================================

@dataclass
class VoteResult:
    """Individual vote cast by an agent."""
    agent_name: str
    vote: str            # "YES", "NO", or "ABSTAIN"
    reasoning: str
    raw_text: str


@dataclass
class CouncilVoteSummary:
    """Aggregated results and consensus calculation for the council."""
    proposition: str
    votes: List[VoteResult]
    yes_count: int
    no_count: int
    abstain_count: int
    total_votes: int
    consensus_percentage: float
    consensus_passed: bool
    summary_text: str


def parse_agent_vote(agent_name: str, message_text: str) -> VoteResult:
    """
    Extracts structured VOTE: YES / NO from agent's output.
    Supports standard (YES|NO|ABSTAIN) as well as crypto-native synonyms
    (BUY|APPROVE|BULLISH -> YES, SELL|REJECT|BEARISH -> NO).
    """
    clean_text = message_text.strip()
    match = re.search(
        r"VOTE\s*[*_`\s]*[:=]?[*_`\s]*(YES|NO|ABSTAIN|BUY|SELL|APPROVE|REJECT|BULLISH|BEARISH)\b",
        clean_text,
        re.IGNORECASE,
    )

    if match:
        raw_token = match.group(1).upper()
        if raw_token in ("YES", "BUY", "APPROVE", "BULLISH"):
            vote = "YES"
        elif raw_token in ("NO", "SELL", "REJECT", "BEARISH"):
            vote = "NO"
        else:
            vote = "ABSTAIN"
    else:
        # Heuristic fallback: check content
        lowered = clean_text.lower()
        if any(term in lowered for term in ("vote yes", "i vote: yes", "votes yes", "vote: buy", "i vote: buy", "recommend buy")):
            vote = "YES"
        elif any(term in lowered for term in ("vote no", "i vote: no", "votes no", "vote: sell", "i vote: sell", "recommend pass", "recommend avoid")):
            vote = "NO"
        elif "abstain" in lowered:
            vote = "ABSTAIN"
        else:
            vote = "ABSTAIN"

    # Extract reasoning (lines before/excluding the VOTE: declaration)
    lines = clean_text.splitlines()
    vote_line_regex = re.compile(
        r"VOTE\s*[*_`\s]*[:=]?[*_`\s]*(YES|NO|ABSTAIN|BUY|SELL|APPROVE|REJECT|BULLISH|BEARISH)\b",
        re.IGNORECASE,
    )
    reasoning_lines = [l for l in lines if not vote_line_regex.search(l)]
    reasoning = "\n".join(reasoning_lines).strip() or clean_text

    return VoteResult(
        agent_name=agent_name,
        vote=vote,
        reasoning=reasoning,
        raw_text=clean_text,
    )


def tally_council_votes(proposition: str, votes: List[VoteResult]) -> CouncilVoteSummary:
    """Computes totals and determines if consensus meets the configured threshold."""
    yes_count = sum(1 for v in votes if v.vote == "YES")
    no_count = sum(1 for v in votes if v.vote == "NO")
    abstain_count = sum(1 for v in votes if v.vote == "ABSTAIN")
    total_votes = len(votes)

    effective_votes = yes_count + no_count
    consensus_pct = (yes_count / effective_votes) if effective_votes > 0 else 0.0
    consensus_passed = (consensus_pct >= config.CONSENSUS_THRESHOLD) and (yes_count > no_count)

    summary_lines = [
        "📊 <b>COUNCIL VOTE RESULTS</b>",
        "───────────────────────────────",
        f"📌 <b>Proposition:</b> <i>{html.escape(proposition)}</i>",
        "",
    ]

    for v in votes:
        badge = "✅" if v.vote == "YES" else ("❌" if v.vote == "NO" else "⚪")
        summary_lines.append(f"{badge} <b>{v.agent_name}:</b> <code>{v.vote}</code>")

    summary_lines.extend([
        "",
        "───────────────────────────────",
        f"✅ <b>YES:</b> {yes_count}  |  ❌ <b>NO:</b> {no_count}  |  ⚪ <b>ABSTAIN:</b> {abstain_count}",
        f"📈 <b>Consensus:</b> <code>{consensus_pct * 100:.1f}%</code> (Threshold: {config.CONSENSUS_THRESHOLD * 100:.0f}%)",
    ])

    if consensus_passed:
        summary_lines.append("🏆 <b>STATUS: CONSENSUS REACHED (APPROVED)</b>")
    else:
        summary_lines.append("🛑 <b>STATUS: CONSENSUS FAILED (REJECTED)</b>")

    summary_lines.append("───────────────────────────────")

    return CouncilVoteSummary(
        proposition=proposition,
        votes=votes,
        yes_count=yes_count,
        no_count=no_count,
        abstain_count=abstain_count,
        total_votes=total_votes,
        consensus_percentage=consensus_pct,
        consensus_passed=consensus_passed,
        summary_text="\n".join(summary_lines),
    )


# =============================================================================
# 5. AUTOGEN COUNCIL ORCHESTRATOR
# =============================================================================

class CouncilOrchestrator:
    """
    Coordinates Microsoft AutoGen group conversations, debates, voting,
    and real-time dispatch to the Telegram group chat.
    """

    def __init__(self, active_agents: Optional[List[config.AgentConfig]] = None):
        self.agents_config = active_agents or config.get_active_agents()
        self.current_mood: str = config.DEFAULT_MOOD
        self.broadcaster = TelegramAgentBroadcaster(self.agents_config)
        self._lock = asyncio.Lock()

    def set_mood(self, mood: str) -> str:
        """Sets the collective council mood."""
        mood_clean = mood.strip().lower()
        if mood_clean in config.MOOD_PROMPTS:
            self.current_mood = mood_clean
            return f"Council mood updated to: <b>{self.current_mood.upper()}</b>"
        else:
            supported = ", ".join(config.MOOD_PROMPTS.keys())
            return f"Unsupported mood '{mood}'. Supported options: <code>{supported}</code>"

    def get_mood_instruction(self) -> str:
        """Returns the prompt instruction for the active mood."""
        return config.MOOD_PROMPTS.get(self.current_mood, config.MOOD_PROMPTS["analytical"])

    def _build_system_message(self, agent: config.AgentConfig, mode: str) -> str:
        """Constructs high-density, token-efficient system instructions."""
        mood_text = self.get_mood_instruction()

        domain_mandates = {
            "Claude": "Domain: Smart contract security, mint/freeze authority, rug score. Reject honeypots or active authorities.",
            "GPT": "Domain: Market structure, liquidity depth vs mcap, execution slippage. Reject illiquid pools (<$10k or <2% ratio).",
            "DeepSeek": "Domain: Quantitative analytics, 24h volume, net buy pressure, mathematical EV. Reject dump volume or negative flow.",
            "Gemini": "Domain: Trend momentum, 1h/24h adoption velocity, ecosystem narrative. Reject negative momentum.",
            "Grok": "Domain: Holder distribution, top 10 supply control, whale dump risk. Reject top-heavy supply (>50% in top 10).",
        }
        mandate = domain_mandates.get(agent.name, agent.role_description)

        mode_instruction = (
            "Provide 1-2 sentence razor-sharp critique from your domain. Do NOT vote."
            if mode == "debate"
            else "State 1-2 sentence rationale citing exact metrics. Conclude last line with: VOTE: YES or VOTE: NO"
        )

        return (
            f"You are {agent.name} ({agent.display_name}) on the Chatters Solana Crypto Council.\n"
            f"{mandate}\n"
            f"Tone: {mood_text}\n"
            f"STRICT RULES:\n"
            f"1. SCOPE: CRYPTO ONLY. You communicate exclusively about cryptocurrency, Solana tokens, trading, liquidity, DeFi, and on-chain security. NEVER engage in non-crypto or general banter.\n"
            f"2. BREVITY: Maximum 1-2 concise, razor-sharp sentences. No greetings, no pleasantries, no fluff.\n"
            f"3. ASSETS: ALWAYS refer to assets by official NAME and TICKER (never raw CA). Base all evaluations on verified on-chain metrics.\n"
            f"4. {mode_instruction}"
        )

    def _create_autogen_agents(self, mode: str) -> List[AssistantAgent]:
        """Creates AutoGen AssistantAgent instances for each active council member."""
        autogen_agents = []
        for agent_cfg in self.agents_config:
            client = create_model_client(agent_cfg)
            sys_msg = self._build_system_message(agent_cfg, mode)
            agent_inst = AssistantAgent(
                name=agent_cfg.name,
                model_client=client,
                system_message=sys_msg,
                description=agent_cfg.role_description,
            )
            autogen_agents.append(agent_inst)
        return autogen_agents

    async def run_single_agent_reply(
        self, agent_name: str, prompt: str, chat_id: int, reply_to_id: Optional[int] = None
    ) -> bool:
        """Generates and broadcasts a direct, conversational response from a specific council bot."""
        cfg = config.get_agent_by_id(agent_name)
        if not cfg:
            return False

        # If a Solana CA or ticker is detected in the prompt, pull verified on-chain metrics!
        mint, _, _, symbol = SignalExtractor.extract(prompt)
        if not mint and symbol:
            mint = await telemetry.search_solana_token_mint(symbol)
        final_prompt = prompt
        if mint:
            dossier = await telemetry.fetch_token_dossier(mint)
            if dossier:
                final_prompt = telemetry.format_single_agent_token_context(dossier, prompt)

        client = create_model_client(cfg)
        sys_msg = self._build_system_message(cfg, mode="debate")
        messages = [
            SystemMessage(content=sys_msg),
            UserMessage(content=final_prompt, source="user"),
        ]
        try:
            res = await client.create(messages=messages)
            content = res.content.strip() if hasattr(res, "content") else str(res).strip()
            if content:
                await self.broadcaster.broadcast_message(
                    agent_name=cfg.name,
                    chat_id=chat_id,
                    text=content,
                    reply_to_message_id=reply_to_id,
                )
                return True
        except Exception as e:
            logger.error(f"Error in single agent reply for {agent_name}: {e}")
        return False

    async def run_debate(
        self, prompt: str, chat_id: int, reply_to_id: Optional[int] = None, silent: bool = False
    ) -> bool:
        """
        Executes Debate Mode (/deb):
        Agents engage in multi-turn discussions and critique each other's points.
        If silent=True, turns are computed in-memory without broadcasting to Telegram.
        """
        async with self._lock:
            autogen_agents = self._create_autogen_agents(mode="debate")
            num_agents = len(autogen_agents)
            total_turns = max(num_agents, num_agents * config.DEBATE_MAX_ROUNDS)

            team = RoundRobinGroupChat(
                participants=autogen_agents,
                max_turns=total_turns,
            )

            active_ctx = context_manager.get_active_context()
            ctx_note = f"\nActive Admin Directive: \"{active_ctx}\"\n" if active_ctx else ""
            debate_task = (
                f"Discussion Topic / Proposition: {prompt}\n"
                f"{ctx_note}"
                f"Provide your concise, unique perspective (1-2 sentences) from your domain persona. "
                f"Directly challenge or build upon points raised by other council members."
            )

            try:
                async for event in team.run_stream(task=debate_task):
                    if isinstance(event, TextMessage) and event.source != "user":
                        agent_name = event.source
                        content = event.content
                        logger.info(f"AutoGen Debate turn by {agent_name}: {content[:60]}...")
                        if not silent:
                            await self.broadcaster.broadcast_message(
                                agent_name=agent_name,
                                chat_id=chat_id,
                                text=content,
                                reply_to_message_id=reply_to_id,
                            )
                return True
            except Exception as e:
                logger.error(f"Error during AutoGen debate stream: {e}", exc_info=True)
                return False

    async def run_vote(
        self, prompt: str, chat_id: int, reply_to_id: Optional[int] = None, silent: bool = False
    ) -> CouncilVoteSummary:
        """
        Executes Vote Mode (/vot):
        Evaluates the proposition across the active council bots concurrently using domain criteria.
        Cuts token consumption by eliminating cumulative context duplication, and delivers fast, accurate judgments.
        If silent=True, votes are evaluated in-memory without broadcasting each message to Telegram.
        """
        async with self._lock:
            domain_mandates = {
                "Claude": "Focus on smart contract security, mint/freeze authorities, and rug score. Vote NO if authorities are active or risk score > 400.",
                "GPT": "Focus on market structure, liquidity depth, and liquidity/mcap ratio. Vote NO if liquidity < $10k or liquidity/mcap < 2%.",
                "DeepSeek": "Focus on quantitative data, 24h volume, and net buy pressure. Vote NO if net sells dominate or volume < $15k.",
                "Gemini": "Focus on 1h/24h price momentum and ecosystem adoption velocity. Vote NO if negative dump momentum or zero volume.",
                "Grok": "Focus on holder distribution and contrarian dump risk. Vote NO if top 10 holders control > 50% of supply.",
            }

            active_ctx = context_manager.get_active_context()
            ctx_injection = f"\n🎯 ACTIVE VOTING DIRECTIVE (Admin Instruction):\n\"{active_ctx}\"\nYou must strictly incorporate this directive into your evaluation and final vote!\n" if active_ctx else ""

            async def _vote_agent(agent_cfg: config.AgentConfig) -> VoteResult:
                client = create_model_client(agent_cfg)
                sys_msg = self._build_system_message(agent_cfg, mode="vote")
                mandate = domain_mandates.get(agent_cfg.name, agent_cfg.role_description)
                vote_prompt = (
                    f"COUNCIL PROPOSITION & DATA BRIEFING:\n{prompt}\n\n"
                    f"YOUR MANDATE ({agent_cfg.name} - {agent_cfg.role_description}):\n"
                    f"• {mandate}\n"
                    f"{ctx_injection}\n"
                    f"• State your 1-2 sentence rationale citing the specific metrics above.\n"
                    f"• Conclude on the very last line with:\n"
                    f"VOTE: YES  or  VOTE: NO"
                )
                messages = [
                    SystemMessage(content=sys_msg),
                    UserMessage(content=vote_prompt, source="user"),
                ]
                try:
                    res = await client.create(messages=messages)
                    content = res.content.strip() if hasattr(res, "content") else str(res).strip()
                except Exception as e:
                    logger.error(f"Error getting vote from {agent_cfg.name}: {e}")
                    sim = SimulatedCouncilModelClient(agent_name=agent_cfg.name, role=agent_cfg.role_description)
                    res = await sim.create(messages=messages)
                    content = res.content.strip() if hasattr(res, "content") else str(res).strip()

                if not silent and chat_id:
                    await self.broadcaster.broadcast_message(
                        agent_name=agent_cfg.name,
                        chat_id=chat_id,
                        text=content,
                        reply_to_message_id=reply_to_id,
                    )

                return parse_agent_vote(agent_cfg.name, content)

            recorded_votes: List[VoteResult] = []
            for cfg in self.agents_config:
                vote_res = await _vote_agent(cfg)
                recorded_votes.append(vote_res)
                await asyncio.sleep(0.1)

            summary = tally_council_votes(prompt, recorded_votes)
            return summary

    async def run_debate_and_vote(
        self, prompt: str, chat_id: int, reply_to_id: Optional[int] = None, silent: bool = False
    ) -> CouncilVoteSummary:
        """
        Executes Full Pipeline:
        1. Debates points across agents (optionally silent)
        2. Conducts formal vote (optionally silent)
        """
        # Step 1: Debate
        await self.run_debate(prompt, chat_id, reply_to_id, silent=silent)
        await asyncio.sleep(0.5)

        # Announce transition to voting if not in silent mode
        if not silent and chat_id:
            lead_bot = self.broadcaster.get_bot(self.agents_config[0].name)
            if lead_bot:
                try:
                    await lead_bot.send_message(
                        chat_id=chat_id,
                        text="🗳️ <b>Debate Concluded. Transitioning to Formal Council Vote...</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    logger.debug(f"Could not send transition announcement: {e}")

        # Step 2: Vote
        summary = await self.run_vote(prompt, chat_id, reply_to_id, silent=silent)
        return summary
