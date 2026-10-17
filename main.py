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
import re

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

import config
from agents import CouncilOrchestrator
from executor import MSnifferExecutor, SignalExtractor, format_execution_announcement
from autopilot import autopilot

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
        "<b>🗣️ Operational Modes (Slash Commands):</b>\n"
        "• <code>/deb &lt;prompt&gt;</code> - Multi-turn debate across the 5 models\n"
        "• <code>/vot &lt;prompt&gt;</code> - Formal vote with rationale & consensus tally\n"
        "• <code>/exe &lt;prompt&gt;</code> - Vote & Execute: consensus triggers Market Sniffer on Solana\n"
        "• <code>/dve &lt;prompt&gt;</code> - Full Pipeline: Debate ➔ Vote ➔ Execute\n\n"
        "<b>🪙 Manual Contract Address (CA) & Flag Triggers:</b>\n"
        "You can simply paste a Solana CA or DEX link into the group chat with flags:\n"
        "• <code>&lt;CA&gt; -dve</code> - Instantly launches Full DVE Pipeline\n"
        "• <code>&lt;CA&gt; -exe</code> - Instantly launches Vote & Execute\n"
        "• <code>&lt;CA&gt; -vot</code> - Instantly launches Formal Vote\n"
        "• <code>&lt;CA&gt; -deb</code> - Instantly launches Debate\n"
        "• <code>&lt;CA&gt; -a 0.50</code> - Selects investment preset (0.05, 0.10, 0.25, 0.50, 1.00 SOL)\n"
        "• <code>&lt;CA&gt; -m ruthless</code> - Overrides mood for this evaluation\n"
        "<i>💡 Tip: Posting a bare CA with NO flags brings up interactive 1-click buttons with preset investment options!</i>\n\n"
        "<b>🤖 Autonomous Autopilot Stream Trading:</b>\n"
        "• <code>/auto on</code> - Starts autonomous loop (scans breakout tokens, debates, votes & trades)\n"
        "• <code>/auto off</code> - Halts autonomous loop\n"
        "• <code>/auto status</code> - Live statistics (cycles, tokens evaluated, approvals)\n"
        "<i>🛡️ Safety Guard: Autopilot automatically halts if wallet SOL drops below minimum!</i>\n\n"
        "<b>💳 Treasury, Wallet & Investment Options:</b>\n"
        "• <code>/wallet</code> - View Market Sniffer execution wallet pubkey & live SOL balance\n"
        "• <code>/invest [amount]</code> - View or change default investment preset (0.05, 0.10, 0.25, 0.50, 1.00 SOL)\n\n"
        "<b>🎭 Personality & Moods:</b>\n"
        "• <code>/mood &lt;type&gt;</code> (analytical, skeptical, aggressive, conservative, ruthless, contrarian)\n\n"
        "<b>⚙️ Management & Utility:</b>\n"
        "• <code>/status</code> - Display active models, autopilot state, and endpoints\n"
        "• <code>/setnames</code> - Re-sync bot display names to {AI}talks pattern\n"
        "• <code>/cancel</code> - Abort any active council session or autopilot"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /status command."""
    chat_id = check_and_record_chat_id(update)
    active_agents = config.get_active_agents()

    backend_desc = "OpenRouter Unified Engine" if config.OPENROUTER_API_KEY else "Simulated Fallback Mode"
    wallet_info = await MSnifferExecutor.get_wallet_status()
    sol_bal = wallet_info.get("sol_balance", 0.0)
    wallet_addr = wallet_info.get("wallet", "Unavailable")

    lines = [
        "🏛️ <b>AI COUNCIL SYSTEM STATUS</b>",
        "───────────────────────────────",
        f"🤖 <b>Council Agents:</b> <code>5/5 Active</code>",
        f"🎭 <b>Current Mood:</b> <code>{council.current_mood.upper() if council else 'ANALYTICAL'}</code>",
        f"🧠 <b>LLM Backend:</b> <code>{backend_desc}</code>",
        f"🤖 <b>Autopilot Loop:</b> <code>{'RUNNING 🟢' if autopilot.is_running else 'STOPPED ⚪'}</code>",
        f"💳 <b>Wallet SOL:</b> <code>{sol_bal:.4f} SOL</code> ({wallet_addr[:6]}...{wallet_addr[-4:] if len(wallet_addr) > 10 else ''})",
        f"📍 <b>Group Chat ID:</b> <code>{config.GROUP_CHAT_ID or chat_id}</code>",
        f"⚡ <b>Consensus Threshold:</b> <code>{config.CONSENSUS_THRESHOLD * 100:.0f}%</code>",
        f"🐕 <b>MSniffer Endpoint:</b> <code>{config.MSNIFFER_WEBHOOK_URL}</code>",
        "",
        "<b>Agent Roster:</b>",
    ]

    for idx, agent in enumerate(active_agents, 1):
        lines.append(f"{idx}. <b>{agent.display_name}</b> (@{agent.username}) | <i>{agent.model}</i>")

    lines.append("───────────────────────────────")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays Market Sniffer execution wallet pubkey and live SOL balance."""
    chat_id = check_and_record_chat_id(update)
    wallet_info = await MSnifferExecutor.get_wallet_status()

    is_online = wallet_info.get("online", False)
    status_icon = "🟢 Online" if is_online else "🔴 Offline (Start with 'bots start sniffer')"
    pubkey = wallet_info.get("wallet", "Unavailable")
    sol_bal = wallet_info.get("sol_balance", 0.0)

    solscan_url = f"https://solscan.io/account/{pubkey}" if pubkey and pubkey != "Unavailable" else ""

    lines = [
        "💳 <b>COUNCIL EXECUTION WALLET STATUS</b>",
        "───────────────────────────────",
        f"🐕 <b>Market Sniffer:</b> {status_icon}",
        f"🔑 <b>Public Key:</b> <code>{pubkey}</code>",
        f"💰 <b>SOL Balance:</b> <code>{sol_bal:.4f} SOL</code>",
        f"🎯 <b>Default Buy Size:</b> <code>{config.DEFAULT_TRADE_AMOUNT_SOL:.2f} SOL</code>",
        f"🤖 <b>Autopilot State:</b> <code>{'ACTIVE 🟢' if autopilot.is_running else 'STOPPED ⚪'}</code>",
    ]

    if solscan_url:
        lines.append(f"🔗 <a href='{solscan_url}'>View Wallet on Solscan</a>")

    lines.append("───────────────────────────────")
    if is_online and sol_bal < config.DEFAULT_TRADE_AMOUNT_SOL:
        lines.append(
            f"⚠️ <i>Warning: Balance is below default trade amount ({config.DEFAULT_TRADE_AMOUNT_SOL} SOL). "
            f"Deposit SOL to enable automated trade execution.</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def autopilot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /autopilot [on|off|status] (or /auto)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args or []
    subcmd = args[0].strip().lower() if args else "status"

    lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
    if not lead_bot:
        await update.message.reply_text("❌ Council Orchestrator is not ready.")
        return

    if subcmd in ("on", "start", "run"):
        success, msg = await autopilot.start(chat_id=chat_id, lead_bot=lead_bot, council_orchestrator=council)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif subcmd in ("off", "stop", "halt"):
        msg = await autopilot.stop()
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    else:
        # Status
        stats = autopilot.get_status()
        st_badge = "🟢 RUNNING (Scanning & Evaluating)" if stats["is_running"] else "⚪ STOPPED (Idle)"
        wallet_info = await MSnifferExecutor.get_wallet_status()
        sol_bal = wallet_info.get("sol_balance", 0.0)

        lines = [
            "🤖 <b>AI COUNCIL AUTOPILOT STATUS</b>",
            "───────────────────────────────",
            f"⚡ <b>State:</b> {st_badge}",
            f"⏱️ <b>Scan Interval:</b> <code>{stats['scan_interval']}s</code>",
            f"🔄 <b>Cycles Completed:</b> <code>{stats['cycles']}</code>",
            f"🔍 <b>Tokens Evaluated:</b> <code>{stats['evaluated_count']}</code>",
            f"✅ <b>Approved:</b> <code>{stats['approved']}</code>",
            f"❌ <b>Rejected:</b> <code>{stats['rejected']}</code>",
            f"🚀 <b>Trades Dispatched:</b> <code>{stats['executed']}</code>",
            f"💰 <b>Wallet SOL:</b> <code>{sol_bal:.4f} SOL</code>",
            "───────────────────────────────",
            "<b>Controls:</b>",
            "• <code>/auto on</code> - Start autonomous loop",
            "• <code>/auto off</code> - Halt autonomous loop",
        ]
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
    """Cancels active operation or autopilot if running."""
    global is_busy, active_task_name
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    msgs = []
    if autopilot.is_running:
        ap_msg = await autopilot.stop()
        msgs.append(ap_msg)

    if is_busy:
        is_busy = False
        old_task = active_task_name
        active_task_name = ""
        msgs.append(f"🛑 Cancelled active session: <b>{old_task}</b>")

    if msgs:
        await update.message.reply_text("\n".join(msgs), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("ℹ️ No active council session or autopilot is currently running.")


# =============================================================================
# 4. UNIFIED PIPELINE ENGINE & COMMAND DELEGATORS
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


async def execute_council_session(
    chat_id: int,
    prompt: str,
    mode: str,
    reply_to_id: Optional[int] = None,
    mood_override: Optional[str] = None,
) -> bool:
    """
    Centralized execution engine for all Council modes (debate, vote, execute, dve).
    Used by slash commands, manual CA postings with flags, and inline keyboard clicks.
    """
    global is_busy, active_task_name

    if not council:
        logger.error("Council orchestrator not initialized.")
        return False

    lead_bot = council.broadcaster.get_bot(council.agents_config[0].name)

    if is_busy:
        if lead_bot:
            await lead_bot.send_message(
                chat_id=chat_id,
                text=f"⏳ The council is currently busy with: <b>{active_task_name}</b>. Please wait or use /cancel.",
                parse_mode=ParseMode.HTML,
            )
        return False

    old_mood = council.current_mood
    if mood_override and mood_override in config.MOOD_PROMPTS:
        council.set_mood(mood_override)

    is_busy = True
    active_task_name = f"{mode.upper()}: {prompt[:30]}..."

    try:
        if mode == "debate":
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        f"🎙️ <b>AI COUNCIL DEBATE COMMENCING</b>\n"
                        f"───────────────────────────────\n"
                        f"📌 <b>Topic:</b> <i>{html.escape(prompt)}</i>\n"
                        f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
                        f"👥 <b>Active Debaters:</b> {len(council.agents_config)}\n"
                        f"───────────────────────────────"
                    ),
                )
            success = await council.run_debate(prompt=prompt, chat_id=chat_id, reply_to_id=reply_to_id)
            if success and lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text="🏁 <b>Debate Concluded.</b> To hold a formal vote, use <code>/vot &lt;prompt&gt;</code> or <code>/exe</code>.",
                )
            return success

        elif mode in ("vote", "vot"):
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        f"🗳️ <b>FORMAL COUNCIL VOTE CONVENED</b>\n"
                        f"───────────────────────────────\n"
                        f"📌 <b>Proposition:</b> <i>{html.escape(prompt)}</i>\n"
                        f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
                        f"⚖️ <b>Threshold:</b> <code>{config.CONSENSUS_THRESHOLD * 100:.0f}% Majority</code>\n"
                        f"───────────────────────────────\n"
                        f"<i>Each council member will now state their rationale and cast an official vote...</i>"
                    ),
                )
            vote_summary = await council.run_vote(prompt=prompt, chat_id=chat_id, reply_to_id=reply_to_id)
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=vote_summary.summary_text,
                )
            return vote_summary.consensus_passed

        elif mode in ("execute", "exe"):
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        f"⚡ <b>COUNCIL VOTE & EXECUTE SESSION</b>\n"
                        f"───────────────────────────────\n"
                        f"📌 <b>Target Task:</b> <i>{html.escape(prompt)}</i>\n"
                        f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
                        f"🐕 <b>Execution Bridge:</b> <code>Market Sniffer (@Msnifferbot)</code>\n"
                        f"───────────────────────────────\n"
                        f"<i>Voting commenced. If majority consensus passes, task will execute instantly.</i>"
                    ),
                )
            vote_summary = await council.run_vote(prompt=prompt, chat_id=chat_id, reply_to_id=reply_to_id)
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=vote_summary.summary_text,
                )
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
                        disable_web_page_preview=True,
                    )
                return exec_res.success
            else:
                if lead_bot:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text="🛑 <b>CONSENSUS NOT REACHED:</b> Task execution aborted by council vote.",
                    )
                return False

        elif mode == "dve":
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        f"🚀 <b>FULL INSTITUTIONAL PIPELINE (DEBATE ➔ VOTE ➔ EXECUTE)</b>\n"
                        f"───────────────────────────────\n"
                        f"📌 <b>Proposition:</b> <i>{html.escape(prompt)}</i>\n"
                        f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
                        f"👥 <b>Debate Rounds:</b> {config.DEBATE_MAX_ROUNDS}\n"
                        f"🐕 <b>Execution Bridge:</b> Market Sniffer\n"
                        f"───────────────────────────────\n"
                        f"<b>Phase 1: Multi-Agent Debate Commencing...</b>"
                    ),
                )
            vote_summary = await council.run_debate_and_vote(prompt=prompt, chat_id=chat_id, reply_to_id=reply_to_id)
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=vote_summary.summary_text,
                )
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
                        disable_web_page_preview=True,
                    )
                return exec_res.success
            else:
                if lead_bot:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text="🛑 <b>PIPELINE TERMINATED:</b> Proposition failed to secure majority council consensus.",
                    )
                return False

    finally:
        if mood_override:
            council.set_mood(old_mood)
        is_busy = False
        active_task_name = ""


