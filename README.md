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
- `/pro` - купить Pro навсегда за Telegram Stars
- `/export` - экспорт CSV/XLSX/JSON для Pro
- `/limits` - лимиты по категориям для Pro
- `/limit Кофе 3000` - задать месячный лимит для Pro
- `/search кофе` - поиск расходов для Pro
- `/month` - месячный отчёт для Pro
- `/recurring` - повторяющиеся расходы для Pro
- `/stoprepeat 1` - остановить повтор для Pro
- `/help` - помощь

База создаётся рядом с файлом: `expenses.sqlite3`.
