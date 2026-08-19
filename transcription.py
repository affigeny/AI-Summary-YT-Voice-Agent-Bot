"""
Transcription module — расширение для YT_Bot_Sum
Добавляет:
- Отдельную команду /transcribe для транскрибации аудио/видео
- Rate limiting (1 запрос/10 сек на пользователя)
- Free tier (3 транскрибации/месяц)
- Progress streaming
- Экспорт с таймкодами (как в оригинале Буквица)
"""

import os
import sqlite3
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


# ==================== CONFIG ====================
FREE_TRANSCRIPTIONS_PER_MONTH = 3
RATE_LIMIT_SECONDS = 10
MAX_TRANSCRIPTION_DURATION = 1800  # 30 минут


# ==================== DATABASE ====================
class TranscriptionDB:
    """База данных для учёта транскрибаций"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Инициализация таблиц"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transcription_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                transcriptions_count INTEGER DEFAULT 0,
                last_reset_date DATE,
                is_premium INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица транскрипций
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transcription_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_name TEXT,
                duration_seconds REAL,
                word_count INTEGER,
                result_path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
        logger.info("TranscriptionDB initialized")
    
    def get_or_create_user(self, user_id: int, username: str = None) -> Dict[str, Any]:
        """Получить или создать пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Проверяем существование
        cursor.execute("SELECT * FROM transcription_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if not row:
            # Создаём нового пользователя
            cursor.execute(
                "INSERT INTO transcription_users (user_id, username, last_reset_date) VALUES (?, ?, ?)",
                (user_id, username, datetime.now().date())
            )
            conn.commit()
            logger.info(f"User {user_id} created")
        
        # Получаем актуальные данные
        cursor.execute("SELECT * FROM transcription_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        conn.close()
        
        # Сброс счётчика если новый месяц
        last_reset = datetime.strptime(row[4], "%Y-%m-%d").date() if row[4] else None
        if last_reset and (datetime.now().date() - last_reset).days >= 30:
            self._reset_monthly_count(user_id)
            return self.get_user_stats(user_id)
        
        return {
            "count": row[2],
            "limit": FREE_TRANSCRIPTIONS_PER_MONTH,
            "is_premium": bool(row[5]),
            "remaining": FREE_TRANSCRIPTIONS_PER_MONTH - row[2] if not row[5] else float('inf')
        }
    
    def _reset_monthly_count(self, user_id: int):
        """Сбросить счётчик на новый месяц"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE transcription_users SET transcriptions_count = 0, last_reset_date = ? WHERE user_id = ?",
            (datetime.now().date(), user_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"Monthly count reset for user {user_id}")
    
    def add_transcription(self, user_id: int, file_name: str, duration: float, word_count: int, result_path: str):
        """Добавить запись о транскрибации"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Запись в историю
        cursor.execute(
            "INSERT INTO transcription_history (user_id, file_name, duration_seconds, word_count, result_path) VALUES (?, ?, ?, ?, ?)",
            (user_id, file_name, duration, word_count, result_path)
        )
        
        # Увеличение счётчика
        cursor.execute(
            "UPDATE transcription_users SET transcriptions_count = transcriptions_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        
        conn.commit()
        conn.close()
        logger.info(f"Transcription added for user {user_id}: {file_name}")
    
    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Получить статистику пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM transcription_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if not row:
            return {"count": 0, "limit": FREE_TRANSCRIPTIONS_PER_MONTH, "is_premium": False, "remaining": FREE_TRANSCRIPTIONS_PER_MONTH}
        
        conn.close()
        
        return {
            "count": row[2],
            "limit": FREE_TRANSCRIPTIONS_PER_MONTH,
            "is_premium": bool(row[5]),
            "remaining": FREE_TRANSCRIPTIONS_PER_MONTH - row[2] if not row[5] else float('inf')
        }
    
    def get_recent_transcriptions(self, user_id: int, limit: int = 5) -> list:
        """Получить последние транскрибации пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            """SELECT file_name, duration_seconds, word_count, created_at 
               FROM transcription_history 
               WHERE user_id = ? 
               ORDER BY created_at DESC 
               LIMIT ?""",
            (user_id, limit)
        )
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "file": r[0],
                "duration": r[1],
                "words": r[2],
                "date": r[3]
            }
            for r in rows
        ]


# ==================== RATE LIMITER ====================
class RateLimiter:
    """Ограничитель частоты запросов"""
    
    def __init__(self, limit_seconds: int = RATE_LIMIT_SECONDS):
        self.limit_seconds = limit_seconds
        self.last_request: Dict[int, float] = {}
    
    def is_allowed(self, user_id: int) -> bool:
        """Проверить, можно ли сделать запрос"""
        now = datetime.now().timestamp()
        last = self.last_request.get(user_id, 0)
        
        if now - last < self.limit_seconds:
            return False
        
        self.last_request[user_id] = now
        return True
    
    def get_wait_time(self, user_id: int) -> float:
        """Получить время ожидания в секундах"""
        now = datetime.now().timestamp()
        last = self.last_request.get(user_id, 0)
        wait = self.limit_seconds - (now - last)
        return max(0, wait)


# ==================== WHISPER SERVICE ====================
class TranscriptionService:
    """Сервис транскрибации на базе faster-whisper"""
    
    def __init__(self, model_size: str = "small"):
        self.model_size = model_size
        self.model = None
    
    def load_model(self):
        """Lazy load модели"""
        if self.model is None:
            from faster_whisper import WhisperModel
            logger.info(f"Loading Whisper model: {self.model_size}")
            self.model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            logger.info("Whisper model loaded")
    
    async def transcribe(
        self,
        audio_path: str,
        language: str = "ru",
        progress_callback=None
    ) -> Dict[str, Any]:
        """
        Транскрибация аудиофайла
        
        Returns:
            {
                "text": str,
                "text_with_timestamps": str,
                "segments": list,
                "duration": float,
                "word_count": int
            }
        """
        if self.model is None:
            self.load_model()
        
        # Transcribe
        segments, info = self.model.transcribe(
            audio_path,
            language=language,
            beam_size=5,
            vad_filter=True
        )
        
        # Format result
        text = ""
        segments_list = []
        
        for segment in segments:
            text += segment.text
            segments_list.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip()
            })
        
        # Format with timestamps
        text_with_timestamps = "\n".join([
            f"[{s['start']:.2f} -> {s['end']:.2f}] {s['text']}"
            for s in segments_list
        ])
        
        return {
            "text": text.strip(),
            "text_with_timestamps": text_with_timestamps,
            "segments": segments_list,
            "duration": info.duration,
            "word_count": len(text.split())
        }


# ==================== INTEGRATION ====================
def register_transcription_handlers(bot, dp):
    """
    Регистрация handlers для транскрибации
    Вызывать после создания приложения бота
    """
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import ContextTypes
    
    # Инициализация
    db_path = os.getenv("DB_PATH", "bot_database.db")
    trans_db = TranscriptionDB(db_path)
    rate_limiter = RateLimiter()
    trans_service = TranscriptionService(model_size=os.getenv("WHISPER_MODEL", "small"))
    
    # Command /transcribe
    async def cmd_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        stats = trans_db.get_or_create_user(user_id, update.effective_user.username)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Баланс", callback_data="transcribe_balance")],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="transcribe_help")]
        ])
        
        await update.message.reply_text(
            f"🎙️ *Транскрибация* — аудио/видео в текст\n\n"
            f"✅ Осталось: **{int(stats['remaining'])}** из {stats['limit']}\n"
            "📁 Отправьте аудио, видео или ссылку\n\n"
            "Команды:\n"
            "/transcribe — начать транскрибацию\n"
            "/stats — статистика",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    # Command /stats
    async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        stats = trans_db.get_user_stats(user_id)
        recent = trans_db.get_recent_transcriptions(user_id, limit=5)
        
        text = f"📊 *Статистика*\n\n"
        text += f"Использовано: {stats['count']}/{stats['limit']}\n"
        text += f"Осталось: {int(stats['remaining'])}\n\n"
        
        if recent:
            text += "📝 Последние транскрибации:\n"
            for r in recent:
                text += f"• {r['file']} — {r['duration']:.0f}сек, {r['words']} слов\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
    
    # Callback handlers
    async def cb_transcribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        stats = trans_db.get_user_stats(user_id)
        
        if query.data == "transcribe_balance":
            await query.edit_message_text(
                f"📊 Баланс: {int(stats['remaining'])}/{stats['limit']} транскрибаций"
            )
        elif query.data == "transcribe_help":
            await query.edit_message_text(
                "📖 *Помощь:*\n\n"
                "1️⃣ Отправьте голосовое сообщение\n"
                "2️⃣ Или аудио/видео файл\n"
                "3️⃣ Или ссылку на YouTube\n\n"
                "Результат: текст с таймкодами"
            )
    
    # Message handlers
    async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _process_audio(update, context, trans_db, rate_limiter, trans_service, "voice")
    
    async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _process_audio(update, context, trans_db, rate_limiter, trans_service, "audio")
    
    async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await _process_audio(update, context, trans_db, rate_limiter, trans_service, "video")
    
    async def _process_audio(update: Update, context, trans_db, rate_limiter, trans_service, file_type: str):
        """Обработка аудио/видео"""
        user_id = update.effective_user.id
        
        # Rate limit check
        if not rate_limiter.is_allowed(user_id):
            wait = rate_limiter.get_wait_time(user_id)
            await update.message.reply_text(f"⏳ Подождите {wait:.0f} сек...")
            return
        
        # Check limits
        stats = trans_db.get_user_stats(user_id)
        if not stats["is_premium"] and stats["count"] >= stats["limit"]:
            await update.message.reply_text(
                "⚠️ *Лимит исчерпан!*\n\n"
                f"Использовано {stats['count']}/{stats['limit']} транскрибаций.\n"
                "Напишите /transcribe для подробностей."
            )
            return
        
        # Show progress
        progress_msg = await update.message.reply_text("⏳ Загружаю файл...")
        
        try:
            # Download file
            if file_type == "voice":
                file = await context.bot.get_file(update.message.voice.file_id)
            elif file_type == "audio":
                file = await context.bot.get_file(update.message.audio.file_id)
            else:
                file = await context.bot.get_file(update.message.video.audio.file_id)
            
            # Save to temp
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp:
                await file.download_to_memory(out=tmp)
                audio_path = tmp.name
            
            # Convert to WAV
            from pydub import AudioSegment
            audio = AudioSegment.from_file(audio_path)
            wav_path = audio_path.replace(".ogg", ".wav")
            audio.export(wav_path, format="wav")
            
            # Transcribe
            await progress_msg.edit_text("🎙️ Транскрибирую...")
            result = await trans_service.transcribe(wav_path)
            
            # Save result
            result_path = f"/tmp/transcription_{user_id}_{int(datetime.now().timestamp())}.txt"
            with open(result_path, 'w', encoding='utf-8') as f:
                f.write(result["text_with_timestamps"])
            
            # Send result
            await progress_msg.edit_text("✅ Готово!")
            
            if len(result["text_with_timestamps"]) > 4000:
                with open(result_path, 'rb') as f:
                    await update.message.reply_document(
                        f,
                        caption=f"📝 Транскрибация ({result['word_count']} слов, {result['duration']:.1f} сек)"
                    )
            else:
                await update.message.reply_text(
                    f"📝 *Транскрибация*\n\n"
                    f"⏱ {result['duration']:.1f} сек | 📊 {result['word_count']} слов\n\n"
                    f"```\n{result['text_with_timestamps'][:3500]}\n```",
                    parse_mode="Markdown"
                )
            
            # Update stats
            trans_db.add_transcription(user_id, f"{'voice' if file_type == 'voice' else 'audio'}", 
                                     result["duration"], result["word_count"], result_path)
            
            # Cleanup
            os.unlink(audio_path)
            os.unlink(wav_path)
            if os.path.exists(result_path):
                os.unlink(result_path)
            
        except Exception as e:
            logger.error(f"Error processing {file_type}: {e}")
            await progress_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    
    # Register handlers
    dp.add_handler(CommandHandler("transcribe", cmd_transcribe))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CallbackQueryHandler(cb_transcribe, pattern=r"^transcribe_"))
    dp.add_handler(MessageHandler(filters.VOICE, handle_voice))
    dp.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    dp.add_handler(MessageHandler(filters.VIDEO, handle_video))
    
    logger.info("Transcription handlers registered")