async def deb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /deb <prompt> (Debate Mode)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text("🗣️ <b>Usage:</b> <code>/deb &lt;prompt&gt;</code>", parse_mode=ParseMode.HTML)
        return
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(chat_id=chat_id, prompt=prompt, mode="debate", reply_to_id=update.message.message_id)


async def vot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /vot <prompt> (Vote Mode)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text("🗳️ <b>Usage:</b> <code>/vot &lt;proposition&gt;</code>", parse_mode=ParseMode.HTML)
        return
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(chat_id=chat_id, prompt=prompt, mode="vote", reply_to_id=update.message.message_id)


async def exe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exe <prompt> (Vote & Execute Mode)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text("⚡ <b>Usage:</b> <code>/exe &lt;trade proposition with Solana CA&gt;</code>", parse_mode=ParseMode.HTML)
        return
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(chat_id=chat_id, prompt=prompt, mode="execute", reply_to_id=update.message.message_id)


async def dve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /dve <prompt> (Debate, Vote & Execute Mode - Full Pipeline)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    if not prompt:
        await update.message.reply_text("🚀 <b>Usage:</b> <code>/dve &lt;trade proposition with Solana CA&gt;</code>", parse_mode=ParseMode.HTML)
        return
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(chat_id=chat_id, prompt=prompt, mode="dve", reply_to_id=update.message.message_id)


