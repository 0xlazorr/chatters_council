"""
telemetry.py - On-Chain Token Intelligence & Telemetry Engine.

Fetches real-time market data, liquidity metrics, holder distributions,
and smart contract security audits directly from a Solana Contract Address (CA).
Provides structured institutional dossiers to the AI Council so they can talk
about tokens using their actual NAME and TICKER, grounded in verifiable data.
"""

import asyncio
import json
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("multi_agent_council.telemetry")


async def fetch_token_dossier(mint: str) -> Optional[Dict[str, Any]]:
    """
    Fetches comprehensive on-chain token data from DexScreener and RugCheck APIs in parallel:
    1. DexScreener: Token Name, Symbol, Price USD, Market Cap / FDV, Liquidity, 24h/1h Volume,
       Buy/Sell transaction counts, 1h/24h Price Change, DEX Pair platform.
    2. RugCheck: Mint Authority (revoked/active), Freeze Authority (revoked/active),
       Top 10 holders percentage, overall security risk score, and identified warnings.
    """
    if not mint or len(mint) < 32:
        return None

    clean_mint = mint.strip()
    loop = asyncio.get_running_loop()

    def _fetch_dex() -> Optional[Dict[str, Any]]:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{clean_mint}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ChattersCouncil/2.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug(f"DexScreener fetch error for {clean_mint}: {e}")
            return None

    def _fetch_rugcheck() -> Optional[Dict[str, Any]]:
        url = f"https://api.rugcheck.xyz/v1/tokens/{clean_mint}/report"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.debug(f"RugCheck fetch error for {clean_mint}: {e}")
            return None

    # Run queries concurrently
    dex_data, rug_data = await asyncio.gather(
        loop.run_in_executor(None, _fetch_dex),
        loop.run_in_executor(None, _fetch_rugcheck),
    )

    pairs = (dex_data or {}).get("pairs") or []
    # Pick pair with highest liquidity
    best_pair = None
    if pairs:
        best_pair = max(
            pairs,
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0)
        )

    # Parse RugCheck security data
    rug = rug_data or {}
    token_meta = rug.get("tokenMeta") or {}
    mint_auth_raw = token_meta.get("mintAuthority")
    freeze_auth_raw = token_meta.get("freezeAuthority")

    mint_auth = "REVOKED 🟢" if mint_auth_raw is None else "ACTIVE ⚠️"
    freeze_auth = "REVOKED 🟢" if freeze_auth_raw is None else "ACTIVE ⚠️"
    score = rug.get("score", 0)
    top_holders = rug.get("topHolders") or []
    top10_pct = round(sum(float(h.get("pct", 0) or 0) for h in top_holders[:10]), 2)
    risks = [rk.get("name") for rk in (rug.get("risks") or []) if rk.get("name")]

    if not best_pair:
        logger.warning(f"No active DEX trading pairs found on Solana for {clean_mint}")
        fallback_name = token_meta.get("name") or f"Token {clean_mint[:6]}"
        fallback_symbol = (token_meta.get("symbol") or clean_mint[:4]).replace("$", "").upper()
        return {
            "mint": clean_mint,
            "name": fallback_name,
            "symbol": fallback_symbol,
            "price_usd": 0.0,
            "market_cap_usd": 0.0,
            "fdv_usd": 0.0,
            "liquidity_usd": 0.0,
            "volume_24h_usd": 0.0,
            "volume_1h_usd": 0.0,
            "buys_24h": 0,
            "sells_24h": 0,
            "change_1h": 0.0,
            "change_24h": 0.0,
            "dex": "Solana DEX (Unlisted/Early)",
            "pair_url": f"https://dexscreener.com/solana/{clean_mint}",
            "mint_authority": mint_auth,
            "freeze_authority": freeze_auth,
            "top10_pct": top10_pct,
            "rugcheck_score": score,
            "risks": risks,
        }

    base = best_pair.get("baseToken") or {}
    name = base.get("name") or token_meta.get("name") or "Solana Token"
    symbol = (base.get("symbol") or token_meta.get("symbol") or "TOKEN").replace("$", "").upper()

    price = float(best_pair.get("priceUsd") or 0.0)
    fdv = float(best_pair.get("fdv") or best_pair.get("marketCap") or 0.0)
    mcap = float(best_pair.get("marketCap") or fdv)
    liq = float((best_pair.get("liquidity") or {}).get("usd") or 0.0)
    vol24 = float((best_pair.get("volume") or {}).get("h24") or 0.0)
    vol1 = float((best_pair.get("volume") or {}).get("h1") or 0.0)
    change1h = float((best_pair.get("priceChange") or {}).get("h1") or 0.0)
    change24h = float((best_pair.get("priceChange") or {}).get("h24") or 0.0)
    txns24 = (best_pair.get("txns") or {}).get("h24") or {}
    buys24 = int(txns24.get("buys") or 0)
    sells24 = int(txns24.get("sells") or 0)
    dex = (best_pair.get("dexId") or "Raydium").upper()
    pair_url = best_pair.get("url") or f"https://dexscreener.com/solana/{clean_mint}"

    return {
        "mint": clean_mint,
        "name": name,
        "symbol": symbol,
        "price_usd": price,
        "market_cap_usd": mcap,
        "fdv_usd": fdv,
        "liquidity_usd": liq,
        "volume_24h_usd": vol24,
        "volume_1h_usd": vol1,
        "buys_24h": buys24,
        "sells_24h": sells24,
        "change_1h": change1h,
        "change_24h": change24h,
        "dex": dex,
        "pair_url": pair_url,
        "mint_authority": mint_auth,
        "freeze_authority": freeze_auth,
        "top10_pct": top10_pct,
        "rugcheck_score": score,
        "risks": risks,
    }


