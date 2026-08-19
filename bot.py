"""YT_Bot_Sum — YouTube/voice summarizer bot.

Принимает:
  - ссылку YouTube / Shorts;
  - голосовое сообщение;
  - аудиофайл (mp3/wav/m4a/ogg).

Что делает:
  1. Достаёт контент: субтитры (все языки + автогенерация) через yt-dlp,
     либо распознаёт речь локально через faster-whisper (Whisper large-v3-turbo).
  2. Перерабатывает текст через LLM (OpenRouter, OpenAI-совместимый API)
     по шаблону: саммари / Пирамида Минто / экшен-план / конспект.
  3. Даёт инлайн-кнопки: выбор шаблона + скачивание видео/аудио в любом качестве.

Архитектура (модульная):
  BotDatabase  — слой SQLite (шаблоны, кэш YouTube, история диалогов);
  LLMClient    — обращение к LLM-провайдеру;
  YTClient     — извлечение субтитров и списка форматов через yt-dlp;
  STTClient    — распознавание речи (faster-whisper локально);
  MediaBot     — Telegram-обработчики и инлайн-интерфейс.

Запуск: python bot.py
Деплой: Render Web Service (Free) + фиктивный HTTP-сервер /health.
"""

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

__version__ = "4.1.4"
VERSION_STRING = __version__  # Keep in sync

import aiohttp
from pydub import AudioSegment
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Конфигурация из окружения (env-переменные задаются в панели Render).
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_URL = os.getenv("AI_API_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-4o-mini")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")
PORT = int(os.getenv("PORT", "10000"))

# Модель локального распознавания речи (faster-whisper).
# "small" — баланс точности и размера (~460 МБ, помещается в Free Render).
# Для продуктива: "large-v3-turbo" (задаётся через env, требует больше памяти).
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

# Сколько секунд аудио держать в памяти для распознавания (меньше = быстрее).
MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "1800"))

# YouTube-куки для обхода bot-check (YouTube всё чаще требует авторизацию).
# Вариант A: путь к файлу с куками (Netscape-формат), экспортировать через
#   yt-dlp --cookies-from-browser chrome -o cookies.txt "URL"
# Вариант B: имя браузера, из которого взять куки ("chrome", "firefox", "safari"...).
#   Работает только если бот запущен на той же машине, где есть этот браузер (не на Render).
YT_COOKIES_FILE = os.getenv("YT_COOKIES_FILE", "")
YT_COOKIES_FROM_BROWSER = os.getenv("YT_COOKIES_FROM_BROWSER", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Исключение для обнаружения блокировки YouTube
class YouTubeBlockingError(Exception):
    """Исключение, возникающее при обнаружении блокировки запроса YouTube."""
    pass


def yt_cookie_opts() -> dict:
    """Возвращает опции yt-dlp для передачи куки (если заданы в env)."""
    opts = {}
    if YT_COOKIES_FILE:
        opts["cookiefile"] = YT_COOKIES_FILE
    if YT_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YT_COOKIES_FROM_BROWSER,)
    return opts

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
        "name": "\u2705 Экшен-план (Action Items)",
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

# ---------------------------------------------------------------------------
# База данных.
# ---------------------------------------------------------------------------
class BotDatabase:
    """Слой работы с SQLite: шаблоны, кэш YouTube, история диалогов."""

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
        conn.commit()
        conn.close()

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
    """Обращение к OpenAI-совместимому API (OpenRouter)."""

    def __init__(self, api_key: str, api_url: str, model: str):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.model = model

    async def complete(self, prompt: str, context_text: str, history=None, retries=3):
        if not self.api_key:
            return "\u26a0\ufe0f Ошибка: API ключ для нейросети не настроен."

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
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
                        f"{context_text}"
                    ),
                }
            )
        if history:
            messages.extend(history)
        else:
            messages.append({"role": "user", "content": prompt})

        payload = {"model": self.model, "messages": messages, "temperature": 0.5}

        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=60,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["choices"][0]["message"]["content"].strip()
                        if resp.status in (429, 500, 502, 503, 504):
                            await asyncio.sleep((attempt + 1) * 2)
            except Exception:  # noqa: BLE001
                await asyncio.sleep((attempt + 1) * 2)

        return (
            "\u26a0\ufe0f Облачный ИИ сейчас недоступен. "
            "Пожалуйста, повторите попытку позже."
        )

