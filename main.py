import os
import sys
import json
import base64
import asyncio
import re
import math
import time
import socket
import hashlib
import subprocess
import urllib.parse
from datetime import datetime
import httpx
import google.generativeai as genai
from dotenv import load_dotenv

# Загрузка переменных окружения из .env
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

if not GEMINI_API_KEY:
    print("❌ Ошибка: GEMINI_API_KEY не найден в файле .env")
if not TELEGRAM_BOT_TOKEN:
    print("❌ Ошибка: TELEGRAM_BOT_TOKEN не найден в файле .env")

# Настройка Google Gemini
genai.configure(api_key=GEMINI_API_KEY, transport="rest")

# --- Глобальные структуры для защиты от дублирования ответов ---
user_language_prefs = {}
processed_update_ids = {}       # update_id -> timestamp
processed_message_keys = {}     # "chat_id:message_id" -> timestamp
processed_media_groups = {}     # "chat_id:media_group_id" -> timestamp
recent_user_query_hashes = {}   # "chat_id:hash" -> timestamp
in_flight_requests = set()      # активные запросы в процессе обработки
chat_locks = {}                 # chat_id -> asyncio.Lock (строго 1 ответ за раз для чата)

# Приоритетный порядок актуальных моделей при выборе из ListModels
PREFERRED_MODEL_CANDIDATES = [
    "models/gemini-3.8-flash",
    "models/gemini-flash-latest",
    "models/gemini-3.1-flash-lite",
    "models/gemini-2.5-flash",
    "models/gemini-3.1-pro-preview",
    "models/gemini-pro-latest",
]

DEPRECATED_MODELS = {
    "models/gemini-pro",
    "models/gemini-1.0-pro",
    "models/gemini-1.5-flash",
    "models/gemini-1.5-pro",
    "models/gemini-pro-vision",
}


def kill_duplicate_bot_processes():
    """
    Останавливает старые/зависшие процессы main.py или uvicorn в фоне,
    которые вызывают двойные ответы бота в Telegram.
    """
    current_pid = os.getpid()
    try:
        if sys.platform.startswith("win"):
            # Находим все процессы python.exe, где в командной строке есть main.py или uvicorn
            cmd = (
                'powershell -NoProfile -Command '
                '"Get-CimInstance Win32_Process | '
                "Where-Object { $_.ProcessId -ne " + str(current_pid) + " -and "
                "($_.Name -match 'python|uvicorn') -and "
                "($_.CommandLine -match 'main\\.py|main:app') } | "
                'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"'
            )
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        else:
            # Linux / macOS
            out = subprocess.check_output(["ps", "-ef"], text=True, timeout=5)
            for line in out.splitlines():
                if ("main.py" in line or "main:app" in line) and ("python" in line or "uvicorn" in line):
                    parts = line.split()
                    if len(parts) > 1 and parts[1].isdigit():
                        pid = int(parts[1])
                        if pid != current_pid:
                            try:
                                os.kill(pid, 9)
                                print(f"🧹 Остановлен старый дублирующий процесс бота (PID {pid})")
                            except Exception:
                                pass
    except Exception:
        pass


def acquire_single_instance_socket():
    """
    Гарантирует, что на компьютере запущен только 1 экземпляр бота.
    """
    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock_socket.bind(("127.0.0.1", 59421))
        return lock_socket
    except socket.error:
        print("⚠️ Обнаружен другой запущенный экземпляр бота. Останавливаем старый процесс...")
        kill_duplicate_bot_processes()
        time.sleep(1.0)
        try:
            lock_socket.bind(("127.0.0.1", 59421))
            return lock_socket
        except socket.error:
            print("❌ Не удалось запустить: другой процесс бота уже занимает порт блокировки. Закройте все старые терминалы.")
            sys.exit(1)