def format_council_prompt_from_dossier(
    dossier: Dict[str, Any],
    action: str = "BUY",
    amount_sol: float = 0.25,
    user_context: str = "",
) -> str:
    """
    Constructs a high-density, token-efficient institutional council briefing string.
    Cuts prompt token overhead while ensuring complete accuracy and verifiable data.
    """
    name = dossier.get("name", "Unknown Token")
    symbol = dossier.get("symbol", "TOKEN")
    mint = dossier.get("mint", "")
    dex = dossier.get("dex", "Solana DEX")
    price = float(dossier.get("price_usd") or 0.0)
    mcap = float(dossier.get("market_cap_usd") or dossier.get("fdv_usd") or 0.0)
    liq = float(dossier.get("liquidity_usd") or 0.0)
    vol24 = float(dossier.get("volume_24h_usd") or 0.0)
    vol1 = float(dossier.get("volume_1h_usd") or 0.0)
    c1h = float(dossier.get("change_1h") or 0.0)
    c24h = float(dossier.get("change_24h") or 0.0)
    buys = int(dossier.get("buys_24h") or 0)
    sells = int(dossier.get("sells_24h") or 0)
    total_txns = buys + sells
    buy_ratio = (buys / total_txns * 100.0) if total_txns > 0 else 50.0
    liq_ratio = (liq / mcap * 100.0) if mcap > 0 else 0.0

    mint_auth = dossier.get("mint_authority", "Unknown")
    freeze_auth = dossier.get("freeze_authority", "Unknown")
    top10 = float(dossier.get("top10_pct") or 0.0)
    score = dossier.get("rugcheck_score", 0)
    risks = dossier.get("risks") or []
    risks_str = ", ".join(risks[:3]) if risks else "Clean"

    extra_note = f" (Context: {user_context})" if user_context and user_context != mint else ""

    return (
        f"ASSET DOSSIER: {name} (${symbol}) | Mint: {mint} | DEX: {dex}\n"
        f"• Price: ${price:.8f} | Mcap/FDV: ${mcap:,.0f} | Liquidity: ${liq:,.0f} ({liq_ratio:.1f}% ratio)\n"
        f"• 24h Vol: ${vol24:,.0f} (1h Vol: ${vol1:,.0f}) | Activity: {buys:,}B/{sells:,}S ({buy_ratio:.1f}% buys) | 1h: {c1h:+.1f}%, 24h: {c24h:+.1f}%\n"
        f"• Security: Mint Auth: {mint_auth} | Freeze Auth: {freeze_auth} | Top 10 Holders: {top10:.1f}% | Risk Score: {score} ({risks_str})\n"
        f"PROPOSITION: Evaluate {action} {amount_sol:.2f} SOL of {name} (${symbol}).{extra_note}\n"
        f"MANDATE: Refer to this asset as '{name}' (${symbol}). State your 1-2 sentence rationale citing these exact metrics."
    )


