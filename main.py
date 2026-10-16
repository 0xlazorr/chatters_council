"""
main.py - Main Entrypoint & Command Controller for Multi-Agent Telegram Council.

Orchestrates:
- Automatic startup synchronization of display names ({AI}talks via setMyName)
- Telegram group command handling (/deb, /vot, /exe, /dve, /mood, /status, /help)
- Admin whitelist validation & automatic GROUP_CHAT_ID detection
- Concurrency locks & background dispatch to AutoGen and MSniffer executor
"""

import asyncio
import html
import logging
import os
import sys
import time
import socket
from typing import Any, Dict, List, Optional, Tuple

# Prefer IPv4 for Telegram API to bypass IPv6 routing timeouts on Linux
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4_pref(host, port, family=0, type=0, proto=0, flags=0):
    if host and "telegram.org" in str(host):
        return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _getaddrinfo_ipv4_pref

import urllib.request
import urllib.parse
import json

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from agents import CouncilOrchestrator
from executor import MSnifferExecutor, format_execution_announcement

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("multi_agent_council.main")

# Global Council Orchestrator instance
council: Optional[CouncilOrchestrator] = None

# Active operation state lock
is_busy: bool = False
active_task_name: str = ""


# =============================================================================
# 1. STARTUP UTILITY: SETMYNAME & BOT DISCOVERY
# =============================================================================

async def sync_bot_display_names(agents: List[config.AgentConfig]) -> dict:
    """
    Calls the Telegram Bot API (setMyName) for each bot token to automatically
    rename display names to follow the pattern {AI}talks (e.g. Claudetalks, GPTtalks).
    Also queries getMe to verify bot connectivity and group privacy settings.
    """
    logger.info("=" * 65)
    logger.info("🤖 INITIATING TELEGRAM BOT STARTUP & DISPLAY NAME SYNCHRONIZATION")
    logger.info("=" * 65)

    results = {}
    for agent in agents:
        if not agent.token:
            continue

        try:
            req = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0)
            bot = Bot(token=agent.token, request=req)
            me = await bot.get_me()

            # 1. Ensure display name follows {AI}talks
            desired_name = agent.display_name
            current_name = me.first_name

            rename_status = "already matches"
            if current_name != desired_name:
                try:
                    renamed = await bot.set_my_name(name=desired_name)
                    rename_status = "renamed successfully" if renamed else "rename returned false"
                except Exception as ne:
                    rename_status = f"rename failed: {ne}"

            # 2. Check Group Privacy setting
            privacy_note = (
                "⚠️ Group Privacy ON (Bot may only see slash commands or admin messages)"
                if not me.can_read_all_group_messages
                else "✅ Group Privacy OFF (Bot reads all group messages)"
            )

            logger.info(
                f"[{agent.name}] @{me.username} (ID: {me.id}) -> "
                f"Display: '{desired_name}' ({rename_status}) | {privacy_note}"
            )

            results[agent.name] = {
                "id": me.id,
                "username": me.username,
                "name": desired_name,
                "can_read_all_group_messages": me.can_read_all_group_messages,
                "status": "online",
            }

        except Exception as e:
            logger.error(f"[{agent.name}] Failed to verify bot token: {e}")
            results[agent.name] = {"status": "error", "error": str(e)}

    logger.info("=" * 65)
    return results


# =============================================================================
# 2. AUTHORIZATION & GROUP CHAT VALIDATOR
# =============================================================================

def is_authorized_admin(update: Update) -> Tuple[bool, str]:
    """Validates that the user sending the command is in the ADMIN_USER_IDS whitelist."""
    user = update.effective_user
    if not user:
        return False, "Unknown user"

    # If no admin IDs are configured, allow group members to trigger
    if not config.ADMIN_USER_IDS:
        return True, "No admin whitelist enforced"

    if user.id in config.ADMIN_USER_IDS:
        return True, "Authorized"

    return False, f"Unauthorized: User ID {user.id} is not in ADMIN_USER_IDS whitelist."


