"""GPT-4o analytics functions for marketplace analysis."""

import json
import logging
from datetime import datetime, timezone
from openai import OpenAI

# Uses the same client as bot.py — passed as parameter


async def analyze_niche(client: OpenAI, query: str, wb_data: list, ozon_data: list) -> str:
    """Analyze marketplace niche using parsed data + GPT-4o."""
    data_summary = f"WB товары ({len(wb_data)} шт.):\n"
    for p in wb_data[:15]:
        data_summary += f"- {p['name']} | {p.get('brand','')} | {p['price']}₽ | ★{p.get('rating',0)} | {p.get('feedbacks',0)} отзывов\n"

    if ozon_data:
        data_summary += f"\nOZON товары ({len(ozon_data)} шт.):\n"
        for p in ozon_data[:10]:
            data_summary += f"- {p['name']} | {p['price']}₽\n"

    prompt = f"""Ты эксперт по маркетплейсам WB/OZON/Amazon. Проанализируй нишу по запросу «{query}».

Данные с маркетплейсов:
{data_summary}

{"Если данных мало — используй свои экспертные знания о российском рынке (WB, OZON) и мировом (Amazon)." if len(wb_data) < 5 else ""}

Верни СТРОГО JSON:
{{
  "competition_level": "низкая/средняя/высокая",
  "avg_price": число,
  "price_range": [мин, макс],
  "avg_reviews": число,
  "market_leaders": ["топ-3 бренда"],
  "entry_recommendation": "рекомендуем/осторожно/не рекомендуем",
  "optimal_price": число,
  "key_features": ["топ-5 характеристик у лидеров"],
  "summary": "краткий вывод 2-3 предложения"
}}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
    )
    return response.choices[0].message.content.strip()


async def analyze_season(client: OpenAI, product: str) -> str:
    """Analyze seasonality of a product for Russian market."""
    current_month = datetime.now(timezone.utc).month
    month_names = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
                   "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

    prompt = f"""Ты эксперт по маркетплейсам РФ (WB, OZON, Яндекс Маркет).
Проанализируй сезонность товара: «{product}»
Сейчас месяц #{current_month} ({month_names[current_month]}).

Верни СТРОГО JSON:
{{
  "peak_months": [список номеров месяцев 1-12 когда пик продаж],
  "low_months": [список номеров месяцев когда низкий сезон],
  "current_month_status": "пик/рост/спад/низкий сезон",
  "months_to_peak": число (сколько месяцев до следующего пика от текущего),
  "should_enter_now": true/false,
  "reasoning": "объяснение 2-3 предложения",
  "preparation_advice": "что делать прямо сейчас",
  "yearly_trend": "растущий/стабильный/падающий"
}}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=600,
    )
    return response.choices[0].message.content.strip()