def format_single_agent_token_context(dossier: Dict[str, Any], user_query: str) -> str:
    """Provides a concise, token-efficient context packet for 1-on-1 agent conversations."""
    name = dossier.get("name", "Unknown Token")
    symbol = dossier.get("symbol", "TOKEN")
    price = float(dossier.get("price_usd") or 0.0)
    mcap = float(dossier.get("market_cap_usd") or 0.0)
    liq = float(dossier.get("liquidity_usd") or 0.0)
    vol24 = float(dossier.get("volume_24h_usd") or 0.0)
    c1h = float(dossier.get("change_1h") or 0.0)
    mint_auth = dossier.get("mint_authority", "Unknown")
    freeze_auth = dossier.get("freeze_authority", "Unknown")
    top10 = float(dossier.get("top10_pct") or 0.0)
    score = dossier.get("rugcheck_score", 0)

    return (
        f"User Query: {user_query}\n\n"
        f"Real-Time On-Chain Data for {name} (${symbol}):\n"
        f"• Price: ${price:.8f} | Mcap: ${mcap:,.0f} | Liquidity: ${liq:,.0f}\n"
        f"• 24h Vol: ${vol24:,.0f} | 1h Momentum: {c1h:+.1f}%\n"
        f"• Security: Mint Auth: {mint_auth} | Freeze Auth: {freeze_auth} | Top 10: {top10:.1f}% | Risk Score: {score}\n\n"
        f"Instruction: Respond conversationally in 2-3 sentences using the token's real name '{name}' (${symbol}) "
        f"and verifiable metrics. Never output raw contract addresses."
    )


def format_telegram_token_card(dossier: Dict[str, Any], action: str = "BUY", amount_sol: float = 0.25) -> str:
    """Formats an attractive Telegram HTML briefing card displaying the live token data."""
    name = dossier.get("name", "Unknown Token")
    symbol = dossier.get("symbol", "TOKEN")
    mint = dossier.get("mint", "")
    dex = dossier.get("dex", "Solana")
    price = dossier.get("price_usd", 0.0)
    mcap = dossier.get("market_cap_usd", 0.0)
    liq = dossier.get("liquidity_usd", 0.0)
    vol24 = dossier.get("volume_24h_usd", 0.0)
    c1h = dossier.get("change_1h", 0.0)
    c24h = dossier.get("change_24h", 0.0)
    buys = dossier.get("buys_24h", 0)
    sells = dossier.get("sells_24h", 0)
    top10 = dossier.get("top10_pct", 0.0)
    mint_auth = "Revoked 🟢" if "REVOKED" in str(dossier.get("mint_authority", "")) else "Active ⚠️"
    freeze_auth = "Revoked 🟢" if "REVOKED" in str(dossier.get("freeze_authority", "")) else "Active ⚠️"
    pair_url = dossier.get("pair_url") or f"https://dexscreener.com/solana/{mint}"

    c1h_badge = f"+{c1h:.1f}% 🟢" if c1h >= 0 else f"{c1h:.1f}% 🔴"
    c24h_badge = f"+{c24h:.1f}% 🟢" if c24h >= 0 else f"{c24h:.1f}% 🔴"

    return (
        f"🪙 <b>TOKEN DOSSIER: {name} (${symbol})</b>\n"
        f"───────────────────────────────\n"
        f"💵 <b>Price:</b> <code>${price:.6f}</code> | <b>DEX:</b> <code>{dex}</code>\n"
        f"📊 <b>Market Cap:</b> <code>${mcap:,.0f}</code>\n"
        f"💧 <b>Liquidity:</b> <code>${liq:,.0f}</code>\n"
        f"📈 <b>24h Volume:</b> <code>${vol24:,.0f}</code>\n"
        f"⚡ <b>1h Momentum:</b> <b>{c1h_badge}</b> | <b>24h:</b> <b>{c24h_badge}</b>\n"
        f"👥 <b>24h Txns:</b> <code>{buys:,} buys / {sells:,} sells</code>\n"
        f"🛡️ <b>Mint:</b> <code>{mint_auth}</code> | <b>Freeze:</b> <code>{freeze_auth}</code>\n"
        f"🐋 <b>Top 10 Holders:</b> <code>{top10:.1f}%</code>\n"
        f"🎯 <b>Target Allocation:</b> <code>{action} {amount_sol:.2f} SOL</code>\n"
        f"───────────────────────────────\n"
        f"🔗 <a href='{pair_url}'>View on DexScreener</a> | <code>{mint[:6]}...{mint[-4:]}</code>"
    )
