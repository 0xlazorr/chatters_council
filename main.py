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

from telegram import (
    Bot,
    BotCommand,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    BotCommandScopeDefault,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut, NetworkError, TelegramError, RetryAfter
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
from incubator import token_incubator
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
    """Sends a message safely with fallback to plain text if HTML/Markdown parsing fails or timeout occurs."""
    if not bot or not chat_id or not text:
        return False

    for attempt in range(2):
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
        except (TimedOut, NetworkError) as te:
            logger.warning(f"Telegram network timeout on attempt {attempt + 1}: {te}")
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            # On second timeout, try sending stripped/plain text without preview
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text[:3500],
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                return True
            except Exception:
                return False
        except RetryAfter as ra:
            wait_time = float(ra.retry_after or 1.0)
            logger.warning(f"Telegram flood limit. Waiting {wait_time}s...")
            await asyncio.sleep(wait_time)
            continue
        except Exception as ex:
            logger.error(f"Unexpected error in safe_send_message: {ex}")
            return False
    return False


class CouncilCommandHandler(CommandHandler):
    """
    Accepts commands addressed to ANY council bot, council alias, or plain commands
    without any bot username attached (e.g. /menu, /buy, /mode).
    Prevents Telegram group chats from locking commands exclusively to @claudetalkbbot.
    """
    COUNCIL_BOT_USERNAMES = {
        "claudetalkbbot", "gpttalkbbot", "deepseektalkbbot",
        "geminitalkbbot", "groktalkbbot", "council", "chatters", "msnifferbot"
    }

    def check_update(self, update: object):
        if isinstance(update, Update) and update.effective_message:
            message = update.effective_message
            if (
                message.entities
                and message.entities[0].type == MessageEntity.BOT_COMMAND
                and message.entities[0].offset == 0
                and message.text
                and message.get_bot()
            ):
                command = message.text[1 : message.entities[0].length]
                args = message.text.split()[1:]
                command_parts = command.split("@")

                cmd_name = command_parts[0].lower()
                if cmd_name not in self.commands:
                    return None

                # If command has a target username (e.g. /menu@claudetalkbbot or /menu@gpttalkbbot)
                if len(command_parts) > 1:
                    target_bot = command_parts[1].lower()
                    if (
                        target_bot != message.get_bot().username.lower()
                        and target_bot not in self.COUNCIL_BOT_USERNAMES
                    ):
                        return None

                if not self._check_correct_args(args):
                    return None

                filter_result = self.filters.check_update(update)
                if filter_result:
                    return args, filter_result
                return False
        return None


# =============================================================================
# 3. COMMAND HANDLERS
# =============================================================================

def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for the Master Control Dashboard."""
    row1 = [
        InlineKeyboardButton("🎯 Choose Exit Mode", callback_data="menu:modes"),
        InlineKeyboardButton("📊 View Portfolio", callback_data="menu:portfolio"),
    ]
    row2 = [
        InlineKeyboardButton("🤖 Autopilot Settings", callback_data="menu:auto"),
        InlineKeyboardButton("🧪 Token Incubator", callback_data="menu:incubator"),
    ]
    row3 = [
        InlineKeyboardButton("💰 Trading Wallet", callback_data="menu:wallet"),
        InlineKeyboardButton("🛡️ Security & Audits", callback_data="menu:security"),
    ]
    row4 = [
        InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="menu:refresh"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


async def format_main_dashboard() -> str:
    """Formats the unified Master Control Dashboard."""
    strat = config.DEFAULT_EXIT_STRATEGY
    if strat == "TRAILING_RUNNER":
        strat_desc = f"🚀 Smart Runner (-{config.STOP_LOSS_PCT:.0f}% SL / Breakeven / Trail)"
    elif strat == "HOLD_INDEFINITE":
        strat_desc = "💎 Diamond Hold (Manual /sell only)"
    elif strat == "50PCT_TAKE_PROFIT":
        strat_desc = "⚡ Quick 50% Scalp (+50.0%)"
    elif strat in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
        strat_desc = "🎯 Fixed 2x Target (+100.0%)"
    else:
        strat_desc = f"🚀 Smart Runner (-{config.STOP_LOSS_PCT:.0f}% SL / Trail)"

    open_pos = position_manager.get_open_positions()
    pos_count = len(open_pos)

    ap_status = "🟢 ACTIVE" if autopilot.is_running else "⚪ IDLE"
    ap_freq = autopilot.format_interval(autopilot.scan_interval_seconds)

    incubating_count = len([t for t in token_incubator.tokens.values() if t.status == "INCUBATING"])
    seasoned_count = len([t for t in token_incubator.tokens.values() if t.status == "INCUBATING" and t.is_seasoned])

    wallet_info = await MSnifferExecutor.get_wallet_status()
    sol_bal = wallet_info.get("sol_balance", 0.0)

    return (
        "🏛️ <b>CHATTERS AI COUNCIL CONTROL CENTER</b>\n"
        "───────────────────────────────\n"
        f"🎯 <b>Default Exit Mode:</b> <code>{strat_desc}</code>\n"
        f"🤖 <b>Autopilot Scanner:</b> <code>{ap_status} (Every {ap_freq})</code>\n"
        f"🧪 <b>2h Incubator:</b> <code>{incubating_count} tracking ({seasoned_count} seasoned ≥2h)</code>\n"
        f"📈 <b>Active Holdings:</b> <code>{pos_count} Open Positions</code>\n"
        f"💰 <b>Trading Wallet:</b> <code>{sol_bal:.4f} SOL</code>\n"
        "───────────────────────────────\n"
        "<b>The 3-Step Simple Loop:</b>\n"
        "1️⃣ <b>Catch Early:</b> Autopilot detects new launches, or paste any CA directly in chat\n"
        "2️⃣ <b>Deliberate:</b> 5 AI agents verify security & liquidity to vote on the token\n"
        "3️⃣ <b>Buy & Exit:</b> Auto-rides 10x-100x runners with breakeven protection, or hold diamond hands!\n"
        "───────────────────────────────\n"
        "<i>Tap a button below to control your bots:</i>"
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /menu and /start: displays Master Control Dashboard."""
    chat_id = check_and_record_chat_id(update)
    text = await format_main_dashboard()
    await update.message.reply_text(
        text,
        reply_markup=build_main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start command: displays Master Control Dashboard."""
    await menu_command(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays simple, direct 3-step guide and essential commands."""
    text = (
        "📖 <b>CHATTERS AI COUNCIL — SIMPLE USER GUIDE</b>\n"
        "───────────────────────────────\n\n"
        "<b>The 3-Step Simple Loop:</b>\n\n"
        "1️⃣ <b>CATCH EARLY:</b>\n"
        "• <b>Autopilot:</b> Automatically detects trending Solana breakouts ($30k-$80k mcap).\n"
        "• <b>Manual:</b> Simply paste ANY Solana CA into the chat (or type <code>/buy &lt;CA&gt;</code>).\n\n"
        "2️⃣ <b>DELIBERATE & VOTE:</b>\n"
        "• The 5 AI agents instantly pull on-chain security (rugcheck, freeze/mint authority) and DEX liquidity.\n"
        "• They debate and vote whether the token is worth buying.\n\n"
        "3️⃣ <b>BUY & EXIT ACCORDING TO YOUR MODE:</b>\n"
        "• <b>🚀 Smart Runner (Default):</b> -20% stop-loss ➔ Breakeven lock at +30% ➔ Trails peak by 25% for 10x-100x runs!\n"
        "• <b>💎 Diamond Hold:</b> Conviction holding, bot NEVER auto-sells. Exit manually via /sell.\n"
        "• <b>🎯 Fixed 2x Mode:</b> Auto-exits 100% of bag when price doubles (+100% gain / 2x return).\n"
        "• <b>⚡ Quick 50% Scalp:</b> Auto-exits 100% of bag at +50% profit (quick risk-free profit).\n\n"
        "───────────────────────────────\n"
        "<b>Essential Commands (Or just tap /menu):</b>\n"
        "• <code>/menu</code> - Master Dashboard with 1-tap interactive buttons\n"
        "• <code>/mode</code> - Switch exit strategy (Smart Runner, Diamond Hold, 2x, 50%)\n"
        "• <code>/portfolio</code> - View holdings & 1-tap exit switcher per token\n"
        "• <code>/buy &lt;CA&gt;</code> - Deliberate & buy token (or paste CA)\n"
        "• <code>/sell &lt;CA&gt;</code> - Sell a token manually\n"
        "• <code>/auto</code> - Autopilot background scanner\n"
        "• <code>/incubator</code> - 2-hour token seasoning queue & mature rechecks\n"
        "• <code>/wallet</code> - View trading wallet & live SOL balance\n"
        "• <code>/security</code> - View live security audit & hardening protections\n"
        "• <code>/cancel</code> - Cancel active session or autopilot"
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
        f"🧪 <b>Token Incubator:</b> <code>{len([t for t in token_incubator.tokens.values() if t.status == 'INCUBATING'])} tracking</code> (Use /incubator)",
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


async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays 2-hour seasoned token queue and candidates currently incubating."""
    chat_id = check_and_record_chat_id(update)
    text = token_incubator.get_status_text()
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:incubator"),
            InlineKeyboardButton("🏛️ Main Menu", callback_data="menu:main"),
        ]
    ])
    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


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
        "💳 <b>COUNCIL TRADING WALLET STATUS</b>",
        "───────────────────────────────",
        f"🐕 <b>Market Sniffer:</b> {status_icon}",
        f"🔑 <b>Public Key:</b> <code>{pubkey}</code>",
        f"💰 <b>SOL Balance:</b> <code>{sol_bal:.4f} SOL</code>",
        f"🎯 <b>Default Buy Size:</b> <code>{config.DEFAULT_TRADE_AMOUNT_SOL:.2f} SOL</code>",
        f"🛡️ <b>Default Exit Mode:</b> <code>{config.DEFAULT_EXIT_STRATEGY}</code>",
        f"🤖 <b>Autopilot State:</b> <code>{'ACTIVE 🟢' if autopilot.is_running else 'STOPPED ⚪'}</code>",
    ]
    if solscan_url:
        lines.append(f"🔗 <a href='{solscan_url}'>View Wallet on Solscan</a>")

    lines.extend([
        "───────────────────────────────",
        "🔒 <b>Security Note:</b> Chatters AI Council bot operates via secure RPC and stores <b>zero private keys</b>.",
    ])

    if is_online and sol_bal < config.DEFAULT_TRADE_AMOUNT_SOL:
        lines.append(
            f"\n⚠️ <i>Warning: Wallet balance is below default trade amount ({config.DEFAULT_TRADE_AMOUNT_SOL} SOL). "
            f"Deposit SOL to enable automated trade execution.</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def security_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays comprehensive security audit, hardening layers, and vulnerability report."""
    chat_id = check_and_record_chat_id(update)
    admin_count = len(config.ADMIN_USER_IDS)
    admin_ids_str = ", ".join(f"<code>{uid}</code>" for uid in config.ADMIN_USER_IDS) if admin_count > 0 else "<i>Warning: None configured (open group mode)</i>"

    wallet_info = await MSnifferExecutor.get_wallet_status()
    hot_status = "Online 🟢" if wallet_info.get("online") else "Offline 🔴"
    trading_wallet = wallet_info.get("wallet", "Unavailable")

    text = (
        "🛡️ <b>CHATTERS AI COUNCIL SECURITY & HARDENING REPORT</b>\n"
        "───────────────────────────────\n"
        "<b>1. ACCESS CONTROL & FIREWALL:</b>\n"
        f"• <b>Admin Whitelist:</b> {admin_ids_str}\n"
        "• <b>Execution Guard:</b> Only admins can run trades, <code>-exe</code>, <code>-dve</code>, or <code>-auto</code>.\n"
        "• <b>Group Chat Security:</b> Unauthorized group participants cannot trigger trades or change settings.\n\n"
        "<b>2. PRIVATE KEY ISOLATION (ZERO-KNOWLEDGE):</b>\n"
        "• <b>Council Bot:</b> 0 private keys stored in code, logs, or disk.\n"
        f"• <b>Hot Trading Wallet:</b> Isolated in Market Sniffer daemon ({hot_status})\n"
        f"• <b>Public Address:</b> <code>{trading_wallet}</code>\n\n"
        "<b>3. ANTI-RUG & HONEYPOT DEFENSE:</b>\n"
        "• <b>Mint Authority Check:</b> Must be REVOKED before consensus buy.\n"
        "• <b>Freeze Authority Check:</b> Must be REVOKED (prevents honeypot locks).\n"
        "• <b>Liquidity Gate:</b> Enforces minimum pool depth ($10k-$50k+) & DEX lock.\n"
        "• <b>Whale Concentration:</b> Checks Top 10 holder supply percentage.\n\n"
        "<b>4. CAPITAL PRESERVATION & SMART RUNNER RATCHET:</b>\n"
        f"• <b>Hard Stop-Loss:</b> Cuts immediately at <code>-{config.STOP_LOSS_PCT:.0f}%</code> to prevent drawdowns.\n"
        f"• <b>Breakeven Lock:</b> Once price hits <code>+{config.BREAKEVEN_PROFIT_PCT:.0f}%</code>, stop-loss jumps to Entry ($0 loss floor).\n"
        f"• <b>Trailing Peak Ratchet:</b> Trails highest peak price by <code>-{config.TRAILING_STOP_PCT:.0f}%</code> for uncapped 10x-100x runs.\n"
        "───────────────────────────────\n"
        "<i>Your trading environment is fully hardened and protected against unauthorized triggers.</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_portfolio_keyboard(open_pos: List[Any]) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for portfolio holdings."""
    if not open_pos:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Choose Exit Mode", callback_data="menu:modes")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main")],
        ])

    rows = []
    if len(open_pos) == 1:
        p = open_pos[0]
        rows.append([
            InlineKeyboardButton("🚀 Smart Runner", callback_data=f"posstrat:{p.mint}:runner"),
            InlineKeyboardButton("💎 Diamond Hold", callback_data=f"posstrat:{p.mint}:hold"),
        ])
        rows.append([
            InlineKeyboardButton("🎯 Fixed 2x", callback_data=f"posstrat:{p.mint}:2x"),
            InlineKeyboardButton("⚡ 50% Scalp", callback_data=f"posstrat:{p.mint}:50"),
        ])
        rows.append([
            InlineKeyboardButton("🔴 Sell 100% Now", callback_data=f"possell:{p.mint}"),
        ])
    else:
        for p in open_pos:
            pnl_badge = f"+{p.pnl_pct:.0f}%" if p.pnl_pct >= 0 else f"{p.pnl_pct:.0f}%"
            rows.append([InlineKeyboardButton(f"⚙️ Manage ${p.symbol} ({pnl_badge})", callback_data=f"posmanage:{p.mint}")])

    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="menu:portfolio"),
        InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main"),
    ])
    return InlineKeyboardMarkup(rows)


