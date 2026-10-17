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

from telegram import Bot, BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
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
from positions import position_manager
from vault import profit_vault
from context_manager import context_manager
import telemetry

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

CRYPTO_KEYWORDS = {
    "sol", "solana", "crypto", "token", "coin", "ca", "mint", "contract", "pump", "pump.fun",
    "raydium", "dex", "liquidity", "mcap", "market cap", "fdv", "volume", "airdrop", "yield",
    "staking", "swap", "buy", "sell", "trading", "trade", "degen", "rug", "honeypot", "wallet",
    "bull", "bullish", "bear", "bearish", "ath", "dip", "slippage", "gas", "fees", "whale",
    "orderbook", "vault", "profit", "positions", "hold", "exit", "tp", "sl", "stop loss",
    "take profit", "sniffer", "btc", "eth", "usdt", "usdc", "memecoin", "altcoin", "defi",
    "chart", "breakout", "dump", "orca", "meteora", "jupiter", "jup", "wif", "bonk",
    "popcat", "bome", "fartcoin", "act", "pnut", "goat", "moodeng", "invest", "balance"
}


def is_crypto_related(text: str) -> bool:
    """Checks if a text message is strictly related to cryptocurrency, tokens, or trading."""
    if not text:
        return False
    lower = text.lower()
    if re.search(r"\$[A-Za-z0-9]{2,10}\b", text):
        return True
    words = set(re.findall(r"\b[a-zA-Z0-9_\.]+\b", lower))
    return bool(words.intersection(CRYPTO_KEYWORDS))


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
    first_name = html.escape(user.first_name) if user else "Trader"

    text = (
        f"🏛️ <b>Chatters Solana Crypto Council is Online!</b>\n\n"
        f"Welcome, {first_name}. The AI Council features 5 autonomous models "
        f"dedicated strictly to Solana token intelligence, on-chain risk audits, and trade execution.\n\n"
        f"<b>Shorthand Commands:</b>\n"
        f"• <code>/deb &lt;CA, $ticker, or topic&gt;</code> - Debate crypto tokens, market depth & liquidity\n"
        f"• <code>/vot &lt;CA, $ticker, or trade&gt;</code> - Debate & Vote on crypto trades with formal consensus\n"
        f"• <code>/exe &lt;CA or $ticker&gt;</code> - Silent background vote & buy on Solana\n"
        f"• <code>/dve &lt;CA or $ticker&gt;</code> - Full Pipeline (Debate ➔ Vote ➔ Execute on Solana)\n"
        f"• <code>/auto on|off</code> - Autonomous breakout token scanner & trader\n"
        f"• <code>/context</code> - View or customize instructions for AI bot voting\n"
        f"• <code>/positions</code> - View open positions & live performance\n"
        f"• <code>/wallet</code> - View wallet address & SOL balance\n"
        f"• <code>/vault</code> - View Profit Vault ($25 USDT sweeps)\n"
        f"• <code>/command</code> - View complete command cheat sheet\n\n"
        f"📍 <i>Current Chat ID:</i> <code>{chat_id}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /command, /commands, and /help."""
    text = (
        "📖 <b>CHATTERS CRYPTO COUNCIL — COMMAND GUIDE</b>\n"
        "───────────────────────────────\n\n"
        "<b>🗣️ Council Modes (Strictly Crypto):</b>\n"
        "• <code>/deb &lt;CA, $ticker, or crypto topic&gt;</code> - <b>Crypto Debate:</b> The 5 models debate on-chain metrics, liquidity depth, and risks.\n"
        "• <code>/vot &lt;CA, $ticker, or trade&gt;</code> - <b>Debate & Vote:</b> The models debate and cast majority consensus votes.\n"
        "• <code>/exe &lt;CA or $ticker&gt;</code> - <b>Vote & Execute (Silent):</b> Fast background vote; buys on Solana if consensus passes.\n"
        "• <code>/dve &lt;CA or $ticker&gt;</code> - <b>Full DVE Pipeline:</b> Verbal debate + vote + on-chain trade execution on consensus.\n"
        "• <code>/context [instructions]</code> - <b>Dynamic Voting Directives:</b> Customize voting instructions (presets: conservative, aggressive, memecoin).\n\n"
        "<b>🪙 Solana Token Actions:</b>\n"
        "• Paste any Solana CA or DEX link into the group to get a live on-chain briefing card with 1-click execution buttons!\n"
        "• Type <code>$TICKER</code> (e.g. <code>$BONK</code>, <code>$WIF</code>) to automatically resolve on-chain metrics.\n"
        "• Or use shorthand flags: <code>&lt;CA&gt; -exe</code> or <code>&lt;CA&gt; -dv</code> or <code>&lt;CA&gt; -deb</code>\n\n"
        "<b>🤖 Autonomous Trading & Profit Security:</b>\n"
        "• <code>/auto</code> - View Autonomous dashboard & interactive frequency buttons\n"
        "• <code>/auto on [time]</code> - Activate autonomous scanner (e.g. <code>/auto on 15m</code>, <code>/auto on 1h</code>, <code>/auto on 1w</code>)\n"
        "• <code>/auto &lt;time&gt;</code> - Switch scan frequency (e.g. <code>/auto 1m</code>, <code>/auto 15m</code>, <code>/auto 30m</code>, <code>/auto 1h</code>, <code>/auto 1w</code>)\n"
        "• <code>/auto off</code> - Halt autonomous scanner\n"
        "• <code>/positions</code> - View open token positions & live performance\n"
        "• <code>/sell &lt;CA&gt;</code> - Manually exit a token position\n"
        "• <code>/vault</code> - View profit vault (sweeps $25 profit to USDT so profits are never burned)\n"
        "• <code>/wallet</code> - View Market Sniffer execution wallet & SOL balance\n"
        "• <code>/invest [amount]</code> - Set default trade size (e.g. <code>/invest 0.25</code>)\n"
        "• <code>/cancel</code> - Stop any active session or autopilot"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /status command."""
    chat_id = check_and_record_chat_id(update)
    active_agents = config.get_active_agents()

    openrouter_info = await config.check_openrouter_status()
    backend_desc = openrouter_info.get("status", "OpenRouter Unified Engine")
    wallet_info = await MSnifferExecutor.get_wallet_status()
    sol_bal = wallet_info.get("sol_balance", 0.0)
    wallet_addr = wallet_info.get("wallet", "Unavailable")
    open_pos = position_manager.get_open_positions()

    lines = [
        "🏛️ <b>AI COUNCIL SYSTEM STATUS</b>",
        "───────────────────────────────",
        f"🤖 <b>Council Agents:</b> <code>5/5 Active</code>",
        f"🎭 <b>Current Mood:</b> <code>{council.current_mood.upper() if council else 'ANALYTICAL'}</code>",
        f"🧠 <b>LLM Backend:</b> <code>{backend_desc}</code>",
        f"🤖 <b>Autopilot Loop:</b> <code>{'RUNNING 🟢' if autopilot.is_running else 'STOPPED ⚪'}</code>",
        f"📈 <b>Active Positions:</b> <code>{len(open_pos)} open</code> (Use /positions)",
        f"🏦 <b>USDT Profit Vault:</b> <code>${profit_vault.total_usdt_harvested:.2f} USDT</code> (Use /vault)",
        f"💳 <b>Wallet SOL:</b> <code>{sol_bal:.4f} SOL</code> ({wallet_addr[:6]}...{wallet_addr[-4:] if len(wallet_addr) > 10 else ''})",
        f"📍 <b>Group Chat ID:</b> <code>{config.GROUP_CHAT_ID or chat_id}</code>",
        f"⚡ <b>Consensus Threshold:</b> <code>{config.CONSENSUS_THRESHOLD * 100:.0f}%</code>",
        f"🐕 <b>MSniffer Endpoint:</b> <code>{config.MSNIFFER_WEBHOOK_URL}</code>",
        "",
        "<b>Agent Roster:</b>",
    ]

    for idx, agent in enumerate(active_agents, 1):
        lines.append(f"{idx}. <b>{agent.display_name}</b> (@{agent.username}) | <i>{agent.model}</i>")

    if openrouter_info.get("credit_usd") is not None and openrouter_info.get("credit_usd", 0.0) <= 0.005:
        lines.append("")
        lines.append("⚠️ <b>NOTICE:</b> OpenRouter balance is $0.00 USD. The bots are operating on high-fidelity offline crypto analysis. Top up credits at <a href='https://openrouter.ai/settings/credits'>openrouter.ai/settings/credits</a> to resume cloud models.")

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


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays all currently open positions with entry prices, current prices, and live PnL."""
    chat_id = check_and_record_chat_id(update)
    summary = position_manager.get_summary_text()
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def vault_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays Profit Vault status or sets customizable threshold (/vault [amount|reset])."""
    chat_id = check_and_record_chat_id(update)
    args = context.args or []

    if args:
        auth, reason = is_authorized_admin(update)
        if not auth:
            await update.message.reply_text(f"⛔ {reason}")
            return

        arg_val = args[0].strip().lower()
        if arg_val in ("reset", "sync"):
            wallet_info = await MSnifferExecutor.get_wallet_status()
            sol_bal = wallet_info.get("sol_balance", 0.0)
            profit_vault.reset_baseline(sol_bal)
            await update.message.reply_text(
                f"✅ <b>Profit Vault Baseline Synced:</b> <code>{sol_bal:.4f} SOL</code>\n"
                f"Future profit sweeps will calculate gains from this new baseline.",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            cleaned = float(arg_val.replace("$", "").replace("usd", ""))
            ok, msg = profit_vault.set_threshold(cleaned)
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            return
        except ValueError:
            pass

    wallet_info = await MSnifferExecutor.get_wallet_status()
    sol_bal = wallet_info.get("sol_balance", 0.0)
    text = profit_vault.get_status_text(current_sol_balance=sol_bal)
    await update.message.reply_text(
        text,
        reply_markup=build_vault_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def exitmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exitmode [auto|hold] to configure default Council exit strategy."""
    chat_id = check_and_record_chat_id(update)
    args = context.args or []

    if args:
        auth, reason = is_authorized_admin(update)
        if not auth:
            await update.message.reply_text(f"⛔ {reason}")
            return

        val = args[0].strip().lower()
        if val in ("runner", "long", "trail", "trailing"):
            config.DEFAULT_EXIT_STRATEGY = "TRAILING_RUNNER"
            await update.message.reply_text(
                f"🚀 <b>Default Exit Strategy Set to LONG RUNNER MODE</b>\n\n"
                f"• Positions can climb indefinitely to 5x-100x without premature selling.\n"
                f"• Once in profit (>= +50%), it trails the highest peak price by -{config.TRAILING_STOP_PCT:.0f}% (wider margin for memecoin breathing room).\n"
                f"• Stop-loss cuts at -{config.STOP_LOSS_PCT:.0f}% to protect initial capital.",
                parse_mode=ParseMode.HTML,
            )
            return
        elif val in ("2x", "double", "auto", "tp", "profit"):
            config.DEFAULT_EXIT_STRATEGY = "2X_TAKE_PROFIT"
            await update.message.reply_text(
                f"🎯 <b>Default Exit Strategy Set to FIXED 2X TAKE-PROFIT (+100%)</b>\n\n"
                f"• Automatically sells and locks in profit as soon as the token doubles in value (2x return / +100% gain).\n"
                f"• Stop-loss cuts at -{config.STOP_LOSS_PCT:.0f}% if price drops.",
                parse_mode=ParseMode.HTML,
            )
            return
        elif val in ("hold", "diamond", "moon", "moonbag"):
            config.DEFAULT_EXIT_STRATEGY = "HOLD_INDEFINITE"
            await update.message.reply_text(
                "💎 <b>Default Exit Strategy Set to HOLD INDEFINITELY</b>\n\n"
                "Autonomous and manual trades will hold tokens indefinitely without auto-selling.\n"
                "You can manually exit anytime using <code>/sell &lt;CA&gt; [pct]</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

    if config.DEFAULT_EXIT_STRATEGY == "TRAILING_RUNNER":
        current_strat = f"🚀 LONG RUNNER (Trail peak -{config.TRAILING_STOP_PCT:.0f}%)"
    elif config.DEFAULT_EXIT_STRATEGY in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
        current_strat = "🎯 FIXED 2X TAKE-PROFIT (+100.0%)"
    else:
        current_strat = "💎 HOLD INDEFINITELY (Diamond Hands)"

    text = (
        "🛡️ <b>COUNCIL EXIT STRATEGY CONTROLLER</b>\n"
        "───────────────────────────────\n"
        f"🎯 <b>Active Default:</b> <code>{current_strat}</code>\n"
        f"🚀 <b>Long Runner Mode:</b> Rides 5x-100x; trails peak by <code>-{config.TRAILING_STOP_PCT:.0f}%</code>\n"
        f"🎯 <b>Fixed 2x Mode:</b> Auto-exits and banks profit at <code>+100.0%</code>\n"
        f"📉 <b>Stop-Loss Cut:</b> <code>-{config.STOP_LOSS_PCT:.0f}%</code>\n"
        "───────────────────────────────\n"
        "<i>Tap a strategy below to switch the default mode for both autonomous and manual trades:</i>"
    )
    await update.message.reply_text(
        text,
        reply_markup=build_exitmode_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def tp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /tp <pct> to configure Take-Profit target percentage."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"🎯 <b>Current Take-Profit Target:</b> <code>+{config.TAKE_PROFIT_PCT:.0f}%</code>\n\n"
            f"Usage: <code>/tp &lt;percentage&gt;</code> (e.g. <code>/tp 50</code>, <code>/tp 100</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        new_tp = float(args[0].strip().replace("%", ""))
        if new_tp < 5.0:
            await update.message.reply_text("⚠️ Minimum Take-Profit target is +5.0%.")
            return
        config.TAKE_PROFIT_PCT = new_tp
        await update.message.reply_text(
            f"✅ <b>Take-Profit Target Updated:</b> <code>+{new_tp:.1f}%</code>\n"
            f"Tokens configured for AUTO-EXIT will automatically sell when profit reaches +{new_tp:.1f}%.",
            parse_mode=ParseMode.HTML,
        )
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number. Usage: <code>/tp 50</code>", parse_mode=ParseMode.HTML)


async def context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /context command:
    - /context : View active voting instruction & preset buttons
    - /context <custom text> : Set custom voting instructions for the bots
    - /context reset / /context clear : Reset back to DEFAULT instructions
    - /context <preset> : Apply built-in preset (conservative, aggressive, memecoin, default)
    """
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args or []
    user_name = update.effective_user.first_name if update.effective_user else "Admin"

    if not args:
        dashboard = context_manager.format_status_dashboard()
        await update.message.reply_text(
            dashboard,
            reply_markup=context_manager.build_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return

    full_arg = " ".join(args).strip()
    arg_lower = full_arg.lower()

    if arg_lower in ("reset", "clear", "default"):
        res = context_manager.reset_to_default(updated_by=user_name)
        await update.message.reply_text(res, parse_mode=ParseMode.HTML)
        return

    if arg_lower in ("conservative", "safe", "defense"):
        ok, res = context_manager.apply_preset("CONSERVATIVE", updated_by=user_name)
        await update.message.reply_text(res, parse_mode=ParseMode.HTML)
        return

    if arg_lower in ("aggressive", "scalp", "momentum"):
        ok, res = context_manager.apply_preset("AGGRESSIVE", updated_by=user_name)
        await update.message.reply_text(res, parse_mode=ParseMode.HTML)
        return

    if arg_lower in ("memecoin", "meme", "degen"):
        ok, res = context_manager.apply_preset("MEMECOIN", updated_by=user_name)
        await update.message.reply_text(res, parse_mode=ParseMode.HTML)
        return

    # User provided custom text
    ok, res = context_manager.set_context(
        instruction=full_arg,
        preset_name="CUSTOM",
        updated_by=user_name,
    )
    await update.message.reply_text(res, parse_mode=ParseMode.HTML)


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually triggers selling of a specific token position."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: <code>/sell &lt;Solana CA&gt; [pct]</code> (e.g. <code>/sell 2e3z... 100</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    mint = args[0].strip()
    pct = float(args[1]) if len(args) > 1 else 100.0

    # Hard safety check: never sell USDT
    if mint == config.USDT_SOLANA_MINT:
        await update.message.reply_text(
            "⛔ <b>Safety Error:</b> Cannot sell USDT. Automated USDT -> SOL conversion is strictly prohibited.",
            parse_mode=ParseMode.HTML,
        )
        return

    prompt = f"SELL {pct:.1f}% of token {mint}"
    await execute_council_session(
        chat_id=chat_id,
        prompt=prompt,
        mode="execute",
        reply_to_id=update.message.message_id,
    )


async def autopilot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manages autonomous background loop and customizable scan frequency:
    - /auto : View dashboard + 1-click frequency selector buttons
    - /auto on [time] : Start autonomous loop (e.g. /auto on 15m, /auto on 1h, /auto on 1w)
    - /auto off : Stop autonomous loop
    - /auto <time> : Update scan frequency (e.g. /auto 1m, 15m, 30m, 1h, 6h, 1d, 1w)
    - /auto interval <time> / /auto freq <time> : Update scan frequency
    """
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args or []

    lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
    if not lead_bot:
        await update.message.reply_text("❌ Council Orchestrator is not ready.")
        return

    if not args:
        stats = autopilot.get_status()
        wallet_info = await MSnifferExecutor.get_wallet_status()
        sol_bal = wallet_info.get("sol_balance", 0.0)
        dashboard = format_autopilot_dashboard(stats, sol_bal)
        await update.message.reply_text(
            dashboard,
            reply_markup=build_autopilot_keyboard(stats["scan_interval"], stats["is_running"]),
            parse_mode=ParseMode.HTML,
        )
        return

    first_arg = args[0].strip().lower()

    if first_arg in ("on", "start", "run"):
        requested_interval = None
        if len(args) > 1:
            raw_time = " ".join(args[1:])
            parsed_sec = autopilot.parse_duration_string(raw_time)
            if parsed_sec:
                autopilot.set_interval(parsed_sec)
                requested_interval = parsed_sec

        success, msg = await autopilot.start(
            chat_id=chat_id,
            lead_bot=lead_bot,
            council_orchestrator=council,
            scan_interval=requested_interval,
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    elif first_arg in ("off", "stop", "halt"):
        msg = await autopilot.stop()
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    elif first_arg in ("freq", "interval", "every", "time"):
        if len(args) < 2:
            curr_freq = autopilot.format_interval(autopilot.scan_interval_seconds)
            await update.message.reply_text(
                f"⏱️ <b>Current Scan Frequency:</b> <code>Every {curr_freq}</code>\n\n"
                f"Usage: <code>/auto interval &lt;time&gt;</code>\n"
                f"Examples: <code>1m</code>, <code>15m</code>, <code>30m</code>, <code>1h</code>, <code>6h</code>, <code>1d</code>, <code>1w</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        time_str = " ".join(args[1:])
        sec = autopilot.parse_duration_string(time_str)
        if not sec:
            await update.message.reply_text(
                f"⚠️ Unrecognized time duration '{time_str}'.\n"
                f"Examples: <code>1m</code>, <code>15m</code>, <code>30m</code>, <code>1h</code>, <code>6h</code>, <code>24h</code>, <code>1w</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        success, msg = autopilot.set_interval(sec)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    else:
        # Check if argument is a duration string (e.g. /auto 15m, /auto 1h, /auto 1 week)
        full_time_str = " ".join(args)
        sec = autopilot.parse_duration_string(full_time_str)
        if sec:
            success, msg = autopilot.set_interval(sec)
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            return

        # Fallback to dashboard with note
        stats = autopilot.get_status()
        wallet_info = await MSnifferExecutor.get_wallet_status()
        sol_bal = wallet_info.get("sol_balance", 0.0)
        dashboard = format_autopilot_dashboard(stats, sol_bal)
        await update.message.reply_text(
            dashboard,
            reply_markup=build_autopilot_keyboard(stats["scan_interval"], stats["is_running"]),
            parse_mode=ParseMode.HTML,
        )


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
        safe_old = html.escape(old_task)
        msgs.append(f"🛑 Cancelled active session: <b>{safe_old}</b>")

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


async def _handle_post_execution(
    prompt: str,
    exec_res,
    lead_bot: Optional[Bot],
    chat_id: int,
    exit_strategy: Optional[str] = None,
):
    """
    Handles position registration, automated exit monitoring, and profit vault sweeps
    following consensus trade execution.
    """
    if not exec_res or not exec_res.success:
        return

    mint, action, amount, symbol = SignalExtractor.extract(prompt)
    if not mint or mint == config.USDT_SOLANA_MINT:
        return

    if action == "BUY":
        raw_telem = await autopilot._enrich_candidate_raw(mint)
        price = raw_telem.get("price_usd", 0.0) if raw_telem else 0.0
        tok_name = raw_telem.get("name", symbol or "Token") if raw_telem else (symbol or "Token")
        tok_sym = raw_telem.get("symbol", symbol or "TOKEN") if raw_telem else (symbol or "TOKEN")

        strat = exit_strategy or config.DEFAULT_EXIT_STRATEGY
        pos = position_manager.open_position(
            mint=mint,
            symbol=tok_sym,
            name=tok_name,
            entry_price_usd=price,
            amount_sol=amount,
            tx_hash=exec_res.tx_hash or "",
            exit_strategy=strat,
        )
        strat_badge = "💎 HOLD INDEFINITELY" if pos.exit_strategy == "HOLD_INDEFINITE" else f"🎯 AUTO-EXIT (+{config.TAKE_PROFIT_PCT:.0f}%)"
        logger.info(f"📈 Position registered: {tok_sym} ({mint}) at ${price:.6f} | Strategy: {strat_badge}")

    elif action == "SELL":
        raw_telem = await autopilot._enrich_candidate_raw(mint)
        curr_price = raw_telem.get("price_usd", 0.0) if raw_telem else 0.0
        position_manager.close_position(
            mint=mint,
            exit_price_usd=curr_price,
            exit_reason="Manual execution or council exit",
            exit_tx_hash=exec_res.tx_hash or "",
        )
        logger.info(f"📉 Position closed for {mint} at ${curr_price:.6f}")

        # Trigger Profit Vault Sweep check
        wallet_info = await MSnifferExecutor.get_wallet_status()
        if wallet_info.get("online") and lead_bot:
            await profit_vault.check_and_sweep(
                current_sol_balance=wallet_info.get("sol_balance", 0.0),
                broadcaster_bot=lead_bot,
                reply_to_chat_id=chat_id,
            )


async def execute_council_session(
    chat_id: int,
    prompt: str,
    mode: str,
    reply_to_id: Optional[int] = None,
    mood_override: Optional[str] = None,
    exit_strategy: Optional[str] = None,
    mint_override: Optional[str] = None,
) -> bool:
    """
    Centralized execution engine for all Council modes:
    1. debate (/deb): Verbal debate across the 5 models using real metrics, no vote.
    2. debate_vote (/debvote, /dv, /vot): Verbal debate, then formal vote with consensus tally.
    3. dve (/dve): Debate, Vote & Execute Mode.
    4. execute (/exe): FAST & SILENT background evaluation & voting. Carries out buying/selling
       silently and responds that they've bought or sold due to consensus.
    """
    global is_busy, active_task_name

    if not council:
        logger.error("Council orchestrator not initialized.")
        return False

    lead_bot = council.broadcaster.get_bot(council.agents_config[0].name)

    if is_busy:
        if lead_bot:
            safe_task = html.escape(active_task_name)
            await lead_bot.send_message(
                chat_id=chat_id,
                text=f"⏳ The council is currently busy with: <b>{safe_task}</b>. Please wait or use /cancel.",
                parse_mode=ParseMode.HTML,
            )
        return False

    old_mood = council.current_mood
    if mood_override and mood_override in config.MOOD_PROMPTS:
        council.set_mood(mood_override)

    # 1. Detect Solana CA and enrich with real on-chain dossier
    mint, action, amount, symbol = SignalExtractor.extract(prompt)
    if mint_override:
        mint = mint_override

    # If no mint but $TICKER or token symbol detected, auto-resolve via DexScreener
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)
    if not mint and mode in ("dve", "execute", "exe", "debate_vote", "debvote", "dv"):
        first_token = prompt.strip().split()[0].replace("$", "").upper()
        if 2 <= len(first_token) <= 10 and first_token.isalnum() and first_token not in ("BUY", "SELL", "WHAT", "HOW", "WHY"):
            resolved = await telemetry.search_solana_token_mint(first_token)
            if resolved:
                mint = resolved

    dossier = None
    if mint:
        dossier = await telemetry.fetch_token_dossier(mint)
        if dossier and dossier.get("mint"):
            mint = dossier["mint"]

    if dossier:
        tok_name = dossier.get("name", "Token")
        tok_symbol = dossier.get("symbol", "TOKEN")
        dex = dossier.get("dex", "Solana DEX")
        target_display = f"{tok_name} (${tok_symbol})"
        enriched_prompt = telemetry.format_council_prompt_from_dossier(
            dossier=dossier,
            action=action,
            amount_sol=amount,
            user_context=prompt,
        )
    else:
        tok_name = symbol or ""
        tok_symbol = symbol or ""
        dex = "Solana DEX"
        target_display = prompt[:35]
        enriched_prompt = prompt

    is_busy = True
    active_task_name = f"{mode.upper()}: {target_display}"

    try:
        # Strictly crypto-only council enforcement
        if not mint and not dossier and not is_crypto_related(prompt):
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        "🏛️ <b>Chatters Council Directive: Strictly Crypto Scope</b>\n\n"
                        "This council operates exclusively on cryptocurrency, Solana tokens, DEX liquidity, and trade execution.\n"
                        "Please provide a Solana CA, $ticker, or crypto trading question."
                    ),
                )
            return False

        # Check if user invoked execution mode without a Solana CA or resolvable ticker
        if mode in ("dve", "execute", "exe") and not mint:
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        "⚠️ <b>Execution mode requires a Solana Contract Address (CA) or $ticker.</b>\n\n"
                        "• Examples: <code>/exe $BONK</code> or <code>/exe &lt;Solana CA&gt;</code>\n"
                        "• For crypto token debates: <code>/deb &lt;$ticker or CA&gt;</code>\n"
                        "• For debates with a formal majority vote: <code>/vot &lt;$ticker or CA&gt;</code>"
                    ),
                )
            return False

        # -------------------------------------------------------------
        # MODE 1: DEBATE ONLY (/deb)
        # -------------------------------------------------------------
        if mode in ("debate", "deb"):
            if lead_bot:
                if dossier:
                    card_html = telemetry.format_telegram_token_card(dossier, action=action, amount_sol=amount)
                    await safe_send_message(bot=lead_bot, chat_id=chat_id, text=card_html, disable_web_page_preview=True)
                else:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=f"🎙️ <b>Topic:</b> <i>{html.escape(prompt)}</i>",
                    )
            success = await council.run_debate(prompt=enriched_prompt, chat_id=chat_id, reply_to_id=reply_to_id, silent=False)
            return success

        # -------------------------------------------------------------
        # MODE 2: DEBATE AND VOTE (/debvote, /dv, /vot, /vote)
        # -------------------------------------------------------------
        elif mode in ("debate_vote", "debvote", "dv", "vot", "vote"):
            if lead_bot:
                if dossier:
                    card_html = telemetry.format_telegram_token_card(dossier, action=action, amount_sol=amount)
                    await safe_send_message(bot=lead_bot, chat_id=chat_id, text=card_html, disable_web_page_preview=True)
                else:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=(
                            f"🗳️ <b>COUNCIL DEBATE & VOTE</b>\n"
                            f"───────────────────────────────\n"
                            f"📌 <b>Topic:</b> <i>{html.escape(prompt)}</i>\n"
                            f"👥 <b>Voters:</b> Claude, GPT, DeepSeek, Gemini, Grok\n"
                            f"⚖️ <b>Consensus:</b> <code>50% Majority</code>\n"
                            f"───────────────────────────────"
                        ),
                    )
            vote_summary = await council.run_debate_and_vote(prompt=enriched_prompt, chat_id=chat_id, reply_to_id=reply_to_id, silent=False)
            if lead_bot:
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=vote_summary.summary_text,
                )
            return vote_summary.consensus_passed

        # -------------------------------------------------------------
        # MODE 3: DEBATE, VOTE & EXECUTE (/dve)
        # -------------------------------------------------------------
        elif mode == "dve":
            if lead_bot:
                if dossier:
                    card_html = telemetry.format_telegram_token_card(dossier, action=action, amount_sol=amount)
                    await safe_send_message(bot=lead_bot, chat_id=chat_id, text=card_html, disable_web_page_preview=True)
                else:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=(
                            f"🚀 <b>FULL DVE PIPELINE (DEBATE ➔ VOTE ➔ EXECUTE)</b>\n"
                            f"───────────────────────────────\n"
                            f"📌 <b>Target:</b> <i>{html.escape(target_display)}</i>\n"
                            f"🎭 <b>Mood:</b> <code>{council.current_mood.upper()}</code>\n"
                            f"🐕 <b>Execution Bridge:</b> Market Sniffer\n"
                            f"───────────────────────────────\n"
                            f"<b>Phase 1: Multi-Agent Debate Commencing...</b>"
                        ),
                    )
            vote_summary = await council.run_debate_and_vote(prompt=enriched_prompt, chat_id=chat_id, reply_to_id=reply_to_id, silent=False)
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
                await _handle_post_execution(
                    prompt=prompt,
                    exec_res=exec_res,
                    lead_bot=lead_bot,
                    chat_id=chat_id,
                    exit_strategy=exit_strategy,
                )
                return exec_res.success
            else:
                if lead_bot:
                    safe_name = html.escape(tok_name)
                    safe_sym = html.escape(tok_symbol)
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=f"🛑 <b>CONSENSUS REJECTED:</b> Council voted against executing on <b>{safe_name}</b> (${safe_sym}).",
                    )
                return False

        # -------------------------------------------------------------
        # MODE 4: VOTE & EXECUTE (/exe) - SILENT / FAST BACKGROUND MODE
        # -------------------------------------------------------------
        elif mode in ("execute", "exe"):
            # Carry out buying and selling silently in background without sending 5-10 messages!
            if lead_bot:
                safe_name = html.escape(tok_name)
                safe_sym = html.escape(tok_symbol)
                await safe_send_message(
                    bot=lead_bot,
                    chat_id=chat_id,
                    text=(
                        f"⚡ <b>COUNCIL EVALUATING: {safe_name} (${safe_sym})</b>\n"
                        f"<i>Conducting background deliberation & voting...</i>"
                    ),
                )

            # Silent in-memory evaluation
            vote_summary = await council.run_vote(prompt=enriched_prompt, chat_id=chat_id, reply_to_id=reply_to_id, silent=True)

            if vote_summary.consensus_passed:
                exec_res = await MSnifferExecutor.execute_consensus(
                    prompt=prompt,
                    consensus_text=vote_summary.summary_text,
                    yes_count=vote_summary.yes_count,
                    total_votes=vote_summary.total_votes,
                    broadcaster_bot=lead_bot,
                    reply_to_chat_id=chat_id,
                )

                safe_name = html.escape(tok_name)
                safe_sym = html.escape(tok_symbol)
                solscan_link = f"\n🔗 <a href='https://solscan.io/tx/{exec_res.tx_hash}'>View Solana Transaction</a>" if exec_res.tx_hash else ""
                approval_pct = (vote_summary.yes_count / vote_summary.total_votes * 100.0) if vote_summary.total_votes > 0 else 100.0

                if action == "BUY":
                    result_msg = (
                        f"✅ <b>COUNCIL CONSENSUS APPROVED: BOUGHT {safe_name}</b>\n"
                        f"───────────────────────────────\n"
                        f"🪙 <b>Token:</b> {safe_name} (<code>${safe_sym}</code>)\n"
                        f"⚡ <b>Action:</b> BUY <code>{amount:.2f} SOL</code> on {dex}\n"
                        f"📊 <b>Consensus:</b> <code>{vote_summary.yes_count}/{vote_summary.total_votes} YES ({approval_pct:.0f}%)</code>\n"
                        f"🚀 <b>Execution:</b> {html.escape(exec_res.message)}"
                        f"{solscan_link}\n"
                        f"───────────────────────────────"
                    )
                else:
                    result_msg = (
                        f"🎯 <b>COUNCIL CONSENSUS APPROVED: SOLD {safe_name}</b>\n"
                        f"───────────────────────────────\n"
                        f"🪙 <b>Token:</b> {safe_name} (<code>${safe_sym}</code>)\n"
                        f"⚡ <b>Action:</b> SOLD <code>{amount:.0f}%</code> back to SOL\n"
                        f"📊 <b>Consensus:</b> <code>{vote_summary.yes_count}/{vote_summary.total_votes} YES ({approval_pct:.0f}%)</code>\n"
                        f"🚀 <b>Execution:</b> {html.escape(exec_res.message)}"
                        f"{solscan_link}\n"
                        f"───────────────────────────────"
                    )

                if lead_bot:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=result_msg,
                        disable_web_page_preview=True,
                    )

                await _handle_post_execution(
                    prompt=prompt,
                    exec_res=exec_res,
                    lead_bot=lead_bot,
                    chat_id=chat_id,
                    exit_strategy=exit_strategy,
                )
                return exec_res.success
            else:
                safe_name = html.escape(tok_name)
                safe_sym = html.escape(tok_symbol)
                rejection_msg = (
                    f"🛑 <b>COUNCIL CONSENSUS REJECTED: {safe_name}</b>\n"
                    f"───────────────────────────────\n"
                    f"🪙 <b>Token:</b> {safe_name} (<code>${safe_sym}</code>)\n"
                    f"📊 <b>Vote Breakdown:</b> <code>{vote_summary.yes_count} YES / {vote_summary.no_count} NO / {vote_summary.abstain_count} ABSTAIN</code>\n"
                    f"🛡️ <b>Action:</b> Trade cancelled due to lack of majority consensus to protect treasury.\n"
                    f"───────────────────────────────"
                )
                if lead_bot:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=rejection_msg,
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
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else ""
    mint, action, amount, symbol = SignalExtractor.extract(prompt, reply_text=reply_text)
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)
    if not prompt and not mint:
        await update.message.reply_text("🗣️ <b>Usage:</b> <code>/deb &lt;CA, $ticker, or crypto topic&gt;</code>", parse_mode=ParseMode.HTML)
        return
    full_prompt = prompt or f"{action} {amount:.2f} SOL of token {mint}"
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(
        chat_id=chat_id,
        prompt=full_prompt,
        mode="debate",
        reply_to_id=update.message.message_id,
        mint_override=mint if mint else None,
    )


