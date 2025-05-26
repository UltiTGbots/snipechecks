#!/usr/bin/env python3
import os
import re
import logging
import requests
from datetime import datetime
from urllib.parse import quote

from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Update, Chat
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==========================================
# 1. Load Environment Variables
# ==========================================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")

if not TELEGRAM_BOT_TOKEN or not MONGODB_URI:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or MONGODB_URI in .env")

# ==========================================
# 2. Logging Configuration
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 3. MongoDB Setup
# ==========================================
client = MongoClient(MONGODB_URI)
db = client["snipe_checks"]
picks_collection = db["picks"]     # For shilled CAs
wallets_collection = db["wallets"] # For sniper bowl wallets

# Ensure indexes
picks_collection.create_index(
    [("chat_id", 1), ("mint_address", 1)],
    unique=True,
    name="chat_mint_unique_index"
)

# ==========================================
# 4. Pump.fun API
# ==========================================
def get_sol_price() -> float:
    url = "https://frontend-api-v2.pump.fun/sol-price"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("solPrice", 0.0)
    except Exception as e:
        logger.error(f"Error fetching SOL price: {e}")
        return 0.0

def get_latest_close_price_in_sol(mint_address: str) -> float:
    base_url = f"https://frontend-api-v2.pump.fun/candlesticks/{mint_address}"
    params = {"offset": "0", "limit": "1", "timeframe": "1"}
    try:
        resp = requests.get(base_url, params=params, timeout=10)
        resp.raise_for_status()
        csticks = resp.json()
        if not csticks:
            return 0.0
        latest_candle = csticks[-1]
        return float(latest_candle.get("close", 0.0))
    except Exception as e:
        logger.error(f"Error fetching candlestick for {mint_address}: {e}")
        return 0.0

def is_valid_solana_address(address: str) -> bool:
    if len(address) not in [43, 44]:
        return False
    pattern = r'^[1-9A-HJ-NP-Za-km-z]+$'
    return bool(re.match(pattern, address))

def get_wallet_balances(wallet_address: str, limit=50, offset=0) -> list:
    url = f"https://frontend-api-v2.pump.fun/balances/{wallet_address}"
    params = {"limit": limit, "offset": offset, "minBalance": -1}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()  # list of token objects
    except Exception as e:
        logger.error(f"Error fetching balances for {wallet_address}: {e}")
        return []

# ==========================================
# 5. Bot Handlers
# ==========================================

