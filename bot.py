import os
import json
import re
import base64
import logging
import random
import urllib.request
import asyncio
import hmac
import hashlib
import httpx
try:
    import aiohttp.web
    _AIOHTTP = True
except ImportError:
    _AIOHTTP = False
    logging.warning("aiohttp not installed — REST API disabled")
from urllib.parse import parse_qsl
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
PORT               = int(os.getenv("PORT", "8080"))
MINI_APP_URL       = os.getenv("MINI_APP_URL", "https://ai-infografika-bot-clean.vercel.app")
WH_URL             = os.getenv("WEBHOOK_URL", "")  # set on Railway for webhook mode

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

# Шрифты: Playfair Display (serif заголовки), Montserrat (callouts), Roboto/NotoSans (fallback)
_FONT_SOURCES = [
    # Playfair Display (multiple mirrors — Google Fonts repo reorganized static fonts)
    ("serif_bold",   f"{FONT_DIR}/PlayfairDisplay-Bold.ttf", [
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/playfairdisplay/static/PlayfairDisplay-Bold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/playfairdisplay/static/PlayfairDisplay-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/clauseggers/Playfair-Display@master/fonts/PlayfairDisplay-Bold.ttf",
    ]),
    # NotoSerif Bold — надёжный serif с кириллицей (тот же источник что NotoSans)
    ("serif_noto",   f"{FONT_DIR}/NotoSerif-Bold.ttf", [
        "https://github.com/notofonts/noto-fonts/raw/main/hinted/ttf/NotoSerif/NotoSerif-Bold.ttf",
    ]),
    # Montserrat Bold (multiple mirrors)
    ("callout_bold", f"{FONT_DIR}/Montserrat-Bold.ttf", [
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/montserrat/static/Montserrat-Bold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-Bold.ttf",
        "https://cdn.jsdelivr.net/gh/JulietaUla/Montserrat@master/fonts/ttf/Montserrat-Bold.ttf",
    ]),
    # Montserrat SemiBold
    ("callout_semi", f"{FONT_DIR}/Montserrat-SemiBold.ttf", [
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/montserrat/static/Montserrat-SemiBold.ttf",
        "https://github.com/google/fonts/raw/main/ofl/montserrat/static/Montserrat-SemiBold.ttf",
        "https://cdn.jsdelivr.net/gh/JulietaUla/Montserrat@master/fonts/ttf/Montserrat-SemiBold.ttf",
    ]),
    ("bold",    f"{FONT_DIR}/Roboto-Bold.ttf", [
        "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Bold.ttf",
    ]),
    ("regular", f"{FONT_DIR}/Roboto-Regular.ttf", [
        "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Regular.ttf",
    ]),
    ("bold_fallback",    f"{FONT_DIR}/NotoSans-Bold.ttf", [
        "https://github.com/notofonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Bold.ttf",
    ]),
    ("regular_fallback", f"{FONT_DIR}/NotoSans-Regular.ttf", [
        "https://github.com/notofonts/noto-fonts/raw/main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
    ]),
]
_SYSTEM_FONTS = {
    "serif": [
        f"{FONT_DIR}/PlayfairDisplay-Bold.ttf",
        f"{FONT_DIR}/NotoSerif-Bold.ttf",
        f"{FONT_DIR}/Roboto-Bold.ttf",
        f"{FONT_DIR}/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    "callout": [
        f"{FONT_DIR}/Montserrat-Bold.ttf",
        f"{FONT_DIR}/Montserrat-SemiBold.ttf",
        f"{FONT_DIR}/Roboto-Bold.ttf",
        f"{FONT_DIR}/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
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
    for name, path, urls in _FONT_SOURCES:
        # Скачиваем если файл отсутствует или битый
        if os.path.exists(path) and _is_valid_font(path):
            logging.info(f"Font OK: {path}")
            continue
        # Удаляем битый файл если есть
        if os.path.exists(path):
            os.remove(path)
        # Пробуем все URL по очереди
        success = False
        for url in urls:
            try:
                urllib.request.urlretrieve(url, path)
                if _is_valid_font(path):
                    logging.info(f"Font downloaded and verified: {path} (from {url})")
                    success = True
                    break
                else:
                    if os.path.exists(path):
                        os.remove(path)
                    logging.warning(f"Font downloaded but failed Cyrillic check: {path}")
            except Exception as e:
                logging.warning(f"Font download failed ({name}) from {url}: {e}")
        if not success:
            logging.warning(f"All URLs failed for font: {name}")


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
  "title": "ЗАГОЛОВОК КРУПНО (макс 26 символов, КАПСЛОК, ёмко и продающе)",
  "subtitle": "Подзаголовок / УТП (макс 42 символа, может содержать бренд и аромат)",
  "features": [
    "ПРЕИМУЩЕСТВО 1 (макс 20 символов, КАПСЛОК, с цифрами если есть)",
    "ПРЕИМУЩЕСТВО 2 (макс 20 символов, КАПСЛОК)",
    "ПРЕИМУЩЕСТВО 3 (макс 20 символов, КАПСЛОК)",
    "ПРЕИМУЩЕСТВО 4 (макс 20 символов, КАПСЛОК)",
    "ПРЕИМУЩЕСТВО 5 (макс 20 символов, КАПСЛОК)"
  ],
  "badge": "ХИТ | НОВИНКА | БЕСТСЕЛЛЕР | ТОП",
  "color_theme": "warm | cool | neutral | dark",
  "scene_description": "EXTREMELY DETAILED English description of a DARK MOODY lifestyle scene. Dark wooden table or dark surface. Rich props specific to the product. Example for a candle: 'very dark walnut wooden table, scattered dried orange slices, cinnamon sticks, star anise, pine cones, fresh pine branches, abundant warm golden fairy lights bokeh in dark background'. ALWAYS specific, ALWAYS dark and atmospheric — never bright or white.",
  "scene_props": ["prop1 English", "prop2", "prop3", "prop4", "prop5", "prop6", "prop7", "prop8"]
}}
{user_hint_block}
Правила:
- Пиши по-русски (кроме scene_description и scene_props — ТОЛЬКО на АНГЛИЙСКОМ)
- features — РОВНО 5 штук, ВСЕ КАПСЛОКОМ, УНИКАЛЬНЫЕ, конкретные
- Примеры хороших features для свечи: "ГОРИТ 25 ЧАСОВ", "КОКОСОВЫЙ ВОСК", "ДЕРЕВЯННЫЙ ФИТИЛЬ", "ЭФИРНЫЕ МАСЛА", "УЮТНЫЙ АРОМАТ"
- НИКОГДА не повторяй одно преимущество
- scene_description — тёмная атмосферная сцена на английском, 6-8 конкретных реквизитов
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


