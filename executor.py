"""
executor.py - External Task Execution Engine & MSniffer Crypto Bot Bridge.

Triggered when consensus is reached in /exe (Vote & Execute) or /dve (Debate, Vote & Execute) modes.
Supports:
1. HTTP Webhook dispatch to Market Sniffer (POST /trade or POST /signal)
2. Telegram Bot dispatch (direct command /signal to @Msnifferbot or group alert)
3. Local IPC / Direct Signal File dispatch for co-located deployments
"""

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import aiohttp
from telegram import Bot
from telegram.constants import ParseMode

import config

logger = logging.getLogger("multi_agent_council.executor")

# Regex pattern for Solana Base58 Token Mint Addresses (32-44 characters)
SOLANA_CA_REGEX = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


@dataclass
class ExecutionResult:
    """Details of the execution attempt dispatched to external bots."""
    success: bool
    action: str
    mint: str
    amount: float
    message: str
    tx_hash: Optional[str] = None
    duration_seconds: float = 0.0
    dispatch_mode: str = "webhook"
    raw_response: Optional[Dict[str, Any]] = None


class SignalExtractor:
    """Extracts Solana contract addresses, actions, and trade parameters from prompt & consensus."""

    @classmethod
    def extract(cls, prompt: str, consensus_summary: str = "") -> Tuple[str, str, float, str]:
        """
        Extracts (mint_address, action, amount, symbol) from text.
        Defaults: action="BUY", amount=DEFAULT_TRADE_AMOUNT_SOL.
        """
        combined = f"{prompt} {consensus_summary}"

        # 1. Extract Solana Mint Address
        # Check for known dex URLs first (pump.fun, dexscreener, gmgn, birdeye, solscan)
        dex_match = re.search(
            r"(?:pump\.fun/(?:coin/)?|dexscreener\.com/solana/|gmgn\.ai/sol/token/|birdeye\.so/token/|solscan\.io/token/)([1-9A-HJ-NP-Za-km-z]{32,44})",
            combined,
            re.IGNORECASE,
        )
        if dex_match:
            mint = dex_match.group(1)
        else:
            cas = SOLANA_CA_REGEX.findall(combined)
            # Filter out non-mint common words that match base58
            valid_cas = [ca for ca in cas if len(ca) >= 32 and not ca.startswith("http")]
            mint = valid_cas[0] if valid_cas else ""

        # 2. Extract Action: BUY vs SELL
        action = "BUY"
        if re.search(r"\b(sell|dump|exit|liquidate|short)\b", combined, re.IGNORECASE):
            action = "SELL"

        # 3. Extract Amount
        amount = config.DEFAULT_TRADE_AMOUNT_SOL
        amt_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:sol|solana)\b", combined, re.IGNORECASE)
        if amt_match:
            try:
                parsed_amt = float(amt_match.group(1))
                if 0.01 <= parsed_amt <= 500.0:
                    amount = parsed_amt
            except ValueError:
                pass
        elif action == "SELL":
            pct_match = re.search(r"(\d{1,3})\s*%", combined)
            amount = float(pct_match.group(1)) if pct_match else 100.0

        # 4. Extract Symbol / Ticker (e.g. $BONK, $WIF)
        sym_match = re.search(r"\$([A-Za-z0-9]{2,10})\b", combined)
        symbol = sym_match.group(1).upper() if sym_match else ""

        return mint, action, amount, symbol


