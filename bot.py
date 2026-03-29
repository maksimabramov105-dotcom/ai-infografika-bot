import os
import json
import re
import base64
import logging
import random
import urllib.request
import asyncio
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
from telegram.error import Conflict
from openai import OpenAI, RateLimitError, AuthenticationError, APIConnectionError
import db as userdb
import parsers
from analytics import (
    analyze_niche, analyze_season, analyze_suppliers,
    format_niche, format_season, format_suppliers,
)

# ── CONFIG ───────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8728265878:AAH7pdsOpSO1x4eDrnY9pKaFA5IYS7DlU6E")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY", "sk-ВСТАВЬ_КЛЮЧ")
YOOKASSA_TOKEN     = os.getenv("YOOKASSA_TOKEN", "")
CRYPTO_BOT_TOKEN   = os.getenv("CRYPTO_BOT_TOKEN", "")
CRYPTO_BOT_API     = "https://pay.crypt.bot/api"
OWNER_ID           = int(os.getenv("OWNER_ID", "0"))
ADMIN_ID           = int(os.getenv("ADMIN_ID", str(os.getenv("OWNER_ID", "0"))))

# ── TELEGRAM STARS PRODUCTS ───────────────────────────────────────────────────────
PRODUCTS = {
    "infographic_10": {
        "title": "10 инфографик",
        "description": "10 профессиональных карточек для WB/OZON",
        "stars": 250,
        "credits_infographic": 10,
        "credits_analysis": 0,
    },
    "infographic_30": {
        "title": "30 инфографик + аналитика",
        "description": "30 инфографик + 999 анализов ниш на 30 дней",
        "stars": 500,
        "credits_infographic": 30,
        "credits_analysis": 999,
    },
    "analysis_pack": {
        "title": "Пакет аналитики ×10",
        "description": "10 полных анализов (ниша + сезон + поставщики)",
        "stars": 400,
        "credits_infographic": 0,
        "credits_analysis": 10,
    },
    "all_in_one": {
        "title": "Всё включено на месяц",
        "description": "30 инфографик + 20 анализов + приоритет",
        "stars": 750,
        "credits_infographic": 30,
        "credits_analysis": 20,
    },
}

client = OpenAI(api_key=OPENAI_API_KEY)
logging.basicConfig(level=logging.INFO)

# ── ПРОМОКОДЫ ────────────────────────────────────────────────────────────────────
# Формат: "КОД": {"credits": N, "used_by": set()}
# credits=-1 → добавить к безлимиту на 7 дней
PROMO_CODES: dict[str, dict] = {
    "TOP777":   {"credits": 3,  "used_by": set()},   # для рассылки при сбоях
    "ТОП777":   {"credits": 3,  "used_by": set()},   # для рассылки при сбоях (кириллица)
    "WELCOME3": {"credits": 3,  "used_by": set()},   # приветственный
    "SELLER2026": {"credits": 3, "used_by": set()},  # email-рассылка для лидогенерации
}

# Реквизиты для ручной оплаты переводом
CARD_RU      = "2201 0402 0305 8978"   # Сбербанк / Т-Банк (РФ)
CARD_REVOLUT = "4216 0400 2047 6089"   # Visa Revolut (зарубежный банк)

# ── ТАРИФНЫЕ ПЛАНЫ ───────────────────────────────────────────────────────────────
FREE_CREDITS = 1

PLANS = {
    "single": {
        "name": "1 карточка", "emoji": "🎨",
        "credits": 1, "seo_credits": 0, "duration_days": None,
        "price_rub": 60, "price_usdt": 0.7, "price_ton": 7,
        "description": "1 карточка",
    },
    "seo": {
        "name": "SEO-текст", "emoji": "📝",
        "credits": 0, "seo_credits": 1, "duration_days": None,
        "price_rub": 50, "price_usdt": 0.6, "price_ton": 5,
        "description": "1 SEO-текст для маркетплейса",
    },
    "combo": {
        "name": "Комбо", "emoji": "🎯",
        "credits": 1, "seo_credits": 1, "duration_days": None,
        "price_rub": 100, "price_usdt": 1.1, "price_ton": 11,
        "description": "1 карточка + SEO-текст",
    },
    "start": {
        "name": "Старт", "emoji": "🚀",
        "credits": 10, "seo_credits": 0, "duration_days": None,
        "price_rub": 490, "price_usdt": 5.0, "price_ton": 50,
        "description": "10 карточек",
    },
    "pro": {
        "name": "Про", "emoji": "💎",
        "credits": 30, "seo_credits": 0, "duration_days": None,
        "price_rub": 990, "price_usdt": 10.0, "price_ton": 100,
        "description": "30 карточек",
    },
    "unlimited": {
        "name": "Безлимит", "emoji": "♾️",
        "credits": -1, "seo_credits": 0, "duration_days": 30,
        "price_rub": 9980, "price_usdt": 100.0, "price_ton": 1000,
        "description": "Безлимит на 30 дней",
    },
    "analytics_nicha": {
        "name": "Анализ ниши", "emoji": "🔍",
        "credits": 0, "seo_credits": 0, "analytics_credits": 1, "duration_days": None,
        "price_rub": 149, "price_usdt": 1.7, "price_ton": 17,
        "description": "1 анализ ниши",
    },
    "analytics_season": {
        "name": "Анализ товаров + сезонность", "emoji": "📅",
        "credits": 0, "seo_credits": 0, "analytics_credits": 1, "duration_days": None,
        "price_rub": 149, "price_usdt": 1.7, "price_ton": 17,
        "description": "Анализ товаров + сезонность",
    },
    "analytics_supplier": {
        "name": "Поставщики", "emoji": "🏭",
        "credits": 0, "seo_credits": 0, "analytics_credits": 1, "duration_days": None,
        "price_rub": 199, "price_usdt": 2.2, "price_ton": 22,
        "description": "Поиск поставщиков (1688, Alibaba)",
    },
    "analytics_full": {
        "name": "Полный анализ", "emoji": "📦",
        "credits": 0, "seo_credits": 0, "analytics_credits": 3, "duration_days": None,
        "price_rub": 399, "price_usdt": 4.5, "price_ton": 45,
        "description": "Ниша + сезонность + поставщики (экономия ~30%)",
    },
    "analytics_bundle_10": {
        "name": "Пакет 10 полных анализов", "emoji": "📊",
        "credits": 0, "seo_credits": 0, "analytics_credits": 30, "duration_days": None,
        "price_rub": 2490, "price_usdt": 28, "price_ton": 280,
        "description": "10 полных анализов (~250₽/шт) — для серьёзных селлеров",
    },
}

# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────────────────────────
user_data: dict[int, dict] = {}
active_generations: dict[int, asyncio.Task] = {}  # uid -> running generation task
cancelled_users: set[int] = set()                  # uids that pressed Stop

# Счётчик ошибок для авто-оповещения владельца
_error_counts: dict[str, int] = {}


UNLIMITED_MONTHLY_CAP = 1000  # Лимит генераций для безлимитного тарифа


def get_user(uid: int) -> dict:
    if uid not in user_data:
        user_data[uid] = {
            "credits": FREE_CREDITS, "seo_credits": 0,
            "unlimited_until": None, "pending": {},
            "monthly_count": 0, "monthly_reset": None,
        }
    u = user_data[uid]
    if "seo_credits" not in u:
        u["seo_credits"] = 0
    if "monthly_count" not in u:
        u["monthly_count"] = 0
        u["monthly_reset"] = None
    return u


def _reset_monthly_if_needed(u: dict):
    """Сбрасывает счётчик если наступил новый месяц."""
    now = datetime.now(timezone.utc)
    if not u["monthly_reset"] or now >= u["monthly_reset"]:
        u["monthly_count"] = 0
        # Следующий сброс — первое число следующего месяца
        if now.month == 12:
            u["monthly_reset"] = now.replace(year=now.year + 1, month=1, day=1,
                                              hour=0, minute=0, second=0, microsecond=0)
        else:
            u["monthly_reset"] = now.replace(month=now.month + 1, day=1,
                                              hour=0, minute=0, second=0, microsecond=0)


