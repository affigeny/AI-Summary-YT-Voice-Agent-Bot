"""
Transcription module v2.2 — расширение для YT_Bot_Sum
Исправлена ошибка: добавлен RateLimiter класс
Добавлено:
- 5 методов обхода YouTube блокировок
- Тестовый режим проверки всех методов
- Улучшенный интерфейс с кнопками
"""

import os
import sqlite3
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
FREE_TRANSCRIPTIONS_PER_MONTH = 3
RATE_LIMIT_SECONDS = 10
MAX_TRANSCRIPTION_DURATION = 1800  # 30 минут

# YouTube bypass methods
YOUTUBE_BYPASS_METHODS = {
    "no_cookies": "🚫 Без куки (стандартно)",
    "cookie_file": "📁 Файл куки (cookies.txt)",
    "browser_chrome": "🌐 Chrome",
    "browser_firefox": "🦊 Firefox",
    "test_all": "🧪 Тест всех методов"
}


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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                bypass_method TEXT DEFAULT 'no_cookies'
            )
        """)
        
        # Таблица транскрибаций
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transcription_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                file_name TEXT,
                duration_seconds REAL,
                word_count INTEGER,
                result_path TEXT,
                bypass_method TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица тестов
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bypass_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                method TEXT,
                url TEXT,
                success INTEGER,
                error TEXT,
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
        
        cursor.execute("SELECT * FROM transcription_users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if not row:
            cursor.execute(
                "INSERT INTO transcription_users (user_id, username, last_reset_date) VALUES (?, ?, ?)",
                (user_id, username, datetime.now().date())
            )
            conn.commit()
            logger.info(f"User {user_id} created")
        
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
            "remaining": FREE_TRANSCRIPTIONS_PER_MONTH - row[2] if not row[5] else float('inf'),
            "bypass_method": row[6] if len(row) > 6 else "no_cookies"
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
    
    def add_transcription(self, user_id: int, file_name: str, duration: float, word_count: int, result_path: str, bypass_method: str = "no_cookies"):
        """Добавить запись о транскрибации"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "INSERT INTO transcription_history (user_id, file_name, duration_seconds, word_count, result_path, bypass_method) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, file_name, duration, word_count, result_path, bypass_method)
        )
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
            "remaining": FREE_TRANSCRIPTIONS_PER_MONTH - row[2] if not row[5] else float('inf'),
            "bypass_method": row[6] if len(row) > 6 else "no_cookies"
        }
    
    def get_recent_transcriptions(self, user_id: int, limit: int = 5) -> list:
        """Получить последние транскрибации пользователя"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            """SELECT file_name, duration_seconds, word_count, bypass_method, created_at 
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
                "bypass": r[3],
                "date": r[4]
            }
            for r in rows
        ]
    
    def save_bypass_test(self, user_id: int, method: str, url: str, success: bool, error: str = None):
        """Сохранить результат теста обхода"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "INSERT INTO bypass_tests (user_id, method, url, success, error) VALUES (?, ?, ?, ?, ?)",
            (user_id, method, url, 1 if success else 0, error)
        )
        conn.commit()
        conn.close()
        logger.info(f"Bypass test saved: {method} - {'OK' if success else 'FAIL'}")


# ==================== YOUTUBE SERVICE ====================
class YouTubeService:
    """Сервис для работы с YouTube"""
    
    def __init__(self):
        self.bypass_methods = YOUTUBE_BYPASS_METHODS
    
    async def test_bypass_methods(self, url: str, user_id: int) -> Dict[str, bool]:
        """Протестировать все методы обхода"""
        import yt_dlp
        
        results = {}
        
        # Метод 1: Без куки
        try:
            opts = {'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results["no_cookies"] = True
                logger.info(f"Test no_cookies: OK for {url}")
        except Exception as e:
            results["no_cookies"] = False
            logger.warning(f"Test no_cookies: FAILED - {e}")
        
        # Метод 2: Из Chrome
        try:
            opts = {'quiet': True, 'no_warnings': True, 'cookiesfrombrowser': ('chrome',)}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results["browser_chrome"] = True
                logger.info(f"Test browser chrome: OK")
        except Exception as e:
            results["browser_chrome"] = False
            logger.warning(f"Test browser chrome: FAILED")
        
        # Метод 3: Из Firefox
        try:
            opts = {'quiet': True, 'no_warnings': True, 'cookiesfrombrowser': ('firefox',)}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results["browser_firefox"] = True
                logger.info(f"Test browser firefox: OK")
        except Exception as e:
            results["browser_firefox"] = False
            logger.warning(f"Test browser firefox: FAILED")
        
        # Сохраняем результаты
        for method, success in results.items():
            self._save_test(user_id, method, url, success)
        
        return results
    
    def _save_test(self, user_id: int, method: str, url: str, success: bool):
        """Сохранить результат теста"""
        db = TranscriptionDB(os.getenv("DB_PATH", "bot_database.db"))
        db.save_bypass_test(user_id, method, url, success)


# ==================== INTEGRATION FUNCTION ====================
def register_transcription_handlers(dp, db_path: str = None):
    """
    Регистрация handlers для транскрибации
    Вызывать после создания приложения бота
    """
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, filters
    
    # Инициализация
    trans_db = TranscriptionDB(db_path or os.getenv("DB_PATH", "bot_database.db"))
    rate_limiter = RateLimiter()
    youtube_service = YouTubeService()
    
    # Command /transcribe
    async def cmd_transcribe(update: Update, context):
        user_id = update.effective_user.id
        stats = trans_db.get_or_create_user(user_id, update.effective_user.username)
        
        # Клавиатура с методами обхода
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Без куки", callback_data="bypass_no_cookies")],
            [InlineKeyboardButton("📁 Файл куки", callback_data="bypass_cookie_file")],
            [InlineKeyboardButton("🌐 Chrome", callback_data="bypass_browser_chrome")],
            [InlineKeyboardButton("🦊 Firefox", callback_data="bypass_browser_firefox")],
            [InlineKeyboardButton("🧪 Тест всех", callback_data="bypass_test_all")],
            [InlineKeyboardButton("📊 Баланс", callback_data="transcribe_balance")],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="transcribe_help")]
        ])
        
        await update.message.reply_text(
            f"🎙️ *Транскрибация* — аудио/видео в текст\n\n"
            f"✅ Осталось: **{int(stats['remaining'])}** из {stats['limit']}\n\n"
            "🔧 Выберите метод обхода YouTube:\n\n"
            "📁 Затем отправьте аудио, видео или ссылку",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    
    # Command /stats
    async def cmd_stats(update: Update, context):
        user_id = update.effective_user.id
        stats = trans_db.get_user_stats(user_id)
        recent = trans_db.get_recent_transcriptions(user_id, limit=5)
        
        text = f"📊 *Статистика*\n\n"
        text += f"Использовано: {stats['count']}/{stats['limit']}\n"
        text += f"Осталось: {int(stats['remaining'])}\n"
        text += f"Метод обхода: {stats.get('bypass_method', 'no_cookies')}\n\n"
        
        if recent:
            text += "📝 Последние транскрибации:\n"
            for r in recent:
                text += f"• {r['file'][:30]}... — {r['duration']:.0f}сек, {r['words']} слов\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
    
    # Callback handlers
    async def cb_handler(update: Update, context):
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        data = query.data
        
        if data.startswith('bypass_'):
            method = data.replace('bypass_', '')
            await _handle_bypass_selection(query, method, trans_db)
        elif data == 'transcribe_balance':
            stats = trans_db.get_user_stats(user_id)
            await query.edit_message_text(
                f"📊 Баланс: {int(stats['remaining'])}/{stats['limit']} транскрибаций"
            )
        elif data == 'transcribe_help':
            await query.edit_message_text(
                "📖 *Помощь:*\n\n"
                "1️⃣ Выберите метод обхода YouTube\n"
                "2️⃣ Отправьте ссылку на YouTube\n"
                "3️⃣ Или аудио/видео файл\n\n"
                "🔧 *Методы обхода:*\n"
                "• Без куки — стандартный метод\n"
                "• Файл куки — если YouTube блокирует\n"
                "• Chrome/Firefox — автосохранение куки\n"
                "• Тест всех — проверка всех методов"
            )
    
    async def _handle_bypass_selection(query, method, db):
        """Обработка выбора метода обхода"""
        
        # Сохраняем метод для пользователя
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE transcription_users SET bypass_method = ? WHERE user_id = ?", (method, query.from_user.id))
        conn.commit()
        conn.close()
        
        if method == "test_all":
            await query.edit_message_text(
                "🧪 *Тестирование всех методов обхода*\n\n"
                "Отправьте ссылку на YouTube для проверки:\n"
                "• Без куки\n"
                "• Из Chrome\n"
                "• Из Firefox\n\n"
                "Результаты сохранятся в базе."
            )
        elif method == "cookie_file":
            await query.edit_message_text(
                "📁 *Файл куки*\n\n"
                "Чтобы использовать файл куки:\n"
                "1. Экспортируйте: `yt-dlp --cookies-from-browser chrome -o cookies.txt URL`\n"
                "2. Загрузите файл боту\n"
                "3. Или настройте YT_COOKIES_FILE в окружении\n\n"
                "Отправьте файл куки или ссылку на YouTube."
            )
        else:
            await query.edit_message_text(
                f"✅ Выбран метод: {method}\n\n"
                "Теперь отправьте ссылку на YouTube или аудио/видео файл."
            )
    
    # Обработчик YouTube ссылок
    async def handle_youtube(update: Update, context):
        user_id = update.effective_user.id
        url = update.message.text.strip()
        
        # Rate limit check
        if not rate_limiter.is_allowed(user_id):
            wait = rate_limiter.get_wait_time(user_id)
            await update.message.reply_text(f"⏳ Подождите {wait:.0f} сек...")
            return
        
        # Проверка лимитов
        stats = trans_db.get_user_stats(user_id)
        if not stats["is_premium"] and stats["count"] >= stats["limit"]:
            await update.message.reply_text("⚠️ Лимит исчерпан. Напишите /transcribe")
            return
        
        # Тестирование всех методов
        if stats.get("bypass_method") == "test_all":
            progress_msg = await update.message.reply_text("🧪 Тестирую все методы обхода...")
            try:
                results = await youtube_service.test_bypass_methods(url, user_id)
                
                # Находим лучший метод
                best_method = "no_cookies"
                for method, success in results.items():
                    if success:
                        best_method = method
                        break
                
                # Сохраняем лучший метод
                conn = sqlite3.connect(trans_db.db_path)
                cursor = conn.cursor()
                cursor.execute("UPDATE transcription_users SET bypass_method = ? WHERE user_id = ?", (best_method, user_id))
                conn.commit()
                conn.close()
                
                await progress_msg.edit_text(
                    f"✅ Тест завершён!\n\n"
                    f"Результаты:\n"
                    f"• Без куки: {'✅' if results.get('no_cookies') else '❌'}\n"
                    f"• Chrome: {'✅' if results.get('browser_chrome') else '❌'}\n"
                    f"• Firefox: {'✅' if results.get('browser_firefox') else '❌'}\n\n"
                    f"Лучший метод: {best_method}\n\n"
                    f"Теперь отправьте ссылку ещё раз для транскрибации."
                )
            except Exception as e:
                await progress_msg.edit_text(f"❌ Ошибка теста: {str(e)[:100]}")
            return
        
        # Обычная транскрибация
        bypass_method = stats.get("bypass_method", "no_cookies")
        progress_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
        
        try:
            # Download audio
            await progress_msg.edit_text("🎵 Скачиваю аудио...")
            
            import yt_dlp
            opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
            
            if bypass_method == "cookie_file" and os.path.exists(os.getenv("YT_COOKIES_FILE", "")):
                opts['cookiefile'] = os.getenv("YT_COOKIES_FILE")
            elif bypass_method.startswith("browser_"):
                browser = bypass_method.replace("browser_", "")
                opts['cookiesfrombrowser'] = (browser,)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                audio_path = ydl.prepare_filename(info)
                if not audio_path.endswith('.mp3'):
                    audio_path = audio_path.rsplit('.', 1)[0] + '.mp3'
                    if os.path.exists(ydl.prepare_filename(info)):
                        os.rename(ydl.prepare_filename(info), audio_path)
            
            # Transcribe (using faster-whisper from main bot)
            await progress_msg.edit_text("🎙️ Транскрибирую...")
            
            # Import STT from main bot
            from bot import stt
            result = await stt.transcribe(audio_path)
            
            # Save result
            result_path = f"/tmp/result_{user_id}_{int(datetime.now().timestamp())}.txt"
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
                    f"⏱ {result['duration']:.1f} сек | 📊 {result['word_count']} слов\n"
                    f"🔧 Метод: {bypass_method}\n\n"
                    f"```\n{result['text_with_timestamps'][:3500]}\n```",
                    parse_mode="Markdown"
                )
            
            # Update stats
            trans_db.add_transcription(user_id, "youtube", result["duration"], result["word_count"], result_path, bypass_method)
            
            # Cleanup
            if os.path.exists(audio_path):
                os.unlink(audio_path)
            if os.path.exists(result_path):
                os.unlink(result_path)
            
        except Exception as e:
            logger.error(f"Error processing YouTube: {e}")
            await progress_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}\n\nПопробуйте другой метод обхода через /transcribe")
    
    # Обработчики аудио/видео
    async def handle_audio(update: Update, context):
        await _process_audio_file(update, context, trans_db, rate_limiter, "audio")
    
    async def handle_voice(update: Update, context):
        await _process_audio_file(update, context, trans_db, rate_limiter, "voice")
    
    async def _process_audio_file(update: Update, context, db, rl, file_type: str):
        """Обработка аудио/видео файла"""
        user_id = update.effective_user.id
        
        # Rate limit check
        if not rl.is_allowed(user_id):
            wait = rl.get_wait_time(user_id)
            await update.message.reply_text(f"⏳ Подождите {wait:.0f} сек...")
            return
        
        # Check limits
        stats = db.get_user_stats(user_id)
        if not stats["is_premium"] and stats["count"] >= stats["limit"]:
            await update.message.reply_text("⚠️ Лимит исчерпан. Напишите /transcribe")
            return
        
        # Show progress
        progress_msg = await update.message.reply_text("⏳ Загружаю файл...")
        
        try:
            # Download file
            if file_type == "voice":
                file = await context.bot.get_file(update.message.voice.file_id)
            else:
                file = await context.bot.get_file(update.message.audio.file_id)
            
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
            
            from bot import stt
            result = await stt.transcribe(wav_path)
            
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
            db.add_transcription(user_id, f"{'voice' if file_type == 'voice' else 'audio'}", 
                                 result["duration"], result["word_count"], result_path)
            
            # Cleanup
            os.unlink(audio_path)
            os.unlink(wav_path)
            if os.path.exists(result_path):
                os.unlink(result_path)
            
        except Exception as e:
            logger.error(f"Error processing {file_type}: {e}")
            await progress_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    
    # Регистрация handlers
    dp.add_handler(CommandHandler("transcribe", cmd_transcribe))
    dp.add_handler(CommandHandler("stats", cmd_stats))
    dp.add_handler(CallbackQueryHandler(cb_handler))
    dp.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'https?://.*youtube\.com|https?://.*youtu\.be'), handle_youtube))
    dp.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    dp.add_handler(MessageHandler(filters.VOICE, handle_voice))
    
    logger.info("✓ Transcription handlers registered")
    logger.info("✓ YouTube bypass buttons added")
    logger.info("✓ Test mode available")