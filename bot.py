import asyncio
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "expenses.sqlite3"
ENV_PATH = BASE_DIR / ".env"
PRO_PRICE_STARS = 1
PRO_PAYLOAD_PREFIX = "pro_forever"

DEFAULT_CATEGORIES = [
    ("🍔", "Еда"),
    ("☕", "Кофе"),
    ("🚌", "Транспорт"),
    ("🛒", "Продукты"),
    ("🏠", "Дом"),
    ("🎮", "Развлечения"),
    ("💊", "Здоровье"),
    ("🎁", "Другое"),
]

EXPENSE_RE = re.compile(
    r"^\s*(?P<title>.+?)\s+(?P<amount>\d+(?:[.,]\d{1,2})?)\s*(?:р|руб|₽)?\s*$",
    re.IGNORECASE,
)

router = Router()


class CustomCategory(StatesGroup):
    waiting_for_name = State()


@dataclass(frozen=True)
class Category:
    id: int
    emoji: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


def load_env() -> None:
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def connect_db():
    conn = db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(user_id, name)
            );

            CREATE TABLE IF NOT EXISTS pending_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS pro_subscriptions (
                user_id INTEGER PRIMARY KEY,
                activated_at TEXT NOT NULL,
                telegram_payment_charge_id TEXT,
                provider_payment_charge_id TEXT
            );
            """
        )


def ensure_default_categories(user_id: int) -> None:
    with connect_db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO categories(user_id, emoji, name) VALUES (?, ?, ?)",
            [(user_id, emoji, name) for emoji, name in DEFAULT_CATEGORIES],
        )


def get_categories(user_id: int) -> list[Category]:
    ensure_default_categories(user_id)
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT id, emoji, name FROM categories WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    return [Category(row["id"], row["emoji"], row["name"]) for row in rows]


def create_category(user_id: int, raw_name: str) -> Category:
    emoji = "📌"
    name = raw_name.strip() or "Своя"
    first = name.split(maxsplit=1)

    if len(first[0]) <= 2 and not first[0].isalnum():
        emoji = first[0]
        name = first[1].strip() if len(first) > 1 else "Своя"

    if not name:
        name = "Своя"

    with connect_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories(user_id, emoji, name) VALUES (?, ?, ?)",
            (user_id, emoji, name),
        )
        row = conn.execute(
            "SELECT id, emoji, name FROM categories WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
    return Category(row["id"], row["emoji"], row["name"])


def create_pending_expense(user_id: int, title: str, amount_cents: int) -> int:
    with connect_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO pending_expenses(user_id, title, amount_cents, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, title, amount_cents, datetime.now().isoformat(timespec="seconds")),
        )
        return int(cur.lastrowid)


def get_pending_expense(user_id: int, pending_id: int) -> sqlite3.Row | None:
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM pending_expenses WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        ).fetchone()


def save_expense(user_id: int, pending_id: int, category_id: int) -> sqlite3.Row | None:
    with connect_db() as conn:
        pending = conn.execute(
            "SELECT * FROM pending_expenses WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        ).fetchone()
        category = conn.execute(
            "SELECT * FROM categories WHERE id = ? AND user_id = ?",
            (category_id, user_id),
        ).fetchone()

        if pending is None or category is None:
            return None

        conn.execute(
            """
            INSERT INTO expenses(user_id, title, amount_cents, category_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                pending["title"],
                pending["amount_cents"],
                category_id,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.execute("DELETE FROM pending_expenses WHERE id = ?", (pending_id,))
        return pending


def cancel_pending(user_id: int, pending_id: int) -> bool:
    with connect_db() as conn:
        cur = conn.execute(
            "DELETE FROM pending_expenses WHERE id = ? AND user_id = ?",
            (pending_id, user_id),
        )
        return cur.rowcount > 0


def get_recent_expenses(user_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT e.id, e.title, e.amount_cents, e.created_at, c.emoji, c.name AS category_name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ?
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def delete_last_expense(user_id: int) -> sqlite3.Row | None:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT e.id, e.title, e.amount_cents, c.emoji, c.name AS category_name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ?
            ORDER BY e.id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        if row is None:
            return None

        conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (row["id"], user_id))
        return row


def is_pro(user_id: int) -> bool:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM pro_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def activate_pro(
    user_id: int,
    telegram_payment_charge_id: str | None,
    provider_payment_charge_id: str | None,
) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO pro_subscriptions(
                user_id,
                activated_at,
                telegram_payment_charge_id,
                provider_payment_charge_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                activated_at = excluded.activated_at,
                telegram_payment_charge_id = excluded.telegram_payment_charge_id,
                provider_payment_charge_id = excluded.provider_payment_charge_id
            """,
            (
                user_id,
                datetime.now().isoformat(timespec="seconds"),
                telegram_payment_charge_id,
                provider_payment_charge_id,
            ),
        )


def parse_expense(text: str) -> tuple[str, int] | None:
    match = EXPENSE_RE.match(text)
    if not match:
        return None

    title = match.group("title").strip()
    amount_text = match.group("amount").replace(",", ".")

    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        return None

    if amount <= 0:
        return None

    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return title, cents


def money(amount_cents: int) -> str:
    rubles = Decimal(amount_cents) / Decimal(100)
    if amount_cents % 100 == 0:
        return f"{int(rubles)} ₽"
    return f"{rubles:.2f} ₽"


def categories_keyboard(user_id: int, pending_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=category.label, callback_data=f"cat:{pending_id}:{category.id}")
        for category in get_categories(user_id)
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(text="➕ Своя", callback_data=f"custom:{pending_id}"),
            InlineKeyboardButton(text="✖️ Отмена", callback_data=f"cancel:{pending_id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="📊 Сегодня", callback_data="stats:today"),
            InlineKeyboardButton(text="📅 Месяц", callback_data="stats:month"),
        ],
        [
            InlineKeyboardButton(text="🧾 Последние", callback_data="recent"),
            InlineKeyboardButton(text="🏷 Категории", callback_data="categories"),
        ],
        [InlineKeyboardButton(text="➕ Категория", callback_data="add_category")],
    ]

    if user_id is not None and not is_pro(user_id):
        rows.append([InlineKeyboardButton(text="⭐ Pro навсегда", callback_data="pro")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def pro_keyboard(user_id: int) -> InlineKeyboardMarkup:
    if is_pro(user_id):
        return main_keyboard(user_id)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ Купить за {PRO_PRICE_STARS} XTR", callback_data="buy_pro")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def pro_text(user_id: int) -> str:
    if is_pro(user_id):
        return "Pro уже активирован навсегда."

    return (
        "Pro навсегда\n\n"
        f"Цена: {PRO_PRICE_STARS} ⭐\n"
        "Платные функции добавим позже. Сейчас покупка просто включает Pro-статус."
    )


def stats_text(user_id: int, period: str) -> str:
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = "Сегодня"
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        title = "Этот месяц"

    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT c.emoji, c.name, SUM(e.amount_cents) AS total, COUNT(*) AS count
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ? AND e.created_at >= ?
            GROUP BY c.id
            ORDER BY total DESC
            """,
            (user_id, start.isoformat(timespec="seconds")),
        ).fetchall()

    if not rows:
        return f"{title}: расходов пока нет."

    total = sum(row["total"] for row in rows)
    lines = [f"{title}: {money(total)}", ""]
    lines.extend(
        f"{row['emoji']} {row['name']}: {money(row['total'])} ({row['count']})"
        for row in rows
    )
    return "\n".join(lines)


def categories_text(user_id: int) -> str:
    categories = get_categories(user_id)
    lines = ["Твои категории:", ""]
    lines.extend(category.label for category in categories)
    lines.append("")
    lines.append("Добавить: /addcategory")
    return "\n".join(lines)


def recent_text(user_id: int) -> str:
    expenses = get_recent_expenses(user_id)

    if not expenses:
        return "Расходов пока нет."

    lines = ["Последние расходы:", ""]
    for index, expense in enumerate(expenses, start=1):
        date = expense["created_at"][5:16].replace("T", " ")
        lines.append(
            f"{index}. {expense['emoji']} {expense['title']} — "
            f"{money(expense['amount_cents'])} ({date})"
        )
    return "\n".join(lines)


def recent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Удалить последний", callback_data="delete:last")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    ensure_default_categories(message.from_user.id)
    await message.answer(
        "Пиши расход так: кофе 300\n"
        "Я спрошу категорию и сохраню в SQLite.",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Пример: кофе 300\n"
        "Можно: такси 450.50 или продукты 1200р\n\n"
        "/stats - статистика\n"
        "/recent - последние расходы\n"
        "/categories - категории\n"
        "/addcategory - добавить категорию\n"
        "/pro - подписка Pro",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    await message.answer(stats_text(message.from_user.id, "today"), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("categories"))
async def categories_command(message: Message) -> None:
    await message.answer(categories_text(message.from_user.id), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("recent"))
async def recent_command(message: Message) -> None:
    await message.answer(recent_text(message.from_user.id), reply_markup=recent_keyboard())


@router.message(Command("addcategory"))
async def add_category_command(message: Message, state: FSMContext) -> None:
    await state.set_state(CustomCategory.waiting_for_name)
    await state.update_data(pending_id=None)
    await message.answer("Напиши новую категорию. Например: 🚕 Такси")


@router.message(Command("pro"))
async def pro_command(message: Message) -> None:
    await message.answer(pro_text(message.from_user.id), reply_markup=pro_keyboard(message.from_user.id))


@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Меню", reply_markup=main_keyboard(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("stats:"))
async def stats_callback(callback: CallbackQuery) -> None:
    period = callback.data.split(":", 1)[1]
    await callback.message.edit_text(
        stats_text(callback.from_user.id, period),
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "categories")
async def categories_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        categories_text(callback.from_user.id),
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "recent")
async def recent_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(recent_text(callback.from_user.id), reply_markup=recent_keyboard())
    await callback.answer()


@router.callback_query(F.data == "delete:last")
async def delete_last_callback(callback: CallbackQuery) -> None:
    deleted = delete_last_expense(callback.from_user.id)

    if deleted is None:
        await callback.answer("Нечего удалять", show_alert=True)
        return

    await callback.message.edit_text(
        f"Удалил: {deleted['title']} — {money(deleted['amount_cents'])}",
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer("Удалено")


@router.callback_query(F.data == "add_category")
async def add_category_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CustomCategory.waiting_for_name)
    await state.update_data(pending_id=None)
    await callback.message.edit_text("Напиши новую категорию. Например: 🚕 Такси")
    await callback.answer()


@router.callback_query(F.data == "pro")
async def pro_callback(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        pro_text(callback.from_user.id),
        reply_markup=pro_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "buy_pro")
async def buy_pro_callback(callback: CallbackQuery) -> None:
    if is_pro(callback.from_user.id):
        await callback.message.edit_text(
            "Pro уже активирован навсегда.",
            reply_markup=main_keyboard(callback.from_user.id),
        )
        await callback.answer()
        return

    await callback.message.answer_invoice(
        title="Pro навсегда",
        description="Единоразовая подписка Pro в этом боте.",
        payload=f"{PRO_PAYLOAD_PREFIX}:{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Pro навсегда", amount=PRO_PRICE_STARS)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def pro_pre_checkout(query: PreCheckoutQuery) -> None:
    expected_payload = f"{PRO_PAYLOAD_PREFIX}:{query.from_user.id}"

    if query.invoice_payload != expected_payload:
        await query.answer(ok=False, error_message="Некорректный платеж.")
        return

    if is_pro(query.from_user.id):
        await query.answer(ok=False, error_message="Pro уже активирован.")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    expected_payload = f"{PRO_PAYLOAD_PREFIX}:{message.from_user.id}"

    if payment.invoice_payload != expected_payload or payment.currency != "XTR":
        await message.answer("Платеж получен, но не похож на Pro. Напиши разработчику.")
        return

    activate_pro(
        message.from_user.id,
        payment.telegram_payment_charge_id,
        payment.provider_payment_charge_id,
    )
    await message.answer(
        "Pro активирован навсегда.",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.callback_query(F.data.startswith("cat:"))
async def category_callback(callback: CallbackQuery) -> None:
    _, pending_id, category_id = callback.data.split(":")
    pending = save_expense(callback.from_user.id, int(pending_id), int(category_id))

    if pending is None:
        await callback.answer("Расход не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"Сохранил: {pending['title']} — {money(pending['amount_cents'])}",
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("custom:"))
async def custom_category_callback(callback: CallbackQuery, state: FSMContext) -> None:
    pending_id = int(callback.data.split(":", 1)[1])
    pending = get_pending_expense(callback.from_user.id, pending_id)

    if pending is None:
        await callback.answer("Расход не найден", show_alert=True)
        return

    await state.set_state(CustomCategory.waiting_for_name)
    await state.update_data(pending_id=pending_id)
    await callback.message.edit_text(
        "Напиши название категории.\n"
        "Можно с эмодзи: 🚕 Такси",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_callback(callback: CallbackQuery) -> None:
    pending_id = int(callback.data.split(":", 1)[1])
    if cancel_pending(callback.from_user.id, pending_id):
        await callback.message.edit_text("Ок, не сохраняю.", reply_markup=main_keyboard(callback.from_user.id))
    else:
        await callback.answer("Расход не найден", show_alert=True)
        return
    await callback.answer()


@router.message(CustomCategory.waiting_for_name, F.text)
async def custom_category_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    category = create_category(message.from_user.id, message.text)
    pending_id = data.get("pending_id")

    if pending_id is None:
        await state.clear()
        await message.answer(f"Добавил категорию: {category.label}", reply_markup=main_keyboard(message.from_user.id))
        return

    pending = save_expense(message.from_user.id, int(pending_id), category.id)
    await state.clear()

    if pending is None:
        await message.answer("Не нашёл расход. Напиши его заново.")
        return

    await message.answer(
        f"Сохранил: {pending['title']} — {money(pending['amount_cents'])}\n"
        f"Категория: {category.label}",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(CustomCategory.waiting_for_name)
async def custom_category_bad_message(message: Message) -> None:
    await message.answer("Напиши категорию текстом. Например: 🚕 Такси")


@router.message(F.text)
async def expense_message(message: Message) -> None:
    parsed = parse_expense(message.text)

    if parsed is None:
        await message.answer("Не понял. Напиши так: кофе 300")
        return

    title, amount_cents = parsed
    pending_id = create_pending_expense(message.from_user.id, title, amount_cents)
    await message.answer(
        f"{title} — {money(amount_cents)}\nВыбери категорию:",
        reply_markup=categories_keyboard(message.from_user.id, pending_id),
    )


async def main() -> None:
    load_env()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Добавь BOT_TOKEN в .env или переменные окружения.")

    proxy_url = os.getenv("PROXY_URL")
    init_db()
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    while True:
        session = AiohttpSession(proxy=proxy_url) if proxy_url else None
        bot = Bot(token, session=session)
        try:
            print("Bot started. Press Ctrl+C to stop.")
            await dp.start_polling(bot)
        except TelegramNetworkError as error:
            print(f"Telegram network error: {error}. Retry in 15 seconds.")
            await asyncio.sleep(15)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
