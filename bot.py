import os
import json
import re
import base64
import logging
import urllib.request
import httpx
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    PreCheckoutQueryHandler, CallbackQueryHandler, ContextTypes, filters,
)
from openai import OpenAI

# ── CONFIG ───────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8728265878:AAH7pdsOpSO1x4eDrnY9pKaFA5IYS7DlU6E")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "sk-ВСТАВЬ_КЛЮЧ")

# YooKassa: подключается в @BotFather → Payments → YooKassa
# Формат: "SHOP_ID:LIVE_KEY" — берёшь в личном кабинете ЮКасса
YOOKASSA_TOKEN = os.getenv("YOOKASSA_TOKEN", "")

# CryptoBot: получи API-ключ в Telegram → @CryptoBot → "Create App"
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")
CRYPTO_BOT_API   = "https://pay.crypt.bot/api"

client = OpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

# ── ТАРИФНЫЕ ПЛАНЫ ───────────────────────────────────────────────────────────────
FREE_CREDITS = 3

PLANS = {
    "start": {
        "name": "Старт",
        "emoji": "🚀",
        "credits": 10,
        "duration_days": None,         # бессрочно
        "price_rub": 490,
        "price_usdt": 5.0,
        "price_ton": 50,
        "description": "10 инфографик",
    },
    "pro": {
        "name": "Про",
        "emoji": "💎",
        "credits": 30,
        "duration_days": None,
        "price_rub": 990,
        "price_usdt": 10.0,
        "price_ton": 100,
        "description": "30 инфографик",
    },
    "unlimited": {
        "name": "Безлимит",
        "emoji": "♾️",
        "credits": -1,                 # -1 = безлимит
        "duration_days": 30,           # на 30 дней
        "price_rub": 9980,
        "price_usdt": 100.0,
        "price_ton": 1000,
        "description": "Безлимит на 30 дней",
    },
}

# ── ХРАНИЛИЩЕ (in-memory; заменить на SQLite для продакшна) ──────────────────────
# user_id → {"credits": int, "unlimited_until": datetime|None, "pending": {...}}
user_data: dict[int, dict] = {}


def get_user(uid: int) -> dict:
    if uid not in user_data:
        user_data[uid] = {"credits": FREE_CREDITS, "unlimited_until": None, "pending": {}}
    return user_data[uid]


def has_access(uid: int) -> bool:
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        return True
    return u["credits"] > 0


def consume(uid: int):
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        return   # безлимит, не тратим
    if u["credits"] > 0:
        u["credits"] -= 1


def credits_display(uid: int) -> str:
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        until = u["unlimited_until"].strftime("%d.%m.%Y")
        return f"♾️ Безлимит до {until}"
    return f"💡 Осталось генераций: *{u['credits']}*"


# ── FONTS ────────────────────────────────────────────────────────────────────────
FONT_DIR = "/tmp/fonts"
os.makedirs(FONT_DIR, exist_ok=True)

