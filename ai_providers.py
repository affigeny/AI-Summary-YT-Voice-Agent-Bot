"""Выбор ИИ-провайдера пользователем — каталог бесплатных API, авто-перебор,
свои ключи, тест соединения и загрузка списка моделей.

Устройство (по аналогии с youtube_bypass.py — только UI + резолвер):
  - CATALOG    — бесплатные OpenAI-совместимые провайдеры;
  - resolve()  — во что превращается выбор пользователя при запросе к LLM;
  - build_chain() — порядок перебора в режиме «Авто»;
  - UI-меню /ai с кнопками: выбор, авто вкл/выкл, свои URL/ключ/модель,
    🧪 тест соединения, 📥 загрузить модели.

Ключи берутся в порядке: личный ключ пользователя (БД) → переменная
окружения провайдера → общий AI_API_KEY. Колбэки имеют префикс `ai:` и
маршрутизируются единым диспетчером MediaBot.handle_callback_query.
"""

import asyncio
import logging
import os

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Каталог бесплатных OpenAI-совместимых провайдеров.
# key      — идентификатор в БД
# label    — подпись кнопки
# url      — базовый URL (без /chat/completions)
# model    — бесплатная модель по умолчанию
# env      — переменная окружения с ключом этого провайдера
# note     — что важно знать про лимиты
# ---------------------------------------------------------------------------
CATALOG = {
    "gemini": {
        "label": "\U0001F537 Google Gemini (Flash)",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.6-flash",
        "env": "GEMINI_API_KEY",
        "note": "Бесплатный тариф, большой контекст. Ключ: aistudio.google.com",
    },
    "groq": {
        "label": "\u26a1 Groq (Llama 3.3 70B)",
        "url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "env": "GROQ_API_KEY",
        "note": "Очень быстрый, щедрый free tier. Ключ: console.groq.com",
    },
    "cerebras": {
        "label": "\U0001F9E0 Cerebras (Llama 3.3 70B)",
        "url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
        "env": "CEREBRAS_API_KEY",
        "note": "Самый быстрый инференс. Ключ: cloud.cerebras.ai",
    },
    "openrouter": {
        "label": "\U0001F310 OpenRouter (free-модели)",
        "url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "env": "OPENROUTER_API_KEY",
        "note": "Десятки моделей с суффиксом :free. Ключ: openrouter.ai",
    },
    "mistral": {
        "label": "\U0001F32C Mistral (Small)",
        "url": "https://api.mistral.ai/v1",
        "model": "mistral-small-latest",
        "env": "MISTRAL_API_KEY",
        "note": "Бесплатный experimental-тариф. Ключ: console.mistral.ai",
    },
    "github": {
        "label": "\U0001F419 GitHub Models (GPT-4o mini)",
        "url": "https://models.inference.ai.azure.com",
        "model": "gpt-4o-mini",
        "env": "GITHUB_MODELS_TOKEN",
        "note": "Бесплатно по GitHub PAT (scope: models). Лимит скромный.",
    },
    "together": {
        "label": "\U0001F91D Together AI (free)",
        "url": "https://api.together.xyz/v1",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        "env": "TOGETHER_API_KEY",
        "note": "Есть бесплатные модели с суффиксом -Free. Ключ: together.ai",
    },
    "custom": {
        "label": "\U0001F527 Свой API (URL + ключ + модель)",
        "url": "",
        "model": "",
        "env": "",
        "note": "Любой OpenAI-совместимый эндпоинт: локальный, платный, прокси.",
    },
}

# Порядок перебора в режиме «Авто»: сначала быстрые и щедрые.
AUTO_ORDER = ("gemini", "groq", "cerebras", "openrouter", "mistral",
              "together", "github")

TEST_PROMPT = "Ответь одним словом: работает"


# ---------------------------------------------------------------------------
# Резолвинг настроек в конкретные креды.
# ---------------------------------------------------------------------------
def provider_key(name: str, user_key: str = "") -> str:
    """Ключ провайдера: личный пользователя → env провайдера → общий."""
    if user_key:
        return user_key
    spec = CATALOG.get(name) or {}
    env_name = spec.get("env") or ""
    if env_name:
        val = os.getenv(env_name, "")
        if val:
            return val
    return os.getenv("AI_API_KEY", "")


