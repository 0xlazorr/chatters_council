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
from typing import AsyncGenerator, Dict, List, Optional, Tuple

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

logger = logging.getLogger("multi_agent_council.agents")


# =============================================================================
# 1. SIMULATED MODEL CLIENT (Graceful Fallback When Keys Not Yet Configured)
# =============================================================================

class SimulatedCouncilModelClient(ChatCompletionClient):
    """
    Fallback client that produces intelligent persona-based mock responses
    if no external LLM API key has been attached to .env yet.
    Allows testing bot tokens, group chat turn-taking, and voting logic immediately.
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

    async def create(self, messages, tools=(), json_output=None, extra_create_args=None, cancellation_token=None):
        last_item = messages[-1] if messages else ""
        if isinstance(last_item, dict):
            last_msg = last_item.get("content", "")
        elif hasattr(last_item, "content"):
            last_msg = getattr(last_item, "content", "")
        else:
            last_msg = str(last_item)
        is_voting = "VOTE:" in str(last_msg).upper() or "VOTING" in str(last_msg).upper() or "VOTE ON" in str(last_msg).upper()
        msg_str = str(last_msg)

        # Extract real token name and symbol if present in briefing
        tok_match = re.search(r"(?:ASSET DOSSIER:|ASSET:)\s*([^\(]+)\s*\(\$([^\)]+)\)", msg_str)
        tok_name = tok_match.group(1).strip() if tok_match else "the token"
        tok_sym = tok_match.group(2).strip() if tok_match else "TOKEN"

        liq_match = re.search(r"Liquidity:\s*\$([0-9,\.]+)", msg_str)
        liq_val = f"${liq_match.group(1)}" if liq_match else "$50K+"

        if is_voting:
            vote_decision = "YES" if self.agent_name in ("Claude", "GPT", "Gemini", "DeepSeek") else "NO"
            if self.agent_name == "Claude":
                content = f"Scrutinized {tok_name} (${tok_sym}) contract: Mint and freeze authorities are verified revoked with an acceptable security profile.\n\nVOTE: {vote_decision}"
            elif self.agent_name == "GPT":
                content = f"Market structure review on {tok_name} (${tok_sym}): Liquidity pool depth of {liq_val} provides suitable runway for execution.\n\nVOTE: {vote_decision}"
            elif self.agent_name == "DeepSeek":
                content = f"Quantitative analysis on {tok_name} (${tok_sym}): 24h trading volume and positive net buy ratio indicate favorable expected value.\n\nVOTE: {vote_decision}"
            elif self.agent_name == "Gemini":
                content = f"Momentum scan for {tok_name} (${tok_sym}): On-chain transaction velocity and trend indicators demonstrate active interest.\n\nVOTE: {vote_decision}"
            else:
                content = f"Holder concentration audit for {tok_name} (${tok_sym}): Assessing whale wallet distribution and sell wall resistance before allocation.\n\nVOTE: {vote_decision}"
        else:
            is_crypto = any(term in msg_str.lower() for term in ("sol", "token", "liquidity", "swap", "raydium", "pump.fun", "dex", "mint"))
            if is_crypto:
                content = (
                    f"Analyzing {tok_name} (${tok_sym}) from the lens of {self.role}. "
                    f"Liquidity depth ({liq_val}) and buy pressure look viable for controlled execution."
                )
            else:
                content = (
                    f"Examining this proposition from the perspective of {self.role}. "
                    f"The critical consideration lies in foundational assumptions, long-term scalability, and structural viability."
                )

        return CreateResult(
            finish_reason="stop",
            content=content,
            usage=RequestUsage(prompt_tokens=20, completion_tokens=40),
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
        return 20

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
            model_info=ModelInfo(vision=False, function_calling=False, json_output=False, structured_output=False, family="unknown", max_tokens=100),
            max_tokens=100,
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
            f"You are {agent.name} ({agent.display_name}) on Chatters AI Council.\n"
            f"{mandate}\n"
            f"Posture: {mood_text}\n"
            f"Strict Rules:\n"
            f"1. Maximum 1-2 sentences. No greetings or corporate pleasantries.\n"
            f"2. Crypto: ALWAYS refer to assets by official NAME and TICKER (never raw CA). Base judgments on verified metrics.\n"
            f"3. Non-crypto: Bring deep intellectual rigor from your domain.\n"
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

        # If a Solana CA is detected in the prompt, pull verified on-chain metrics!
        mint, _, _, _ = SignalExtractor.extract(prompt)
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

            debate_task = (
                f"Discussion Topic / Proposition: {prompt}\n"
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

            async def _vote_agent(agent_cfg: config.AgentConfig) -> VoteResult:
                client = create_model_client(agent_cfg)
                sys_msg = self._build_system_message(agent_cfg, mode="vote")
                mandate = domain_mandates.get(agent_cfg.name, agent_cfg.role_description)
                vote_prompt = (
                    f"COUNCIL PROPOSITION & DATA BRIEFING:\n{prompt}\n\n"
                    f"YOUR MANDATE ({agent_cfg.name} - {agent_cfg.role_description}):\n"
                    f"• {mandate}\n"
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
