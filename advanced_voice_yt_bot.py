import os
import re
import logging
import json
from io import BytesIO
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

# Токены и API ключи (задаются через переменные окружения)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")  # "openai" или "ollama" или "custom"
AI_API_KEY = os.getenv("AI_API_KEY", "")          # Токен API (например, OpenAI)
AI_API_URL = os.getenv("AI_API_URL", "https://api.openai.com/v1")  # Базовый URL для OpenAI или вашего провайдера
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")   # Используемая модель

# Базовые шаблоны переработки информации (форматирования)
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

class AdvancedMediaYTAgentBot:
    def __init__(self):
        self.recognizer = sr.Recognizer()
        # Хранилище сессий пользователей (сохраненный текст для переработки и истории диалога)
        self.user_sessions = {}
        # Загружаем шаблоны
        self.templates = DEFAULT_TEMPLATES.copy()

    def extract_youtube_id(self, url: str) -> str:
        """Извлекает ID видео или Shorts из ссылки на YouTube разных форматов."""
        pattern = r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:[^/
\s]+/\S+/|(?:v|e(?:mbed)?)/|shorts/|\S*?[?&]v=)|youtu\.be/)([a-zA-Z0-9_-]{11})'
        match = re.search(pattern, url)
        return match.group(1) if match else None

    def fetch_youtube_transcript(self, video_id: str) -> str:
        """Парсит транскрипт видео или Shorts."""
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
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

            data = transcript.fetch()
            # Собираем чистый текст без таймкодов для более удобной обработки нейросетью
            full_text = " ".join([entry['text'] for entry in data])
            return full_text
        except Exception as e:
            logger.error(f"Ошибка парсинга YouTube транскрипта: {e}")
            return f"Error: {str(e)}"

    def transcribe_audio_bytes(self, file_bytes: bytes, original_format: str) -> str:
        """Универсальный транскрибатор: конвертирует MP3, WAV, OGG в нужный формат и распознает."""
        try:
            # Читаем аудиофайл любого формата через pydub
            audio = AudioSegment.from_file(BytesIO(file_bytes), format=original_format)
            
            # Конвертируем в WAV
            wav_io = BytesIO()
            audio.export(wav_io, format="wav")
            wav_io.seek(0)
            
            with sr.AudioFile(wav_io) as source:
                audio_data = self.recognizer.record(source)
                # Используем Google Speech API для распознавания русского языка
                text = self.recognizer.recognize_google(audio_data, language="ru-RU")
                return text
        except sr.UnknownValueError:
            return "[Не удалось распознать аудио — неразборчивый звук или тишина]"
        except sr.RequestError as e:
            return f"[Ошибка сервиса распознавания Google: {e}]"
        except Exception as e:
            logger.error(f"Ошибка транскрибации аудио: {e}")
            return f"[Ошибка обработки файла: {str(e)}]"

    def call_llm_api(self, prompt: str, context_text: str, chat_history: list = None) -> str:
        """Делает запрос к LLM (например, OpenAI-совместимый эндпоинт) для обработки текста или чата."""
        if not AI_API_KEY:
            return "⚠️ Ошибка: API ключ для нейросети не настроен. Обработка ИИ недоступна."

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_API_KEY}"
        }

        # Формируем сообщения для модели
        messages = [
            {"role": "system", "content": "Ты — полезный ИИ-ассистент. Твоя задача — обрабатывать полученные тексты, делать выжимки или вести диалог по ним на русском языке."}
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

        try:
            response = requests.post(f"{AI_API_URL}/chat/completions", headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                result_json = response.json()
                return result_json['choices'][0]['message']['content'].strip()
            else:
                return f"⚠️ Ошибка API ({response.status_code}): {response.text}"
        except Exception as e:
            return f"⚠️ Ошибка при подключении к ИИ: {str(e)}"

    # --- Обработчики Telegram-команд ---
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_text = (
            "🚀 **Привет! Я твой продвинутый ИИ-ассистент обработки информации.**

"
            "📥 **Принимаю любые входящие форматы:**
"
            "• **Голосовые сообщения** с микрофона.
"
            "• **Аудиофайлы** в форматах `mp3`, `wav`, `ogg`, `m4a`.
"
            "• **Ссылки на YouTube** (включая обычные видео и **Shorts**).

"
            "⚙️ **Как это работает:**
"
            "1. Пришли мне аудио или ссылку.
"
            "2. Я извлеку текст и предложу **выбрать шаблон** для его мгновенной переработки.
"
            "3. Затем ты сможешь **общаться с ИИ в режиме диалога**, задавая вопросы по содержанию этого материала!"
        )
        await update.message.reply_text(welcome_text)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        video_id = self.extract_youtube_id(text)

        if video_id:
            status = await update.message.reply_text("📥 Обнаружена ссылка на YouTube. Получаю транскрипт видео...")
            transcript = self.fetch_youtube_transcript(video_id)
            
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
            # Если это обычный текст и у пользователя активна сессия общения с ИИ
            user_id = update.effective_user.id
            if user_id in self.user_sessions and self.user_sessions[user_id]["text"]:
                status = await update.message.reply_text("🤖 Думаю над ответом...")
                session = self.user_sessions[user_id]
                session["chat_history"].append({"role": "user", "content": text})
                
                ai_response = self.call_llm_api("", session["text"], session["chat_history"])
                session["chat_history"].append({"role": "assistant", "content": ai_response})
                
                await status.edit_text(ai_response)
            else:
                await update.message.reply_text(
                    "Отправь мне аудиофайл, голосовое сообщение или ссылку на YouTube/Shorts, чтобы начать работу."
                )

    async def handle_audio_or_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = await update.message.reply_text("📥 Обрабатываю входящий аудиопоток...")
        
        is_voice = update.message.voice is not None
        file_obj = update.message.voice if is_voice else update.message.audio
        
        # Определение формата файла
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
                original_format = "ogg"  # fallback

        # Скачивание файла
        tg_file = await context.bot.get_file(file_obj.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        
        await status.edit_text("🎙 Распознаю речь (транскрибирую)...")
        transcribed_text = self.transcribe_audio_bytes(bytes(file_bytes), original_format)
        
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
        """Выводит клавиатуру для выбора шаблона обработки информации."""
        keyboard = []
        for tid, temp in self.templates.items():
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
            await query.edit_message_text("⚠️ Ошибка: Сессия не найдена. Пожалуйста, отправьте аудио или ссылку заново.")
            return

        data = query.data
        if data.startswith("template_"):
            template_id = data.split("_")[1]
            template = self.templates.get(template_id)
            
            if template:
                await query.edit_message_text(f"🤖 Применяю шаблон: *{template['name']}*... Обрабатываю в ИИ...")
                
                context_text = self.user_sessions[user_id]["text"]
                processed_result = self.call_llm_api(template["prompt"], context_text)
                
                result_text = (
                    f"✨ **Результат обработки по шаблону '{template['name']}':**

"
                    f"{processed_result}

"
                    f"💬 *Вы можете продолжить общение с ИИ. Просто пишите вопросы текстом в чат, и я отвечу по контексту этого материала!*"
                )
                
                # Обновляем историю диалога ответом ИИ
                self.user_sessions[user_id]["chat_history"] = [
                    {"role": "assistant", "content": processed_result}
                ]
                
                # Если текст слишком длинный, разбиваем
                if len(result_text) > 4000:
                    await query.message.reply_text("Текст очень длинный, отправляю по частям:")
                    for chunk in range(0, len(result_text), 4000):
                        await query.message.reply_text(result_text[chunk:chunk+4000])
                else:
                    await query.message.reply_text(result_text)

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("[!] Внимание: Укажите реальный TELEGRAM_BOT_TOKEN!")
        return

    bot_agent = AdvancedMediaYTAgentBot()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", bot_agent.start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot_agent.handle_text))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot_agent.handle_audio_or_voice))
    application.add_handler(CallbackQueryHandler(bot_agent.handle_callback_query))

    print("🚀 Умный бот-транскрибатор с ИИ запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