def resolve(settings: dict, fallback_url: str, fallback_model: str) -> dict:
    """Во что превращается выбор пользователя при запросе к LLM.

    settings — строка из БД (get_ai_settings). Возвращает
    {name, url, key, model}; при 'auto' — первый доступный из цепочки.
    """
    name = (settings or {}).get("ai_provider") or "auto"
    user_url = (settings or {}).get("ai_api_url") or ""
    user_key = (settings or {}).get("ai_api_key") or ""
    user_model = (settings or {}).get("ai_model") or ""

    if name == "custom":
        return {
            "name": "custom",
            "url": (user_url or fallback_url).rstrip("/"),
            "key": user_key or os.getenv("AI_API_KEY", ""),
            "model": user_model or fallback_model,
        }

    if name == "auto":
        chain = build_chain(settings, fallback_url, fallback_model)
        if chain:
            return chain[0]
        return {
            "name": "env", "url": fallback_url.rstrip("/"),
            "key": os.getenv("AI_API_KEY", ""), "model": fallback_model,
        }

    spec = CATALOG.get(name)
    if not spec:
        return {
            "name": "env", "url": fallback_url.rstrip("/"),
            "key": os.getenv("AI_API_KEY", ""), "model": fallback_model,
        }
    return {
        "name": name,
        "url": (user_url or spec["url"] or fallback_url).rstrip("/"),
        "key": provider_key(name, user_key),
        "model": user_model or spec["model"] or fallback_model,
    }


def build_chain(settings: dict, fallback_url: str, fallback_model: str) -> list:
    """Цепочка провайдеров для авто-перебора: только те, где есть ключ."""
    settings = settings or {}
    chain = []
    seen = set()

    # Свой API первым, если пользователь его задал.
    if settings.get("ai_api_url") and settings.get("ai_provider") == "custom":
        chain.append({
            "name": "custom",
            "url": settings["ai_api_url"].rstrip("/"),
            "key": settings.get("ai_api_key") or os.getenv("AI_API_KEY", ""),
            "model": settings.get("ai_model") or fallback_model,
        })
        seen.add("custom")

    # Выбранный вручную провайдер — приоритетный в переборе.
    current = settings.get("ai_provider") or "auto"
    order = list(AUTO_ORDER)
    if current in CATALOG and current not in ("auto", "custom"):
        order.insert(0, current)

    for name in order:
        if name in seen:
            continue
        spec = CATALOG.get(name)
        if not spec or not spec.get("url"):
            continue
        key = provider_key(name)
        if not key:
            continue  # без ключа провайдер бесполезен
        seen.add(name)
        chain.append({
            "name": name, "url": spec["url"].rstrip("/"),
            "key": key, "model": spec["model"],
        })

    # Последний шанс — общие переменные окружения.
    env_key = os.getenv("AI_API_KEY", "")
    if env_key and fallback_url:
        chain.append({
            "name": "env", "url": fallback_url.rstrip("/"),
            "key": env_key, "model": fallback_model,
        })
    return chain


