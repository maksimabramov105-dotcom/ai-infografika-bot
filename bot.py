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
from telegram import (
    Update, LabeledPrice,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    PreCheckoutQueryHandler, CallbackQueryHandler, ContextTypes, filters,
)
from openai import OpenAI, RateLimitError, AuthenticationError, APIConnectionError

# ── CONFIG ───────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8728265878:AAH7pdsOpSO1x4eDrnY9pKaFA5IYS7DlU6E")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "sk-ВСТАВЬ_КЛЮЧ")
YOOKASSA_TOKEN     = os.getenv("YOOKASSA_TOKEN", "")
CRYPTO_BOT_TOKEN   = os.getenv("CRYPTO_BOT_TOKEN", "")
CRYPTO_BOT_API     = "https://pay.crypt.bot/api"
OWNER_ID           = int(os.getenv("OWNER_ID", "0"))

client = OpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

# ── ПРОМОКОДЫ ────────────────────────────────────────────────────────────────────
# Формат: "КОД": {"credits": N, "used_by": set()}
# credits=-1 → добавить к безлимиту на 7 дней
PROMO_CODES: dict[str, dict] = {
    "SORRY5":   {"credits": 5,  "used_by": set()},   # для рассылки при сбоях
    "WELCOME3": {"credits": 3,  "used_by": set()},   # приветственный
}

# ── ТАРИФНЫЕ ПЛАНЫ ───────────────────────────────────────────────────────────────
FREE_CREDITS = 3

PLANS = {
    "start": {
        "name": "Старт", "emoji": "🚀",
        "credits": 10, "duration_days": None,
        "price_rub": 490, "price_usdt": 5.0, "price_ton": 50,
        "description": "10 карточек",
    },
    "pro": {
        "name": "Про", "emoji": "💎",
        "credits": 30, "duration_days": None,
        "price_rub": 990, "price_usdt": 10.0, "price_ton": 100,
        "description": "30 карточек",
    },
    "unlimited": {
        "name": "Безлимит", "emoji": "♾️",
        "credits": -1, "duration_days": 30,
        "price_rub": 9980, "price_usdt": 100.0, "price_ton": 1000,
        "description": "Безлимит на 30 дней",
    },
}

# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────────────
user_data: dict[int, dict] = {}

# Счётчик ошибок для авто-оповещения владельца
_error_counts: dict[str, int] = {}


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


# ── ПРОМОКОД ЛОГИКА ──────────────────────────────────────────────────────────────
def apply_promo(uid: int, code: str) -> tuple[bool, str]:
    """Возвращает (успех, сообщение)"""
    code = code.strip().upper()
    if code not in PROMO_CODES:
        return False, "❌ Промокод не найден. Проверь правильность написания."
    promo = PROMO_CODES[code]
    if uid in promo["used_by"]:
        return False, "⚠️ Ты уже активировал этот промокод."
    promo["used_by"].add(uid)
    credits = promo["credits"]
    get_user(uid)["credits"] = get_user(uid).get("credits", 0) + credits
    return True, (
        f"🎉 Промокод *{code}* активирован!\n"
        f"Добавлено *{credits} карточек*.\n\n"
        f"{credits_display(uid)}"
    )


# ── УВЕДОМЛЕНИЕ ВЛАДЕЛЬЦА ─────────────────────────────────────────────────────────
async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    if OWNER_ID:
        try:
            await context.bot.send_message(OWNER_ID, f"⚠️ *Системное уведомление*\n\n{text}",
                                            parse_mode="Markdown")
        except Exception:
            pass


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
  "color_theme": "warm | cool | neutral | dark",
  "scene_description": "Детальное описание красивой фоновой сцены для товара на английском. Например для свечи с ароматом апельсина и корицы: 'cozy dark background with soft bokeh lights, orange slices, cinnamon sticks, dried flowers, warm golden tones, luxury lifestyle product photography'. Описание должно включать элементы которые ассоциируются с товаром (ингредиенты, назначение, стиль жизни). НЕ включай сам товар в описание сцены — только окружение и декор."
}