def cleanup_dedup_caches(now: float):
    """Очистка устаревших записей кэша дедупликации."""
    if len(processed_update_ids) > 2000:
        cutoff = now - 3600
        for k in [k for k, ts in processed_update_ids.items() if ts < cutoff]:
            processed_update_ids.pop(k, None)

    if len(processed_message_keys) > 2000:
        cutoff = now - 3600
        for k in [k for k, ts in processed_message_keys.items() if ts < cutoff]:
            processed_message_keys.pop(k, None)

    for k in [k for k, ts in processed_media_groups.items() if now - ts > 300]:
        processed_media_groups.pop(k, None)

    for k in [k for k, ts in recent_user_query_hashes.items() if now - ts > 30]:
        recent_user_query_hashes.pop(k, None)


def resolve_dynamic_model_via_list_models() -> str:
    """
    Динамически запрашивает список доступных моделей через ModelService.ListModels
    перед выполнением каждого вызова и выбирает актуальную модель с поддержкой generateContent.
    """
    try:
        models = list(genai.list_models())
        supported_models = []
        for m in models:
            methods = getattr(m, "supported_generation_methods", []) or []
            name = getattr(m, "name", "")
            if "generateContent" in methods and name not in DEPRECATED_MODELS:
                if not any(tag in name.lower() for tag in ["tts", "audio", "embedding", "veo", "lyria", "imagen", "aqa"]):
                    supported_models.append(name)

        selected_model = None
        for candidate in PREFERRED_MODEL_CANDIDATES:
            if candidate in supported_models:
                selected_model = candidate
                break

        if not selected_model:
            for name in supported_models:
                if "flash" in name.lower():
                    selected_model = name
                    break

        if not selected_model and supported_models:
            selected_model = supported_models[0]

        if not selected_model:
            selected_model = "models/gemini-flash-latest"

        clean_model_name = selected_model.replace("models/", "")
        print(f"[ListModels] Выбрана актуальная модель: {clean_model_name}")
        return clean_model_name
    except Exception as e:
        print(f"[ListModels Warning] Не удалось получить список моделей ({e}), используем gemini-flash-latest")
        return "gemini-flash-latest"


# --- Вспомогательные функции расчета цен и форматирования ---

def build_marketplace_search_url(platform_id: str, query: str) -> str:
    encoded = urllib.parse.quote(query.strip())

    if platform_id == 'uzum':
        return f"https://uzum.uz/ru/search?query={encoded}"
    elif platform_id == 'wildberries':
        return f"https://www.wildberries.uz/catalog/0/search.aspx?search={encoded}"
    elif platform_id == 'ozon':
        return f"https://uz.ozon.com/search/?text={encoded}"
    elif platform_id == 'aliexpress':
        ali_encoded = encoded.replace('%20', '-')
        return f"https://www.aliexpress.com/w/wholesale-{ali_encoded}.html"
    elif platform_id == 'yandex':
        return f"https://market.yandex.uz/search?text={encoded}"
    else:
        return f"https://www.google.com/search?q={encoded}"