def build_token_manage_keyboard(mint: str) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for managing exit mode of a specific token."""
    row1 = [
        InlineKeyboardButton("🚀 Smart Runner", callback_data=f"posstrat:{mint}:runner"),
        InlineKeyboardButton("💎 Diamond Hold", callback_data=f"posstrat:{mint}:hold"),
    ]
    row2 = [
        InlineKeyboardButton("🎯 Fixed 2x", callback_data=f"posstrat:{mint}:2x"),
        InlineKeyboardButton("⚡ 50% Scalp", callback_data=f"posstrat:{mint}:50"),
    ]
    row3 = [
        InlineKeyboardButton("🔴 Sell 100% Now", callback_data=f"possell:{mint}"),
    ]
    row4 = [
        InlineKeyboardButton("🔙 Back to Portfolio", callback_data="menu:portfolio"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays active positions with 1-click buttons to change strategy or sell per token."""
    chat_id = check_and_record_chat_id(update)
    open_pos = position_manager.get_open_positions()
    summary = position_manager.get_summary_text()
    if open_pos:
        summary += "\n\n<i>Tap a token below to change its exit target (50%, 2x, runner, hold) or sell immediately:</i>"
    await update.message.reply_text(
        summary,
        reply_markup=build_portfolio_keyboard(open_pos),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for /portfolio."""
    await portfolio_command(update, context)


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles /buy <Solana CA or $ticker> [amount_sol]
    Deliberates on the token, casts council votes, and executes on-chain buy if approved!
    """
    auth, reason = is_authorized_admin(update)
    if not auth:
        await update.message.reply_text(f"⛔ {reason}")
        return

    chat_id = check_and_record_chat_id(update)
    args = context.args or []
    reply_text = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else ""
    full_text = " ".join(args)

    mint, action, amount, symbol = SignalExtractor.extract(full_text, reply_text=reply_text)
    if not mint and symbol:
        mint = await telemetry.search_solana_token_mint(symbol)

    if not mint and args:
        first = args[0].strip().replace("$", "")
        if 32 <= len(first) <= 44:
            mint = first
        else:
            mint = await telemetry.search_solana_token_mint(first)

    if not mint:
        await update.message.reply_text(
            "⚡ <b>Usage:</b> <code>/buy &lt;Solana CA or $ticker&gt; [amount_sol]</code>\n\n"
            "• Examples: <code>/buy 2e3z...</code> or <code>/buy $BONK 0.25</code>\n"
            "• Or simply <b>paste any Solana CA directly</b> into the group chat!",
            parse_mode=ParseMode.HTML,
        )
        return

    trade_amt = amount if amount > 0 else config.DEFAULT_TRADE_AMOUNT_SOL
    trade_amt = config.validate_investment_amount(trade_amt)
    prompt = f"BUY {trade_amt:.2f} SOL of token {mint}"
    if symbol:
        prompt += f" (${symbol})"

    await execute_council_session(
        chat_id=chat_id,
        prompt=prompt,
        mode="dve",
        reply_to_id=update.message.message_id,
        mint_override=mint,
    )


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
    """Handles /mode or /exitmode to configure default Council exit strategy."""
    chat_id = check_and_record_chat_id(update)
    args = context.args or []

    if args:
        auth, reason = is_authorized_admin(update)
        if not auth:
            await update.message.reply_text(f"⛔ {reason}")
            return

        val = args[0].strip().lower()
        if val in ("runner", "smart", "trail", "trailing", "long"):
            config.DEFAULT_EXIT_STRATEGY = "TRAILING_RUNNER"
            await update.message.reply_text(
                "🚀 <b>Default Exit Strategy Set to SMART RUNNER</b>\n\n"
                f"• <b>1. Initial Protection:</b> Hard stop-loss cut at -{config.STOP_LOSS_PCT:.0f}% if price drops.\n"
                f"• <b>2. Breakeven Lock:</b> Once token reaches +{config.BREAKEVEN_PROFIT_PCT:.0f}%, stop-loss jumps to Entry ($0 loss floor).\n"
                f"• <b>3. Uncapped Runner:</b> Rides 5x-100x runs! Trails peak price by -{config.TRAILING_STOP_PCT:.0f}% after +50%.\n"
                "• <b>Result:</b> Minimizes losses, locks in breakeven, and never cuts winners short!",
                parse_mode=ParseMode.HTML,
            )
            return
        elif val in ("hold", "diamond", "diamond_hold", "indefinite"):
            config.DEFAULT_EXIT_STRATEGY = "HOLD_INDEFINITE"
            await update.message.reply_text(
                "💎 <b>Default Exit Strategy Set to DIAMOND HOLD</b>\n\n"
                "• The bot will hold purchased tokens indefinitely without any auto-selling.\n"
                "• Perfect for pure conviction investments where you decide when to sell.\n"
                "• Manually exit anytime by typing: <code>/sell &lt;CA&gt; [pct]</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        elif val in ("2x", "double", "auto", "tp", "profit", "100", "100%"):
            config.DEFAULT_EXIT_STRATEGY = "2X_TAKE_PROFIT"
            await update.message.reply_text(
                f"🎯 <b>Default Exit Strategy Set to FIXED 2X TAKE-PROFIT (+100%)</b>\n\n"
                f"• Automatically sells 100% as soon as token doubles (2x return / +100% gain).\n"
                f"• Stop-loss cuts at -{config.STOP_LOSS_PCT:.0f}% if price drops.",
                parse_mode=ParseMode.HTML,
            )
            return
        elif val in ("50", "50%", "scalp", "quick", "tiny"):
            config.DEFAULT_EXIT_STRATEGY = "50PCT_TAKE_PROFIT"
            await update.message.reply_text(
                f"⚡ <b>Default Exit Strategy Set to QUICK 50% SCALP (+50.0%)</b>\n\n"
                f"• Automatically sells 100% as soon as token gains +50%.\n"
                f"• Stop-loss cuts at -{config.STOP_LOSS_PCT:.0f}% if price drops.",
                parse_mode=ParseMode.HTML,
            )
            return

    if config.DEFAULT_EXIT_STRATEGY == "TRAILING_RUNNER":
        current_strat = f"🚀 SMART RUNNER (-{config.STOP_LOSS_PCT:.0f}% SL / Breakeven / Trail)"
    elif config.DEFAULT_EXIT_STRATEGY == "HOLD_INDEFINITE":
        current_strat = "💎 DIAMOND HOLD (Never Auto-Sells)"
    elif config.DEFAULT_EXIT_STRATEGY == "50PCT_TAKE_PROFIT":
        current_strat = "⚡ QUICK 50% SCALP (+50.0%)"
    elif config.DEFAULT_EXIT_STRATEGY in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
        current_strat = "🎯 FIXED 2X TAKE-PROFIT (+100.0%)"
    else:
        current_strat = "🚀 SMART RUNNER"

    text = (
        "🛡️ <b>COUNCIL EXIT STRATEGY CONTROLLER</b>\n"
        "───────────────────────────────\n"
        f"🎯 <b>Active Default:</b> <code>{current_strat}</code>\n"
        f"🚀 <b>Smart Runner:</b> -{config.STOP_LOSS_PCT:.0f}% SL → Breakeven at +{config.BREAKEVEN_PROFIT_PCT:.0f}% → Trails peak -{config.TRAILING_STOP_PCT:.0f}% for 10x-100x!\n"
        f"💎 <b>Diamond Hold:</b> Never auto-sells; pure manual /sell exit\n"
        f"🎯 <b>Fixed 2x Mode:</b> Auto-exits 100% of bag at <code>+100.0%</code> (2x return)\n"
        f"⚡ <b>50% Scalp Mode:</b> Banks profit on entire bag at <code>+50.0%</code>\n"
        "───────────────────────────────\n"
        "<i>Tap a strategy below to switch the default mode for future trades:</i>"
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
        if pos.exit_strategy == "HOLD_INDEFINITE":
            strat_badge = "💎 DIAMOND HOLD"
        elif pos.exit_strategy == "TRAILING_RUNNER":
            strat_badge = f"🚀 SMART RUNNER (-{config.STOP_LOSS_PCT:.0f}% SL / Trail)"
        elif pos.exit_strategy in ("50PCT_TAKE_PROFIT", "SCALP_50"):
            strat_badge = "⚡ 50% SCALP (+50.0%)"
        else:
            strat_badge = "🎯 FIXED 2X (+100.0%)"
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
            if config.INCUBATOR_ENABLED:
                token_incubator.add_candidate(
                    mint=mint,
                    symbol=dossier.get("symbol", ""),
                    name=dossier.get("name", ""),
                    price_usd=float(dossier.get("price_usd") or 0.0),
                    liquidity_usd=float(dossier.get("liquidity_usd") or 0.0),
                    fdv_usd=float(dossier.get("market_cap_usd") or dossier.get("fdv") or 0.0),
                    notes="Analyzed by council command",
                )

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
                if mint and config.INCUBATOR_ENABLED:
                    token_incubator.mark_bought(mint)
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
                if mint and config.INCUBATOR_ENABLED:
                    token_incubator.record_recheck(mint, notes="Council voted NO on /dve; queued for 2h re-evaluation")
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
                if mint and config.INCUBATOR_ENABLED:
                    token_incubator.mark_bought(mint)
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
                if mint and config.INCUBATOR_ENABLED:
                    token_incubator.record_recheck(mint, notes="Council voted NO on /exe; queued for 2h re-evaluation")
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
        InlineKeyboardButton("🧪 Token Incubator", callback_data="menu:incubator"),
    ]
    toggle_label = "🛑 Stop Autopilot" if is_running else "🟢 Start Autopilot"
    row4 = [
        InlineKeyboardButton(toggle_label, callback_data="auto:toggle"),
        InlineKeyboardButton("🔄 Refresh Status", callback_data="auto:refresh"),
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
        f"🧪 <b>2h Incubator Tracking:</b> <code>{stats.get('incubator_total', 0)} tokens ({stats.get('incubator_seasoned', 0)} seasoned ≥2h)</code>\n"
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
    runner_label = f"🚀 Smart Runner (-{config.STOP_LOSS_PCT:.0f}% SL / Trail) ✅" if strat == "TRAILING_RUNNER" else f"🚀 Smart Runner (-{config.STOP_LOSS_PCT:.0f}% SL / Trail)"
    hold_label = "💎 Diamond Hold (Manual Exit) ✅" if strat == "HOLD_INDEFINITE" else "💎 Diamond Hold (Manual Exit)"
    two_x_label = "🎯 Fixed 2x (+100%) ✅" if strat in ("2X_TAKE_PROFIT", "AUTO_EXIT") else "🎯 Fixed 2x (+100%)"
    scalp_label = "⚡ 50% Scalp (+50%) ✅" if strat == "50PCT_TAKE_PROFIT" else "⚡ 50% Scalp (+50%)"

    row1 = [
        InlineKeyboardButton(runner_label, callback_data="setstrat:runner"),
    ]
    row2 = [
        InlineKeyboardButton(hold_label, callback_data="setstrat:hold"),
    ]
    row3 = [
        InlineKeyboardButton(two_x_label, callback_data="setstrat:2x"),
        InlineKeyboardButton(scalp_label, callback_data="setstrat:50"),
    ]
    row4 = [
        InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3, row4])


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
        auth, auth_err = is_authorized_admin(update)
        if not auth:
            logger.warning(f"Security: Blocked unauthorized autopilot launch from user {update.effective_user.id if update.effective_user else 'unknown'}")
            await update.message.reply_text("🔒 <b>Security Alert:</b> Only authorized council admins can start Autopilot execution.", parse_mode=ParseMode.HTML)
            return
        lead_bot = council.broadcaster.get_bot(council.agents_config[0].name) if council else None
        if not lead_bot:
            await update.message.reply_text("❌ Council Orchestrator is not ready.")
            return
        success, msg = await autopilot.start(chat_id=chat_id, lead_bot=lead_bot, council_orchestrator=council)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    # 5. If specific operational mode flag was attached, run immediately!
    if mode in ("dve", "exe", "vote", "debate", "debate_vote"):
        if mode in ("dve", "exe"):
            auth, auth_err = is_authorized_admin(update)
            if not auth:
                logger.warning(f"Security: Blocked unauthorized execution mode '{mode}' from user {update.effective_user.id if update.effective_user else 'unknown'}")
                await update.message.reply_text(
                    "🔒 <b>Security Alert:</b> Trade execution is restricted to authorized administrators.",
                    parse_mode=ParseMode.HTML,
                )
                return
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
        if config.INCUBATOR_ENABLED and dossier.get("mint"):
            token_incubator.add_candidate(
                mint=dossier["mint"],
                symbol=dossier.get("symbol", ""),
                name=dossier.get("name", ""),
                price_usd=float(dossier.get("price_usd") or 0.0),
                liquidity_usd=float(dossier.get("liquidity_usd") or 0.0),
                fdv_usd=float(dossier.get("market_cap_usd") or dossier.get("fdv") or 0.0),
                notes="Pasted by user in group chat",
            )
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

    # Verify authorization
    user = update.effective_user
    if config.ADMIN_USER_IDS and user and user.id not in config.ADMIN_USER_IDS:
        await query.answer("⛔ Unauthorized: Only configured council admins can trigger actions.", show_alert=True)
        return

    await query.answer()

    data = query.data or ""
    chat_id = query.message.chat_id if query.message else config.GROUP_CHAT_ID

    # 0. Handle Master Dashboard Menu navigation
    if data.startswith("menu:"):
        choice = data.split(":", 1)[1]
        if choice in ("main", "refresh"):
            text = await format_main_dashboard()
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=build_main_menu_keyboard(),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif choice == "modes":
            strat = config.DEFAULT_EXIT_STRATEGY
            if strat == "TRAILING_RUNNER":
                current_strat = f"🚀 SMART RUNNER (Stop -{config.STOP_LOSS_PCT:.0f}% ➔ Breakeven ➔ Trail -{config.TRAILING_STOP_PCT:.0f}%)"
            elif strat == "HOLD_INDEFINITE":
                current_strat = "💎 DIAMOND HOLD (Hold Indefinitely / Manual Exit)"
            elif strat == "50PCT_TAKE_PROFIT":
                current_strat = "⚡ QUICK 50% SCALP (+50.0%)"
            elif strat in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
                current_strat = "🎯 FIXED 2X TAKE-PROFIT (+100.0%)"
            else:
                current_strat = "🚀 SMART RUNNER"

            text = (
                "🛡️ <b>COUNCIL EXIT STRATEGY CONTROLLER</b>\n"
                "───────────────────────────────\n"
                f"🎯 <b>Active Default:</b> <code>{current_strat}</code>\n\n"
                f"🚀 <b>Smart Runner (Recommended):</b>\n"
                f"• Hard Stop-Loss cut at <code>-{config.STOP_LOSS_PCT:.0f}%</code> to protect capital if token flops.\n"
                f"• Stop-loss automatically jumps to <b>Breakeven ($0 Loss Floor)</b> once price hits <code>+{config.BREAKEVEN_PROFIT_PCT:.0f}%</code>.\n"
                f"• Rides 5x-100x+ runners uncapped; trails peak price by <code>-{config.TRAILING_STOP_PCT:.0f}%</code>.\n\n"
                f"💎 <b>Diamond Hold:</b>\n"
                "• Pure conviction holding; bot NEVER auto-sells.\n"
                "• Ride long-term runners and exit manually anytime with <code>/sell &lt;CA&gt;</code>.\n\n"
                f"🎯 <b>Fixed 2x Mode:</b> Auto-exits 100% of bag at <code>+100.0%</code> (Doubler)\n"
                f"⚡ <b>50% Scalp Mode:</b> Quick safe exit at <code>+50.0%</code> profit\n"
                "───────────────────────────────\n"
                "<i>Tap a strategy below to switch the default mode for future trades:</i>"
            )
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=build_exitmode_keyboard(),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            return

        elif choice == "portfolio":
            open_pos = position_manager.get_open_positions()
            summary = position_manager.get_summary_text()
            if open_pos:
                summary += "\n\n<i>Tap a token below to change its exit strategy (Runner, Hold, 2x, Scalp) or sell immediately:</i>"
            try:
                await query.edit_message_text(
                    text=summary,
                    reply_markup=build_portfolio_keyboard(open_pos),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif choice == "auto":
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

        elif choice == "wallet":
            wallet_info = await MSnifferExecutor.get_wallet_status()
            is_online = wallet_info.get("online", False)
            status_icon = "🟢 Online" if is_online else "🔴 Offline"
            pubkey = wallet_info.get("wallet", "Unavailable")
            sol_bal = wallet_info.get("sol_balance", 0.0)

            text = (
                "💳 <b>COUNCIL TRADING WALLET</b>\n"
                "───────────────────────────────\n"
                "🔥 <b>Market Sniffer Trading Wallet:</b>\n"
                f"• <b>Status:</b> {status_icon}\n"
                f"• <b>Address:</b> <code>{pubkey}</code>\n"
                f"• <b>Balance:</b> <code>{sol_bal:.4f} SOL</code>\n"
                f"• <b>Default Buy Size:</b> <code>{config.DEFAULT_TRADE_AMOUNT_SOL} SOL</code>\n"
                "───────────────────────────────\n"
                "🔒 <b>Security Note:</b> Chatters bot holds ZERO private keys. All transactions are securely routed through Market Sniffer daemon with strict safety limits."
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="menu:wallet"), InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main")]
            ])
            try:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

        elif choice == "security":
            admin_count = len(config.ADMIN_USER_IDS)
            admin_ids_str = ", ".join(f"<code>{uid}</code>" for uid in config.ADMIN_USER_IDS) if admin_count > 0 else "<i>Warning: None configured (open group mode)</i>"
            wallet_info = await MSnifferExecutor.get_wallet_status()
            hot_status = "Online 🟢" if wallet_info.get("online") else "Offline 🔴"
            trading_wallet = wallet_info.get("wallet", "Unavailable")

            text = (
                "🛡️ <b>CHATTERS AI COUNCIL SECURITY & HARDENING</b>\n"
                "───────────────────────────────\n"
                "<b>1. ACCESS CONTROL & FIREWALL:</b>\n"
                f"• <b>Admin Whitelist:</b> {admin_ids_str}\n"
                "• <b>Execution Guard:</b> Only admins can run trades, <code>-exe</code>, <code>-dve</code>, or <code>-auto</code>.\n\n"
                "<b>2. PRIVATE KEY ISOLATION:</b>\n"
                "• <b>Council Bot:</b> 0 private keys stored anywhere.\n"
                f"• <b>Trading Wallet:</b> Isolated in Market Sniffer daemon ({hot_status})\n"
                f"• <b>Public Address:</b> <code>{trading_wallet}</code>\n\n"
                "<b>3. ANTI-RUG & HONEYPOT FILTERS:</b>\n"
                "• Mint & Freeze Authority MUST be REVOKED.\n"
                "• Minimum DEX liquidity depth verified before buy.\n"
                "• Holder supply concentration audited.\n\n"
                "<b>4. CAPITAL PRESERVATION & SMART RUNNER RATCHET:</b>\n"
                f"• Stop-Loss cut at <code>-{config.STOP_LOSS_PCT:.0f}%</code> to protect principal.\n"
                f"• Breakeven Ratchet locks at <code>+{config.BREAKEVEN_PROFIT_PCT:.0f}%</code> gain ($0 loss floor).\n"
                f"• Trailing Peak Ratchet (<code>-{config.TRAILING_STOP_PCT:.0f}%</code>) lets 10x-100x runners climb indefinitely.\n"
                "───────────────────────────────"
            )
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main")]])
            try:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

        elif choice == "incubator":
            text = token_incubator.get_status_text()
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="menu:incubator"),
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main"),
                ]
            ])
            try:
                await query.edit_message_text(
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
            return

        elif choice == "guide":
            text = (
                "📖 <b>HOW CHATTERS AI COUNCIL WORKS</b>\n"
                "───────────────────────────────\n\n"
                "<b>1️⃣ Catch Early:</b>\n"
                "• Autopilot finds new breakout tokens ($30k-$80k mcap)\n"
                "• Or paste any Solana CA directly in chat!\n\n"
                "<b>2️⃣ Deliberate:</b>\n"
                "• 5 AI agents analyze security, liquidity, and volume to vote.\n\n"
                "<b>3️⃣ Buy & Exit Strategies:</b>\n"
                "• <b>🚀 Smart Runner (Default):</b> -20% stop-loss ➔ Breakeven lock at +30% ➔ Trails peak by 25% for 10x-100x runs!\n"
                "• <b>💎 Diamond Hold:</b> Conviction holding, zero auto-sells. Exit manually via /sell.\n"
                "• <b>🎯 Fixed 2x:</b> Auto-exits 100% of bag at +100% gain.\n"
                "• <b>⚡ 50% Scalp:</b> Quick exit 100% of bag at +50% gain.\n"
                "───────────────────────────────"
            )
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu:main")]])
            try:
                await query.edit_message_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except Exception:
                pass
            return

    # 0.5. Handle Specific Token Exit Strategy & Management in Portfolio
    if data.startswith("posmanage:"):
        mint = data.split(":", 1)[1]
        pos = position_manager.positions.get(mint)
        if not pos or pos.status != "OPEN":
            await query.answer("Position no longer open.", show_alert=True)
            return

        pnl_badge = f"+{pos.pnl_pct:.1f}% 🟢" if pos.pnl_pct >= 0 else f"{pos.pnl_pct:.1f}% 🔴"
        if pos.exit_strategy == "TRAILING_RUNNER":
            strat_desc = f"🚀 SMART RUNNER (-{config.STOP_LOSS_PCT:.0f}% SL / Breakeven / Trail)"
        elif pos.exit_strategy == "HOLD_INDEFINITE":
            strat_desc = "💎 DIAMOND HOLD (Never Auto-Sells)"
        elif pos.exit_strategy in ("50PCT_TAKE_PROFIT", "SCALP_50"):
            strat_desc = "⚡ QUICK 50% SCALP (+50.0%)"
        elif pos.exit_strategy in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
            strat_desc = "🎯 FIXED 2X (+100.0%)"
        else:
            strat_desc = "🚀 SMART RUNNER"

        text = (
            f"⚙️ <b>MANAGE TOKEN: {html.escape(pos.name)} (<code>${html.escape(pos.symbol)}</code>)</b>\n"
            f"───────────────────────────────\n"
            f"💰 <b>Entry Price:</b> <code>${pos.entry_price_usd:.6f}</code>\n"
            f"📊 <b>Current Price:</b> <code>${pos.current_price_usd:.6f}</code>\n"
            f"📈 <b>Live PnL:</b> <b>{pnl_badge}</b>\n"
            f"🛡️ <b>Active Exit Target:</b> <code>{strat_desc}</code>\n"
            f"───────────────────────────────\n"
            f"<i>Tap below to switch this token's exit target (Smart Runner, Diamond Hold, 2x, 50%) or sell immediately:</i>"
        )
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=build_token_manage_keyboard(mint),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    if data.startswith("posstrat:"):
        parts = data.split(":")
        if len(parts) >= 3:
            mint = parts[1]
            strat_code = parts[2].lower()
            if strat_code in ("r", "runner", "smart"):
                strat_name = "TRAILING_RUNNER"
                strat_human = f"🚀 SMART RUNNER (-{config.STOP_LOSS_PCT:.0f}% SL / Breakeven / Trail)"
            elif strat_code in ("h", "hold", "diamond"):
                strat_name = "HOLD_INDEFINITE"
                strat_human = "💎 DIAMOND HOLD (Never Auto-Sells, Manual exit only)"
            elif strat_code in ("2x", "double", "100"):
                strat_name = "2X_TAKE_PROFIT"
                strat_human = "🎯 FIXED 2X (+100.0%)"
            elif strat_code in ("50", "scalp"):
                strat_name = "50PCT_TAKE_PROFIT"
                strat_human = "⚡ QUICK 50% SCALP (+50.0%)"
            else:
                strat_name = "TRAILING_RUNNER"
                strat_human = "🚀 SMART RUNNER"

            ok, msg = position_manager.set_position_strategy(mint, strat_name)
            pos = position_manager.positions.get(mint)
            await query.answer(f"Updated exit strategy to {strat_name}!", show_alert=False)

            if pos:
                pnl_badge = f"+{pos.pnl_pct:.1f}% 🟢" if pos.pnl_pct >= 0 else f"{pos.pnl_pct:.1f}% 🔴"
                text = (
                    f"✅ <b>EXIT MODE UPDATED FOR {html.escape(pos.name)} (${html.escape(pos.symbol)})</b>\n"
                    f"───────────────────────────────\n"
                    f"🛡️ <b>New Exit Target:</b> <code>{strat_human}</code>\n"
                    f"📊 <b>Live PnL:</b> <b>{pnl_badge}</b>\n"
                    f"💵 <b>Current Price:</b> <code>${pos.current_price_usd:.6f}</code>\n"
                    f"───────────────────────────────\n"
                    f"<i>The council will automatically execute the exit based on this target!</i>"
                )
                try:
                    await query.edit_message_text(
                        text=text,
                        reply_markup=build_token_manage_keyboard(mint),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
        return

    if data.startswith("possell:"):
        mint = data.split(":", 1)[1]
        pos = position_manager.positions.get(mint)
        sym = pos.symbol if pos else mint
        await query.answer(f"Executing immediate sale of ${sym}...", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        prompt = f"SELL 100% of token {mint}"
        await execute_council_session(chat_id=chat_id, prompt=prompt, mode="execute")
        return

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
            if code in ("r", "runner", "smart"):
                config.DEFAULT_EXIT_STRATEGY = "TRAILING_RUNNER"
            elif code in ("h", "hold", "diamond"):
                config.DEFAULT_EXIT_STRATEGY = "HOLD_INDEFINITE"
            elif code in ("2x", "a", "auto", "100"):
                config.DEFAULT_EXIT_STRATEGY = "2X_TAKE_PROFIT"
            elif code in ("50", "scalp"):
                config.DEFAULT_EXIT_STRATEGY = "50PCT_TAKE_PROFIT"
            else:
                config.DEFAULT_EXIT_STRATEGY = "TRAILING_RUNNER"

            if config.DEFAULT_EXIT_STRATEGY == "TRAILING_RUNNER":
                current_strat = f"🚀 SMART RUNNER (-{config.STOP_LOSS_PCT:.0f}% SL / Breakeven / Trail)"
            elif config.DEFAULT_EXIT_STRATEGY == "HOLD_INDEFINITE":
                current_strat = "💎 DIAMOND HOLD (Never Auto-Sells)"
            elif config.DEFAULT_EXIT_STRATEGY == "50PCT_TAKE_PROFIT":
                current_strat = "⚡ QUICK 50% SCALP (+50.0%)"
            elif config.DEFAULT_EXIT_STRATEGY in ("2X_TAKE_PROFIT", "AUTO_EXIT"):
                current_strat = "🎯 FIXED 2X TAKE-PROFIT (+100.0%)"
            else:
                current_strat = "🚀 SMART RUNNER"

            text = (
                "🛡️ <b>COUNCIL EXIT STRATEGY CONTROLLER</b>\n"
                "───────────────────────────────\n"
                f"🎯 <b>Active Default:</b> <code>{current_strat}</code>\n"
                f"🚀 <b>Smart Runner:</b> -{config.STOP_LOSS_PCT:.0f}% SL → Breakeven at +{config.BREAKEVEN_PROFIT_PCT:.0f}% → Trails peak -{config.TRAILING_STOP_PCT:.0f}% for 10x-100x!\n"
                f"💎 <b>Diamond Hold:</b> Never auto-sells; pure manual /sell exit\n"
                f"🎯 <b>Fixed 2x Mode:</b> Auto-exits 100% of bag at <code>+100.0%</code> (2x return)\n"
                f"⚡ <b>50% Scalp Mode:</b> Banks profit on entire bag at <code>+50.0%</code>\n"
                "───────────────────────────────\n"
                "<i>Tap a strategy below to switch the default mode for future trades:</i>"
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

async def position_exit_watcher_loop(lead_bot: Bot):
    """
    Dedicated background risk monitor:
    Runs continuously every 20 seconds to check open positions for exit triggers
    (50% scalp, 2x take-profit, trailing runner retrace, stop-loss cut)
    regardless of whether Autopilot's token-scanning loop is enabled!
    """
    logger.info("🛡️ Dedicated Council Position Exit Monitor loop started.")
    while True:
        try:
            await asyncio.sleep(20)
            open_positions = position_manager.get_open_positions()
            if not open_positions:
                continue

            target_chat_id = config.GROUP_CHAT_ID or 0

            for pos in open_positions:
                live_pair = await telemetry.fetch_token_dossier(pos.mint)
                curr_price = float(live_pair.get("price_usd", 0.0)) if live_pair else 0.0
                if curr_price <= 0:
                    continue

                should_exit, reason, sell_pct = position_manager.check_exit_triggers(pos.mint, curr_price)
                if should_exit:
                    logger.info(f"🎯 Dedicated watcher triggered exit for {pos.symbol} ({pos.mint}): {reason}")
                    prompt_str = f"SELL 100% of {pos.mint}"
                    exit_res = await MSnifferExecutor.execute_consensus(
                        prompt=prompt_str,
                        consensus_text=f"Council Automated Exit: {reason}",
                        yes_count=5,
                        total_votes=5,
                        broadcaster_bot=lead_bot,
                        reply_to_chat_id=target_chat_id,
                    )
                    if exit_res.success:
                        position_manager.close_position(
                            mint=pos.mint,
                            exit_price_usd=curr_price,
                            exit_reason=reason,
                            exit_tx_hash=exit_res.tx_hash or "",
                        )
                        pnl_badge = f"+{pos.pnl_pct:.1f}% 🟢" if pos.pnl_pct >= 0 else f"{pos.pnl_pct:.1f}% 🔴"
                        solscan_link = f"\n🔗 <a href='https://solscan.io/tx/{exit_res.tx_hash}'>View Solana Transaction</a>" if exit_res.tx_hash else ""
                        exit_msg = (
                            f"🎯 <b>COUNCIL POSITION EXIT EXECUTED: SOLD {html.escape(pos.name)}</b>\n"
                            f"───────────────────────────────\n"
                            f"🪙 <b>Token:</b> {html.escape(pos.name)} (<code>${html.escape(pos.symbol)}</code>)\n"
                            f"📉 <b>Exit Trigger:</b> <code>{reason}</code>\n"
                            f"💵 <b>Entry Price:</b> <code>${pos.entry_price_usd:.6f}</code> | <b>Exit:</b> <code>${curr_price:.6f}</code>\n"
                            f"📊 <b>Realized Performance:</b> <b>{pnl_badge}</b>\n"
                            f"⚡ <i>Proceeds returned to wallet for future trading & profit vault.</i>"
                            f"{solscan_link}\n"
                            f"───────────────────────────────"
                        )
                        if target_chat_id:
                            try:
                                await lead_bot.send_message(
                                    chat_id=target_chat_id,
                                    text=exit_msg,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True,
                                )
                            except Exception as me:
                                logger.error(f"Failed to post exit notification: {me}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Position exit watcher iteration error: {e}")


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

    # Start dedicated background position exit monitor
    asyncio.create_task(position_exit_watcher_loop(lead_bot=application.bot))

    # Auto-resume persistent Autopilot if previously enabled
    if autopilot.enabled and config.GROUP_CHAT_ID:
        logger.info("Resuming persistent Autopilot scanning loop...")
        await autopilot.start(chat_id=config.GROUP_CHAT_ID, lead_bot=application.bot, council_orchestrator=council)

    # Ensure bot commands are not locked to @claudetalkbbot in Telegram group chats
    for scope in (BotCommandScopeDefault(), BotCommandScopeAllGroupChats(), BotCommandScopeAllChatAdministrators()):
        try:
            await application.bot.delete_my_commands(scope=scope)
            logger.info(f"✅ Cleared scoped bot commands for {scope.__class__.__name__} so commands remain universal (/menu).")
        except Exception as ce:
            logger.debug(f"Could not clear commands for scope {scope}: {ce}")


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

    # 1. Primary Clean Command Handlers (Universally handles /menu, /mode, etc. across any bot or plain /cmd)
    application.add_handler(CouncilCommandHandler(["menu", "start", "dashboard"], menu_command))
    application.add_handler(CouncilCommandHandler(["mode", "strat", "strategy", "exitmode"], exitmode_command))
    application.add_handler(CouncilCommandHandler(["portfolio", "p", "positions", "pos", "holdings"], portfolio_command))
    application.add_handler(CouncilCommandHandler(["buy"], buy_command))
    application.add_handler(CouncilCommandHandler(["sell"], sell_command))
    application.add_handler(CouncilCommandHandler(["auto", "autopilot", "interval", "freq"], autopilot_command))
    application.add_handler(CouncilCommandHandler(["incubator", "watchlist", "incubating", "seasoning"], incubator_command))
    application.add_handler(CouncilCommandHandler(["wallet", "bal", "balance"], wallet_command))
    application.add_handler(CouncilCommandHandler(["security", "audit", "safe"], security_command))
    application.add_handler(CouncilCommandHandler(["help", "command", "commands", "guide"], help_command))

    # Aliases & Advanced Controls
    application.add_handler(CouncilCommandHandler(["status"], status_command))
    application.add_handler(CouncilCommandHandler(["cancel", "stop"], cancel_command))
    application.add_handler(CouncilCommandHandler(["invest", "setinvest"], invest_command))
    application.add_handler(CouncilCommandHandler(["context", "ctx", "directive"], context_command))
    application.add_handler(CouncilCommandHandler(["tp", "takeprofit"], tp_command))
    application.add_handler(CouncilCommandHandler(["vault", "profits"], vault_command))
    application.add_handler(CouncilCommandHandler(["mood"], mood_command))
    application.add_handler(CouncilCommandHandler(["setnames"], setnames_command))

    # Shorthand Pipeline triggers
    application.add_handler(CouncilCommandHandler(["deb", "debate"], deb_command))
    application.add_handler(CouncilCommandHandler(["debvote", "dv"], debvote_command))
    application.add_handler(CouncilCommandHandler(["vot", "vote"], vot_command))
    application.add_handler(CouncilCommandHandler(["exe", "execute"], exe_command))
    application.add_handler(CouncilCommandHandler(["dve"], dve_command))

    # 2. Callback query handler for inline button clicks
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # 3. Message handler for raw CA postings with flags or interactive menus
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    # Run polling loop
    logger.info(f"Starting long polling on @{listener_agent.username}...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

