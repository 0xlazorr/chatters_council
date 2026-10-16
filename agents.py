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
from telegram.error import BadRequest, TelegramError
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
from autogen_ext.models.anthropic import AnthropicChatCompletionClient
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
        last_msg = messages[-1].content if messages else ""
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
# 2. MODEL CLIENT FACTORY
# =============================================================================

def create_model_client(agent: config.AgentConfig) -> ChatCompletionClient:
    """
    Instantiates the appropriate AutoGen ChatCompletionClient for an agent.
    Priority:
    1. OpenRouter unified key (if OPENROUTER_API_KEY is present)
    2. Native provider keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, etc.)
    3. Simulated fallback client (if no keys are available yet)
    """
    # 1. OpenRouter Unified Setup
    if config.OPENROUTER_API_KEY:
        logger.info(f"Using OpenRouter for agent {agent.name} (model: {agent.model})")
        return OpenAIChatCompletionClient(
            model=agent.model,
            base_url=config.OPENROUTER_BASE_URL,
            api_key=config.OPENROUTER_API_KEY,
            model_info=ModelInfo(vision=False, function_calling=True, json_output=True, structured_output=True, family="openai"),
            default_headers={
                "HTTP-Referer": "https://telegram.org",
                "X-Title": "Chatters AI Council",
            },
        )

    # 2. Native Provider Routing
    provider = agent.provider.lower()

    if provider == "anthropic" and config.ANTHROPIC_API_KEY:
        logger.info(f"Using native Anthropic client for {agent.name}")
        model_name = "claude-3-5-sonnet-20241022" if "/" in agent.model else agent.model
        return AnthropicChatCompletionClient(
            model=model_name,
            api_key=config.ANTHROPIC_API_KEY,
        )

    if provider == "openai" and config.OPENAI_API_KEY:
        logger.info(f"Using native OpenAI client for {agent.name}")
        model_name = "gpt-4o" if "/" in agent.model else agent.model
        return OpenAIChatCompletionClient(
            model=model_name,
            base_url=config.OPENAI_BASE_URL,
            api_key=config.OPENAI_API_KEY,
        )

    if provider == "deepseek" and config.DEEPSEEK_API_KEY:
        logger.info(f"Using native DeepSeek client for {agent.name}")
        model_name = "deepseek-chat" if "/" in agent.model else agent.model
        return OpenAIChatCompletionClient(
            model=model_name,
            base_url="https://api.deepseek.com",
            api_key=config.DEEPSEEK_API_KEY,
        )

    if provider == "gemini" and config.GEMINI_API_KEY:
        logger.info(f"Using native Google Gemini OpenAI-compatible client for {agent.name}")
        model_name = "gemini-2.5-flash" if "/" in agent.model else agent.model
        return OpenAIChatCompletionClient(
            model=model_name,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=config.GEMINI_API_KEY,
        )

    if provider == "grok" and config.GROK_API_KEY:
        logger.info(f"Using native xAI Grok client for {agent.name}")
        model_name = "grok-2" if "/" in agent.model else agent.model
        return OpenAIChatCompletionClient(
            model=model_name,
            base_url="https://api.x.ai/v1",
            api_key=config.GROK_API_KEY,
        )

    if provider == "mistral" and config.MISTRAL_API_KEY:
        logger.info(f"Using native Mistral client for {agent.name}")
        model_name = "mistral-large-latest" if "/" in agent.model else agent.model
        return OpenAIChatCompletionClient(
            model=model_name,
            base_url="https://api.mistral.ai/v1",
            api_key=config.MISTRAL_API_KEY,
        )

    # 3. Fallback to OpenAI Key if set
    if config.OPENAI_API_KEY:
        return OpenAIChatCompletionClient(
            model="gpt-4o-mini",
            api_key=config.OPENAI_API_KEY,
        )

    # 4. Fallback to Simulated Mode with alert
    logger.warning(
        f"No API key found for agent {agent.name}. Using SimulatedCouncilModelClient. "
        f"Add OPENROUTER_API_KEY or provider keys to .env for live LLM generation."
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

            # 2. Attempt sending with Markdown formatting
            sent = False
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_to_message_id=reply_to_message_id,
                )
                sent = True
            except BadRequest as e:
                # If markdown parsing fails due to unescaped symbols, fallback to plain text
                logger.debug(f"Markdown send failed for {agent_name} ({e}); retrying as plain text.")
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode=None,
                        reply_to_message_id=reply_to_message_id,
                    )
                    sent = True
                except Exception as inner_e:
                    logger.error(f"Failed to send plain text message for {agent_name}: {inner_e}")
            except TelegramError as te:
                logger.error(f"Telegram error sending message for {agent_name}: {te}")
            except Exception as ex:
                logger.error(f"Unexpected error broadcasting message for {agent_name}: {ex}")

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
    Looks for pattern 'VOTE: <YES|NO|ABSTAIN>'.
    """
    clean_text = message_text.strip()
    match = re.search(r"VOTE\s*[*_`\s]*[:=]?[*_`\s]*(YES|NO|ABSTAIN)\b", clean_text, re.IGNORECASE)

    if match:
        vote = match.group(1).upper()
    else:
        # Heuristic fallback: check last lines
        lowered = clean_text.lower()
        if "vote yes" in lowered or "i vote: yes" in lowered or "votes yes" in lowered:
            vote = "YES"
        elif "vote no" in lowered or "i vote: no" in lowered or "votes no" in lowered:
            vote = "NO"
        elif "abstain" in lowered:
            vote = "ABSTAIN"
        else:
            vote = "ABSTAIN"

    # Extract reasoning (lines before the VOTE: declaration)
    lines = clean_text.splitlines()
    reasoning_lines = [l for l in lines if not re.search(r"VOTE\s*[*_`\s]*[:=]?[*_`\s]*(YES|NO|ABSTAIN)\b", l, re.IGNORECASE)]
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
            "You are participating in an interactive Telegram Group Chat Council debate.\n"
            "Rule 1: Keep contributions concise, punchy, and direct (2-4 sentences max).\n"
            "Rule 2: Directly reference or challenge points raised by other council members.\n"
            "Rule 3: Maintain your distinct analytical persona and domain perspective.\n"
            "Rule 4: Do NOT repeat greetings or corporate pleasantries. Dive straight into your analysis."
        )

        if mode == "debate":
            mode_rules = (
                "MODE: DEBATE\n"
                "Provide critical reasoning, identify hidden risks, evaluate upside, and engage "
                "other agents in a constructive multi-turn debate. Do not cast a vote in this mode."
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
        self, prompt: str, chat_id: int, reply_to_id: Optional[int] = None
    ) -> bool:
        """
        Executes Debate Mode (/deb):
        Agents engage in multi-turn discussions and critique each other's points.
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
        self, prompt: str, chat_id: int, reply_to_id: Optional[int] = None
    ) -> CouncilVoteSummary:
        """
        Executes Vote Mode (/vot):
        Each agent states their logic and casts a structured vote (VOTE: YES / NO).
        Tallies the results and returns CouncilVoteSummary.
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

                        # Broadcast to Telegram group under agent's bot identity
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
        self, prompt: str, chat_id: int, reply_to_id: Optional[int] = None
    ) -> CouncilVoteSummary:
        """
        Executes Full Pipeline (/dve):
        1. Debates points across agents
        2. Conducts formal vote
        """
        # Step 1: Debate
        await self.run_debate(prompt, chat_id, reply_to_id)
        await asyncio.sleep(1.0)

        # Announce transition to voting
        lead_bot = self.broadcaster.get_bot(self.agents_config[0].name)
        if lead_bot:
            await lead_bot.send_message(
                chat_id=chat_id,
                text="🗳️ <b>Debate Concluded. Transitioning to Formal Council Vote...</b>",
                parse_mode=ParseMode.HTML,
            )

        # Step 2: Vote
        summary = await self.run_vote(prompt, chat_id, reply_to_id)
        return summary