def format_telegram_markdown(lang: str, product_name: str, platforms: list, recommendation: str) -> str:
    def get_offer(plat_id):
        for p in platforms:
            if p['platformId'] == plat_id:
                return p['PriceDisplay'][lang], p['searchUrl']
        return '—', '#'

    uzum_price, uzum_url = get_offer('uzum')
    wb_price, wb_url = get_offer('wildberries')
    ozon_price, ozon_url = get_offer('ozon')
    ali_price, ali_url = get_offer('aliexpress')
    yandex_price, yandex_url = get_offer('yandex')

    if lang == 'uz':
        return (
            f"🔍 **Mahsulot:** {product_name}\n"
            f"📊 **Platformalar bo'yicha narxlar taqqoslashi:**\n"
            f"- 🇺🇿 [**Uzum Market**]({uzum_url}): {uzum_price}\n"
            f"- 🇷🇺 [**Wildberries**]({wb_url}): {wb_price}\n"
            f"- 🇷🇺 [**Ozon**]({ozon_url}): {ozon_price}\n"
            f"- 📦 [**AliExpress**]({ali_url}): {ali_price}\n"
            f"- 🌐 [**Yandex Market**]({yandex_url}): {yandex_price}\n\n"
            f"💡 **Tavsiya:** {recommendation}"
        )
    elif lang == 'ru':
        return (
            f"🔍 **Товар:** {product_name}\n"
            f"📊 **Сравнение цен по платформам:**\n"
            f"- 🇺🇿 [**Uzum Market**]({uzum_url}): {uzum_price}\n"
            f"- 🇷🇺 [**Wildberries**]({wb_url}): {wb_price}\n"
            f"- 🇷🇺 [**Ozon**]({ozon_url}): {ozon_price}\n"
            f"- 📦 [**AliExpress**]({ali_url}): {ali_price}\n"
            f"- 🌐 [**Yandex Market**]({yandex_url}): {yandex_price}\n\n"
            f"💡 **Рекомендация:** {recommendation}"
        )
    else:
        return (
            f"🔍 **Product:** {product_name}\n"
            f"📊 **Price Comparison by Platform:**\n"
            f"- 🇺🇿 [**Uzum Market**]({uzum_url}): {uzum_price}\n"
            f"- 🇷🇺 [**Wildberries**]({wb_url}): {wb_price}\n"
            f"- 🇷🇺 [**Ozon**]({ozon_url}): {ozon_price}\n"
            f"- 📦 [**AliExpress**]({ali_url}): {ali_price}\n"
            f"- 🌐 [**Yandex Market**]({yandex_url}): {yandex_price}\n\n"
            f"💡 **Recommendation:** {recommendation}"
        )


def format_uzs_number(num: int) -> str:
    return f"{math.floor(num):,}".replace(",", " ")


def build_offers_from_base_usd(canonical_name: str, prices: dict) -> list:
    UZS_RATE = 12850

    raw_configs = [
        {
            "id": "uzum", "name": "Uzum Market", "flag": "🇺🇿",
            "minUsd": prices.get("uzumMinUsd", 100), "maxUsd": prices.get("uzumMaxUsd", 110),
            "shortDeliveryTag": {"uz": "1 kunda yetkazib berish", "ru": "Доставка за 1 день", "en": "1-day local delivery"},
            "isFastestDelivery": True
        },
        {
            "id": "wildberries", "name": "Wildberries", "flag": "🇷🇺",
            "minUsd": prices.get("wbMinUsd", 93), "maxUsd": prices.get("wbMaxUsd", 102),
            "shortDeliveryTag": {"uz": "5–8 kun PVZ", "ru": "5–8 дней до ПВЗ", "en": "5–8 days to PVZ"},
            "isFastestDelivery": False
        },
        {
            "id": "ozon", "name": "Ozon", "flag": "🇷🇺",
            "minUsd": prices.get("ozonMinUsd", 95), "maxUsd": prices.get("ozonMaxUsd", 104),
            "shortDeliveryTag": {"uz": "7–11 kun", "ru": "7–11 дней", "en": "7–11 days"},
            "isFastestDelivery": False
        },
        {
            "id": "aliexpress", "name": "AliExpress", "flag": "📦",
            "minUsd": prices.get("aliMinUsd", 84), "maxUsd": prices.get("aliMaxUsd", 92),
            "shortDeliveryTag": {"uz": "14–22 kun pochta", "ru": "14–22 дня из Китая", "en": "14–22 days direct"},
            "isFastestDelivery": False
        },
        {
            "id": "yandex", "name": "Yandex Market", "flag": "🌐",
            "minUsd": prices.get("yandexMinUsd", 97), "maxUsd": prices.get("yandexMaxUsd", 106),
            "shortDeliveryTag": {"uz": "2–5 kun", "ru": "2–5 дней", "en": "2–5 days"},
            "isFastestDelivery": False
        }
    ]

    result = []
    for cfg in raw_configs:
        min_usd = max(1, round(float(cfg["minUsd"])))
        max_usd = max(min_usd + 1, round(float(cfg["maxUsd"])))
        min_uzs = round((min_usd * UZS_RATE) / 1000) * 1000
        max_uzs = round((max_usd * UZS_RATE) / 1000) * 1000

        min_uzs_str = format_uzs_number(min_uzs)
        max_uzs_str = format_uzs_number(max_uzs)

        result.append({
            "platformId": cfg["id"],
            "platformName": cfg["name"],
            "PriceDisplay": {
                "uz": f"{min_uzs_str} – {max_uzs_str} so'm (~${min_usd}–${max_usd}) · {cfg['shortDeliveryTag']['uz']}",
                "ru": f"{min_uzs_str} – {max_uzs_str} сум (~${min_usd}–${max_usd}) · {cfg['shortDeliveryTag']['ru']}",
                "en": f"{min_uzs_str} – {max_uzs_str} UZS (~${min_usd}–${max_usd}) · {cfg['shortDeliveryTag']['en']}"
            },
            "searchUrl": build_marketplace_search_url(cfg["id"], canonical_name)
        })
    return result


