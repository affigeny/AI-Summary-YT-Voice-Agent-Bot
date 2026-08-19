"""
Checkpoint system — восстановление состояния бота из Markdown файлов
Если бот прервётся, можно восстановить всё из VAULT-2
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
import sqlite3

logger = logging.getLogger(__name__)

# Путь к vault
VAULT_PATH = Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents" / "VAULT-2"
CHECKPOINT_DIR = VAULT_PATH / "b" / "checkpoints"


class CheckpointManager:
    """Менеджер checkpoint'ов для восстановления состояния бота"""
    
    def __init__(self, bot_state_path: str = "bot_state.json"):
        self.bot_state_path = bot_state_path or str(VAULT_PATH / "bot_state.json")
        self.checkpoint_dir = CHECKPOINT_DIR
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        
        # Инициализация базового checkpoint
        self._init_checkpoint()
    
    def _init_checkpoint(self):
        """Создать базовый checkpoint файл"""
        checkpoint_file = self.checkpoint_dir / "0000000000_base.md"
        
        if not checkpoint_file.exists():
            content = f"""# 🤖 Бот Checkpoint — Базовый

**Создан:** {datetime.now().isoformat()}
**Статус:** Инициализация
**Версия бота:** 4.1.4
**Модуль транскрибации:** Буквица v2.0

## 📊 Состояние системы

```json
{{
    "transcriptions_count": 0,
    "users_count": 0,
    "last_error": null,
    "last_restart": "{datetime.now().isoformat()}"
}}
```

## 🎯 Следующие шаги

- [ ] Настроить TELEGRAM_BOT_TOKEN
- [ ] Настроить AI_API_KEY
- [ ] Протестировать транскрибацию
- [ ] Деплоить на Render

## 📝 Логи восстановления

| Время | Действие | Статус |
|-------|----------|--------|
| {datetime.now().isoformat()} | Базовый checkpoint создан | ✅ |

---
*Checkpoint system v1.0*
"""
            checkpoint_file.write_text(content, encoding="utf-8")
            logger.info(f"Base checkpoint created: {checkpoint_file}")
    
    def save_checkpoint(self, state: Dict[str, Any], reason: str = "") -> str:
        """
        Сохранить состояние бота в checkpoint
        
        Args:
            state: словарь с состоянием бота
            reason: причина сохранения (crash, manual, etc.)
        
        Returns:
            путь к сохранённому checkpoint файлу
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        checkpoint_file = self.checkpoint_dir / f"{timestamp}_{reason}.md"
        
        # Формируем markdown контент
        content = f"""# 🤖 Бот Checkpoint — {reason}

**Сохранено:** {datetime.now().isoformat()}
**Статус:** {'CRASH' if 'crash' in reason.lower() else 'OK'}
**Версия бота:** 4.1.4

## 📊 Состояние системы

```json
{json.dumps(state, indent=4, ensure_ascii=False)}
```

## 🎯 Что было сделано перед остановкой

{state.get('recent_actions', 'Нет данных')}

## 📝 Ошибка (если была)

```
{state.get('last_error', 'Нет ошибок')}
```

## 🔧 Для восстановления

1. Скопировать `bot_database.db` из backup
2. Запустить бота: `python bot.py`
3. Бот автоматически загрузит checkpoint

## 📋 История checkpoint'ов

| Время | Причина | Статус |
|-------|---------|--------|
"""
        
        # Добавляем историю из предыдущих checkpoint'ов
        existing = self.checkpoint_dir.glob("*.md")
        for ef in sorted(existing, reverse=True)[:5]:
            content += f"| {ef.stem[:19]} | {ef.stem.split('_')[-1] if '_' in ef.stem else 'unknown'} | ✅ |\n"
        
        content += """