def check_and_record_chat_id(update: Update) -> int:
    """Detects and logs the current group chat ID, binding to the active group."""
    chat = update.effective_chat
    if not chat:
        return 0

    chat_id = chat.id
    chat_title = getattr(chat, "title", "") or "Private/Direct"

    if config.GROUP_CHAT_ID == 0:
        config.GROUP_CHAT_ID = chat_id
        logger.info(f"💡 AUTO-BOUND TO TELEGRAM GROUP: '{chat_title}' (ID: {chat_id})")
        if chat_title.lower() == "chatters":
            logger.info("🎯 CONFIRMED: Active group is 'Chatters'!")
    return chat_id


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    parse_mode=ParseMode.HTML,
    disable_web_page_preview: bool = False,
) -> bool:
    """Sends a message safely with fallback to plain text if HTML/Markdown parsing fails."""
    if not bot or not chat_id or not text:
        return False
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return True
    except BadRequest as e:
        logger.debug(f"Formatting failed ({e}); falling back to plain text.")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=None,
                disable_web_page_preview=disable_web_page_preview,
            )
            return True
        except Exception as inner_e:
            logger.error(f"Failed to send plain text message: {inner_e}")
            return False
    except Exception as ex:
        logger.error(f"Unexpected error in safe_send_message: {ex}")
        return False


