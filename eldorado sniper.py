"""
╔══════════════════════════════════════════════╗
║       Eldorado DonutSMP Sniper Bot v2        ║
║  Monitors Donut Dollar listings & alerts     ║
║  Discord when good/insane deals appear       ║
╚══════════════════════════════════════════════╝

Requirements:
    pip install discord.py selenium webdriver-manager

Also needs Google Chrome installed on your machine.

Setup:
    1. Fill in the CONFIG section below
    2. Run: python eldorado_sniper.py
"""

import discord
import asyncio
import time
import re
import logging
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# ─────────────────────────────────────────────
#                   CONFIG
# ─────────────────────────────────────────────

DISCORD_TOKEN       = "YOUR_BOT_TOKEN_HERE"
ALERT_CHANNEL_ID    = 1570191400526549116      # Channel ID to post alerts in

# ── Price thresholds ──
# Good deal  → posts alert, no @everyone
# Insane deal → @everyone ping + resell advice + drop warning
GOOD_PRICE_PER_M    = 0.008    # ≤ $0.008/M = good deal
INSANE_PRICE_PER_M  = 0.006    # ≤ $0.006/M = INSANE deal → @everyone

# ── Stock filters ──
MIN_STOCK_M         = 85       # Must have at least 85M in stock
MAX_MIN_QTY_M       = 900      # Skip if seller requires you to buy > 900M minimum

# ── Resell config ──
# Bot will auto-calculate the best resell price based on current market listings
# It undercuts the cheapest non-insane listing by a small margin
RESELL_UNDERCUT     = 0.0001   # Undercut best market price by this much per M

# ── Market baseline (used for % savings calc if scrape doesn't find enough data) ──
MARKET_BASELINE     = 0.009    # $0.009/M

# Eldorado DonutSMP currency page
ELDORADO_URL        = "https://www.eldorado.gg/donut-smp/currency"

# How often to check in seconds (don't go below 60 or you'll get IP blocked)
CHECK_INTERVAL      = 90

# ─────────────────────────────────────────────
#              LOGGING SETUP
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("sniper")

# ─────────────────────────────────────────────
#              SELENIUM SCRAPER
# ─────────────────────────────────────────────