# ── ПАЛИТРЫ И РЕКВИЗИТ ПО АРОМАТУ / ТИПУ ТОВАРА ─────────────────────────────────
SCENT_PALETTES = {
    "orange_cinnamon": {
        "bg": "#F5DEB3", "title": "#1B3A2D", "arrow": "#3D2B1F", "accent": "#C8500A",
        "props": "dried orange slices, cinnamon sticks, star anise, pine branches, pine cones, warm fairy lights",
        "keywords": ["апельсин", "корица", "orange", "cinnamon", "пряности", "пряник"],
    },
    "lavender": {
        "bg": "#EDE0F5", "title": "#2D1B4E", "arrow": "#4A2C6B", "accent": "#8B5CF6",
        "props": "lavender sprigs, purple wildflowers, soft linen cloth, dried herbs, ceramic vase",
        "keywords": ["лаванда", "lavender", "фиолетов", "herb"],
    },
    "vanilla": {
        "bg": "#FFF8E7", "title": "#3D2B00", "arrow": "#6B4423", "accent": "#C49A3C",
        "props": "vanilla pods, honey drizzle, cream ribbons, gold spoon, beeswax, warm light",
        "keywords": ["ваниль", "vanilla", "мёд", "honey", "молоко"],
    },
    "floral": {
        "bg": "#FFF0F5", "title": "#4A1942", "arrow": "#8B2252", "accent": "#E91E8C",
        "props": "fresh roses, peony petals, eucalyptus, white linen, glass vase, gold scissors",
        "keywords": ["роза", "пион", "пион", "роз", "цветок", "floral", "rose", "peony"],
    },
    "forest": {
        "bg": "#E8F5E9", "title": "#1B3A2D", "arrow": "#2D5A3D", "accent": "#388E3C",
        "props": "pine cones, moss, fir branches, cedar bark, forest mushrooms, morning dew drops",
        "keywords": ["хвоя", "лес", "кедр", "сосна", "pine", "cedar", "forest", "мох"],
    },
    "citrus": {
        "bg": "#FFFDE7", "title": "#3E2700", "arrow": "#E65100", "accent": "#FF8F00",
        "props": "lemon slices, lime wedges, grapefruit halves, orange zest, mint leaves, sunny light",
        "keywords": ["лимон", "лайм", "грейпфрут", "цитрус", "lemon", "lime", "citrus"],
    },
    "default": {
        "bg": "#FAF6F0", "title": "#1B3A2D", "arrow": "#3D2B1F", "accent": "#C49A3C",
        "props": "natural wooden surface, soft fabric, warm bokeh lights, elegant lifestyle props",
        "keywords": [],
    },
}