# =============================================================================
# 3. COMMAND HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command."""
    chat_id = check_and_record_chat_id(update)
    user = update.effective_user
    first_name = html.escape(user.first_name) if user else "Admin"

    text = (
        f"🏛️ <b>Multi-Agent AI Council is Online!</b>\n\n"
        f"Welcome, {first_name}. The AI Council allows 5-15 autonomous AI models "
        f"to debate, vote, and execute tasks directly inside this Telegram group chat.\n\n"
        f"<b>Shorthand Commands:</b>\n"
        f"• <code>/deb &lt;prompt&gt;</code> - Debate Mode (multi-turn discussion)\n"
        f"• <code>/vot &lt;prompt&gt;</code> - Vote Mode (structured logic & tally)\n"
        f"• <code>/exe &lt;prompt&gt;</code> - Vote & Execute Mode (consensus triggers MSniffer)\n"
        f"• <code>/dve &lt;prompt&gt;</code> - Full Pipeline (Debate ➔ Vote ➔ Execute)\n"
        f"• <code>/mood &lt;type&gt;</code> - Switch mood (e.g. skeptical, aggressive, ruthless)\n"
        f"• <code>/status</code> - View active council roster & endpoints\n"
        f"• <code>/help</code> - Detailed command reference\n\n"
        f"📍 <i>Current Chat ID:</i> <code>{chat_id}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /help command."""
    text = (
        "📖 <b>AI Council Command Reference</b>\n"
        "───────────────────────────────\n\n"
        "<b>🗣️ Operational Modes:</b>\n"
        "• <code>/deb &lt;prompt&gt;</code>\n"
        "  <i>Debate Mode:</i> Agents engage in a multi-turn discussion, challenging each other's points.\n"
        "  <i>Example:</i> <code>/deb Is Solana infrastructure mature enough for high-frequency trading?</code>\n\n"
        "• <code>/vot &lt;prompt&gt;</code>\n"
        "  <i>Vote Mode:</i> Agents state their reasoning in 2-3 sentences and cast formal YES/NO votes. "
        "The system tallies and announces final consensus.\n"
        "  <i>Example:</i> <code>/vot Approve 0.5 SOL allocation to pump.fun breakout token?</code>\n\n"
        "• <code>/exe &lt;prompt&gt;</code>\n"
        "  <i>Vote & Execute Mode:</i> Agents vote on a proposed contract address or task. If majority "
        "consensus passes, the MSniffer crypto bot is triggered automatically.\n"
        "  <i>Example:</i> <code>/exe Buy 0.25 SOL of DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263</code>\n\n"
        "• <code>/dve &lt;prompt&gt;</code>\n"
        "  <i>Debate, Vote & Execute Mode:</i> The complete institutional pipeline: full debate first, "
        "followed by structured voting, followed by automatic execution if approved.\n"
        "  <i>Example:</i> <code>/dve Evaluate DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 and buy 0.5 SOL if safe</code>\n\n"
        "<b>🎭 Personality & Moods:</b>\n"
        "• <code>/mood &lt;mood_type&gt;</code>\n"
        "  Available: <code>analytical</code>, <code>skeptical</code>, <code>aggressive</code>, "
        "<code>conservative</code>, <code>ruthless</code>, <code>contrarian</code>\n\n"
        "<b>⚙️ Management & Utility:</b>\n"
        "• <code>/status</code> - Display active models, mood, and connectivity\n"
        "• <code>/setnames</code> - Re-sync bot display names to {AI}talks pattern\n"
        "• <code>/cancel</code> - Abort any active council session"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /status command."""
    chat_id = check_and_record_chat_id(update)
    active_agents = config.get_active_agents()

    backend_desc = "OpenRouter Unified" if config.OPENROUTER_API_KEY else "Native Provider API Keys"
    if not config.OPENROUTER_API_KEY and not config.OPENAI_API_KEY and not config.ANTHROPIC_API_KEY:
        backend_desc = "Simulated Fallback Mode (No LLM keys in .env)"

    lines = [
        "🏛️ <b>AI COUNCIL SYSTEM STATUS</b>",
        "───────────────────────────────",
        f"🤖 <b>Active Agents:</b> <code>{len(active_agents)}/15</code>",
        f"🎭 <b>Current Mood:</b> <code>{council.current_mood.upper() if council else 'ANALYTICAL'}</code>",
        f"🧠 <b>LLM Backend:</b> <code>{backend_desc}</code>",
        f"📍 <b>Group Chat ID:</b> <code>{config.GROUP_CHAT_ID or chat_id}</code>",
        f"⚡ <b>Consensus Threshold:</b> <code>{config.CONSENSUS_THRESHOLD * 100:.0f}%</code>",
        f"🐕 <b>MSniffer Mode:</b> <code>{config.MSNIFFER_DISPATCH_MODE.upper()}</code>",
        "",
        "<b>Agent Roster:</b>",
    ]

    for idx, agent in enumerate(active_agents, 1):
        lines.append(f"{idx}. <b>{agent.display_name}</b> (@{agent.username}) | <i>{agent.model}</i>")

    lines.append("───────────────────────────────")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def mood_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /mood <mood_type>."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    args = context.args
    if not args:
        supported = ", ".join(config.MOOD_PROMPTS.keys())
        await update.message.reply_text(
            f"🎭 <b>Usage:</b> <code>/mood &lt;mood_type&gt;</code>\n"
            f"Supported moods: <code>{supported}</code>\n"
            f"Current mood: <b>{council.current_mood.upper() if council else 'ANALYTICAL'}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    requested_mood = args[0].strip().lower()
    res_text = council.set_mood(requested_mood) if council else f"Council not initialized."
    await update.message.reply_text(res_text, parse_mode=ParseMode.HTML)


async def setnames_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /setnames command to manually refresh display names."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    msg = await update.message.reply_text("🔄 Synchronizing bot display names via Telegram API...")
    results = await sync_bot_display_names(config.get_active_agents())

    summary_lines = ["✅ <b>Display Names Synchronized:</b>"]
    for name, data in results.items():
        st = data.get("status", "unknown")
        uname = data.get("username", "")
        summary_lines.append(f"• <b>{name}</b> (@{uname}): <code>{st}</code>")

    await msg.edit_text("\n".join(summary_lines), parse_mode=ParseMode.HTML)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels active operation if stuck."""
    global is_busy, active_task_name
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    if is_busy:
        is_busy = False
        old_task = active_task_name
        active_task_name = ""
        await update.message.reply_text(f"🛑 Cancelled active session: <b>{old_task}</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("ℹ️ No active session is currently running.")


# =============================================================================
# 4. OPERATIONAL PIPELINE COMMANDS (/deb, /vot, /exe, /dve)
# =============================================================================

def extract_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Extracts prompt text following the command shorthand."""
    if not update.message or not update.message.text:
        return ""

    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) > 1:
        return parts[1].strip()

    # Check if reply to a message
    if update.message.reply_to_message and update.message.reply_to_message.text:
        return update.message.reply_to_message.text.strip()

    return ""


async def deb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /deb <prompt> (Debate Mode)."""
    global is_busy, active_task_name
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text(
            "🗣️ <b>Usage:</b> <code>/deb &lt;proposition or prompt&gt;</code>\n"
            "<i>Example:</i> <code>/deb Should we allocate capital to Solana LST protocols?</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if is_busy:
        await update.message.reply_text(
            f"⏳ The council is currently busy with: <b>{active_task_name}</b>. Please wait or use /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = check_and_record_chat_id(update)
    is_busy = True
    active_task_name = f"Debate: {prompt[:30]}..."

    try:
        ack_msg = await update.message.reply_text(
            f"🎙️ <b>AI COUNCIL DEBATE COMMENCING</b>\n"
            f"───────────────────────────────\n"
            f"📌 <b>Topic:</b> <i>{html.escape(prompt)}</i>\n"
            f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
            f"👥 <b>Active Debaters:</b> {len(council.agents_config)}\n"
            f"───────────────────────────────",
            parse_mode=ParseMode.HTML,
        )

        success = await council.run_debate(prompt=prompt, chat_id=chat_id, reply_to_id=update.message.message_id)

        if success:
            await update.message.reply_text(
                "🏁 <b>Debate Concluded.</b>\n"
                "To hold a formal vote on this proposition, use <code>/vot &lt;prompt&gt;</code> or <code>/exe</code>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text("⚠️ An issue occurred during the debate session.")

    finally:
        is_busy = False
        active_task_name = ""


async def vot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /vot <prompt> (Vote Mode)."""
    global is_busy, active_task_name
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text(
            "🗳️ <b>Usage:</b> <code>/vot &lt;proposition or question&gt;</code>\n"
            "<i>Example:</i> <code>/vot Approve buying $BONK at current liquidity levels?</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if is_busy:
        await update.message.reply_text(
            f"⏳ The council is currently busy with: <b>{active_task_name}</b>. Please wait or use /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = check_and_record_chat_id(update)
    is_busy = True
    active_task_name = f"Vote: {prompt[:30]}..."

    try:
        await update.message.reply_text(
            f"🗳️ <b>FORMAL COUNCIL VOTE CONVENED</b>\n"
            f"───────────────────────────────\n"
            f"📌 <b>Proposition:</b> <i>{html.escape(prompt)}</i>\n"
            f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
            f"⚖️ <b>Threshold:</b> <code>{config.CONSENSUS_THRESHOLD * 100:.0f}% Majority</code>\n"
            f"───────────────────────────────\n"
            f"<i>Each council member will now state their rationale and cast an official vote...</i>",
            parse_mode=ParseMode.HTML,
        )

        vote_summary = await council.run_vote(
            prompt=prompt, chat_id=chat_id, reply_to_id=update.message.message_id
        )

        # Broadcast final tallied results into the group
        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name)
        if lead_bot:
            await safe_send_message(
                bot=lead_bot,
                chat_id=chat_id,
                text=vote_summary.summary_text,
                parse_mode=ParseMode.HTML,
            )

    finally:
        is_busy = False
        active_task_name = ""


async def exe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exe <prompt> (Vote & Execute Mode)."""
    global is_busy, active_task_name
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text(
            "⚡ <b>Usage:</b> <code>/exe &lt;trade proposition with Solana CA&gt;</code>\n"
            "<i>Example:</i> <code>/exe Buy 0.25 SOL of DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if is_busy:
        await update.message.reply_text(
            f"⏳ The council is currently busy with: <b>{active_task_name}</b>. Please wait or use /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = check_and_record_chat_id(update)
    is_busy = True
    active_task_name = f"Execute: {prompt[:30]}..."

    try:
        await update.message.reply_text(
            f"⚡ <b>COUNCIL VOTE & EXECUTE SESSION</b>\n"
            f"───────────────────────────────\n"
            f"📌 <b>Target Task:</b> <i>{html.escape(prompt)}</i>\n"
            f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
            f"🐕 <b>Target Bot:</b> <code>Market Sniffer (@Msnifferbot)</code>\n"
            f"───────────────────────────────\n"
            f"<i>Voting commenced. If majority consensus passes, task will execute instantly.</i>",
            parse_mode=ParseMode.HTML,
        )

        # 1. Take Vote
        vote_summary = await council.run_vote(
            prompt=prompt, chat_id=chat_id, reply_to_id=update.message.message_id
        )

        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name)
        if lead_bot:
            await safe_send_message(
                bot=lead_bot,
                chat_id=chat_id,
                text=vote_summary.summary_text,
                parse_mode=ParseMode.HTML,
            )

        # 2. Check Consensus
        if vote_summary.consensus_passed:
            exec_res = await MSnifferExecutor.execute_consensus(
                prompt=prompt,
                consensus_text=vote_summary.summary_text,
                yes_count=vote_summary.yes_count,
                total_votes=vote_summary.total_votes,
                broadcaster_bot=lead_bot,
                reply_to_chat_id=chat_id,
            )
            announcement = format_execution_announcement(exec_res)
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=announcement,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        else:
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text="🛑 <b>CONSENSUS NOT REACHED:</b> Task execution aborted by council vote.",
                    parse_mode=ParseMode.HTML,
                )

    finally:
        is_busy = False
        active_task_name = ""


async def dve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /dve <prompt> (Debate, Vote & Execute Mode - Full Pipeline)."""
    global is_busy, active_task_name
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text(
            "🚀 <b>Usage:</b> <code>/dve &lt;trade proposition with Solana CA&gt;</code>\n"
            "<i>Example:</i> <code>/dve Debate & execute DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 (0.25 SOL)</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if is_busy:
        await update.message.reply_text(
            f"⏳ The council is currently busy with: <b>{active_task_name}</b>. Please wait or use /cancel.",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = check_and_record_chat_id(update)
    is_busy = True
    active_task_name = f"DVE Pipeline: {prompt[:30]}..."

    try:
        await update.message.reply_text(
            f"🚀 <b>FULL INSTITUTIONAL PIPELINE (DEBATE ➔ VOTE ➔ EXECUTE)</b>\n"
            f"───────────────────────────────\n"
            f"📌 <b>Proposition:</b> <i>{html.escape(prompt)}</i>\n"
            f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
            f"👥 <b>Debate Rounds:</b> {config.DEBATE_MAX_ROUNDS}\n"
            f"🐕 <b>Execution Bridge:</b> Market Sniffer\n"
            f"───────────────────────────────\n"
            f"<b>Phase 1: Multi-Agent Debate Commencing...</b>",
            parse_mode=ParseMode.HTML,
        )

        # 1. Debate & Vote
        vote_summary = await council.run_debate_and_vote(
            prompt=prompt, chat_id=chat_id, reply_to_id=update.message.message_id
        )

        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name)
        if lead_bot:
            await safe_send_message(
                bot=lead_bot,
                chat_id=chat_id,
                text=vote_summary.summary_text,
                parse_mode=ParseMode.HTML,
            )

        # 2. Check Consensus and Execute
        if vote_summary.consensus_passed:
            exec_res = await MSnifferExecutor.execute_consensus(
                prompt=prompt,
                consensus_text=vote_summary.summary_text,
                yes_count=vote_summary.yes_count,
                total_votes=vote_summary.total_votes,
                broadcaster_bot=lead_bot,
                reply_to_chat_id=chat_id,
            )
            announcement = format_execution_announcement(exec_res)
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=announcement,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
        else:
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text="🛑 <b>PIPELINE TERMINATED:</b> Proposition failed to secure majority council consensus.",
                    parse_mode=ParseMode.HTML,
                )

    finally:
        is_busy = False
        active_task_name = ""


# =============================================================================
# 5. APPLICATION INITIALIZATION & MAIN LOOP
# =============================================================================

async def post_init(application: Application):
    """Post initialization routine: sync display names and initialize orchestrator."""
    global council
    active_agents = config.get_active_agents()
    if not active_agents:
        logger.error("No active agents configured with valid Telegram bot tokens!")
        return

    # Sync display names via setMyName
    await sync_bot_display_names(active_agents)

    # Instantiate AutoGen Council Orchestrator
    council = CouncilOrchestrator(active_agents)
    logger.info(f"🏛️ Council Orchestrator ready with {len(active_agents)} AI members.")


def main():
    """Main application runner."""
    active_agents = config.get_active_agents()
    if not active_agents:
        print("❌ Error: No agents configured with Telegram Bot tokens in config.py or .env!")
        sys.exit(1)

    listener_idx = min(config.LISTENER_BOT_INDEX, len(active_agents) - 1)
    listener_agent = active_agents[listener_idx]

    print("\n" + "=" * 65)
    print("🏛️ MULTI-AGENT TELEGRAM GROUP CHAT SYSTEM STARTING")
    print("=" * 65)
    print(f"📡 Listener / Poller Bot : {listener_agent.name} (@{listener_agent.username})")
    print(f"👥 Active Council Agents : {len(active_agents)}")
    print(f"📍 Target Group Chat ID : {config.GROUP_CHAT_ID or 'Auto-detect on first message'}")
    print(f"🐕 MSniffer Dispatch Mode: {config.MSNIFFER_DISPATCH_MODE.upper()}")
    print("=" * 65 + "\n")

    # Build python-telegram-bot application with listener bot token and robust network timeouts
    ptb_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    application = (
        ApplicationBuilder()
        .token(listener_agent.token)
        .request(ptb_request)
        .post_init(post_init)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler(["start"], start_command))
    application.add_handler(CommandHandler(["help"], help_command))
    application.add_handler(CommandHandler(["status"], status_command))
    application.add_handler(CommandHandler(["mood"], mood_command))
    application.add_handler(CommandHandler(["setnames"], setnames_command))
    application.add_handler(CommandHandler(["cancel", "stop"], cancel_command))

    application.add_handler(CommandHandler(["deb", "debate"], deb_command))
    application.add_handler(CommandHandler(["vot", "vote"], vot_command))
    application.add_handler(CommandHandler(["exe", "execute"], exe_command))
    application.add_handler(CommandHandler(["dve"], dve_command))

    # Run polling loop
    logger.info(f"Starting long polling on @{listener_agent.username}...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