# ---------------------------------------------------------------------------
# YouTube-клиент (yt-dlp).
# ---------------------------------------------------------------------------
class YTClient:
    """Извлечение метаданных, форматов и субтитров через yt-dlp."""

    def __init__(self):
        self._yt_dlp = None

    def _module(self):
        """Ленивый импорт yt-dlp (тяжёлый, кэшируем)."""
        if self._yt_dlp is None:
            import yt_dlp  # noqa: WPS433
            self._yt_dlp = yt_dlp
        return self._yt_dlp

    def extract_info(self, video_id: str) -> dict:
        """Метаданные видео и список форматов (синхронно, из executor)."""
        yt = self._module()
        url = f"https://www.youtube.com/watch?v={video_id}"
        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        opts.update(yt_cookie_opts())
        with yt.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def fetch_subtitles(self, video_id: str) -> str:
        """Возвращает текст субтитров (ru → en → авто), либо пустую строку.
        Raises YouTubeBlockingError if YouTube blocks the request.
        """
        yt = self._module()
        url = f"https://www.youtube.com/watch?v={video_id}"
        with tempfile.TemporaryDirectory() as tmpdir:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,   # мы хотим только субтитры
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["ru", "en"],
                "subtitlesformat": "vtt",
                "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
                # Таймауты, чтобы не зависать вечно при блокировке
                "http_timeout": 15,
                "socket_timeout": 10,
                # Имитируем браузер, чтобы уменьшить шансы блокировки
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://www.youtube.com/",
                },
            }
            opts.update(yt_cookie_opts())
            try:
                with yt.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                err_str = str(e).lower()
                # YouTube bot-check: "Sign in to confirm you're not a bot".
                if (
                    "confirm you" in err_str
                    or "not a bot" in err_str
                    or "sign in" in err_str
                    or "cookies" in err_str
                    or "429" in err_str
                    or "403" in err_str
                    or "too many requests" in err_str
                    or "blocked" in err_str
                ):
                    raise YouTubeBlockingError(
                        "YouTube требует подтверждения, что вы не бот "
                        "(bot-check), и блокирует выкачку субтитров.\n"
                        "Чтобы это обойти, задайте куки: переменная "
                        "YT_COOKIES_FILE (путь к cookies.txt) или "
                        "YT_COOKIES_FROM_BROWSER (chrome/firefox/safari).\n"
                        "Подробнее: https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
                    )
                logger.error("YT subtitle download failed: %s", e)
                return ""

            # Список загруженных .vtt-файлов
            subtitle_files = [
                os.path.join(tmpdir, f)
                for f in os.listdir(tmpdir)
                if f.endswith(".vtt")
            ]
            if not subtitle_files:
                # Если файлов нет — вероятно, видео без субтитров (не блокировка).
                return ""

            # Извлекаем язык из имени файла
            def _lang_from_name(path: str):
                base = os.path.basename(path)
                name = base[:-4]  # без .vtt
                m = re.search(r'[_-]([a-z]{2})(?:[_-]|$)', name)
                return m.group(1) if m else None

            # Оценка приоритета: 0 - ru, 1 - en, 2 - другой
            def _score(path: str):
                lang = _lang_from_name(path) or ""
                if lang == "ru":
                    return 0
                if lang == "en":
                    return 1
                return 2

            subtitle_files.sort(key=_score)

            for sub_path in subtitle_files:
                try:
                    with open(sub_path, "r", encoding="utf-8") as f:
                        vtt_content = f.read()
                except Exception:
                    continue

                # Если получено HTML-сообщение об ошибке — считаем это блокировкой
                stripped = vtt_content.strip()
                if stripped.startswith("<!DOCTYPE html>") or "<html" in stripped.lower():
                    raise YouTubeBlockingError(
                        "YouTube возвращает HTML-страницу ошибки (возможно, блокировка). "
                        "Пожалуйста, попробуйте позже или задайте куки (YT_COOKIES_FILE)."
                    )

                # Обрабатываем VTT‑контент (удаляем таймкоды и служебные строки)
                text = self._process_vtt(vtt_content)
                if text.strip():
                    return text
            return ""

    @staticmethod
    def _process_vtt(vtt_text: str) -> str:
        """Очищает raw VTT‑текст: удаляет таймкоды, служебные строки и дубли."""
        lines = []
        for line in vtt_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped == "WEBVTT" or "-->" in stripped:
                continue
            if stripped.isdigit():
                continue
            lines.append(stripped)
        # Убираем дублирующиеся подряд строки
        deduped = []
        for line in lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)
        return " ".join(deduped)

    @staticmethod
    def _download_vtt(url: str) -> str:
        """Скачивает VTT‑субтитры по URL и убирает таймкоды/служебные строки."""
        import requests
        try:
            resp = requests.get(url, timeout=20)  # добавляем разумный таймаут
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to download VTT from {url}: {e}")
            return ""
        return YTClient._process_vtt(resp.text)

    def list_downloadable_formats(self, video_id: str) -> dict:
        """Возвращает {качество: format_id} для видео и аудио отдельно."""
        info = self.extract_info(video_id)
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

    def download(self, video_id: str, format_id: str, dest_dir: str) -> str:
        """Скачивает конкретный формат, возвращает путь к файлу."""
        yt = self._module()
        url = f"https://www.youtube.com/watch?v={video_id}"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "format": format_id,
            "outtmpl": os.path.join(dest_dir, "%(title).80s.%(ext)s"),
            "noplaylist": True,
        }
        opts.update(yt_cookie_opts())
        with yt.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

