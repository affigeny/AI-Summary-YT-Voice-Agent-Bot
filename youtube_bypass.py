"""
YT_Bot_Sum v4.3.0 — YouTube Bypass Interface
Простой интерфейс с кнопками для обхода защиты YouTube
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import yt_dlp
import asyncio
import os

# ==================== КОНФИГ ====================
YOUTUBE_BYPASS_METHODS = {
    "no_cookies": "🚫 Без куки",
    "cookie_file": "📁 Файл куки",
    "browser_chrome": "🌐 Chrome",
    "browser_firefox": "🦊 Firefox",
    "test_all": "🧪 Тест всех"
}

# Текущий метод для каждого пользователя
user_bypass_methods = {}


# ==================== КНОПКИ ====================
def get_bypass_keyboard():
    """Создать клавиатуру с методами обхода"""
    keyboard = [
        [
            InlineKeyboardButton("🚫 Без куки", callback_data="bypass_no_cookies"),
            InlineKeyboardButton("📁 Файл куки", callback_data="bypass_cookie_file"),
        ],
        [
            InlineKeyboardButton("🌐 Chrome", callback_data="bypass_browser_chrome"),
            InlineKeyboardButton("🦊 Firefox", callback_data="bypass_browser_firefox"),
        ],
        [
            InlineKeyboardButton("🧪 Тест всех методов", callback_data="bypass_test_all"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("ℹ️ Помощь", callback_data="help"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ==================== ОБРАБОТЧИКИ ====================
async def cmd_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /bypass — выбор метода обхода"""
    user_id = update.effective_user.id
    
    current = user_bypass_methods.get(user_id, "no_cookies")
    
    await update.message.reply_text(
        f"🔧 *Выбор метода обхода YouTube*\n\n"
        f"Текущий: **{current}**\n\n"
        "Выберите метод ниже:",
        parse_mode="Markdown",
        reply_markup=get_bypass_keyboard()
    )


async def cb_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора метода"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data.startswith("bypass_"):
        method = data.replace("bypass_", "")
        user_bypass_methods[user_id] = method
        
        await query.edit_message_text(
            f"✅ Выбран метод: **{method}**\n\n"
            f"Теперь отправьте ссылку на YouTube для транскрибации."
        )
    
    elif data == "stats":
        await query.edit_message_text(
            f"📊 *Статистика*\n\n"
            f"Всего пользователей: 1\n"
            f"Текущий метод: {user_bypass_methods.get(user_id, 'no_cookies')}"
        )
    
    elif data == "help":
        await query.edit_message_text(
            "📖 *Помощь:*\n\n"
            "🚫 **Без куки** — стандартный метод\n"
            "📁 **Файл куки** — если есть cookies.txt\n"
            "🌐 **Chrome** — куки из браузера Chrome\n"
            "🦊 **Firefox** — куки из браузера Firefox\n"
            "🧪 **Тест всех** — проверка всех методов\n\n"
            "После выбора отправьте ссылку на YouTube."
        )


async def test_all_methods(url: str, user_id: int) -> dict:
    """Протестировать все методы обхода"""
    results = {}
    
    # Метод 1: Без куки
    try:
        opts = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            results["no_cookies"] = True
    except Exception as e:
        results["no_cookies"] = False
    
    # Метод 2: Chrome
    try:
        opts = {'quiet': True, 'no_warnings': True, 'cookiesfrombrowser': ('chrome',)}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            results["browser_chrome"] = True
    except Exception as e:
        results["browser_chrome"] = False
    
    # Метод 3: Firefox
    try:
        opts = {'quiet': True, 'no_warnings': True, 'cookiesfrombrowser': ('firefox',)}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            results["browser_firefox"] = True
    except Exception as e:
        results["browser_firefox"] = False
    
    # Метод 4: Файл куки
    cookies_file = os.getenv("YT_COOKIES_FILE", "")
    if cookies_file and os.path.exists(cookies_file):
        try:
            opts = {'quiet': True, 'no_warnings': True, 'cookiefile': cookies_file}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                results["cookie_file"] = True
        except Exception as e:
            results["cookie_file"] = False
    else:
        results["cookie_file"] = False  # Файл не найден
    
    return results


