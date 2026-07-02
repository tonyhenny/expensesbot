# Expenses Bot

Простой Telegram-бот для расходов на Python, aiogram и SQLite.

## Запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

В `.env` вставь токен:

```env
BOT_TOKEN=твой_токен
```

Потом:

```powershell
python bot.py
```

## Как пользоваться

Напиши боту:

```text
кофе 300
```

Бот покажет inline-кнопки категорий. Можно выбрать готовую или нажать `➕ Своя`.

Команды:

- `/stats` - расходы за сегодня
- `/recent` - последние расходы
- `/categories` - список категорий
- `/addcategory` - добавить категорию
- `/help` - помощь

База создаётся рядом с файлом: `expenses.sqlite3`.