async def vot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /vot <prompt> (Debate and Vote Mode)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else ""
    mint, action, amount, symbol = SignalExtractor.extract(prompt, reply_text=reply_text)
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)
    if not prompt and not mint:
        await update.message.reply_text("🗳️ <b>Usage:</b> <code>/vot &lt;CA, $ticker, or trade proposition&gt;</code>", parse_mode=ParseMode.HTML)
        return
    full_prompt = prompt or f"{action} {amount:.2f} SOL of token {mint}"
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(
        chat_id=chat_id,
        prompt=full_prompt,
        mode="debate_vote",
        reply_to_id=update.message.message_id,
        mint_override=mint if mint else None,
    )


async def debvote_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /debvote or /dv <prompt> (Debate and Vote Mode)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else ""
    mint, action, amount, symbol = SignalExtractor.extract(prompt, reply_text=reply_text)
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)
    if not prompt and not mint:
        await update.message.reply_text("🗳️ <b>Usage:</b> <code>/debvote &lt;CA, $ticker, or trade proposition&gt;</code>", parse_mode=ParseMode.HTML)
        return
    full_prompt = prompt or f"{action} {amount:.2f} SOL of token {mint}"
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(
        chat_id=chat_id,
        prompt=full_prompt,
        mode="debate_vote",
        reply_to_id=update.message.message_id,
        mint_override=mint if mint else None,
    )


