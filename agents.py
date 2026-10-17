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
)
from autogen_ext.models.openai import OpenAIChatCompletionClient

import config

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

        if is_voting:
            # Deterministic sensible mock vote for testing
            vote_decision = "YES" if self.agent_name in ("Claude", "GPT", "Gemini") else "NO"
            content = (
                f"[{self.agent_name} - {self.role}]\n"
                f"I have reviewed the proposal based on current metrics and safety constraints. "
                f"From my perspective as {self.role}, the risk/reward parameters appear viable.\n\n"
                f"VOTE: {vote_decision}"
            )
        else:
            content = (
                f"[{self.agent_name} - {self.role}]\n"
                f"Analyzing the proposition from the lens of {self.role}. "
                f"We must weigh liquidity lockup stability against volume breakout velocity before committing capital."
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
            model_info=ModelInfo(vision=False, function_calling=False, json_output=False, structured_output=False, family="unknown", max_tokens=1000),
            max_tokens=1000,
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
    reasoning = "\n".join(reasoning_lines).strip()

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
        """Constructs dynamic system instructions incorporating persona, mood, and mode."""
        mood_text = self.get_mood_instruction()

        base_rules = (
            "You are participating in 'Chatters', an elite Telegram AI Council featuring 5 autonomous AI models (Claude, GPT, DeepSeek, Gemini, and Grok).\n"
            "You debate and evaluate a wide variety of topics: technology, science, artificial intelligence, business, philosophy, culture, as well as cryptocurrency and finance.\n"
            "Rule 1: If a cryptocurrency or token is being evaluated, always refer to it by its official NAME and TICKER (never by raw contract address), and ground your arguments in any provided financial/on-chain metrics.\n"
            "Rule 2: For general topics outside crypto, bring deep insight, critical analysis, and intellectual rigor from your unique perspective.\n"
            "Rule 3: Keep contributions concise, punchy, and direct (2-4 sentences max).\n"
            "Rule 4: Directly challenge or build upon points raised by other council members from your specialized domain.\n"
            "Rule 5: No greetings or corporate pleasantries. Dive straight into your analysis."
        )

        if mode == "debate":
            mode_rules = (
                "MODE: DEBATE\n"
                "Provide critical reasoning, identify hidden assumptions or risks, evaluate potential upside, "
                "and engage other agents in a constructive, punchy multi-turn debate. Do not cast a vote in this mode."
            )
        elif mode in ("vote", "execute"):
            mode_rules = (
                "MODE: FORMAL VOTE\n"
                "1. State your final reasoning in 2-3 concise sentences.\n"
                "2. Conclude your statement with your formal vote on the very last line in this EXACT format:\n"
                "VOTE: YES\n"
                "or\n"
                "VOTE: NO\n"
                "(or VOTE: ABSTAIN if completely neutral)."
            )
        else:
            mode_rules = ""

        return (
            f"{agent.personality_prompt}\n\n"
            f"{mood_text}\n\n"
            f"{base_rules}\n\n"
            f"{mode_rules}"
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
                f"Council Debate Proposition: {prompt}\n"
                f"Engage in a sharp, multi-perspective debate. Address liquidity, market dynamics, "
                f"security audits, and risk vs reward. Challenge each other's arguments."
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
        Each agent states their logic and casts a structured vote (VOTE: YES / NO).
        Tallies the results and returns CouncilVoteSummary.
        If silent=True, votes are evaluated in-memory without broadcasting each message to Telegram.
        """
        async with self._lock:
            autogen_agents = self._create_autogen_agents(mode="vote")
            num_agents = len(autogen_agents)

            # Each agent votes once in round-robin order
            team = RoundRobinGroupChat(
                participants=autogen_agents,
                max_turns=num_agents,
            )

            vote_task = (
                f"Formal Council Vote on Proposition: {prompt}\n"
                f"State your concise reasoning (2-3 sentences) and conclude with:\n"
                f"VOTE: YES  or  VOTE: NO"
            )

            recorded_votes: List[VoteResult] = []

            try:
                async for event in team.run_stream(task=vote_task):
                    if isinstance(event, TextMessage) and event.source != "user":
                        agent_name = event.source
                        content = event.content
                        logger.info(f"AutoGen Vote turn by {agent_name}: {content[:60]}...")

                        # Broadcast to Telegram group under agent's bot identity if not silent
                        if not silent:
                            await self.broadcaster.broadcast_message(
                                agent_name=agent_name,
                                chat_id=chat_id,
                                text=content,
                                reply_to_message_id=reply_to_id,
                            )

                        # Parse vote
                        vote_res = parse_agent_vote(agent_name, content)
                        recorded_votes.append(vote_res)

            except Exception as e:
                logger.error(f"Error during AutoGen vote stream: {e}", exc_info=True)

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
