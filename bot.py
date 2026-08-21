"""YT_Bot_Sum — YouTube/voice summarizer bot (v5.0).

Принимает:
  - ссылку YouTube / Shorts или просто ID видео (11 символов);
  - голосовое сообщение;
  - аудиофайл (mp3/wav/m4a/ogg/…) и видеофайл (mp4/mkv/webm/…).

Что делает:
  1. Достаёт транскрипт: цепочка методов обхода блокировок YouTube
     (InnerTube API → yt-dlp web → TV-клиент → Android VR → Invidious →
     Piped → куки; см. yt_transcript.py), а если субтитров нет — скачивает
     аудио и распознаёт речь (Google Speech, бесплатно, без загрузки моделей).
  2. Сохраняет транскрипт в БД (/history — вернуться к любому).
  3. Перерабатывает текст через LLM (OpenAI-совместимый API) по шаблону:
     стандартные (саммари / Минто / экшен-план / конспект) или свои
     (/add_template), выбор — кнопками.
  4. Даёт инлайн-кнопки: шаблоны, полный транскрипт файлом, история,
     скачивание видео/аудио в любом качестве.

Архитектура (модульная):
  BotDatabase  — SQLite (шаблоны, транскрипты, кэш YouTube, история, настройки);
  LLMClient    — обращение к LLM-провайдеру;
  YTClient     — метаданные/форматы/скачивание через yt-dlp;
  STTClient    — распознавание речи (Google Speech Recognition);
  MediaBot     — Telegram-обработчики и инлайн-интерфейс (единый диспетчер
                 колбэков — никаких конфликтующих CallbackQueryHandler);
  yt_transcript — движок получения транскрипта с обходом блокировок;
  youtube_bypass — меню /bypass (выбор/тест метода обхода).

Запуск: python bot.py
Деплой: Render Web Service (Free) + health-check сервер на $PORT.
"""

import asyncio
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import ai_providers
import yt_transcript
from ai_providers import register_ai_command, show_ai_menu
from youtube_bypass import handle_bypass_callback, register_bypass_command

__version__ = "6.2.0"
VERSION_STRING = __version__

# ---------------------------------------------------------------------------
# Конфигурация из окружения (env-переменные задаются в панели Render).
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_URL = os.getenv("AI_API_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
AI_MODEL = os.getenv("AI_MODEL", "gemini-3.6-flash")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")
PORT = int(os.getenv("PORT", "10000"))

# Язык распознавания речи (Google Speech). ru-RU по умолчанию.
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "ru-RU")

# Лимиты аудио и таймауты распознавания (Google Speech — быстро, но чанками).
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "1800"))
STT_CHUNK_SECONDS = int(os.getenv("STT_CHUNK_SECONDS", "50"))  # <60 с на чанк
STT_TIMEOUT = int(os.getenv("STT_TIMEOUT", "600"))   # на одно аудио
YT_FETCH_TIMEOUT = int(os.getenv("YT_FETCH_TIMEOUT", "180"))  # на цепочку методов
YT_DOWNLOAD_TIMEOUT = int(os.getenv("YT_DOWNLOAD_TIMEOUT", "300"))

# Лимиты LLM: длинный транскрипт урезаем до LLM_MAX_CHARS (голова+хвост).
LLM_MAX_CHARS = int(os.getenv("LLM_MAX_CHARS", "24000"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "240"))

# Сколько последних сообщений диалога отправлять в LLM (защита от переполнения
# контекста: транскрипт уже занимает до LLM_MAX_CHARS).
HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "12"))

# Максимум символов на одно сообщение диалога (ответы ИИ бывают огромными и
# при обсуждении быстро переполняют контекст → HTTP 400 у провайдера).
HISTORY_MSG_MAX_CHARS = int(os.getenv("HISTORY_MSG_MAX_CHARS", "4000"))

# Префикс, по которому распознаём неудачный ответ LLM (для кнопки «Повторить»).
LLM_ERROR_PREFIX = "\u26a0\ufe0f"

# Telegram Bot API не позволяет боту скачивать файлы больше 20 МБ.
TELEGRAM_FILE_LIMIT = 20 * 1024 * 1024

# Кулдаун между тяжёлыми запросами одного пользователя (сек).
USER_COOLDOWN_SECONDS = 5

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Дефолтные шаблоны переработки.
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES = {
    "1": {
        "name": "\U0001F4DD Краткое саммари",
        "prompt": (
            "Сделай краткую выжимку (summary) следующего текста в виде "
            "маркированного списка ключевых мыслей и выводов. Пиши на русском."
        ),
    },
    "2": {
        "name": "\U0001F4CA Пирамида Минто",
        "prompt": (
            "Переработай текст по принципу Пирамиды Минто: сначала главное "
            "утверждение, затем ключевые аргументы/подпункты. Пиши на русском."
        ),
    },
    "3": {
        "name": "\u2705 Экшен-план",
        "prompt": (
            "Выдели из текста только конкретные задачи, действия, шаги и "
            "договорённости (Action Items) в виде чек-листа. Пиши на русском."
        ),
    },
    "4": {
        "name": "\U0001F393 Подробный конспект",
        "prompt": (
            "Составь подробный учебный/аналитический конспект: логические разделы "
            "с заголовками, важные термины и инсайты. Пиши на русском."
        ),
    },
}

TEMPLATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


