"""Marketplace parsers: WB, OZON search (public APIs)."""

import httpx
import logging
import json
import re


async def search_wb(query: str) -> list[dict]:
    """Search Wildberries via public catalog API. Returns top-20 products."""
    url = "https://search.wb.ru/exactmatch/ru/common/v5/search"
    params = {
        "appType": 1, "curr": "rub", "query": query,
        "resultset": "catalog", "sort": "popular", "spp": 30,
    }
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(url, params=params, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "application/json",
                })
                r.raise_for_status()
                data = r.json()

            products = data.get("data", {}).get("products", [])[:20]
            results = []
            for p in products:
                results.append({
                    "name": p.get("name", ""),
                    "brand": p.get("brand", ""),
                    "price": p.get("salePriceU", 0) // 100,  # WB returns in kopecks
                    "rating": p.get("rating", 0),
                    "feedbacks": p.get("feedbacks", 0),
                    "id": p.get("id", 0),
                })
            logging.info(f"WB search '{query}': {len(results)} products")
            return results
        except Exception as e:
            logging.warning(f"WB search attempt {attempt+1} failed: {e}")
    return []


async def search_ozon(query: str) -> list[dict]:
    """Search OZON via public page API. Returns top-20 products."""
    url = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2"
    params = {"url": f"/search/?text={query}&from_global=true"}
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(url, params=params, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "application/json",
                })
                r.raise_for_status()
                data = r.json()

            # OZON page JSON structure varies, try to find products
            results = []
            # Navigate nested structure to find search results
            widgets = data.get("widgetStates", {})
            for key, val in widgets.items():
                if "searchResultsV2" in key.lower() or "catalog" in key.lower():
                    try:
                        parsed = json.loads(val) if isinstance(val, str) else val
                        items = parsed.get("items", [])
                        for item in items[:20]:
                            cell = item.get("mainState", [])
                            name = ""
                            price = 0
                            rating = 0
                            reviews = 0
                            for atom in cell:
                                if atom.get("id") == "name":
                                    name = atom.get("atom", {}).get("textAtom", {}).get("text", "")
                                if atom.get("id") == "atom" and "price" in str(atom):
                                    price_text = atom.get("atom", {}).get("textAtom", {}).get("text", "")
                                    nums = re.findall(r"\d+", price_text.replace(" ", ""))
                                    if nums:
                                        price = int(nums[0])
                            if name:
                                results.append({
                                    "name": name, "price": price,
                                    "rating": rating, "feedbacks": reviews,
                                })
                    except Exception:
                        continue
            logging.info(f"OZON search '{query}': {len(results)} products")
            return results
        except Exception as e:
            logging.warning(f"OZON search attempt {attempt+1} failed: {e}")
    return []


async def search_1688(query_cn: str, query_en: str) -> list[dict]:
    """Search 1688.com or Alibaba for suppliers."""
    # Try Alibaba first (more accessible)
    url = f"https://www.alibaba.com/trade/search"
    params = {"SearchText": query_en, "viewtype": "G"}
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                r = await c.get(url, params=params, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "text/html",
                })
                r.raise_for_status()
                # Basic parsing from HTML — look for product JSON in page
                text = r.text
                # Alibaba embeds product data in various ways
                results = []
                # Try to find price patterns
                price_pattern = re.findall(r'\$(\d+\.?\d*)\s*-\s*\$?(\d+\.?\d*)', text)
                name_pattern = re.findall(r'class="elements-title-normal[^"]*"[^>]*>([^<]+)', text)
                moq_pattern = re.findall(r'(\d+)\s*(?:Piece|Set|Pair|Unit)', text, re.IGNORECASE)

                for i in range(min(10, len(name_pattern))):
                    item = {
                        "name": name_pattern[i].strip() if i < len(name_pattern) else "",
                        "price_usd_min": float(price_pattern[i][0]) if i < len(price_pattern) else 0,
                        "price_usd_max": float(price_pattern[i][1]) if i < len(price_pattern) else 0,
                        "min_order": moq_pattern[i] if i < len(moq_pattern) else "1",
                    }
                    if item["name"]:
                        results.append(item)

                logging.info(f"Alibaba search '{query_en}': {len(results)} suppliers")
                return results
        except Exception as e:
            logging.warning(f"Alibaba search attempt {attempt+1} failed: {e}")
    return []