async def analyze_product_with_gemini(
    query: str = None,
    image_base64: str = None,
    mime_type: str = None,
    preferred_language: str = 'auto'
) -> dict:
    # Динамическое обновление идентификатора модели через сервис ListModels перед каждым вызовом
    active_model_name = await asyncio.to_thread(resolve_dynamic_model_via_list_models)

    system_instruction = (
        "You are an expert product recognition and price comparison engine for Central Asia & CIS marketplaces "
        "(Uzum Market, Wildberries, Ozon, AliExpress, Yandex Market). "
        "Identify the exact product and realistic market prices in USD across all 5 marketplaces. "
        "Always return strictly valid JSON with these exact keys: "
        "detectedLanguage ('uz', 'ru', or 'en'), "
        "searchCanonicalQuery (string, concise model/product name for search URLs), "
        "productNameUz (string), productNameRu (string), productNameEn (string), "
        "uzumMinUsd (number), uzumMaxUsd (number), "
        "wbMinUsd (number), wbMaxUsd (number), "
        "ozonMinUsd (number), ozonMaxUsd (number), "
        "aliMinUsd (number), aliMaxUsd (number), "
        "yandexMinUsd (number), yandexMaxUsd (number), "
        "recommendationUz (string), recommendationRu (string), recommendationEn (string)."
    )

    parts = []

    if image_base64:
        clean_base64 = re.sub(r'^data:[^;]+;base64,', '', image_base64).replace(" ", "")
        valid_mime = (
            mime_type.lower()
            if mime_type and mime_type.lower() in ['image/jpeg', 'image/png', 'image/webp', 'image/heic']
            else 'image/jpeg'
        )
        parts.append({
            "mime_type": valid_mime,
            "data": base64.b64decode(clean_base64)
        })

    query_trim = query.strip() if query else ""
    if query_trim:
        parts.append(
            f"Analyze this product request (and attached image if present): \"{query_trim}\". "
            f"Identify the product and estimate realistic prices in USD across Uzum Market, Wildberries, Ozon, "
            f"AliExpress, and Yandex Market. Preferred language: {preferred_language}. Return JSON only."
        )
    else:
        parts.append(
            f"Examine this product photo. Identify the brand, model, or product type shown in the image and "
            f"estimate its current prices in USD across Uzum Market, Wildberries, Ozon, AliExpress, and Yandex Market. "
            f"Preferred language: {preferred_language}. Return JSON only."
        )

    model = genai.GenerativeModel(
        model_name=active_model_name,
        system_instruction=system_instruction,
        generation_config={"response_mime_type": "application/json"}
    )

    try:
        response = await asyncio.to_thread(model.generate_content, parts)
        raw_text = re.sub(r'^```json\s*', '', response.text, flags=re.IGNORECASE)
        raw_text = re.sub(r'^```\s*', '', raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r'\s*```$', '', raw_text).strip()
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            parsed = parsed[0]
    except Exception as e:
        raise Exception(f"Gemini generation failed (model={active_model_name}): {str(e)}")

    has_cyrillic = bool(re.search(r'[а-яА-ЯёЁ]', query_trim))
    detected_lang = parsed.get("detectedLanguage")
    if detected_lang not in ['ru', 'en', 'uz']:
        detected_lang = 'ru' if has_cyrillic else 'uz'

    applied_lang = preferred_language if preferred_language != 'auto' else detected_lang
    canonical_query = (
        parsed.get("searchCanonicalQuery")
        or parsed.get("productNameEn")
        or parsed.get("productNameRu")
        or query_trim
        or "Product"
    )

    safe_product_name = {
        "uz": parsed.get("productNameUz") or canonical_query,
        "ru": parsed.get("productNameRu") or canonical_query,
        "en": parsed.get("productNameEn") or canonical_query,
    }

    base_usd = float(parsed.get("uzumMinUsd") or 100)
    platforms = build_offers_from_base_usd(canonical_query, {
        "uzumMinUsd": float(parsed.get("uzumMinUsd") or base_usd),
        "uzumMaxUsd": float(parsed.get("uzumMaxUsd") or round(base_usd * 1.08)),
        "wbMinUsd": float(parsed.get("wbMinUsd") or round(base_usd * 0.93)),
        "wbMaxUsd": float(parsed.get("wbMaxUsd") or round(base_usd * 1.01)),
        "ozonMinUsd": float(parsed.get("ozonMinUsd") or round(base_usd * 0.95)),
        "ozonMaxUsd": float(parsed.get("ozonMaxUsd") or round(base_usd * 1.03)),
        "aliMinUsd": float(parsed.get("aliMinUsd") or round(base_usd * 0.84)),
        "aliMaxUsd": float(parsed.get("aliMaxUsd") or round(base_usd * 0.92)),
        "yandexMinUsd": float(parsed.get("yandexMinUsd") or round(base_usd * 0.97)),
        "yandexMaxUsd": float(parsed.get("yandexMaxUsd") or round(base_usd * 1.05)),
    })

    safe_recommendation = {
        "uz": parsed.get("recommendationUz") or "Tezkor 1 kunlik yetkazib berish uchun Uzum Market tavsiya etiladi.",
        "ru": parsed.get("recommendationRu") or "Для быстрой доставки выбирайте Uzum Market.",
        "en": parsed.get("recommendationEn") or "Choose Uzum Market for fast delivery."
    }

    telegram_markdown = {
        "uz": format_telegram_markdown('uz', safe_product_name['uz'], platforms, safe_recommendation['uz']),
        "ru": format_telegram_markdown('ru', safe_product_name['ru'], platforms, safe_recommendation['ru']),
        "en": format_telegram_markdown('en', safe_product_name['en'], platforms, safe_recommendation['en'])
    }

    return {
        "appliedLanguage": applied_lang,
        "telegramMarkdown": telegram_markdown,
    }