def detect_scent_palette(data: dict) -> dict:
    """Определяет палитру по заголовку, подзаголовку и описанию сцены."""
    haystack = " ".join([
        data.get("title", ""), data.get("subtitle", ""),
        data.get("scene_description", ""), " ".join(data.get("scene_props", [])),
    ]).lower()
    for key, palette in SCENT_PALETTES.items():
        if key == "default":
            continue
        if any(kw in haystack for kw in palette["keywords"]):
            return palette
    return SCENT_PALETTES["default"]


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
    props    = data.get("scene_props", [])

    feat_text = "\n".join(f"  «{f}»" for f in features)
    props_text = ", ".join(props) if props else "natural lifestyle props matching this product"

    # Если пользователь указал пожелания — добавляем к сцене
    if user_caption:
        scene = f"{user_caption}. {scene}"

    palette = detect_scent_palette(data)
    style = random.randint(0, 3)

    # Shared core — DARK ATMOSPHERIC SCENE, product as hero, no text overlays
    CORE_RULES = f"""HIGH-END PRODUCT PHOTOGRAPHY for a premium marketplace listing.

CRITICAL RULES — NO EXCEPTIONS:
• ZERO text, ZERO arrows, ZERO labels, ZERO watermarks anywhere in the image
• Text will be added separately — render ONLY product + environment
• Photorealistic — NOT illustration, NOT cartoon, NOT flat design

PRODUCT (the star of the image):
• The product from the input photo must appear EXACTLY — same shape, same label text, same proportions
• Place it CENTERED, large, occupying 50-60% of the image height
• Position it slightly below center (product bottom at ~85% of image height)
• Sharp focus, studio-quality rendering, enhanced lighting on the product itself
• The label on the product must be CLEARLY READABLE — crisp, sharp, legible

COMPOSITION — leave space for text overlays:
• Top 25% of image: mostly dark/blurred background — this area gets the title text
• Left and right edges: clear enough for callout text (not too busy)
• Center: the product, dominant and sharp

SCENE & PROPS: {scene}
SPECIFIC PROPS TO SCATTER: {props_text}
Scatter these props abundantly in corners and around the product — NOT covering it.
Every corner filled. Rich, layered, professional lifestyle photography."""

    if style == 0:
        prompt = f"""{CORE_RULES}

LIGHTING & MOOD: Dark, moody, atmospheric luxury.
Surface: dark aged wood or very dark walnut table surface.
Background: very dark with multiple warm golden bokeh circles (out-of-focus fairy lights).
Color palette: deep chocolate brown, near-black, rich gold bokeh highlights, amber warm tones.
The product is the brightest element — warm spotlight lighting on it from above-front.
Mood: high-end artisan brand, luxury candle/cosmetics shoot, Vogue-quality dark editorial."""

    elif style == 1:
        prompt = f"""{CORE_RULES}

LIGHTING & MOOD: Dark romantic with warm fairy lights.
Surface: dark wooden table, slightly worn, warm grain visible.
Background: very dark with abundant warm fairy light bokeh — many golden circles.
Color palette: near-black background, deep warm brown, gold bokeh, amber.
Product lit from front with soft warm light — glowing, inviting.
Mood: cozy luxury, premium gift, romantic dark atmosphere."""

    elif style == 2:
        prompt = f"""{CORE_RULES}

LIGHTING & MOOD: Dark botanical luxury.
Surface: dark slate or very dark marble with subtle texture.
Background: deep dark with scattered warm bokeh, botanical elements in shadow.
Color palette: charcoal, deep forest green accents, gold metallic reflections, warm amber bokeh.
Soft dramatic lighting illuminates the product from one side.
Mood: premium spa brand, botanical apothecary, luxury dark editorial."""

    else:
        prompt = f"""{CORE_RULES}

LIGHTING & MOOD: Dark festive premium.
Surface: dark rustic wood with visible grain texture.
Background: deep dark background with masses of warm golden bokeh fairy lights.
Color palette: near-black, deep brown, rich gold, burgundy accents, warm amber.
Product dramatically lit — warm glow highlighting label and shape.
Mood: premium holiday gift, luxury seasonal collection, high-end marketplace hero image."""

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