def has_access(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        _reset_monthly_if_needed(u)
        return u["monthly_count"] < UNLIMITED_MONTHLY_CAP
    return u["credits"] > 0


def consume(uid: int):
    if uid == OWNER_ID:
        return
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        _reset_monthly_if_needed(u)
        u["monthly_count"] += 1
        return
    if u["credits"] > 0:
        u["credits"] -= 1


def credits_display(uid: int) -> str:
    if uid == OWNER_ID:
        return "👑 Владелец — безлимит"
    u = get_user(uid)
    if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
        _reset_monthly_if_needed(u)
        used = u["monthly_count"]
        remaining = UNLIMITED_MONTHLY_CAP - used
        until = u["unlimited_until"].strftime("%d.%m.%Y")
        return f"♾️ Безлимит до {until} | Сгенерировано в этом месяце: *{used}/{UNLIMITED_MONTHLY_CAP}*"
    seo = u.get("seo_credits", 0)
    seo_str = f"\n📝 SEO-текстов: *{seo}*" if seo > 0 else ""
    return f"💡 Осталось карточек: *{u['credits']}*{seo_str}"


def has_seo_access(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    return get_user(uid).get("seo_credits", 0) > 0


def consume_seo(uid: int):
    if uid == OWNER_ID:
        return
    u = get_user(uid)
    if u.get("seo_credits", 0) > 0:
        u["seo_credits"] -= 1


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
    if p.get("seo_credits", 0) > 0:
        u["seo_credits"] = u.get("seo_credits", 0) + p["seo_credits"]


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


# ── УТИЛИТЫ ───────────────────────────────────────────────────────────────────────
def esc(text: str) -> str:
    """Escape user-provided strings for Telegram Markdown v1."""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def user_tag(user) -> str:
    """Safe user mention — escapes username underscores."""
    if user and user.username:
        return f"@{esc(user.username)}"
    return f"ID {user.id if user else '?'}"


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

# Roboto — основной (полная кириллица), NotoSans — запасной
_FONT_SOURCES = [
    ("bold",    f"{FONT_DIR}/Roboto-Bold.ttf",
     "https://github.com/googlefonts/roboto/raw/main/fonts/ttf/Roboto-Bold.ttf"),
    ("regular", f"{FONT_DIR}/Roboto-Regular.ttf",
     "https://github.com/googlefonts/roboto/raw/main/fonts/ttf/Roboto-Regular.ttf"),
    ("bold_fallback",    f"{FONT_DIR}/NotoSans-Bold.ttf",
     "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf"),
    ("regular_fallback", f"{FONT_DIR}/NotoSans-Regular.ttf",
     "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf"),
]
_SYSTEM_FONTS = {
    "bold": [
        f"{FONT_DIR}/Roboto-Bold.ttf",
        f"{FONT_DIR}/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "regular": [
        f"{FONT_DIR}/Roboto-Regular.ttf",
        f"{FONT_DIR}/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}


def _is_valid_font(path: str) -> bool:
    """Проверяет что файл является рабочим TTF (не битый и поддерживает кириллицу)."""
    try:
        f = ImageFont.truetype(path, 20)
        # Проверяем рендеринг кириллицы
        img = Image.new("RGB", (100, 30))
        ImageDraw.Draw(img).text((0, 0), "Привет", font=f, fill=(255, 255, 255))
        return True
    except Exception:
        return False


def download_fonts():
    for name, path, url in _FONT_SOURCES:
        # Скачиваем если файл отсутствует или битый
        if os.path.exists(path) and _is_valid_font(path):
            logging.info(f"Font OK: {path}")
            continue
        # Удаляем битый файл если есть
        if os.path.exists(path):
            os.remove(path)
        try:
            urllib.request.urlretrieve(url, path)
            if _is_valid_font(path):
                logging.info(f"Font downloaded and verified: {path}")
            else:
                os.remove(path)
                logging.warning(f"Font downloaded but failed Cyrillic check: {path}")
        except Exception as e:
            logging.warning(f"Font download failed ({name}): {e}")


def get_font(style: str, size: int) -> ImageFont.FreeTypeFont:
    for path in _SYSTEM_FONTS.get(style, _SYSTEM_FONTS["regular"]):
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                return font
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

    style = random.randint(0, 3)

    # Pre-built rules injected at the top of every prompt
    TEXT_ACCURACY_RULES = f"""⚠️ ABSOLUTE TEXT ACCURACY LAW — OBEY WITHOUT EXCEPTION ⚠️
TITLE must be printed EXACTLY as: «{title}»
SUBTITLE must be printed EXACTLY as: «{subtitle}»
Features must be printed EXACTLY as given below.
• Copy every letter, space, and punctuation CHARACTER BY CHARACTER.
• DO NOT add, remove, or change even ONE letter.
• DO NOT invent, translate, or rephrase any word.
• «{title}» — memorize this spelling. Reproduce it perfectly.
• NEVER render readable text on the product itself or its label.
  If the product has a label, render it blurred/unreadable — all text goes in the overlay callouts ONLY.
• Minimum 60px safe margin from ALL edges — no text cut off.
• Every callout label must be COMPLETE — never cropped by the frame border.
"""

    if style == 0:
        # STYLE: Arrow callouts — luxury annotated product card
        prompt = f"""{TEXT_ACCURACY_RULES}
Create a STUNNING premium product infographic for Russian marketplace (Wildberries/OZON). 1080x1080px square.

🎯 SCENE: «{scene}» — render this EXACTLY and LITERALLY. This is absolute law.

═══ PRODUCT PLACEMENT (CRITICAL) ═══
The product from the photo MUST be placed DEAD CENTER of the image.
It must occupy 60% of the total frame — large, dominant, impossible to miss.
Render it photo-realistically, exactly as it looks in the input image.
Surround it with a LUSH lifestyle scene: fresh flowers, natural textures (marble, wood, silk, linen),
scattered botanicals or ingredients relevant to the product. Scene fills all corners.

═══ LIGHTING & COLOR ═══
• Primary palette: deep jewel tones — emerald, sapphire, burgundy, or deep amber — in the background
• Product lit with soft frontal key light + warm golden rim light
• Background: rich bokeh with color depth, NOT flat. Layer multiple light sources.
• Color grading: rich, saturated, editorial — think Vogue product photography

═══ TYPOGRAPHY — ALL TEXT IN RUSSIAN ═══
TITLE «{title}»:
  — Massive, top-center, bold display font (like Playfair Display or Cormorant Garamond)
  — Color: bright gold (#FFD700) or luminous ivory with golden outline
  — Letter-spacing: slightly wide, elegant

SUBTITLE «{subtitle}»:
  — Italic serif or thin elegant sans, below title
  — Color: soft champagne or rose gold

FEATURE CALLOUTS with CURVED ARROW LINES pointing to product:
{feat_text}
  — Place labels at 4 corners (top-left, top-right, bottom-left, bottom-right)
  — Each label: clean white sans-serif text on semi-transparent dark pill shape
  — Arrows: thin, elegant, slightly curved white/gold lines with small arrowhead tip
  — Arrows point FROM label TOWARD the relevant part of the product

═══ OVERALL ═══
RESULT: Museum-quality product card. Looks like a campaign for a luxury Russian brand.
Rich colors, cinematic depth, gorgeous typography. NOT flat, NOT dull, NOT generic.

═══ TEXT RENDERING RULES (CRITICAL) ═══
• ALL text must be FULLY VISIBLE — no truncation, no cut-off words, no partial letters
• Minimum 60px margin from all edges — text never bleeds off frame
• Each callout label must be COMPLETE — never cut by image border
• Russian text only — correct Cyrillic spelling, no Latin substitutes, no OCR artifacts"""

    elif style == 1:
        # STYLE: Modern editorial — floating badges, no arrows
        prompt = f"""{TEXT_ACCURACY_RULES}
Create a BREATHTAKING premium product infographic for Russian marketplace (Wildberries/OZON). 1080x1080px square.

🎯 SCENE: «{scene}» — render this EXACTLY and LITERALLY. This is absolute law.

═══ PRODUCT PLACEMENT (CRITICAL) ═══
Product from the photo: PERFECTLY CENTERED, taking up 60% of the frame.
Sharp focus on product, background has natural depth-of-field blur.
Around product: editorial lifestyle staging — textured fabrics, flowers, matching props.

═══ LIGHTING & COLOR ═══
• Background: soft, creamy, desaturated (linen, alabaster, blush) — clean and luxe
• Product: crisp, bright, perfectly lit — studio beauty lighting
• Accents: dusty rose, sage green, warm terracotta, or soft cobalt as accent colors
• Feel: high-fashion magazine editorial, Scandinavian luxury

═══ TYPOGRAPHY — ALL TEXT IN RUSSIAN ═══
TITLE «{title}»:
  — Top area, large bold condensed sans-serif (like Neue Haas Grotesk or Futura Bold)
  — Color: rich black or deep charcoal with thin colored accent line underneath
  — Very confident, clean, architectural

SUBTITLE «{subtitle}»:
  — Light weight, elegant, below title. Color: medium gray or muted accent

FEATURES as FLOATING BADGE LABELS (NO arrows):
{feat_text}
  — Each feature: small rounded rectangle pill, positioned AROUND the centered product
  — Pills: matte dark background (charcoal or deep navy) with bright white text
  — Small accent dot or icon before each text
  — Placed asymmetrically for visual rhythm: some higher, some lower

═══ OVERALL ═══
Aesthetic: modern Scandinavian luxury, minimal but rich in detail.
Typography is the hero. Colors are muted but intentional. NOT sterile — textured and warm.

═══ TEXT RENDERING RULES (CRITICAL) ═══
• ALL text must be FULLY VISIBLE — no truncation, no cut-off words, no partial letters
• Minimum 60px margin from all edges — text never bleeds off frame
• Each callout label must be COMPLETE — never cut by image border
• Russian text only — correct Cyrillic spelling, no Latin substitutes, no OCR artifacts"""

    elif style == 2:
        # STYLE: Dark dramatic — cinematic with vibrant accent color
        prompt = f"""{TEXT_ACCURACY_RULES}
Create a VISUALLY STRIKING premium product infographic for Russian marketplace (Wildberries/OZON). 1080x1080px square.

🎯 SCENE: «{scene}» — render this EXACTLY and LITERALLY. This is absolute law.

═══ PRODUCT PLACEMENT (CRITICAL) ═══
Product from the photo: DEAD CENTER of the image, occupying 60% of the frame.
Bold, dramatic, heroic presence. Nothing competes with it visually.
Scene: moody and cinematic — deep shadows, dramatic props, rich textures behind and around product.

═══ LIGHTING & COLOR ═══
• Dominant palette: DARK — deep navy, charcoal, near-black background
• Vivid accent: choose ONE strong accent color (neon teal, electric coral, gold, or violet)
• Product lit dramatically from one side + subtle colored rim light matching the accent
• Background: scattered points of light (bokeh), smoke or mist effect, luxury product vibe
• Color grading: moody, contrasty, cinematic — like a perfume commercial

═══ TYPOGRAPHY — ALL TEXT IN RUSSIAN ═══
TITLE «{title}»:
  — HUGE, dominant, top center
  — White or vibrant accent color
  — Bold display font with strong presence — condensed or wide, not regular weight

SUBTITLE «{subtitle}»:
  — Elegant thin font below title, in accent color or light silver

FEATURES — annotated with ELEGANT POINTER LINES:
{feat_text}
  — Feature text in glowing/bright labels connected to product by thin luminous lines
  — Labels glow subtly matching the accent color
  — Spread evenly around the product

═══ OVERALL ═══
Final look: A bold, dark, dramatic luxury product card. Like a high-end perfume or tech launch campaign.
Extremely visual, premium, impossible to scroll past.

═══ TEXT RENDERING RULES (CRITICAL) ═══
• ALL text must be FULLY VISIBLE — no truncation, no cut-off words, no partial letters
• Minimum 60px margin from all edges — text never bleeds off frame
• Each callout label must be COMPLETE — never cut by image border
• Russian text only — correct Cyrillic spelling, no Latin substitutes, no OCR artifacts"""

    else:
        # STYLE: Warm organic — lifestyle rich
        prompt = f"""{TEXT_ACCURACY_RULES}
Create an EXQUISITE warm lifestyle product infographic for Russian marketplace (Wildberries/OZON). 1080x1080px square.

🎯 SCENE: «{scene}» — render this EXACTLY and LITERALLY. This is absolute law.

═══ PRODUCT PLACEMENT (CRITICAL) ═══
Product from the photo: CENTERED, occupying 60% of the frame — large, beautiful, in perfect focus.
The product should look appetizing, inviting, desirable.
Around it: an abundant, lush organic scene — fresh botanicals, fruit slices, fabric textures,
natural materials (wood, stone, terra cotta) that perfectly match the product category.
EVERY CORNER filled with beautiful lifestyle props. Scene is abundant, NOT sparse.

═══ LIGHTING & COLOR ═══
• Warm golden hour lighting — honey-gold, amber, sunset tones
• Palette: rich terracotta, deep sage, warm cream, burnt sienna, dusty rose
• Multiple warm light sources: top-down golden light + soft side fill
• Deep warm shadows that add dimension, not darkness
• Result: feels like a beautiful food/beauty/lifestyle Instagram still life

═══ TYPOGRAPHY — ALL TEXT IN RUSSIAN ═══
TITLE «{title}»:
  — Top of image, large elegant serif (like Garamond or Bodoni)
  — Color: deep warm terracotta or rich burgundy
  — Slight vintage charm, but modern and premium

SUBTITLE «{subtitle}»:
  — Script or italic serif, slightly smaller. Color: muted gold or warm taupe

FEATURES:
{feat_text}
  — Each feature placed near edges, framed by small decorative botanical element
  — Text in warm charcoal or deep brown — warm, organic, hand-crafted feel
  — No arrows — features naturally integrated into the scene layout

═══ OVERALL ═══
Final look: An artisan lifestyle editorial. Like a premium organic/beauty brand campaign.
Warm, lush, inviting, and deeply appetizing. Rich colors, beautiful typography, abundant scene.

═══ TEXT RENDERING RULES (CRITICAL) ═══
• ALL text must be FULLY VISIBLE — no truncation, no cut-off words, no partial letters
• Minimum 60px margin from all edges — text never bleeds off frame
• Each callout label must be COMPLETE — never cut by image border
• Russian text only — correct Cyrillic spelling, no Latin substitutes, no OCR artifacts"""

    out_path = image_path.rsplit(".", 1)[0] + "_infographic.png"

    try:
        # gpt-image-1 с input image
        with open(image_path, "rb") as img_file:
            result = client.images.edit(
                model="gpt-image-1",
                image=img_file,
                prompt=prompt,
                size="1024x1024",
                quality="high",
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


# ── SEO-ТЕКСТ ────────────────────────────────────────────────────────────────────
def generate_seo_text(data: dict, user_caption: str = "") -> str:
    title    = data.get("title", "Товар")
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])
    hint = f"\nДополнительный контекст: {user_caption}" if user_caption else ""

    prompt = f"""Ты эксперт по SEO и продажам на маркетплейсах (Wildberries, OZON, Яндекс Маркет).
Напиши полный продающий SEO-текст для карточки товара.

Товар: {title}
Подзаголовок: {subtitle}
Характеристики: {', '.join(features)}{hint}

Структура ответа (строго в этом порядке):

**НАЗВАНИЕ ТОВАРА ДЛЯ МАРКЕТПЛЕЙСА**
(SEO-оптимизированное название, 60-80 символов, включи ключевые слова)

**ОПИСАНИЕ**
(2-3 абзаца, продающий текст, включи ключевые слова органично, польза для покупателя)

**ХАРАКТЕРИСТИКИ**
(маркированный список, 5-8 пунктов с конкретными данными)

**КЛЮЧЕВЫЕ СЛОВА**
(15-20 поисковых запросов через запятую, по которым покупатели ищут этот товар)

Пиши только по-русски. Текст должен быть живым, убедительным и SEO-оптимизированным."""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )
    return response.choices[0].message.content.strip()


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
            [KeyboardButton("📸 Сгенерировать карточку"), KeyboardButton("📝 SEO-текст")],
            [KeyboardButton("🛒 Купить"),                 KeyboardButton("🎁 Промокод")],
            [KeyboardButton("📊 Аналитика"),              KeyboardButton("📌 Памятка")],
            [KeyboardButton("💰 Мой баланс"),             KeyboardButton("🆘 Поддержка")],
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


def stars_keyboard():
    """Keyboard showing Stars packages (PRODUCTS)."""
    rows = []
    for pid, p in PRODUCTS.items():
        rows.append([InlineKeyboardButton(
            f"⭐ {p['title']} — {p['stars']} Stars",
            callback_data=f"stars_{pid}",
        )])
    rows.append([InlineKeyboardButton("💳 Оплатить рублями/крипто", callback_data="buy")])
    return InlineKeyboardMarkup(rows)


def payment_keyboard(plan_id: str):
    p = PLANS[plan_id]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⭐ Telegram Stars",                    callback_data=f"stars_buy_{plan_id}")],
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
    username = update.effective_user.username

    # Handle referral parameter
    referrer_id = None
    if context.args and context.args[0].startswith("REF_"):
        try:
            referrer_id = int(context.args[0].replace("REF_", ""))
            if referrer_id == uid:
                referrer_id = None
        except Exception:
            pass

    is_new = await userdb.register_user(uid, referrer_id, username)
    get_user(uid)  # ensure in-memory record exists

    if is_new and referrer_id:
        try:
            await context.bot.send_message(
                referrer_id,
                f"👤 По твоей ссылке зарегистрировался новый пользователь!\n"
                f"Ты получишь *30% с его покупок*.",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await update.message.reply_text(
        "🏆 *TOP SELLER* — твой ИИ-ассистент для маркетплейсов\n\n"
        "Что я умею:\n"
        "📸 *Инфографика* — отправь фото товара → получи карточку 1080×1080 для WB/OZON/Яндекс Маркет\n"
        "📝 *SEO-текст* — название, описание и ключевые слова для маркетплейса\n"
        "🔍 *Анализ ниши* — конкуренция, цены, лидеры рынка\n"
        "📅 *Сезонность* — когда входить, когда пик продаж\n"
        "🏭 *Поставщики* — цены и ссылки на 1688/Alibaba\n\n"
        "💡 *Совет:* добавь подпись к фото — _«подушка, светлый интерьер, на диване»_\n\n"
        f"🎁 Тебе доступна *{FREE_CREDITS} бесплатная* карточка. Начинай!",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛒 *Выбери раздел:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎨 Инфографика & SEO", callback_data="buy_section_cards")],
            [InlineKeyboardButton("📊 Аналитика",         callback_data="buy_section_analytics")],
        ]),
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
    await userdb.delete_pending_transfer(target_uid)
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


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    task = active_generations.pop(uid, None)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("⛔ Генерация остановлена.")
    else:
        await update.message.reply_text("Нет активной генерации.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Только для владельца: /pending — список ожидающих подтверждения переводов."""
    if update.effective_user.id != OWNER_ID:
        return
    rows = await userdb.get_all_pending_transfers()
    if not rows:
        await update.message.reply_text("Нет ожидающих переводов.")
        return
    lines = ["📋 Ожидают подтверждения:\n"]
    for r in rows:
        p = PLANS.get(r["plan_id"], {})
        method_label = "РФ карта" if r["method"] == "ru_card" else "Revolut"
        lines.append(
            f"• ID {r['user_id']} — {p.get('name', r['plan_id'])} ({method_label})\n"
            f"  /confirm {r['user_id']} {r['plan_id']}"
        )
    await update.message.reply_text("\n".join(lines))


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
    if not transfer_info:
        transfer_info = await userdb.get_pending_transfer(uid)
        if transfer_info:
            get_user(uid)["awaiting_transfer"] = transfer_info  # restore in-memory
    if transfer_info:
        pid    = transfer_info["plan_id"]
        method = transfer_info["method"]
        p      = PLANS[pid]
        expected_amount = p["price_rub"] if method == "ru_card" else p["price_usdt"]
        currency = "₽" if method == "ru_card" else "$"

        del get_user(uid)["awaiting_transfer"]
        await userdb.delete_pending_transfer(uid)

        user_obj     = update.effective_user
        first_name   = user_obj.first_name or ""
        last_name    = user_obj.last_name or ""
        full_name    = (first_name + " " + last_name).strip() or f"ID {uid}"
        method_label = "РФ карта" if method == "ru_card" else "Visa Revolut"
        photo        = update.message.photo[-1]

        # Суммы > 500₽ (или > $5.5) — только ручное подтверждение
        amount_rub = p["price_rub"] if method == "ru_card" else int(p["price_usdt"] * 90)
        needs_manual = amount_rub > 500

        if not OWNER_ID:
            logging.error("OWNER_ID not set — cannot process transfer!")
            return

        if needs_manual:
            # Ждём ручного подтверждения от владельца
            await update.message.reply_text(
                "📨 Скриншот получен!\n\n"
                "Сумма требует ручной проверки — подтвердим в течение нескольких минут.\n"
                "Как только проверим, баланс пополнится автоматически 🙏",
            )
            caption = (
                f"⚠️ Требует подтверждения\n\n"
                f"👤 {full_name} (@{user_obj.username or 'без username'}, ID: {uid})\n"
                f"💰 {expected_amount} {currency} — {method_label}\n"
                f"📦 Тариф: {p['name']}\n\n"
                f"Сумма > 500₽ — подтверди вручную."
            )
            try:
                await context.bot.send_photo(
                    OWNER_ID, photo.file_id, caption=caption,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Подтвердить", callback_data=f"owner_confirm_{uid}_{pid}"),
                        InlineKeyboardButton("❌ Отклонить",   callback_data=f"owner_reject_{uid}"),
                    ]]),
                )
            except Exception as e:
                logging.error(f"Failed to forward transfer to owner: {e}")
        else:
            # Авто-подтверждение для малых сумм
            apply_plan(uid, pid)
            await update.message.reply_text(
                f"✅ Оплата подтверждена!\n\n"
                f"{p['emoji']} *{p['name']}* активирован.\n\n"
                f"{credits_display(uid)}\n\n"
                f"Отправляй фото товаров — создадим карточки! 🎨",
                parse_mode="Markdown",
            )
            caption = (
                f"🔔 Авто-подтверждено (≤500₽)\n\n"
                f"👤 {full_name} (@{user_obj.username or 'без username'}, ID: {uid})\n"
                f"💰 {expected_amount} {currency} — {method_label}\n"
                f"📦 Тариф: {p['name']}\n\n"
                f"Баланс пополнен. Нажми ↩️ если скриншот поддельный."
            )
            try:
                await context.bot.send_photo(
                    OWNER_ID, photo.file_id, caption=caption,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("↩️ Отозвать (мошенник)", callback_data=f"owner_revoke_{uid}_{pid}"),
                    ]]),
                )
            except Exception as e:
                logging.error(f"Failed to notify owner of auto-confirmed transfer: {e}")
        return

    if not has_access(uid):
        u = get_user(uid)
        # Проверяем: безлимит активен, но месячный лимит исчерпан
        if u["unlimited_until"] and datetime.now(timezone.utc) < u["unlimited_until"]:
            reset_date = u.get("monthly_reset")
            reset_str = reset_date.strftime("%d.%m.%Y") if reset_date else "1-го числа"
            await update.message.reply_text(
                f"⚠️ *Достигнут месячный лимит генераций*\n\n"
                f"В целях обеспечения качества сервиса для всех пользователей "
                f"тариф «Безлимит» ограничен *{UNLIMITED_MONTHLY_CAP} карточками в месяц*.\n\n"
                f"Лимит обновится *{reset_str}*. Увидимся! 🙏",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "😔 *Карточки закончились.*\n\nКупи пакет или активируй промокод:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒 Купить карточки", callback_data="buy")],
                    [InlineKeyboardButton("🎁 Ввести промокод", callback_data="promo_input")],
                ]),
            )
        return

    stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⛔ Остановить", callback_data="stop_gen")]])
    msg = await update.message.reply_text("⏳ Анализирую товар...", reply_markup=stop_kb)
    img_path = f"/tmp/product_{uid}.jpg"
    out_path = None

    async def _do_generate():
        nonlocal out_path
        try:
            photo = update.message.photo[-1]
            file  = await context.bot.get_file(photo.file_id)
            await file.download_to_drive(img_path)

            user_caption = update.message.caption or ""

            if uid in cancelled_users:
                cancelled_users.discard(uid)
                await msg.edit_text("⛔ Генерация остановлена.")
                return

            await msg.edit_text("🔍 Анализирую товар...", reply_markup=stop_kb)
            data = analyze_product_image(img_path, user_caption=user_caption)

            if uid in cancelled_users:
                cancelled_users.discard(uid)
                await msg.edit_text("⛔ Генерация остановлена.")
                return

            await msg.edit_text("🎨 Генерирую инфографику (20-30 сек)...", reply_markup=stop_kb)
            out_path = generate_full_infographic(img_path, data, user_caption=user_caption)

            if uid in cancelled_users:
                cancelled_users.discard(uid)
                await msg.edit_text("⛔ Генерация остановлена.")
                return

            if not out_path:
                await msg.edit_text("😔 Не удалось сгенерировать. Попробуй ещё раз.")
                return

            consume(uid)
            get_user(uid)["last_product_data"] = data
            get_user(uid)["last_user_caption"] = user_caption

            caption = (
                f"✅ *{data.get('title', '')}*\n"
                f"_{data.get('subtitle', '')}_\n\n"
                + "\n".join(f"• {f}" for f in data.get("features", []))
                + f"\n\n{credits_display(uid)}"
                + "\n\n💡 _Добавь подпись к фото для кастомизации_"
            )
            if has_seo_access(uid):
                seo_btn = InlineKeyboardButton("📝 Получить SEO-текст", callback_data="gen_seo")
            else:
                seo_btn = InlineKeyboardButton("📝 SEO-текст — 50 ₽", callback_data="buy_seo")
            feedback_kb = InlineKeyboardMarkup([
                [seo_btn],
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
            try:
                await msg.edit_text(err_text, parse_mode="Markdown")
            except Exception:
                pass
            if is_critical_error(e):
                await notify_owner(context,
                    f"🚨 Критическая ошибка API!\n`{type(e).__name__}: {e}`\n\n"
                    "Проверь баланс OpenAI и Railway Variables.")
        finally:
            active_generations.pop(uid, None)
            cancelled_users.discard(uid)
            for p in [img_path, out_path]:
                if p:
                    try: os.remove(p)
                    except OSError: pass

    task = asyncio.create_task(_do_generate())
    active_generations[uid] = task
    # Do NOT await — runs as background task so stop_gen callback can fire


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
            user_obj = update.effective_user
            if OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"📝 *Обратная связь*\n\n"
                        f"От: {user_tag(user_obj)} (ID: `{uid}`)\n"
                        f"Текст: {esc(text)}",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logging.error(f"Failed to send feedback to owner: {e}")
            await update.message.reply_text(
                "🙏 Спасибо за отзыв! Мы учтём его для улучшения бота.",
                reply_markup=main_menu_keyboard(),
            )
            return

        # ИИ-поддержка — всегда уведомляем владельца
        user_obj = update.effective_user
        if OWNER_ID:
            try:
                await context.bot.send_message(
                    OWNER_ID,
                    f"🆘 *Вопрос в поддержку*\n\n"
                    f"От: {user_tag(user_obj)} (ID: `{uid}`)\n"
                    f"Вопрос: {esc(text)}\n\n"
                    f"Ответить: `/reply {uid} ТЕКСТ`",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.error(f"Failed to forward support msg to owner: {e}")

        answer = await handle_support_question(uid, text, context)
        if answer:
            await update.message.reply_text(answer, reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text(
                "📨 Вопрос передан в поддержку. Мы ответим в ближайшее время!",
                reply_markup=main_menu_keyboard(),
            )
        return

    if text == "📝 SEO-текст":
        data = get_user(uid).get("last_product_data")
        if not data:
            await update.message.reply_text(
                "📝 *SEO-текст*\n\n"
                "Сначала сгенерируй карточку товара — отправь фото.\n"
                "После генерации SEO-текст создастся на основе твоего товара.",
                parse_mode="Markdown",
            )
        elif not has_seo_access(uid):
            await update.message.reply_text(
                "📝 *SEO-текст — 50 ₽*\n\n"
                "Получи продающее название, описание и ключевые слова для WB/OZON/Яндекс Маркет.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📝 SEO — 50 ₽", callback_data="plan_seo")],
                    [InlineKeyboardButton("🎯 Комбо (карточка+SEO) — 100 ₽", callback_data="plan_combo")],
                ]),
            )
        else:
            await update.message.reply_text("⏳ Генерирую SEO-текст...")
            try:
                user_caption = get_user(uid).get("last_user_caption", "")
                seo_text = generate_seo_text(data, user_caption)
                consume_seo(uid)
                await update.message.reply_text(
                    f"📝 *SEO-текст готов!*\n\n{seo_text}\n\n{credits_display(uid)}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logging.exception(e)
                await update.message.reply_text("😔 Не удалось сгенерировать SEO. Попробуй ещё раз.")
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
    elif text == "📌 Памятка":
        await update.message.reply_text(
            "📌 *Памятка: как получить идеальную карточку*\n\n"
            "📸 *Какое фото прислать:*\n"
            "• Чистый фон (белый или однотонный) — лучше всего\n"
            "• Хорошее освещение, без теней\n"
            "• Товар крупно, по центру, без обрезки\n"
            "• Высокое разрешение (от 1000×1000 пикс.)\n"
            "• Без водяных знаков и текста\n"
            "• Один товар на фото (не набор)\n\n"
            "❌ *Не подходят:* скриншоты с WB, размытые фото, "
            "фото на пёстром фоне, коллажи\n\n"
            "✍️ *Как написать подпись (промпт):*\n"
            "Добавь подпись к фото перед отправкой — бот создаст "
            "именно ту сцену, которую ты описал.\n\n"
            "✅ *Хорошие примеры:*\n"
            "• _«свеча на пианино, с цветами, тёплый свет»_\n"
            "• _«крем для лица, светлый интерьер, минималистичный стиль»_\n"
            "• _«кроссовки, спортивный стиль, динамичный фон, неон»_\n"
            "• _«подушка, уютная гостиная, на диване, пастельные тона»_\n\n"
            "❌ *Плохие примеры:*\n"
            "• _«сделай красиво»_ — слишком абстрактно\n"
            "• _«товар»_ — ничего не описывает\n\n"
            "💡 *Формула промпта:*\n"
            "`[товар], [где/сцена], [стиль/атмосфера], [детали]`\n\n"
            "📝 *SEO-текст:*\n"
            "После генерации карточки нажми «📝 SEO-текст» — "
            "получишь готовые название, описание и ключевые слова для маркетплейса.",
            parse_mode="Markdown",
        )
    elif text == "📊 Аналитика":
        await update.message.reply_text(
            "📊 *Аналитика для маркетплейсов*\n\n"
            "🔍 *Анализ ниши* — /nicha `товар`\n"
            "Конкуренция, цены, топ-продавцы на WB/OZON\n\n"
            "📅 *Сезонность* — /season `товар`\n"
            "Пики и спады продаж по месяцам, лучшее время входа\n\n"
            "🏭 *Поставщики* — /supplier `товар`\n"
            "Поставщики на 1688/Alibaba: цены и маржа\n\n"
            "📦 *Полный анализ* — /full `товар`\n"
            "Все три отчёта сразу: ниша + сезонность + поставщики\n\n"
            "Пример: `/nicha кокосовое масло`\n"
            "Купить кредиты: /buy → Аналитика",
            parse_mode="Markdown",
        )
    elif text == "💰 Мой баланс":
        bal = await userdb.get_analytics_balance(uid)
        u = get_user(uid)
        await update.message.reply_text(
            f"💰 *Твой баланс*\n\n"
            f"🎨 Карточки: *{credits_display(uid)}*\n"
            f"📊 Аналитика: *{bal} кредит(а)*\n\n"
            f"Пополнить: /buy",
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

    if d == "stop_gen":
        task = active_generations.pop(uid, None)
        if task and not task.done():
            task.cancel()
            try:
                await q.message.edit_text("⛔ Генерация остановлена.")
            except Exception:
                pass
        else:
            await q.answer("Нет активной генерации.", show_alert=True)
        return

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

    if d == "gen_seo":
        data = get_user(uid).get("last_product_data")
        if not data:
            await q.message.reply_text("❌ Нет данных о товаре. Сначала сгенерируй карточку.")
            return
        if not has_seo_access(uid):
            await q.message.reply_text(
                "📝 *SEO-текст — 50 ₽*\n\nКупи SEO или Комбо в разделе /buy",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Купить", callback_data="buy")]]),
            )
            return
        await q.answer("⏳ Генерирую SEO...")
        await q.message.reply_text("⏳ Генерирую SEO-текст...")
        try:
            user_caption = get_user(uid).get("last_user_caption", "")
            seo_text = generate_seo_text(data, user_caption)
            consume_seo(uid)
            await q.message.reply_text(
                f"📝 *SEO-текст готов!*\n\n{seo_text}\n\n{credits_display(uid)}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logging.exception(e)
            await q.message.reply_text("😔 Не удалось сгенерировать SEO. Попробуй ещё раз.")
        return

    if d.startswith("stars_buy_"):
        # Stars payment for a PLANS-based plan (via payment_keyboard)
        plan_id = d.replace("stars_buy_", "")
        p = PLANS.get(plan_id)
        if not p:
            return
        # Map PLANS price_rub to approximate Stars (1 Star ≈ ~2₽)
        stars_amount = max(1, round(p["price_rub"] / 2))
        await context.bot.send_invoice(
            chat_id=uid,
            title=f"{p['emoji']} {p['name']}",
            description=p["description"],
            payload=f"plan_{plan_id}:{uid}",
            currency="XTR",
            prices=[LabeledPrice(p["name"], stars_amount)],
        )
        return

    if d.startswith("stars_"):
        # Stars payment for PRODUCTS
        product_id = d.replace("stars_", "")
        product = PRODUCTS.get(product_id)
        if not product:
            return
        await context.bot.send_invoice(
            chat_id=uid,
            title=product["title"],
            description=product["description"],
            payload=f"{product_id}:{uid}",
            currency="XTR",
            prices=[LabeledPrice(product["title"], product["stars"])],
        )
        return

    if d.startswith("ana_"):
        # Format: ana_{cmd}_{marketplace}:{query}
        # e.g. ana_nicha_wb:кокосовое масло
        rest = d[4:]  # strip "ana_"
        parts = rest.split(":", 1)
        if len(parts) < 2:
            return
        cmd_market = parts[0]   # e.g. "nicha_wb"
        query = parts[1].strip()
        cmd_parts = cmd_market.split("_", 1)
        if len(cmd_parts) < 2:
            return
        cmd, marketplace = cmd_parts[0], cmd_parts[1]

        await q.answer()
        await q.message.delete()

        async def _reply(text):
            return await context.bot.send_message(uid, text)

        if cmd == "nicha":
            await _run_nicha(uid, query, marketplace, _reply)
        elif cmd == "season":
            mkt_label = {"wb": "Wildberries", "ozon": "OZON", "amazon": "Amazon", "all": "все площадки"}
            msg = await _reply(f"📅 Анализирую «{query}» ({mkt_label.get(marketplace, marketplace)})…")
            try:
                await _use_analytics_credit(uid, 1)
                raw = await analyze_season(client, query, marketplace)
                text = format_season(query, raw)
                await userdb.log_analysis(uid, query, f"season_{marketplace}", raw)
                await msg.edit_text(text, parse_mode="Markdown")
            except Exception as e:
                logging.exception(e)
                await msg.edit_text("❌ Ошибка анализа. Попробуй позже.")
        elif cmd == "full":
            # Recreate update-like send for _run_full
            class _FakeUpdate:
                class message:
                    @staticmethod
                    async def reply_text(text, **kw):
                        return await context.bot.send_message(uid, text, **kw)
            await _run_full(uid, query, marketplace, _FakeUpdate())
        return

    if d == "buy_section_cards":
        await q.message.edit_text(
            "🎨 *Инфографика & SEO*\n\n"
            "⭐ *Telegram Stars (быстро, без банков):*\n"
            "• 10 инфографик — 250 ⭐\n"
            "• 30 инфографик + аналитика — 500 ⭐\n"
            "• Всё включено — 750 ⭐\n\n"
            "💳 *Рубли / Крипто:*\n"
            "• 1 карточка — 60 ₽\n"
            "• Комбо (карточка + SEO) — 100 ₽\n"
            "• Старт 10шт — 490 ₽\n"
            "• Про 30шт — 990 ₽\n"
            "• Безлимит/мес — 9 980 ₽\n\n"
            "_SEO-текст включён в Комбо или списывается с баланса (50 ₽)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Оплатить Stars", callback_data="buy_stars")],
                [InlineKeyboardButton("🎨 1 карточка — 60 ₽",        callback_data="plan_single")],
                [InlineKeyboardButton("🎯 Комбо (карт.+SEO) — 100 ₽", callback_data="plan_combo")],
                [InlineKeyboardButton("🚀 Старт 10шт — 490 ₽",        callback_data="plan_start")],
                [InlineKeyboardButton("💎 Про 30шт — 990 ₽",           callback_data="plan_pro")],
                [InlineKeyboardButton("♾️ Безлимит — 9 980 ₽",         callback_data="plan_unlimited")],
                [InlineKeyboardButton("← Назад", callback_data="buy_main")],
            ]),
        )
        return

    if d == "buy_section_analytics":
        await q.message.edit_text(
            "📊 *Аналитика для маркетплейсов*\n\n"
            "🔍 *Анализ ниши* — 149 ₽\n"
            "Конкуренция, цены, топ-продавцы на WB/OZON. "
            "Узнай насколько перегрета ниша перед входом.\n\n"
            "📅 *Анализ товаров + сезонность* — 149 ₽\n"
            "Лучшее время входа на рынок, пики и спады продаж по месяцам, "
            "прогноз спроса. Не зайди в несезон.\n\n"
            "🏭 *Поставщики* — 199 ₽\n"
            "Поиск поставщиков на 1688 и Alibaba: цены, маржа, условия.\n\n"
            "📦 *Полный анализ* — 399 ₽ _(скидка ~30%)_\n"
            "ВСЕ ТРИ отчёта: ниша + сезонность + поставщики.\n\n"
            "📊 *Пакет 10 полных анализов* — 2 490 ₽ _(~250₽/шт)_\n"
            "Для серьёзных селлеров — максимальная выгода.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Анализ ниши — 149 ₽",            callback_data="plan_analytics_nicha")],
                [InlineKeyboardButton("📅 Сезонность — 149 ₽",             callback_data="plan_analytics_season")],
                [InlineKeyboardButton("🏭 Поставщики — 199 ₽",             callback_data="plan_analytics_supplier")],
                [InlineKeyboardButton("📦 Полный анализ — 399 ₽",          callback_data="plan_analytics_full")],
                [InlineKeyboardButton("📊 Пакет ×10 — 2 490 ₽",           callback_data="plan_analytics_bundle_10")],
                [InlineKeyboardButton("← Назад", callback_data="buy_main")],
            ]),
        )
        return

    if d == "buy_main":
        await q.message.edit_text(
            "🛒 *Выбери раздел:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎨 Инфографика & SEO", callback_data="buy_section_cards")],
                [InlineKeyboardButton("📊 Аналитика",         callback_data="buy_section_analytics")],
            ]),
        )
        return

    if d == "buy_stars":
        await q.message.edit_text(
            "⭐ *Telegram Stars — выбери пакет:*",
            parse_mode="Markdown",
            reply_markup=stars_keyboard(),
        )
        return

    if d.startswith("buy_analytics_"):
        cmd = d.replace("buy_analytics_", "")
        name, price, emoji = ANALYTICS_PRICES.get(cmd, ("Анализ", 149, "📊"))
        await q.message.reply_text(
            f"{emoji} *{name}* — {price}₽\n\n"
            f"Купи через /buy → Аналитика.\n\n"
            f"После оплаты: `/{cmd} [запрос]`",
            parse_mode="Markdown",
        )
        return

    if d == "buy_seo":
        await q.message.reply_text(
            "📝 *SEO-текст* списывается с баланса (50 ₽ / 1 кредит).\n\n"
            "Купи *Комбо* (карточка + SEO) или пополни баланс через /buy.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Комбо — 100 ₽", callback_data="plan_combo")],
                [InlineKeyboardButton("← Назад", callback_data="buy_section_cards")],
            ]),
        )
        return

    if d == "buy":
        await q.message.edit_text(
            "🛒 *Выбери раздел:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎨 Инфографика & SEO", callback_data="buy_section_cards")],
                [InlineKeyboardButton("📊 Аналитика",         callback_data="buy_section_analytics")],
            ]),
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

    if d.startswith("show_card_ru_"):
        pid = d[13:]
        p = PLANS[pid]
        await context.bot.send_message(
            uid,
            f"💳 Номер карты (РФ):\n\n"
            f"`{CARD_RU}`\n\n"
            f"Сумма: {p['price_rub']} ₽ — Максим А.\n"
            f"_Нажми на номер выше чтобы скопировать_",
            parse_mode="Markdown",
        )
        return

    if d.startswith("show_card_rev_"):
        pid = d[14:]
        p = PLANS[pid]
        await context.bot.send_message(
            uid,
            f"💳 Номер карты (Revolut):\n\n"
            f"`{CARD_REVOLUT}`\n\n"
            f"Сумма: {p['price_usdt']}$ — Maksim A.\n"
            f"_Нажми на номер выше чтобы скопировать_",
            parse_mode="Markdown",
        )
        return

    if d.startswith("pay_transfer_"):
        pid = d[13:]
        p   = PLANS[pid]
        get_user(uid)["awaiting_transfer"] = {"plan_id": pid, "method": "ru_card"}
        await userdb.save_pending_transfer(uid, pid, "ru_card")
        await q.message.edit_text(
            f"🏦 *Перевод на карту (РФ)*\n\n"
            f"Сумма: *{p['price_rub']} ₽*\n"
            f"Получатель: Максим А.\n\n"
            f"👇 Нажми кнопку ниже — номер карты появится отдельным сообщением, "
            f"его легко скопировать.\n\n"
            f"После перевода *отправь скриншот* чека прямо в этот чат.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Показать номер карты", callback_data=f"show_card_ru_{pid}")],
                [InlineKeyboardButton("← Назад", callback_data=f"plan_{pid}")],
            ]),
        )
        return

    if d.startswith("pay_revolut_"):
        pid = d[12:]
        p   = PLANS[pid]
        get_user(uid)["awaiting_transfer"] = {"plan_id": pid, "method": "revolut"}
        await userdb.save_pending_transfer(uid, pid, "revolut")
        await q.message.edit_text(
            f"🌐 *Оплата Visa Revolut (зарубежный банк)*\n\n"
            f"Сумма: *{p['price_usdt']}$*\n"
            f"Получатель: Maksim A.\n\n"
            f"👇 Нажми кнопку ниже — номер карты появится отдельным сообщением.\n\n"
            f"После перевода *отправь скриншот* чека прямо в этот чат.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💳 Показать номер карты", callback_data=f"show_card_rev_{pid}")],
                [InlineKeyboardButton("← Назад", callback_data=f"plan_{pid}")],
            ]),
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

    # ── Кнопки владельца для подтверждения/отклонения перевода ──
    if d.startswith("owner_confirm_"):
        if uid != OWNER_ID:
            return
        parts = d.split("_")  # owner_confirm_UID_PLAN
        target_uid = int(parts[2])
        plan_id = parts[3]
        apply_plan(target_uid, plan_id)
        await userdb.delete_pending_transfer(target_uid)
        p = PLANS[plan_id]
        # Update caption without parse_mode to avoid Markdown errors
        try:
            await q.message.edit_caption(caption=q.message.caption + "\n\n✅ ПОДТВЕРЖДЕНО")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                target_uid,
                f"🎉 Оплата подтверждена!\n\n"
                f"{p['emoji']} {p['name']} активирован.\n\n"
                f"Присылай фото товаров!",
            )
        except Exception:
            pass
        await q.answer("✅ Баланс пополнен!", show_alert=True)
        return

    if d.startswith("owner_revoke_"):
        if uid != OWNER_ID:
            return
        parts = d.split("_")  # owner_revoke_UID_PLAN
        target_uid = int(parts[2])
        plan_id = parts[3]
        p = PLANS[plan_id]
        # Remove credits that were auto-granted
        u = get_user(target_uid)
        if p["credits"] == -1:
            u["unlimited_until"] = None
        else:
            u["credits"] = max(0, u.get("credits", 0) - p["credits"])
        if p.get("seo_credits", 0) > 0:
            u["seo_credits"] = max(0, u.get("seo_credits", 0) - p["seo_credits"])
        try:
            await q.message.edit_caption(caption=q.message.caption + "\n\n↩️ ОТОЗВАНО")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                target_uid,
                "⚠️ Оплата отозвана.\n\n"
                "Скриншот не прошёл проверку. Обратись в поддержку /help или выбери другой способ оплаты /buy.",
            )
        except Exception:
            pass
        await q.answer("↩️ Баланс отозван!", show_alert=True)
        return

    if d.startswith("owner_reject_"):
        if uid != OWNER_ID:
            return
        target_uid = int(d.split("_")[2])
        await userdb.delete_pending_transfer(target_uid)
        try:
            await q.message.edit_caption(caption=q.message.caption + "\n\n❌ ОТКЛОНЕНО")
        except Exception:
            pass
        try:
            await context.bot.send_message(
                target_uid,
                "❌ Оплата не подтверждена.\n\n"
                "Попробуй отправить скриншот ещё раз или выбери другой способ оплаты /buy.",
            )
        except Exception:
            pass
        await q.answer("❌ Отклонено", show_alert=True)
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
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    stars_paid = payment.total_amount
    tg_payment_id = payment.telegram_payment_charge_id

    if payload.startswith("plan_"):
        # Plan paid via Stars (from payment_keyboard → stars_buy_ callback)
        raw = payload  # "plan_start:123456" or "plan_start"
        parts = raw.split(":")
        plan_key = parts[0][5:]  # strip "plan_"
        apply_plan(uid, plan_key)
        p = PLANS[plan_key]
        await userdb.save_stars_transaction(uid, plan_key, stars_paid, tg_payment_id)
        await update.message.reply_text(
            f"🎉 *Оплата прошла!* {p['emoji']} *{p['name']}* активирован.\n\n"
            f"{credits_display(uid)}\n\nПрисылай фото товаров!",
            parse_mode="Markdown",
        )

    elif ":" in payload:
        # PRODUCTS Stars payment: "product_id:user_id"
        parts = payload.split(":")
        product_id = parts[0]
        product = PRODUCTS.get(product_id)
        if not product:
            return

        # Add infographic credits to in-memory store
        inf_credits = product["credits_infographic"]
        ana_credits = product["credits_analysis"]
        if inf_credits > 0:
            u = get_user(uid)
            u["credits"] = u.get("credits", 0) + inf_credits
        if ana_credits > 0:
            await userdb.add_analytics_credits(uid, ana_credits)

        await userdb.save_stars_transaction(uid, product_id, stars_paid, tg_payment_id)

        # Referral reward (30%)
        referrer_id = await userdb.get_referrer(uid)
        if referrer_id:
            reward = max(1, int(stars_paid * 0.30))
            await userdb.add_referral_reward(referrer_id, uid, stars_paid, reward)
            try:
                total = await userdb.get_total_referral_reward(referrer_id)
                await context.bot.send_message(
                    referrer_id,
                    f"🎉 Твой реферал купил пакет!\n"
                    f"Ты получил бонус: *{reward} ⭐*\n"
                    f"Всего заработано: *{total} ⭐*\n\n"
                    f"Вывести: /withdraw",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        lines = []
        if inf_credits:
            lines.append(f"🎨 Инфографик: +{inf_credits}")
        if ana_credits:
            lines.append(f"📊 Анализов: +{ana_credits}")
        await update.message.reply_text(
            f"✅ *Оплата получена!* {stars_paid} ⭐\n\n"
            f"Начислено:\n" + "\n".join(lines) + f"\n\n{credits_display(uid)}",
            parse_mode="Markdown",
        )


# ── РЕФЕРАЛЬНАЯ СИСТЕМА ───────────────────────────────────────────────────────────
async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await userdb.register_user(uid)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=REF_{uid}"
    total = await userdb.get_total_referral_reward(uid)
    withdrawable = await userdb.get_withdrawable_stars(uid)
    await update.message.reply_text(
        f"🤝 *Реферальная программа*\n\n"
        f"Приглашай друзей и получай *30% с каждой их покупки* в Telegram Stars.\n\n"
        f"Твоя ссылка:\n`{ref_link}`\n\n"
        f"⭐ Заработано всего: *{total} Stars*\n"
        f"💸 Доступно к выводу: *{withdrawable} Stars*\n\n"
        f"Вывод: /withdraw",
        parse_mode="Markdown",
    )


async def cmd_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    withdrawable = await userdb.get_withdrawable_stars(uid)
    if withdrawable < 50:
        await update.message.reply_text(
            f"💸 *Вывод Stars*\n\n"
            f"Доступно: *{withdrawable} ⭐*\n\n"
            f"Минимальная сумма вывода — 50 Stars.\n"
            f"Продолжай приглашать друзей! /ref",
            parse_mode="Markdown",
        )
        return
    await userdb.request_withdrawal(uid, withdrawable)
    if OWNER_ID:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"💸 *Заявка на вывод Stars*\n\n"
                f"User: {user_tag(update.effective_user)} (ID: {uid})\n"
                f"Сумма: *{withdrawable} ⭐*\n\n"
                f"Выплати через Fragment или Telegram.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    await update.message.reply_text(
        f"✅ Заявка на вывод *{withdrawable} ⭐* принята.\n\n"
        f"Обработка в течение 24 часов.",
        parse_mode="Markdown",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    stats = await userdb.get_admin_stats()
    top = "\n".join(f"  #{i+1} user {r[0]}: {r[1]} рефералов" for i, r in enumerate(stats["top_refs"]))
    await update.message.reply_text(
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего пользователей: *{stats['total_users']}*\n"
        f"💳 Всего покупок: *{stats['total_purchases']}*\n"
        f"⭐ Всего Stars: *{stats['total_stars']}*\n\n"
        f"📅 Сегодня покупок: *{stats['today']}*\n"
        f"📅 За неделю: *{stats['week']}*\n\n"
        f"🏆 Топ рефереры:\n{top or '  (нет данных)'}",
        parse_mode="Markdown",
    )


# ── АНАЛИТИКА ─────────────────────────────────────────────────────────────────────
ANALYTICS_PRICES = {
    "nicha": ("Анализ ниши", 149, "🔍"),
    "season": ("Анализ сезонности", 149, "📅"),
    "supplier": ("Поставщики", 199, "🏭"),
    "full": ("Полный анализ", 399, "📦"),
}


def _analytics_buy_keyboard(cmd: str) -> InlineKeyboardMarkup:
    name, price, emoji = ANALYTICS_PRICES[cmd]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💳 Купить за {price}₽", callback_data=f"buy_analytics_{cmd}"),
    ]])


