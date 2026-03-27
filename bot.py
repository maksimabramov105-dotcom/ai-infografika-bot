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


# ── INFOGRAPHIC ───────────────────────────────────────────────────────────────────
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

    # ── Фон: размытое фото товара + цветной оверлей (как у конкурентов) ──────────
    raw = Image.open(img_path).convert("RGB")
    rw, rh = raw.size
    # Кадрируем по центру до квадрата, растягиваем на весь холст
    sq = min(rw, rh)
    bg = raw.crop(((rw-sq)//2, (rh-sq)//2, (rw-sq)//2+sq, (rh-sq)//2+sq))
    bg = bg.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(38))
    # Цветной оверлей — тема warm/cool/neutral/dark
    overlay_alpha = 210 if is_dark else 200
    ov = Image.new("RGBA", (W, H), (*t["bg"], overlay_alpha))
    canvas = Image.alpha_composite(bg.convert("RGBA"), ov)
    draw   = ImageDraw.Draw(canvas)

    # ── Шрифты ───────────────────────────────────────────────────────────────────
    f_title  = get_font("bold",    60)
    f_title2 = get_font("bold",    48)
    f_title3 = get_font("bold",    40)
    f_sub    = get_font("regular", 30)
    f_feat   = get_font("bold",    26)
    f_feat2  = get_font("regular", 24)
    f_badge  = get_font("bold",    26)
    f_cta    = get_font("bold",    26)
    f_num    = get_font("bold",    22)

    # ── Заголовок — авто-размер чтобы влезало в 1-2 строки ──────────────────────
    MARGIN = 40
    for f_t in (f_title, f_title2, f_title3):
        title_lines = wrap(title, f_t, W - MARGIN * 2)
        if len(title_lines) <= 2:
            f_title_use = f_t
            break

    ty = 90
    th = text_h(f_title_use)
    for line in title_lines[:2]:
        x = centered_x(line, f_title_use)
        # Тень для читаемости
        draw.text((x+2, ty+2), line, font=f_title_use, fill=(0, 0, 0, 80))
        draw.text((x, ty), line, font=f_title_use, fill=(*t["text"], 255))
        ty += th + 8

    # Подзаголовок
    ty += 6
    if subtitle:
        sub_lines = wrap(subtitle, f_sub, W - MARGIN * 2)
        for line in sub_lines[:1]:
            x = centered_x(line, f_sub)
            draw.text((x, ty), line, font=f_sub, fill=(*acc2, 230))
            ty += text_h(f_sub) + 4
    ty += 18

    # ── Фото товара (чёткое, по центру) ─────────────────────────────────────────
    # Оставшееся место: от ty до верха feature-блока
    # Feature блок займёт ~280px снизу, badge 80px сверху уже учтён
    available = H - ty - 300
    PHOTO_SIZE = min(420, max(280, available))

    prod = Image.open(img_path).convert("RGBA")
    pw, ph = prod.size
    sq2 = min(pw, ph)
    prod = prod.crop(((pw-sq2)//2, (ph-sq2)//2, (pw-sq2)//2+sq2, (ph-sq2)//2+sq2))
    prod = prod.resize((PHOTO_SIZE, PHOTO_SIZE), Image.LANCZOS)

    px = (W - PHOTO_SIZE) // 2
    py = ty

    # Тень под фото
    sh_img = Image.new("RGBA", (PHOTO_SIZE + 60, PHOTO_SIZE + 60), (0, 0, 0, 0))
    ImageDraw.Draw(sh_img).rounded_rectangle(
        [15, 15, PHOTO_SIZE + 45, PHOTO_SIZE + 45], radius=30, fill=(0, 0, 0, 70)
    )
    sh_img = sh_img.filter(ImageFilter.GaussianBlur(22))
    paste_a(canvas, sh_img, (px - 30, py - 5))

    # Белая карточка под фото
    paste_a(canvas, rlayer(PHOTO_SIZE + 16, PHOTO_SIZE + 16, 26,
                            fill=(*t["card"][:3], 250)), (px - 8, py - 8))

    # Фото с скруглёнными углами
    photo_mask = Image.new("L", (PHOTO_SIZE, PHOTO_SIZE), 0)
    ImageDraw.Draw(photo_mask).rounded_rectangle(
        [0, 0, PHOTO_SIZE, PHOTO_SIZE], radius=22, fill=255
    )
    canvas.paste(prod, (px, py), photo_mask)

    # ── Блоки преимуществ (2×2) ──────────────────────────────────────────────────
    FEAT_TOP = py + PHOTO_SIZE + 24
    COL_W    = (W - MARGIN * 2 - 16) // 2   # ~490
    ROW_H    = min(115, (H - FEAT_TOP - 20) // 2 - 10)
    GAP      = 12
    LEFT_X   = MARGIN
    RIGHT_X  = MARGIN + COL_W + GAP

    # Цвета иконок
    IC = [acc, acc2,
          tuple(min(255, int(c * 1.15)) for c in acc),
          tuple(min(255, int(c * 0.85)) for c in acc2)]

    positions = [
        (LEFT_X,  FEAT_TOP),
        (RIGHT_X, FEAT_TOP),
        (LEFT_X,  FEAT_TOP + ROW_H + GAP),
        (RIGHT_X, FEAT_TOP + ROW_H + GAP),
    ]

    for i, feat in enumerate(features[:4]):
        if i >= len(positions):
            break
        fx, fy = positions[i]
        ic = IC[i % len(IC)]

        # Тень карточки
        paste_a(canvas, rlayer(COL_W + 6, ROW_H + 6, 20, (0, 0, 0, 50), blur=10), (fx - 3, fy + 2))

        # Карточка
        card = rlayer(COL_W, ROW_H, 20,
                      fill=(*t["card"][:3], 248),
                      outline=(*ic, 100), ow=2)
        cd = ImageDraw.Draw(card)

        # Цветная полоска слева
        cd.rounded_rectangle([0, 0, 7, ROW_H], radius=4, fill=(*ic, 255))

        # Круглая иконка с номером (рисуем вручную — без emoji)
        ICON_R = 24
        ICX    = 18 + ICON_R  # центр круга по X
        ICY    = ROW_H // 2
        cd.ellipse([ICX - ICON_R, ICY - ICON_R, ICX + ICON_R, ICY + ICON_R],
                   fill=(*ic, 220))
        num_str = str(i + 1)
        nb = f_num.getbbox(num_str)
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        cd.text((ICX - nw // 2, ICY - nh // 2 - 1), num_str, font=f_num, fill=(255, 255, 255, 255))

        # Текст преимущества
        TEXT_X  = ICX + ICON_R + 14
        max_txt = COL_W - TEXT_X - 12
        lines   = wrap(feat, f_feat, max_txt)
        if not lines:
            lines = [feat[:20]]
        lh = text_h(f_feat) + 4

        # Если не влезает в 2 строки bold — уменьшаем
        if len(lines) > 2:
            lines = wrap(feat, f_feat2, max_txt)

        total_h = min(len(lines), 2) * lh
        tty = (ROW_H - total_h) // 2 - 2

        for line in lines[:2]:
            cd.text((TEXT_X, tty), line, font=f_feat, fill=(*t["text"], 240))
            tty += lh

        paste_a(canvas, card, (fx, fy))

    # ── Бейдж (верх-лево) ─────────────────────────────────────────────────────────
    btxt = f"  {badge}  "
    bw   = int(f_badge.getlength(btxt)) + 18
    bh   = 46
    bl   = rlayer(bw, bh, 12, fill=(210, 40, 40, 255))
    ImageDraw.Draw(bl).text((9, (bh - 26) // 2), btxt, font=f_badge, fill=WHITE)
    paste_a(canvas, bl, (MARGIN, 30))

    # ── CTA (верх-право) ──────────────────────────────────────────────────────────
    ctxt = f"  {cta}  "
    cw   = int(f_cta.getlength(ctxt)) + 22
    ch   = 46
    cl   = rlayer(cw, ch, 14, fill=(*acc, 240), outline=(*acc2, 180), ow=2)
    ImageDraw.Draw(cl).text((11, (ch - 26) // 2), ctxt, font=f_cta, fill=WHITE)
    paste_a(canvas, cl, (W - cw - MARGIN, 30))

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
    out_path = None
    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
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
        for p in [img_path, out_path]:
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
