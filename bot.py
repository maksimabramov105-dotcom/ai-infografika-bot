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
YOOKASSA_TOKEN     = os.getenv("YOOKASSA_TOKEN", "")
CRYPTO_BOT_TOKEN   = os.getenv("CRYPTO_BOT_TOKEN", "")
CRYPTO_BOT_API     = "https://pay.crypt.bot/api"

# Владелец — бесплатный безлимит. Добавь свой Telegram ID в Railway Variables.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

client = OpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

# ── ТАРИФНЫЕ ПЛАНЫ ───────────────────────────────────────────────────────────────
FREE_CREDITS = 3

PLANS = {
    "start": {
        "name": "Старт",
        "emoji": "🚀",
        "credits": 10,
        "duration_days": None,
        "price_rub": 490,
        "price_usdt": 5.0,
        "price_ton": 50,
        "description": "10 карточек",
    },
    "pro": {
        "name": "Про",
        "emoji": "💎",
        "credits": 30,
        "duration_days": None,
        "price_rub": 990,
        "price_usdt": 10.0,
        "price_ton": 100,
        "description": "30 карточек",
    },
    "unlimited": {
        "name": "Безлимит",
        "emoji": "♾️",
        "credits": -1,
        "duration_days": 30,
        "price_rub": 9980,
        "price_usdt": 100.0,
        "price_ton": 1000,
        "description": "Безлимит на 30 дней",
    },
}

# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────────────
user_data: dict[int, dict] = {}


def get_user(uid: int) -> dict:
    if uid not in user_data:
        user_data[uid] = {"credits": FREE_CREDITS, "unlimited_until": None, "pending": {}}
    return user_data[uid]


def has_access(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        return True
    return u["credits"] > 0


def consume(uid: int):
    if uid == OWNER_ID:
        return
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        return
    if u["credits"] > 0:
        u["credits"] -= 1


def credits_display(uid: int) -> str:
    if uid == OWNER_ID:
        return "👑 Владелец — безлимит"
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        until = u["unlimited_until"].strftime("%d.%m.%Y")
        return f"♾️ Безлимит до {until}"
    return f"💡 Осталось карточек: *{u['credits']}*"


# ── FONTS ────────────────────────────────────────────────────────────────────────
FONT_DIR = "/tmp/fonts"
os.makedirs(FONT_DIR, exist_ok=True)

_FONT_SOURCES = [
    ("bold",    f"{FONT_DIR}/NotoSans-Bold.ttf",
     "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf"),
    ("regular", f"{FONT_DIR}/NotoSans-Regular.ttf",
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
    ],
}


def download_fonts():
    for _, path, url in _FONT_SOURCES:
        if not os.path.exists(path):
            try:
                urllib.request.urlretrieve(url, path)
                logging.info(f"Font downloaded: {path}")
            except Exception as e:
                logging.warning(f"Font download failed: {e}")


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
  "title": "ЗАГОЛОВОК КРУПНО (макс 28 символов, как на WB — КАПСЛОК или смешанный)",
  "subtitle": "Подзаголовок / УТП (макс 40 символов)",
  "features": [
    "Короткое преимущество 1 (макс 22 символа)",
    "Короткое преимущество 2 (макс 22 символа)",
    "Короткое преимущество 3 (макс 22 символа)",
    "Короткое преимущество 4 (макс 22 символа)"
  ],
  "badge": "ХИТ | НОВИНКА | -20% | БЕСТСЕЛЛЕР | ТОП",
  "cta": "Купить сейчас | В корзину | Заказать",
  "color_theme": "warm | cool | neutral | dark"
}

Правила:
- Пиши по-русски
- Преимущества — конкретные, с цифрами (напр: "Горит 25 часов", "100% кокос. воск")
- color_theme: warm=еда/beauty/дом/свечи, cool=техника/спорт, neutral=одежда, dark=люкс
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        max_tokens=400,
    )
    raw = re.sub(r"```json|```", "", response.choices[0].message.content.strip()).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {
            "title": "Товар", "subtitle": "Отличное качество",
            "features": ["Натуральный состав", "Горит 25 часов", "Быстрая доставка", "Гарантия"],
            "badge": "ХИТ", "cta": "Купить сейчас", "color_theme": "warm",
        }