# ── BEZIER CURVED ARROW ───────────────────────────────────────────────────────────
def draw_curved_arrow(draw, start, end, color=(40, 40, 40, 210), width=3, curvature=0.28):
    """Рисует плавную bezier-стрелку с наконечником."""
    import math
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    dx, dy = ex - sx, ey - sy
    dist = math.sqrt(dx * dx + dy * dy)
    if dist < 5:
        return
    # Контрольная точка — смещение перпендикулярно середине
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    perp_x = -dy / dist * dist * curvature
    perp_y =  dx / dist * dist * curvature
    ctrl = (mx + perp_x, my + perp_y)
    # Генерация точек кривой
    steps = 48
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * ctrl[0] + t ** 2 * ex
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * ctrl[1] + t ** 2 * ey
        pts.append((int(x), int(y)))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=color, width=width)
    # Наконечник стрелки
    if len(pts) >= 4:
        adx = pts[-1][0] - pts[-4][0]
        ady = pts[-1][1] - pts[-4][1]
        alen = math.sqrt(adx * adx + ady * ady)
        if alen > 0:
            adx, ady = adx / alen, ady / alen
            tip = (int(ex), int(ey))
            p1 = (int(ex - 11 * adx + 6 * ady), int(ey - 11 * ady - 6 * adx))
            p2 = (int(ex - 11 * adx - 6 * ady), int(ey - 11 * ady + 6 * adx))
            draw.polygon([tip, p1, p2], fill=color)


def _callout_text_block(draw, x, y, text, font, color, shadow_color, align="left", max_w=220):
    """Рисует текстовый блок callout с тенью, поддержка left/right выравнивания."""
    lines = wrap(text, font, max_w)
    lh = text_h(font) + 5
    for i, line in enumerate(lines[:2]):
        lx = x
        if align == "right":
            bw = int(font.getlength(line))
            lx = x - bw
        sx, sy = lx + 2, y + i * lh + 2
        draw.text((sx, sy), line, font=font, fill=(*shadow_color, 120))
        draw.text((lx, y + i * lh), line, font=font, fill=color)


def _shadow_text(draw, x, y, text, font, fill=(255, 255, 255, 255)):
    """Рисует текст с многослойной тенью для максимальной читаемости на любом фоне."""
    # Толстая тёмная тень (несколько смещений)
    for dx, dy in [(-3,-3),(-3,3),(3,-3),(3,3),(0,4),(0,-4),(4,0),(-4,0)]:
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 180))
    # Основной текст белый
    draw.text((x, y), text, font=font, fill=fill)


