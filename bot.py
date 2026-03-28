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
    "TOP777":   {"credits": 3,  "used_by": set()},   # для рассылки при сбоях
    "ТОП777":   {"credits": 3,  "used_by": set()},   # для рассылки при сбоях (кириллица)
    "WELCOME3": {"credits": 3,  "used_by": set()},   # приветственный
}

# Реквизиты для ручной оплаты переводом
CARD_RU      = "2201 0402 0305 8978"   # Сбербанк / Т-Банк (РФ)
CARD_REVOLUT = "4216 0400 2047 6089"   # Visa Revolut (зарубежный банк)

# ── ТАРИФНЫЕ ПЛАНЫ ───────────────────────────────────────────────────────────────
FREE_CREDITS = 1

PLANS = {
    "single": {
        "name": "1 карточка", "emoji": "🎨",
        "credits": 1, "duration_days": None,
        "price_rub": 60, "price_usdt": 0.7, "price_ton": 7,
        "description": "1 карточка",
    },
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
def analyze_product_image(image_path: str, user_caption: str = "") -> dict:
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    user_hint_block = ""
    if user_caption:
        user_hint_block = f"""
ВАЖНО: Пользователь указал пожелания: «{user_caption}»
Обязательно учти их при формировании scene_description — сцена ДОЛЖНА соответствовать этим пожеланиям.
Например если пользователь написал «светлый интерьер, на диване» — scene_description должно быть про светлый интерьер с диваном.
"""

    prompt = f"""Ты эксперт по маркетингу на маркетплейсах (Wildberries, OZON, Яндекс Маркет).
Посмотри на фото товара и верни ТОЛЬКО JSON (без markdown, без пояснений):

{{
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
  "scene_description": "Детальное описание красивой сцены для товара НА АНГЛИЙСКОМ. Описывай окружение, декор, атмосферу — НЕ сам товар. Например для свечи: 'cozy bright living room, sofa with soft cushions, warm morning light, dried flowers, candles around'."
}}
{user_hint_block}
Правила:
- Пиши по-русски (кроме scene_description — оно на АНГЛИЙСКОМ)
- Преимущества — конкретные, с цифрами (напр: "Горит 25 часов", "100% кокос. воск")
- color_theme: warm=еда/beauty/дом/свечи, cool=техника/спорт, neutral=одежда, dark=люкс
- scene_description — ВСЕГДА на английском, максимально детально, с учётом пожеланий пользователя
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": prompt},
        ]}],
        max_tokens=600,
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


# ── ГЕНЕРАЦИЯ ПОЛНОЙ ИНФОГРАФИКИ ЧЕРЕЗ GPT-IMAGE-1 ───────────────────────────────
def generate_full_infographic(image_path: str, data: dict, user_caption: str = "") -> str | None:
    """
    Генерирует ПОЛНУЮ готовую инфографику через gpt-image-1:
    - Передаёт фото товара как input image
    - ИИ создаёт сцену с товаром + красивый текст + дизайн
    Возвращает путь к файлу или None.
    """
    title    = data.get("title", "Товар")
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])[:4]
    badge    = data.get("badge", "")
    scene    = data.get("scene_description", "luxury product photography, warm bokeh, elegant")

    feat_text = "\n".join(f"• {f}" for f in features)

    # Если пользователь указал пожелания — они ПОЛНОСТЬЮ ЗАМЕНЯЮТ сцену
    if user_caption:
        scene = user_caption  # Пожелания клиента = описание сцены

    prompt = f"""Создай профессиональную инфографику-карточку товара для маркетплейса (Wildberries/OZON/Яндекс Маркет). Квадратный формат 1:1, 1080x1080.

⚠️ ГЛАВНОЕ ТРЕБОВАНИЕ — СЦЕНА И ОКРУЖЕНИЕ:
Товар должен находиться в следующей обстановке: «{scene}»
Это описание сцены — ЗАКОН. Если написано «на пианино» — товар СТОИТ на пианино. Если «светлый интерьер» — фон светлый/белый/бежевый, НЕ тёмный. Если «с цветами» — вокруг товара живые цветы. Выполняй БУКВАЛЬНО.

ТРЕБОВАНИЯ К ДИЗАЙНУ:
1. ТОВАР — главный элемент, крупно, реалистично. Занимает 45-55% кадра.
2. ТОВАР должен выглядеть ТОЧНО как на приложенном фото — та же форма/упаковка/цвет/этикетка. НЕ искажай.
3. Вокруг товара — красивые декоративные элементы, подходящие к сцене и товару.
4. ТЕКСТ на карточке (НА РУССКОМ ЯЗЫКЕ):
   - Заголовок: «{title}» — крупный жирный шрифт, контрастный, сверху карточки
   - Подзаголовок: «{subtitle}» — изящный шрифт, меньше заголовка
   - 4 преимущества разместить ВОКРУГ товара с разных сторон (не только снизу):
{feat_text}
5. ТИПОГРАФИКА: разные размеры и стили — крупный bold заголовок, elegant italic подзаголовок, чёткие labels. Текст органично вписан в дизайн.
6. ЦВЕТА: элегантная палитра, текст читаем, всё гармонирует.
7. КАЧЕСТВО: уровень профессионального дизайна топовых продавцов — красиво, нестандартно, уникально.

Стиль: премиальная инфографика для маркетплейсов."""

    out_path = image_path.rsplit(".", 1)[0] + "_infographic.png"

    try:
        # gpt-image-1 с input image
        with open(image_path, "rb") as img_file:
            result = client.images.edit(
                model="gpt-image-1",
                image=img_file,
                prompt=prompt,
                size="1024x1024",
            )
        img_data = result.data[0].b64_json
        if img_data:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(img_data))
            return out_path
        if result.data[0].url:
            urllib.request.urlretrieve(result.data[0].url, out_path)
            return out_path
    except Exception as e:
        logging.warning(f"gpt-image-1 edit failed: {e}")

    # Fallback: gpt-image-1 generate (без input image, но с описанием)
    try:
        result = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality="high",
            n=1,
        )
        img_data = result.data[0].b64_json
        if img_data:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(img_data))
            return out_path
    except Exception as e2:
        logging.warning(f"gpt-image-1 generate fallback failed: {e2}")

    return None


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


def draw_text_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0), s_alpha=180, offset=3):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=font, fill=(*shadow, s_alpha))
    draw.text((x, y), text, font=font, fill=fill)


# ── MAKE INFOGRAPHIC — финальная сборка ──────────────────────────────────────────
def make_infographic(img_path: str, data: dict, scene_bg_path: str | None = None) -> str:
    title    = data.get("title", "Товар").upper()
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])[:4]
    badge    = data.get("badge", "")

    WHITE = (255, 255, 255, 255)

    # ── Фон: сгенерированная сцена или fallback ──────────────────────────────────
    if scene_bg_path and os.path.exists(scene_bg_path):
        canvas = Image.open(scene_bg_path).convert("RGBA").resize((W, H), Image.LANCZOS)
    else:
        raw = Image.open(img_path).convert("RGB")
        rw, rh = raw.size
        sq = min(rw, rh)
        bg = raw.crop(((rw-sq)//2, (rh-sq)//2, (rw-sq)//2+sq, (rh-sq)//2+sq))
        canvas = bg.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(45)).convert("RGBA")

    # ── Затемнение сверху и снизу для текста ─────────────────────────────────────
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(380):
        a = int(170 * (1 - i / 380) ** 1.6)
        od.line([(0, i), (W, i)], fill=(0, 0, 0, a))
    for i in range(350):
        a = int(190 * (1 - i / 350) ** 1.4)
        od.line([(0, H - 1 - i), (W, H - 1 - i)], fill=(0, 0, 0, a))
    canvas = Image.alpha_composite(canvas, ov)
    draw = ImageDraw.Draw(canvas)

    # ── ЗАГОЛОВОК — крупно сверху ────────────────────────────────────────────────
    title_max_w = W - MARGIN * 2
    f_title_use = None
    title_lines = []
    for size in (100, 88, 76, 64, 54):
        f_t = get_font("bold", size)
        lines = wrap(title, f_t, title_max_w)
        if len(lines) <= 3:
            f_title_use = f_t
            title_lines = lines
            break
    if not f_title_use:
        f_title_use = get_font("bold", 54)
        title_lines = wrap(title, f_title_use, title_max_w)

    ty = 50
    th = text_h(f_title_use)
    for line in title_lines[:3]:
        x = centered_x(line, f_title_use)
        draw_text_shadow(draw, (x, ty), line, f_title_use, WHITE, s_alpha=220, offset=4)
        ty += th + 6

    # Подзаголовок — курсив-стиль, мягче
    if subtitle:
        f_sub = get_font("regular", 36)
        ty += 8
        for line in wrap(subtitle, f_sub, title_max_w)[:2]:
            x = centered_x(line, f_sub)
            draw_text_shadow(draw, (x, ty), line, f_sub, (255, 220, 160, 255), s_alpha=160, offset=2)
            ty += text_h(f_sub) + 4

    # ── ПРЕИМУЩЕСТВА — элегантно внизу ───────────────────────────────────────────
    f_feat = get_font("bold", 30)
    feat_lh = text_h(f_feat) + 16
    total_fh = len(features) * feat_lh + 24

    fy_start = H - total_fh - 26

    # Полупрозрачная подложка
    feat_panel = Image.new("RGBA", (W, total_fh + 30), (0, 0, 0, 0))
    ImageDraw.Draw(feat_panel).rounded_rectangle(
        [MARGIN - 14, 0, W - MARGIN + 14, total_fh + 30], radius=22, fill=(0, 0, 0, 80)
    )
    paste_a(canvas, feat_panel, (0, fy_start - 8))

    fy = fy_start
    f_dot = get_font("bold", 34)
    for feat in features[:4]:
        # Символ-буллет
        draw_text_shadow(draw, (MARGIN + 4, fy - 2), "•", f_dot,
                         (255, 190, 80, 255), s_alpha=150, offset=2)
        # Текст
        draw_text_shadow(draw, (MARGIN + 36, fy), feat, f_feat,
                         WHITE, s_alpha=180, offset=2)
        fy += feat_lh

    # ── БЕЙДЖ ────────────────────────────────────────────────────────────────────
    if badge:
        f_badge = get_font("bold", 24)
        btxt = f"  {badge}  "
        bw = int(f_badge.getlength(btxt)) + 14
        bh = 40
        bl = rlayer(bw, bh, 10, fill=(210, 35, 35, 240))
        ImageDraw.Draw(bl).text((7, (bh - 24) // 2), btxt, font=f_badge, fill=WHITE)
        paste_a(canvas, bl, (MARGIN, 14))

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
            [KeyboardButton("🆘 Поддержка")],
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
        [InlineKeyboardButton(f"💳 Картой  {p['price_rub']} ₽",       callback_data=f"pay_card_{plan_id}")],
        [InlineKeyboardButton(f"🏦 Перевод на карту  {p['price_rub']} ₽", callback_data=f"pay_transfer_{plan_id}")],
        [InlineKeyboardButton(f"🌐 Visa Revolut  {p['price_usdt']}$",  callback_data=f"pay_revolut_{plan_id}")],
        [InlineKeyboardButton(f"₮ USDT  {p['price_usdt']}$",          callback_data=f"pay_crypto_{plan_id}_USDT"),
         InlineKeyboardButton(f"💎 TON  {p['price_ton']}",            callback_data=f"pay_crypto_{plan_id}_TON")],
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
            "*ТОП777* командой /promo и получи 3 карточки бесплатно! 🎁"
        )
    if isinstance(e, AuthenticationError):
        return (
            "🔧 *Технические работы* — сервис временно недоступен.\n\n"
            "Мы уже чиним! Попробуй позже, а пока держи промокод "
            "*ТОП777* — /promo для 3 бесплатных карточек."
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
        "🏆 *TOP SELLER — AI Карточки для маркетплейсов*\n\n"
        "Превращаю обычные фото товаров в *продающие инфографики* "
        "для Wildberries, OZON, Яндекс Маркет и любых других площадок.\n\n"
        "📸 *Как пользоваться:*\n"
        "1. Отправь фото товара\n"
        "2. ИИ создаст красивую сцену с товаром\n"
        "3. Получи готовую карточку 1080×1080\n\n"
        "💡 *Совет:* добавь подпись к фото с пожеланиями!\n"
        "_Пример: «подушка, светлый интерьер, на диване, с цветами»_\n"
        "Результат будет точно соответствовать твоим пожеланиям.\n\n"
        f"🎁 У тебя *{FREE_CREDITS} бесплатная* карточка.\n\n"
        "⬇️ Выбери действие:",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 *Выбери тариф:*\n\n"
        "🆓 Бесплатно — 1 карточка\n"
        "🎨 1 карточка — 60 ₽\n"
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
            "Пример: `/promo ТОП777`",
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


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для владельца: /confirm USER_ID PLAN_ID — подтвердить оплату переводом."""
    if update.effective_user.id != OWNER_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /confirm USER_ID PLAN_ID\nПример: /confirm 123456789 single")
        return
    try:
        target_uid = int(args[0])
        plan_id    = args[1]
    except ValueError:
        await update.message.reply_text("Неверный формат. USER_ID должен быть числом.")
        return
    if plan_id not in PLANS:
        await update.message.reply_text(f"Тариф «{plan_id}» не найден. Доступные: {', '.join(PLANS.keys())}")
        return
    apply_plan(target_uid, plan_id)
    p = PLANS[plan_id]
    await update.message.reply_text(f"✅ Оплата подтверждена. {p['emoji']} {p['name']} активирован для {target_uid}.")
    try:
        await context.bot.send_message(
            target_uid,
            f"🎉 *Оплата подтверждена!*\n\n"
            f"{p['emoji']} *{p['name']}* активирован.\n\n"
            f"{credits_display(target_uid)}\n\n"
            f"Присылай фото товаров — создадим карточки! 🎨",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning(f"Could not notify user {target_uid}: {e}")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для владельца: /reject USER_ID — отклонить заявку на оплату."""
    if update.effective_user.id != OWNER_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /reject USER_ID")
        return
    try:
        target_uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный формат USER_ID.")
        return
    await update.message.reply_text(f"❌ Заявка {target_uid} отклонена.")
    try:
        await context.bot.send_message(
            target_uid,
            "❌ *Оплата не подтверждена.*\n\n"
            "Скриншот не прошёл проверку. Если ты уверен, что перевод совершён — "
            "отправь скриншот ещё раз или выбери другой способ оплаты /buy.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning(f"Could not notify user {target_uid}: {e}")


async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для владельца: /reply USER_ID ТЕКСТ — ответить пользователю из поддержки."""
    if update.effective_user.id != OWNER_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /reply USER_ID ТЕКСТ_ОТВЕТА")
        return
    try:
        target_uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный USER_ID.")
        return
    reply_text = " ".join(args[1:])
    try:
        await context.bot.send_message(
            target_uid,
            f"💬 *Ответ от поддержки:*\n\n{reply_text}",
            parse_mode="Markdown",
        )
        await update.message.reply_text(f"✅ Ответ отправлен пользователю {target_uid}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось отправить: {e}")


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

    # Если пользователь ждёт подтверждения перевода — принимаем скриншот
    transfer_info = get_user(uid).get("awaiting_transfer")
    if transfer_info:
        pid    = transfer_info["plan_id"]
        method = transfer_info["method"]
        p      = PLANS[pid]
        method_label = "Перевод РФ карта" if method == "ru_card" else "Visa Revolut"
        user_obj = update.effective_user
        user_str = f"@{user_obj.username}" if user_obj.username else f"ID {uid}"

        # Пересылаем скриншот владельцу
        if OWNER_ID:
            try:
                photo = update.message.photo[-1]
                await context.bot.send_photo(
                    OWNER_ID,
                    photo.file_id,
                    caption=(
                        f"💰 *Заявка на оплату переводом*\n\n"
                        f"Пользователь: {user_str} (ID: `{uid}`)\n"
                        f"Способ: {method_label}\n"
                        f"Тариф: {p['emoji']} {p['name']} — {p['description']}\n\n"
                        f"Для подтверждения: `/confirm {uid} {pid}`\n"
                        f"Для отказа: `/reject {uid}`"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.warning(f"Failed to forward transfer screenshot to owner: {e}")

        del get_user(uid)["awaiting_transfer"]
        await update.message.reply_text(
            "✅ *Скриншот получен!*\n\n"
            "Мы проверим оплату и активируем твои карточки в течение нескольких минут.\n\n"
            "Ожидай уведомления от бота 🙏",
            parse_mode="Markdown",
        )
        return

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
    out_path = None
    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        await file.download_to_drive(img_path)

        # Пользователь может добавить подпись к фото с пожеланиями
        user_caption = update.message.caption or ""

        await msg.edit_text("🔍 Анализирую товар...")
        data = analyze_product_image(img_path, user_caption=user_caption)

        await msg.edit_text("🎨 Генерирую инфографику (20-30 сек)...")
        out_path = generate_full_infographic(img_path, data, user_caption=user_caption)

        if not out_path:
            await msg.edit_text("😔 Не удалось сгенерировать. Попробуй ещё раз.")
            return

        consume(uid)

        caption = (
            f"✅ *{data.get('title', '')}*\n"
            f"_{data.get('subtitle', '')}_\n\n"
            + "\n".join(f"• {f}" for f in data.get("features", []))
            + f"\n\n{credits_display(uid)}"
            + "\n\n💡 _Добавь подпись к фото для кастомизации_"
        )
        feedback_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👍 Отлично!", callback_data="fb_good"),
             InlineKeyboardButton("👎 Нужно лучше", callback_data="fb_bad")],
        ])
        with open(out_path, "rb") as f:
            await update.message.reply_photo(f, caption=caption, parse_mode="Markdown",
                                              reply_markup=feedback_kb)
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
        for p in [img_path, out_path]:
            if p:
                try: os.remove(p)
                except OSError: pass


async def handle_support_question(uid: int, question: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """ИИ отвечает на вопрос пользователя о боте. Если не знает — эскалирует к владельцу."""
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": """Ты — бот поддержки TOP SELLER (бот для генерации инфографики товаров для маркетплейсов).

Что умеет бот:
- Генерирует карточки для WB, OZON, Яндекс Маркет и других маркетплейсов
- Пользователь отправляет фото товара → получает готовую инфографику 1080x1080
- Можно добавить подпись к фото для кастомизации (например «на пианино, с цветами, светлый фон»)
- Тарифы: 1 карточка (60₽), Старт 10шт (490₽), Про 30шт (990₽), Безлимит (9980₽/мес)
- Оплата: перевод на карту РФ, Visa Revolut, USDT, TON
- Промокоды вводятся командой /promo

Правила ответа:
- Отвечай коротко, по делу, дружелюбно
- Если вопрос не связан с ботом или ты не уверен в ответе — ответь: ESCALATE
- Если вопрос о технической ошибке или жалоба — ответь: ESCALATE"""},
                {"role": "user", "content": question},
            ],
            max_tokens=300,
        )
        answer = response.choices[0].message.content.strip()
        if "ESCALATE" in answer:
            return None  # Нужна помощь владельца
        return answer
    except Exception:
        return None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки постоянного меню и режим поддержки."""
    text = update.message.text
    uid  = update.effective_user.id

    # Режим поддержки — если пользователь задал вопрос
    u = get_user(uid)
    if u.get("support_mode") and text not in (
        "📸 Сгенерировать карточку", "💡 Мои карточки", "🛒 Купить", "🎁 Промокод", "🆘 Поддержка"
    ):
        u["support_mode"] = False
        # Если это обратная связь по карточке
        if u.get("awaiting_feedback"):
            u["awaiting_feedback"] = False
            # Отправляем фидбек владельцу
            user_obj = update.effective_user
            user_str = f"@{user_obj.username}" if user_obj.username else f"ID {uid}"
            if OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"📝 *Обратная связь*\n\n"
                        f"От: {user_str} (ID: `{uid}`)\n"
                        f"Текст: {text}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                "🙏 Спасибо за отзыв! Мы учтём его для улучшения бота.",
                reply_markup=main_menu_keyboard(),
            )
            return

        # ИИ-поддержка
        answer = await handle_support_question(uid, text, context)
        if answer:
            await update.message.reply_text(answer, reply_markup=main_menu_keyboard())
        else:
            # Эскалация к владельцу
            user_obj = update.effective_user
            user_str = f"@{user_obj.username}" if user_obj.username else f"ID {uid}"
            if OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"🆘 *Вопрос в поддержку*\n\n"
                        f"От: {user_str} (ID: `{uid}`)\n"
                        f"Вопрос: {text}\n\n"
                        f"Ответить: `/reply {uid} ТЕКСТ`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                "📨 Вопрос передан в поддержку. Мы ответим в ближайшее время!",
                reply_markup=main_menu_keyboard(),
            )
        return

    if text == "📸 Сгенерировать карточку":
        await update.message.reply_text(
            "📸 *Отправь фото товара* — я создам карточку для маркетплейса!\n\n"
            "💡 *Подсказка:* добавь подпись к фото для лучшего результата.\n"
            "_Например: «крем для лица, с цветами, нежный стиль» или "
            "«кроссовки, спортивный стиль, динамичный фон»_\n\n"
            "Без подписи тоже работает — ИИ сам определит товар.",
            parse_mode="Markdown",
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
            "🎁 *Введи промокод командой:*\n\n`/promo ТВОЙКОД`\n\nПример: `/promo ТОП777`",
            parse_mode="Markdown",
        )
    elif text == "🆘 Поддержка":
        await update.message.reply_text(
            "🆘 *Поддержка TOP SELLER*\n\n"
            "Задай вопрос прямо здесь — отвечу!\n\n"
            "Частые вопросы:\n"
            "• Как сгенерировать карточку?\n"
            "• Как оплатить?\n"
            "• Как использовать промокод?\n"
            "• Как улучшить результат?\n\n"
            "Просто напиши свой вопрос текстом 👇",
            parse_mode="Markdown",
        )
        get_user(uid)["support_mode"] = True


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    d   = q.data

    if d == "fb_good":
        await q.message.reply_text("🙏 Спасибо! Рады что понравилось. Отправляй ещё фото!",
                                    reply_markup=main_menu_keyboard())
        return

    if d == "fb_bad":
        get_user(uid)["support_mode"] = True
        get_user(uid)["awaiting_feedback"] = True
        await q.message.reply_text(
            "😔 Жаль, что не понравилось. Расскажи, что не так?\n\n"
            "Напиши что хотелось бы исправить — мы учтём это для улучшения!",
        )
        return

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

    if d.startswith("pay_transfer_"):
        pid = d[13:]
        p   = PLANS[pid]
        get_user(uid)["awaiting_transfer"] = {"plan_id": pid, "method": "ru_card"}
        await q.message.edit_text(
            f"🏦 *Перевод на карту (РФ)*\n\n"
            f"Номер карты: `{CARD_RU}`\n"
            f"Сумма: *{p['price_rub']} ₽*\n"
            f"Получатель: Максим А.\n\n"
            f"После перевода *отправь скриншот* чека прямо в этот чат — "
            f"оплата будет подтверждена вручную в течение нескольких минут.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data=f"plan_{pid}")]]),
        )
        return

    if d.startswith("pay_revolut_"):
        pid = d[12:]
        p   = PLANS[pid]
        get_user(uid)["awaiting_transfer"] = {"plan_id": pid, "method": "revolut"}
        await q.message.edit_text(
            f"🌐 *Оплата Visa Revolut (зарубежный банк)*\n\n"
            f"Номер карты: `{CARD_REVOLUT}`\n"
            f"Сумма: *{p['price_usdt']}$*\n"
            f"Получатель: Maksim A.\n\n"
            f"После перевода *отправь скриншот* чека прямо в этот чат — "
            f"оплата будет подтверждена вручную в течение нескольких минут.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Назад", callback_data=f"plan_{pid}")]]),
        )
        return

    if d.startswith("pay_card_"):
        pid = d[9:]
        if not YOOKASSA_TOKEN:
            await q.message.reply_text(
                "⚙️ Оплата картой через платёжную систему временно недоступна.\n\n"
                "Воспользуйся переводом на карту или крипто-оплатой выше."
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


# ── НАСТРОЙКА БОТА В TELEGRAM ─────────────────────────────────────────────────────
async def setup_bot(app):
    """Устанавливает описание, about и команды бота при запуске."""
    bot = app.bot
    try:
        await bot.set_my_description(
            "🎨 TOP SELLER — ИИ-генератор карточек товаров для маркетплейсов\n\n"
            "Превращаю обычные фото в продающие инфографики для Wildberries, OZON, "
            "Яндекс Маркет и любых других площадок.\n\n"
            "Что умеет бот:\n"
            "📸 Принимает фото товара\n"
            "🤖 ИИ создаёт красивую сцену вокруг товара\n"
            "✨ Генерирует готовую карточку 1080×1080\n"
            "🎯 Добавляет продающий текст и премиальный дизайн\n"
            "🏆 Уровень топовых продавцов на маркетплейсах\n\n"
            "Как пользоваться:\n"
            "Отправь фото товара с описанием — получи карточку за 30 секунд!\n\n"
            "Подходит для: WB, OZON, Яндекс Маркет, AliExpress, Авито и других."
        )
        await bot.set_my_short_description(
            "🏆 TOP SELLER — ИИ карточки для WB, OZON, Яндекс Маркет. "
            "Отправь фото — получи продающую инфографику за 30 сек!"
        )
        await bot.set_my_commands([
            ("start",   "Запустить бота"),
            ("buy",     "Купить карточки"),
            ("credits", "Мой баланс"),
            ("promo",   "Ввести промокод"),
        ])
        logging.info("Bot description and commands set.")
    except Exception as e:
        logging.warning(f"Could not set bot info: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────────
def main():
    download_fonts()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(setup_bot).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("buy",       cmd_buy))
    app.add_handler(CommandHandler("credits",   cmd_credits))
    app.add_handler(CommandHandler("promo",     cmd_promo))
    app.add_handler(CommandHandler("addpromo",  cmd_admin_promo))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("confirm",   cmd_confirm))
    app.add_handler(CommandHandler("reject",    cmd_reject))
    app.add_handler(CommandHandler("reply",     cmd_reply))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    logging.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