Правила:
- Пиши по-русски (кроме scene_description — оно на АНГЛИЙСКОМ)
- Преимущества — конкретные, с цифрами (напр: "Горит 25 часов", "100% кокос. воск")
- color_theme: warm=еда/beauty/дом/свечи, cool=техника/спорт, neutral=одежда, dark=люкс
- scene_description — ВСЕГДА на английском, максимально детально, для генерации фонового изображения
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        max_tokens=500,
    )
    raw = re.sub(r"```json|```", "", response.choices[0].message.content.strip()).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {
            "title": "Товар", "subtitle": "Отличное качество",
            "features": ["Натуральный состав", "Быстрая доставка", "Высокое качество", "Гарантия"],
            "badge": "ХИТ", "color_theme": "warm",
            "scene_description": "elegant dark background with soft bokeh lights, luxury product photography, warm tones",
        }


# ── DALL-E 3: генерация красивого фона ────────────────────────────────────────────
def generate_scene_background(scene_desc: str) -> str | None:
    """Генерирует фоновую сцену 1024x1024 через DALL-E 3 и возвращает путь к файлу."""
    prompt = (
        f"Beautiful product photography background scene, no text, no product, no labels, "
        f"no words, square format 1:1, professional studio quality, shallow depth of field: "
        f"{scene_desc}"
    )
    try:
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        image_url = response.data[0].url
        bg_path = "/tmp/scene_bg.png"
        urllib.request.urlretrieve(image_url, bg_path)
        return bg_path
    except Exception as e:
        logging.warning(f"DALL-E scene generation failed: {e}")
        return None


# ── COLOR THEMES ─────────────────────────────────────────────────────────────────
# overlay_rgb = цвет оверлея поверх размытого фото
# overlay_a   = прозрачность оверлея (0=прозрачный фон, 255=полностью закрашен)
# title_color = цвет заголовка
# sub_color   = цвет подзаголовка
# feat_bg     = фон карточки преимуществ
# feat_text   = цвет текста в карточках
# acc         = акцент (бейдж, полоска, номер)
THEMES = {
    "warm": {
        "overlay_rgb": (60, 28, 5),   "overlay_a": 165,
        "title_color": (255, 248, 235), "sub_color": (255, 190, 100),
        "feat_bg": (255, 255, 255, 200), "feat_text": (40, 20, 5),
        "acc": (210, 90, 20), "acc2": (240, 150, 50),
    },
    "cool": {
        "overlay_rgb": (8, 25, 65),    "overlay_a": 170,
        "title_color": (230, 240, 255), "sub_color": (120, 180, 255),
        "feat_bg": (255, 255, 255, 200), "feat_text": (10, 25, 60),
        "acc": (40, 100, 220), "acc2": (80, 150, 255),
    },
    "neutral": {
        "overlay_rgb": (25, 25, 25),   "overlay_a": 155,
        "title_color": (250, 250, 250), "sub_color": (190, 190, 190),
        "feat_bg": (255, 255, 255, 200), "feat_text": (20, 20, 20),
        "acc": (80, 80, 80), "acc2": (140, 140, 140),
    },
    "dark": {
        "overlay_rgb": (12, 10, 20),   "overlay_a": 180,
        "title_color": (245, 225, 165), "sub_color": (200, 170, 100),
        "feat_bg": (30, 25, 45, 220),  "feat_text": (240, 230, 200),
        "acc": (210, 165, 65), "acc2": (245, 205, 100),
    },
}

W, H = 1080, 1080
MARGIN = 44


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


def draw_text_shadow(draw, xy, text, font, fill, shadow_alpha=100):
    x, y = xy
    draw.text((x + 3, y + 3), text, font=font, fill=(0, 0, 0, shadow_alpha))
    draw.text((x, y), text, font=font, fill=fill)