async def _check_analytics_credit(uid: int, cost: int) -> bool:
    if uid == OWNER_ID:
        return True
    bal = await userdb.get_analytics_balance(uid)
    return bal >= cost


async def _use_analytics_credit(uid: int, cost: int):
    if uid == OWNER_ID:
        return
    await userdb.use_analytics_credit(uid, cost)


def _marketplace_keyboard(cmd: str, query: str) -> InlineKeyboardMarkup:
    """Keyboard asking which marketplace to analyze."""
    import urllib.parse
    q = urllib.parse.quote(query)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟣 Wildberries", callback_data=f"ana_{cmd}_wb:{query[:60]}"),
            InlineKeyboardButton("🔵 OZON",        callback_data=f"ana_{cmd}_ozon:{query[:60]}"),
            InlineKeyboardButton("🟠 Amazon",      callback_data=f"ana_{cmd}_amazon:{query[:60]}"),
        ],
        [InlineKeyboardButton("🌐 Все сразу",     callback_data=f"ana_{cmd}_all:{query[:60]}")],
    ])


async def cmd_nicha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "🔍 *Анализ ниши*\n\nИспользование: `/nicha кокосовое масло`",
            parse_mode="Markdown",
        )
        return
    if not await _check_analytics_credit(uid, 1):
        await update.message.reply_text(
            "🔍 *Анализ ниши* — 149₽\n\nКупи кредиты аналитики:",
            parse_mode="Markdown", reply_markup=_analytics_buy_keyboard("nicha"),
        )
        return
    await update.message.reply_text(
        f"🔍 *Анализ ниши:* «{query}»\n\nКакую площадку анализировать?",
        parse_mode="Markdown", reply_markup=_marketplace_keyboard("nicha", query),
    )