# --- Логика Telegram Бота (без Web-сервера) ---

def get_language_confirm_text(lang_cmd: str) -> str:
    if lang_cmd == 'uz':
        return "✅ **Til O'zbek tiliga o'zgartirildi!**"
    if lang_cmd == 'ru':
        return "✅ **Язык успешно изменен на Русский!**"
    if lang_cmd == 'en':
        return "✅ **Language set to English!**"
    return "✅ **Avtomatik til aniqlash yoqildi / Auto-detect enabled!**"


def markdown_to_html(md: str) -> str:
    escaped = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', escaped)
    escaped = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', escaped)
    return escaped


async def send_telegram_message(token: str, chat_id: int, text: str, reply_markup: dict = None):
    html_text = markdown_to_html(text)
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
        resp = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        data = resp.json()
        # Повторная отправка без HTML выполняется ТОЛЬКО если первая не доставлена из-за ошибки 400
        if not data.get("ok") and data.get("error_code") == 400:
            payload["text"] = text.replace("**", "")
            payload.pop("parse_mode", None)
            await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)


async def handle_telegram_update(token: str, update: dict):
    now = time.time()
    cleanup_dedup_caches(now)

    # 1. Строгая проверка update_id
    update_id = update.get("update_id")
    if update_id is not None:
        if update_id in processed_update_ids:
            return
        processed_update_ids[update_id] = now

    # Обработка кнопок выбора языка
    cb = update.get("callback_query")
    if cb:
        cb_id = cb.get("id")
        cb_key = f"cb:{cb_id}"
        if cb_id and cb_key in processed_message_keys:
            return
        if cb_id:
            processed_message_keys[cb_key] = now

        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        if chat_id and data.startswith("set_lang:"):
            lang = data.split(":")[1]
            user_language_prefs[chat_id] = lang
            await send_telegram_message(token, chat_id, get_language_confirm_text(lang))
        return

    msg = update.get("message")
    if not msg or "chat" not in msg:
        return

    chat_id = msg["chat"]["id"]
    message_id = msg.get("message_id")

    # 2. Строгая дедупликация по chat_id + message_id
    if message_id is not None:
        msg_key = f"{chat_id}:{message_id}"
        if msg_key in processed_message_keys:
            return
        processed_message_keys[msg_key] = now

    # 3. Дедупликация медиа-групп (если отправлено несколько фото сразу)
    media_group_id = msg.get("media_group_id")
    if media_group_id:
        mg_key = f"{chat_id}:{media_group_id}"
        if mg_key in processed_media_groups:
            return
        processed_media_groups[mg_key] = now

    raw_text = (msg.get("text") or msg.get("caption") or "").strip()

    if raw_text.startswith("/start") or raw_text.startswith("/help"):
        welcome = (
            "👋 **BozorScan AI — Narxlar Taqqoslash Boti**\n\n"
            "🇺🇿 Mahsulot nomini yozing yoki rasmini yuboring — Uzum, Wildberries, Ozon, AliExpress va Yandex Market narxlarini solishtirib beraman.\n"
            "🇷🇺 Отправьте название или фото товара для сравнения цен на 5 маркетплейсах.\n"
            "🇬🇧 Send a product name or photo to compare prices across 5 marketplaces.\n\n"
            "🌐 Buyruqlar / Команды / Commands:\n"
            "/language — Tilni o'zgartirish / Сменить язык / Change language\n"
            "/uz — O'zbek tili\n"
            "/ru — Русский язык\n"
            "/en — English\n"
            "/auto — Auto-detect"
        )
        await send_telegram_message(token, chat_id, welcome)
        return

    if raw_text in ["/uz", "/ru", "/en", "/auto"]:
        lang_code = raw_text.lstrip("/")
        user_language_prefs[chat_id] = lang_code
        await send_telegram_message(token, chat_id, get_language_confirm_text(lang_code))
        return

    if raw_text.startswith("/language"):
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🇺🇿 O'zbekcha", "callback_data": "set_lang:uz"},
                    {"text": "🇷🇺 Русский", "callback_data": "set_lang:ru"},
                ],
                [
                    {"text": "🇬🇧 English", "callback_data": "set_lang:en"},
                    {"text": "✨ Auto", "callback_data": "set_lang:auto"},
                ]
            ]
        }
        await send_telegram_message(
            token,
            chat_id,
            "🌐 **Tilni tanlang / Выберите язык / Choose language:**",
            reply_markup=keyboard
        )
        return

    target_file_id = None
    file_unique_id = ""
    mime_type = None

    if msg.get("photo"):
        largest_photo = msg["photo"][-1]
        target_file_id = largest_photo["file_id"]
        file_unique_id = largest_photo.get("file_unique_id", target_file_id)
        mime_type = "image/jpeg"
    elif msg.get("document") and str(msg["document"].get("mime_type")).startswith("image/"):
        target_file_id = msg["document"]["file_id"]
        file_unique_id = msg["document"].get("file_unique_id", target_file_id)
        mime_type = msg["document"]["mime_type"]

    if not raw_text and not target_file_id:
        return

    # 4. Блокировка на уровне чата + проверка хэша запроса за последние 30 секунд
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()

    raw_signature = f"{chat_id}:{raw_text.lower()}:{file_unique_id}"
    query_hash = hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()[:16]
    dedup_hash_key = f"{chat_id}:{query_hash}"

    if dedup_hash_key in in_flight_requests:
        return

    last_sent_ts = recent_user_query_hashes.get(dedup_hash_key)
    if last_sent_ts and (time.time() - last_sent_ts) < 30.0:
        return

    in_flight_requests.add(dedup_hash_key)
    recent_user_query_hashes[dedup_hash_key] = time.time()

    async with chat_locks[chat_id]:
        try:
            image_base64 = None
            if target_file_id:
                async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
                    file_resp = await client.get(f"https://api.telegram.org/bot{token}/getFile?file_id={target_file_id}")
                    file_data = file_resp.json()
                    if file_data.get("ok"):
                        file_path = file_data["result"]["file_path"]
                        img_resp = await client.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
                        if img_resp.status_code == 200:
                            image_base64 = base64.b64encode(img_resp.content).decode('utf-8')

            pref = user_language_prefs.get(chat_id, "auto")
            analysis = await analyze_product_with_gemini(raw_text, image_base64, mime_type, pref)
            reply_md = analysis["telegramMarkdown"][analysis["appliedLanguage"]]
            await send_telegram_message(token, chat_id, reply_md)
        except Exception as e:
            recent_user_query_hashes.pop(dedup_hash_key, None)
            print("Error processing telegram message:", e)
            await send_telegram_message(token, chat_id, "⚠️ Error processing your request.")
        finally:
            in_flight_requests.discard(dedup_hash_key)