async def exe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /exe <prompt> (Vote & Execute Mode)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else ""
    mint, action, amount, symbol = SignalExtractor.extract(prompt, reply_text=reply_text)
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)
    if not prompt and not mint:
        await update.message.reply_text("⚡ <b>Usage:</b> <code>/exe &lt;CA or $ticker&gt;</code>", parse_mode=ParseMode.HTML)
        return
    full_prompt = prompt or f"{action} {amount:.2f} SOL of token {mint}"
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(
        chat_id=chat_id,
        prompt=full_prompt,
        mode="execute",
        reply_to_id=update.message.message_id,
        mint_override=mint if mint else None,
    )


async def dve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /dve <prompt> (Debate, Vote & Execute Mode - Full Pipeline)."""
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return
    prompt = extract_prompt(update, context)
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else ""
    mint, action, amount, symbol = SignalExtractor.extract(prompt, reply_text=reply_text)
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)
    if not prompt and not mint:
        await update.message.reply_text("🚀 <b>Usage:</b> <code>/dve &lt;CA or $ticker&gt;</code>", parse_mode=ParseMode.HTML)
        return
    full_prompt = prompt or f"{action} {amount:.2f} SOL of token {mint}"
    chat_id = check_and_record_chat_id(update)
    await execute_council_session(
        chat_id=chat_id,
        prompt=full_prompt,
        mode="dve",
        reply_to_id=update.message.message_id,
        mint_override=mint if mint else None,
    )


def build_autopilot_keyboard(current_interval: int, is_running: bool) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for choosing scan frequency and starting/stopping autopilot."""
    def lbl(sec: int, name: str) -> str:
        return f"{name} ✅" if current_interval == sec else name

    row1 = [
        InlineKeyboardButton(lbl(60, "⏱️ 1 Min"), callback_data="setfreq:60"),
        InlineKeyboardButton(lbl(900, "⏱️ 15 Min"), callback_data="setfreq:900"),
        InlineKeyboardButton(lbl(1800, "⏱️ 30 Min"), callback_data="setfreq:1800"),
    ]
    row2 = [
        InlineKeyboardButton(lbl(3600, "⏱️ 1 Hour"), callback_data="setfreq:3600"),
        InlineKeyboardButton(lbl(21600, "⏱️ 6 Hours"), callback_data="setfreq:21600"),
        InlineKeyboardButton(lbl(86400, "⏱️ 24 Hours"), callback_data="setfreq:86400"),
    ]
    row3 = [
        InlineKeyboardButton(lbl(604800, "⏱️ 1 Week"), callback_data="setfreq:604800"),
        InlineKeyboardButton("🔄 Refresh Status", callback_data="auto:refresh"),
    ]
    toggle_label = "🛑 Stop Autopilot" if is_running else "🟢 Start Autopilot"
    row4 = [
        InlineKeyboardButton(toggle_label, callback_data="auto:toggle"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


def format_autopilot_dashboard(stats: Dict[str, Any], wallet_sol: float) -> str:
    """Formats the comprehensive autonomous trading dashboard."""
    st_badge = "🟢 ACTIVE (Scanning & Screening)" if stats["is_running"] else "⚪ STOPPED (Idle)"
    freq_desc = stats.get("formatted_interval") or autopilot.format_interval(stats["scan_interval"])

    return (
        f"🤖 <b>AI COUNCIL AUTONOMOUS TRADING ENGINE</b>\n"
        f"───────────────────────────────\n"
        f"⚡ <b>Engine Status:</b> {st_badge}\n"
        f"⏱️ <b>Scan Frequency:</b> <code>Every {freq_desc}</code>\n"
        f"🔄 <b>Cycles Run:</b> <code>{stats['cycles']}</code>\n"
        f"🔍 <b>Breakouts Evaluated:</b> <code>{stats['evaluated_count']}</code>\n"
        f"✅ <b>Approved by Consensus:</b> <code>{stats['approved']}</code>\n"
        f"❌ <b>Rejected by Consensus:</b> <code>{stats['rejected']}</code>\n"
        f"🚀 <b>Trades Dispatched:</b> <code>{stats['executed']}</code>\n"
        f"📈 <b>Open Positions:</b> <code>{stats['open_positions_count']}</code>\n"
        f"🏦 <b>Profit Vault Harvested:</b> <code>${stats['total_usdt_vaulted']:.2f} USDT</code>\n"
        f"💰 <b>Wallet Balance:</b> <code>{wallet_sol:.4f} SOL</code>\n"
        f"───────────────────────────────\n"
        f"<b>Frequency Selection:</b>\n"
        f"• Tap a preset button below to switch frequency instantly.\n"
        f"• Or type <code>/auto &lt;time&gt;</code> (e.g. <code>/auto 1m</code>, <code>/auto 15m</code>, <code>/auto 30m</code>, <code>/auto 1h</code>, <code>/auto 1w</code>).\n\n"
        f"<b>Controls:</b>\n"
        f"• <code>/auto on [time]</code> - Activate autonomous background engine\n"
        f"• <code>/auto off</code> - Halt autonomous engine"
    )


def build_ca_keyboard(
    mint: str,
    selected_amount: Optional[float] = None,
    strategy: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """
    Builds a clean, simple 4-button inline keyboard for token actions:
    Row 1: Buy Execution (Vote & Buy | Full DVE)
    Row 2: Discussion (Debate & Vote | Debate Only)
    """
    if selected_amount is None:
        selected_amount = config.DEFAULT_TRADE_AMOUNT_SOL
    selected_amount = config.validate_investment_amount(selected_amount)

    strat_code = "h" if strategy and strategy.upper() == "HOLD_INDEFINITE" else "a"

    row1 = [
        InlineKeyboardButton(f"⚡ Vote & Buy ({selected_amount:.2f} SOL)", callback_data=f"exe:{mint}:{selected_amount:.2f}:{strat_code}"),
        InlineKeyboardButton(f"🚀 Full DVE ({selected_amount:.2f} SOL)", callback_data=f"dve:{mint}:{selected_amount:.2f}:{strat_code}"),
    ]
    row2 = [
        InlineKeyboardButton("🗳️ Debate & Vote", callback_data=f"dv:{mint}:{selected_amount:.2f}:{strat_code}"),
        InlineKeyboardButton("🎙️ Debate Only", callback_data=f"deb:{mint}:{selected_amount:.2f}:{strat_code}"),
    ]
    return InlineKeyboardMarkup([row1, row2])


def build_invest_keyboard() -> InlineKeyboardMarkup:
    """Builds the inline keyboard for the /invest command."""
    current = config.DEFAULT_TRADE_AMOUNT_SOL
    row = []
    for opt in config.ALLOWED_INVESTMENT_OPTIONS:
        label = f"{opt:.2f} SOL ✅" if abs(opt - current) < 0.001 else f"{opt:.2f} SOL"
        row.append(InlineKeyboardButton(label, callback_data=f"setinv:{opt:.2f}"))
    return InlineKeyboardMarkup([row])


def build_vault_keyboard() -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for customizing Profit Vault harvest threshold."""
    current = profit_vault.threshold_usd
    row1 = []
    for th in config.ALLOWED_VAULT_THRESHOLDS:
        label = f"${th:.0f} ✅" if abs(th - current) < 0.01 else f"${th:.0f}"
        row1.append(InlineKeyboardButton(label, callback_data=f"setvault:{th:.0f}"))

    row2 = [InlineKeyboardButton("🔄 Sync Baseline to Live SOL", callback_data="setvault:reset")]
    return InlineKeyboardMarkup([row1, row2])


def build_exitmode_keyboard() -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for toggling Council default exit strategy."""
    strat = config.DEFAULT_EXIT_STRATEGY
    runner_label = f"🚀 Long Runner (-{config.TRAILING_STOP_PCT:.0f}%) ✅" if strat == "TRAILING_RUNNER" else f"🚀 Long Runner (-{config.TRAILING_STOP_PCT:.0f}%)"
    two_x_label = "🎯 Fixed 2x (+100%) ✅" if strat in ("2X_TAKE_PROFIT", "AUTO_EXIT") else "🎯 Fixed 2x (+100%)"
    hold_label = "💎 Hold Indefinite ✅" if strat == "HOLD_INDEFINITE" else "💎 Hold Indefinite"
    row1 = [
        InlineKeyboardButton(runner_label, callback_data="setstrat:runner"),
        InlineKeyboardButton(two_x_label, callback_data="setstrat:2x"),
    ]
    row2 = [
        InlineKeyboardButton(hold_label, callback_data="setstrat:hold"),
    ]
    return InlineKeyboardMarkup([row1, row2])


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
    2. Checks for flags (-dve, -exe, -dv, -vot, -deb, -auto, -a <amt>, -m <mood>)
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

    # If no mint but $TICKER detected, auto-resolve Solana mint via DexScreener
    if not mint and symbol:
        resolved_mint = await telemetry.search_solana_token_mint(symbol)
        if resolved_mint:
            mint = resolved_mint

    # If still no mint, check if text is a single token ticker
    if not mint:
        words = text.strip().split()
        if len(words) == 1 and 2 <= len(words[0]) <= 10 and words[0].isalnum():
            candidate = words[0].replace("$", "").upper()
            if candidate in ("BONK", "WIF", "POPCAT", "BOME", "FARTCOIN", "ACT", "PNUT", "GOAT", "MOODENG", "JUP", "RAY"):
                resolved_mint = await telemetry.search_solana_token_mint(candidate)
                if resolved_mint:
                    mint = resolved_mint

    # -----------------------------------------------------------------
    # REGULAR CONVERSATIONS (NO CA DETECTED - STRICTLY CRYPTO SCOPE)
    # -----------------------------------------------------------------
    if not mint:
        is_private = bool(update.effective_chat and update.effective_chat.type == "private")
        lower_text = text.lower()

        # 1. Detect if a specific bot was addressed
        target_agent = None
        for agent_cfg in config.get_active_agents():
            handles = [
                f"@{agent_cfg.username.lower()}",
                f"{agent_cfg.name.lower()},",
                f"{agent_cfg.name.lower()}:",
                f"hey {agent_cfg.name.lower()}",
            ]
            if any(h in lower_text for h in handles):
                target_agent = agent_cfg.name
                break

        # 2. Check if message is replying to a specific bot message
        if not target_agent and update.message.reply_to_message and update.message.reply_to_message.from_user:
            rep_user = update.message.reply_to_message.from_user
            if rep_user.is_bot:
                for agent_cfg in config.get_active_agents():
                    if rep_user.username and rep_user.username.lower() == agent_cfg.username.lower():
                        target_agent = agent_cfg.name
                        break
                    elif rep_user.first_name and agent_cfg.name.lower() in rep_user.first_name.lower():
                        target_agent = agent_cfg.name
                        break

        # 3. Clean message prompt
        clean_text = text
        for h in ("@claudetalkbbot", "@gpttalkbbot", "@deepseektalkbbot", "@geminitalkbbot", "@groktalkbbot", "@council", "@chatters"):
            clean_text = re.sub(re.escape(h), "", clean_text, flags=re.IGNORECASE).strip()
        clean_text = re.sub(r"^(?:council|chatters|claude|gpt|deepseek|gemini|grok):\s*", "", clean_text, flags=re.IGNORECASE).strip()

        if not clean_text:
            return

        crypto_valid = is_crypto_related(clean_text)

        # Case A: Private DM or addressed to a specific bot
        if is_private or target_agent:
            agent_to_respond = target_agent or "Claude"
            if not crypto_valid:
                lead_bot = council.broadcaster.get_bot(agent_to_respond) if council else None
                if lead_bot:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=(
                            f"🏛️ <b>Chatters Council ({agent_to_respond}):</b>\n"
                            f"My analysis is strictly reserved for cryptocurrency, Solana tokens, liquidity, and on-chain security. "
                            f"Please provide a token CA, $ticker, or crypto trading question."
                        ),
                        reply_to_id=update.message.message_id,
                    )
                return

            await council.run_single_agent_reply(
                agent_name=agent_to_respond,
                prompt=clean_text,
                chat_id=chat_id,
                reply_to_id=update.message.message_id,
            )
            return

        # Case B: In group chat, if addressed explicitly to council
        council_triggers = ("@council", "council:", "chatters:", "@chatters")
        is_council_addressed = any(t in lower_text for t in council_triggers)

        if is_council_addressed:
            if not crypto_valid:
                lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
                if lead_bot:
                    await safe_send_message(
                        bot=lead_bot,
                        chat_id=chat_id,
                        text=(
                            "🏛️ <b>Chatters Council Directive: Strictly Crypto Scope</b>\n\n"
                            "The council communicates exclusively about cryptocurrency, Solana tokens, DEX liquidity, and trade execution.\n"
                            "Please provide a token CA, $ticker, or crypto question."
                        ),
                        reply_to_id=update.message.message_id,
                    )
                return

            await council.run_debate(
                prompt=clean_text,
                chat_id=chat_id,
                reply_to_id=update.message.message_id,
                silent=False,
            )
            return

        # ELIMINATE RANDOM TALK: Silently ignore all other group conversation
        return

    # -----------------------------------------------------------------
    # SOLANA TOKEN ACTIONS (CA DETECTED)
    # -----------------------------------------------------------------
    # Parse flags
    mode = None
    if re.search(r"(?:^|\s)(?:--?dve)\b", text, re.IGNORECASE):
        mode = "dve"
    elif re.search(r"(?:^|\s)(?:--?(?:exe|execute))\b", text, re.IGNORECASE):
        mode = "exe"
    elif re.search(r"(?:^|\s)(?:--?(?:debvote|dv))\b", text, re.IGNORECASE):
        mode = "debate_vote"
    elif re.search(r"(?:^|\s)(?:--?(?:vot|vote))\b", text, re.IGNORECASE):
        mode = "debate_vote"
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

    # Strategy flag: -hold / --hold (Diamond Hands) or -auto-exit / -tp
    exit_strategy = None
    if re.search(r"(?:^|\s)(?:--?hold)\b", text, re.IGNORECASE):
        exit_strategy = "HOLD_INDEFINITE"
    elif re.search(r"(?:^|\s)(?:--?(?:auto-?exit|tp))\b", text, re.IGNORECASE):
        exit_strategy = "AUTO_EXIT"

    # Take-Profit percentage override flag (-tp 75 or --tp 75)
    tp_match = re.search(r"(?:-tp|--tp)\s+(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if tp_match:
        try:
            config.TAKE_PROFIT_PCT = float(tp_match.group(1))
        except ValueError:
            pass

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
    if mode in ("dve", "exe", "vote", "debate", "debate_vote"):
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
            exit_strategy=exit_strategy,
            mint_override=mint,
        )
        return

    # Check if this is a conversational query or question about the token
    is_private = bool(update.effective_chat and update.effective_chat.type == "private")
    lower_text = text.lower()

    target_agent = None
    for agent_cfg in config.get_active_agents():
        handles = [
            f"@{agent_cfg.username.lower()}",
            f"{agent_cfg.name.lower()},",
            f"{agent_cfg.name.lower()}:",
            f"hey {agent_cfg.name.lower()}",
        ]
        if any(h in lower_text for h in handles):
            target_agent = agent_cfg.name
            break

    if not target_agent and update.message.reply_to_message and update.message.reply_to_message.from_user:
        rep_user = update.message.reply_to_message.from_user
        if rep_user.is_bot:
            for agent_cfg in config.get_active_agents():
                if rep_user.username and rep_user.username.lower() == agent_cfg.username.lower():
                    target_agent = agent_cfg.name
                    break
                elif rep_user.first_name and agent_cfg.name.lower() in rep_user.first_name.lower():
                    target_agent = agent_cfg.name
                    break

    # If addressed to a single bot or in private DM with questions about this CA
    if is_private or target_agent:
        agent_to_respond = target_agent or "Claude"
        await council.run_single_agent_reply(
            agent_name=agent_to_respond,
            prompt=text,
            chat_id=chat_id,
            reply_to_id=update.message.message_id,
        )
        return

    # If conversational question or addressed to council
    council_triggers = ("@council", "council", "chatters", "everyone", "guys", "bots", "all")
    is_council_addressed = any(t in lower_text for t in council_triggers)
    is_question = "?" in text or any(lower_text.startswith(w) for w in ("what", "how", "why", "who", "when", "is", "can", "should", "tell", "explain", "think", "which", "analyze"))

    if is_council_addressed or is_question:
        await execute_council_session(
            chat_id=chat_id,
            prompt=text,
            mode="debate",
            reply_to_id=update.message.message_id,
            mint_override=mint,
        )
        return

    # 6. If NO flag attached and no conversation (pure CA posting), present rich Token Dossier card with 1-click buttons!
    strat_to_use = exit_strategy or config.DEFAULT_EXIT_STRATEGY
    reply_markup = build_ca_keyboard(mint=mint, selected_amount=amount, strategy=strat_to_use)

    dossier = await telemetry.fetch_token_dossier(mint)
    if dossier:
        card_text = telemetry.format_telegram_token_card(dossier, action="BUY", amount_sol=amount)
    else:
        dex_url = f"https://dexscreener.com/solana/{mint}"
        card_text = (
            f"🪙 <b>Solana Token Detected:</b> <code>{mint}</code>\n"
            f"📊 <a href='{dex_url}'>View DexScreener Chart</a>\n"
            f"💰 <b>Default Allocation:</b> <code>{amount:.2f} SOL</code>\n"
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

    # 1. Handle Autopilot Frequency, Refresh, and Toggle
    if data.startswith("setfreq:"):
        parts = data.split(":")
        if len(parts) >= 2:
            try:
                sec = int(parts[1])
                autopilot.set_interval(sec)
                stats = autopilot.get_status()
                wallet_info = await MSnifferExecutor.get_wallet_status()
                sol_bal = wallet_info.get("sol_balance", 0.0)
                dashboard = format_autopilot_dashboard(stats, sol_bal)
                await query.edit_message_text(
                    text=dashboard,
                    reply_markup=build_autopilot_keyboard(stats["scan_interval"], stats["is_running"]),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.debug(f"Error updating autopilot frequency: {e}")
        return

    if data == "auto:refresh":
        stats = autopilot.get_status()
        wallet_info = await MSnifferExecutor.get_wallet_status()
        sol_bal = wallet_info.get("sol_balance", 0.0)
        dashboard = format_autopilot_dashboard(stats, sol_bal)
        try:
            await query.edit_message_text(
                text=dashboard,
                reply_markup=build_autopilot_keyboard(stats["scan_interval"], stats["is_running"]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if data == "auto:toggle":
        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
        if not lead_bot:
            await query.message.reply_text("❌ Council Orchestrator is not ready.")
            return

        if autopilot.is_running:
            res_msg = await autopilot.stop()
        else:
            success, res_msg = await autopilot.start(chat_id=chat_id, lead_bot=lead_bot, council_orchestrator=council)

        stats = autopilot.get_status()
        wallet_info = await MSnifferExecutor.get_wallet_status()
        sol_bal = wallet_info.get("sol_balance", 0.0)
        dashboard = format_autopilot_dashboard(stats, sol_bal)
        try:
            await query.edit_message_text(
                text=dashboard,
                reply_markup=build_autopilot_keyboard(stats["scan_interval"], stats["is_running"]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await query.message.reply_text(res_msg, parse_mode=ParseMode.HTML)
        return

    # 2. Handle Investment Option selection on CA card (amt:<mint>:<val>:<strat>)
    if data.startswith("amt:"):
        parts = data.split(":")
        if len(parts) >= 3:
            mint = parts[1]
            try:
                new_amt = config.validate_investment_amount(float(parts[2]))
            except ValueError:
                new_amt = config.DEFAULT_TRADE_AMOUNT_SOL
            strat_code = parts[3] if len(parts) > 3 else "a"
            strat = "HOLD_INDEFINITE" if strat_code == "h" else "AUTO_EXIT"

            new_markup = build_ca_keyboard(mint=mint, selected_amount=new_amt, strategy=strat)
            dossier = await telemetry.fetch_token_dossier(mint)
            if dossier:
                updated_text = telemetry.format_telegram_token_card(dossier, action="BUY", amount_sol=new_amt)
                updated_text += "\n\n<i>Select an investment preset and exit strategy, then trigger an action below:</i>"
            else:
                dex_url = f"https://dexscreener.com/solana/{mint}"
                strat_desc = "💎 Hold Indefinitely" if strat == "HOLD_INDEFINITE" else f"🎯 Auto-Exit (+{config.TAKE_PROFIT_PCT:.0f}%)"
                updated_text = (
                    f"🪙 <b>Solana Token Detected:</b> <code>{mint}</code>\n"
                    f"📊 <a href='{dex_url}'>View DexScreener Chart</a>\n"
                    f"💰 <b>Selected Investment:</b> <code>{new_amt:.2f} SOL</code>\n"
                    f"🛡️ <b>Exit Strategy:</b> <code>{strat_desc}</code>\n\n"
                    f"<i>Select an investment preset and exit strategy, then trigger an action below:</i>"
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

    # 3. Handle Exit Strategy selection on CA card (strat:<mint>:<amt>:<strat>)
    if data.startswith("strat:"):
        parts = data.split(":")
        if len(parts) >= 4:
            mint = parts[1]
            try:
                amt = config.validate_investment_amount(float(parts[2]))
            except ValueError:
                amt = config.DEFAULT_TRADE_AMOUNT_SOL
            strat_code = parts[3]
            strat = "HOLD_INDEFINITE" if strat_code == "h" else "AUTO_EXIT"

            new_markup = build_ca_keyboard(mint=mint, selected_amount=amt, strategy=strat)
            dossier = await telemetry.fetch_token_dossier(mint)
            if dossier:
                updated_text = telemetry.format_telegram_token_card(dossier, action="BUY", amount_sol=amt)
                updated_text += "\n\n<i>Select an investment preset and exit strategy, then trigger an action below:</i>"
            else:
                dex_url = f"https://dexscreener.com/solana/{mint}"
                strat_desc = "💎 Hold Indefinitely" if strat == "HOLD_INDEFINITE" else f"🎯 Auto-Exit (+{config.TAKE_PROFIT_PCT:.0f}%)"
                updated_text = (
                    f"🪙 <b>Solana Token Detected:</b> <code>{mint}</code>\n"
                    f"📊 <a href='{dex_url}'>View DexScreener Chart</a>\n"
                    f"💰 <b>Selected Investment:</b> <code>{amt:.2f} SOL</code>\n"
                    f"🛡️ <b>Exit Strategy:</b> <code>{strat_desc}</code>\n\n"
                    f"<i>Select an investment preset and exit strategy, then trigger an action below:</i>"
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

    # 4. Handle Setting Global Default Investment Option (setinv:<val>)
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

    # 5. Handle Setting Vault Threshold or Resetting Baseline (setvault:<val>)
    if data.startswith("setvault:"):
        parts = data.split(":")
        if len(parts) >= 2:
            val_str = parts[1]
            if val_str == "reset":
                wallet_info = await MSnifferExecutor.get_wallet_status()
                sol_bal = wallet_info.get("sol_balance", 0.0)
                profit_vault.reset_baseline(sol_bal)
            else:
                try:
                    profit_vault.set_threshold(float(val_str))
                except ValueError:
                    pass
            wallet_info = await MSnifferExecutor.get_wallet_status()
            sol_bal = wallet_info.get("sol_balance", 0.0)
            text = profit_vault.get_status_text(current_sol_balance=sol_bal)
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=build_vault_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.debug(f"Error updating vault markup: {e}")
        return

    # 6. Handle Setting Default Exit Strategy (setstrat:<strat>)
    if data.startswith("setstrat:"):
        parts = data.split(":")
        if len(parts) >= 2:
            code = parts[1].lower()
            if code in ("r", "runner"):
                config.DEFAULT_EXIT_STRATEGY = "TRAILING_RUNNER"
            elif code in ("2x", "a", "auto"):
                config.DEFAULT_EXIT_STRATEGY = "2X_TAKE_PROFIT"
            else:
                config.DEFAULT_EXIT_STRATEGY = "HOLD_INDEFINITE"

            if config.DEFAULT_EXIT_STRATEGY == "TRAILING_RUNNER":
                current_strat = f"🚀 LONG RUNNER (Trail peak -{config.TRAILING_STOP_PCT:.0f}%)"
            elif config.DEFAULT_EXIT_STRATEGY in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
                current_strat = "🎯 FIXED 2X TAKE-PROFIT (+100.0%)"
            else:
                current_strat = "💎 HOLD INDEFINITELY (Diamond Hands)"

            text = (
                "🛡️ <b>COUNCIL EXIT STRATEGY CONTROLLER</b>\n"
                "───────────────────────────────\n"
                f"🎯 <b>Active Default:</b> <code>{current_strat}</code>\n"
                f"🚀 <b>Long Runner Mode:</b> Rides 5x-100x; trails peak by <code>-{config.TRAILING_STOP_PCT:.0f}%</code>\n"
                f"🎯 <b>Fixed 2x Mode:</b> Auto-exits and banks profit at <code>+100.0%</code>\n"
                f"📉 <b>Stop-Loss Cut:</b> <code>-{config.STOP_LOSS_PCT:.0f}%</code>\n"
                "───────────────────────────────\n"
                "<i>Tap a strategy below to switch the default mode for both autonomous and manual trades:</i>"
            )
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=build_exitmode_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                logger.debug(f"Error updating exitmode markup: {e}")
        return

    # 6.5. Handle Context Preset selection (ctx:<preset>)
    if data.startswith("ctx:"):
        choice = data.split(":", 1)[1]
        user_name = update.effective_user.first_name if update.effective_user else "Admin"
        if choice == "refresh":
            pass
        elif choice == "default":
            context_manager.reset_to_default(updated_by=user_name)
        else:
            context_manager.apply_preset(choice.upper(), updated_by=user_name)

        dashboard = context_manager.format_status_dashboard()
        try:
            await query.edit_message_text(
                text=dashboard,
                reply_markup=context_manager.build_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.debug(f"Error updating context markup: {e}")
        return

    # 7. Handle Council Action dispatch (dve, exe, vot, deb)
    parts = data.split(":")
    if len(parts) >= 2:
        cmd_type = parts[0]
        mint = parts[1]
        raw_amt = float(parts[2]) if len(parts) > 2 else config.DEFAULT_TRADE_AMOUNT_SOL
        amount = config.validate_investment_amount(raw_amt)
        strat_code = parts[3] if len(parts) > 3 else "a"
        strategy = "HOLD_INDEFINITE" if strat_code == "h" else "AUTO_EXIT"

        mode_map = {
            "dve": "dve",
            "exe": "execute",
            "dv": "debate_vote",
            "vot": "debate_vote",
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
            exit_strategy=strategy,
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

    # Register bot commands menu in Telegram UI
    bot_commands = [
        BotCommand("command", "Display simple Crypto Council command guide"),
        BotCommand("deb", "Debate crypto tokens, market depth & liquidity"),
        BotCommand("vot", "Debate & Vote on crypto trades with formal consensus"),
        BotCommand("exe", "Silent background vote & buy on Solana"),
        BotCommand("dve", "Full Pipeline: Debate ➔ Vote ➔ Execute on Solana"),
        BotCommand("auto", "Autonomous trading & frequency (/auto 15m, 1h, 1w)"),
        BotCommand("context", "View or customize AI bot voting instructions"),
        BotCommand("positions", "View open token positions & live performance"),
        BotCommand("sell", "Manually sell token position (/sell <CA>)"),
        BotCommand("vault", "Profit Vault status & customizable threshold"),
        BotCommand("wallet", "View Solana execution wallet & SOL balance"),
        BotCommand("invest", "Change default trade size (/invest 0.25)"),
        BotCommand("status", "View active models, autopilot state & endpoints"),
        BotCommand("cancel", "Stop active council session or autopilot"),
    ]
    try:
        await application.bot.set_my_commands(bot_commands)
        logger.info("✅ Telegram bot command menu registered successfully.")
    except Exception as ce:
        logger.debug(f"Could not register command menu: {ce}")


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
    print(f"🧠 LLM Engine Backend    : {'OpenRouter Unified Engine' if config.OPENROUTER_API_KEY else 'Simulated Testing Mode (Add OPENROUTER_API_KEY for live AI)'}")
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
    application.add_handler(CommandHandler(["command", "commands"], help_command))
    application.add_handler(CommandHandler(["help"], help_command))
    application.add_handler(CommandHandler(["status"], status_command))
    application.add_handler(CommandHandler(["wallet", "bal", "balance"], wallet_command))
    application.add_handler(CommandHandler(["invest", "setinvest"], invest_command))
    application.add_handler(CommandHandler(["context", "ctx", "directive"], context_command))
    application.add_handler(CommandHandler(["positions", "pos"], positions_command))
    application.add_handler(CommandHandler(["exitmode", "strat", "strategy"], exitmode_command))
    application.add_handler(CommandHandler(["tp", "takeprofit"], tp_command))
    application.add_handler(CommandHandler(["vault", "profits"], vault_command))
    application.add_handler(CommandHandler(["sell"], sell_command))
    application.add_handler(CommandHandler(["autopilot", "auto", "interval", "freq"], autopilot_command))
    application.add_handler(CommandHandler(["mood"], mood_command))
    application.add_handler(CommandHandler(["setnames"], setnames_command))
    application.add_handler(CommandHandler(["cancel", "stop"], cancel_command))

    application.add_handler(CommandHandler(["deb", "debate"], deb_command))
    application.add_handler(CommandHandler(["debvote", "dv"], debvote_command))
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