async def analyze_suppliers(client: OpenAI, product: str, supplier_data: list) -> str:
    """Analyze supplier data from 1688/Alibaba."""
    # First translate product to Chinese and English
    translate_resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content":
            f"Переведи товар «{product}» на:\n1. Китайский (иероглифы)\n2. Английский\n"
            f"Формат: CN: иероглифы\nEN: english"}],
        max_tokens=50,
    )
    translations = translate_resp.choices[0].message.content.strip()

    data_str = ""
    if supplier_data:
        data_str = "Найденные поставщики:\n"
        for s in supplier_data[:10]:
            data_str += (f"- {s.get('name','')} | "
                        f"${s.get('price_usd_min',0)}-${s.get('price_usd_max',0)} | "
                        f"MOQ: {s.get('min_order','?')}\n")

    prompt = f"""Ты эксперт по закупкам в Китае для продажи на WB/OZON.
Товар: «{product}»
Переводы: {translations}

{data_str if data_str else "Данные с площадок недоступны — используй экспертные знания о типичных ценах и поставщиках для этого типа товара."}

Верни СТРОГО JSON:
{{
  "translations": {{"cn": "иероглифы", "en": "english"}},
  "suppliers": [
    {{
      "name": "описание поставщика/товара",
      "price_usd": число (средняя цена),
      "price_rub": число (умножь на 95),
      "min_order": "минимальный заказ",
      "estimated_margin_wb": "примерная маржа если продавать на WB по средней цене",
      "recommendation": "краткий комментарий"
    }}
  ],
  "logistics_note": "заметка о логистике (карго ~300-500₽/кг, 25-35 дней)",
  "search_links": [
    "https://s.1688.com/selloffer/offer_search.htm?keywords=КИТАЙСКИЙ_ЗАПРОС",
    "https://www.alibaba.com/trade/search?SearchText=ENGLISH_QUERY"
  ],
  "total_advice": "общий совет 2-3 предложения"
}}"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )
    return response.choices[0].message.content.strip(), translations


def format_niche(query: str, raw_json: str) -> str:
    """Format niche analysis into a beautiful Telegram message."""
    import re
    raw = re.sub(r"```json|```", "", raw_json).strip()
    try:
        d = json.loads(raw)
    except Exception:
        return f"🔍 *Анализ ниши: «{query}»*\n\n{raw_json}"

    comp_emoji = {"низкая": "🟢", "средняя": "🟡", "высокая": "🔴"}.get(d.get("competition_level", ""), "⚪")
    rec_emoji = {"рекомендуем": "✅", "осторожно": "⚠️", "не рекомендуем": "🚫"}.get(d.get("entry_recommendation", ""), "❓")

    pr = d.get("price_range", [0, 0])
    leaders = ", ".join(d.get("market_leaders", []))
    features = "\n".join(f"  • {f}" for f in d.get("key_features", []))

    return (
        f"🔍 *Анализ ниши: «{query}»*\n\n"
        f"{comp_emoji} Конкуренция: *{d.get('competition_level', '?')}*\n"
        f"💰 Средняя цена: *{d.get('avg_price', '?')} ₽* (диапазон: {pr[0]}–{pr[1]} ₽)\n"
        f"⭐ Средние отзывы: *{d.get('avg_reviews', '?')}*\n"
        f"🏆 Лидеры: {leaders}\n\n"
        f"{rec_emoji} Рекомендация: *{d.get('entry_recommendation', '?').upper()}*\n"
        f"💡 Оптимальная цена входа: *{d.get('optimal_price', '?')} ₽*\n\n"
        f"🎯 Ключевые фишки лидеров:\n{features}\n\n"
        f"📝 {d.get('summary', '')}"
    )


def format_season(product: str, raw_json: str) -> str:
    """Format seasonality analysis with emoji graph."""
    import re
    raw = re.sub(r"```json|```", "", raw_json).strip()
    try:
        d = json.loads(raw)
    except Exception:
        return f"📅 *Сезонность: «{product}»*\n\n{raw_json}"

    months = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
              "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    peaks = set(d.get("peak_months", []))
    lows = set(d.get("low_months", []))
    current = datetime.now(timezone.utc).month

    graph = ""
    for i in range(1, 13):
        marker = "▶️" if i == current else "  "
        if i in peaks:
            bar = "🟩🟩🟩🟩🟩"
        elif i in lows:
            bar = "🟥"
        else:
            bar = "🟨🟨🟨"
        graph += f"{marker} {months[i-1]}: {bar}\n"

    status_emoji = {"пик": "🔥", "рост": "📈", "спад": "📉", "низкий сезон": "❄️"}.get(
        d.get("current_month_status", ""), "📊")
    enter_emoji = "✅" if d.get("should_enter_now") else "⏳"

    return (
        f"📅 *Сезонность: «{product}»*\n\n"
        f"{status_emoji} Сейчас: *{d.get('current_month_status', '?')}*\n"
        f"⏱ До пика: *{d.get('months_to_peak', '?')} мес.*\n"
        f"{enter_emoji} Входить сейчас: *{'Да' if d.get('should_enter_now') else 'Подождать'}*\n"
        f"📈 Годовой тренд: *{d.get('yearly_trend', '?')}*\n\n"
        f"*График сезонности:*\n{graph}\n"
        f"🟩 пик  🟨 средне  🟥 низкий  ▶️ сейчас\n\n"
        f"💡 {d.get('reasoning', '')}\n\n"
        f"🎯 *Что делать:* {d.get('preparation_advice', '')}"
    )


def format_suppliers(product: str, raw_json: str) -> str:
    """Format supplier analysis."""
    import re
    raw = re.sub(r"```json|```", "", raw_json).strip()
    try:
        d = json.loads(raw)
    except Exception:
        return f"🏭 *Поставщики: «{product}»*\n\n{raw_json}"

    suppliers_text = ""
    for i, s in enumerate(d.get("suppliers", [])[:5], 1):
        suppliers_text += (
            f"\n*{i}. {s.get('name', '?')}*\n"
            f"   💵 Цена: ~${s.get('price_usd', '?')} (~{s.get('price_rub', '?')} ₽)\n"
            f"   📦 Мин. заказ: {s.get('min_order', '?')}\n"
            f"   📊 Маржа на WB: {s.get('estimated_margin_wb', '?')}\n"
            f"   💬 {s.get('recommendation', '')}\n"
        )

    links = d.get("search_links", [])
    links_text = "\n".join(f"  🔗 {l}" for l in links) if links else ""

    translations = d.get("translations", {})
    tr_text = ""
    if translations:
        tr_text = f"🇨🇳 {translations.get('cn', '')} | 🇬🇧 {translations.get('en', '')}\n\n"

    return (
        f"🏭 *Поставщики: «{product}»*\n\n"
        f"{tr_text}"
        f"*Найденные варианты:*{suppliers_text}\n"
        f"🚚 {d.get('logistics_note', '')}\n\n"
        f"*Ссылки для поиска:*\n{links_text}\n\n"
        f"📝 {d.get('total_advice', '')}"
    )