async def handle_youtube_bypass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка YouTube ссылки с обходом"""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # Определяем метод
    method = user_bypass_methods.get(user_id, "no_cookies")
    
    if method == "test_all":
        # Тестируем все методы
        progress_msg = await update.message.reply_text("🧪 Тестирую все методы обхода...")
        
        try:
            results = await test_all_methods(url, user_id)
            
            # Находим лучший метод
            best_method = "no_cookies"
            for m, success in results.items():
                if success:
                    best_method = m
                    break
            
            # Сохраняем лучший метод
            user_bypass_methods[user_id] = best_method
            
            # Формируем результат
            result_text = "✅ *Тест завершён!*\n\n"
            result_text += f"🚫 Без куки: {'✅' if results.get('no_cookies') else '❌'}\n"
            result_text += f"📁 Файл куки: {'✅' if results.get('cookie_file') else '❌'}\n"
            result_text += f"🌐 Chrome: {'✅' if results.get('browser_chrome') else '❌'}\n"
            result_text += f"🦊 Firefox: {'✅' if results.get('browser_firefox') else '❌'}\n\n"
            result_text += f"🎯 Лучший метод: **{best_method}**\n\n"
            result_text += "Теперь отправьте ссылку ещё раз для транскрибации."
            
            await progress_msg.edit_text(result_text, parse_mode="Markdown")
            
        except Exception as e:
            await progress_msg.edit_text(f"❌ Ошибка теста: {str(e)[:100]}")
    
    else:
        # Используем выбранный метод
        progress_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
        
        try:
            # Настраиваем опции yt-dlp
            opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
            
            if method == "cookie_file":
                cookies_file = os.getenv("YT_COOKIES_FILE", "")
                if cookies_file:
                    opts['cookiefile'] = cookies_file
            
            elif method.startswith("browser_"):
                browser = method.replace("browser_", "")
                opts['cookiesfrombrowser'] = (browser,)
            
            # Скачиваем
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                audio_path = ydl.prepare_filename(info)
                
                # Конвертируем в mp3 если нужно
                if not audio_path.endswith('.mp3'):
                    audio_path = audio_path.rsplit('.', 1)[0] + '.mp3'
                    if os.path.exists(ydl.prepare_filename(info)):
                        os.rename(ydl.prepare_filename(info), audio_path)
            
            await progress_msg.edit_text("🎙️ Распознаю речь...")
            
            # Здесь будет вызов Whisper (из основного бота)
            # Для теста просто отправляем инфо
            result_text = f"✅ *Аудио скачано!*\n\n"
            result_text += f"📹 Видео: {info.get('title', 'N/A')}\n"
            result_text += f"⏱ Длительность: {info.get('duration', 0)} сек\n"
            result_text += f"🔧 Метод: {method}\n\n"
            result_text += f"📁 Файл: {audio_path}"
            
            await progress_msg.edit_text(result_text, parse_mode="Markdown")
            
            # Отправляем файл
            if os.path.exists(audio_path):
                with open(audio_path, 'rb') as f:
                    await update.message.reply_document(
                        f,
                        caption="🎵 Аудиофайл для транскрибации"
                    )
                os.unlink(audio_path)
            
        except Exception as e:
            await progress_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}\n\nПопробуйте другой метод через /bypass")


# ==================== РЕГИСТРАЦИЯ ====================
def register_youtube_handlers(dp):
    """Регистрация обработчиков в боте"""
    from telegram.ext import CommandHandler, MessageHandler, filters
    
    dp.add_handler(CommandHandler("bypass", cmd_bypass))
    dp.add_handler(MessageHandler(filters.TEXT & filters.Regex(r'https?://.*youtube\.com|https?://.*youtu\.be'), handle_youtube_bypass))
    
    print("✅ YouTube bypass handlers registered")