def _render_classic(canvas: "Image.Image", title: str, subtitle: str,
                    features: list, badge: str, palette: dict) -> "Image.Image":
    """
    Современный шаблон — стиль премиум маркетплейс:
    - Крупный БЕЛЫЙ serif заголовок ALL CAPS сверху
    - Cream подзаголовок
    - 5 callouts: БЕЛЫЙ текст ALL CAPS, крупный (48px), без фоновых коробок
    - Тонкие белые bezier стрелки (2px)
    - Многослойная тень для читаемости на тёмном фоне
    """
    canvas = canvas.copy()
    draw = ImageDraw.Draw(canvas)

    WHITE   = (255, 255, 255, 255)
    CREAM   = (255, 245, 220, 255)   # тёплый кремовый для подзаголовка
    ARROW_C = (255, 255, 255, 220)   # белые полупрозрачные стрелки

    # ── ЗАГОЛОВОК — белый serif, ALL CAPS, крупный ────────────────────────────
    f_title = None
    title_lines = []
    title_upper = title.upper()
    title_max_w = W - MARGIN * 2 - 20
    for size in (96, 82, 70, 58, 48):
        ft = get_font("serif", size)
        lines = wrap(title_upper, ft, title_max_w)
        if len(lines) <= 2:
            f_title, title_lines = ft, lines
            break
    if not f_title:
        f_title = get_font("serif", 48)
        title_lines = wrap(title_upper, f_title, title_max_w)

    ty = 32
    th = text_h(f_title)
    for line in title_lines[:2]:
        x = centered_x(line, f_title)
        _shadow_text(draw, x, ty, line, f_title, fill=WHITE)
        ty += th + 6

    # ── ПОДЗАГОЛОВОК — кремовый, средний ──────────────────────────────────────
    if subtitle:
        f_sub = get_font("callout", 36)
        ty += 6
        for line in wrap(subtitle, f_sub, title_max_w)[:2]:
            x = centered_x(line, f_sub)
            _shadow_text(draw, x, ty, line, f_sub, fill=CREAM)
            ty += text_h(f_sub) + 4

    # ── 5 CALLOUT POSITIONS — вокруг товара ───────────────────────────────────
    # Товар: центрирован, нижний край ~85% высоты, высота ~55% канваса
    # Края: left ~22%, right ~78%, top ~30%, bottom ~85%
    prod_cx    = W // 2
    prod_top   = int(H * 0.30)
    prod_bot   = int(H * 0.85)
    prod_mid_y = (prod_top + prod_bot) // 2
    prod_left  = int(W * 0.22)
    prod_right = int(W * 0.78)

    feat_list = (features + [""] * 5)[:5]
    # ALL CAPS для каждого
    feat_list = [f.upper() for f in feat_list]

    f_c = get_font("callout", 46)   # крупный размер как у конкурента
    MAX_W = 210                      # максимальная ширина текста callout
    PAD_L = 28                       # отступ от левого края
    PAD_R = W - 28                   # отступ от правого края

    callouts = [
        # 0: левый верх
        {"text": feat_list[0],
         "tx": PAD_L, "ty": int(H * 0.34), "align": "left",
         "tip": (prod_left,  int(H * 0.44))},
        # 1: центр верх (выше товара, над фитилём/верхушкой)
        {"text": feat_list[1],
         "tx": W // 2, "ty": int(H * 0.24), "align": "center",
         "tip": (W // 2,  prod_top + 20)},
        # 2: правый верх
        {"text": feat_list[2],
         "tx": PAD_R, "ty": int(H * 0.34), "align": "right",
         "tip": (prod_right, int(H * 0.44))},
        # 3: левый низ
        {"text": feat_list[3],
         "tx": PAD_L, "ty": int(H * 0.64), "align": "left",
         "tip": (prod_left,  int(H * 0.72))},
        # 4: правый низ
        {"text": feat_list[4],
         "tx": PAD_R, "ty": int(H * 0.64), "align": "right",
         "tip": (prod_right, int(H * 0.72))},
    ]

    for c in callouts:
        if not c["text"].strip():
            continue
        tx, ty_c = c["tx"], c["ty"]
        align = c["align"]
        lines_c = wrap(c["text"], f_c, MAX_W)[:2]
        if not lines_c:
            continue

        lh = text_h(f_c)
        block_w = max(int(f_c.getlength(l)) for l in lines_c)
        block_h = lh * len(lines_c) + 4 * (len(lines_c) - 1)

        # Вычисляем координату начала текста и начало стрелки
        if align == "left":
            text_x = tx
            arrow_sx = min(text_x + block_w + 14, prod_left - 10)
        elif align == "right":
            text_x = tx - block_w
            arrow_sx = max(text_x - 14, prod_right + 10)
        else:  # center
            text_x = tx - block_w // 2
            arrow_sx = tx

        # Гарантируем что текст не выходит за края
        text_x = max(PAD_L, min(text_x, PAD_R - block_w))
        arrow_sy = ty_c + block_h // 2

        # Рисуем строки — БЕЛЫЙ текст с тёмной тенью, без фоновых коробок
        for i, line in enumerate(lines_c):
            ly = ty_c + i * (lh + 4)
            lx = text_x
            if align == "center":
                lw = int(f_c.getlength(line))
                lx = tx - lw // 2
            _shadow_text(draw, lx, ly, line, f_c, fill=WHITE)

        # Тонкая белая bezier стрелка
        draw_curved_arrow(draw,
                          start=(arrow_sx, arrow_sy),
                          end=c["tip"],
                          color=ARROW_C, width=2)

    # ── БЕЙДЖ (ХИТ / НОВИНКА) ─────────────────────────────────────────────────
    if badge:
        f_badge = get_font("callout", 26)
        btxt = f"  {badge}  "
        bw = int(f_badge.getlength(btxt)) + 16
        bh = 44
        # Тёмный полупрозрачный бейдж с accent цветом
        accent_hex = palette["accent"].lstrip("#")
        accent_rgb = tuple(int(accent_hex[i:i+2], 16) for i in (0, 2, 4))
        bl = rlayer(bw, bh, 6, fill=(*accent_rgb, 230))
        ImageDraw.Draw(bl).text((8, (bh - 26) // 2), btxt, font=f_badge,
                                fill=(255, 255, 255, 255))
        paste_a(canvas, bl, (MARGIN, 18))

    return canvas


def _render_minimal(canvas: "Image.Image", title: str, subtitle: str,
                    features: list, badge: str, palette: dict) -> "Image.Image":
    """
    Шаблон МИНИМАЛИЗМ:
    - Светлый/кремовый фон
    - Заголовок сверху по центру
    - Характеристики справа в карточках-бейджах
    - Без стрелок
    """
    canvas = canvas.copy()
    draw = ImageDraw.Draw(canvas)

    title_color   = tuple(int(palette["title"].lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    accent_color  = tuple(int(palette["accent"].lstrip("#")[i:i+2], 16) for i in (0, 2, 4))

    # ── ЗАГОЛОВОК ─────────────────────────────────────────────────────────────
    f_title = None
    title_lines = []
    for size in (82, 70, 58, 48):
        ft = get_font("serif", size)
        lines = wrap(title, ft, W - MARGIN * 2)
        if len(lines) <= 2:
            f_title, title_lines = ft, lines
            break
    if not f_title:
        f_title = get_font("serif", 48)
        title_lines = wrap(title, f_title, W - MARGIN * 2)

    ty = 40
    for line in title_lines[:2]:
        x = centered_x(line, f_title)
        draw.text((x + 2, ty + 2), line, font=f_title, fill=(*title_color, 50))
        draw.text((x, ty), line, font=f_title, fill=(*title_color, 255))
        ty += text_h(f_title) + 8

    if subtitle:
        f_sub = get_font("callout", 30)
        for line in wrap(subtitle, f_sub, W - MARGIN * 2)[:1]:
            x = centered_x(line, f_sub)
            draw.text((x, ty + 4), line, font=f_sub, fill=(*accent_color, 200))
            ty += text_h(f_sub) + 6

    # ── FEATURE БЕЙДЖИ — правая сторона ───────────────────────────────────────
    f_feat = get_font("callout", 32)
    badge_x = W - MARGIN - 280
    badge_y = int(H * 0.38)
    badge_gap = 10

    for feat in features[:4]:
        lines_f = wrap(feat, f_feat, 260)
        feat_h = text_h(f_feat) * len(lines_f[:2]) + 20
        # Карточка
        card = rlayer(286, feat_h + 4, 14,
                      fill=(255, 255, 255, 200),
                      outline=(*accent_color, 80), ow=2)
        paste_a(canvas, card, (badge_x - 3, badge_y - 2))
        # Цветная линия слева
        line_bar = Image.new("RGBA", (5, feat_h), (*accent_color, 220))
        paste_a(canvas, line_bar, (badge_x - 3, badge_y - 2))
        # Текст
        ty_f = badge_y + 10
        for fl in lines_f[:2]:
            draw.text((badge_x + 12, ty_f), fl, font=f_feat,
                      fill=(*title_color, 240))
            ty_f += text_h(f_feat) + 4
        badge_y += feat_h + badge_gap + 8

    return canvas


# ── MAKE INFOGRAPHIC — финальная сборка ──────────────────────────────────────────
def make_infographic(img_path: str, data: dict, scene_bg_path: str | None = None,
                     template: int = 0) -> str:
    """
    Финальная сборка инфографики. Всегда вызывается поверх AI-сцены.
    template 0 = Классика (curved arrows + callout-текст) — ВСЕГДА
    """
    title    = data.get("title", "Товар")
    subtitle = data.get("subtitle", "")
    features = data.get("features", [])[:5]
    badge    = data.get("badge", "")
    palette  = detect_scent_palette(data)

    # ── Фон ──────────────────────────────────────────────────────────────────────
    if scene_bg_path and os.path.exists(scene_bg_path):
        canvas = Image.open(scene_bg_path).convert("RGBA").resize((W, H), Image.LANCZOS)
    else:
        # Fallback: размытый продукт + тёплый кремовый overlay
        raw = Image.open(img_path).convert("RGB")
        rw, rh = raw.size
        sq = min(rw, rh)
        bg = raw.crop(((rw - sq) // 2, (rh - sq) // 2, (rw - sq) // 2 + sq, (rh - sq) // 2 + sq))
        canvas = bg.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(50)).convert("RGBA")
        # Тёплый кремовый overlay вместо тёмного — светлый фон
        bg_hex = palette["bg"].lstrip("#")
        bg_rgb = tuple(int(bg_hex[i:i+2], 16) for i in (0, 2, 4))
        warm_ov = Image.new("RGBA", (W, H), (*bg_rgb, 160))
        canvas = Image.alpha_composite(canvas, warm_ov)

    # ── Применяем шаблон ─────────────────────────────────────────────────────────
    if template == 0:
        canvas = _render_classic(canvas, title, subtitle, features, badge, palette)
    else:
        canvas = _render_minimal(canvas, title, subtitle, features, badge, palette)

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

        # Суммы > 1500₽ — только ручное подтверждение; ≤1500₽ — авто
        amount_rub = p["price_rub"] if method == "ru_card" else int(p["price_usdt"] * 90)
        needs_manual = amount_rub > 1500

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
                f"Сумма > 1500₽ — подтверди вручную."
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
                f"Авто ≤1500₽. Нажми ↩️ если скриншот поддельный."
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

            await msg.edit_text("🎨 Генерирую сцену (20-30 сек)...", reply_markup=stop_kb)
            scene_path = generate_full_infographic(img_path, data, user_caption=user_caption)

            if uid in cancelled_users:
                cancelled_users.discard(uid)
                await msg.edit_text("⛔ Генерация остановлена.")
                return

            # Всегда применяем PIL-постобработку: заголовок + bezier стрелки + callouts
            await msg.edit_text("✏️ Добавляю типографику и стрелки...", reply_markup=stop_kb)
            out_path = make_infographic(img_path, data, scene_bg_path=scene_path)

            # Удаляем промежуточную AI-сцену
            if scene_path and scene_path != out_path:
                try:
                    os.remove(scene_path)
                except OSError:
                    pass

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


# ── MINI APP COMMAND ─────────────────────────────────────────────────────────────
from telegram import WebAppInfo

async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👇 Нажми чтобы открыть панель управления:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Открыть панель управления", web_app=WebAppInfo(url=MINI_APP_URL))
        ]]),
    )


# ── REST API ──────────────────────────────────────────────────────────────────────
_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}


def _json(data: dict, status: int = 200) -> aiohttp.web.Response:
    return aiohttp.web.Response(text=json.dumps(data, ensure_ascii=False), status=status, headers=_CORS)


def validate_telegram_init_data(init_data: str) -> dict | None:
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_value = parsed.pop("hash", None)
    if not hash_value:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, hash_value):
        try:
            return json.loads(parsed.get("user", "{}"))
        except Exception:
            return {}
    return None