async def cmd_season(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    product = " ".join(context.args).strip() if context.args else ""
    if not product:
        await update.message.reply_text(
            "📅 *Анализ товаров + сезонность*\n\nИспользование: `/season пуховик`",
            parse_mode="Markdown",
        )
        return
    if not await _check_analytics_credit(uid, 1):
        await update.message.reply_text(
            "📅 *Анализ сезонности* — 149₽\n\nКупи кредиты аналитики:",
            parse_mode="Markdown", reply_markup=_analytics_buy_keyboard("season"),
        )
        return
    await update.message.reply_text(
        f"📅 *Анализ товаров + сезонность:* «{product}»\n\nКакую площадку учитывать?",
        parse_mode="Markdown", reply_markup=_marketplace_keyboard("season", product),
    )


async def cmd_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    product = " ".join(context.args).strip() if context.args else ""
    if not product:
        await update.message.reply_text(
            "🏭 *Поиск поставщиков*\n\nИспользование: `/supplier кокосовое масло`\n\n"
            "Ищет поставщиков на 1688 и Alibaba с ценами и маржой.",
            parse_mode="Markdown",
        )
        return
    if not await _check_analytics_credit(uid, 1):
        await update.message.reply_text(
            "🏭 *Поиск поставщиков* — 199₽\n\nКупи кредиты аналитики:",
            parse_mode="Markdown", reply_markup=_analytics_buy_keyboard("supplier"),
        )
        return
    # Suppliers always from Chinese platforms — no marketplace choice
    msg = await update.message.reply_text("🏭 Ищу поставщиков «{}» на 1688/Alibaba…".format(product))
    try:
        await _use_analytics_credit(uid, 1)
        supplier_data = await parsers.search_1688(product, product)
        result = await analyze_suppliers(client, product, supplier_data)
        raw = result[0] if isinstance(result, tuple) else result
        text = format_suppliers(product, raw)
        await userdb.log_analysis(uid, product, "supplier", raw)
        await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logging.exception(e)
        await msg.edit_text("❌ Ошибка анализа. Попробуй позже.")


async def cmd_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text(
            "📦 *Полный анализ*\n\nИспользование: `/full кокосовое масло`\n\n"
            "Ниша + сезонность + поставщики (3 кредита).",
            parse_mode="Markdown",
        )
        return
    if not await _check_analytics_credit(uid, 3):
        bal = await userdb.get_analytics_balance(uid)
        await update.message.reply_text(
            f"📦 *Полный анализ* — 399₽\n\nНужно 3 кредита, у тебя: {bal}.",
            parse_mode="Markdown", reply_markup=_analytics_buy_keyboard("full"),
        )
        return
    await update.message.reply_text(
        f"📦 *Полный анализ:* «{query}»\n\nКакую площадку анализировать?",
        parse_mode="Markdown", reply_markup=_marketplace_keyboard("full", query),
    )


async def _run_nicha(uid: int, query: str, marketplace: str, reply_fn):
    """Run niche analysis for chosen marketplace and send result."""
    mkt_label = {"wb": "Wildberries", "ozon": "OZON", "amazon": "Amazon", "all": "WB + OZON + Amazon"}
    msg = await reply_fn(f"🔍 Анализирую нишу «{query}» на {mkt_label.get(marketplace, marketplace)}…")
    try:
        await _use_analytics_credit(uid, 1)
        wb_data = await parsers.search_wb(query) if marketplace in ("wb", "all") else []
        ozon_data = await parsers.search_ozon(query) if marketplace in ("ozon", "all") else []
        raw = await analyze_niche(client, query, wb_data, ozon_data, marketplace)
        text = format_niche(query, raw)
        await userdb.log_analysis(uid, query, f"nicha_{marketplace}", raw)
        await msg.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logging.exception(e)
        await msg.edit_text("❌ Ошибка анализа. Попробуй позже.")


async def _run_full(uid: int, query: str, marketplace: str, update: Update):
    """Run full analysis (nicha + season + supplier) for chosen marketplace."""
    mkt_label = {"wb": "Wildberries", "ozon": "OZON", "amazon": "Amazon", "all": "WB + OZON + Amazon"}
    msg = await update.message.reply_text(
        f"📦 Запускаю полный анализ «{query}» на {mkt_label.get(marketplace, marketplace)}… (~40 сек)"
    )
    try:
        await _use_analytics_credit(uid, 3)
        wb_data = await parsers.search_wb(query) if marketplace in ("wb", "all") else []
        ozon_data = await parsers.search_ozon(query) if marketplace in ("ozon", "all") else []
        supplier_data = await parsers.search_1688(query, query)
        raw_niche = await analyze_niche(client, query, wb_data, ozon_data, marketplace)
        raw_season = await analyze_season(client, query, marketplace)
        supplier_result = await analyze_suppliers(client, query, supplier_data)
        raw_supplier = supplier_result[0] if isinstance(supplier_result, tuple) else supplier_result
        await userdb.log_analysis(uid, query, f"full_{marketplace}", raw_niche)
        await msg.edit_text(format_niche(query, raw_niche), parse_mode="Markdown")
        await update.message.reply_text(format_season(query, raw_season), parse_mode="Markdown")
        await update.message.reply_text(format_suppliers(query, raw_supplier), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        logging.exception(e)
        await msg.edit_text("❌ Ошибка анализа. Попробуй позже.")




async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bal = await userdb.get_analytics_balance(uid)
    u = get_user(uid)
    await update.message.reply_text(
        f"💰 *Твой баланс:*\n\n"
        f"🎨 Карточки: *{credits_display(uid)}*\n"
        f"📊 Аналитика: *{bal} кредит(а)*\n\n"
        f"Купи /buy",
        parse_mode="Markdown",
    )


# ── НАСТРОЙКА БОТА В TELEGRAM ─────────────────────────────────────────────────────
async def setup_bot(app):
    """Устанавливает описание, about и команды бота при запуске."""
    bot = app.bot
    try:
        await bot.set_my_description(
            "🏆 TOP SELLER — сервис для селлеров маркетплейсов\n\n"
            "Что умеет бот:\n"
            "📸 Инфографика — загрузи фото, получи готовую карточку за 30 сек\n"
            "📝 SEO-тексты — заголовки, описания, ключевые слова под алгоритмы\n"
            "🔍 Анализ ниши — конкуренция, цены, лидеры рынка\n"
            "📅 Сезонность — когда заходить, пики и спады спроса\n"
            "🏭 Поставщики — поиск на 1688/Alibaba с ценами и маржой\n\n"
            "Площадки: WB, OZON, Яндекс Маркет, Amazon\n"
            "От карточки до аналитики — всё в одном боте."
        )
        await bot.set_my_short_description(
            "🏆 TOP SELLER — сервис для селлеров: инфографика, SEO-тексты, "
            "аналитика ниш, сезонность, поставщики. Всё в одном боте."
        )
        await bot.set_my_commands([
            ("start",    "Запустить бота"),
            ("buy",      "Купить пакет"),
            ("credits",  "Мой баланс"),
            ("promo",    "Ввести промокод"),
            ("ref",      "Реферальная ссылка"),
            ("withdraw", "Вывести Stars"),
            ("nicha",    "Анализ ниши"),
            ("season",   "Анализ сезонности"),
            ("supplier", "Поставщики на 1688"),
            ("full",     "Полный анализ"),
            ("balance",  "Баланс аналитики"),
        ])
        logging.info("Bot description and commands set.")
    except Exception as e:
        logging.warning(f"Could not set bot info: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────────
def main():
    import asyncio
    asyncio.get_event_loop().run_until_complete(userdb.init_db())
    download_fonts()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(setup_bot).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("buy",       cmd_buy))
    app.add_handler(CommandHandler("credits",   cmd_credits))
    app.add_handler(CommandHandler("promo",     cmd_promo))
    app.add_handler(CommandHandler("addpromo",  cmd_admin_promo))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("confirm",   cmd_confirm))
    app.add_handler(CommandHandler("reject",    cmd_reject))
    app.add_handler(CommandHandler("pending",   cmd_pending))
    app.add_handler(CommandHandler("reply",     cmd_reply))
    app.add_handler(CommandHandler("nicha",     cmd_nicha))
    app.add_handler(CommandHandler("season",    cmd_season))
    app.add_handler(CommandHandler("supplier",  cmd_supplier))
    app.add_handler(CommandHandler("full",      cmd_full))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("ref",       cmd_ref))
    app.add_handler(CommandHandler("withdraw",  cmd_withdraw))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
        if isinstance(context.error, Conflict):
            logging.warning("409 Conflict on startup — old instance still shutting down, retrying...")
            return
        logging.exception(context.error)

    app.add_error_handler(error_handler)
    logging.info("Bot started.")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "pre_checkout_query"],
    )


if __name__ == "__main__":
    main()