# ---------------------------------------------------------------------------
# Распознавание речи (faster-whisper, локально — как в Hermes).
# ---------------------------------------------------------------------------
class STTClient:
    """Локальный STT на faster-whisper; Google Speech — как лёгкий fallback."""

    def __init__(self, model_name: str = "large-v3-turbo"):
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
        """Распознаёт аудиофайл локально (faster-whisper)."""
        model = self._ensure_model()
        segments, _info = model.transcribe(path, language="ru")
        return " ".join(seg.text.strip() for seg in segments)

    def transcribe_bytes(self, data: bytes, fmt: str) -> str:
        """Конвертирует аудио в wav через pydub и распознаёт локально."""
        audio = AudioSegment.from_file(BytesIO(data), format=fmt)
        if len(audio) > MAX_AUDIO_SECONDS * 1000:
            audio = audio[: MAX_AUDIO_SECONDS * 1000]
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tf:
            audio.export(tf.name, format="wav")
            return self.transcribe_file(tf.name)

# ---------------------------------------------------------------------------
# Telegram-бот.
# ---------------------------------------------------------------------------
class MediaBot:
    """Обработчики сообщений + инлайн-интерфейс."""

    YOUTUBE_ID_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|v/|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})")

    # Значения callback_data.
    CB_SEL = "tmpl_"      # выбрать шаблон
    CB_DL_MENU = "dlmenu"  # открыть подменю скачивания
    CB_DL_VIDEO = "dlv_"   # скачать видео по format_id
    CB_DL_AUDIO = "dla_"   # скачать аудио по format_id

    def __init__(self, db: BotDatabase, llm: LLMClient, yt: YTClient, stt: STTClient):
        self.db = db
        self.llm = llm
        self.yt = yt
        self.stt = stt
        self.user_sessions = {}

    # -- Утилиты -----------------------------------------------------------
    def extract_youtube_id(self, url: str):
        match = self.YOUTUBE_ID_RE.search(url)
        return match.group(1) if match else None

    def _set_session(self, user_id, text, source="youtube", video_id=None, title=""):
        self.user_sessions[user_id] = {
            "text": text,
            "chat_history": [],
            "source": source,
            "video_id": video_id,
            "title": title,
        }

    async def _run_sync(self, func, *args, timeout=30):
        """Запуск синхронной функции в отдельном потоке с таймаутом."""
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, func, *args),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Задача {func.__name__} не завершена за {timeout}s — "
                "YouTube медленный или заблокировал запрос."
            )

    # -- Команды -----------------------------------------------------------
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            f"\U0001F680 **{VERSION_STRING}**\n\n"
            "Пришлите YouTube-ссылку, голосовое или аудиофайл.\n"
            "Я достану содержание и переработаю его через ИИ.\n\n"
            "**Шаблоны:** саммари · Пирамида Минто · экшен-план · конспект.\n"
            "**Доп. команды:**\n"
            "\U0001F4DD `/add_template ID | Название | Промпт` — свой шаблон."
        )
        await update.message.reply_text(text, parse_mode="Markdown")

    async def add_template_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = update.effective_user.id
        text = " ".join(context.args)
        if not text or "|" not in text:
            await update.message.reply_text(
                "\u26a0\ufe0f Формат: `/add_template ID | Название | Промпт`"
            )
            return
        try:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 3:
                raise ValueError("Не все поля заполнены.")
            self.db.save_template(user_id, parts[0], parts[1], parts[2])
            await update.message.reply_text(
                f"\u2705 Шаблон *'{parts[1]}'* (ID: {parts[0]}) сохранён."
            )
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(
                f"\u274c Ошибка добавления шаблона: {exc}"
            )

    # -- Приём текста / ссылки --------------------------------------------
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            text = (update.message.text or "").strip()
            video_id = self.extract_youtube_id(text)
            user_id = update.effective_user.id

            if video_id:
                status = await update.message.reply_text(
                    "\U0001F4E5 Извлекаю видео и субтитры..."
                )
                transcript = None
                blocked = False
                try:
                    transcript = await self._run_sync(
                        self.yt.fetch_subtitles, video_id, timeout=45
                    )
                except YouTubeBlockingError as e:
                    # Субтитры заблокированы bot-check → пробуем аудио-fallback.
                    await status.edit_text(
                        "\U0001F3A7 Субтитры заблокированы YouTube — "
                        "пробую скачать аудио и распознать речь (Whisper)..."
                    )
                    try:
                        transcript = await asyncio.wait_for(
                            self._transcribe_youtube_audio(video_id),
                            timeout=60
                        )
                    except YouTubeBlockingError:
                        blocked = True
                    except asyncio.TimeoutError:
                        blocked = True
                if blocked:
                    await status.edit_text(
                        "\u26a0\ufe0f YouTube блокирует выкачку (bot-check). "
                        "Задайте куки: YT_COOKIES_FILE (cookies.txt) или "
                        "YT_COOKIES_FROM_BROWSER (chrome/firefox/safari)."
                    )
                    return
                if not transcript:
                    # Нет субтитров → пробуем скачать аудио и распознать локально.
                    await status.edit_text(
                        "\U0001F3A7 Субтитров нет — распознаю речь локально (Whisper)..."
                    )
                    try:
                        transcript = await asyncio.wait_for(
                            self._transcribe_youtube_audio(video_id),
                            timeout=60
                        )
                    except asyncio.TimeoutError:
                        transcript = None
                if not transcript:
                    await status.edit_text(
                        "\u274c Не удалось получить содержание видео (YouTube блокирует или нет субтитров)."
                    )
                    return
                title = ""
                try:
                    info = await self._run_sync(self.yt.extract_info, video_id)
                    title = info.get("title", "")
                except Exception:  # noqa: BLE001
                    pass
                self._set_session(
                    user_id, transcript, source="youtube",
                    video_id=video_id, title=title,
                )
                await status.edit_text(
                    f"\u2705 Готово:\n**{title}**\n\n{transcript[:400]}..."
                )
                await self._send_action_keyboard(update, context)
                return

            # Обычный текст → продолжение диалога с ИИ.
            session = self.user_sessions.get(user_id)
            if session and session.get("text"):
                status = await update.message.reply_text("\U0001F916 Думаю...")
                session["chat_history"].append({"role": "user", "content": text})
                reply = await self.llm.complete(
                    "", session["text"], session["chat_history"]
                )
                session["chat_history"].append({"role": "assistant", "content": reply})
                await status.edit_text(reply)
            else:
                await update.message.reply_text(
                    "Пришлите YouTube-ссылку, голосовое или аудиофайл."
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в handle_text")
            try:
                await update.message.reply_text(
                    f"\u26a0\ufe0f Внутренняя ошибка: {e}"
                )
            except Exception:
                pass  # если даже не удалось отправить, просто логгируем

    async def _transcribe_youtube_audio(self, video_id: str, timeout=60) -> str:
        """Скачивает аудио и распознаёт локально (fallback без субтитров).

        Raises YouTubeBlockingError если выкачка аудио заблокирована.
        Raises TimeoutError если операция заняла слишком много времени.
        """
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = await self._run_sync(
                    self.yt.download, video_id, "bestaudio", tmp, timeout=timeout
                )
                if not os.path.exists(path):
                    return ""
                return await self._run_sync(
                    self.stt.transcribe_file, path, timeout=timeout
                )
        except YouTubeBlockingError:
            raise
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка распознавания аудио YT: %s", exc)
            return ""

    # -- Приём аудио -------------------------------------------------------
    async def handle_audio_or_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        try:
            status = await update.message.reply_text("\U0001F4E5 Обрабатываю аудио...")
            is_voice = update.message.voice is not None
            file_obj = update.message.voice if is_voice else update.message.audio
            fmt = "ogg" if is_voice else self._fmt_from_filename(file_obj.file_name)
            tg_file = await context.bot.get_file(file_obj.file_id)
            raw = await tg_file.download_as_bytearray()
            await status.edit_text("\U0001F399 Распознаю речь (Whisper)...")
            text = await self._run_sync(
                self.stt.transcribe_bytes, bytes(raw), fmt
            )
            if not text:
                await status.edit_text("\u274c Не удалось распознать аудио.")
                return
            user_id = update.effective_user.id
            self._set_session(user_id, text, source="audio")
            await status.edit_text(
                f"\U0001F5E3 **Распознанный текст:**\n{text[:300]}..."
            )
            await self._send_action_keyboard(update, context)
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в handle_audio_or_voice")
            try:
                await update.message.reply_text(
                    f"\u26a0\ufe0f Внутренняя ошибка: {e}"
                )
            except Exception:
                pass

    @staticmethod
    def _fmt_from_filename(fname):
        fname = (fname or "").lower()
        for suffix in ("mp3", "wav", "m4a", "aac"):
            if fname.endswith(suffix):
                return suffix
        return "ogg"

    # -- Инлайн-интерфейс ---------------------------------------------------
    async def _send_action_keyboard(self, update: Update, context):
        user_id = update.effective_user.id
        templates = self.db.get_templates(user_id)
        rows = [
            [
                InlineKeyboardButton(
                    temp["name"], callback_data=f"{self.CB_SEL}{tid}"
                )
            ]
            for tid, temp in templates.items()
        ]
        rows.append(
            [InlineKeyboardButton(
                "\u2b07\ufe0f Скачать видео/аудио", callback_data=self.CB_DL_MENU
            )]
        )
        await update.effective_message.reply_text(
            "\u2699\ufe0f **Что сделать с контентом?**",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        data = query.data or ""

        if data == self.CB_DL_MENU:
            await self._show_download_menu(update, context)
            return
        if data.startswith(self.CB_DL_VIDEO) or data.startswith(self.CB_DL_AUDIO):
            await self._do_download(update, context, data)
            return
        if data.startswith(self.CB_SEL):
            template_id = data[len(self.CB_SEL):]
            await self._process_template(update, context, template_id)
            return

    async def _process_template(self, update, context, template_id):
        try:
            user_id = update.effective_user.id
            session = self.user_sessions.get(user_id)
            if not session or not session.get("text"):
                await update.callback_query.edit_message_text(
                    "\u26a0\ufe0f Сессия устарела — отправьте ссылку/аудио заново."
                )
                return
            templates = self.db.get_templates(user_id)
            template = templates.get(template_id)
            if not template:
                return
            await update.callback_query.edit_message_text(
                f"\U0001F916 Применяю шаблон *{template['name']}*..."
            )
            result = await self.llm.complete(template["prompt"], session["text"])
            session["chat_history"] = [{"role": "assistant", "content": result}]
            await self._send_long(update.callback_query.message, result)
            await update.effective_message.reply_text(
                "\U0001F4AC Можно задать вопрос по этому контенту — просто напишите текст."
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в _process_template")
            try:
                await update.callback_query.edit_message_text(
                    f"\u26a0\ufe0f Внутренняя ошибка: {e}"
                )
            except Exception:
                pass

    async def _show_download_menu(self, update, context):
        try:
            session = self.user_sessions.get(update.effective_user.id)
            if not session or not session.get("video_id"):
                await update.callback_query.edit_message_text(
                    "\u26a0\ufe0f Скачивание доступно только для YouTube-ссылок."
                )
                return
            video_id = session["video_id"]
            try:
                fmts = await self._run_sync(
                    self.yt.list_downloadable_formats, video_id
                )
            except Exception as exc:  # noqa: BLE001
                await update.callback_query.edit_message_text(
                    f"\u274c Не удалось получить форматы: {exc}"
                )
                return
            rows = []
            # Видео-форматы по убыванию разрешения.
            video_ids = sorted(
                fmts["video"].items(),
                key=lambda kv: int(kv[1].replace("p", "")) if kv[1].replace("p", "").isdigit() else 0,
                reverse=True,
            )
            for fid, label in video_ids:
                rows.append([InlineKeyboardButton(
                    f"\U0001F3A5 {label}", callback_data=f"{self.CB_DL_VIDEO}{fid}"
                )])
            for fid, label in fmts["audio"].items():
                rows.append([InlineKeyboardButton(
                    label, callback_data=f"{self.CB_DL_AUDIO}{fid}"
                )])
            await update.callback_query.edit_message_text(
                "\u2b07\ufe0f **Выберите формат для скачивания:**",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в _show_download_menu")
            try:
                await update.callback_query.edit_message_text(
                    f"\u26a0\ufe0f Внутренняя ошибка: {e}"
                )
            except Exception:
                pass

    async def _do_download(self, update, context, data):
        try:
            session = self.user_sessions.get(update.effective_user.id)
            if not session or not session.get("video_id"):
                await update.callback_query.edit_message_text(
                    "\u26a0\ufe0f Скачивание доступно только для YouTube-ссылок."
                )
                return
            if data.startswith(self.CB_DL_VIDEO):
                fmt_id = data[len(self.CB_DL_VIDEO):]
                format_spec = f"{fmt_id}+bestaudio/best"
            else:
                fmt_id = data[len(self.CB_DL_AUDIO):]
                format_spec = fmt_id
            await update.callback_query.edit_message_text(
                "\u23f3 Скачиваю файл..."
            )
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    path = await self._run_sync(
                        self.yt.download, session["video_id"], format_spec, tmp
                    )
                    size = os.path.getsize(path) if os.path.exists(path) else 0
                    if size > 45 * 1024 * 1024:  # лимит Telegram ~50 МБ
                        await update.callback_query.edit_message_text(
                            "\u26a0\ufe0f Файл больше 50 МБ — Telegram не пропустит. "
                            "Выберите качество ниже или аудио."
                        )
                        return
                    with open(path, "rb") as fh:
                        await context.bot.send_document(
                            chat_id=update.effective_chat.id, document=fh
                        )
                    await update.callback_query.edit_message_text(
                        "\u2705 Файл отправлен."
                    )
            except Exception as exc:  # noqa: BLE001
                await update.callback_query.edit_message_text(
                    f"\u274c Ошибка скачивания: {exc}"
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("Ошибка в _do_download")
            try:
                await update.callback_query.edit_message_text(
                    f"\u26a0\ufe0f Внутренняя ошибка: {e}"
                )
            except Exception:
                pass

    async def _send_long(self, message, text):
        """Отправляет длинный текст кусками по 4000 символов."""
        for idx in range(0, len(text), 4000):
            await message.reply_text(text[idx:idx + 4000])

# ---------------------------------------------------------------------------
# Фейковый HTTP-сервер (для Render Free Web Service).
# ---------------------------------------------------------------------------
def run_health_server(port: int):
    """Отвечает 200 на любой GET — чтобы Web Service не заснул и прошёл health-check."""

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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", bot.start_command))
    app.add_handler(CommandHandler("add_template", bot.add_template_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, bot.handle_audio_or_voice)
    )
    app.add_handler(CallbackQueryHandler(bot.handle_callback_query))

    # ========================================================================
    # ИНТЕГРАЦИЯ: Модуль транскрибации (Буквица v2.1)
    # ========================================================================
    try:
        from transcription import register_transcription_handlers
        register_transcription_handlers(app, DB_PATH)
        logger.info("✓ Transcription module loaded (Буквица v2.1)")
        logger.info("✓ YouTube bypass buttons added")
        logger.info("✓ Test mode available")
    except Exception as e:
        logger.warning(f"⚠ Could not load transcription module: {e}")

    print(f"Бот {VERSION_STRING} запущен: polling + health-check сервер.")
    app.run_polling()

if __name__ == "__main__":
    main()