async def api_get_user(request: aiohttp.web.Request) -> aiohttp.web.Response:
    uid = int(request.match_info["user_id"])
    u = get_user(uid)
    analytics = await userdb.get_analytics_balance(uid)
    total_ref = await userdb.get_total_referral_reward(uid)
    withdrawable = await userdb.get_withdrawable_stars(uid)
    # count referrals
    import aiosqlite
    async with aiosqlite.connect(userdb.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM referral_rewards WHERE referrer_id=?", (uid,))
        row = await cur.fetchone()
        ref_count = row[0] if row else 0
    return _json({
        "credits_infographic": u.get("credits", 0),
        "credits_analysis": analytics,
        "referral_count": ref_count,
        "referral_earned": total_ref,
        "referral_withdrawable": withdrawable,
        "unlimited_until": u.get("unlimited_until"),
    })


async def api_get_history(request: aiohttp.web.Request) -> aiohttp.web.Response:
    uid = int(request.match_info["user_id"])
    rows = []
    import aiosqlite
    async with aiosqlite.connect(userdb.DB_PATH) as db:
        cur = await db.execute(
            "SELECT product_id, stars, created_at FROM stars_transactions WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (uid,),
        )
        async for r in cur:
            rows.append({"type": "purchase", "product": r[0], "stars": r[1], "date": r[2]})
        cur2 = await db.execute(
            "SELECT query, analysis_type, created_at FROM analysis_log WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (uid,),
        )
        async for r in cur2:
            rows.append({"type": "analysis", "product": r[1], "query": r[0], "stars": 0, "date": r[2]})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return _json({"history": rows[:10]})


async def api_get_stats(request: aiohttp.web.Request) -> aiohttp.web.Response:
    stats = await userdb.get_admin_stats()
    return _json({
        "total_users": stats.get("total_users", 0),
        "infographics_today": stats.get("paid_today", 0),
    })


async def api_validate(request: aiohttp.web.Request) -> aiohttp.web.Response:
    body = await request.json()
    user = validate_telegram_init_data(body.get("init_data", ""))
    if user is None:
        return _json({"ok": False, "error": "invalid"}, status=401)
    return _json({"ok": True, "user": user})


async def api_buy(request: aiohttp.web.Request) -> aiohttp.web.Response:
    body = await request.json()
    uid = body.get("user_id")
    product_id = body.get("product_id")
    if not uid or product_id not in PRODUCTS:
        return _json({"ok": False, "error": "bad_request"}, status=400)
    product = PRODUCTS[product_id]
    try:
        await _bot_app_ref.bot.send_invoice(
            chat_id=uid,
            title=product["title"],
            description=product["description"],
            payload=f"{product_id}:{uid}",
            currency="XTR",
            prices=[LabeledPrice(product["title"], product["stars"])],
        )
        return _json({"ok": True})
    except Exception as e:
        return _json({"ok": False, "error": str(e)}, status=500)


async def handle_options(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(status=204, headers=_CORS)


async def handle_webhook(request: aiohttp.web.Request) -> aiohttp.web.Response:
    data = await request.json()
    update = Update.de_json(data, _bot_app_ref.bot)
    await _bot_app_ref.process_update(update)
    return aiohttp.web.Response(status=200)


# global ref set in main()
_bot_app_ref = None


# ── MAIN ──────────────────────────────────────────────────────────────────────────
def _build_app():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(setup_bot).build()
    for cmd, handler in [
        ("start", cmd_start), ("buy", cmd_buy), ("credits", cmd_credits),
        ("promo", cmd_promo), ("addpromo", cmd_admin_promo), ("broadcast", cmd_broadcast),
        ("stop", cmd_stop), ("confirm", cmd_confirm), ("reject", cmd_reject),
        ("pending", cmd_pending), ("reply", cmd_reply), ("nicha", cmd_nicha),
        ("season", cmd_season), ("supplier", cmd_supplier), ("full", cmd_full),
        ("balance", cmd_balance), ("ref", cmd_ref), ("withdraw", cmd_withdraw),
        ("stats", cmd_stats), ("app", cmd_app),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    async def error_handler(update, context):
        if isinstance(context.error, Conflict):
            logging.warning("409 Conflict — old instance shutting down")
            return
        logging.exception(context.error)

    app.add_error_handler(error_handler)
    return app


async def _async_main():
    global _bot_app_ref
    await userdb.init_db()
    download_fonts()
    app = _build_app()
    _bot_app_ref = app

    # Always start aiohttp so Railway health checks pass on port PORT.
    # Webhook endpoint is added when WH_URL is set; otherwise bot uses polling.
    await app.initialize()

    if WH_URL and _AIOHTTP:
        await app.bot.set_webhook(url=f"{WH_URL}/{TELEGRAM_BOT_TOKEN}")
        await app.start()
        logging.info("Webhook mode active.")
    else:
        await app.bot.delete_webhook()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "pre_checkout_query"],
        )
        logging.info("Polling mode active.")

    if _AIOHTTP:
        web = aiohttp.web.Application()

        async def health(_): return aiohttp.web.Response(text="ok")
        web.router.add_get("/", health)
        web.router.add_get("/health", health)
        web.router.add_get("/api/user/{user_id}", api_get_user)
        web.router.add_get("/api/history/{user_id}", api_get_history)
        web.router.add_get("/api/stats/public", api_get_stats)
        web.router.add_post("/api/validate_user", api_validate)
        web.router.add_post("/api/buy", api_buy)
        if WH_URL:
            web.router.add_post(f"/{TELEGRAM_BOT_TOKEN}", handle_webhook)
        web.router.add_route("OPTIONS", "/{path_info:.*}", handle_options)

        runner = aiohttp.web.AppRunner(web)
        await runner.setup()
        await aiohttp.web.TCPSite(runner, "0.0.0.0", PORT).start()
        logging.info(f"HTTP server on port {PORT}")

    await asyncio.Event().wait()  # keep alive forever


def main():
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