def build_driver() -> webdriver.Chrome:
    """Spin up a headless Chrome instance with anti-bot fingerprint evasion."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    service = Service(ChromeDriverManager().install())
    driver  = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"}
    )
    return driver


def parse_millions(text: str) -> float | None:
    """Parse '1,499 M' or '40,721M' → float of millions."""
    text = text.replace(",", "").replace(" ", "").upper()
    match = re.search(r"([\d.]+)M?", text)
    return float(match.group(1)) if match else None


def parse_price(text: str) -> float | None:
    """Parse '$0.009 / M' or '$0.0103/M' → float price per million."""
    text = text.replace(",", "").replace(" ", "")
    match = re.search(r"\$([\d.]+)", text)
    return float(match.group(1)) if match else None


def scrape_listings(driver: webdriver.Chrome) -> list[dict]:
    """
    Load Eldorado and extract all seller listings.
    Returns list of dicts: {seller, price_per_m, stock_m, min_qty_m, url, verified}
    """
    listings = []

    try:
        log.info(f"Loading {ELDORADO_URL} ...")
        driver.get(ELDORADO_URL)

        # Wait for any offer/listing card to appear
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "[class*='offer'], [class*='listing'], [class*='seller'], [class*='Offer'], article")
            )
        )
        # Extra wait for JS-rendered prices to populate
        time.sleep(3)

        # Try multiple card selectors — Eldorado uses hashed React class names
        card_selectors = [
            "[class*='OfferCard']",
            "[class*='offer-card']",
            "[class*='offerCard']",
            "[class*='listing-item']",
            "[class*='sellerOffer']",
            "[class*='Offer']:not([class*='OfferPage'])",
            "article",
        ]

        cards = []
        for sel in card_selectors:
            cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if len(cards) >= 1:
                log.info(f"Found {len(cards)} cards via selector: {sel}")
                break

        for card in cards:
            try:
                text = card.text.strip()
                if not text:
                    continue

                lines = [l.strip() for l in text.splitlines() if l.strip()]

                # ── Price ──
                price = None
                for line in lines:
                    if "$" in line and ("/ M" in line or "/M" in line or "per M" in line.lower()):
                        price = parse_price(line)
                        break
                if price is None:
                    continue

                # ── Seller name (first meaningful line) ──
                seller = lines[0] if lines else "Unknown"

                # ── Stock ──
                stock_m = None
                for i, line in enumerate(lines):
                    if "stock" in line.lower() and i + 1 < len(lines):
                        stock_m = parse_millions(lines[i + 1])
                        break

                # ── Min qty ──
                min_qty_m = None
                for i, line in enumerate(lines):
                    if "min" in line.lower() and "qty" in line.lower() and i + 1 < len(lines):
                        min_qty_m = parse_millions(lines[i + 1])
                        break

                # ── Verified badge ──
                verified = "✓" in text or "verified" in text.lower() or "✅" in text

                # ── Link ──
                try:
                    link_el = card.find_element(By.TAG_NAME, "a")
                    href = link_el.get_attribute("href") or ELDORADO_URL
                except Exception:
                    href = ELDORADO_URL

                listings.append({
                    "seller":      seller,
                    "price_per_m": price,
                    "stock_m":     stock_m,
                    "min_qty_m":   min_qty_m,
                    "url":         href,
                    "verified":    verified,
                })

            except Exception as e:
                log.debug(f"Skipped card: {e}")
                continue

    except Exception as e:
        log.error(f"Scrape error: {e}")

    log.info(f"Scraped {len(listings)} total listings")
    return listings


# ─────────────────────────────────────────────
#         DEAL CLASSIFIER + RESELL CALC
# ─────────────────────────────────────────────

def classify_listings(listings: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Returns (good_deals, insane_deals) — both pass stock/minqty filters.
    insane_deals also pass the INSANE_PRICE_PER_M threshold.
    """
    good   = []
    insane = []

    for listing in listings:
        price  = listing.get("price_per_m")
        stock  = listing.get("stock_m")
        minqty = listing.get("min_qty_m")

        if price is None:
            continue

        # Stock must be ≥ MIN_STOCK_M (if we could read it)
        if stock is not None and stock < MIN_STOCK_M:
            log.info(f"Skip {listing['seller']} — stock too low ({stock}M)")
            continue

        # Min qty must be ≤ MAX_MIN_QTY_M (skip if forces you to buy too much)
        if minqty is not None and minqty > MAX_MIN_QTY_M:
            log.info(f"Skip {listing['seller']} — min qty too high ({minqty}M)")
            continue

        if price <= INSANE_PRICE_PER_M:
            insane.append(listing)
        elif price <= GOOD_PRICE_PER_M:
            good.append(listing)

    return good, insane


def calculate_resell(buy_price: float, all_listings: list[dict]) -> dict:
    """
    Auto-calculates the best resell price and profit margins.

    Strategy:
    - Find the cheapest listing that is NOT an insane deal (normal market floor)
    - Undercut it by RESELL_UNDERCUT to become the most attractive listing
    - Calculate profit per million, ROI %, and recommended buy quantity
    """
    # Market prices = everything above INSANE threshold (normal sellers)
    normal_prices = [
        l["price_per_m"] for l in all_listings
        if l.get("price_per_m") and l["price_per_m"] > INSANE_PRICE_PER_M
    ]

    if normal_prices:
        market_floor  = min(normal_prices)
        resell_price  = round(market_floor - RESELL_UNDERCUT, 5)
    else:
        # Fallback to baseline if we can't read market
        market_floor  = MARKET_BASELINE
        resell_price  = round(MARKET_BASELINE - RESELL_UNDERCUT, 5)

    # Don't resell below cost — sanity check
    if resell_price <= buy_price:
        resell_price = round(buy_price * 1.15, 5)   # At least 15% markup

    profit_per_m  = round(resell_price - buy_price, 5)
    roi_pct       = round((profit_per_m / buy_price) * 100, 1)

    # How much would you spend to buy MIN_STOCK_M at this price?
    cost_for_min  = round(buy_price * MIN_STOCK_M, 2)
    # Profit if you flip all MIN_STOCK_M
    profit_total  = round(profit_per_m * MIN_STOCK_M, 2)

    # Is it worth it? Only recommend resell if ROI > 10%
    worth_resell  = roi_pct >= 10.0

    return {
        "market_floor":  market_floor,
        "resell_price":  resell_price,
        "profit_per_m":  profit_per_m,
        "roi_pct":       roi_pct,
        "cost_for_min":  cost_for_min,
        "profit_total":  profit_total,
        "worth_resell":  worth_resell,
    }


