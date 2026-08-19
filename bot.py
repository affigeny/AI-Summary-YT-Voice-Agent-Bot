"""
Advanced Media / YouTube / Voice AI Agent Bot — PEP8 edition.

Telegram-бот: принимает YouTube-ссылку, голосовое или аудиофайл,
извлекает транскрипт, затем перерабатывает его через LLM (OpenRouter)
по выбранному пользователем шаблону (саммари, Пирамида Минто, экшен-план).

Запуск: python bot.py
Режим деплоя: Render Web Service (Free) — поднимает фиктивный
HTTP-сервер на /health, чтобы пройти health-check и не уснуть.
"""

import asyncio
import logging
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import aiohttp
import speech_recognition as sr
from pydub import AudioSegment
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------------------
# Конфигурация из окружения (env-переменные задаются в панели Render)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_URL = os.getenv("AI_API_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Дефолтные шаблоны переработки
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATES = {
    "1": {
        "name": "\U0001F4DD Краткое саммари (простые буллеты)",
        "prompt": (
            "Сделай краткую выжимку (summary) следующего текста в виде "
            "маркированного списка ключевых мыслей и выводов. Пиши на русском языке."
        ),
    },
    "2": {
        "name": "\U0001F4CA Пирамида Минто (Суть -> Аргументы)",
        "prompt": (
            "Переработай текст по принципу Пирамиды Минто: сначала укажи главное "
            "утверждение (основную идею), затем приведи ключевые аргументы/подпункты, "
            "подтверждающие её. Пиши на русском."
        ),
    },
    "3": {
        "name": "\u2705 Экшен-план (Action Items)",
        "prompt": (
            "Выдели из этого текста только конкретные задачи, действия, шаги и "
            "договоренности (Action Items). Сделай это в виде чек-листа. Пиши на русском."
        ),
    },
    "4": {
        "name": "\U0001F393 Подробный конспект (Инсайт-анализ)",
        "prompt": (
            "Составь подробный учебный или аналитический конспект на основе текста. "
            "Раздели его на логические разделы с заголовками. Выдели важные термины и инсайты."
        ),
    },
}


class BotDatabase:
    """Слой работы с SQLite: шаблоны, кэш YouTube, история диалогов."""

    def __init__(self, db_path: str):
        self.db_path = db_path
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
                "INSERT OR REPLACE INTO youtube_cache (video_id, transcript) VALUES (?, ?)",
                (video_id, transcript),
            )
            conn.commit()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка записи в кэш YT: %s", exc)


class LLMClient:
    """Клиент обращения к OpenAI-совместимому API (OpenRouter)."""

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
                    "Ты — полезный ИИ-ассистент. Твоя задача — обрабатывать "
                    "предоставленные тексты на русском языке."
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

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.5,
        }

        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.api_url}/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=30,
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