class MSnifferExecutor:
    """Dispatches consensus execution commands to the external Market Sniffer crypto bot."""

    @classmethod
    async def execute_consensus(
        cls,
        prompt: str,
        consensus_text: str,
        yes_count: int,
        total_votes: int,
        broadcaster_bot: Optional[Bot] = None,
        reply_to_chat_id: Optional[int] = None,
    ) -> ExecutionResult:
        """
        Entry point for executing approved council consensus.
        Dispatches via Webhook, Telegram, or Local IPC according to config.
        """
        start_time = time.time()
        mint, action, amount, symbol = SignalExtractor.extract(prompt, consensus_text)

        if not mint:
            return ExecutionResult(
                success=False,
                action=action,
                mint="",
                amount=amount,
                message="No valid Solana Contract Address (CA) was detected in the prompt or council consensus.",
                duration_seconds=time.time() - start_time,
                dispatch_mode=config.MSNIFFER_DISPATCH_MODE,
            )

        reason = (
            f"Council Consensus PASSED ({yes_count}/{total_votes} YES, "
            f"{(yes_count/total_votes*100):.0f}% approval). Prompt: {prompt[:120]}"
        )

        dispatch_mode = config.MSNIFFER_DISPATCH_MODE.lower()
        result: Optional[ExecutionResult] = None

        if dispatch_mode in ("webhook", "all"):
            result = await cls._dispatch_webhook(mint, action, amount, symbol, reason, start_time)

        if dispatch_mode in ("telegram", "all") and (not result or not result.success or dispatch_mode == "all"):
            tg_result = await cls._dispatch_telegram(
                mint, action, amount, symbol, reason, broadcaster_bot, reply_to_chat_id, start_time
            )
            if not result or (not result.success and tg_result.success):
                result = tg_result

        if not result:
            result = ExecutionResult(
                success=False,
                action=action,
                mint=mint,
                amount=amount,
                message=f"No execution dispatcher succeeded for mode '{config.MSNIFFER_DISPATCH_MODE}'.",
                duration_seconds=time.time() - start_time,
                dispatch_mode=dispatch_mode,
            )

        return result

    @classmethod
    async def _dispatch_webhook(
        cls, mint: str, action: str, amount: float, symbol: str, reason: str, start_time: float
    ) -> ExecutionResult:
        """Dispatches an HTTP POST request to Market Sniffer's webhook endpoint."""
        url = config.MSNIFFER_WEBHOOK_URL
        payload = {
            "action": action,
            "mint": mint,
            "amount": amount,
            "symbol": symbol,
            "slippage_bps": config.DEFAULT_SLIPPAGE_BPS,
            "reason": reason,
            "secret": config.MSNIFFER_SECRET,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Orchestrator-Secret": config.MSNIFFER_SECRET,
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20.0)) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    duration = time.time() - start_time
                    status = response.status
                    try:
                        data = await response.json()
                    except Exception:
                        raw_body = await response.text()
                        data = {"error": raw_body.strip() if raw_body else f"HTTP status {status}"}

                    is_success = bool(data.get("success", True)) if status in (200, 201) else False
                    tx_hash = data.get("tx_hash") or data.get("signature") or data.get("tx")

                    if is_success:
                        msg = data.get("message") or f"Dispatched successfully to Market Sniffer via HTTP ({status})"
                    else:
                        err_detail = data.get("error") or data.get("message") or data.get("detail") or f"HTTP status {status}"
                        msg = f"Market Sniffer execution notice: {err_detail}"

                    return ExecutionResult(
                        success=is_success,
                        action=action,
                        mint=mint,
                        amount=amount,
                        message=msg,
                        tx_hash=tx_hash,
                        duration_seconds=duration,
                        dispatch_mode="webhook",
                        raw_response=data,
                    )

        except aiohttp.ClientConnectorError as e:
            logger.warning(f"Could not connect to MSniffer webhook at {url}: {e}")
            return ExecutionResult(
                success=False,
                action=action,
                mint=mint,
                amount=amount,
                message=f"Webhook connection error: Market Sniffer server unreachable at {url} ({e})",
                duration_seconds=time.time() - start_time,
                dispatch_mode="webhook",
            )
        except Exception as e:
            logger.error(f"Error dispatching webhook to {url}: {e}", exc_info=True)
            return ExecutionResult(
                success=False,
                action=action,
                mint=mint,
                amount=amount,
                message=f"Webhook dispatch exception: {str(e)}",
                duration_seconds=time.time() - start_time,
                dispatch_mode="webhook",
            )

    @classmethod
    async def _dispatch_telegram(
        cls,
        mint: str,
        action: str,
        amount: float,
        symbol: str,
        reason: str,
        broadcaster_bot: Optional[Bot],
        reply_to_chat_id: Optional[int],
        start_time: float,
    ) -> ExecutionResult:
        """Dispatches a structured Telegram command or signal message."""
        target_chat = config.MSNIFFER_CHAT_ID or (str(reply_to_chat_id) if reply_to_chat_id else "")
        if not broadcaster_bot or not target_chat:
            return ExecutionResult(
                success=False,
                action=action,
                mint=mint,
                amount=amount,
                message="Cannot dispatch Telegram signal: No active bot or target MSniffer chat configured.",
                duration_seconds=time.time() - start_time,
                dispatch_mode="telegram",
            )

        # Build signal command accepted by market_sniffer_bot/orchestrator_receiver.py
        # Syntax: /signal <BUY|SELL> <mint> <amount_or_pct> [slippage_bps] [secret]
        secret_arg = f" {config.MSNIFFER_SECRET}" if config.MSNIFFER_SECRET else ""
        cmd_text = f"/signal {action} {mint} {amount} {config.DEFAULT_SLIPPAGE_BPS}{secret_arg}"

        try:
            sent_msg = await broadcaster_bot.send_message(
                chat_id=target_chat,
                text=cmd_text,
                parse_mode=None,  # Raw text command
            )
            return ExecutionResult(
                success=True,
                action=action,
                mint=mint,
                amount=amount,
                message=f"Dispatched command '{action} {mint}' directly to Market Sniffer via Telegram (Chat: {target_chat})",
                duration_seconds=time.time() - start_time,
                dispatch_mode="telegram",
                raw_response={"message_id": sent_msg.message_id},
            )
        except Exception as e:
            logger.error(f"Error dispatching Telegram signal to {target_chat}: {e}")
            return ExecutionResult(
                success=False,
                action=action,
                mint=mint,
                amount=amount,
                message=f"Telegram dispatch failed: {str(e)}",
                duration_seconds=time.time() - start_time,
                dispatch_mode="telegram",
            )