# ── COLOR THEMES ─────────────────────────────────────────────────────────────────
THEMES = {
    "warm":    {"bg": (252, 246, 237), "bg2": (255, 232, 205),
                "accent": (180, 75, 20), "accent2": (230, 135, 45),
                "card": (255, 255, 255), "text": (30, 20, 10), "text2": (130, 80, 40)},
    "cool":    {"bg": (240, 246, 255), "bg2": (210, 228, 255),
                "accent": (30, 80, 190), "accent2": (60, 120, 230),
                "card": (255, 255, 255), "text": (15, 25, 60), "text2": (50, 90, 170)},
    "neutral": {"bg": (250, 250, 250), "bg2": (232, 232, 232),
                "accent": (50, 50, 50),  "accent2": (110, 110, 110),
                "card": (255, 255, 255), "text": (25, 25, 25), "text2": (90, 90, 90)},
    "dark":    {"bg": (22, 22, 32),    "bg2": (14, 14, 24),
                "accent": (205, 160, 70), "accent2": (240, 200, 100),
                "card": (38, 38, 50),   "text": (242, 238, 225), "text2": (185, 170, 135)},
}

W, H = 1080, 1080


def make_gradient(bg, bg2) -> Image.Image:
    t    = np.linspace(0, 1, H)[:, np.newaxis]
    rows = (np.array(bg, np.float32) * (1-t) + np.array(bg2, np.float32) * t).astype(np.uint8)
    return Image.fromarray(np.broadcast_to(rows[:, np.newaxis, :], (H, W, 3)).copy(), "RGB").convert("RGBA")


def paste_a(canvas, layer, xy):
    canvas.paste(layer, xy, layer.split()[3])


def rlayer(w, h, r, fill, outline=None, ow=2, blur=0):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle([0, 0, w, h], radius=r,
                                           fill=fill, outline=outline, width=ow)
    return img.filter(ImageFilter.GaussianBlur(blur)) if blur else img