# ── INFOGRAPHIC — DALL-E фон + текст поверх ─────────────────────────────────────
def make_infographic(img_path: str, data: dict, scene_bg_path: str | None = None) -> str:
    title    = data.get("title", "Товар").upper()
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])[:4]
    badge    = data.get("badge", "ХИТ")
    t        = THEMES.get(data.get("color_theme", "warm"), THEMES["warm"])

    acc  = t["acc"]
    acc2 = t["acc2"]
    tc   = (*t["title_color"], 255)
    sc   = (*t["sub_color"], 230)
    WHITE = (255, 255, 255, 255)

    # ── ФОН: DALL-E сгенерированная сцена, или fallback на размытое фото ────────
    if scene_bg_path and os.path.exists(scene_bg_path):
        bg_img = Image.open(scene_bg_path).convert("RGB").resize((W, H), Image.LANCZOS)
    else:
        raw = Image.open(img_path).convert("RGB")
        rw, rh = raw.size
        sq = min(rw, rh)
        bg_img = raw.crop(((rw-sq)//2, (rh-sq)//2, (rw-sq)//2+sq, (rh-sq)//2+sq))
        bg_img = bg_img.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(45))

    canvas = bg_img.convert("RGBA")

    # Затемнение сверху и снизу — для читаемости текста
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # Сверху — градиент от тёмного к прозрачному
    for i in range(450):
        a = int(180 * (1 - i / 450) ** 1.5)
        od.line([(0, i), (W, i)], fill=(0, 0, 0, a))
    # Снизу — градиент от прозрачного к тёмному
    for i in range(500):
        a = int(200 * (1 - i / 500) ** 1.3)
        od.line([(0, H - 1 - i), (W, H - 1 - i)], fill=(0, 0, 0, a))
    canvas = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(canvas)

    # ── ЗАГОЛОВОК — огромный, сверху ────────────────────────────────────────────
    title_max_w = W - MARGIN * 2
    f_title_use = None
    title_lines = []
    for size in (96, 84, 72, 60, 50):
        f_t = get_font("bold", size)
        lines = wrap(title, f_t, title_max_w)
        if len(lines) <= 3:
            f_title_use = f_t
            title_lines = lines
            break
    if not f_title_use:
        f_title_use = get_font("bold", 50)
        title_lines = wrap(title, f_title_use, title_max_w)

    ty = 55
    th = text_h(f_title_use)
    for line in title_lines[:3]:
        x = centered_x(line, f_title_use)
        draw_text_shadow(draw, (x, ty), line, f_title_use, tc, shadow_alpha=200)
        ty += th + 8
    ty += 10

    # Подзаголовок
    f_sub = get_font("regular", 36)
    if subtitle:
        for line in wrap(subtitle, f_sub, title_max_w)[:2]:
            x = centered_x(line, f_sub)
            draw_text_shadow(draw, (x, ty), line, f_sub, sc, shadow_alpha=150)
            ty += text_h(f_sub) + 6

    # ── ПРЕИМУЩЕСТВА — внизу поверх затемнения ──────────────────────────────────
    f_feat = get_font("bold", 32)
    f_feat_sm = get_font("regular", 28)
    feat_line_h = text_h(f_feat) + 14
    total_feat_h = len(features) * feat_line_h + 20

    # Полупрозрачная тёмная подложка снизу
    feat_y_start = H - total_feat_h - 30
    feat_bg = Image.new("RGBA", (W, total_feat_h + 50), (0, 0, 0, 0))
    ImageDraw.Draw(feat_bg).rounded_rectangle(
        [MARGIN - 10, 0, W - MARGIN + 10, total_feat_h + 50],
        radius=24, fill=(0, 0, 0, 90),
    )
    paste_a(canvas, feat_bg, (0, feat_y_start - 10))

    fy = feat_y_start
    for i, feat in enumerate(features[:4]):
        # Кружок с номером
        cx = MARGIN + 22
        cy = fy + feat_line_h // 2
        draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], fill=(*acc, 230))
        f_num = get_font("bold", 20)
        ns = str(i + 1)
        nb = f_num.getbbox(ns)
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        draw.text((cx - nw // 2, cy - nh // 2 - 1), ns, font=f_num, fill=WHITE)
        # Текст
        draw_text_shadow(draw, (cx + 28, cy - text_h(f_feat) // 2),
                         feat, f_feat, WHITE, shadow_alpha=180)
        fy += feat_line_h

    # ── БЕЙДЖ (верх-лево) ─────────────────────────────────────────────────────────
    f_badge = get_font("bold", 26)
    btxt = f"  {badge}  "
    bw = int(f_badge.getlength(btxt)) + 16
    bh = 44
    bl = rlayer(bw, bh, 11, fill=(205, 35, 35, 240))
    ImageDraw.Draw(bl).text((8, (bh - 26) // 2), btxt, font=f_badge, fill=WHITE)
    paste_a(canvas, bl, (MARGIN, 18))

    out = img_path.rsplit(".", 1)[0] + "_card.png"
    canvas.convert("RGB").save(out, "PNG", quality=95)
    return out


# ── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────────
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню снизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("📸 Сгенерировать карточку"), KeyboardButton("💡 Мои карточки")],
            [KeyboardButton("🛒 Купить"),                 KeyboardButton("🎁 Промокод")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Отправь фото товара или выбери действие...",
    )


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
        [InlineKeyboardButton(f"💳 Картой  {p['price_rub']} ₽",   callback_data=f"pay_card_{plan_id}")],
        [InlineKeyboardButton(f"₮ USDT  {p['price_usdt']}$",      callback_data=f"pay_crypto_{plan_id}_USDT"),
         InlineKeyboardButton(f"💎 TON  {p['price_ton']}",        callback_data=f"pay_crypto_{plan_id}_TON")],
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


# ── ДРУЖЕЛЮБНЫЕ СООБЩЕНИЯ ОБ ОШИБКАХ ─────────────────────────────────────────────
def user_error_message(e: Exception) -> str:
    if isinstance(e, RateLimitError):
        return (
            "⚠️ *Сервис временно перегружен* — мы уже знаем об этом и чиним.\n\n"
            "Попробуй через 5–10 минут. В качестве извинения введи промокод "
            "*SORRY5* командой /promo и получи 5 карточек бесплатно! 🎁"
        )
    if isinstance(e, AuthenticationError):
        return (
            "🔧 *Технические работы* — сервис временно недоступен.\n\n"
            "Мы уже чиним! Попробуй позже, а пока держи промокод "
            "*SORRY5* — /promo для 5 бесплатных карточек."
        )
    if isinstance(e, APIConnectionError):
        return (
            "📡 *Нет связи с сервером* — проверяем соединение.\n\n"
            "Попробуй ещё раз через пару минут."
        )
    return (
        "😔 *Не удалось создать карточку* — что-то пошло не так на нашей стороне.\n\n"
        "Попробуй отправить фото ещё раз. Если ошибка повторяется — "
        "напиши нам, мы разберёмся!"
    )


def is_critical_error(e: Exception) -> bool:
    return isinstance(e, (RateLimitError, AuthenticationError))


# ── HANDLERS ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    await update.message.reply_text(
        "👋 *Привет!* Я создаю профессиональные карточки товаров для маркетплейсов.\n\n"
        "📸 Пришли фото товара — получишь карточку 1080×1080 для WB / OZON!\n\n"
        f"🎁 У тебя *{FREE_CREDITS} бесплатных* карточки.\n\n"
        "Выбери действие в меню ниже ↓",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 *Выбери тариф:*\n\n"
        "🆓 Бесплатно — 3 карточки\n"
        "🚀 Старт — 10 карточек | 490 ₽ / 5 USDT\n"
        "💎 Про — 30 карточек | 990 ₽ / 10 USDT\n"
        "♾️ Безлимит — ∞ карточек/мес | 9 980 ₽ / 100 USDT",
        parse_mode="Markdown",
        reply_markup=plans_keyboard(),
    )


async def cmd_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(credits_display(update.effective_user.id), parse_mode="Markdown")


async def cmd_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда: /promo КОД  или кнопка 🎁 Промокод"""
    args = context.args
    if not args:
        await update.message.reply_text(
            "🎁 *Промокод*\n\n"
            "Введи команду в формате:\n`/promo ТВОЙКОД`\n\n"
            "Пример: `/promo SORRY5`",
            parse_mode="Markdown",
        )
        return
    code = args[0]
    ok, msg = apply_promo(update.effective_user.id, code)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_admin_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для владельца: /addpromo КОД КОЛИЧЕСТВО"""
    if update.effective_user.id != OWNER_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /addpromo КОД КОЛИЧЕСТВО\nПример: /addpromo PROMO10 10")
        return
    code    = args[0].upper()
    try:
        credits = int(args[1])
    except ValueError:
        await update.message.reply_text("Количество должно быть числом.")
        return
    PROMO_CODES[code] = {"credits": credits, "used_by": set()}
    await update.message.reply_text(f"✅ Промокод *{code}* на *{credits} карточек* создан.", parse_mode="Markdown")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для владельца: /broadcast ТЕКСТ — рассылка всем пользователям."""
    if update.effective_user.id != OWNER_ID:
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Использование: /broadcast ТЕКСТ СООБЩЕНИЯ")
        return
    sent, failed = 0, 0
    for uid in list(user_data.keys()):
        try:
            await context.bot.send_message(uid, text, parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📨 Отправлено: {sent}, не доставлено: {failed}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user(uid)
    if not has_access(uid):
        await update.message.reply_text(
            "😔 *Карточки закончились.*\n\nКупи пакет или активируй промокод:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Купить карточки", callback_data="buy")],
                [InlineKeyboardButton("🎁 Ввести промокод", callback_data="promo_input")],
            ]),
        )
        return

    msg = await update.message.reply_text("⏳ Анализирую товар...")
    img_path = f"/tmp/product_{uid}.jpg"
    scene_path = f"/tmp/scene_{uid}.png"
    out_path = None
    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        await file.download_to_drive(img_path)

        await msg.edit_text("🔍 Определяю товар и пишу тексты...")
        data = analyze_product_image(img_path)

        await msg.edit_text("🎨 Генерирую дизайн сцены...")
        scene_desc = data.get("scene_description", "")
        bg_path = generate_scene_background(scene_desc) if scene_desc else None

        await msg.edit_text("✨ Собираю карточку...")
        out_path = make_infographic(img_path, data, scene_bg_path=bg_path)
        consume(uid)

        caption = (
            f"✅ *{data.get('title', '')}*\n"
            f"_{data.get('subtitle', '')}_\n\n"
            + "\n".join(f"• {f}" for f in data.get("features", []))
            + f"\n\n{credits_display(uid)}"
        )
        with open(out_path, "rb") as f:
            await update.message.reply_photo(f, caption=caption, parse_mode="Markdown",
                                              reply_markup=main_menu_keyboard())
        await msg.delete()

    except Exception as e:
        logging.exception(e)
        err_text = user_error_message(e)
        await msg.edit_text(err_text, parse_mode="Markdown")
        # Оповещаем владельца при критических ошибках
        if is_critical_error(e):
            await notify_owner(context,
                f"🚨 Критическая ошибка API!\n`{type(e).__name__}: {e}`\n\n"
                "Проверь баланс OpenAI и Railway Variables.")
    finally:
        for p in [img_path, out_path, scene_path]:
            if p:
                try: os.remove(p)
                except OSError: pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки постоянного меню."""
    text = update.message.text
    uid  = update.effective_user.id

    if text == "📸 Сгенерировать карточку":
        await update.message.reply_text(
            "📸 Отправь фото товара — я создам карточку для маркетплейса!",
        )
    elif text == "💡 Мои карточки":
        await update.message.reply_text(
            credits_display(uid), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🛒 Пополнить", callback_data="buy"),
            ]]),
        )
    elif text == "🛒 Купить":
        await cmd_buy(update, context)
    elif text == "🎁 Промокод":
        await update.message.reply_text(
            "🎁 *Введи промокод командой:*\n\n`/promo ТВОЙКОД`\n\nПример: `/promo SORRY5`",
            parse_mode="Markdown",
        )


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

    if d == "promo_input":
        await q.message.reply_text(
            "🎁 Введи промокод:\n\n`/promo ТВОЙКОД`",
            parse_mode="Markdown",
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
        inv_id = int(d[6:])
        pid    = get_user(uid)["pending"].get(str(inv_id))
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
            "2. ИИ анализирует и пишет продающие тексты\n"
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
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("buy",       cmd_buy))
    app.add_handler(CommandHandler("credits",   cmd_credits))
    app.add_handler(CommandHandler("promo",     cmd_promo))
    app.add_handler(CommandHandler("addpromo",  cmd_admin_promo))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    logging.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
