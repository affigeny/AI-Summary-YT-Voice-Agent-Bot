"""YT_Bot_Sum — YouTube/voice summarizer bot (v5.0).

Принимает:
  - ссылку YouTube / Shorts или просто ID видео (11 символов);
  - голосовое сообщение;
  - аудиофайл (mp3/wav/m4a/ogg/…) и видеофайл (mp4/mkv/webm/…).

Что делает:
  1. Достаёт транскрипт: цепочка методов обхода блокировок YouTube
     (InnerTube API → yt-dlp web → TV-клиент → Android VR → Invidious →
     Piped → куки; см. yt_transcript.py), а если субтитров нет — скачивает
     аудио и распознаёт речь локально (faster-whisper).
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
  STTClient    — распознавание речи (faster-whisper локально);
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
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import yt_transcript
from youtube_bypass import handle_bypass_callback, register_bypass_command

__version__ = "5.2.0"
VERSION_STRING = __version__

# ---------------------------------------------------------------------------
# Конфигурация из окружения (env-переменные задаются в панели Render).
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_URL = os.getenv("AI_API_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-exp:free")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")
PORT = int(os.getenv("PORT", "10000"))

# Модель локального распознавания речи (faster-whisper).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

# Лимиты аудио и таймауты: распознавание на CPU Render Free — минуты, не секунды.
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "1800"))
WHISPER_TIMEOUT = int(os.getenv("WHISPER_TIMEOUT", "900"))   # на одно аудио
YT_FETCH_TIMEOUT = int(os.getenv("YT_FETCH_TIMEOUT", "180"))  # на цепочку методов
YT_DOWNLOAD_TIMEOUT = int(os.getenv("YT_DOWNLOAD_TIMEOUT", "300"))

# Лимиты LLM: длинный транскрипт урезаем до LLM_MAX_CHARS (голова+хвост).
LLM_MAX_CHARS = int(os.getenv("LLM_MAX_CHARS", "24000"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "240"))

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
                bypass_method TEXT DEFAULT 'auto'
            )
            """
        )
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

    async def complete(self, prompt: str, context_text: str, history=None, retries=2):
        if not self.api_key:
            return (
                "\u26a0\ufe0f API-ключ нейросети не настроен. "
                "Задайте AI_API_KEY в настройках сервиса."
            )

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
            messages.extend(history)
        else:
            messages.append({"role": "user", "content": prompt})

        payload = {"model": self.model, "messages": messages, "temperature": 0.5}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT)

        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=timeout,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            content = data["choices"][0]["message"]["content"]
                            return content.strip()
                        body = (await resp.text())[:200]
                        logger.warning(
                            "LLM HTTP %s: %s", resp.status, body
                        )
                        if resp.status not in (429, 500, 502, 503, 504):
                            return (
                                f"\u26a0\ufe0f Ошибка нейросети (HTTP {resp.status}). "
                                "Проверьте AI_API_KEY / AI_MODEL."
                            )
                        await asyncio.sleep((attempt + 1) * 2)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM error: %s", exc)
                await asyncio.sleep((attempt + 1) * 2)

        return (
            "\u26a0\ufe0f Облачный ИИ сейчас недоступен. "
            "Пожалуйста, повторите попытку позже."
        )


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
# Распознавание речи (faster-whisper, локально).
# ---------------------------------------------------------------------------
class STTClient:
    """Локальный STT на faster-whisper (язык определяется автоматически)."""

    def __init__(self, model_name: str = "small"):
        self.model_name = model_name
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # noqa: WPS433
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8"
            )
            logger.info("Whisper-модель %s загружена", self.model_name)
        return self._model

    def transcribe_file(self, path: str) -> str:
        """Распознаёт медиафайл (аудио/видео — faster-whisper декодирует сам)."""
        model = self._ensure_model()
        segments, _info = model.transcribe(path, vad_filter=True)
        parts = []
        for seg in segments:
            if seg.start > MAX_AUDIO_SECONDS:
                break
            parts.append(seg.text.strip())
        return " ".join(p for p in parts if p)


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

    def __init__(self, db: BotDatabase, llm: LLMClient, yt: YTClient, stt: STTClient):
        self.db = db
        self.llm = llm
        self.yt = yt
        self.stt = stt
        self.user_sessions = {}
        self._busy = {}       # user_id -> идёт тяжёлая обработка
        self._last_job = {}   # user_id -> monotonic-время последнего запроса

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

    def _set_session(self, user_id, text, source="youtube", video_id=None,
                     title="", transcript_id=None):
        self.user_sessions[user_id] = {
            "text": text,
            "chat_history": [],
            "source": source,
            "video_id": video_id,
            "title": title,
            "transcript_id": transcript_id,
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
            "\U0001F4DD Шаблоны: саммари · Пирамида Минто · экшен-план · конспект "
            "— выбор кнопками после транскрипта. Свои: /add_template\n"
            "\U0001F527 Команды:\n"
            "/bypass — методы обхода блокировок YouTube\n"
            "/templates — список шаблонов, /del_template — удалить свой\n"
            "/history — сохранённые транскрипты\n"
            "/stats — статистика, /help — справка"
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "\U0001F4D8 Как пользоваться\n\n"
            "1. Пришлите ссылку YouTube / ID видео / аудио / голосовое.\n"
            "2. Транскрипт сохраняется; появятся кнопки шаблонов.\n"
            "3. Нажмите шаблон — получите результат; дальше можно просто "
            "писать вопросы по тексту.\n\n"
            "\U0001F527 Команды\n"
            "/bypass — выбор метода обхода блокировок + тест методов\n"
            "/add_template ID | Название | Промпт — свой шаблон\n"
            "/templates, /del_template ID\n"
            "/history — последние транскрипты (можно обработать заново)\n"
            "/stats — статистика\n\n"
            "\U0001F510 Приватность: транскрипты хранятся в БД бота и доступны "
            "только вам через /history."
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
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            text = (update.message.text or "").strip()
            user_id = update.effective_user.id
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
                    session["chat_history"].append({"role": "user", "content": text})
                    reply = await self.llm.complete(
                        "", session["text"], session["chat_history"]
                    )
                    session["chat_history"].append(
                        {"role": "assistant", "content": reply}
                    )
                    await self._send_long(status, reply)
                finally:
                    self._release_user(user_id)
            else:
                await update.message.reply_text(
                    "Пришлите ссылку на YouTube, ID видео, голосовое или аудиофайл."
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в handle_text")
            try:
                await update.message.reply_text(f"\u26a0\ufe0f Внутренняя ошибка: {e}")
            except Exception:  # noqa: BLE001
                pass

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
                        f"речь (Whisper)…\nДиагностика: {exc.report[0] if exc.report else ''}",
                    )

            if not transcript:
                transcript = await self._transcribe_youtube_audio(status, video_id)
                method_label = "\U0001F399 Whisper (аудио)"
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
                        status, "\U0001F399 Аудио скачано — распознаю речь (Whisper)…"
                    )
                    return await self._run_sync(
                        self.stt.transcribe_file, path, timeout=WHISPER_TIMEOUT
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
        try:
            info = await self._run_sync(
                self.yt.extract_info, video_id, timeout=45
            )
            return info.get("title", "")
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
                    "\U0001F399 Распознаю речь (Whisper)… на длинном файле это "
                    "может занять несколько минут."
                )
                text = await self._run_sync(
                    self.stt.transcribe_file, media_path, timeout=WHISPER_TIMEOUT
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
                user_id, kind, None, file_name, text, "Whisper"
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
        rows.append(
            [
                InlineKeyboardButton("\U0001F4C4 Полный транскрипт", callback_data=self.CB_TXT),
                InlineKeyboardButton("\U0001F559 История", callback_data=self.CB_HIST_MENU),
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
        return InlineKeyboardMarkup(rows)

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
            await self._safe_edit(
                query.message,
                f"\U0001F916 Применяю шаблон «{template['name']}»…",
            )
            result = await self.llm.complete(template["prompt"], session["text"])
            session["chat_history"] = [{"role": "assistant", "content": result}]
            await self._send_long(query.message, result)
            await query.message.reply_text(
                "\U0001F4AC Можно задать вопрос по этому тексту — просто напишите."
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в _process_template")
            await self._safe_edit(query.message, f"\u26a0\ufe0f Внутренняя ошибка: {e}")

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

    async def _show_history(self, message, user_id: int):
        items = self.db.get_transcripts(user_id, limit=8)
        if not items:
            await message.reply_text(
                "\U0001F559 История пуста — пришлите ссылку или аудио."
            )
            return
        rows = []
        for it in items:
            title = (it["title"] or it["source"])[:38]
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🔁 #{it['id']} {title} · {it['chars']} симв.",
                        callback_data=f"{self.CB_HIST}{it['id']}",
                    )
                ]
            )
        await message.reply_text(
            "\U0001F559 Последние транскрипты — нажмите, чтобы обработать заново:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

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
        preview = item["text"][:300] + ("…" if len(item["text"]) > 300 else "")
        await query.message.reply_text(
            f"✅ Загружен транскрипт #{item['id']} · {len(item['text'])} симв.\n\n{preview}"
        )
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
        session = self.user_sessions.get(update.effective_user.id)
        if not session or not session.get("video_id"):
            await self._safe_edit(
                query.message, "\u26a0\ufe0f Скачивание доступно только для YouTube-видео."
            )
            return
        if data.startswith(self.CB_DL_VIDEO):
            fmt_id = data[len(self.CB_DL_VIDEO):]
            format_spec = f"{fmt_id}+bestaudio/best"
        else:
            fmt_id = data[len(self.CB_DL_AUDIO):]
            format_spec = fmt_id
        await self._safe_edit(query.message, "\u23f3 Скачиваю файл…")
        tmp = tempfile.mkdtemp()
        try:
            path = await self._run_sync(
                self.yt.download, session["video_id"], format_spec, tmp,
                timeout=YT_DOWNLOAD_TIMEOUT,
            )
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size > 45 * 1024 * 1024:  # лимит отправки Telegram ~50 МБ
                await self._safe_edit(
                    query.message,
                    "\u26a0\ufe0f Файл больше 50 МБ — Telegram не пропустит. "
                    "Выберите качество ниже или аудио.",
                )
                return
            with open(path, "rb") as fh:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id, document=fh
                )
            await self._safe_edit(query.message, "\u2705 Файл отправлен.")
        except asyncio.TimeoutError:
            await self._safe_edit(
                query.message, "\u274C Скачивание заняло слишком долго — попробуйте аудио."
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка в _do_download")
            await self._safe_edit(query.message, f"\u274C Ошибка скачивания: {exc}")
        finally:
            threading.Timer(5.0, _cleanup_dir, args=(tmp,)).start()

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
                BotCommand("bypass", "Методы обхода YouTube"),
                BotCommand("templates", "Мои шаблоны"),
                BotCommand("add_template", "Добавить шаблон"),
                BotCommand("del_template", "Удалить шаблон"),
                BotCommand("history", "Сохранённые транскрипты"),
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
    stt = STTClient(WHISPER_MODEL)
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
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text)
    )
    app.add_handler(MessageHandler(MediaFilter(), bot.handle_media))
    app.add_handler(CallbackQueryHandler(bot.handle_callback_query))

    register_bypass_command(app, bot)

    print(f"Бот {VERSION_STRING} запущен: polling + health-check сервер.")
    app.run_polling()


if __name__ == "__main__":
    main()