class AdvancedMediaYTAgentBot:
    """Основной класс бота: обработчики сообщений и бизнес-логика."""

    YOUTUBE_ID_RE = re.compile(
        r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:[^/\n\s]+/\S+/|"
        r"(?:v|e(?:mbed)?)/|shorts/|\S*?[?&]v=)|youtu\.be/)"
        r"([a-zA-Z0-9_-]{11})"
    )

    def __init__(self, db: BotDatabase, llm: LLMClient):
        self.db = db
        self.llm = llm
        self.recognizer = sr.Recognizer()
        self.user_sessions = {}

    # -- Вспомогательные --
    def extract_youtube_id(self, url: str):
        match = self.YOUTUBE_ID_RE.search(url)
        return match.group(1) if match else None

    def _set_session(self, user_id, text):
        self.user_sessions[user_id] = {"text": text, "chat_history": []}

    # -- Получение транскрипта --
    async def fetch_youtube_transcript(self, video_id: str) -> str:
        cached = self.db.get_cached_youtube(video_id)
        if cached:
            logger.info("Транскрипт для %s взят из кэша SQLite.", video_id)
            return cached
        try:
            loop = asyncio.get_running_loop()
            transcript_list = await loop.run_in_executor(
                None, YouTubeTranscriptApi.list_transcripts, video_id
            )
            try:
                transcript = transcript_list.find_transcript(["ru"])
            except Exception:  # noqa: BLE001
                try:
                    transcript = transcript_list.find_transcript(["en"])
                except Exception:  # noqa: BLE001
                    generated = transcript_list.get_generated_transcripts()
                    if generated:
                        transcript = list(generated.values())[0]
                    else:
                        raise RuntimeError("Субтитры отсутствуют.")
            data = await loop.run_in_executor(None, transcript.fetch)
            full_text = " ".join(item["text"] for item in data)
            self.db.save_youtube_cache(video_id, full_text)
            return full_text
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка парсинга YouTube: %s", exc)
            return f"Error: {exc}"

    async def transcribe_audio(self, file_bytes: bytes, fmt: str) -> str:
        try:
            loop = asyncio.get_running_loop()
            audio = await loop.run_in_executor(
                None, lambda: AudioSegment.from_file(BytesIO(file_bytes), format=fmt)
            )
            wav_io = BytesIO()
            await loop.run_in_executor(
                None, lambda: audio.export(wav_io, format="wav")
            )
            wav_io.seek(0)

            def recognize():
                with sr.AudioFile(wav_io) as src_audio:
                    data = self.recognizer.record(src_audio)
                return self.recognizer.recognize_google(
                    data, language="ru-RU"
                )

            return await loop.run_in_executor(None, recognize)
        except sr.UnknownValueError:
            return "[Не удалось распознать аудио — неразборчивый звук или тишина]"
        except sr.RequestError as exc:
            return f"[Ошибка сервиса распознавания Google: {exc}]"
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка транскрибации аудио: %s", exc)
            return f"[Ошибка обработки файла: {exc}]"

    # -- Обработчики Telegram --
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_text = (
            "\U0001F680 **v2.0.0 (Версия с Базой Данных и Управлением Шаблонами)**\n\n"
            "В новой версии добавлены:\n"
            "\U0001F5C4 **SQLite База Данных**: Сессии, кэш субтитров и шаблоны сохраняются вечно.\n"
            "\U0001F4BE **Кэширование YouTube**: Повторный запрос видео выполняется мгновенно и без лимитов.\n"
            "\U0001F6E0 **Кастомные шаблоны**: Вы можете создавать и изменять свои шаблоны переработки!\n\n"
            "**Команды для управления шаблонами:**\n"
            "\U0001F4DD `/add_template ID | Название | Промпт` — Добавить или обновить ваш шаблон.\n"
            "Например:\n"
            "`/add_template 5 | Мой переводчик | Переведи текст на английский язык.`"
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    async def add_template_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = update.effective_user.id
        text = " ".join(context.args)
        if not text or "|" not in text:
            await update.message.reply_text(
                "\u26a0\ufe0f Неверный формат! Используйте:\n"
                "`/add_template ID | Название | Промпт`"
            )
            return
        try:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 3:
                raise ValueError("Не все поля заполнены.")
            template_id, name, prompt = parts[0], parts[1], parts[2]
            self.db.save_template(user_id, template_id, name, prompt)
            await update.message.reply_text(
                f"\u2705 Шаблон *'{name}'* (ID: {template_id}) успешно сохранен "
                "и доступен в меню!",
                parse_mode="Markdown",
            )
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(
                f"\u274c Ошибка добавления шаблона: {exc}"
            )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip() if update.message.text else ""
        video_id = self.extract_youtube_id(text)
        if video_id:
            status = await update.message.reply_text(
                "\U0001F4E5 Получаю транскрипт видео (проверяю кэш)..."
            )
            transcript = await self.fetch_youtube_transcript(video_id)
            if transcript.startswith("Error"):
                await status.edit_text(
                    f"\u274c Не удалось получить субтитры: {transcript}"
                )
            else:
                user_id = update.effective_user.id
                self._set_session(user_id, transcript)
                await status.edit_text(
                    "\u2705 Транскрипт успешно получен!"
                )
                await self.show_template_keyboard(update, context)
            return

        user_id = update.effective_user.id
        session = self.user_sessions.get(user_id)
        if session and session["text"]:
            status = await update.message.reply_text(
                "\U0001F916 Думаю над ответом..."
            )
            session["chat_history"].append({"role": "user", "content": text})
            reply = await self.llm.complete(
                "", session["text"], session["chat_history"]
            )
            session["chat_history"].append({"role": "assistant", "content": reply})
            await status.edit_text(reply)
        else:
            await update.message.reply_text(
                "Отправьте аудиофайл, голосовое или ссылку на YouTube/Shorts."
            )

    async def handle_audio_or_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        status = await update.message.reply_text(
            "\U0001F4E5 Обрабатываю аудиопоток..."
        )
        is_voice = update.message.voice is not None
        file_obj = update.message.voice if is_voice else update.message.audio
        if is_voice:
            fmt = "ogg"
        else:
            fname = (file_obj.file_name or "").lower() if file_obj.file_name else ""
            if fname.endswith(".mp3"):
                fmt = "mp3"
            elif fname.endswith(".wav"):
                fmt = "wav"
            elif fname.endswith(".m4a"):
                fmt = "m4a"
            else:
                fmt = "ogg"
        tg_file = await context.bot.get_file(file_obj.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        await status.edit_text("\U0001F399 Распознаю речь...")
        text = await self.transcribe_audio(bytes(file_bytes), fmt)
        if text.startswith("["):
            await status.edit_text(f"\u274c Ошибка распознавания: {text}")
        else:
            user_id = update.effective_user.id
            self._set_session(user_id, text)
            await status.edit_text(
                f"\U0001F5E3 **Распознанный текст:**\n{text[:200]}..."
            )
            await self.show_template_keyboard(update, context)

    async def show_template_keyboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = update.effective_user.id
        templates = self.db.get_templates(user_id)
        keyboard = [
            [
                InlineKeyboardButton(
                    temp["name"], callback_data=f"template_{tid}"
                )
            ]
            for tid, temp in templates.items()
        ]
        await update.message.reply_text(
            "\u2699\ufe0f **Выберите шаблон переработки информации:**",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        if user_id not in self.user_sessions:
            await query.edit_message_text(
                "\u26a0\ufe0f Ошибка: Сессия не найдена."
            )
            return
        data = query.data or ""
        if data.startswith("template_"):
            template_id = data.split("_")[1]
            templates = self.db.get_templates(user_id)
            template = templates.get(template_id)
            if not template:
                return
            await query.edit_message_text(
                f"\U0001F916 Применяю шаблон: *{template['name']}*..."
            )
            context_text = self.user_sessions[user_id]["text"]
            result = await self.llm.complete(template["prompt"], context_text)
            result_text = (
                f"\u2728 **Результат по шаблону '{template['name']}':**\n\n"
                f"{result}\n\n"
                "\U0001F4AC *Вы можете продолжить общение с ИИ. "
                "Просто пишите вопросы текстом в чат!*"
            )
            self.user_sessions[user_id]["chat_history"] = [
                {"role": "assistant", "content": result}
            ]
            if len(result_text) > 4000:
                for chunk in range(0, len(result_text), 4000):
                    await query.message.reply_text(result_text[chunk : chunk + 4000])
            else:
                await query.message.reply_text(result_text)


def run_health_server(port: int):
    """Фиктивный HTTP-сервер для Render: отвечает 200 на /health и любой GET."""

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info("Health-check сервер слушает порт %s", port)
        server.serve_forever()
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка запуска веб-сервера: %s", exc)


def main() -> None:
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[!] Укажите реальный TELEGRAM_BOT_TOKEN!")
        return

    threading.Thread(target=run_health_server, args=(PORT,), daemon=True).start()

    db = BotDatabase(DB_PATH)
    llm = LLMClient(AI_API_KEY, AI_API_URL, AI_MODEL)
    bot = AdvancedMediaYTAgentBot(db, llm)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", bot.start_command))
    app.add_handler(CommandHandler("add_template", bot.add_template_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text)
    )
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, bot.handle_audio_or_voice)
    )
    app.add_handler(CallbackQueryHandler(bot.handle_callback_query))

    print("Бот запущен. Режим: polling + health-check сервер.")
    app.run_polling()


if __name__ == "__main__":
    main()
