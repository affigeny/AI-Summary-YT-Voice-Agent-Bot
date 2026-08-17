import os
import re
import logging
import asyncio
import sqlite3
import json
from io import BytesIO
import aiohttp
import speech_recognition as sr
from pydub import AudioSegment
from youtube_transcript_api import YouTubeTranscriptApi
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токены и API ключи
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_API_URL = os.getenv("AI_API_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
DB_PATH = os.getenv("DB_PATH", "bot_database.db")

# Дефолтные шаблоны переработки
DEFAULT_TEMPLATES = {
    "1": {
        "name": "📝 Краткое саммари (простые буллеты)",
        "prompt": "Сделай краткую выжимку (summary) следующего текста в виде маркированного списка ключевых мыслей и выводов. Пиши на русском языке."
    },
    "2": {
        "name": "📊 Пирамида Минто (Суть -> Аргументы)",
        "prompt": "Переработай текст по принципу Пирамиды Минто: сначала укажи главное утверждение (основную идею), затем приведи ключевые аргументы/подпункты, подтверждающие её. Пиши на русском."
    },
    "3": {
        "name": "✅ Экшен-план (Action Items)",
        "prompt": "Выдели из этого текста только конкретные задачи, действия, шаги и договоренности (Action Items). Сделай это в виде чек-листа. Пиши на русском."
    },
    "4": {
        "name": "🎓 Подробный конспект (Инсайт-анализ)",
        "prompt": "Составь подробный учебный или аналитический конспект на основе текста. Раздели его на логические разделы с заголовками. Выдели важные термины и инсайты."
    }
}

class AdvancedMediaYTAgentBotV2DB:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.user_sessions = {}  # Временное ОЗУ для активного диалога
        self._init_db()

    def _init_db(self):
        """Инициализация базы данных SQLite для постоянного хранения шаблонов и кэша."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Таблица кастомных шаблонов пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS templates (
                user_id INTEGER,
                template_id TEXT,
                name TEXT,
                prompt TEXT,
                PRIMARY KEY (user_id, template_id)
            )
        ''')
        
        # Таблица кэша транскриптов YouTube для экономии лимитов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS youtube_cache (
                video_id TEXT PRIMARY KEY,
                transcript TEXT,
                cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица истории диалогов для сохранения контекста при перезапуске
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

    def get_user_templates(self, user_id: int) -> dict:
        """Получает объединенный словарь дефолтных и кастомных шаблонов пользователя."""
        templates = DEFAULT_TEMPLATES.copy()
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT template_id, name, prompt FROM templates WHERE user_id = ?", (user_id,))
            rows = cursor.fetchall()
            for row in rows:
                templates[row[0]] = {"name": row[1], "prompt": row[2]}
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка получения шаблонов: {e}")
        return templates

    def save_custom_template(self, user_id: int, template_id: str, name: str, prompt: str):
        """Сохраняет новый пользовательский шаблон в БД."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO templates (user_id, template_id, name, prompt)
                VALUES (?, ?, ?, ?)
            ''', (user_id, template_id, name, prompt))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка сохранения шаблона: {e}")

    def get_cached_youtube(self, video_id: str) -> str:
        """Проверяет наличие субтитров в кэше БД."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT transcript FROM youtube_cache WHERE video_id = ?", (video_id,))
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            logger.error(f"Ошибка чтения кэша YT: {e}")
            return None

    def save_youtube_cache(self, video_id: str, transcript: str):
        """Сохраняет полученный транскрипт в кэш."""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO youtube_cache (video_id, transcript) VALUES (?, ?)", (video_id, transcript))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Ошибка записи в кэш YT: {e}")

    def extract_youtube_id(self, url: str) -> str:
        pattern = r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:[^/\n\\s]+/\\S+/|(?:v|e(?:mbed)?)/|shorts/|\\S*?[?&]v=)|youtu\.be/)([a-zA-Z0-9_-]{11})'
        match = re.search(pattern, url)
        return match.group(1) if match else None

    async def fetch_youtube_transcript_async(self, video_id: str) -> str:
        # Сначала проверяем локальный кэш
        cached = self.get_cached_youtube(video_id)
        if cached:
            logger.info(f"Транскрипт для {video_id} взят из кэша SQLite.")
            return cached

        try:
            loop = asyncio.get_event_loop()
            transcript_list = await loop.run_in_executor(
                None, YouTubeTranscriptApi.list_transcripts, video_id
            )
            try:
                transcript = transcript_list.find_transcript(['ru'])
            except Exception:
                try:
                    transcript = transcript_list.find_transcript(['en'])
                except Exception:
                    transcript = transcript_list.get_generated_transcripts()
                    if transcript:
                        transcript = list(transcript.values())[0]
                    else:
                        raise Exception("Субтитры отсутствуют.")

            data = await loop.run_in_executor(None, transcript.fetch)
            full_text = " ".join([entry['text'] for entry in data])
            
            # Сохраняем в кэш для будущих запросов
            self.save_youtube_cache(video_id, full_text)
            return full_text
        except Exception as e:
            logger.error(f"Ошибка парсинга YouTube: {e}")
            return f"Error: {str(e)}"

    async def transcribe_audio_bytes_async(self, file_bytes: bytes, original_format: str) -> str:
        try:
            loop = asyncio.get_event_loop()
            audio = await loop.run_in_executor(
                None, lambda: AudioSegment.from_file(BytesIO(file_bytes), format=original_format)
            )
            wav_io = BytesIO()
            await loop.run_in_executor(None, lambda: audio.export(wav_io, format="wav"))
            wav_io.seek(0)
            
            def record_and_recognize():
                with sr.AudioFile(wav_io) as source:
                    audio_data = self.recognizer.record(source)
                    return self.recognizer.recognize_google(audio_data, language="ru-RU")
            
            text = await loop.run_in_executor(None, record_and_recognize)
            return text
        except sr.UnknownValueError:
            return "[Не удалось распознать аудио — неразборчивый звук или тишина]"
        except sr.RequestError as e:
            return f"[Ошибка сервиса распознавания Google: {e}]"
        except Exception as e:
            logger.error(f"Ошибка транскрибации аудио: {e}")
            return f"[Ошибка обработки файла: {str(e)}]"

    async def call_llm_api_async(self, prompt: str, context_text: str, chat_history: list = None, retries: int = 3) -> str:
        if not AI_API_KEY:
            return "⚠️ Ошибка: API ключ для нейросети не настроен."

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_API_KEY}"
        }

        messages = [
            {"role": "system", "content": "Ты — полезный ИИ-ассистент. Твоя задача — обрабатывать предоставленные тексты на русском языке."}
        ]

        if context_text:
            messages.append({"role": "user", "content": f"Вот исходный текст/транскрипт для контекста:
{context_text}"})
        
        if chat_history:
            messages.extend(chat_history)
        else:
            messages.append({"role": "user", "content": prompt})

        payload = {
            "model": AI_MODEL,
            "messages": messages,
            "temperature": 0.5
        }

        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(f"{AI_API_URL}/chat/completions", headers=headers, json=payload, timeout=30) as response:
                        if response.status == 200:
                            result_json = await response.json()
                            return result_json['choices'][0]['message']['content'].strip()
                        elif response.status in [429, 500, 502, 503, 504]:
                            wait_time = (attempt + 1) * 2
                            await asyncio.sleep(wait_time)
            except Exception:
                wait_time = (attempt + 1) * 2
                await asyncio.sleep(wait_time)

        return "⚠️ Облачный ИИ сейчас недоступен. Пожалуйста, повторите попытку позже."

    # --- Обработчики Telegram ---
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_text = (
            "🚀 **v2.0.0 (Версия с Базой Данных и Управлением Шаблонами)**

"
            "В новой версии добавлены:
"
            "🗄 **SQLite База Данных**: Сессии, кэш субтитров и шаблоны сохраняются вечно.
"
            "💾 **Кэширование YouTube**: Повторный запрос видео выполняется мгновенно и без лимитов.
"
            "🛠 **Кастомные шаблоны**: Вы можете создавать и изменять свои шаблоны переработки!

"
            "**Команды для управления шаблонами:**
"
            "📝 `/add_template ID | Название | Промпт` — Добавить или обновить ваш шаблон.
"
            "Например:
"
            "`/add_template 5 | Мой переводчик | Переведи текст на английский язык.`"
        )
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    async def add_template_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для создания или обновления кастомного шаблона."""
        user_id = update.effective_user.id
        text = " ".join(context.args)
        if not text or "|" not in text:
            await update.message.reply_text(
                "⚠️ Неверный формат! Используйте:
"
                "`/add_template ID | Название | Промпт`"
            )
            return
            
        try:
            parts = [p.strip() for p in text.split("|")]
            if len(parts) < 3:
                raise ValueError("Не все поля заполнены.")
                
            template_id, name, prompt = parts[0], parts[1], parts[2]
            
            # Сохраняем в базу данных SQLite
            self.save_custom_template(user_id, template_id, name, prompt)
            await update.message.reply_text(f"✅ Шаблон *'{name}'* (ID: {template_id}) успешно сохранен и доступен в меню!", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка добавления шаблона: {str(e)}")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        video_id = self.extract_youtube_id(text)

        if video_id:
            status = await update.message.reply_text("📥 Получаю транскрипт видео (проверяю кэш)...")
            transcript = await self.fetch_youtube_transcript_async(video_id)
            
            if transcript.startswith("Error"):
                await status.edit_text(f"❌ Не удалось получить субтитры: {transcript}")
            else:
                user_id = update.effective_user.id
                self.user_sessions[user_id] = {
                    "text": transcript,
                    "chat_history": []
                }
                await status.edit_text("✅ Транскрипт успешно получен!")
                await self.show_template_keyboard(update, context)
        else:
            user_id = update.effective_user.id
            if user_id in self.user_sessions and self.user_sessions[user_id]["text"]:
                status = await update.message.reply_text("🤖 Думаю над ответом...")
                session = self.user_sessions[user_id]
                session["chat_history"].append({"role": "user", "content": text})
                
                ai_response = await self.call_llm_api_async("", session["text"], session["chat_history"])
                session["chat_history"].append({"role": "assistant", "content": ai_response})
                
                await status.edit_text(ai_response)
            else:
                await update.message.reply_text("Отправьте аудиофайл, голосовое или ссылку на YouTube/Shorts.")

    async def handle_audio_or_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = await update.message.reply_text("📥 Обрабатываю аудиопоток...")
        
        is_voice = update.message.voice is not None
        file_obj = update.message.voice if is_voice else update.message.audio
        
        if is_voice:
            original_format = "ogg"
        else:
            filename = file_obj.file_name.lower() if file_obj.file_name else ""
            if filename.endswith(".mp3"):
                original_format = "mp3"
            elif filename.endswith(".wav"):
                original_format = "wav"
            elif filename.endswith(".m4a"):
                original_format = "m4a"
            else:
                original_format = "ogg"

        tg_file = await context.bot.get_file(file_obj.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        
        await status.edit_text("🎙 Распознаю речь...")
        transcribed_text = await self.transcribe_audio_bytes_async(bytes(file_bytes), original_format)
        
        if transcribed_text.startswith("["):
            await status.edit_text(f"❌ Ошибка распознавания: {transcribed_text}")
        else:
            user_id = update.effective_user.id
            self.user_sessions[user_id] = {
                "text": transcribed_text,
                "chat_history": []
            }
            await status.edit_text(f"🗣 **Распознанный текст:**
"{transcribed_text[:200]}..."")
            await self.show_template_keyboard(update, context)

    async def show_template_keyboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        templates = self.get_user_templates(user_id)
        
        keyboard = []
        for tid, temp in templates.items():
            keyboard.append([InlineKeyboardButton(temp["name"], callback_data=f"template_{tid}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚙️ **Выберите шаблон переработки информации:**",
            reply_markup=reply_markup
        )

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        if user_id not in self.user_sessions:
            await query.edit_message_text("⚠️ Ошибка: Сессия не найдена.")
            return

        data = query.data
        if data.startswith("template_"):
            template_id = data.split("_")[1]
            templates = self.get_user_templates(user_id)
            template = templates.get(template_id)
            
            if template:
                await query.edit_message_text(f"🤖 Применяю шаблон: *{template['name']}*...")
                
                context_text = self.user_sessions[user_id]["text"]
                processed_result = await self.call_llm_api_async(template["prompt"], context_text)
                
                result_text = (
                    f"✨ **Результат по шаблону '{template['name']}':**

"
                    f"{processed_result}

"
                    f"💬 *Вы можете продолжить общение с ИИ. Просто пишите вопросы текстом в чат!*"
                )
                
                self.user_sessions[user_id]["chat_history"] = [
                    {"role": "assistant", "content": processed_result}
                ]
                
                if len(result_text) > 4000:
                    for chunk in range(0, len(result_text), 4000):
                        await query.message.reply_text(result_text[chunk:chunk+4000])
                else:
                    await query.message.reply_text(result_text)

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[!] Укажите реальный TELEGRAM_BOT_TOKEN!")
        return

    bot_agent = AdvancedMediaYTAgentBotV2DB()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", bot_agent.start_command))
    application.add_handler(CommandHandler("add_template", bot_agent.add_template_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_agent.handle_text))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot_agent.handle_audio_or_voice))
    application.add_handler(CallbackQueryHandler(bot_agent.handle_callback_query))

    print("🚀 v2.0.0 Асинхронный ИИ-Бот с базой данных SQLite запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