---
*Checkpoint system v1.0*
"""
        
        # Сохраняем файл
        checkpoint_file.write_text(content, encoding="utf-8")
        
        # Также сохраняем JSON для программной обработки
        json_file = self.checkpoint_dir / f"{timestamp}_{reason}.json"
        json_file.write_text(json.dumps(state, indent=4, ensure_ascii=False), encoding="utf-8")
        
        logger.info(f"Checkpoint saved: {checkpoint_file}")
        return str(checkpoint_file)
    
    def load_latest_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Загрузить последний checkpoint"""
        checkpoints = sorted(self.checkpoint_dir.glob("*.json"), reverse=True)
        
        if not checkpoints:
            logger.warning("No checkpoints found")
            return None
        
        latest = checkpoints[0]
        logger.info(f"Loading checkpoint: {latest.name}")
        
        with open(latest, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def create_recovery_note(self, bot_state: Dict[str, Any]):
        """Создать заметку для восстановления в Obsidian"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        recovery_file = self.checkpoint_dir / f"RECOVERY_{timestamp}.md"
        
        content = f"""# 🔄 Восстановление бота — {datetime.now().strftime('%Y-%m-%d %H:%M')}

## ❗ Проблема

Бот был остановлен. Для восстановления выполните:

## ✅ Шаг 1: Проверить базу данных
```bash
ls -la bot_database.db
```

## ✅ Шаг 2: Восстановить checkpoint
```bash
python -c "
from checkpoint_manager import CheckpointManager
cm = CheckpointManager()
state = cm.load_latest_checkpoint()
print(f'Checkpoint loaded: {state}')
"
```

## ✅ Шаг 3: Запустить бота
```bash
python bot.py
```

## 📊 Состояние перед остановкой

```json
{json.dumps(bot_state, indent=2, ensure_ascii=False)}
```

## 🔗 Связанные файлы

- Checkpoint: {self.checkpoint_dir.name}
- Бэкап базы: {self.bot_state_path}

---
*Создано автоматически Checkpoint Manager*
"""
        
        recovery_file.write_text(content, encoding="utf-8")
        logger.info(f"Recovery note created: {recovery_file}")
        return str(recovery_file)


def backup_database(db_path: str, vault_path: str = None):
    """
    Создать бэкап базы данных в Obsidian vault
    
    Args:
        db_path: путь к базе данных бота
        vault_path: путь к vault (опционально)
    """
    if not os.path.exists(db_path):
        logger.warning(f"Database not found: {db_path}")
        return
    
    vault = vault_path or str(VAULT_PATH)
    backup_dir = Path(vault) / "backups" / "database"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"bot_database_{timestamp}.db"
    
    # Копируем базу
    import shutil
    shutil.copy2(db_path, backup_file)
    
    # Создаём MD файл с метаданными
    md_file = backup_dir / f"bot_database_{timestamp}.md"
    
    # Получаем статистику
    conn = sqlite3.connect(backup_file)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
    except:
        users_count = 0
    
    try:
        cursor.execute("SELECT COUNT(*) FROM transcriptions")
        transcriptions_count = cursor.fetchone()[0]
    except:
        transcriptions_count = 0
    
    try:
        cursor.execute("SELECT MAX(created_at) FROM transcriptions")
        last_transcription = cursor.fetchone()[0]
    except:
        last_transcription = "N/A"
    
    conn.close()
    
    content = f"""# 🗄 Бэкап базы данных бота

**Создан:** {datetime.now().isoformat()}
**Файл:** {backup_file.name}

## 📊 Статистика

| Метрика | Значение |
|---------|----------|
| Пользователи | {users_count} |
| Транскрибации | {transcriptions_count} |
| Последняя транскрибация | {last_transcription} |

## 🔧 Для восстановления

```bash
cp {backup_file.name} bot_database.db
python bot.py
```

---
*Автоматический бэкап*
"""
    
    md_file.write_text(content, encoding="utf-8")
    
    logger.info(f"Database backup created: {backup_file}")
    return str(backup_file)


if __name__ == "__main__":
    # Тестирование
    cm = CheckpointManager()
    
    test_state = {
        "transcriptions_count": 42,
        "users_count": 15,
        "last_error": None,
        "last_restart": datetime.now().isoformat(),
        "recent_actions": [
            "Пользователь 123 отправил голосовое",
            "Транскрибация завершена (45 сек, 120 слов)",
            "Результат отправлен в Telegram"
        ]
    }
    
    cp_path = cm.save_checkpoint(test_state, "test")
    print(f"✅ Checkpoint saved: {cp_path}")
    
    # Тест восстановления
    loaded = cm.load_latest_checkpoint()
    print(f"✅ Checkpoint loaded: {loaded}")
    
    # Тест бэкапа
    backup = backup_database("bot_database.db")
    print(f"✅ Backup created: {backup}")            content = f"""# 🤖 Бот Checkpoint — Базовый

**Создан:** {datetime.now().isoformat()}
**Статус:** Инициализация
**Версия бота:** 4.1.4
**Модуль транскрибации:** Буквица v2.0

## 📊 Состояние системы

```json
{{
    "transcriptions_count": 0,
    "users_count": 0,
    "last_error": null,
    "last_restart": "{datetime.now().isoformat()}"
}}
```

## 🎯 Следующие шаги

- [ ] Настроить TELEGRAM_BOT_TOKEN
- [ ] Настроить AI_API_KEY
- [ ] Протестировать транскрибацию
- [ ] Деплоить на Render

## 📝 Логи восстановления

| Время | Действие | Статус |
|-------|----------|--------|
| {datetime.now().isoformat()} | Базовый checkpoint создан | ✅ |

---
*Checkpoint system v1.0*
"""
            checkpoint_file.write_text(content, encoding="utf-8")
            logger.info(f"Base checkpoint created: {checkpoint_file}")
    
    def save_checkpoint(self, state: Dict[str, Any], reason: str = "") -> str:
        """
        Сохранить состояние бота в checkpoint
        
        Args:
            state: словарь с состоянием бота
            reason: причина сохранения (crash, manual, etc.)
        
        Returns:
            путь к сохранённому checkpoint файлу
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        checkpoint_file = self.checkpoint_dir / f"{timestamp}_{reason}.md"
        
        # Формируем markdown контент
        content = f"""# 🤖 Бот Checkpoint — {reason}

**Сохранено:** {datetime.now().isoformat()}
**Статус:** {'CRASH' if 'crash' in reason.lower() else 'OK'}
**Версия бота:** 4.1.4

## 📊 Состояние системы

```json
{json.dumps(state, indent=4, ensure_ascii=False)}
```

## 🎯 Что было сделано перед остановкой

{state.get('recent_actions', 'Нет данных')}

## 📝 Ошибка (если была)

```
{state.get('last_error', 'Нет ошибок')}
```

## 🔧 Для восстановления

1. Скопировать `bot_database.db` из backup
2. Запустить бота: `python bot.py`
3. Бот автоматически загрузит checkpoint

## 📋 История checkpoint'ов

| Время | Причина | Статус |
|-------|---------|--------|
"""
        
        # Добавляем историю из предыдущих checkpoint'ов
        existing = self.checkpoint_dir.glob("*.md")
        for ef in sorted(existing, reverse=True)[:5]:
            content += f"| {ef.stem[:19]} | {ef.stem.split('_')[-1] if '_' in ef.stem else 'unknown'} | ✅ |\n"
        
        content += """
---
*Checkpoint system v1.0*
"""
        
        # Сохраняем файл
        checkpoint_file.write_text(content, encoding="utf-8")
        
        # Также сохраняем JSON для программной обработки
        json_file = self.checkpoint_dir / f"{timestamp}_{reason}.json"
        json_file.write_text(json.dumps(state, indent=4, ensure_ascii=False), encoding="utf-8")
        
        logger.info(f"Checkpoint saved: {checkpoint_file}")
        return str(checkpoint_file)
    
    def load_latest_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Загрузить последний checkpoint"""
        checkpoints = sorted(self.checkpoint_dir.glob("*.json"), reverse=True)
        
        if not checkpoints:
            logger.warning("No checkpoints found")
            return None
        
        latest = checkpoints[0]
        logger.info(f"Loading checkpoint: {latest.name}")
        
        with open(latest, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def create_recovery_note(self, bot_state: Dict[str, Any]):
        """Создать заметку для восстановления в Obsidian"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        recovery_file = self.checkpoint_dir / f"RECOVERY_{timestamp}.md"
        
        content = f"""# 🔄 Восстановление бота — {datetime.now().strftime('%Y-%m-%d %H:%M')}

## ❗ Проблема

Бот был остановлен. Для восстановления выполните:

## ✅ Шаг 1: Проверить базу данных
```bash
ls -la bot_database.db
```

## ✅ Шаг 2: Восстановить checkpoint
```bash
python -c "
from checkpoint_manager import CheckpointManager
cm = CheckpointManager()
state = cm.load_latest_checkpoint()
print(f'Checkpoint loaded: {state}')
"
```

## ✅ Шаг 3: Запустить бота
```bash
python bot.py
```

## 📊 Состояние перед остановкой

```json
{json.dumps(bot_state, indent=2, ensure_ascii=False)}
```

## 🔗 Связанные файлы

- Checkpoint: {self.checkpoint_dir.name}
- Бэкап базы: {self.bot_state_path}

---
*Создано автоматически Checkpoint Manager*
"""
        
        recovery_file.write_text(content, encoding="utf-8")
        logger.info(f"Recovery note created: {recovery_file}")
        return str(recovery_file)


def backup_database(db_path: str, vault_path: str = None):
    """
    Создать бэкап базы данных в Obsidian vault
    
    Args:
        db_path: путь к базе данных бота
        vault_path: путь к vault (опционально)
    """
    if not os.path.exists(db_path):
        logger.warning(f"Database not found: {db_path}")
        return
    
    vault = vault_path or str(VAULT_PATH)
    backup_dir = Path(vault) / "backups" / "database"
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"bot_database_{timestamp}.db"
    
    # Копируем базу
    import shutil
    shutil.copy2(db_path, backup_file)
    
    # Создаём MD файл с метаданными
    md_file = backup_dir / f"bot_database_{timestamp}.md"
    
    # Получаем статистику
    conn = sqlite3.connect(backup_file)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
    except:
        users_count = 0
    
    try:
        cursor.execute("SELECT COUNT(*) FROM transcriptions")
        transcriptions_count = cursor.fetchone()[0]
    except:
        transcriptions_count = 0
    
    try:
        cursor.execute("SELECT MAX(created_at) FROM transcriptions")
        last_transcription = cursor.fetchone()[0]
    except:
        last_transcription = "N/A"
    
    conn.close()
    
    content = f"""# 🗄 Бэкап базы данных бота

**Создан:** {datetime.now().isoformat()}
**Файл:** {backup_file.name}

## 📊 Статистика

| Метрика | Значение |
|---------|----------|
| Пользователи | {users_count} |
| Транскрибации | {transcriptions_count} |
| Последняя транскрибация | {last_transcription} |

## 🔧 Для восстановления

```bash
cp {backup_file.name} bot_database.db
python bot.py
```

---
*Автоматический бэкап*
"""
    
    md_file.write_text(content, encoding="utf-8")
    
    logger.info(f"Database backup created: {backup_file}")
    return str(backup_file)


if __name__ == "__main__":
    # Тестирование
    cm = CheckpointManager()
    
    test_state = {
        "transcriptions_count": 42,
        "users_count": 15,
        "last_error": None,
        "last_restart": datetime.now().isoformat(),
        "recent_actions": [
            "Пользователь 123 отправил голосовое",
            "Транскрибация завершена (45 сек, 120 слов)",
            "Результат отправлен в Telegram"
        ]
    }
    
    cp_path = cm.save_checkpoint(test_state, "test")
    print(f"✅ Checkpoint saved: {cp_path}")
    
    # Тест восстановления
    loaded = cm.load_latest_checkpoint()
    print(f"✅ Checkpoint loaded: {loaded}")
    
    # Тест бэкапа
    backup = backup_database("bot_database.db")
    print(f"✅ Backup created: {backup}")