# ---------------------------------------------------------------------------
# Сетевые проверки: тест соединения и список моделей.
# ---------------------------------------------------------------------------
async def test_provider(creds: dict, timeout: int = 45) -> tuple:
    """Пробный запрос к провайдеру. Возвращает (ok: bool, текст отчёта)."""
    if not creds.get("key"):
        return False, "нет ключа — задайте свой ключ или переменную окружения"
    if not creds.get("url"):
        return False, "не задан URL API"
    payload = {
        "model": creds["model"],
        "messages": [{"role": "user", "content": TEST_PROMPT}],
        "max_tokens": 20,
        "temperature": 0,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {creds['key']}",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{creds['url']}/chat/completions",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    return False, _explain(resp.status, body)
                try:
                    data = await resp.json(content_type=None)
                except Exception:  # noqa: BLE001
                    return False, "ответ не в JSON"
                choices = data.get("choices") or []
                if not choices:
                    return False, "пустой ответ модели"
                text = (choices[0].get("message", {}).get("content") or "").strip()
                return True, text[:80] or "(пустой текст, но HTTP 200)"
    except asyncio.TimeoutError:
        return False, f"нет ответа за {timeout} с"
    except Exception as exc:  # noqa: BLE001
        return False, f"сетевая ошибка: {str(exc)[:80]}"


async def fetch_models(creds: dict, timeout: int = 30) -> tuple:
    """GET /models. Возвращает (список id, текст ошибки)."""
    if not creds.get("key") or not creds.get("url"):
        return [], "нет ключа или URL"
    headers = {"Authorization": f"Bearer {creds['key']}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{creds['url']}/models", headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    return [], _explain(resp.status, (await resp.text())[:200])
                data = await resp.json(content_type=None)
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return [], "неожиданный формат ответа"
        ids = []
        for it in items:
            mid = it.get("id") if isinstance(it, dict) else str(it)
            if mid:
                ids.append(str(mid).replace("models/", ""))
        return sorted(set(ids)), ""
    except asyncio.TimeoutError:
        return [], f"нет ответа за {timeout} с"
    except Exception as exc:  # noqa: BLE001
        return [], f"сетевая ошибка: {str(exc)[:80]}"


def _explain(status: int, body: str) -> str:
    low = (body or "").lower()
    if status in (401, 403):
        return "ключ отклонён провайдером (401/403) — проверьте ключ"
    if status == 404:
        return "модель или эндпоинт не найдены (404) — проверьте URL и модель"
    if status == 429:
        return "превышен лимит запросов (429) — подождите минуту"
    if status == 400 and ("token" in low or "exceed" in low):
        return "запрос слишком длинный для модели (400)"
    return f"HTTP {status}: {(body or '')[:100]}"


# ---------------------------------------------------------------------------
# UI: меню выбора провайдера.
# ---------------------------------------------------------------------------
def _mask(value: str) -> str:
    """Ключ показываем усечённым — в чат секреты не печатаем."""
    if not value:
        return "—"
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def keyboard(settings: dict) -> InlineKeyboardMarkup:
    current = (settings or {}).get("ai_provider") or "auto"
    auto_on = bool((settings or {}).get("ai_auto", 1))
    rows = [[
        InlineKeyboardButton(
            ("\u2705 " if current == "auto" else "") + "\U0001F916 Авто (лучший доступный)",
            callback_data="ai:set:auto",
        )
    ]]
    for name in AUTO_ORDER:
        spec = CATALOG[name]
        mark = "\u2705 " if current == name else ""
        has_key = "" if provider_key(name) else " \U0001F511"  # 🔑 = нужен ключ
        rows.append([InlineKeyboardButton(
            f"{mark}{spec['label']}{has_key}", callback_data=f"ai:set:{name}"
        )])
    rows.append([InlineKeyboardButton(
        ("\u2705 " if current == "custom" else "") + CATALOG["custom"]["label"],
        callback_data="ai:set:custom",
    )])
    rows.append([InlineKeyboardButton(
        f"\U0001F501 Авто-перебор при ошибке: {'вкл' if auto_on else 'выкл'}",
        callback_data="ai:toggle_auto",
    )])
    rows.append([
        InlineKeyboardButton("\U0001F9EA Тест", callback_data="ai:test"),
        InlineKeyboardButton("\U0001F4E5 Модели", callback_data="ai:models"),
    ])
    rows.append([
        InlineKeyboardButton("\U0001F517 URL", callback_data="ai:ask:url"),
        InlineKeyboardButton("\U0001F511 Ключ", callback_data="ai:ask:key"),
        InlineKeyboardButton("\U0001F9E9 Модель", callback_data="ai:ask:model"),
    ])
    rows.append([InlineKeyboardButton(
        "\u267b\ufe0f Сбросить свои настройки", callback_data="ai:reset"
    )])
    return InlineKeyboardMarkup(rows)


def status_text(settings: dict, fallback_url: str, fallback_model: str) -> str:
    creds = resolve(settings, fallback_url, fallback_model)
    name = (settings or {}).get("ai_provider") or "auto"
    label = (
        "\U0001F916 Авто" if name == "auto"
        else CATALOG.get(name, {}).get("label", name)
    )
    chain = build_chain(settings, fallback_url, fallback_model)
    chain_names = " → ".join(c["name"] for c in chain[:5]) or "нет доступных"
    auto_on = bool((settings or {}).get("ai_auto", 1))
    lines = [
        "\U0001F9E0 Выбор нейросети",
        "",
        f"Сейчас: {label}",
        f"Модель: {creds['model'] or '—'}",
        f"URL: {creds['url'] or '—'}",
        f"Ключ: {_mask(creds['key'])}",
        f"Авто-перебор при ошибке: {'вкл' if auto_on else 'выкл'}",
        f"Цепочка: {chain_names}",
        "",
        "\U0001F511 у кнопки — для провайдера ещё нет ключа. "
        "Добавьте его кнопкой «Ключ» или переменной окружения.",
    ]
    note = CATALOG.get(name, {}).get("note")
    if note:
        lines += ["", f"\u2139\ufe0f {note}"]
    return "\n".join(lines)


async def show_ai_menu(message, db, user_id: int, fallback_url: str,
                       fallback_model: str, edit=False):
    settings = db.get_ai_settings(user_id)
    text = status_text(settings, fallback_url, fallback_model)
    kb = keyboard(settings)
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except Exception:  # noqa: BLE001
            pass  # сообщение не изменилось или устарело
    await message.reply_text(text, reply_markup=kb)


def register_ai_command(app, bot):
    """Регистрирует /ai — меню выбора нейросети."""

    async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await show_ai_menu(
            update.effective_message, bot.db, update.effective_user.id,
            bot.llm.api_url, bot.llm.model,
        )

    app.add_handler(CommandHandler("ai", ai_command))