# ---------------------------------------------------------------------------
# База данных.
# ---------------------------------------------------------------------------
class BotDatabase:
    """Слой SQLite: шаблоны, транскрипты, кэш YouTube, история, настройки."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                user_id INTEGER,
                template_id TEXT,
                name TEXT,
                prompt TEXT,
                PRIMARY KEY (user_id, template_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS youtube_cache (
                video_id TEXT PRIMARY KEY,
                transcript TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_history (
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                source TEXT,
                video_id TEXT,
                title TEXT,
                text TEXT,
                chars INTEGER,
                method TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                bypass_method TEXT DEFAULT 'auto',
                output_format TEXT DEFAULT 'tg'
            )
            """
        )
        # Миграции: добавляем колонки, если таблица создана более старой версией.
        for ddl in (
            "ALTER TABLE user_settings ADD COLUMN output_format TEXT DEFAULT 'tg'",
            "ALTER TABLE user_settings ADD COLUMN ai_provider TEXT DEFAULT 'auto'",
            "ALTER TABLE user_settings ADD COLUMN ai_auto INTEGER DEFAULT 1",
            "ALTER TABLE user_settings ADD COLUMN ai_api_url TEXT",
            "ALTER TABLE user_settings ADD COLUMN ai_api_key TEXT",
            "ALTER TABLE user_settings ADD COLUMN ai_model TEXT",
        ):
            try:
                cur.execute(ddl)
            except sqlite3.OperationalError:
                pass  # колонка уже есть
        conn.commit()
        conn.close()

    # -- Шаблоны ------------------------------------------------------------
    def get_templates(self, user_id: int) -> dict:
        templates = DEFAULT_TEMPLATES.copy()
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT template_id, name, prompt FROM templates WHERE user_id = ?",
                (user_id,),
            )
            for row in cur.fetchall():
                templates[row[0]] = {"name": row[1], "prompt": row[2]}
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка получения шаблонов: %s", exc)
        return templates

    def save_template(self, user_id: int, template_id: str, name: str, prompt: str):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO templates (user_id, template_id, name, prompt)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, template_id, name, prompt),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения шаблона: %s", exc)

    def delete_template(self, user_id: int, template_id: str) -> bool:
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM templates WHERE user_id = ? AND template_id = ?",
                (user_id, template_id),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            conn.close()
            return deleted
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка удаления шаблона: %s", exc)
            return False

    # -- Настройки (метод обхода) --------------------------------------------
    def get_bypass_method(self, user_id: int) -> str:
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT bypass_method FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            conn.close()
            return row[0] if row else "auto"
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения настроек: %s", exc)
            return "auto"

    def set_bypass_method(self, user_id: int, method: str):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_settings (user_id, bypass_method)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET bypass_method = excluded.bypass_method
                """,
                (user_id, method),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка записи настроек: %s", exc)

    # -- Настройки ИИ (провайдер, авто-перебор, свои ключи) ------------------
    AI_FIELDS = ("ai_provider", "ai_auto", "ai_api_url", "ai_api_key", "ai_model")

    def get_ai_settings(self, user_id: int) -> dict:
        """Настройки ИИ пользователя; при отсутствии — значения по умолчанию."""
        defaults = {
            "ai_provider": "auto", "ai_auto": 1,
            "ai_api_url": None, "ai_api_key": None, "ai_model": None,
        }
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                f"SELECT {', '.join(self.AI_FIELDS)} FROM user_settings "
                "WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            conn.close()
            if not row:
                return defaults
            data = dict(zip(self.AI_FIELDS, row))
            if data.get("ai_provider") is None:
                data["ai_provider"] = "auto"
            if data.get("ai_auto") is None:
                data["ai_auto"] = 1
            return data
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения настроек ИИ: %s", exc)
            return defaults

    def set_ai_setting(self, user_id: int, field: str, value):
        if field not in self.AI_FIELDS:
            raise ValueError(f"неизвестное поле {field}")
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                f"""
                INSERT INTO user_settings (user_id, {field})
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET {field} = excluded.{field}
                """,
                (user_id, value),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка записи настроек ИИ: %s", exc)

    def get_output_format(self, user_id: int) -> str:
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT output_format FROM user_settings WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            conn.close()
            return (row[0] if row and row[0] else "tg")
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения формата вывода: %s", exc)
            return "tg"

    def set_output_format(self, user_id: int, fmt: str):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_settings (user_id, output_format)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET output_format = excluded.output_format
                """,
                (user_id, fmt),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка записи формата вывода: %s", exc)

    # -- Транскрипты (история) ------------------------------------------------
    def save_transcript(self, user_id: int, source: str, video_id: str,
                        title: str, text: str, method: str = "") -> int:
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO transcripts (user_id, source, video_id, title, text, chars, method)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, source, video_id, title, text, len(text), method),
            )
            row_id = cur.lastrowid
            conn.commit()
            conn.close()
            return row_id or 0
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения транскрипта: %s", exc)
            return 0

    def get_transcripts(self, user_id: int, limit: int = 8) -> list:
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, source, video_id, title, chars, created_at
                FROM transcripts WHERE user_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            )
            rows = cur.fetchall()
            conn.close()
            return [
                {
                    "id": r[0], "source": r[1], "video_id": r[2],
                    "title": r[3], "chars": r[4], "date": (r[5] or "")[:16],
                }
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения истории: %s", exc)
            return []

    def update_transcript_title(self, transcript_id: int, user_id: int, title: str):
        """Дописывает название задним числом (когда его добыли позже)."""
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "UPDATE transcripts SET title = ? WHERE id = ? AND user_id = ?",
                (title, transcript_id, user_id),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка обновления названия: %s", exc)

    def get_transcript_by_id(self, transcript_id: int, user_id: int):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, source, video_id, title, text
                FROM transcripts WHERE id = ? AND user_id = ?
                """,
                (transcript_id, user_id),
            )
            r = cur.fetchone()
            conn.close()
            if not r:
                return None
            return {
                "id": r[0], "source": r[1], "video_id": r[2],
                "title": r[3], "text": r[4],
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения транскрипта: %s", exc)
            return None

    def count_transcripts(self, user_id: int) -> int:
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM transcripts WHERE user_id = ?", (user_id,)
            )
            n = cur.fetchone()[0]
            conn.close()
            return n
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка подсчёта транскриптов: %s", exc)
            return 0

    # -- Кэш YouTube -----------------------------------------------------------
    def get_cached_youtube(self, video_id: str):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT transcript FROM youtube_cache WHERE video_id = ?",
                (video_id,),
            )
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения кэша YT: %s", exc)
            return None

    def save_youtube_cache(self, video_id: str, transcript: str):
        try:
            conn = self._conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO youtube_cache (video_id, transcript) "
                "VALUES (?, ?)",
                (video_id, transcript),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка записи в кэш YT: %s", exc)


# ---------------------------------------------------------------------------
# LLM-клиент.
# ---------------------------------------------------------------------------
class LLMClient:
    """Обращение к OpenAI-совместимому API (OpenRouter и др.)."""

    def __init__(self, api_key: str, api_url: str, model: str):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.model = model

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= LLM_MAX_CHARS:
            return text
        head = int(LLM_MAX_CHARS * 0.6)
        tail = LLM_MAX_CHARS - head
        return (
            text[:head]
            + "\n\n[…середина текста пропущена…]\n\n"
            + text[-tail:]
        )

    @staticmethod
    def _sanitize(messages: list) -> list:
        """Готовит messages под строгие OpenAI-совместимые API (в т.ч. Gemini).

        Убирает пустые сообщения и склеивает подряд идущие с одной ролью —
        Gemini отвечает HTTP 400 на дубли ролей и пустой content.
        """
        cleaned = []
        for msg in messages:
            content = (msg.get("content") or "").strip()
            role = msg.get("role")
            if not content or role not in ("system", "user", "assistant"):
                continue
            if cleaned and cleaned[-1]["role"] == role:
                cleaned[-1]["content"] += "\n\n" + content
            else:
                cleaned.append({"role": role, "content": content})
        # После system первым обязан идти user.
        if len(cleaned) > 1 and cleaned[1]["role"] == "assistant":
            cleaned.insert(1, {"role": "user", "content": "Продолжай."})
        return cleaned

    async def complete(self, prompt: str, context_text: str, history=None,
                       retries=2, chain=None):
        """Запрос к LLM. chain — список провайдеров для авто-перебора.

        chain: [{name, url, key, model}, …]. Если не передан — используются
        креды по умолчанию (из переменных окружения).
        """
        candidates = chain or [{
            "name": "env", "url": self.api_url,
            "key": self.api_key, "model": self.model,
        }]
        candidates = [c for c in candidates if c.get("key") and c.get("url")]
        if not candidates:
            return (
                "\u26a0\ufe0f Нет доступного ИИ: не задан ни один API-ключ.\n"
                "Откройте «\U0001F9E0 Нейросеть» и добавьте ключ, либо задайте "
                "AI_API_KEY в настройках сервиса."
            )

        messages = self._build_messages(prompt, context_text, history)
        errors = []
        for idx, creds in enumerate(candidates):
            result, reason = await self._try_provider(creds, messages, retries)
            if result is not None:
                if idx > 0:
                    # Сообщаем, что сработал не первый провайдер — иначе
                    # пользователь не поймёт, почему стиль ответа изменился.
                    result = (
                        f"\u2139\ufe0f Ответ получен через {creds['name']} "
                        f"({creds['model']}) — предыдущие были недоступны.\n\n"
                        + result
                    )
                return result
            errors.append(f"• {creds['name']}: {reason}")

        detail = "\n".join(errors[:5])
        return (
            "\u26a0\ufe0f Ни один ИИ не ответил.\n\n"
            f"{detail}\n\n"
            "Нажмите «\u267b\ufe0f Повторить» или откройте «\U0001F9E0 Нейросеть», "
            "чтобы выбрать другого провайдера."
        )

    def _build_messages(self, prompt: str, context_text: str, history=None) -> list:
        """Собирает и санитизирует messages для OpenAI-совместимого API."""
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты — полезный ИИ-ассистент. Обрабатывай предоставленные "
                    "тексты на русском языке."
                ),
            }
        ]
        if context_text:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Вот исходный текст/транскрипт для контекста:\n\n"
                        f"{self._truncate(context_text)}"
                    ),
                }
            )
        if history:
            # Берём только последние сообщения: транскрипт уже занимает
            # почти весь бюджет контекста, длинный диалог ломает запрос.
            trimmed = []
            for msg in history[-HISTORY_MAX_MESSAGES:]:
                content = (msg.get("content") or "")
                if len(content) > HISTORY_MSG_MAX_CHARS:
                    content = content[:HISTORY_MSG_MAX_CHARS] + "\n[…обрезано…]"
                trimmed.append({"role": msg.get("role"), "content": content})
            messages.extend(trimmed)
        if prompt:
            messages.append({"role": "user", "content": prompt})
        return self._sanitize(messages)

    async def _try_provider(self, creds: dict, messages: list, retries: int):
        """Один провайдер с ретраями. Возвращает (текст|None, причина)."""
        payload = {
            "model": creds["model"],
            "messages": messages,
            "temperature": 0.5,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {creds['key']}",
        }
        timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT)
        url = f"{creds['url'].rstrip('/')}/chat/completions"

        last_reason = ""
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, headers=headers, json=payload, timeout=timeout,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            choices = data.get("choices") or []
                            if not choices:
                                last_reason = "пустой ответ модели"
                                await asyncio.sleep((attempt + 1) * 2)
                                continue
                            content = (
                                choices[0].get("message", {}).get("content") or ""
                            ).strip()
                            if not content:
                                last_reason = "модель вернула пустой текст"
                                await asyncio.sleep((attempt + 1) * 2)
                                continue
                            return content, ""
                        body = (await resp.text())[:300]
                        logger.warning(
                            "LLM %s HTTP %s: %s", creds["name"], resp.status, body
                        )
                        last_reason = self._explain_http(resp.status, body)
                        # 4xx (кроме 429) не лечится ретраем — сразу к следующему.
                        if resp.status not in (429, 500, 502, 503, 504):
                            return None, last_reason
                        await asyncio.sleep((attempt + 1) * 2)
            except asyncio.TimeoutError:
                last_reason = f"нет ответа за {LLM_TIMEOUT} с"
                logger.warning("LLM %s timeout", creds["name"])
                await asyncio.sleep((attempt + 1) * 2)
            except Exception as exc:  # noqa: BLE001
                last_reason = f"сетевая ошибка: {str(exc)[:80]}"
                logger.warning("LLM %s error: %s", creds["name"], exc)
                await asyncio.sleep((attempt + 1) * 2)
        return None, last_reason or "причина неизвестна"

    @staticmethod
    def _explain_http(status: int, body: str) -> str:
        """Человеческое объяснение ошибки вместо сухого HTTP-кода.

        Про конкретную переменную окружения здесь не пишем: провайдер может
        быть выбран в меню «🧠 Нейросеть» со своим ключом, и совет «проверьте
        AI_API_KEY» отправил бы пользователя не туда.
        """
        low = (body or "").lower()
        if status == 400 and ("token" in low or "too long" in low or "exceed" in low):
            return (
                "текст слишком длинный для модели — начните новое обсуждение "
                "(кнопка «Другой шаблон») или выберите модель с большим контекстом"
            )
        if status in (401, 403):
            return "ключ отклонён провайдером — проверьте ключ в «Нейросеть»"
        if status == 404:
            return "модель или эндпоинт не найдены — проверьте URL и модель"
        if status == 429:
            return "превышен лимит запросов провайдера — подождите минуту"
        if status == 400:
            return f"провайдер отклонил запрос (HTTP 400): {body[:120]}"
        return f"ошибка провайдера HTTP {status}"


# ---------------------------------------------------------------------------
# YouTube-клиент (yt-dlp): метаданные, форматы, скачивание файлов.
# ---------------------------------------------------------------------------
class YTClient:
    """Извлечение метаданных, форматов и скачивание через yt-dlp."""

    def __init__(self):
        self._yt_dlp = None

    def _module(self):
        """Ленивый импорт yt-dlp (тяжёлый, кэшируем)."""
        if self._yt_dlp is None:
            import yt_dlp  # noqa: WPS433
            self._yt_dlp = yt_dlp
        return self._yt_dlp

    def _base_opts(self):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 15,
            "retries": 2,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.youtube.com/",
            },
        }
        if yt_transcript.YT_PROXY:
            opts["proxy"] = yt_transcript.YT_PROXY
        return opts

    def extract_info(self, video_id: str, client=None, use_cookies=False) -> dict:
        """Метаданные видео и список форматов (синхронно, из executor)."""
        yt = self._module()
        opts = self._base_opts()
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if use_cookies:
            cookies = yt_transcript.get_cookies_file()
            if not cookies:
                raise RuntimeError("куки не настроены")
            opts["cookiefile"] = cookies
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def list_downloadable_formats(self, video_id: str) -> dict:
        """Возвращает {качество: format_id}; пробует TV-клиент, затем web."""
        info = None
        last_exc = None
        for kwargs in ({"client": "tv"}, {}, {"use_cookies": True}):
            try:
                info = self.extract_info(video_id, **kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if info is None:
            raise last_exc or RuntimeError("не удалось получить данные видео")

        video_formats = {}
        audio_formats = {}
        seen_heights = set()
        for fmt in info.get("formats", []):
            has_video = fmt.get("vcodec") not in (None, "none")
            has_audio = fmt.get("acodec") not in (None, "none")
            height = fmt.get("height") or 0
            if has_video and fmt.get("format_id"):
                label = f"{height}p" if height else "видео"
                if label not in seen_heights:
                    seen_heights.add(label)
                    video_formats[fmt["format_id"]] = label
            elif has_audio and fmt.get("format_id") and fmt.get("abr"):
                audio_formats[fmt["format_id"]] = (
                    f"\U0001F3B5 {fmt.get('audio_ext', 'audio')} "
                    f"{fmt.get('abr')}kbps"
                )
        return {
            "title": info.get("title", "video"),
            "video": video_formats,
            "audio": audio_formats,
        }

    def download(self, video_id: str, format_spec: str, dest_dir: str,
                 client=None, use_cookies=False) -> str:
        """Скачивает выбранный формат, возвращает путь к файлу."""
        yt = self._module()
        opts = self._base_opts()
        opts.update(
            {
                "format": format_spec,
                "outtmpl": os.path.join(dest_dir, "%(title).80s.%(ext)s"),
                "noplaylist": True,
            }
        )
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if use_cookies:
            cookies = yt_transcript.get_cookies_file()
            if not cookies:
                raise RuntimeError("куки не настроены")
            opts["cookiefile"] = cookies
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            # Mux-форматы (video+audio) дают итоговое имя, отличное от расчётного.
            files = [
                os.path.join(dest_dir, f)
                for f in os.listdir(dest_dir)
                if os.path.isfile(os.path.join(dest_dir, f))
            ]
            if not files:
                raise RuntimeError("файл не появился после скачивания")
            path = max(files, key=os.path.getmtime)
        return path


# ---------------------------------------------------------------------------
# Распознавание речи (Google Speech Recognition — бесплатно, без загрузки
# моделей; идеально для Render Free, где faster-whisper не помещается в RAM).
# ---------------------------------------------------------------------------
class STTClient:
    """STT через Google Speech API (SpeechRecognition).

    Аудио декодируется pydub/ffmpeg в WAV 16 кГц моно, режется на чанки по
    STT_CHUNK_SECONDS и распознаётся по частям (бесплатный эндпоинт Google
    ограничивает длину одного запроса ~1 минутой).
    """

    def __init__(self, language: str = "ru-RU"):
        self.language = language

    def transcribe_file(self, path: str) -> str:
        """Распознаёт медиафайл (аудио/видео) и возвращает текст."""
        import speech_recognition as sr
        from pydub import AudioSegment

        audio = AudioSegment.from_file(path)
        # Ограничиваем общую длительность и нормализуем формат.
        audio = audio[: MAX_AUDIO_SECONDS * 1000]
        audio = audio.set_channels(1).set_frame_rate(16000)

        recognizer = sr.Recognizer()
        chunk_ms = STT_CHUNK_SECONDS * 1000
        parts = []
        for start in range(0, len(audio), chunk_ms):
            chunk = audio[start:start + chunk_ms]
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                chunk_path = tf.name
            try:
                chunk.export(chunk_path, format="wav")
                with sr.AudioFile(chunk_path) as source:
                    data = recognizer.record(source)
                try:
                    parts.append(recognizer.recognize_google(
                        data, language=self.language
                    ))
                except sr.UnknownValueError:
                    continue  # тишина/шум в этом чанке — пропускаем
                except sr.RequestError as exc:
                    logger.warning("Google STT RequestError: %s", exc)
                    break
            finally:
                try:
                    os.unlink(chunk_path)
                except OSError:
                    pass
        return " ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Telegram-бот.
# ---------------------------------------------------------------------------
class MediaBot:
    """Обработчики сообщений + инлайн-интерфейс с единым диспетчером колбэков."""

    YOUTUBE_ID_RE = re.compile(
        r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|embed/|v/|live/|shorts/)"
        r"|youtu\.be/)([a-zA-Z0-9_-]{11})"
    )
    BARE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

    # Значения callback_data (всё через один диспетчер).
    CB_SEL = "tmpl:"        # применить шаблон
    CB_TXT = "txt"          # прислать транскрипт файлом
    CB_HIST_MENU = "histmenu"
    CB_HIST = "hist:"       # загрузить транскрипт из истории
    CB_DL_MENU = "dlmenu"   # подменю скачивания
    CB_DL_VIDEO = "dlv:"    # скачать видео по format_id
    CB_DL_AUDIO = "dla:"    # скачать аудио по format_id
    CB_BYPASS = "bypass:"   # меню обхода (youtube_bypass.py)
    CB_TMPL_MENU = "tmplmenu"   # меню шаблонов (просмотр/редактирование)
    CB_TMPL_VIEW = "tv:"        # показать текст шаблона
    CB_TMPL_EDIT = "te:"        # начать редактирование шаблона
    CB_TMPL_NEW = "tnew"        # начать создание своего шаблона
    CB_TMPL_DEL = "td:"         # удалить свой шаблон
    CB_OUT_MENU = "outmenu"     # выбрать формат вывода
    CB_OUT_SET = "outset:"      # установить формат вывода (tg/md/both)
    CB_MENU = "menu"            # вернуть главное меню действий
    CB_AI_MENU = "aimenu"       # меню выбора нейросети
    CB_AI = "ai:"               # колбэки настроек ИИ
    CB_RETRY = "retry"          # повторить последний шаблон
    CB_DISCUSS = "disc:"        # выбрать, что обсуждать (transcript|result)

    # Форматы вывода результата.
    OUT_TG = "tg"      # только в чат
    OUT_MD = "md"      # только .md файлом
    OUT_BOTH = "both"  # и в чат, и .md

    def __init__(self, db: BotDatabase, llm: LLMClient, yt: YTClient, stt: STTClient):
        self.db = db
        self.llm = llm
        self.yt = yt
        self.stt = stt
        self.user_sessions = {}
        self._busy = {}       # user_id -> идёт тяжёлая обработка
        self._last_job = {}   # user_id -> monotonic-время последнего запроса
        self._pending_template = {}  # user_id -> ожидание ввода текста шаблона
        self._pending_ai = {}        # user_id -> ожидание ввода url/key/model

    # -- Утилиты -----------------------------------------------------------
    def extract_youtube_id(self, text: str):
        """ID из ссылки или просто 11-символьный ID, присланный сообщением."""
        match = self.YOUTUBE_ID_RE.search(text)
        if match:
            return match.group(1)
        stripped = text.strip()
        if self.BARE_ID_RE.match(stripped):
            return stripped
        return None

    def _llm_chain(self, user_id: int) -> list:
        """Цепочка провайдеров ИИ для пользователя.

        Авто-перебор включён → полная цепочка; выключен → только выбранный.
        """
        settings = self.db.get_ai_settings(user_id)
        creds = ai_providers.resolve(settings, self.llm.api_url, self.llm.model)
        if not settings.get("ai_auto", 1):
            return [creds]
        chain = ai_providers.build_chain(
            settings, self.llm.api_url, self.llm.model
        )
        # Выбранный провайдер всегда первый, дубли убираем.
        result, seen = [], set()
        for item in [creds] + chain:
            fingerprint = (item.get("url"), item.get("model"))
            if not item.get("key") or not item.get("url") or fingerprint in seen:
                continue
            seen.add(fingerprint)
            result.append(item)
        return result

    def _set_session(self, user_id, text, source="youtube", video_id=None,
                     title="", transcript_id=None):
        self.user_sessions[user_id] = {
            "text": text,
            "chat_history": [],
            "source": source,
            "video_id": video_id,
            "title": title,
            "transcript_id": transcript_id,
            "last_template_id": None,   # для «♻️ Повторить»
            "last_result": None,        # последний ответ ИИ
            "discuss_target": "transcript",  # что обсуждаем: transcript | result
        }

    async def _run_sync(self, func, *args, timeout=None):
        """Запуск блокирующей функции в executor с таймаутом."""
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, func, *args), timeout=timeout
        )

    async def _acquire_user(self, user_id: int):
        """Защита от параллельных тяжёлых задач и флуда. Возвращает ошибку."""
        if self._busy.get(user_id):
            return "\u23f3 Подождите — я ещё обрабатываю ваш предыдущий запрос."
        now = time.monotonic()
        last = self._last_job.get(user_id)
        if last is not None and now - last < USER_COOLDOWN_SECONDS:
            return "\u23f3 Слишком часто. Подождите пару секунд."
        self._last_job[user_id] = now
        self._busy[user_id] = True
        return None

    def _release_user(self, user_id: int):
        self._busy[user_id] = False

    @staticmethod
    async def _safe_edit(message, text: str):
        try:
            await message.edit_text(text)
        except Exception:  # noqa: BLE001 — сообщение могли удалить
            pass

    # -- Команды -----------------------------------------------------------
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"\U0001F680 YT_Bot_Sum v{VERSION_STRING}\n\n"
            "Пришлите ссылку на YouTube (или просто ID видео), голосовое, "
            "аудио- или видеофайл — я достану текст и переработаю его через ИИ "
            "по шаблону.\n\n"
            "\U0001F447 Внизу — постоянное меню кнопок: Шаблоны, История, "
            "Формат вывода, Обход YouTube, Помощь. Оно всегда под рукой, "
            "листать вверх не нужно.\n\n"
            "\U0001F4DD Шаблоны: саммари · Пирамида Минто · экшен-план · конспект. "
            "Тексты шаблонов можно смотреть, менять и создавать свои — "
            "кнопка «Шаблоны».",
            reply_markup=self.PERSISTENT_KB,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "\U0001F4D8 Как пользоваться\n\n"
            "1. Пришлите ссылку YouTube / ID видео / аудио / голосовое.\n"
            "2. Появится меню обработки — выберите шаблон.\n"
            "3. После результата меню не исчезает: можно повторить обработку, "
            "выбрать другой шаблон или задать вопрос текстом.\n\n"
            "\u2699\ufe0f Меню обработки — кнопка внизу, открывает шаблоны в любой момент.\n\n"
            "\U0001F9E0 Нейросеть\n"
            "Кнопка «\U0001F9E0 Нейросеть» (или /ai) — выбор ИИ из бесплатных, "
            "авто-перебор при отказе, свои URL/ключ/модель, тест и список моделей.\n\n"
            "\U0001F4AC Обсуждение\n"
            "После обработки вопросы по умолчанию идут по РЕЗУЛЬТАТУ ИИ. "
            "Кнопкой «Обсуждаем: …» можно переключиться на исходный транскрипт. "
            "Если вопрос не прошёл — «Сбросить диалог» и спросить короче.\n\n"
            "\u2699\ufe0f Формат вывода: в чат · .md файлом · оба. "
            "Меняется в любой момент, применяется к следующей обработке "
            "(или сразу — кнопкой «Повторить в этом формате»).\n\n"
            "\U0001F4DD Шаблоны: видно полный текст промпта, можно изменить "
            "или создать свой.\n\n"
            "\U0001F527 Команды\n"
            "/bypass — метод обхода блокировок YouTube + тест\n"
            "/templates, /add_template, /del_template, /cancel\n"
            "/history — последние транскрипты\n"
            "/stats — статистика\n\n"
            "\U0001F510 Транскрипты хранятся в БД бота и доступны только вам.",
            reply_markup=self.PERSISTENT_KB,
        )

    async def add_template_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        text = " ".join(context.args or [])
        if "|" not in text:
            await update.message.reply_text(
                "\u26a0\ufe0f Формат: /add_template ID | Название | Промпт\n"
                "ID — латиница/цифры до 16 симв., например: mynotes"
            )
            return
        try:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 3 or not all(parts[:3]):
                raise ValueError("нужно три части: ID | Название | Промпт")
            template_id, name, prompt = parts[0], parts[1], " | ".join(parts[2:])
            if not TEMPLATE_ID_RE.match(template_id):
                raise ValueError("ID: только латиница/цифры/_-, до 16 символов")
            if len(name) > 64:
                name = name[:64]
            self.db.save_template(update.effective_user.id, template_id, name, prompt)
            await update.message.reply_text(
                f"\u2705 Шаблон «{name}» (ID: {template_id}) сохранён. "
                "Он появится на кнопках."
            )
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"\u274c Ошибка: {exc}")

    async def del_template_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        template_id = " ".join(context.args or []).strip()
        if not template_id:
            await update.message.reply_text("Формат: /del_template ID")
            return
        if template_id in DEFAULT_TEMPLATES:
            await update.message.reply_text(
                "Стандартный шаблон удалить нельзя, но можно переопределить: "
                "/add_template с тем же ID."
            )
            return
        if self.db.delete_template(update.effective_user.id, template_id):
            await update.message.reply_text(f"\u2705 Шаблон {template_id} удалён.")
        else:
            await update.message.reply_text(f"Шаблон {template_id} не найден.")

    async def templates_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        templates = self.db.get_templates(update.effective_user.id)
        lines = ["\U0001F4DC Ваши шаблоны:\n"]
        for tid, t in templates.items():
            mark = "(станд.)" if tid in DEFAULT_TEMPLATES and t == DEFAULT_TEMPLATES.get(tid) else ""
            lines.append(f"• [{tid}] {t['name']} {mark}\n  {t['prompt'][:90]}…")
        lines.append(
            "\nДобавить: /add_template ID | Название | Промпт\n"
            "Удалить: /del_template ID"
        )
        await update.message.reply_text("\n".join(lines))

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        total = self.db.count_transcripts(user_id)
        recent = self.db.get_transcripts(user_id, limit=5)
        lines = [
            "\U0001F4CA Статистика",
            f"Транскриптов сохранено: {total}",
            f"Шаблонов: {len(self.db.get_templates(user_id))}",
            f"Метод обхода: {yt_transcript.METHOD_LABELS.get(self.db.get_bypass_method(user_id), 'авто')}",
        ]
        if recent:
            lines.append("\n\U0001F553 Последние:")
            for r in recent:
                title = (r["title"] or r["source"])[:40]
                lines.append(f"• #{r['id']} {title} · {r['chars']} симв. · {r['date']}")
        await update.message.reply_text("\n".join(lines))

    async def history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._show_history(update.effective_message, update.effective_user.id)

    # -- Приём текста / ссылки --------------------------------------------
    # Подписи постоянной reply-клавиатуры → действия.
    KB_TEMPLATES = "\U0001F4DD Шаблоны"
    KB_HISTORY = "\U0001F559 История"
    KB_OUTPUT = "\u2699\ufe0f Формат вывода"
    KB_BYPASS = "\U0001F513 Обход YouTube"
    KB_MENU = "\u2699\ufe0f Меню обработки"
    KB_AI = "\U0001F9E0 Нейросеть"
    KB_HELP = "\u2139\ufe0f Помощь"

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            text = (update.message.text or "").strip()
            user_id = update.effective_user.id

            # 0. Кнопки постоянной reply-клавиатуры.
            if text == self.KB_MENU:
                session = self.user_sessions.get(user_id)
                if session and session.get("text"):
                    await self._send_action_keyboard(update, context)
                else:
                    await update.message.reply_text(
                        "Сначала пришлите ссылку YouTube, голосовое или аудио — "
                        "затем появится меню обработки.\n"
                        "Или откройте \U0001F559 Историю, чтобы вернуться "
                        "к сохранённому транскрипту.",
                        reply_markup=self.PERSISTENT_KB,
                    )
                return
            if text == self.KB_TEMPLATES:
                await self._show_templates_menu(update, context)
                return
            if text == self.KB_HISTORY:
                await self._show_history(update.effective_message, user_id)
                return
            if text == self.KB_OUTPUT:
                await self._show_output_menu_msg(update, context)
                return
            if text == self.KB_AI:
                await show_ai_menu(
                    update.effective_message, self.db, user_id,
                    self.llm.api_url, self.llm.model,
                )
                return
            if text == self.KB_BYPASS:
                from youtube_bypass import show_bypass_menu
                await show_bypass_menu(
                    update.effective_message, self.db, user_id
                )
                return
            if text == self.KB_HELP:
                await self.help_command(update, context)
                return

            # 1. Ожидание ввода текста нового/редактируемого шаблона.
            pending = self._pending_template.get(user_id)
            if pending:
                await self._finish_template_input(update, context, pending, text)
                return

            # 1b. Ожидание ввода настроек ИИ (URL / ключ / модель).
            pending_ai = self._pending_ai.pop(user_id, None)
            if pending_ai:
                await self._finish_ai_input(update, context, pending_ai, text)
                return

            video_id = self.extract_youtube_id(text)
            if video_id:
                await self._process_youtube(update, context, video_id)
                return

            # Обычный текст → продолжение диалога с ИИ по последнему контенту.
            session = self.user_sessions.get(user_id)
            if session and session.get("text"):
                busy_error = await self._acquire_user(user_id)
                if busy_error:
                    await update.message.reply_text(busy_error)
                    return
                try:
                    status = await update.message.reply_text("\U0001F916 Думаю…")
                    # Что берём в контекст: результат обработки или транскрипт.
                    target = session.get("discuss_target", "transcript")
                    if target == "result" and session.get("last_result"):
                        base_context = (
                            "Результат предыдущей обработки текста:\n\n"
                            f"{session['last_result']}"
                        )
                    else:
                        base_context = session["text"]

                    history = list(session.get("chat_history") or [])
                    history.append({"role": "user", "content": text})
                    reply = await self.llm.complete(
                        "", base_context, history,
                        chain=self._llm_chain(user_id),
                    )
                    if reply.startswith(LLM_ERROR_PREFIX):
                        # Историю не портим неудачным ответом, даём кнопки.
                        await self._safe_edit(status, reply)
                        await self._send_discuss_error_keyboard(update, context)
                    else:
                        history.append({"role": "assistant", "content": reply})
                        session["chat_history"] = history
                        await self._send_long(status, reply)
                finally:
                    self._release_user(user_id)
            else:
                await update.message.reply_text(
                    "Пришлите ссылку на YouTube, ID видео, голосовое или аудиофайл.",
                    reply_markup=self.PERSISTENT_KB,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в handle_text")
            try:
                await update.message.reply_text(f"\u26a0\ufe0f Внутренняя ошибка: {e}")
            except Exception:  # noqa: BLE001
                pass

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._pending_template.pop(update.effective_user.id, None)
        self._pending_ai.pop(update.effective_user.id, None)
        await update.message.reply_text(
            "\u274c Отменено.", reply_markup=self.PERSISTENT_KB
        )

    async def _send_discuss_error_keyboard(self, update, context):
        """Кнопки после неудачного вопроса в обсуждении."""
        session = self.user_sessions.get(update.effective_user.id) or {}
        target = session.get("discuss_target", "transcript")
        rows = [
            [InlineKeyboardButton(
                "\U0001F5D1 Сбросить диалог и спросить заново",
                callback_data=f"{self.CB_DISCUSS}reset",
            )],
            [InlineKeyboardButton(
                ("\U0001F504 Обсуждать транскрипт" if target == "result"
                 else "\U0001F504 Обсуждать результат ИИ"),
                callback_data=f"{self.CB_DISCUSS}toggle",
            )],
            [InlineKeyboardButton(
                "\U0001F4DD Выбрать шаблон обработки", callback_data=self.CB_MENU
            )],
        ]
        await update.effective_message.reply_text(
            "\u26a0\ufe0f Вопрос не прошёл. Частая причина — контекст слишком "
            "разросся. Сбросьте диалог и спросите короче, либо переключите, "
            "что именно обсуждаем.",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _finish_template_input(self, update, context, pending, text):
        """Обрабатывает присланный текст шаблона (создание/редактирование)."""
        user_id = update.effective_user.id
        if text.startswith("/"):  # пользователь передумал и ввёл команду
            self._pending_template.pop(user_id, None)
            await update.message.reply_text(
                "Ввод шаблона отменён.", reply_markup=self.PERSISTENT_KB
            )
            return
        try:
            if pending["mode"] == "edit":
                tid = pending["id"]
                name = pending["name"]
                self.db.save_template(user_id, tid, name, text.strip())
                self._pending_template.pop(user_id, None)
                await update.message.reply_text(
                    f"\u2705 Шаблон «{name}» обновлён. Новый текст сохранён.",
                    reply_markup=self.PERSISTENT_KB,
                )
            else:  # new
                if "|" not in text:
                    await update.message.reply_text(
                        "\u26a0\ufe0f Нужен формат: Название | текст промпта\n"
                        "Попробуйте ещё раз или /cancel."
                    )
                    return
                name, prompt = [p.strip() for p in text.split("|", 1)]
                if not name or not prompt:
                    await update.message.reply_text(
                        "\u26a0\ufe0f И название, и текст обязательны. /cancel для отмены."
                    )
                    return
                # Генерируем свободный ID.
                existing = self.db.get_templates(user_id)
                idx = 1
                while f"u{idx}" in existing:
                    idx += 1
                tid = f"u{idx}"
                self.db.save_template(user_id, tid, name[:64], prompt)
                self._pending_template.pop(user_id, None)
                await update.message.reply_text(
                    f"\u2705 Шаблон «{name[:64]}» создан (ID: {tid}). "
                    "Он появился на кнопках выбора.",
                    reply_markup=self.PERSISTENT_KB,
                )
        except Exception as exc:  # noqa: BLE001
            self._pending_template.pop(user_id, None)
            await update.message.reply_text(f"\u274c Ошибка: {exc}")

    async def _show_output_menu_msg(self, update, context):
        """Меню формата вывода, вызванное reply-кнопкой (не callback)."""
        user_id = update.effective_user.id
        cur = self.db.get_output_format(user_id)
        def mark(f):
            return "\u2705 " if f == cur else ""
        rows = [
            [InlineKeyboardButton(
                f"{mark('tg')}\U0001F4AC Только в чат",
                callback_data=f"{self.CB_OUT_SET}{self.OUT_TG}")],
            [InlineKeyboardButton(
                f"{mark('md')}\U0001F4C4 Только .md файлом",
                callback_data=f"{self.CB_OUT_SET}{self.OUT_MD}")],
            [InlineKeyboardButton(
                f"{mark('both')}\U0001F500 В чат + .md файл",
                callback_data=f"{self.CB_OUT_SET}{self.OUT_BOTH}")],
        ]
        await update.effective_message.reply_text(
            "\u2699\ufe0f Формат вывода результата:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # -- YouTube: транскрипт → сессия → кнопки -----------------------------
    async def _process_youtube(self, update: Update, context, video_id: str):
        user_id = update.effective_user.id
        busy_error = await self._acquire_user(user_id)
        if busy_error:
            await update.message.reply_text(busy_error)
            return
        status = await update.message.reply_text(
            "\U0001F4E5 Получаю транскрипт (цепочка методов обхода)…"
        )
        try:
            transcript, title, method_label = "", "", ""
            # Кэш: недавно обрабатывали это видео — не дёргаем YouTube.
            cached = self.db.get_cached_youtube(video_id)
            if cached:
                transcript, method_label = cached, "\U0001F4C2 кэш"
            else:
                preferred = self.db.get_bypass_method(user_id)
                try:
                    result = await self._run_sync(
                        yt_transcript.fetch_transcript_sync,
                        video_id, preferred, timeout=YT_FETCH_TIMEOUT,
                    )
                    transcript = result.text
                    title = result.title or ""
                    method_label = result.label
                    self.db.save_youtube_cache(video_id, transcript)
                except asyncio.TimeoutError:
                    logger.warning("Цепочка методов не уложилась в таймаут")
                except yt_transcript.TranscriptFetchError as exc:
                    if exc.video_gone:
                        await status.edit_text(
                            "\u274C Похоже, видео недоступно (удалено или приватное)."
                        )
                        return
                    await self._safe_edit(
                        status,
                        "\U0001F3A7 Субтитры не добыты — скачиваю аудио и распознаю "
                        f"речь (Google)…\nДиагностика: {exc.report[0] if exc.report else ''}",
                    )

            if not transcript:
                transcript = await self._transcribe_youtube_audio(status, video_id)
                method_label = "\U0001F399 Google (аудио)"
                if not transcript:
                    await status.edit_text(
                        "\u274C Не удалось получить содержание видео.\n\n"
                        "\U0001F527 Что делать:\n"
                        "• /bypass → «🧪 Тест всех методов» — посмотреть, что проходит\n"
                        "• Настроить куки/прокси (YT_COOKIES / YT_PROXY)\n"
                        "• Или прислать аудиофайл напрямую — распознаю локально."
                    )
                    return

            if not title:
                title = await self._fetch_title_safe(video_id)

            transcript_id = self.db.save_transcript(
                user_id, "youtube", video_id, title, transcript, method_label
            )
            self._set_session(
                user_id, transcript, source="youtube",
                video_id=video_id, title=title, transcript_id=transcript_id,
            )
            preview = transcript[:600] + ("…" if len(transcript) > 600 else "")
            head = f"✅ Транскрипт готов · {method_label} · {len(transcript)} симв."
            if title:
                head += f"\n🎬 {title}"
            await status.edit_text(f"{head}\n\n{preview}")
            await self._send_action_keyboard(update, context)
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в _process_youtube")
            await self._safe_edit(status, f"\u26a0\ufe0f Внутренняя ошибка: {e}")
        finally:
            self._release_user(user_id)

    async def _transcribe_youtube_audio(self, status, video_id: str) -> str:
        """Fallback: скачать аудио (TV-клиент → web → куки) и распознать."""
        for client, use_cookies in (
            ("android_vr", False), ("tv", False), ("mweb", False),
            (None, False), (None, True),
        ):
            if use_cookies and not yt_transcript.get_cookies_file():
                continue
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = await self._run_sync(
                        self.yt.download, video_id, "bestaudio", tmp,
                        client, use_cookies, timeout=YT_DOWNLOAD_TIMEOUT,
                    )
                    await self._safe_edit(
                        status, "\U0001F399 Аудио скачано — распознаю речь (Google)…"
                    )
                    return await self._run_sync(
                        self.stt.transcribe_file, path, timeout=STT_TIMEOUT
                    )
            except (asyncio.TimeoutError, TimeoutError):
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Аудио-fallback (client=%s, cookies=%s): %s",
                    client, use_cookies, exc,
                )
                continue
        return ""

    async def _fetch_title_safe(self, video_id: str) -> str:
        """Название видео: сначала лёгкий oEmbed, затем yt-dlp.

        oEmbed отдаёт заголовок без авторизации и почти никогда не блокируется,
        поэтому в истории и .md названия появляются даже когда yt-dlp упирается
        в bot-check.
        """
        title = await self._fetch_title_oembed(video_id)
        if title:
            return title
        try:
            info = await self._run_sync(
                self.yt.extract_info, video_id, timeout=45
            )
            return (info.get("title") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    async def _fetch_title_oembed(video_id: str) -> str:
        """Заголовок через публичный oEmbed YouTube (без ключей и куки)."""
        url = (
            "https://www.youtube.com/oembed?format=json&url="
            f"https://www.youtube.com/watch?v={video_id}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.json(content_type=None)
            return (data.get("title") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    # -- Приём аудио/видео ---------------------------------------------------
    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        user_id = update.effective_user.id
        file_obj, fmt, kind = self._extract_media(message)
        if not file_obj:
            return
        busy_error = await self._acquire_user(user_id)
        if busy_error:
            await message.reply_text(busy_error)
            return
        status = await message.reply_text("\U0001F4E5 Загружаю файл…")
        try:
            if file_obj.file_size and file_obj.file_size > TELEGRAM_FILE_LIMIT:
                await status.edit_text(
                    "\u26a0\ufe0f Файл больше 20 МБ — Telegram не даёт ботам "
                    "скачивать такие файлы. Пришлите файл меньшего размера "
                    "или ссылку на YouTube."
                )
                return
            tg_file = await context.bot.get_file(file_obj.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            suffix = f".{fmt}" if fmt else ""
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                tf.write(raw)
                media_path = tf.name
            try:
                await status.edit_text(
                    "\U0001F399 Распознаю речь (Google Speech)… на длинном файле "
                    "это может занять до минуты."
                )
                text = await self._run_sync(
                    self.stt.transcribe_file, media_path, timeout=STT_TIMEOUT
                )
            finally:
                try:
                    os.unlink(media_path)
                except OSError:
                    pass
            if not text:
                await status.edit_text(
                    "\u274C Не удалось распознать аудио. Проверьте, что в нём "
                    "есть речь."
                )
                return
            file_name = getattr(file_obj, "file_name", "") or kind
            transcript_id = self.db.save_transcript(
                user_id, kind, None, file_name, text, "Google Speech"
            )
            self._set_session(
                user_id, text, source=kind, video_id=None,
                title=file_name, transcript_id=transcript_id,
            )
            preview = text[:600] + ("…" if len(text) > 600 else "")
            await status.edit_text(
                f"✅ Распознано · {len(text)} симв.\n\n{preview}"
            )
            await self._send_action_keyboard(update, context)
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в handle_media")
            await self._safe_edit(status, f"\u26a0\ufe0f Внутренняя ошибка: {e}")
        finally:
            self._release_user(user_id)

    @staticmethod
    def _extract_media(message):
        """(file_obj, расширение, тип) из voice/audio/video/document."""
        if message.voice:
            return message.voice, "ogg", "voice"
        if message.audio:
            return (
                message.audio,
                MediaBot._ext_from(message.audio.file_name, message.audio.mime_type),
                "audio",
            )
        if message.video:
            return (
                message.video,
                MediaBot._ext_from(message.video.file_name, message.video.mime_type),
                "video",
            )
        doc = message.document
        if doc and (doc.mime_type or "").startswith(("audio/", "video/")):
            return doc, MediaBot._ext_from(doc.file_name, doc.mime_type), "file"
        return None, None, None

    @staticmethod
    def _ext_from(fname, mime_type=None):
        fname = (fname or "").lower()
        if "." in fname:
            ext = fname.rsplit(".", 1)[1]
            if ext.isalnum() and len(ext) <= 5:
                return ext
        mime_map = {
            "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
            "audio/ogg": "ogg", "audio/opus": "ogg", "audio/wav": "wav",
            "audio/x-m4a": "m4a", "audio/webm": "webm",
            "video/mp4": "mp4", "video/webm": "webm",
            "video/x-matroska": "mkv", "video/quicktime": "mov",
        }
        return mime_map.get((mime_type or "").lower(), "")

    # -- Инлайн-интерфейс ---------------------------------------------------
    PERSISTENT_KB = ReplyKeyboardMarkup(
        [
            [KeyboardButton("\u2699\ufe0f Меню обработки")],
            [KeyboardButton("\U0001F4DD Шаблоны"), KeyboardButton("\U0001F559 История")],
            [KeyboardButton("\U0001F9E0 Нейросеть"), KeyboardButton("\u2699\ufe0f Формат вывода")],
            [KeyboardButton("\U0001F513 Обход YouTube"), KeyboardButton("\u2139\ufe0f Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Ссылка YouTube, голосовое, аудио — или вопрос по тексту…",
    )

    def _action_keyboard(self, user_id: int, has_video: bool) -> InlineKeyboardMarkup:
        templates = list(self.db.get_templates(user_id).items())[:12]
        rows = []
        row = []
        for tid, temp in templates:
            row.append(
                InlineKeyboardButton(temp["name"], callback_data=f"{self.CB_SEL}{tid}")
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        cur_fmt = self.db.get_output_format(user_id)
        fmt_label = {
            "tg": "в чат", "md": ".md файл", "both": "чат + .md",
        }.get(cur_fmt, "в чат")
        rows.append(
            [
                InlineKeyboardButton("\U0001F4C4 Транскрипт", callback_data=self.CB_TXT),
                InlineKeyboardButton("\U0001F559 История", callback_data=self.CB_HIST_MENU),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"\u2699\ufe0f Вывод: {fmt_label}", callback_data=self.CB_OUT_MENU
                ),
                InlineKeyboardButton(
                    "\U0001F4DD Шаблоны", callback_data=self.CB_TMPL_MENU
                ),
            ]
        )
        if has_video:
            rows.append(
                [
                    InlineKeyboardButton(
                        "\u2b07\ufe0f Скачать видео/аудио", callback_data=self.CB_DL_MENU
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "\U0001F9E0 Нейросеть", callback_data=self.CB_AI_MENU
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    # -- Настройки ИИ: колбэки, тест, модели, ручной ввод --------------------
    AI_ASK_LABELS = {
        "url": (
            "\U0001F517 Пришлите базовый URL API (без /chat/completions).\n\n"
            "Примеры:\n"
            "• https://api.groq.com/openai/v1\n"
            "• https://openrouter.ai/api/v1\n"
            "• http://localhost:11434/v1 (Ollama)\n\n"
            "/cancel — отмена."
        ),
        "key": (
            "\U0001F511 Пришлите API-ключ.\n\n"
            "Сообщение с ключом будет удалено сразу после сохранения, "
            "в чате ключ показывается только усечённым.\n\n"
            "/cancel — отмена."
        ),
        "model": (
            "\U0001F9E9 Пришлите название модели.\n\n"
            "Например: llama-3.3-70b-versatile, gemini-3.6-flash, "
            "meta-llama/llama-3.3-70b-instruct:free\n\n"
            "Список доступных можно получить кнопкой «\U0001F4E5 Модели».\n\n"
            "/cancel — отмена."
        ),
    }
    AI_ASK_FIELDS = {"url": "ai_api_url", "key": "ai_api_key", "model": "ai_model"}

    async def _handle_ai_callback(self, update, context, action: str):
        query = update.callback_query
        user_id = update.effective_user.id

        if action.startswith("set:"):
            name = action[4:]
            if name != "auto" and name not in ai_providers.CATALOG:
                return
            self.db.set_ai_setting(user_id, "ai_provider", name)
            await show_ai_menu(
                query.message, self.db, user_id,
                self.llm.api_url, self.llm.model, edit=True,
            )
            if name == "custom":
                await query.message.reply_text(
                    "\U0001F527 Свой API выбран. Задайте URL, ключ и модель "
                    "кнопками ниже, затем нажмите «\U0001F9EA Тест»."
                )
            elif not ai_providers.provider_key(name):
                spec = ai_providers.CATALOG[name]
                await query.message.reply_text(
                    f"\U0001F511 Для {spec['label']} нужен ключ.\n\n"
                    f"{spec['note']}\n\n"
                    "Добавьте его кнопкой «\U0001F511 Ключ» или задайте "
                    f"переменную окружения {spec['env']} в настройках сервиса."
                )
            return

        if action == "toggle_auto":
            settings = self.db.get_ai_settings(user_id)
            new_val = 0 if settings.get("ai_auto", 1) else 1
            self.db.set_ai_setting(user_id, "ai_auto", new_val)
            await show_ai_menu(
                query.message, self.db, user_id,
                self.llm.api_url, self.llm.model, edit=True,
            )
            return

        if action == "reset":
            for field in ("ai_api_url", "ai_api_key", "ai_model"):
                self.db.set_ai_setting(user_id, field, None)
            self.db.set_ai_setting(user_id, "ai_provider", "auto")
            self.db.set_ai_setting(user_id, "ai_auto", 1)
            await show_ai_menu(
                query.message, self.db, user_id,
                self.llm.api_url, self.llm.model, edit=True,
            )
            await query.message.reply_text(
                "\u267b\ufe0f Настройки ИИ сброшены: авто-выбор, ключи из "
                "переменных окружения."
            )
            return

        if action.startswith("ask:"):
            what = action[4:]
            if what not in self.AI_ASK_FIELDS:
                return
            self._pending_ai[user_id] = what
            await query.message.reply_text(self.AI_ASK_LABELS[what])
            return

        if action == "test":
            await self._test_ai_provider(update, context)
            return

        if action == "models":
            await self._fetch_ai_models(update, context)
            return

        if action.startswith("model:"):
            model = action[6:]
            self.db.set_ai_setting(user_id, "ai_model", model)
            await show_ai_menu(
                query.message, self.db, user_id,
                self.llm.api_url, self.llm.model, edit=True,
            )
            await query.message.reply_text(f"\u2705 Модель: {model}")
            return

    async def _test_ai_provider(self, update, context):
        query = update.callback_query
        user_id = update.effective_user.id
        settings = self.db.get_ai_settings(user_id)
        auto = bool(settings.get("ai_auto", 1))

        if auto:
            chain = self._llm_chain(user_id)
            if not chain:
                await query.message.reply_text(
                    "\u26a0\ufe0f Нет ни одного провайдера с ключом. "
                    "Добавьте ключ кнопкой «\U0001F511 Ключ»."
                )
                return
            status = await query.message.reply_text(
                f"\U0001F9EA Проверяю цепочку ({len(chain)})…"
            )
            lines = []
            for creds in chain[:6]:
                ok, detail = await ai_providers.test_provider(creds)
                icon = "\u2705" if ok else "\u274C"
                lines.append(f"{icon} {creds['name']} · {creds['model']}\n   {detail}")
            await self._safe_edit(
                status, "\U0001F9EA Результат теста\n\n" + "\n\n".join(lines)
            )
            return

        creds = ai_providers.resolve(settings, self.llm.api_url, self.llm.model)
        status = await query.message.reply_text(
            f"\U0001F9EA Проверяю {creds['name']} · {creds['model']}…"
        )
        ok, detail = await ai_providers.test_provider(creds)
        icon = "\u2705 Работает" if ok else "\u274C Не работает"
        await self._safe_edit(
            status,
            f"{icon}\n\nПровайдер: {creds['name']}\n"
            f"Модель: {creds['model']}\nURL: {creds['url']}\n\n{detail}",
        )

    async def _fetch_ai_models(self, update, context):
        query = update.callback_query
        user_id = update.effective_user.id
        settings = self.db.get_ai_settings(user_id)
        creds = ai_providers.resolve(settings, self.llm.api_url, self.llm.model)
        status = await query.message.reply_text(
            f"\U0001F4E5 Запрашиваю список моделей у {creds['name']}…"
        )
        models, err = await ai_providers.fetch_models(creds)
        if err:
            await self._safe_edit(
                status,
                f"\u274C Не удалось получить список: {err}\n\n"
                "Модель можно задать вручную кнопкой «\U0001F9E9 Модель».",
            )
            return
        if not models:
            await self._safe_edit(status, "\u26a0\ufe0f Провайдер вернул пустой список.")
            return

        # Бесплатные и компактные модели — вперёд: их обычно и хотят.
        def rank(mid: str):
            low = mid.lower()
            return (
                0 if ":free" in low or "-free" in low else 1,
                0 if any(s in low for s in ("flash", "mini", "small", "8b", "7b")) else 1,
                low,
            )

        top = sorted(models, key=rank)[:12]
        rows = [
            [InlineKeyboardButton(m[:60], callback_data=f"{self.CB_AI}model:{m}"[:64])]
            for m in top
        ]
        await self._safe_edit(
            status,
            f"\U0001F4E5 Доступно моделей: {len(models)}. "
            f"Показаны {len(top)} — нажмите, чтобы выбрать.\n\n"
            "Остальные можно задать вручную кнопкой «\U0001F9E9 Модель».",
        )
        await query.message.reply_text(
            "\U0001F9E9 Выберите модель:", reply_markup=InlineKeyboardMarkup(rows)
        )

    async def _finish_ai_input(self, update, context, what: str, text: str):
        """Сохраняет введённые вручную URL / ключ / модель."""
        user_id = update.effective_user.id
        field = self.AI_ASK_FIELDS[what]
        value = text.strip()

        if what == "url":
            if not value.startswith(("http://", "https://")):
                await update.message.reply_text(
                    "\u26a0\ufe0f URL должен начинаться с http:// или https://. "
                    "Попробуйте снова или /cancel."
                )
                self._pending_ai[user_id] = what  # остаёмся в режиме ввода
                return
            value = value.rstrip("/")
            # Частая ошибка: вставляют полный путь до /chat/completions.
            for tail in ("/chat/completions", "/chat"):
                if value.endswith(tail):
                    value = value[: -len(tail)]

        self.db.set_ai_setting(user_id, field, value)
        # Свои креды имеют смысл только с провайдером custom, если URL задан.
        if what == "url":
            self.db.set_ai_setting(user_id, "ai_provider", "custom")

        if what == "key":
            # Ключ из чата удаляем: он не должен оставаться в истории.
            try:
                await update.message.delete()
            except Exception:  # noqa: BLE001
                pass
            await update.effective_chat.send_message(
                "\u2705 Ключ сохранён (сообщение с ключом удалено)."
            )
        else:
            await update.message.reply_text(f"\u2705 Сохранено: {value}")

        await show_ai_menu(
            update.effective_chat, self.db, user_id,
            self.llm.api_url, self.llm.model,
        )

    async def _send_action_keyboard(self, update: Update, context):
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id) or {}
        await update.effective_message.reply_text(
            "\u2699\ufe0f Что сделать с текстом?",
            reply_markup=self._action_keyboard(
                user_id, bool(session.get("video_id"))
            ),
        )

    async def handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        data = query.data or ""

        if data.startswith(self.CB_BYPASS):
            await handle_bypass_callback(update, context, self)
            return
        if data == self.CB_AI_MENU:
            await show_ai_menu(
                query.message, self.db, user_id,
                self.llm.api_url, self.llm.model,
            )
            return
        if data.startswith(self.CB_AI):
            await self._handle_ai_callback(update, context, data[len(self.CB_AI):])
            return
        if data == self.CB_DL_MENU:
            await self._show_download_menu(update, context)
            return
        if data.startswith((self.CB_DL_VIDEO, self.CB_DL_AUDIO)):
            await self._do_download(update, context, data)
            return
        if data.startswith(self.CB_SEL):
            await self._process_template(update, context, data[len(self.CB_SEL):])
            return
        if data == self.CB_TXT:
            await self._send_transcript_file(update, context)
            return
        if data == self.CB_HIST_MENU:
            await self._show_history(query.message, user_id)
            return
        if data.startswith(self.CB_HIST):
            await self._load_history_item(update, context, data[len(self.CB_HIST):])
            return
        if data == self.CB_OUT_MENU:
            await self._show_output_menu(update, context)
            return
        if data.startswith(self.CB_OUT_SET):
            await self._set_output_format(update, context, data[len(self.CB_OUT_SET):])
            return
        if data == self.CB_TMPL_MENU:
            await self._show_templates_menu(update, context)
            return
        if data.startswith(self.CB_TMPL_VIEW):
            await self._view_template(update, context, data[len(self.CB_TMPL_VIEW):])
            return
        if data.startswith(self.CB_TMPL_EDIT):
            await self._start_edit_template(update, context, data[len(self.CB_TMPL_EDIT):])
            return
        if data.startswith(self.CB_TMPL_DEL):
            await self._delete_template_cb(update, context, data[len(self.CB_TMPL_DEL):])
            return
        if data == self.CB_TMPL_NEW:
            await self._start_new_template(update, context)
            return
        if data == self.CB_MENU:
            await self._send_action_keyboard(update, context)
            return
        if data == self.CB_RETRY:
            await self._retry_last_template(update, context)
            return
        if data.startswith(self.CB_DISCUSS):
            action = data[len(self.CB_DISCUSS):]
            if action == "reset":
                await self._reset_discussion(update, context)
            else:
                await self._toggle_discuss_target(update, context)
            return

    # -- Повтор обработки и выбор объекта обсуждения ------------------------
    async def _reset_discussion(self, update, context):
        """Сбрасывает историю диалога, сохраняя транскрипт и результат."""
        session = self.user_sessions.get(update.effective_user.id)
        if not session:
            await self._safe_edit(
                update.callback_query.message, "\u26a0\ufe0f Сессия устарела."
            )
            return
        session["chat_history"] = []
        await self._safe_edit(
            update.callback_query.message,
            "\U0001F5D1 Диалог сброшен. Транскрипт и результат сохранены — "
            "задайте вопрос заново.",
        )

    async def _retry_last_template(self, update, context):
        """Повторяет последний шаблон (после ошибки ИИ или просто ещё раз)."""
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id) or {}
        tid = session.get("last_template_id")
        if not tid:
            await self._send_action_keyboard(update, context)
            return
        await self._process_template(update, context, tid)

    async def _toggle_discuss_target(self, update, context):
        """Переключает, что берётся в контекст при обсуждении текстом."""
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id)
        if not session:
            await self._safe_edit(
                update.callback_query.message, "\u26a0\ufe0f Сессия устарела."
            )
            return
        cur = session.get("discuss_target", "result")
        new = "transcript" if cur == "result" else "result"
        if new == "result" and not session.get("last_result"):
            new = "transcript"
        session["discuss_target"] = new
        # Диалог начинаем заново, чтобы контекст не смешивался.
        session["chat_history"] = []
        label = (
            "результат обработки ИИ" if new == "result" else "исходный транскрипт"
        )
        await self._safe_edit(
            update.callback_query.message,
            f"\u2705 Теперь вопросы обсуждаются по: {label}.\n"
            "История диалога сброшена — задайте вопрос текстом.",
        )

    # -- Формат вывода ------------------------------------------------------
    async def _show_output_menu(self, update, context):
        query = update.callback_query
        cur = self.db.get_output_format(update.effective_user.id)
        def mark(f):
            return "\u2705 " if f == cur else ""
        rows = [
            [InlineKeyboardButton(
                f"{mark('tg')}\U0001F4AC Только в чат",
                callback_data=f"{self.CB_OUT_SET}{self.OUT_TG}")],
            [InlineKeyboardButton(
                f"{mark('md')}\U0001F4C4 Только .md файлом",
                callback_data=f"{self.CB_OUT_SET}{self.OUT_MD}")],
            [InlineKeyboardButton(
                f"{mark('both')}\U0001F500 В чат + .md файл",
                callback_data=f"{self.CB_OUT_SET}{self.OUT_BOTH}")],
        ]
        await self._safe_edit(
            query.message,
            "\u2699\ufe0f Как выводить результат обработки?\n\n"
            "\U0001F4AC в чат — быстро, видно сразу\n"
            "\U0001F4C4 .md — удобно сохранить в Obsidian/заметки\n"
            "\U0001F500 оба варианта",
        )
        try:
            await query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
        except Exception:  # noqa: BLE001
            await query.message.reply_text(
                "Выберите формат:", reply_markup=InlineKeyboardMarkup(rows)
            )

    async def _set_output_format(self, update, context, fmt: str):
        if fmt not in (self.OUT_TG, self.OUT_MD, self.OUT_BOTH):
            return
        self.db.set_output_format(update.effective_user.id, fmt)
        label = {"tg": "только в чат", "md": "только .md файлом",
                 "both": "в чат + .md файл"}[fmt]
        session = self.user_sessions.get(update.effective_user.id) or {}
        rows = []
        if session.get("last_template_id"):
            rows.append([InlineKeyboardButton(
                "\u267b\ufe0f Повторить обработку в этом формате",
                callback_data=self.CB_RETRY,
            )])
        rows.append([InlineKeyboardButton(
            "\U0001F4DD Выбрать шаблон обработки", callback_data=self.CB_MENU
        )])
        await self._safe_edit(
            update.callback_query.message,
            f"\u2705 Формат вывода: {label}.",
        )
        await update.effective_message.reply_text(
            "Дальше:", reply_markup=InlineKeyboardMarkup(rows)
        )

    # -- Меню шаблонов (просмотр текста, создание, редактирование) ----------
    async def _show_templates_menu(self, update, context):
        query = update.callback_query
        user_id = update.effective_user.id
        templates = self.db.get_templates(user_id)
        rows = []
        for tid, t in templates.items():
            rows.append([
                InlineKeyboardButton(
                    f"\U0001F441 {t['name']}", callback_data=f"{self.CB_TMPL_VIEW}{tid}"
                )
            ])
        rows.append([
            InlineKeyboardButton(
                "\u2795 Создать свой шаблон", callback_data=self.CB_TMPL_NEW
            )
        ])
        text = (
            "\U0001F4DD Шаблоны обработки\n\n"
            "Нажмите на шаблон, чтобы увидеть его текст (промпт), "
            "изменить или удалить. Стандартные можно переопределить своим "
            "текстом — они помечаются как изменённые."
        )
        if query:
            await self._safe_edit(query.message, text)
            try:
                await query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
            except Exception:  # noqa: BLE001
                await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
        else:
            await update.effective_message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(rows)
            )

    async def _view_template(self, update, context, tid: str):
        query = update.callback_query
        user_id = update.effective_user.id
        t = self.db.get_templates(user_id).get(tid)
        if not t:
            await self._safe_edit(query.message, "\u26a0\ufe0f Шаблон не найден.")
            return
        is_default = tid in DEFAULT_TEMPLATES
        rows = [
            [InlineKeyboardButton(
                "\u270f\ufe0f Изменить текст", callback_data=f"{self.CB_TMPL_EDIT}{tid}"
            )],
        ]
        if not is_default:
            rows.append([InlineKeyboardButton(
                "\U0001F5d1 Удалить", callback_data=f"{self.CB_TMPL_DEL}{tid}"
            )])
        rows.append([InlineKeyboardButton(
            "\u2b05\ufe0f К списку шаблонов", callback_data=self.CB_TMPL_MENU
        )])
        note = " (стандартный)" if is_default else " (ваш)"
        await self._safe_edit(
            query.message,
            f"\U0001F4DD {t['name']}{note}\nID: {tid}\n\n"
            f"\U0001F4C4 Текст промпта:\n\u2014\u2014\u2014\n{t['prompt']}\n\u2014\u2014\u2014\n\n"
            "Изменить — кнопка ниже. Также командой:\n"
            f"/add_template {tid} | {t['name']} | новый текст",
        )
        try:
            await query.message.edit_reply_markup(InlineKeyboardMarkup(rows))
        except Exception:  # noqa: BLE001
            await query.message.reply_text("Действия:", reply_markup=InlineKeyboardMarkup(rows))

    async def _start_edit_template(self, update, context, tid: str):
        user_id = update.effective_user.id
        t = self.db.get_templates(user_id).get(tid)
        if not t:
            return
        # Запоминаем режим ожидания нового текста для этого шаблона.
        self._pending_template[user_id] = {"mode": "edit", "id": tid, "name": t["name"]}
        await self._safe_edit(
            update.callback_query.message,
            f"\u270f\ufe0f Редактирование «{t['name']}» (ID: {tid}).\n\n"
            "Пришлите СЛЕДУЮЩИМ сообщением новый текст промпта — "
            "инструкцию для ИИ, как обрабатывать текст.\n\n"
            "Отмена: /cancel",
        )

    async def _start_new_template(self, update, context):
        user_id = update.effective_user.id
        self._pending_template[user_id] = {"mode": "new", "step": "id"}
        await self._safe_edit(
            update.callback_query.message,
            "\u2795 Новый шаблон.\n\n"
            "Пришлите одним сообщением в формате:\n"
            "Название | текст промпта\n\n"
            "Например:\n"
            "Перевод на английский | Переведи текст на английский, сохрани смысл.\n\n"
            "Отмена: /cancel",
        )

    async def _delete_template_cb(self, update, context, tid: str):
        user_id = update.effective_user.id
        if tid in DEFAULT_TEMPLATES:
            await self._safe_edit(
                update.callback_query.message,
                "Стандартный шаблон удалить нельзя (можно переопределить текст).",
            )
            return
        self.db.delete_template(user_id, tid)
        await self._show_templates_menu(update, context)

    async def _process_template(self, update, context, template_id: str):
        query = update.callback_query
        try:
            session = self.user_sessions.get(update.effective_user.id)
            if not session or not session.get("text"):
                await self._safe_edit(
                    query.message, "\u26a0\ufe0f Сессия устарела — откройте /history "
                    "или пришлите ссылку/аудио заново."
                )
                return
            template = self.db.get_templates(update.effective_user.id).get(template_id)
            if not template:
                await self._safe_edit(query.message, "\u26a0\ufe0f Шаблон не найден.")
                return
            busy_error = await self._acquire_user(update.effective_user.id)
            if busy_error:
                await query.message.reply_text(busy_error)
                return
            try:
                await self._safe_edit(
                    query.message,
                    f"\U0001F916 Применяю шаблон «{template['name']}»…",
                )
                result = await self.llm.complete(
                    template["prompt"], session["text"],
                    chain=self._llm_chain(update.effective_user.id),
                )
                failed = result.startswith(LLM_ERROR_PREFIX)
                session["last_template_id"] = template_id
                if not failed:
                    session["last_result"] = result
                    session["chat_history"] = [
                        {"role": "user", "content": template["prompt"]},
                        {"role": "assistant", "content": result},
                    ]
                    session["discuss_target"] = "result"
                    await self._deliver_result(
                        update, context, result, template["name"], session,
                    )
                else:
                    # Ошибка ИИ: показываем причину и даём повтор/смену шаблона.
                    await update.effective_message.reply_text(result)
                await self._send_after_result_keyboard(update, context, failed=failed)
            finally:
                self._release_user(update.effective_user.id)
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в _process_template")
            await self._safe_edit(query.message, f"\u26a0\ufe0f Внутренняя ошибка: {e}")
            await self._send_after_result_keyboard(update, context, failed=True)

    async def _send_after_result_keyboard(self, update, context, failed=False):
        """Меню после обработки: повтор, другой шаблон, что обсуждать, вывод."""
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id) or {}
        rows = []
        if failed:
            rows.append([InlineKeyboardButton(
                "\u267b\ufe0f Повторить этот шаблон", callback_data=self.CB_RETRY
            )])
        else:
            rows.append([
                InlineKeyboardButton(
                    "\u267b\ufe0f Ещё раз", callback_data=self.CB_RETRY
                ),
                InlineKeyboardButton(
                    "\U0001F504 Другой шаблон", callback_data=self.CB_MENU
                ),
            ])
            target = session.get("discuss_target", "result")
            rows.append([InlineKeyboardButton(
                ("\U0001F4AC Обсуждаем: результат \u2192 переключить на транскрипт"
                 if target == "result"
                 else "\U0001F4AC Обсуждаем: транскрипт \u2192 переключить на результат"),
                callback_data=f"{self.CB_DISCUSS}toggle",
            )])
        if failed:
            rows.append([InlineKeyboardButton(
                "\U0001F504 Выбрать другой шаблон", callback_data=self.CB_MENU
            )])
        rows.append([
            InlineKeyboardButton(
                "\u2699\ufe0f Формат вывода", callback_data=self.CB_OUT_MENU
            ),
            InlineKeyboardButton(
                "\U0001F4C4 Транскрипт", callback_data=self.CB_TXT
            ),
        ])
        hint = (
            "\u26a0\ufe0f ИИ не справился. Можно повторить или выбрать другой шаблон."
            if failed else
            "\U0001F4AC Дальше: задайте вопрос текстом, повторите обработку "
            "или выберите другой шаблон."
        )
        await update.effective_message.reply_text(
            hint, reply_markup=InlineKeyboardMarkup(rows)
        )

    async def _deliver_result(self, update, context, result, template_name, session):
        """Выдаёт результат согласно выбранному формату вывода (tg/md/both)."""
        user_id = update.effective_user.id
        out_fmt = self.db.get_output_format(user_id)
        message = update.effective_message
        # В чат.
        if out_fmt in (self.OUT_TG, self.OUT_BOTH):
            await self._send_long(message, result)
        # .md файлом.
        if out_fmt in (self.OUT_MD, self.OUT_BOTH):
            md = self._build_markdown(result, template_name, session)
            filename = self._md_filename(template_name, session)
            tmp_path = os.path.join(tempfile.gettempdir(), filename)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(md)
            try:
                title = (session.get("title") or "").strip()
                caption = f"\U0001F4C4 {template_name}"
                if title:
                    caption += f"\n\U0001F3AC {title[:120]}"
                with open(tmp_path, "rb") as fh:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=fh,
                        filename=filename,
                        caption=caption,
                    )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _slugify(text: str, limit: int = 60) -> str:
        """Безопасное имя файла из названия: сохраняем кириллицу и пробелы."""
        text = (text or "").strip()
        # Удаляем только то, что реально запрещено в именах файлов.
        text = re.sub(r'[\\/:*?"<>|\n\r\t]', " ", text)
        text = re.sub(r"\s+", " ", text).strip(" .")
        if len(text) > limit:
            text = text[:limit].rstrip()
        return text

    def _md_filename(self, template_name: str, session) -> str:
        """Имя .md: «Название видео — Краткое саммари — 2026-08-21.md»."""
        from datetime import datetime
        title = self._slugify(session.get("title") or "")
        if not title:
            fallback = {
                "youtube": "Видео YouTube",
                "voice": "Голосовое сообщение",
                "audio": "Аудиозапись",
                "video": "Видеофайл",
                "file": "Медиафайл",
            }
            title = fallback.get(session.get("source") or "", "Транскрипт")
            if session.get("video_id"):
                title += f" {session['video_id']}"
        # Из названия шаблона убираем эмодзи-префикс, оставляя слова.
        tmpl = self._slugify(re.sub(r"^\W+", "", template_name or ""), 40) or "Обработка"
        date = datetime.now().strftime("%Y-%m-%d")
        return f"{title} — {tmpl} — {date}.md"

    @staticmethod
    def _build_markdown(result, template_name, session) -> str:
        """Готовая заметка для Obsidian: YAML-frontmatter + заголовок + текст."""
        from datetime import datetime
        title = (session.get("title") or "").strip()
        source = session.get("source") or ""
        video_id = session.get("video_id")
        now = datetime.now()

        if not title:
            fallback = {
                "youtube": "Видео YouTube",
                "voice": "Голосовое сообщение",
                "audio": "Аудиозапись",
                "video": "Видеофайл",
                "file": "Медиафайл",
            }
            title = fallback.get(source, "Транскрипт")

        # YAML-frontmatter: Obsidian показывает это как свойства заметки.
        safe_title = title.replace('"', "'")
        fm = [
            "---",
            f'title: "{safe_title}"',
            f"date: {now.strftime('%Y-%m-%d %H:%M')}",
            f"source: {source or 'unknown'}",
        ]
        if video_id:
            fm.append(f"url: https://youtu.be/{video_id}")
        fm.append(f'processing: "{template_name}"')
        fm.append("tags: [транскрипт, yt-bot-sum]")
        fm.append("---")

        body = [
            "",
            f"# {title}",
            "",
        ]
        if video_id:
            body.append(f"\U0001F3AC [Смотреть на YouTube](https://youtu.be/{video_id})")
            body.append("")
        body.append(f"> Обработка: **{template_name}** · {now.strftime('%d.%m.%Y %H:%M')}")
        body += ["", "---", "", result, ""]
        return "\n".join(fm + body)

    async def _send_transcript_file(self, update, context):
        query = update.callback_query
        session = self.user_sessions.get(update.effective_user.id)
        if not session or not session.get("text"):
            await self._safe_edit(query.message, "\u26a0\ufe0f Сессия устарела.")
            return
        base = re.sub(
            r"[^\w\s.-]", "", session.get("title") or ""
        ).strip()[:60] or f"transcript_{session.get('transcript_id') or 'session'}"
        tmp_path = os.path.join(
            tempfile.gettempdir(), f"{base.replace(' ', '_')}.txt"
        )
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(session["text"])
        try:
            with open(tmp_path, "rb") as fh:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=fh,
                    filename=f"{base.replace(' ', '_')}.txt",
                    caption=f"\U0001F4C4 Транскрипт · {len(session['text'])} символов",
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Иконки источников для истории.
    SOURCE_ICONS = {
        "youtube": "\U0001F4FA",  # 📺
        "voice": "\U0001F3A4",    # 🎤
        "audio": "\U0001F3B5",    # 🎵
        "video": "\U0001F3AC",    # 🎬
        "file": "\U0001F4C1",     # 📁
    }

    @staticmethod
    def _pretty_size(chars: int) -> str:
        """Человекочитаемый объём: 12 400 симв. → «12.4к»."""
        if chars >= 1000:
            return f"{chars / 1000:.1f}к".replace(".0к", "к")
        return str(chars)

    @staticmethod
    def _pretty_date(raw: str) -> str:
        """'2026-08-21 18:30' → 'сегодня 18:30' / '21.08 18:30'."""
        from datetime import datetime, timedelta
        raw = (raw or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(raw[:19] if len(raw) >= 19 else raw, fmt)
            except ValueError:
                continue
            today = datetime.now().date()
            if dt.date() == today:
                return f"сегодня {dt:%H:%M}"
            if dt.date() == today - timedelta(days=1):
                return f"вчера {dt:%H:%M}"
            return f"{dt:%d.%m %H:%M}"
        return raw[:16]

    def _history_label(self, item: dict) -> str:
        """Красивая подпись кнопки истории: иконка, название, объём, дата."""
        icon = self.SOURCE_ICONS.get(item.get("source") or "", "\U0001F4C4")
        title = (item.get("title") or "").strip()
        if not title:
            # Названия нет (файл без имени / видео без метаданных) —
            # показываем осмысленный человеку заголовок вместо «youtube».
            fallback = {
                "youtube": "Видео YouTube",
                "voice": "Голосовое сообщение",
                "audio": "Аудиозапись",
                "video": "Видеофайл",
                "file": "Медиафайл",
            }
            title = fallback.get(item.get("source") or "", "Транскрипт")
            if item.get("video_id"):
                title += f" ({item['video_id']})"
        # Убираем расширение у файлов — оно шумит в кнопке.
        title = re.sub(r"\.(mp3|mp4|wav|m4a|ogg|webm|mkv|mov|aac)$", "", title, flags=re.I)
        if len(title) > 30:
            title = title[:29].rstrip() + "…"
        size = self._pretty_size(item.get("chars") or 0)
        date = self._pretty_date(item.get("date") or "")
        return f"{icon} {title} · {size} · {date}"

    async def _backfill_titles(self, user_id: int, items: list):
        """Дописывает пропавшие названия YouTube-видео через oEmbed.

        Раньше при bot-check название не сохранялось и в истории оставался
        безликий «youtube» — теперь оно доклеивается при первом показе списка.
        """
        missing = [
            it for it in items
            if it.get("video_id") and not (it.get("title") or "").strip()
        ][:5]  # не больше 5 запросов за раз, чтобы меню не тормозило
        if not missing:
            return
        results = await asyncio.gather(
            *(self._fetch_title_oembed(it["video_id"]) for it in missing),
            return_exceptions=True,
        )
        for item, title in zip(missing, results):
            if isinstance(title, str) and title.strip():
                item["title"] = title.strip()
                self.db.update_transcript_title(item["id"], user_id, title.strip())

    async def _show_history(self, message, user_id: int):
        items = self.db.get_transcripts(user_id, limit=8)
        if not items:
            await message.reply_text(
                "\U0001F559 История пуста.\n\n"
                "Пришлите ссылку на YouTube, голосовое или аудиофайл — "
                "транскрипт сохранится здесь и его можно будет обработать "
                "любым шаблоном повторно.",
                reply_markup=self.PERSISTENT_KB,
            )
            return
        await self._backfill_titles(user_id, items)
        rows = [
            [InlineKeyboardButton(
                self._history_label(it),
                callback_data=f"{self.CB_HIST}{it['id']}",
            )]
            for it in items
        ]
        total = self.db.count_transcripts(user_id)
        header = (
            f"\U0001F559 Сохранённые транскрипты ({len(items)} из {total})\n\n"
            "Нажмите на запись — она станет активной, и её можно обработать "
            "любым шаблоном заново."
        )
        await message.reply_text(header, reply_markup=InlineKeyboardMarkup(rows))

    async def _load_history_item(self, update, context, transcript_id: str):
        query = update.callback_query
        try:
            tid = int(transcript_id)
        except ValueError:
            return
        item = self.db.get_transcript_by_id(tid, update.effective_user.id)
        if not item:
            await self._safe_edit(query.message, "\u26a0\ufe0f Транскрипт не найден.")
            return
        self._set_session(
            update.effective_user.id, item["text"], source=item["source"],
            video_id=item["video_id"], title=item["title"] or "",
            transcript_id=item["id"],
        )
        icon = self.SOURCE_ICONS.get(item.get("source") or "", "\U0001F4C4")
        title = (item.get("title") or "").strip() or "без названия"
        preview = item["text"][:400] + ("…" if len(item["text"]) > 400 else "")
        head = (
            f"\u2705 Загружено: {icon} {title}\n"
            f"{len(item['text'])} символов"
        )
        if item.get("video_id"):
            head += f" · https://youtu.be/{item['video_id']}"
        await query.message.reply_text(f"{head}\n\n{preview}")
        await self._send_action_keyboard(update, context)

    async def _show_download_menu(self, update, context):
        query = update.callback_query
        session = self.user_sessions.get(update.effective_user.id)
        if not session or not session.get("video_id"):
            await self._safe_edit(
                query.message,
                "\u26a0\ufe0f Скачивание доступно только для YouTube-видео.",
            )
            return
        video_id = session["video_id"]
        try:
            fmts = await self._run_sync(
                self.yt.list_downloadable_formats, video_id, timeout=60
            )
        except Exception as exc:  # noqa: BLE001
            await self._safe_edit(query.message, f"\u274C Не удалось получить форматы: {exc}")
            return
        rows = []
        video_items = sorted(
            fmts["video"].items(),
            key=lambda kv: int(kv[1][:-1]) if kv[1][:-1].isdigit() else 0,
            reverse=True,
        )
        for fid, label in video_items:
            rows.append(
                [InlineKeyboardButton(
                    f"\U0001F3A5 {label}", callback_data=f"{self.CB_DL_VIDEO}{fid}"
                )]
            )
        for fid, label in fmts["audio"].items():
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"{self.CB_DL_AUDIO}{fid}")]
            )
        if not rows:
            await self._safe_edit(query.message, "\u26a0\ufe0f Форматы не найдены.")
            return
        await query.edit_message_text(
            "\u2b07\ufe0f Выберите формат для скачивания:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _do_download(self, update, context, data: str):
        query = update.callback_query
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id)
        if not session or not session.get("video_id"):
            await self._safe_edit(
                query.message, "\u26a0\ufe0f Скачивание доступно только для YouTube-видео."
            )
            await self._send_nav(update, context)
            return
        if data.startswith(self.CB_DL_VIDEO):
            fmt_id = data[len(self.CB_DL_VIDEO):]
            format_spec = f"{fmt_id}+bestaudio/best"
        else:
            fmt_id = data[len(self.CB_DL_AUDIO):]
            format_spec = fmt_id

        busy_error = await self._acquire_user(user_id)
        if busy_error:
            await query.message.reply_text(busy_error)
            return

        await self._safe_edit(query.message, "\u23f3 Скачиваю файл…")
        tmp = tempfile.mkdtemp()
        try:
            path, last_exc = None, None
            # Перебираем клиенты, как при аудио-fallback: web-клиент YouTube
            # часто требует авторизацию, а android_vr/tv отдают файл без куки.
            attempts = [
                ("android_vr", False), ("tv", False), ("mweb", False),
                (None, False), (None, True),
            ]
            for client, use_cookies in attempts:
                if use_cookies and not yt_transcript.get_cookies_file():
                    continue
                try:
                    label = client or ("web+куки" if use_cookies else "web")
                    await self._safe_edit(
                        query.message, f"\u23f3 Скачиваю файл… (клиент {label})"
                    )
                    path = await self._run_sync(
                        self.yt.download, session["video_id"], format_spec, tmp,
                        client, use_cookies, timeout=YT_DOWNLOAD_TIMEOUT,
                    )
                    if path and os.path.exists(path):
                        break
                    path = None
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    last_exc = exc
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    logger.warning(
                        "Скачивание (client=%s, cookies=%s): %s",
                        client, use_cookies, str(exc)[:120],
                    )
                    continue

            if not path:
                await self._safe_edit(
                    query.message, self._download_error_text(last_exc)
                )
                await self._send_nav(update, context)
                return

            size = os.path.getsize(path)
            if size > 45 * 1024 * 1024:  # лимит отправки Telegram ~50 МБ
                await self._safe_edit(
                    query.message,
                    f"\u26a0\ufe0f Файл {size // (1024*1024)} МБ — Telegram "
                    "пропускает до 50 МБ. Выберите качество ниже или аудио.",
                )
                await self._send_nav(update, context)
                return
            with open(path, "rb") as fh:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=fh,
                    filename=os.path.basename(path),
                )
            await self._safe_edit(query.message, "\u2705 Файл отправлен.")
            await self._send_nav(update, context)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка в _do_download")
            await self._safe_edit(query.message, self._download_error_text(exc))
            await self._send_nav(update, context)
        finally:
            self._release_user(user_id)
            threading.Timer(5.0, _cleanup_dir, args=(tmp,)).start()

    @staticmethod
    def _download_error_text(exc) -> str:
        """Человеческое объяснение вместо сырой ошибки yt-dlp."""
        msg = str(exc or "").lower()
        if any(
            s in msg for s in
            ("cookie", "sign in", "not a bot", "confirm you", "403", "429",
             "too many requests", "bot")
        ):
            return (
                "\u274C YouTube потребовал авторизацию для скачивания этого "
                "файла (bot-check) — все клиенты (android_vr / tv / mweb / web) "
                "отклонены.\n\n"
                "\U0001F527 Что помогает:\n"
                "• транскрипт обычно всё равно доступен — обработайте текст "
                "шаблоном, кнопки ниже;\n"
                "• для файлов нужны куки: задайте YT_COOKIES (содержимое "
                "cookies.txt) или YT_PROXY в настройках сервиса;\n"
                "• часто выручает выбор аудио вместо видео."
            )
        if "requested format" in msg or "format is not available" in msg:
            return (
                "\u274C Этот формат недоступен для скачивания. "
                "Выберите другое качество или аудио."
            )
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in msg:
            return (
                "\u274C Скачивание не уложилось в лимит времени. "
                "Попробуйте качество ниже или аудио."
            )
        return f"\u274C Не удалось скачать файл: {str(exc)[:150]}"

    async def _send_nav(self, update, context, note: str = ""):
        """Навигация после ЛЮБОГО действия — меню никогда не теряется."""
        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id) or {}
        if not session.get("text"):
            await update.effective_message.reply_text(
                note or "Пришлите ссылку YouTube, голосовое или аудио.",
                reply_markup=self.PERSISTENT_KB,
            )
            return
        await update.effective_message.reply_text(
            note or "\u2699\ufe0f Что дальше?",
            reply_markup=self._action_keyboard(
                user_id, bool(session.get("video_id"))
            ),
        )

    async def _send_long(self, message, text: str):
        """Отправляет длинный текст кусками по 4000 символов (plain text)."""
        if not text:
            return
        for idx in range(0, len(text), 4000):
            await message.reply_text(text[idx:idx + 4000])


def _cleanup_dir(path: str):
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Фейковый HTTP-сервер (для Render Free Web Service).
# ---------------------------------------------------------------------------
def run_health_server(port: int):
    """Отвечает 200 на любой GET — чтобы Web Service прошёл health-check."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: ARG002
            pass

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health-check сервер слушает порт %s", port)
    server.serve_forever()


class MediaFilter(filters.MessageFilter):
    """Голосовые / аудио / видео / аудио-видео документы."""

    def filter(self, message):
        if message.voice or message.audio or message.video:
            return True
        doc = message.document
        return bool(doc and (doc.mime_type or "").startswith(("audio/", "video/")))


async def _post_init(app):
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Запуск и справка"),
                BotCommand("templates", "Шаблоны: смотреть/менять/создавать"),
                BotCommand("history", "Сохранённые транскрипты"),
                BotCommand("ai", "Выбор нейросети, свои ключи, тест"),
                BotCommand("bypass", "Методы обхода YouTube"),
                BotCommand("add_template", "Добавить шаблон"),
                BotCommand("del_template", "Удалить шаблон"),
                BotCommand("cancel", "Отменить ввод"),
                BotCommand("stats", "Статистика"),
                BotCommand("help", "Помощь"),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("set_my_commands: %s", exc)


def main() -> None:
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[!] Укажите реальный TELEGRAM_BOT_TOKEN!")
        return

    threading.Thread(target=run_health_server, args=(PORT,), daemon=True).start()

    db = BotDatabase(DB_PATH)
    llm = LLMClient(AI_API_KEY, AI_API_URL, AI_MODEL)
    yt = YTClient()
    stt = STTClient(STT_LANGUAGE)
    bot = MediaBot(db, llm, yt, stt)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", bot.start_command))
    app.add_handler(CommandHandler("transcribe", bot.start_command))
    app.add_handler(CommandHandler("help", bot.help_command))
    app.add_handler(CommandHandler("templates", bot.templates_command))
    app.add_handler(CommandHandler("add_template", bot.add_template_command))
    app.add_handler(CommandHandler("del_template", bot.del_template_command))
    app.add_handler(CommandHandler("stats", bot.stats_command))
    app.add_handler(CommandHandler("history", bot.history_command))
    app.add_handler(CommandHandler("cancel", bot.cancel_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text)
    )
    app.add_handler(MessageHandler(MediaFilter(), bot.handle_media))
    app.add_handler(CallbackQueryHandler(bot.handle_callback_query))

    register_bypass_command(app, bot)
    register_ai_command(app, bot)

    print(f"Бот {VERSION_STRING} запущен: polling + health-check сервер.")
    app.run_polling()


if __name__ == "__main__":
    main()