def build_ca_keyboard(mint: str, selected_amount: Optional[float] = None) -> InlineKeyboardMarkup:
    """
    Builds the interactive inline keyboard with explicit preset investment amount options.
    Prevents arbitrary custom amounts by strictly offering approved SOL tiers.
    """
    if selected_amount is None:
        selected_amount = config.DEFAULT_TRADE_AMOUNT_SOL
    selected_amount = config.validate_investment_amount(selected_amount)

    # Row 1: Strict preset investment options
    amt_row = []
    for opt in config.ALLOWED_INVESTMENT_OPTIONS:
        label = f"{opt:.2f} SOL ✅" if abs(opt - selected_amount) < 0.001 else f"{opt:.2f} SOL"
        amt_row.append(InlineKeyboardButton(label, callback_data=f"amt:{mint}:{opt:.2f}"))

    # Row 2: Council execution actions with the chosen investment amount
    action_row = [
        InlineKeyboardButton(f"🚀 Full DVE ({selected_amount:.2f} SOL)", callback_data=f"dve:{mint}:{selected_amount:.2f}"),
        InlineKeyboardButton(f"⚡ Vote & Exe ({selected_amount:.2f} SOL)", callback_data=f"exe:{mint}:{selected_amount:.2f}"),
    ]

    # Row 3: Deliberation without execution
    delib_row = [
        InlineKeyboardButton("🗳️ Vote Only", callback_data=f"vot:{mint}:{selected_amount:.2f}"),
        InlineKeyboardButton("🎙️ Debate Only", callback_data=f"deb:{mint}:{selected_amount:.2f}"),
    ]

    # Row 4: Autopilot toggle
    ap_label = "🛑 Stop Autopilot" if autopilot.is_running else "🤖 Start Autopilot"
    ap_row = [InlineKeyboardButton(ap_label, callback_data="auto:toggle")]

    return InlineKeyboardMarkup([amt_row, action_row, delib_row, ap_row])