def format_execution_announcement(result: ExecutionResult) -> str:
    """Formats the execution outcome into a clean Telegram Markdown message for the group."""
    status_icon = "🚀" if result.success else "⚠️"
    status_title = "EXECUTION SIGNAL DISPATCHED" if result.success else "EXECUTION DISPATCH NOTICE"
    action_badge = "🟢 BUY" if result.action == "BUY" else "🔴 SELL"
    amount_str = f"{result.amount:.2f} SOL" if result.action == "BUY" else f"{result.amount:.1f}%"

    lines = [
        f"{status_icon} <b>{status_title}</b>",
        "───────────────────────────────",
        f"🎯 <b>Action:</b> {action_badge} (<code>{amount_str}</code>)",
        f"🪙 <b>Target CA:</b> <code>{html.escape(result.mint)}</code>" if result.mint else "🪙 <b>Target CA:</b> <i>None</i>",
        f"📡 <b>Channel:</b> <code>{html.escape(result.dispatch_mode.upper())}</code>",
        f"⚡ <b>Latency:</b> <code>{result.duration_seconds:.2f}s</code>",
        f"📋 <b>Outcome:</b> <i>{html.escape(result.message)}</i>",
    ]

    if result.tx_hash:
        solscan_url = f"https://solscan.io/tx/{result.tx_hash}"
        lines.append(f"🔗 <a href='{solscan_url}'>View Solana Transaction</a>")
    elif result.mint:
        dex_url = f"https://dexscreener.com/solana/{result.mint}"
        lines.append(f"📊 <a href='{dex_url}'>View DexScreener Chart</a>")

    lines.append("───────────────────────────────")
    return "\n".join(lines)