def wrap(text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if font.getlength(test) <= max_w:
            cur = test
        else:
            if cur: lines.append(cur)
            cur = word
    if cur: lines.append(cur)
    return lines


def centered_x(text, font):
    bbox = font.getbbox(text)
    return (W - (bbox[2] - bbox[0])) // 2


def text_h(font):
    b = font.getbbox("АйЯ")
    return b[3] - b[1]


# ── INFOGRAPHIC — новый дизайн (как у конкурентов) ───────────────────────────────
def make_infographic(img_path: str, data: dict) -> str:
    title    = data.get("title", "Товар").upper()
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])[:4]
    badge    = data.get("badge", "ХИТ")
    cta      = data.get("cta", "Купить сейчас")
    t        = THEMES.get(data.get("color_theme", "warm"), THEMES["warm"])

    is_dark = data.get("color_theme") == "dark"
    acc, acc2 = t["accent"], t["accent2"]
    WHITE = (255, 255, 255, 255)
    CARD  = (*t["card"][:3], 255)

    # ── Фон ──────────────────────────────────────────────────────────
    canvas = make_gradient(t["bg"], t["bg2"])
    draw   = ImageDraw.Draw(canvas)

    # Мягкий декоративный круг в углу
    draw.ellipse([-140, -140, 340, 340], fill=(*acc2, 18))
    draw.ellipse([780, 720, 1220, 1220], fill=(*acc,  14))

    # ── Шрифты ───────────────────────────────────────────────────────
    f_title  = get_font("bold",    64)
    f_title2 = get_font("bold",    52)
    f_sub    = get_font("regular", 32)
    f_feat   = get_font("bold",    24)
    f_feat2  = get_font("regular", 22)
    f_badge  = get_font("bold",    26)
    f_cta    = get_font("bold",    26)
    f_wm     = get_font("regular", 18)

    # ── Заголовок (сверху) ────────────────────────────────────────────
    # Пробуем большой шрифт, если не влезает — уменьшаем
    title_lines = wrap(title, f_title, W - 80)
    if len(title_lines) > 2:
        f_title_use = f_title2
        title_lines = wrap(title, f_title2, W - 80)
    else:
        f_title_use = f_title

    ty = 48
    for line in title_lines[:2]:
        x = centered_x(line, f_title_use)
        if not is_dark:
            draw.text((x+2, ty+2), line, font=f_title_use, fill=(*acc, 60))
        draw.text((x, ty), line, font=f_title_use, fill=(*t["text"], 255))
        ty += text_h(f_title_use) + 8

    # Подзаголовок
    if subtitle:
        sub_lines = wrap(subtitle, f_sub, W - 120)
        for line in sub_lines[:2]:
            x = centered_x(line, f_sub)
            draw.text((x, ty), line, font=f_sub, fill=(*acc2, 255))
            ty += text_h(f_sub) + 4
    ty += 12

    # ── Фото товара (центр) ───────────────────────────────────────────
    # Зона для фото: от ty до ~ty+480
    PHOTO_SIZE = min(480, H - ty - 280)
    prod = Image.open(img_path).convert("RGBA")
    pw, ph = prod.size
    sq = min(pw, ph)
    prod = prod.crop(((pw-sq)//2, (ph-sq)//2, (pw-sq)//2+sq, (ph-sq)//2+sq))
    prod = prod.resize((PHOTO_SIZE, PHOTO_SIZE), Image.LANCZOS)

    px = (W - PHOTO_SIZE) // 2
    py = ty

    # Тень
    sh = Image.new("RGBA", (PHOTO_SIZE+50, PHOTO_SIZE+50), (0,0,0,0))
    ImageDraw.Draw(sh).rounded_rectangle([10, 10, PHOTO_SIZE+40, PHOTO_SIZE+40],
                                          radius=28, fill=(0,0,0,55))
    sh = sh.filter(ImageFilter.GaussianBlur(20))
    paste_a(canvas, sh, (px-25, py-5))

    # Белая подложка
    paste_a(canvas, rlayer(PHOTO_SIZE+20, PHOTO_SIZE+20, 24,
                            fill=(*t["card"][:3], 245)), (px-10, py-10))

    # Фото с маской (скруглённый квадрат)
    mask = Image.new("L", (PHOTO_SIZE, PHOTO_SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, PHOTO_SIZE, PHOTO_SIZE], radius=20, fill=255)
    canvas.paste(prod, (px, py), mask)

    feat_top = py + PHOTO_SIZE + 22

    # ── 4 блока с преимуществами (2×2 сетка внизу) ───────────────────
    # Каждый блок занимает половину ширины минус отступы
    COL_W   = (W - 60) // 2   # ~510
    ROW_H   = 90
    GAP     = 14
    LEFT_X  = 22
    RIGHT_X = LEFT_X + COL_W + GAP

    ICON_SYMBOLS = ["★", "✓", "⚡", "♦"]
    ICON_COLORS  = [acc, acc2,
                    tuple(min(255, int(c*1.1)) for c in acc),
                    tuple(min(255, int(c*0.9)) for c in acc2)]

    positions = [
        (LEFT_X,  feat_top),
        (RIGHT_X, feat_top),
        (LEFT_X,  feat_top + ROW_H + GAP),
        (RIGHT_X, feat_top + ROW_H + GAP),
    ]

    for i, feat in enumerate(features[:4]):
        if i >= len(positions): break
        fx, fy = positions[i]
        ic = ICON_COLORS[i % len(ICON_COLORS)]

        # Карточка
        paste_a(canvas, rlayer(COL_W+4, ROW_H+4, 18, (0,0,0,40), blur=8), (fx-2, fy))
        card = rlayer(COL_W, ROW_H, 18,
                       fill=(*t["card"][:3], 250),
                       outline=(*ic, 120), ow=2)
        cd = ImageDraw.Draw(card)

        # Цветная полоска слева
        cd.rounded_rectangle([0, 0, 8, ROW_H], radius=4, fill=(*ic, 255))

        # Иконка в цветном кружке
        icon_size = 52
        icon_layer = rlayer(icon_size, icon_size, icon_size//2, fill=(*ic, 30))
        icd = ImageDraw.Draw(icon_layer)
        sym = ICON_SYMBOLS[i % len(ICON_SYMBOLS)]
        sb  = f_feat.getbbox(sym)
        sw, sh_sym = sb[2]-sb[0], sb[3]-sb[1]
        icd.text(((icon_size-sw)//2, (icon_size-sh_sym)//2 - 2), sym, font=f_feat, fill=(*ic, 255))
        card.paste(icon_layer, (14, (ROW_H-icon_size)//2), icon_layer.split()[3])

        # Текст
        TEXT_X  = 14 + icon_size + 14
        max_txt = COL_W - TEXT_X - 12
        lines   = wrap(feat, f_feat, max_txt)
        lh      = text_h(f_feat) + 4
        total_h = len(lines[:2]) * lh
        tty     = (ROW_H - total_h) // 2

        for line in lines[:2]:
            cd.text((TEXT_X, tty), line, font=f_feat, fill=(*ic, 255))
            tty += lh

        paste_a(canvas, card, (fx, fy))

    # ── Бейдж (верх лево) ────────────────────────────────────────────
    btxt = f"  {badge}  "
    bw   = int(f_badge.getlength(btxt)) + 18
    bh   = 46
    bl   = rlayer(bw, bh, 12, fill=(210, 45, 45, 255))
    ImageDraw.Draw(bl).text((9, (bh-26)//2), btxt, font=f_badge, fill=WHITE)
    paste_a(canvas, bl, (32, 32))

    # ── CTA кнопка (верх право) ──────────────────────────────────────
    ctxt = f"  {cta}  "
    cw   = int(f_cta.getlength(ctxt)) + 22
    ch   = 46
    cl   = rlayer(cw, ch, 12, fill=(*acc, 245), outline=(*acc2, 200), ow=2)
    ImageDraw.Draw(cl).text((11, (ch-26)//2), ctxt, font=f_cta, fill=WHITE)
    paste_a(canvas, cl, (W - cw - 32, 32))

    # ── Вотермарк снизу ──────────────────────────────────────────────
    wm = "AI Infografika Bot"
    draw = ImageDraw.Draw(canvas)
    draw.text((W//2 - int(f_wm.getlength(wm))//2, H - 26),
              wm, font=f_wm, fill=(*acc, 80))

    out = img_path.rsplit(".", 1)[0] + "_card.png"
    canvas.convert("RGB").save(out, "PNG", quality=95)
    return out


# ── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────────
def plans_keyboard():
    rows = []
    for pid, p in PLANS.items():
        rows.append([InlineKeyboardButton(
            f"{p['emoji']} {p['name']} — {p['description']} | {p['price_rub']} ₽",
            callback_data=f"plan_{pid}",
        )])
    return InlineKeyboardMarkup(rows)


def payment_keyboard(plan_id: str):
    p = PLANS[plan_id]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Картой  {p['price_rub']} ₽", callback_data=f"pay_card_{plan_id}")],
        [InlineKeyboardButton(f"₮ USDT  {p['price_usdt']}$",  callback_data=f"pay_crypto_{plan_id}_USDT"),
         InlineKeyboardButton(f"💎 TON  {p['price_ton']}",    callback_data=f"pay_crypto_{plan_id}_TON")],
        [InlineKeyboardButton("← Назад", callback_data="buy")],
    ])


# ── CRYPTO BOT ───────────────────────────────────────────────────────────────────
async def create_crypto_invoice(asset, amount, plan_id) -> dict | None:
    if not CRYPTO_BOT_TOKEN:
        return None
    p = PLANS[plan_id]
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{CRYPTO_BOT_API}/createInvoice",
                         headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
                         json={"asset": asset, "amount": str(amount),
                               "description": f"AI Infografika — {p['name']}: {p['description']}",
                               "expires_in": 3600},
                         timeout=10)
    d = r.json()
    return d["result"] if d.get("ok") else None


async def check_crypto_invoice(invoice_id: int) -> str:
    if not CRYPTO_BOT_TOKEN:
        return "unknown"
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{CRYPTO_BOT_API}/getInvoices",
                        headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
                        params={"invoice_ids": str(invoice_id)}, timeout=10)
    d = r.json()
    if d.get("ok"):
        items = d["result"].get("items", [])
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
        base = u["unlimited_until"] if (u["unlimited_until"] and u["unlimited_until"] > now) else now
        u["unlimited_until"] = base + timedelta(days=days)
    else:
        u["credits"] = u.get("credits", 0) + p["credits"]


# ── HANDLERS ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛒 Купить карточки", callback_data="buy"),
        InlineKeyboardButton("ℹ️ Как работает",   callback_data="how"),
    ]])
    await update.message.reply_text(
        "👋 *Привет!* Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 Пришли фото товара — получишь карточку 1080×1080 для WB / OZON!\n\n"
        f"🎁 У тебя *{FREE_CREDITS} бесплатных* карточки.",
        parse_mode="Markdown", reply_markup=kb,
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 *Выбери тариф:*\n\n"
        "🆓 Бесплатно — 3 карточки\n"
        "🚀 Старт — 10 карточек | 490 ₽ / 5 USDT / 50 TON\n"
        "💎 Про — 30 карточек | 990 ₽ / 10 USDT / 100 TON\n"
        "♾️ Безлимит — ∞ карточек/мес | 9 980 ₽ / 100 USDT / 1 000 TON",
        parse_mode="Markdown", reply_markup=plans_keyboard(),
    )


async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(credits_display(update.effective_user.id), parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    if not has_access(uid):
        await update.message.reply_text(
            "😔 Карточки закончились. Купи пакет и продолжай:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Купить карточки", callback_data="buy"),
            ]]),
        )
        return

    msg = await update.message.reply_text("⏳ Анализирую товар через GPT-4o...")
    try:
        photo    = update.message.photo[-1]
        file     = await context.bot.get_file(photo.file_id)
        img_path = f"/tmp/product_{uid}.jpg"
        await file.download_to_drive(img_path)
        await msg.edit_text("🎨 Создаю карточку...")

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


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    d   = q.data

    if d == "buy":
        await q.message.edit_text(
            "🛒 *Выбери тариф:*\n\n"
            "🆓 Бесплатно — 3 карточки\n"
            "🚀 Старт — 10 карточек | 490 ₽ / 5 USDT\n"
            "💎 Про — 30 карточек | 990 ₽ / 10 USDT\n"
            "♾️ Безлимит — ∞ карточек/мес | 9 980 ₽ / 100 USDT",
            parse_mode="Markdown", reply_markup=plans_keyboard(),
        )
        return

    if d.startswith("plan_"):
        pid = d[5:]
        p   = PLANS[pid]
        await q.message.edit_text(
            f"{p['emoji']} *{p['name']}* — {p['description']}\n\nВыбери способ оплаты:",
            parse_mode="Markdown", reply_markup=payment_keyboard(pid),
        )
        return

    if d.startswith("pay_card_"):
        pid = d[9:]
        if not YOOKASSA_TOKEN:
            await q.message.reply_text(
                "⚙️ Оплата картой подключается. Напиши @admin для активации вручную."
            )
            return
        p = PLANS[pid]
        await context.bot.send_invoice(
            chat_id=uid, title=f"AI Infografika — {p['name']}",
            description=p["description"], payload=f"plan_{pid}",
            provider_token=YOOKASSA_TOKEN, currency="RUB",
            prices=[LabeledPrice(p["description"], p["price_rub"] * 100)],
        )
        return

    if d.startswith("pay_crypto_"):
        _, _, pid, asset = d.split("_", 3)
        p      = PLANS[pid]
        amount = p["price_usdt"] if asset == "USDT" else float(p["price_ton"])
        await q.message.edit_text("⏳ Создаю счёт...")
        invoice = await create_crypto_invoice(asset, amount, pid)
        if not invoice:
            await q.message.edit_text(
                "⚙️ Крипто-оплата не настроена.\n\n"
                "Добавь `CRYPTO_BOT_TOKEN` в Railway Variables.\n"
                "Токен: Telegram → @CryptoBot → Create App."
            )
            return
        inv_id  = invoice["invoice_id"]
        pay_url = invoice["bot_invoice_url"]
        get_user(uid)["pending"][str(inv_id)] = pid
        await q.message.edit_text(
            f"💰 *Счёт создан!*\n\nСумма: *{amount} {asset}*\n"
            f"Тариф: {p['emoji']} {p['name']}\n\n"
            "Оплати → вернись → нажми «Проверить оплату».",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Оплатить {amount} {asset}", url=pay_url)],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_{inv_id}")],
            ]),
        )
        return

    if d.startswith("check_"):
        inv_id  = int(d[6:])
        pid     = get_user(uid)["pending"].get(str(inv_id))
        if not pid:
            await q.message.reply_text("❌ Счёт не найден.")
            return
        status = await check_crypto_invoice(inv_id)
        if status == "paid":
            apply_plan(uid, pid)
            del get_user(uid)["pending"][str(inv_id)]
            p = PLANS[pid]
            await q.message.edit_text(
                f"🎉 *Оплата подтверждена!*\n\n{p['emoji']} *{p['name']}* активирован.\n\n"
                f"{credits_display(uid)}\n\nПрисылай фото товаров!",
                parse_mode="Markdown",
            )
        elif status == "expired":
            await q.message.reply_text("⌛ Счёт истёк. Создай новый через /buy.")
        else:
            await q.message.reply_text("⏳ Оплата ещё не поступила. Подожди и проверь снова.")
        return

    if d == "how":
        await q.message.reply_text(
            "📌 *Как работает бот:*\n\n"
            "1. Отправь фото товара\n"
            "2. GPT-4o анализирует и пишет тексты\n"
            "3. Получаешь карточку 1080×1080 для WB/OZON\n\n"
            "🎨 Тема подбирается автоматически.\n"
            "💳 1 фото = 1 карточка.",
            parse_mode="Markdown",
        )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload.startswith("plan_"):
        apply_plan(uid, payload[5:])
        p = PLANS[payload[5:]]
        await update.message.reply_text(
            f"🎉 *Оплата прошла!* {p['emoji']} *{p['name']}* активирован.\n\n"
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