def build_invest_keyboard() -> InlineKeyboardMarkup:
    """Builds the inline keyboard for the /invest command."""
    current = config.DEFAULT_TRADE_AMOUNT_SOL
    row = []
    for opt in config.ALLOWED_INVESTMENT_OPTIONS:
        label = f"{opt:.2f} SOL ✅" if abs(opt - current) < 0.001 else f"{opt:.2f} SOL"
        row.append(InlineKeyboardButton(label, callback_data=f"setinv:{opt:.2f}"))
    return InlineKeyboardMarkup([row])


async def invest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /invest [amount] to configure or view the council's investment preset."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args

    if args:
        try:
            raw_val = float(args[0].strip().replace("sol", ""))
            clean_val = round(raw_val, 2)
            if clean_val not in config.ALLOWED_INVESTMENT_OPTIONS:
                opts_str = ", ".join(f"{x:.2f} SOL" for x in config.ALLOWED_INVESTMENT_OPTIONS)
                await update.message.reply_text(
                    f"⚠️ <b>Invalid Investment Amount!</b>\n\n"
                    f"Arbitrary custom amounts are disabled for treasury safety.\n"
                    f"You must select from the approved preset options:\n"
                    f"<code>{opts_str}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            config.DEFAULT_TRADE_AMOUNT_SOL = clean_val
            await update.message.reply_text(
                f"✅ <b>Default Council Investment Updated:</b> <code>{clean_val:.2f} SOL</code>\n"
                f"Future consensus-approved trades will allocate <code>{clean_val:.2f} SOL</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        except ValueError:
            pass

    opts_str = " | ".join(f"{x:.2f} SOL" for x in config.ALLOWED_INVESTMENT_OPTIONS)
    text = (
        f"💰 <b>COUNCIL INVESTMENT AMOUNT PRESET</b>\n"
        f"───────────────────────────────\n"
        f"🎯 <b>Current Active Setting:</b> <code>{config.DEFAULT_TRADE_AMOUNT_SOL:.2f} SOL</code>\n"
        f"🛡️ <b>Allowed Options:</b> <code>{opts_str}</code>\n"
        f"───────────────────────────────\n"
        f"<i>Tap an option below to set the investment amount for future trades:</i>"
    )
    await update.message.reply_text(text, reply_markup=build_invest_keyboard(), parse_mode=ParseMode.HTML)


# =============================================================================
# 5. SMART MESSAGE HANDLER (MANUAL CA POSTINGS WITH FLAGS & INLINE BUTTONS)
# =============================================================================

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles regular text messages posted into the group chat:
    1. Detects Solana CAs, Pump.fun links, DexScreener links, etc.
    2. Checks for flags (-dve, -exe, -vot, -deb, -auto, -a <amt>, -m <mood>)
    3. If flag is present: immediately executes that mode for the CA!
    4. If no flag is present: replies with interactive 1-click inline buttons with preset options!
    """
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = check_and_record_chat_id(update)

    # 1. Check if message contains a Solana CA or DEX URL
    mint, action, amount, symbol = SignalExtractor.extract(text)

    # If no CA in the message, check if replying to a message with a CA
    if not mint and update.message.reply_to_message and update.message.reply_to_message.text:
        mint, action, amount, symbol = SignalExtractor.extract(update.message.reply_to_message.text)

    # If still no CA, ignore silently (so regular chit-chat in the group is untouched)
    if not mint:
        return

    # 2. Check authorization
    auth, reason = is_authorized_admin(update)
    if not auth:
        logger.debug(f"Non-admin posted CA {mint}; ignoring or unauthorized.")
        return

    # 3. Parse flags
    mode = None
    if re.search(r"(?:^|\s)(?:--?dve)\b", text, re.IGNORECASE):
        mode = "dve"
    elif re.search(r"(?:^|\s)(?:--?(?:exe|execute))\b", text, re.IGNORECASE):
        mode = "exe"
    elif re.search(r"(?:^|\s)(?:--?(?:vot|vote))\b", text, re.IGNORECASE):
        mode = "vote"
    elif re.search(r"(?:^|\s)(?:--?(?:deb|debate))\b", text, re.IGNORECASE):
        mode = "debate"
    elif re.search(r"(?:^|\s)(?:--?(?:auto|autopilot|loop))\b", text, re.IGNORECASE):
        mode = "auto"

    # Amount flag (-a 0.5 or --amount 0.5) - strictly snapped to allowed options!
    amt_match = re.search(r"(?:-a|--amount)\s+(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if amt_match:
        try:
            parsed_amt = float(amt_match.group(1))
            amount = config.validate_investment_amount(parsed_amt)
        except ValueError:
            amount = config.DEFAULT_TRADE_AMOUNT_SOL
    else:
        amount = config.validate_investment_amount(amount)

    # Custom mood flag (-m ruthless or --mood ruthless)
    mood_match = re.search(r"(?:-m|--mood)\s+([a-zA-Z]+)", text, re.IGNORECASE)
    mood_override = mood_match.group(1).lower() if mood_match else None

    # 4. Handle Autopilot flag
    if mode == "auto":
        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
        if not lead_bot:
            await update.message.reply_text("❌ Council Orchestrator is not ready.")
            return
        success, msg = await autopilot.start(chat_id=chat_id, lead_bot=lead_bot, council_orchestrator=council)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    # 5. If specific operational mode flag was attached, run immediately!
    if mode in ("dve", "exe", "vote", "debate"):
        action_desc = "BUY" if action == "BUY" else "SELL"
        prompt = f"{action_desc} {amount:.2f} SOL of token {mint}"
        if symbol:
            prompt += f" (${symbol})"
        await execute_council_session(
            chat_id=chat_id,
            prompt=prompt,
            mode=mode,
            reply_to_id=update.message.message_id,
            mood_override=mood_override,
        )
        return

    # 6. If NO flag attached (bare CA), present interactive 1-click inline keyboard with investment options!
    dex_url = f"https://dexscreener.com/solana/{mint}"
    reply_markup = build_ca_keyboard(mint=mint, selected_amount=amount)

    card_text = (
        f"🪙 <b>Solana Token Detected:</b> <code>{mint}</code>\n"
        f"📊 <a href='{dex_url}'>View DexScreener Chart</a>\n"
        f"💰 <b>Selected Investment:</b> <code>{amount:.2f} SOL</code>\n\n"
        f"<i>Select an approved investment option above or trigger an action:</i>"
    )

    await update.message.reply_text(
        card_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles clicks on interactive inline keyboard buttons."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    # Verify authorization
    user = update.effective_user
    if config.ADMIN_USER_IDS and user and user.id not in config.ADMIN_USER_IDS:
        await query.message.reply_text("⛔ Unauthorized: Only configured council admins can trigger actions.")
        return

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else config.GROUP_CHAT_ID

    # 1. Handle Autopilot Toggle
    if data == "auto:toggle":
        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
        if not lead_bot:
            await query.message.reply_text("❌ Council Orchestrator is not ready.")
            return

        if autopilot.is_running:
            res_msg = await autopilot.stop()
            await query.message.reply_text(res_msg, parse_mode=ParseMode.HTML)
        else:
            success, res_msg = await autopilot.start(chat_id=chat_id, lead_bot=lead_bot, council_orchestrator=council)
            await query.message.reply_text(res_msg, parse_mode=ParseMode.HTML)
        return

    # 2. Handle Investment Option selection on CA card (amt:<mint>:<val>)
    if data.startswith("amt:"):
        parts = data.split(":")
        if len(parts) >= 3:
            mint = parts[1]
            try:
                new_amt = config.validate_investment_amount(float(parts[2]))
            except ValueError:
                new_amt = config.DEFAULT_TRADE_AMOUNT_SOL

            new_markup = build_ca_keyboard(mint=mint, selected_amount=new_amt)
            dex_url = f"https://dexscreener.com/solana/{mint}"
            updated_text = (
                f"🪙 <b>Solana Token Detected:</b> <code>{mint}</code>\n"
                f"📊 <a href='{dex_url}'>View DexScreener Chart</a>\n"
                f"💰 <b>Selected Investment:</b> <code>{new_amt:.2f} SOL</code>\n\n"
                f"<i>Select an approved investment option above or trigger an action:</i>"
            )
            try:
                await query.edit_message_text(
                    text=updated_text,
                    reply_markup=new_markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.debug(f"Error updating inline keyboard: {e}")
        return

    # 3. Handle Setting Global Default Investment Option (setinv:<val>)
    if data.startswith("setinv:"):
        parts = data.split(":")
        if len(parts) >= 2:
            try:
                new_amt = config.validate_investment_amount(float(parts[1]))
                config.DEFAULT_TRADE_AMOUNT_SOL = new_amt
                opts_str = " | ".join(f"{x:.2f} SOL" for x in config.ALLOWED_INVESTMENT_OPTIONS)
                text = (
                    f"✅ <b>Default Council Investment Amount Updated!</b>\n\n"
                    f"💰 <b>Active Setting:</b> <code>{new_amt:.2f} SOL</code>\n"
                    f"🛡️ <b>Allowed Options:</b> <code>{opts_str}</code>\n"
                    f"───────────────────────────────\n"
                    f"All future consensus-approved trades will allocate <code>{new_amt:.2f} SOL</code>."
                )
                await query.edit_message_text(
                    text=text,
                    reply_markup=build_invest_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.debug(f"Error setting default investment: {e}")
        return

    # 4. Handle Council Action dispatch (dve, exe, vot, deb)
    parts = data.split(":")
    if len(parts) >= 2:
        cmd_type = parts[0]
        mint = parts[1]
        raw_amt = float(parts[2]) if len(parts) > 2 else config.DEFAULT_TRADE_AMOUNT_SOL
        amount = config.validate_investment_amount(raw_amt)

        mode_map = {
            "dve": "dve",
            "exe": "execute",
            "vot": "vote",
            "deb": "debate",
        }
        mode = mode_map.get(cmd_type, "dve")
        prompt = f"BUY {amount:.2f} SOL of token {mint}"

        # Cleanly remove the buttons to prevent accidental double-clicks or re-entrancy
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await execute_council_session(
            chat_id=chat_id,
            prompt=prompt,
            mode=mode,
            reply_to_id=query.message.message_id if query.message else None,
        )


# =============================================================================
# 6. APPLICATION INITIALIZATION & MAIN LOOP
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

    # 1. Command handlers
    application.add_handler(CommandHandler(["start"], start_command))
    application.add_handler(CommandHandler(["help"], help_command))
    application.add_handler(CommandHandler(["status"], status_command))
    application.add_handler(CommandHandler(["wallet", "bal", "balance"], wallet_command))
    application.add_handler(CommandHandler(["invest", "setinvest"], invest_command))
    application.add_handler(CommandHandler(["autopilot", "auto"], autopilot_command))
    application.add_handler(CommandHandler(["mood"], mood_command))
    application.add_handler(CommandHandler(["setnames"], setnames_command))
    application.add_handler(CommandHandler(["cancel", "stop"], cancel_command))

    application.add_handler(CommandHandler(["deb", "debate"], deb_command))
    application.add_handler(CommandHandler(["vot", "vote"], vot_command))
    application.add_handler(CommandHandler(["exe", "execute"], exe_command))
    application.add_handler(CommandHandler(["dve"], dve_command))

    # 2. Callback query handler for inline button clicks
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # 3. Message handler for raw CA postings with flags or interactive menus
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    # Run polling loop
    logger.info(f"Starting long polling on @{listener_agent.username}...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