_FONT_SOURCES = [
    ("bold",
     f"{FONT_DIR}/NotoSans-Bold.ttf",
     "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf"),
    ("regular",
     f"{FONT_DIR}/NotoSans-Regular.ttf",
     "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf"),
]
_SYSTEM_FONTS = {
    "bold": [
        f"{FONT_DIR}/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "regular": [
        f"{FONT_DIR}/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
}


def download_fonts():
    for _, path, url in _FONT_SOURCES:
        if not os.path.exists(path):
            try:
                urllib.request.urlretrieve(url, path)
                logging.info(f"Font downloaded: {path}")
            except Exception as e:
                logging.warning(f"Font download failed ({path}): {e}")


def get_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    for path in _SYSTEM_FONTS.get(style, _SYSTEM_FONTS["regular"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ── GPT-4o VISION ────────────────────────────────────────────────────────────────
def analyze_product_image(image_path: str) -> dict:
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    prompt = """Ты эксперт по маркетингу на маркетплейсах (Wildberries, OZON).
Посмотри на фото товара и верни ТОЛЬКО JSON (без markdown, без пояснений):

{
  "title": "Название товара (макс 28 символов)",
  "subtitle": "УТП / ключевая выгода (макс 38 символов)",
  "features": [
    "Преимущество 1 (макс 24 символа)",
    "Преимущество 2 (макс 24 символа)",
    "Преимущество 3 (макс 24 символа)",
    "Преимущество 4 (макс 24 символа)"
  ],
  "badge": "ХИТ | НОВИНКА | -20% | БЕСТСЕЛЛЕР | ТОП",
  "cta": "Купить сейчас | В корзину | Заказать",
  "color_theme": "warm | cool | neutral | dark"
}

Правила:
- Пиши по-русски
- Преимущества — конкретные, продающие (цифры, факты)
- color_theme: warm для еды/beauty/дом, cool для техники/спорта, neutral для одежды, dark для люкса
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        max_tokens=400,
    )
    raw = re.sub(r"```json|```", "", response.choices[0].message.content.strip()).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {
            "title": "Товар", "subtitle": "Отличное качество",
            "features": ["Натуральный состав", "Премиум качество", "Быстрая доставка", "Гарантия"],
            "badge": "ХИТ", "cta": "Купить сейчас", "color_theme": "warm",
        }


# ── COLOR THEMES ─────────────────────────────────────────────────────────────────
THEMES = {
    "warm":    {"bg_top":(254,248,240),"bg_bot":(255,220,180),"accent":(180,75,20), "accent2":(230,135,45),"card":(255,253,249),"text":(35,25,15),"text2":(110,75,45)},
    "cool":    {"bg_top":(238,245,255),"bg_bot":(205,225,255),"accent":(35,85,185), "accent2":(65,125,225),"card":(247,250,255),"text":(20,30,60),"text2":(50,80,150)},
    "neutral": {"bg_top":(248,248,248),"bg_bot":(228,228,228),"accent":(55,55,55),  "accent2":(115,115,115),"card":(255,255,255),"text":(30,30,30),"text2":(90,90,90)},
    "dark":    {"bg_top":(28,28,38),   "bg_bot":(18,18,28),   "accent":(210,165,75),"accent2":(245,205,105),"card":(42,42,55),  "text":(240,235,220),"text2":(190,175,140)},
}

W, H = 1080, 1080


def make_gradient(theme: dict) -> Image.Image:
    t    = np.linspace(0, 1, H)[:, np.newaxis]
    top  = np.array(theme["bg_top"], dtype=np.float32)
    bot  = np.array(theme["bg_bot"], dtype=np.float32)
    rows = (top * (1 - t) + bot * t).astype(np.uint8)
    arr  = np.broadcast_to(rows[:, np.newaxis, :], (H, W, 3)).copy()
    return Image.fromarray(arr, "RGB").convert("RGBA")


def rounded_layer(w, h, r, fill, outline=None, ow=2, blur=0) -> Image.Image:
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle([0, 0, w, h], radius=r,
                                             fill=fill, outline=outline, width=ow)
    return layer.filter(ImageFilter.GaussianBlur(blur)) if blur else layer


def paste(canvas, layer, xy):
    canvas.paste(layer, xy, layer.split()[3])


def wrap(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if font.getlength(test) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_centered(draw, text, font, y, color, shadow=None):
    bbox = font.getbbox(text)
    x = (W - (bbox[2] - bbox[0])) // 2
    if shadow:
        draw.text((x + 2, y + 2), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=color)
    return bbox[3] - bbox[1]


# ── INFOGRAPHIC BUILDER ───────────────────────────────────────────────────────────
def make_infographic(img_path: str, data: dict) -> str:
    title    = data.get("title", "Товар")
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])[:4]
    badge    = data.get("badge", "ХИТ")
    cta      = data.get("cta", "Купить сейчас")
    t        = THEMES.get(data.get("color_theme", "warm"), THEMES["warm"])

    is_dark = data.get("color_theme") == "dark"
    acc, acc2 = t["accent"], t["accent2"]
    WHITE = (255, 255, 255, 255)

    canvas = make_gradient(t)
    draw   = ImageDraw.Draw(canvas)

    for i in range(-H, W + H, 55):
        draw.line([(i, 0), (i + H, H)], fill=(*acc, 14), width=1)
    draw.ellipse([-110, -110, 310, 310], fill=(*acc2, 22))
    draw.ellipse([790, 790, 1190, 1190], fill=(*acc,  18))
    draw.ellipse([810, -60, 1100, 230],  fill=(*acc2, 16))
    draw.ellipse([-60, 810, 250, 1100],  fill=(*acc,  14))

    prod = Image.open(img_path).convert("RGBA")
    pw, ph = prod.size
    sq   = min(pw, ph)
    prod = prod.crop(((pw-sq)//2, (ph-sq)//2, (pw-sq)//2+sq, (ph-sq)//2+sq))
    PS   = 560
    prod = prod.resize((PS, PS), Image.LANCZOS)
    px, py = (W - PS) // 2, (H - PS) // 2 - 30

    sh = Image.new("RGBA", (PS+60, PS+60), (0,0,0,0))
    ImageDraw.Draw(sh).ellipse([20, PS-30, PS+40, PS+55], fill=(0,0,0,50))
    sh = sh.filter(ImageFilter.GaussianBlur(24))
    paste(canvas, sh, (px-30, py-5))
    paste(canvas, rounded_layer(PS+28, PS+28, (PS+28)//2, fill=(*t["card"][:3], 235)), (px-14, py-14))
    ring = Image.new("RGBA", (PS+28, PS+28), (0,0,0,0))
    ImageDraw.Draw(ring).ellipse([0, 0, PS+27, PS+27], outline=(*acc2, 160), width=3)
    paste(canvas, ring, (px-14, py-14))
    mask = Image.new("L", (PS, PS), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, PS, PS], fill=255)
    canvas.paste(prod, (px, py), mask)

    FW, FH, PAD, R = 210, 82, 26, 16
    symbols = ["✓", "★", "⚡", "♦"]
    accent_row = [
        (*acc, 255), (*acc2, 255),
        (*(min(255, int(c*1.12)) for c in acc),  255),
        (*(min(255, int(c*0.88)) for c in acc2), 255),
    ]
    positions = [(PAD, PAD), (W-FW-PAD, PAD), (PAD, H-FH-PAD-148), (W-FW-PAD, H-FH-PAD-148)]
    font_feat = get_font("bold", 20)
    font_sym  = get_font("regular", 22)

    for i, feat in enumerate(features):
        if i >= len(positions): break
        fx, fy = positions[i]
        fa = accent_row[i % len(accent_row)][:3]
        paste(canvas, rounded_layer(FW+10, FH+10, R, (0,0,0,45), blur=9), (fx-3, fy-2))
        card = rounded_layer(FW, FH, R, fill=(*t["card"][:3], 248), outline=(*fa, 170), ow=2)
        cd = ImageDraw.Draw(card)
        cd.rounded_rectangle([0, 0, 7, FH], radius=3, fill=(*fa, 255))
        cd.text((14, 7), symbols[i % len(symbols)], font=font_sym, fill=(*fa, 255))
        lines = wrap(feat, font_feat, FW - 48)
        lh = font_feat.getbbox("А")[3] + 3
        ty = (FH - len(lines) * lh) // 2
        for line in lines[:2]:
            cd.text((42, ty), line, font=font_feat, fill=(*fa, 255))
            ty += lh
        paste(canvas, card, (fx, fy))

    PH, py_b = 158, H - 158
    paste(canvas, rounded_layer(W-36, PH+8, 30, (0,0,0,38), blur=14), (16, py_b-10))
    paste(canvas, rounded_layer(W-36, PH-12, 28, fill=(*t["card"][:3], 245),
                                 outline=(*acc2, 130), ow=2), (18, py_b+4))

    draw = ImageDraw.Draw(canvas)
    font_title = get_font("bold", 46)
    font_sub   = get_font("regular", 27)
    shadow_col = (*(int(c*0.65) for c in t["text"]), 120) if not is_dark else None
    ty = py_b + 18
    for line in wrap(title, font_title, W-110)[:2]:
        h = draw_centered(draw, line, font_title, ty, (*t["text"], 255), shadow=shadow_col)
        ty += h + 5
    if subtitle:
        for line in wrap(subtitle, font_sub, W-150)[:1]:
            draw_centered(draw, line, font_sub, ty, (*acc2, 255))

    font_badge = get_font("bold", 25)
    btxt = f"  {badge}  "
    bw, bh = int(font_badge.getlength(btxt)) + 20, 44
    badge_l = rounded_layer(bw, bh, 12, fill=(215, 48, 48, 255))
    ImageDraw.Draw(badge_l).text((10, (bh-25)//2), btxt, font=font_badge, fill=WHITE)
    paste(canvas, badge_l, (48, 48))

    font_cta = get_font("bold", 23)
    ctxt = f"  {cta}  "
    cw, ch = int(font_cta.getlength(ctxt)) + 28, 44
    cta_l = rounded_layer(cw, ch, 13, fill=(*acc, 245), outline=(*acc2, 255), ow=2)
    ImageDraw.Draw(cta_l).text((14, (ch-23)//2), ctxt, font=font_cta, fill=WHITE)
    paste(canvas, cta_l, (W-cw-48, 46))

    font_wm = get_font("regular", 17)
    wm = "AI Infografika Bot"
    draw.text((W//2 - int(font_wm.getlength(wm))//2, H-21),
              wm, font=font_wm, fill=(*acc, 90))

    out = img_path.rsplit(".", 1)[0] + "_card.png"
    canvas.convert("RGB").save(out, "PNG", quality=95)
    return out


# ── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────────
def plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for plan_id, p in PLANS.items():
        rows.append([InlineKeyboardButton(
            f"{p['emoji']} {p['name']} — {p['description']} | {p['price_rub']} ₽",
            callback_data=f"plan_{plan_id}",
        )])
    return InlineKeyboardMarkup(rows)


def payment_keyboard(plan_id: str) -> InlineKeyboardMarkup:
    p = PLANS[plan_id]
    rows = [
        [InlineKeyboardButton(
            f"💳 Картой {p['price_rub']} ₽",
            callback_data=f"pay_card_{plan_id}",
        )],
        [InlineKeyboardButton(
            f"₮ USDT  {p['price_usdt']}$",
            callback_data=f"pay_crypto_{plan_id}_USDT",
        ),
         InlineKeyboardButton(
            f"💎 TON  {p['price_ton']}",
            callback_data=f"pay_crypto_{plan_id}_TON",
        )],
        [InlineKeyboardButton("← Назад", callback_data="buy")],
    ]
    return InlineKeyboardMarkup(rows)


# ── CRYPTO BOT API ───────────────────────────────────────────────────────────────
async def create_crypto_invoice(asset: str, amount: float, plan_id: str) -> dict | None:
    if not CRYPTO_BOT_TOKEN:
        return None
    p = PLANS[plan_id]
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{CRYPTO_BOT_API}/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            json={
                "asset":       asset,
                "amount":      str(amount),
                "description": f"AI Infografika — {p['name']}: {p['description']}",
                "expires_in":  3600,
            },
            timeout=10,
        )
    data = r.json()
    if data.get("ok"):
        return data["result"]
    logging.warning(f"CryptoBot error: {data}")
    return None


async def check_crypto_invoice(invoice_id: int) -> str:
    """Возвращает 'paid', 'active' или 'expired'."""
    if not CRYPTO_BOT_TOKEN:
        return "unknown"
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{CRYPTO_BOT_API}/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            params={"invoice_ids": str(invoice_id)},
            timeout=10,
        )
    data = r.json()
    if data.get("ok"):
        items = data["result"].get("items", [])
        if items:
            return items[0].get("status", "unknown")
    return "unknown"


# ── APPLY PLAN ───────────────────────────────────────────────────────────────────
def apply_plan(uid: int, plan_id: str):
    u = get_user(uid)
    p = PLANS[plan_id]
    if p["credits"] == -1:
        days = p["duration_days"] or 30
        now  = datetime.now(timezone.utc)
        # Продлить если уже есть активная подписка
        current = u["unlimited_until"]
        base = current if (current and current > now) else now
        u["unlimited_until"] = base + timedelta(days=days)
    else:
        u["credits"] = u.get("credits", 0) + p["credits"]


# ── HANDLERS ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Купить генерации", callback_data="buy"),
        InlineKeyboardButton("ℹ️ Как работает", callback_data="how"),
    ]])
    await update.message.reply_text(
        "👋 *Привет!* Я создаю профессиональные инфографики для маркетплейсов.\n\n"
        "📸 Пришли фото товара — получишь готовую карточку 1080×1080 для WB / OZON!\n\n"
        f"🎁 У тебя *{FREE_CREDITS} бесплатных* генерации.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 *Выбери тариф:*\n\n"
        "🆓 Бесплатно — 3 генерации\n"
        "🚀 Старт — 10 генераций | 490 ₽ / 5 USDT / 50 TON\n"
        "💎 Про — 30 генераций | 990 ₽ / 10 USDT / 100 TON\n"
        "♾️ Безлимит — ∞ генераций/мес | 9 980 ₽ / 100 USDT / 1 000 TON",
        parse_mode="Markdown",
        reply_markup=plans_keyboard(),
    )


async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(credits_display(uid), parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)

    if not has_access(uid):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🛒 Купить генерации", callback_data="buy"),
        ]])
        await update.message.reply_text(
            "😔 Бесплатные генерации закончились. Купи пакет и продолжай:",
            reply_markup=kb,
        )
        return

    msg = await update.message.reply_text("⏳ Анализирую товар через GPT-4o...")
    try:
        photo    = update.message.photo[-1]
        file     = await context.bot.get_file(photo.file_id)
        img_path = f"/tmp/product_{uid}.jpg"
        await file.download_to_drive(img_path)
        await msg.edit_text("🎨 Создаю инфографику...")

        data     = analyze_product_image(img_path)
        out_path = make_infographic(img_path, data)
        consume(uid)

        caption = (
            f"✅ *{data.get('title', '')}*\n"
            f"_{data.get('subtitle', '')}_\n\n"
            + "\n".join(f"• {f}" for f in data.get("features", []))
            + f"\n\n{credits_display(uid)}"
        )
        with open(out_path, "rb") as f:
            await update.message.reply_photo(f, caption=caption, parse_mode="Markdown")

        await msg.delete()
        for p in (img_path, out_path):
            try: os.remove(p)
            except OSError: pass

    except Exception as e:
        logging.exception(e)
        await msg.edit_text(f"❌ Ошибка: {e}")


# ── CALLBACK ROUTER ──────────────────────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    data = q.data

    # ── Главное меню покупки ──
    if data == "buy":
        await q.message.edit_text(
            "🛒 *Выбери тариф:*\n\n"
            "🆓 Бесплатно — 3 генерации\n"
            "🚀 Старт — 10 генераций | 490 ₽ / 5 USDT / 50 TON\n"
            "💎 Про — 30 генераций | 990 ₽ / 10 USDT / 100 TON\n"
            "♾️ Безлимит — ∞ генераций/мес | 9 980 ₽ / 100 USDT / 1 000 TON",
            parse_mode="Markdown",
            reply_markup=plans_keyboard(),
        )
        return

    # ── Выбор тарифа → выбор способа оплаты ──
    if data.startswith("plan_"):
        plan_id = data[5:]
        p = PLANS[plan_id]
        await q.message.edit_text(
            f"{p['emoji']} *{p['name']}* — {p['description']}\n\n"
            f"Выбери способ оплаты:",
            parse_mode="Markdown",
            reply_markup=payment_keyboard(plan_id),
        )
        return

    # ── Оплата картой (YooKassa через Telegram) ──
    if data.startswith("pay_card_"):
        plan_id = data[9:]
        if not YOOKASSA_TOKEN:
            await q.message.reply_text(
                "⚙️ Оплата картой пока настраивается.\n"
                "Напиши @admin для ручной активации."
            )
            return
        p = PLANS[plan_id]
        await context.bot.send_invoice(
            chat_id=uid,
            title=f"AI Infografika — {p['name']}",
            description=p["description"],
            payload=f"plan_{plan_id}",
            provider_token=YOOKASSA_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(p["description"], p["price_rub"] * 100)],
        )
        return

    # ── Оплата криптой (CryptoBot) ──
    if data.startswith("pay_crypto_"):
        _, _, plan_id, asset = data.split("_", 3)
        p = PLANS[plan_id]
        amount = p["price_usdt"] if asset == "USDT" else float(p["price_ton"])

        await q.message.edit_text("⏳ Создаю счёт...")
        invoice = await create_crypto_invoice(asset, amount, plan_id)

        if not invoice:
            await q.message.edit_text(
                "⚙️ Крипто-оплата пока не настроена.\n\n"
                "Чтобы включить: добавь `CRYPTO_BOT_TOKEN` в переменные Railway.\n"
                "Токен получи в Telegram → @CryptoBot → Create App."
            )
            return

        inv_id  = invoice["invoice_id"]
        pay_url = invoice["bot_invoice_url"]

        # Сохраняем pending-инвойс
        get_user(uid)["pending"][str(inv_id)] = plan_id

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💳 Оплатить {amount} {asset}", url=pay_url),
        ], [
            InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_{inv_id}"),
        ]])
        await q.message.edit_text(
            f"💰 Счёт создан!\n\n"
            f"Сумма: *{amount} {asset}*\n"
            f"Тариф: {p['emoji']} {p['name']}\n\n"
            "Нажми кнопку ниже → оплати в CryptoBot → вернись и нажми «Проверить оплату».",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    # ── Проверка оплаты крипты ──
    if data.startswith("check_"):
        inv_id  = int(data[6:])
        plan_id = get_user(uid)["pending"].get(str(inv_id))
        if not plan_id:
            await q.message.reply_text("❌ Счёт не найден.")
            return

        status = await check_crypto_invoice(inv_id)
        if status == "paid":
            apply_plan(uid, plan_id)
            del get_user(uid)["pending"][str(inv_id)]
            p = PLANS[plan_id]
            await q.message.edit_text(
                f"🎉 *Оплата подтверждена!*\n\n"
                f"Тариф {p['emoji']} *{p['name']}* активирован.\n\n"
                f"{credits_display(uid)}\n\nПрисылай фото товаров!",
                parse_mode="Markdown",
            )
        elif status == "expired":
            await q.message.reply_text("⌛ Счёт истёк. Создай новый через /buy.")
        else:
            await q.message.reply_text(
                "⏳ Оплата ещё не поступила. Подожди немного и проверь снова."
            )
        return

    # ── Инфо ──
    if data == "how":
        await q.message.reply_text(
            "📌 *Как работает бот:*\n\n"
            "1. Отправь фото товара\n"
            "2. GPT-4o анализирует и пишет тексты\n"
            "3. Получаешь карточку 1080×1080 для WB/OZON\n\n"
            "🎨 Тема подбирается автоматически.\n"
            "💳 1 фото = 1 генерация.",
            parse_mode="Markdown",
        )


# ── YOOKASSA PAYMENTS (Telegram built-in) ────────────────────────────────────────
async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload.startswith("plan_"):
        plan_id = payload[5:]
        apply_plan(uid, plan_id)
        p = PLANS[plan_id]
        await update.message.reply_text(
            f"🎉 *Оплата прошла!*\n\n"
            f"Тариф {p['emoji']} *{p['name']}* активирован.\n\n"
            f"{credits_display(uid)}\n\nПрисылай фото товаров!",
            parse_mode="Markdown",
        )


# ── MAIN ──────────────────────────────────────────────────────────────────────────
def main():
    download_fonts()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("buy",     cmd_buy))
    app.add_handler(CommandHandler("credits", cmd_credits))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    logging.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