# ------------ HELP & START ------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - Simplified welcome message with emojis.
    """
    welcome_text = (
        "👋 *Welcome to Snipe Checks Bot!*\n\n"
        "With our bot you can do 2 very cool things:\n"
        "1️⃣ *Shill a CA:* Paste any Solana *Mint Address (CA)*, and we'll track your PnL on 0.5 SOL.\n"
        "2️⃣ *Sniper Bowl:* Register a wallet used to buy 0.5 SOL and trade. We'll track your real PnL.\n\n"
        "Type /help for commands.\n"
        "Enjoy! 🚀"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help - Shows usage and commands with emojis.
    """
    help_text = (
        "🆘 *Snipe Checks Bot Help*\n\n"
        "• `/leaderboard` – Shows the *shilled CA leaderboard* (Function 1)\n"
        "• `/register_wallet <address>` – Register your wallet for the *Sniper Bowl* (Function 2)\n"
        "• `/sniper_leaderboard` – Shows the *Sniper Bowl leaderboard* (wallet-based)\n"
        "• `/share` – Share your *CA picks* on Twitter\n\n"
        "Just paste a *mint address* in chat to add a shilled CA. 🏹\n"
        "Or `/register_wallet` to track your *wallet* for the Sniper Bowl."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ------------ FUNCTION 1: SHILLING CAs ------------
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("❌ Could not fetch SOL price. Leaderboard unavailable.")
        return

    all_picks = list(picks_collection.find({"chat_id": chat_id}))
    if not all_picks:
        await update.message.reply_text("No CA picks found. Paste a CA to add your first pick!")
        return

    data_list = []
    for pick in all_picks:
        mint = pick["mint_address"]
        cost_basis_usd = pick["cost_basis_usd"]
        num_tokens = pick["num_tokens"]
        username = pick["username"]

        current_close_sol = get_latest_close_price_in_sol(mint)
        current_token_price_usd = current_close_sol * sol_price
        current_value_usd = num_tokens * current_token_price_usd
        pnl = current_value_usd - cost_basis_usd

        data_list.append({
            "username": username,
            "mint": mint,
            "cost_basis_usd": cost_basis_usd,
            "current_price_usd": current_token_price_usd,
            "pnl": pnl
        })

    data_list.sort(key=lambda x: x["pnl"], reverse=True)
    result_text = "🏆 *Shilled CA Leaderboard:* 🏆\n\n"
    for rank, item in enumerate(data_list[:10], start=1):
        sign = "+" if item["pnl"] >= 0 else "-"
        abs_pnl = abs(item["pnl"])
        result_text += (
            f"{rank}. {item['username']} (Mint: `{item['mint']}`)\n"
            f"   PnL: {sign}${abs_pnl:,.2f}\n"
            f"   Entry(0.5 SOL in USD): ${item['cost_basis_usd']:.2f}\n"
            f"   Current Token Price: ${item['current_price_usd']:.8f}\n\n"
        )

    await update.message.reply_text(result_text, parse_mode="Markdown")

# ------------ FUNCTION 2: SNIPER BOWL ------------
async def register_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anonymous"

    if len(context.args) == 0:
        await update.message.reply_text("Usage: /register_wallet <solana_wallet_address>")
        return

    wallet_address = context.args[0].strip()
    if not is_valid_solana_address(wallet_address):
        await update.message.reply_text("❌ Invalid Solana address. Please try again.")
        return

    existing = wallets_collection.find_one({"chat_id": chat_id, "wallet_address": wallet_address})
    if existing:
        await update.message.reply_text("⚠️ This wallet is already registered in this chat.")
        return

    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("❌ Could not fetch SOL price. Try again later.")
        return

    start_usd_value = 0.5 * sol_price

    doc = {
        "chat_id": chat_id,
        "user_id": user_id,
        "username": username,
        "wallet_address": wallet_address,
        "start_usd_value": start_usd_value,
        "created_at": datetime.utcnow()
    }
    try:
        wallets_collection.insert_one(doc)
    except Exception as e:
        logger.error(f"Error registering wallet: {e}")
        await update.message.reply_text("❌ Could not register wallet. Possibly a duplicate or DB error.")
        return

    msg = (
        f"✅ Registered your wallet for the Sniper Bowl:\n"
        f"📍 *{wallet_address}*\n"
        f"Starting assumption: 0.5 SOL (~${start_usd_value:.2f}).\n"
        f"Use /sniper_leaderboard to see who’s winning!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def sniper_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("❌ Could not fetch SOL price. Leaderboard unavailable.")
        return

    all_wallets = list(wallets_collection.find({"chat_id": chat_id}))
    if not all_wallets:
        await update.message.reply_text("No wallets here. Use /register_wallet <address> to join!")
        return

    results = []
    for w in all_wallets:
        user_name = w["username"]
        wallet_address = w["wallet_address"]
        start_usd_value = w["start_usd_value"]

        balances = get_wallet_balances(wallet_address)

        total_usd = 0.0
        for token_info in balances:
            token_price = token_info.get("value", 0)
            token_balance = token_info.get("balance", 0)
            total_usd += token_balance * token_price

        pnl_usd = total_usd - start_usd_value

        results.append({
            "username": user_name,
            "wallet_address": wallet_address,
            "net_worth_usd": total_usd,
            "pnl_usd": pnl_usd
        })

    results.sort(key=lambda x: x["pnl_usd"], reverse=True)

    result_text = "🏆 *Sniper Bowl Leaderboard:* 🏆\n\n"
    for rank, item in enumerate(results[:10], start=1):
        sign = "+" if item["pnl_usd"] >= 0 else "-"
        abs_pnl = abs(item["pnl_usd"])
        result_text += (
            f"{rank}. {item['username']} (Wallet: `{item['wallet_address']}`)\n"
            f"   Net Worth: ${item['net_worth_usd']:.2f}\n"
            f"   PnL: {sign}${abs_pnl:,.2f}\n\n"
        )

    await update.message.reply_text(result_text, parse_mode="Markdown")

# ------------ /share ------------
async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anonymous"

    user_picks = list(picks_collection.find({"chat_id": chat_id, "user_id": user_id}))
    if not user_picks:
        await update.message.reply_text("No CA picks found for you here. Paste a CA first!")
        return

    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("Error fetching SOL price. Try again later.")
        return

    lines = []
    total_pnl = 0.0

    for pick in user_picks:
        mint = pick["mint_address"]
        cost_basis_usd = pick["cost_basis_usd"]
        num_tokens = pick["num_tokens"]

        current_close_sol = get_latest_close_price_in_sol(mint)
        current_price_usd = current_close_sol * sol_price
        current_value_usd = num_tokens * current_price_usd
        pnl = current_value_usd - cost_basis_usd
        total_pnl += pnl

        sign = "+" if pnl >= 0 else "-"
        abs_pnl = abs(pnl)
        lines.append(f"{mint} => {sign}${abs_pnl:,.2f}")

    sign_total = "+" if total_pnl >= 0 else "-"
    abs_total = abs(total_pnl)

    tweet_text = (
        f"{username}'s Picks (Chat {chat_id}):\n\n"
        + "\n".join(lines)
        + f"\n\nTotal PnL: {sign_total}${abs_total:,.2f}\n"
        "Shared via #SnipeChecksBot"
    )
    encoded_tweet = quote(tweet_text)
    twitter_link = f"https://twitter.com/intent/tweet?text={encoded_tweet}"

    msg = (
        f"🔗 Share your picks on Twitter:\n\n"
        f"[Click Here to Tweet]({twitter_link})"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

# ------------ Catch CA or fallback ------------
async def handle_contract_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not is_valid_solana_address(text):
        await fallback_echo(update, context)
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or "Anonymous"
    mint_address = text

    existing_pick = picks_collection.find_one({"chat_id": chat_id, "mint_address": mint_address})
    if existing_pick:
        await update.message.reply_text(f"⚠️ This CA was already shilled here: {mint_address}")
        return

    sol_price = get_sol_price()
    if sol_price <= 0:
        await update.message.reply_text("Error: Could not fetch SOL price. Try again later.")
        return

    close_price_sol = get_latest_close_price_in_sol(mint_address)
    if close_price_sol <= 0:
        await update.message.reply_text(f"Error: Invalid close price for CA: {mint_address}")
        return

    cost_basis_usd = 0.5 * sol_price
    num_tokens = 0.5 / close_price_sol

    pick_doc = {
        "chat_id": chat_id,
        "user_id": user_id,
        "username": username,
        "mint_address": mint_address,
        "cost_basis_usd": cost_basis_usd,
        "num_tokens": num_tokens,
        "created_at": datetime.utcnow()
    }
    try:
        picks_collection.insert_one(pick_doc)
    except Exception as e:
        logger.error(f"Error inserting pick: {e}")
        await update.message.reply_text("❌ Could not add your pick. Possibly a duplicate or DB error.")
        return

    reply_text = (
        f"✅ Added your pick for CA: {mint_address}\n"
        f"Invested: 0.5 SOL (~${cost_basis_usd:.2f})\n"
        f"Received ~{num_tokens:.4f} tokens.\n"
        f"Use /leaderboard to see rankings!\n"
        f"Use /share to post on Twitter."
    )
    await update.message.reply_text(reply_text)

async def fallback_echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback echo if text is not recognized as CA/command."""
    await update.message.reply_text(f"You said: {update.message.text}")

# ==========================================
# 6. Main
# ==========================================
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("register_wallet", register_wallet_command))
    app.add_handler(CommandHandler("sniper_leaderboard", sniper_leaderboard_command))
    app.add_handler(CommandHandler("share", share_command))

    # Handle text -> either valid CA or fallback
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_contract_address))

    logger.info("Starting Snipe Checks Bot with MongoDB persistence...")
    app.run_polling()


if __name__ == "__main__":
    main()