async def telegram_polling_loop():
    token = TELEGRAM_BOT_TOKEN
    if not token or len(token) < 10:
        print("❌ Токен TELEGRAM_BOT_TOKEN не задан. Проверьте файл .env")
        return

    offset = 0
    async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
        # 1. Полностью удаляем вебхук (если он был установлен ранее на сервере), чтобы вебхук и поллинг не отвечали одновременно!
        try:
            await client.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true")
            me_resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            me_data = me_resp.json()
            if me_data.get("ok"):
                bot_user = me_data["result"].get("username")
                print(f"🤖 Telegram-бот @{bot_user} успешно запущен (только бот, без Web-версии)!")
        except Exception as e:
            print("Ошибка инициализации Telegram API:", e)

        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 15,
                        "allowed_updates": json.dumps(["message", "callback_query"])
                    }
                )
                data = resp.json()

                if not data.get("ok"):
                    if data.get("error_code") == 409:
                        print("⚠️ Конфликт 409: обнаружен другой процесс с этим токеном. Останавливаем дубликаты...")
                        kill_duplicate_bot_processes()
                        await asyncio.sleep(3)
                    else:
                        await asyncio.sleep(2)
                    continue

                updates = data.get("result") or []
                if updates:
                    # Мгновенно увеличиваем offset и подтверждаем его в Telegram ДО вызова Gemini
                    offset = max(u["update_id"] for u in updates) + 1
                    try:
                        await client.get(
                            f"https://api.telegram.org/bot{token}/getUpdates",
                            params={"offset": offset, "limit": 1, "timeout": 0}
                        )
                    except Exception:
                        pass

                    for update in updates:
                        await handle_telegram_update(token, update)
            except Exception as e:
                print("Polling error:", e)
                await asyncio.sleep(2)


if __name__ == "__main__":
    # 1. Останавливаем любые старые/зависшие процессы бота на этом компьютере
    kill_duplicate_bot_processes()
    # 2. Захватываем системный сокет-лок, чтобы второй экземпляр никогда не запустился параллельно
    _instance_lock = acquire_single_instance_socket()
    # 3. Запускаем чистый цикл Telegram-бота (без FastAPI и без Web-сервера)
    try:
        asyncio.run(telegram_polling_loop())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем.")