# ─────────────────────────────────────────────
#              EMBED BUILDERS
# ─────────────────────────────────────────────

def build_good_embed(listing: dict, all_listings: list[dict]) -> discord.Embed:
    """Green embed for a good deal (≤ $0.008/M)."""
    price    = listing["price_per_m"]
    stock    = listing.get("stock_m")
    min_qty  = listing.get("min_qty_m")
    seller   = listing["seller"]
    url      = listing["url"]
    verified = listing.get("verified", False)
    savings  = round(((MARKET_BASELINE - price) / MARKET_BASELINE) * 100, 1)
    resell   = calculate_resell(price, all_listings)

    embed = discord.Embed(
        title       = "💚 Good Deal — Donut Dollars",
        description = (
            f"**{seller}** {'✅' if verified else ''} is at "
            f"**${price:.4f}/M** — {savings}% below market"
        ),
        color       = 0x00C853,
        url         = url,
        timestamp   = datetime.utcnow(),
    )

    embed.add_field(name="💰 Price / M",  value=f"`${price:.4f}`",                              inline=True)
    embed.add_field(name="📦 Stock",      value=f"`{stock:,.0f}M`" if stock else "`N/A`",       inline=True)
    embed.add_field(name="🔢 Min Buy",    value=f"`{min_qty:,.0f}M`" if min_qty else "`N/A`",   inline=True)
    embed.add_field(name="📈 Market",     value=f"`${MARKET_BASELINE:.4f}/M`",                  inline=True)
    embed.add_field(name="💵 Cost (min)", value=f"`${resell['cost_for_min']:.2f}`",             inline=True)
    embed.add_field(name="🔗 Buy Link",   value=f"[Click Here]({url})",                         inline=True)

    # Resell advice
    if resell["worth_resell"]:
        embed.add_field(
            name  = "♻️ Resell Advice",
            value = (
                f"✅ **Worth flipping!**\n"
                f"List at `${resell['resell_price']:.4f}/M` (undercuts market floor of `${resell['market_floor']:.4f}/M`)\n"
                f"Profit: `${resell['profit_per_m']:.5f}/M` | ROI: `{resell['roi_pct']}%`\n"
                f"If you flip {MIN_STOCK_M}M → ~`${resell['profit_total']:.2f}` profit"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name  = "♻️ Resell Advice",
            value = f"⚠️ Margin too thin ({resell['roi_pct']}% ROI) — buy for personal use only",
            inline=False,
        )

    embed.set_footer(text="Eldorado Sniper • DonutSMP")
    return embed


def build_insane_embed(listing: dict, all_listings: list[dict]) -> discord.Embed:
    """Red/fire embed for an INSANE deal (≤ $0.006/M) — full drop alert."""
    price    = listing["price_per_m"]
    stock    = listing.get("stock_m")
    min_qty  = listing.get("min_qty_m")
    seller   = listing["seller"]
    url      = listing["url"]
    verified = listing.get("verified", False)
    savings  = round(((MARKET_BASELINE - price) / MARKET_BASELINE) * 100, 1)
    resell   = calculate_resell(price, all_listings)

    embed = discord.Embed(
        title       = "🚨🔥 INSANE DEAL — DROP NOW — Donut Dollars",
        description = (
            f"**{seller}** {'✅' if verified else ''} is at "
            f"**${price:.4f}/M** — **{savings}% BELOW MARKET!**\n"
            f"### ⚡ BUY THIS NOW BEFORE IT'S GONE"
        ),
        color       = 0xFF1744,
        url         = url,
        timestamp   = datetime.utcnow(),
    )

    embed.add_field(name="💰 Price / M",  value=f"`${price:.4f}`",                              inline=True)
    embed.add_field(name="📦 Stock Left", value=f"`{stock:,.0f}M`" if stock else "`N/A`",       inline=True)
    embed.add_field(name="🔢 Min Buy",    value=f"`{min_qty:,.0f}M`" if min_qty else "`N/A`",   inline=True)
    embed.add_field(name="📈 Market",     value=f"`${MARKET_BASELINE:.4f}/M`",                  inline=True)
    embed.add_field(name="💵 Cost (min)", value=f"`${resell['cost_for_min']:.2f}`",             inline=True)
    embed.add_field(name="🏷️ You Save",   value=f"`{savings}%` vs market",                     inline=True)

    # Resell section — always show for insane deals since margins are huge
    embed.add_field(
        name  = "♻️ Resell Advice",
        value = (
            f"{'✅ **FLIP THIS — huge margin**' if resell['worth_resell'] else '⚠️ Thin margin but price is so low it may still be worth it'}\n"
            f"List at `${resell['resell_price']:.4f}/M` (undercuts market at `${resell['market_floor']:.4f}/M`)\n"
            f"Profit per M: `${resell['profit_per_m']:.5f}` | ROI: `{resell['roi_pct']}%`\n"
            f"Flip {MIN_STOCK_M}M → ~**`${resell['profit_total']:.2f}` profit**"
        ),
        inline=False,
    )

    embed.add_field(
        name  = "🔗 Buy Link",
        value = f"**[→ BUY NOW ON ELDORADO ←]({url})**",
        inline=False,
    )

    embed.set_footer(text="Eldorado Sniper • DonutSMP")
    return embed


# ─────────────────────────────────────────────
#              DISCORD BOT
# ─────────────────────────────────────────────

intents = discord.Intents.default()
client  = discord.Client(intents=intents)

# Track alerted listings so we don't re-ping the same deal
alerted: set[str] = set()


async def sniper_loop():
    await client.wait_until_ready()
    channel = client.get_channel(ALERT_CHANNEL_ID)

    if not channel:
        log.error(f"❌ Can't find channel {ALERT_CHANNEL_ID} — check ALERT_CHANNEL_ID in config")
        return

    log.info(f"✅ Sniper armed! Posting to #{channel.name}")
    log.info(f"   Good deal:   ≤ ${GOOD_PRICE_PER_M}/M")
    log.info(f"   INSANE deal: ≤ ${INSANE_PRICE_PER_M}/M → @everyone")
    log.info(f"   Stock min:   ≥ {MIN_STOCK_M}M | Min qty max: ≤ {MAX_MIN_QTY_M}M")
    log.info(f"   Checking every {CHECK_INTERVAL}s")

    driver = build_driver()

    try:
        while not client.is_closed():
            try:
                listings            = scrape_listings(driver)
                good_deals, insane  = classify_listings(listings)

                # ── Log summary ──
                all_prices = [l["price_per_m"] for l in listings if l.get("price_per_m")]
                best_price = min(all_prices) if all_prices else None
                log.info(
                    f"Round done — {len(listings)} listings | "
                    f"Best: ${best_price:.4f}/M | "
                    f"Good: {len(good_deals)} | Insane: {len(insane)}"
                )

                # ── Post INSANE deals first ──
                for deal in insane:
                    key = f"{deal['seller']}:{deal['price_per_m']}"
                    if key in alerted:
                        continue
                    alerted.add(key)
                    embed = build_insane_embed(deal, listings)
                    await channel.send("@everyone 🚨🔥 **INSANE DEAL — DROP NOW**", embed=embed)
                    log.info(f"🚨 INSANE ALERT: {deal['seller']} @ ${deal['price_per_m']}/M")

                # ── Post good deals (no @everyone) ──
                for deal in good_deals:
                    key = f"{deal['seller']}:{deal['price_per_m']}"
                    if key in alerted:
                        continue
                    alerted.add(key)
                    embed = build_good_embed(deal, listings)
                    await channel.send("💚 Good deal spotted!", embed=embed)
                    log.info(f"💚 GOOD DEAL: {deal['seller']} @ ${deal['price_per_m']}/M")

                # ── Clean up alerted set for listings that disappeared ──
                current_keys = {f"{l['seller']}:{l['price_per_m']}" for l in listings}
                alerted.intersection_update(current_keys)

            except Exception as e:
                log.error(f"Loop error: {e}")
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = build_driver()

            await asyncio.sleep(CHECK_INTERVAL)

    finally:
        driver.quit()


@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} ({client.user.id})")
    client.loop.create_task(sniper_loop())


# ─────────────────────────────────────────────
#                    MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Set DISCORD_TOKEN in the CONFIG section!")
        exit(1)
    if ALERT_CHANNEL_ID == 1234567890123456789:
        print("❌ Set ALERT_CHANNEL_ID in the CONFIG section!")
        exit(1)

    client.run(DISCORD_TOKEN)
