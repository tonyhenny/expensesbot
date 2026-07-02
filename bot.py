import asyncio
import calendar
import csv
import io
import json
import os
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)


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


class ProInput(StatesGroup):
    waiting_for_limit = State()
    waiting_for_search = State()


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

            CREATE TABLE IF NOT EXISTS category_learning (
                user_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                hits INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id, keyword, category_id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS monthly_limits (
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                PRIMARY KEY(user_id, category_id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                next_run_date TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS monthly_report_marks (
                user_id INTEGER NOT NULL,
                month_key TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(user_id, month_key)
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


def normalize_keyword(title: str) -> str | None:
    words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", title.lower())
    return words[0] if words else None


def insert_expense(
    user_id: int,
    title: str,
    amount_cents: int,
    category_id: int,
    created_at: str | None = None,
) -> int:
    with connect_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO expenses(user_id, title, amount_cents, category_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                title,
                amount_cents,
                category_id,
                created_at or datetime.now().isoformat(timespec="seconds"),
            ),
        )
        return int(cur.lastrowid)


def learn_category(user_id: int, title: str, category_id: int) -> None:
    keyword = normalize_keyword(title)
    if not keyword:
        return

    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO category_learning(user_id, keyword, category_id, hits)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, keyword, category_id) DO UPDATE SET
                hits = hits + 1
            """,
            (user_id, keyword, category_id),
        )


def predict_category(user_id: int, title: str) -> Category | None:
    keyword = normalize_keyword(title)
    if not keyword:
        return None

    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.emoji, c.name, l.hits
            FROM category_learning l
            JOIN categories c ON c.id = l.category_id
            WHERE l.user_id = ? AND l.keyword = ?
            ORDER BY l.hits DESC
            LIMIT 1
            """,
            (user_id, keyword),
        ).fetchone()

    if row is None or row["hits"] < 2:
        return None

    return Category(row["id"], row["emoji"], row["name"])


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

    learn_category(user_id, pending["title"], category_id)
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


def get_expense(user_id: int, expense_id: int) -> sqlite3.Row | None:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT e.id, e.title, e.amount_cents, e.category_id, e.created_at,
                   c.emoji, c.name AS category_name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ? AND e.id = ?
            """,
            (user_id, expense_id),
        ).fetchone()


def get_last_expense(user_id: int) -> sqlite3.Row | None:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT e.id, e.title, e.amount_cents, e.category_id, e.created_at,
                   c.emoji, c.name AS category_name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ?
            ORDER BY e.id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


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


def get_all_expenses(user_id: int) -> list[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT e.created_at, e.title, e.amount_cents, c.emoji, c.name AS category_name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ?
            ORDER BY e.created_at DESC, e.id DESC
            """,
            (user_id,),
        ).fetchall()


def search_expenses(user_id: int, query: str, limit: int = 20) -> list[sqlite3.Row]:
    query = query.strip()
    amount_match = re.fullmatch(r"([<>]=?)\s*(\d+(?:[.,]\d{1,2})?)", query)

    with connect_db() as conn:
        if amount_match:
            op, raw_amount = amount_match.groups()
            amount_cents = int((Decimal(raw_amount.replace(",", ".")) * 100).quantize(Decimal("1")))
            return conn.execute(
                f"""
                SELECT e.title, e.amount_cents, e.created_at, c.emoji, c.name AS category_name
                FROM expenses e
                JOIN categories c ON c.id = e.category_id
                WHERE e.user_id = ? AND e.amount_cents {op} ?
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                (user_id, amount_cents, limit),
            ).fetchall()

        like = f"%{query.lower()}%"
        return conn.execute(
            """
            SELECT e.title, e.amount_cents, e.created_at, c.emoji, c.name AS category_name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ?
              AND (LOWER(e.title) LIKE ? OR LOWER(c.name) LIKE ?)
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ?
            """,
            (user_id, like, like, limit),
        ).fetchall()


def find_category_by_name(user_id: int, name: str) -> Category | None:
    ensure_default_categories(user_id)
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT id, emoji, name
            FROM categories
            WHERE user_id = ? AND LOWER(name) = LOWER(?)
            """,
            (user_id, name.strip()),
        ).fetchone()
    return Category(row["id"], row["emoji"], row["name"]) if row else None


def set_monthly_limit(user_id: int, category_id: int, amount_cents: int) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO monthly_limits(user_id, category_id, amount_cents)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, category_id) DO UPDATE SET
                amount_cents = excluded.amount_cents
            """,
            (user_id, category_id, amount_cents),
        )


def get_limits_rows(user_id: int) -> list[sqlite3.Row]:
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT c.id, c.emoji, c.name, l.amount_cents,
                   COALESCE(SUM(e.amount_cents), 0) AS spent_cents
            FROM monthly_limits l
            JOIN categories c ON c.id = l.category_id
            LEFT JOIN expenses e ON e.category_id = l.category_id
                AND e.user_id = l.user_id
                AND e.created_at >= ?
            WHERE l.user_id = ?
            GROUP BY c.id, l.amount_cents
            ORDER BY c.name
            """,
            (month_start.isoformat(timespec="seconds"), user_id),
        ).fetchall()


def limit_warning(user_id: int, category_id: int) -> str | None:
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT c.emoji, c.name, l.amount_cents,
                   COALESCE(SUM(e.amount_cents), 0) AS spent_cents
            FROM monthly_limits l
            JOIN categories c ON c.id = l.category_id
            LEFT JOIN expenses e ON e.category_id = l.category_id
                AND e.user_id = l.user_id
                AND e.created_at >= ?
            WHERE l.user_id = ? AND l.category_id = ?
            GROUP BY c.id, l.amount_cents
            """,
            (month_start.isoformat(timespec="seconds"), user_id, category_id),
        ).fetchone()

    if row is None:
        return None

    spent = row["spent_cents"]
    limit = row["amount_cents"]
    if spent >= limit:
        return f"\n\nЛимит {row['emoji']} {row['name']} превышен: {money(spent)} из {money(limit)}."
    if spent >= int(limit * 0.8):
        return f"\n\nЛимит {row['emoji']} {row['name']} почти исчерпан: {money(spent)} из {money(limit)}."
    return None


def add_month(date_text: str) -> str:
    current = datetime.fromisoformat(date_text)
    year = current.year + (current.month // 12)
    month = 1 if current.month == 12 else current.month + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return current.replace(year=year, month=month, day=day).date().isoformat()


def next_month_date() -> str:
    return add_month(datetime.now().date().isoformat())


def create_recurring_from_expense(user_id: int, expense_id: int) -> sqlite3.Row | None:
    expense = get_expense(user_id, expense_id)
    if expense is None:
        return None

    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO recurring_expenses(
                user_id, title, amount_cents, category_id, next_run_date, is_active, created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                user_id,
                expense["title"],
                expense["amount_cents"],
                expense["category_id"],
                next_month_date(),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
    return expense


def get_recurring_rows(user_id: int) -> list[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT r.id, r.title, r.amount_cents, r.next_run_date, c.emoji, c.name AS category_name
            FROM recurring_expenses r
            JOIN categories c ON c.id = r.category_id
            WHERE r.user_id = ? AND r.is_active = 1
            ORDER BY r.next_run_date, r.id
            """,
            (user_id,),
        ).fetchall()


def stop_recurring(user_id: int, recurring_id: int) -> bool:
    with connect_db() as conn:
        cur = conn.execute(
            "UPDATE recurring_expenses SET is_active = 0 WHERE user_id = ? AND id = ?",
            (user_id, recurring_id),
        )
        return cur.rowcount > 0


def process_due_recurring(user_id: int) -> int:
    today = datetime.now().date().isoformat()
    created = 0

    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM recurring_expenses
            WHERE user_id = ? AND is_active = 1 AND next_run_date <= ?
            ORDER BY next_run_date
            """,
            (user_id, today),
        ).fetchall()

        for row in rows:
            next_run = row["next_run_date"]
            while next_run <= today:
                conn.execute(
                    """
                    INSERT INTO expenses(user_id, title, amount_cents, category_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        row["title"],
                        row["amount_cents"],
                        row["category_id"],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                created += 1
                next_run = add_month(next_run)

            conn.execute(
                "UPDATE recurring_expenses SET next_run_date = ? WHERE id = ?",
                (next_run, row["id"]),
            )

    return created


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


def parse_amount_cents(raw_amount: str) -> int | None:
    try:
        amount = Decimal(raw_amount.replace(",", "."))
    except InvalidOperation:
        return None

    if amount <= 0:
        return None

    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rows_for_export(user_id: int) -> list[dict[str, str]]:
    rows = []
    for expense in get_all_expenses(user_id):
        rows.append(
            {
                "date": expense["created_at"],
                "title": expense["title"],
                "amount": str(Decimal(expense["amount_cents"]) / Decimal(100)),
                "category": f"{expense['emoji']} {expense['category_name']}",
            }
        )
    return rows


def build_csv_export(user_id: int) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["date", "title", "amount", "category"])
    writer.writeheader()
    writer.writerows(rows_for_export(user_id))
    return output.getvalue().encode("utf-8-sig")


def build_json_export(user_id: int) -> bytes:
    return json.dumps(rows_for_export(user_id), ensure_ascii=False, indent=2).encode("utf-8")


def xlsx_cell(value: str, row: int, col: int) -> str:
    column = chr(ord("A") + col)
    return (
        f'<c r="{column}{row}" t="inlineStr">'
        f"<is><t>{escape(str(value))}</t></is>"
        "</c>"
    )


def build_xlsx_export(user_id: int) -> bytes:
    rows = [["date", "title", "amount", "category"]]
    rows.extend([list(row.values()) for row in rows_for_export(user_id)])
    sheet_rows = []

    for row_number, row in enumerate(rows, start=1):
        cells = "".join(xlsx_cell(value, row_number, col) for col, value in enumerate(row))
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Expenses" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types_xml)
        archive.writestr("_rels/.rels", rels_xml)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


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

    if user_id is not None and is_pro(user_id):
        rows.append([InlineKeyboardButton(text="⭐ Pro версия", callback_data="pro_status")])
        rows.append(
            [
                InlineKeyboardButton(text="📤 Экспорт", callback_data="export"),
                InlineKeyboardButton(text="🔎 Поиск", callback_data="search"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="💰 Лимиты", callback_data="limits"),
                InlineKeyboardButton(text="📈 Отчёт", callback_data="month_report"),
            ]
        )
        rows.append([InlineKeyboardButton(text="🔁 Повторы", callback_data="recurring")])
    elif user_id is not None:
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
        return "⭐ Pro версия\n\nПодписка активирована навсегда."

    return (
        "Pro навсегда\n\n"
        f"Цена: {PRO_PRICE_STARS} ⭐\n"
        "Откроет экспорт, лимиты, месячные отчёты, поиск и повторы."
    )


async def require_pro_message(message: Message) -> bool:
    if is_pro(message.from_user.id):
        return True

    await message.answer(pro_text(message.from_user.id), reply_markup=pro_keyboard(message.from_user.id))
    return False


async def require_pro_callback(callback: CallbackQuery) -> bool:
    if is_pro(callback.from_user.id):
        return True

    await callback.message.edit_text(pro_text(callback.from_user.id), reply_markup=pro_keyboard(callback.from_user.id))
    await callback.answer()
    return False


def export_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="CSV", callback_data="export:csv"),
                InlineKeyboardButton(text="XLSX", callback_data="export:xlsx"),
                InlineKeyboardButton(text="JSON", callback_data="export:json"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def success_keyboard(user_id: int, expense_id: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    if expense_id is not None and is_pro(user_id):
        rows.append([InlineKeyboardButton(text="🔁 Повторять каждый месяц", callback_data=f"repeat:add:{expense_id}")])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def limits_text(user_id: int) -> str:
    rows = get_limits_rows(user_id)
    if not rows:
        return "Лимитов пока нет.\n\nДобавить: /limit Категория сумма\nПример: /limit Кофе 3000"

    lines = ["Лимиты на этот месяц:", ""]
    for row in rows:
        percent = int(row["spent_cents"] * 100 / row["amount_cents"]) if row["amount_cents"] else 0
        lines.append(
            f"{row['emoji']} {row['name']}: {money(row['spent_cents'])} из {money(row['amount_cents'])} ({percent}%)"
        )
    lines.append("")
    lines.append("Изменить: /limit Категория сумма")
    return "\n".join(lines)


def set_limit_from_text(user_id: int, text: str) -> str:
    match = re.match(r"^\s*(?P<category>.+?)\s+(?P<amount>\d+(?:[.,]\d{1,2})?)\s*$", text)
    if not match:
        return "Напиши так: /limit Кофе 3000"

    category = find_category_by_name(user_id, match.group("category"))
    if category is None:
        return "Не нашёл категорию. Посмотри список: /categories"

    amount_cents = parse_amount_cents(match.group("amount"))
    if amount_cents is None:
        return "Сумма должна быть больше нуля."

    set_monthly_limit(user_id, category.id, amount_cents)
    return f"Лимит сохранён: {category.label} — {money(amount_cents)} в месяц."


def search_text(user_id: int, query: str) -> str:
    rows = search_expenses(user_id, query)
    if not rows:
        return "Ничего не нашёл."

    lines = [f"Поиск: {query}", ""]
    for row in rows:
        date = row["created_at"][5:16].replace("T", " ")
        lines.append(f"{row['emoji']} {row['title']} — {money(row['amount_cents'])} ({date})")
    return "\n".join(lines)


def recurring_text(user_id: int) -> str:
    rows = get_recurring_rows(user_id)
    if not rows:
        return "Повторов пока нет.\n\nЧтобы добавить повтор, сохрани расход и нажми «Повторять каждый месяц»."

    lines = ["Повторяющиеся расходы:", ""]
    for row in rows:
        lines.append(
            f"#{row['id']} {row['emoji']} {row['title']} — {money(row['amount_cents'])}, следующий: {row['next_run_date']}"
        )
    lines.append("")
    lines.append("Остановить: /stoprepeat номер")
    return "\n".join(lines)


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = datetime(year, month, 1)
    next_year = year + (month // 12)
    next_month = 1 if month == 12 else month + 1
    end = datetime(next_year, next_month, 1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def monthly_report_text(user_id: int, year: int | None = None, month: int | None = None) -> str:
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    start, end = month_bounds(year, month)

    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT c.emoji, c.name, SUM(e.amount_cents) AS total, COUNT(*) AS count
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ? AND e.created_at >= ? AND e.created_at < ?
            GROUP BY c.id
            ORDER BY total DESC
            """,
            (user_id, start, end),
        ).fetchall()
        biggest = conn.execute(
            """
            SELECT e.title, e.amount_cents, c.emoji, c.name
            FROM expenses e
            JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ? AND e.created_at >= ? AND e.created_at < ?
            ORDER BY e.amount_cents DESC
            LIMIT 1
            """,
            (user_id, start, end),
        ).fetchone()

    if not rows:
        return "За этот месяц расходов пока нет."

    total = sum(row["total"] for row in rows)
    lines = [f"Отчёт за {month:02d}.{year}: {money(total)}", ""]
    lines.extend(f"{row['emoji']} {row['name']}: {money(row['total'])} ({row['count']})" for row in rows)
    if biggest:
        lines.append("")
        lines.append(f"Самая крупная трата: {biggest['emoji']} {biggest['title']} — {money(biggest['amount_cents'])}")
    return "\n".join(lines)


def previous_month() -> tuple[int, int]:
    now = datetime.now()
    if now.month == 1:
        return now.year - 1, 12
    return now.year, now.month - 1


def should_send_monthly_report(user_id: int) -> tuple[str, str] | None:
    if not is_pro(user_id):
        return None

    year, month = previous_month()
    month_key = f"{year}-{month:02d}"
    text = monthly_report_text(user_id, year, month)
    if "расходов пока нет" in text:
        return None

    with connect_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM monthly_report_marks WHERE user_id = ? AND month_key = ?",
            (user_id, month_key),
        ).fetchone()
        if exists:
            return None

        conn.execute(
            "INSERT INTO monthly_report_marks(user_id, month_key, sent_at) VALUES (?, ?, ?)",
            (user_id, month_key, datetime.now().isoformat(timespec="seconds")),
        )

    return month_key, text


async def run_user_automations(message: Message) -> None:
    if is_pro(message.from_user.id):
        created = process_due_recurring(message.from_user.id)
        if created:
            await message.answer(f"Добавил повторяющиеся расходы: {created}")

    report = should_send_monthly_report(message.from_user.id)
    if report:
        _, text = report
        await message.answer(text)


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
    await run_user_automations(message)
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
        "/export - экспорт Pro\n"
        "/limits - лимиты Pro\n"
        "/search - поиск Pro\n"
        "/month - отчёт Pro\n"
        "/recurring - повторы Pro\n"
        "/stoprepeat - остановить повтор Pro\n"
        "/pro - подписка Pro",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    await run_user_automations(message)
    await message.answer(stats_text(message.from_user.id, "today"), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("categories"))
async def categories_command(message: Message) -> None:
    await message.answer(categories_text(message.from_user.id), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("recent"))
async def recent_command(message: Message) -> None:
    await run_user_automations(message)
    await message.answer(recent_text(message.from_user.id), reply_markup=recent_keyboard())


@router.message(Command("addcategory"))
async def add_category_command(message: Message, state: FSMContext) -> None:
    await state.set_state(CustomCategory.waiting_for_name)
    await state.update_data(pending_id=None)
    await message.answer("Напиши новую категорию. Например: 🚕 Такси")


@router.message(Command("pro"))
async def pro_command(message: Message) -> None:
    await message.answer(pro_text(message.from_user.id), reply_markup=pro_keyboard(message.from_user.id))


@router.message(Command("export"))
async def export_command(message: Message) -> None:
    if not await require_pro_message(message):
        return

    await message.answer("Выбери формат экспорта:", reply_markup=export_keyboard())


@router.message(Command("limits"))
async def limits_command(message: Message) -> None:
    if not await require_pro_message(message):
        return

    await message.answer(limits_text(message.from_user.id), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("limit"))
async def limit_command(message: Message) -> None:
    if not await require_pro_message(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        await message.answer("Напиши так: /limit Кофе 3000")
        return

    await message.answer(set_limit_from_text(message.from_user.id, parts[1]), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("search"))
async def search_command(message: Message, state: FSMContext) -> None:
    if not await require_pro_message(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        await state.set_state(ProInput.waiting_for_search)
        await message.answer("Что ищем? Например: кофе или >1000")
        return

    await message.answer(search_text(message.from_user.id, parts[1]), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("month"))
async def month_command(message: Message) -> None:
    if not await require_pro_message(message):
        return

    await message.answer(monthly_report_text(message.from_user.id), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("recurring"))
async def recurring_command(message: Message) -> None:
    if not await require_pro_message(message):
        return

    await message.answer(recurring_text(message.from_user.id), reply_markup=main_keyboard(message.from_user.id))


@router.message(Command("stoprepeat"))
async def stoprepeat_command(message: Message) -> None:
    if not await require_pro_message(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) == 1 or not parts[1].strip().isdigit():
        await message.answer("Напиши так: /stoprepeat 1")
        return

    if stop_recurring(message.from_user.id, int(parts[1])):
        await message.answer("Повтор остановлен.", reply_markup=main_keyboard(message.from_user.id))
    else:
        await message.answer("Не нашёл такой повтор.", reply_markup=main_keyboard(message.from_user.id))


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


@router.callback_query(F.data == "pro_status")
async def pro_status_callback(callback: CallbackQuery) -> None:
    await callback.answer("Pro версия активна", show_alert=True)


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


@router.callback_query(F.data == "export")
async def export_callback(callback: CallbackQuery) -> None:
    if not await require_pro_callback(callback):
        return

    await callback.message.edit_text("Выбери формат экспорта:", reply_markup=export_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("export:"))
async def export_format_callback(callback: CallbackQuery) -> None:
    if not await require_pro_callback(callback):
        return

    export_format = callback.data.split(":", 1)[1]
    builders = {
        "csv": ("expenses.csv", build_csv_export),
        "json": ("expenses.json", build_json_export),
        "xlsx": ("expenses.xlsx", build_xlsx_export),
    }
    filename, builder = builders[export_format]
    data = builder(callback.from_user.id)
    await callback.message.answer_document(BufferedInputFile(data, filename=filename))
    await callback.answer("Готово")


@router.callback_query(F.data == "limits")
async def limits_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_pro_callback(callback):
        return

    await state.set_state(ProInput.waiting_for_limit)
    await callback.message.edit_text(
        limits_text(callback.from_user.id) + "\n\nЧтобы добавить лимит, напиши: Категория сумма",
    )
    await callback.answer()


@router.callback_query(F.data == "search")
async def search_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_pro_callback(callback):
        return

    await state.set_state(ProInput.waiting_for_search)
    await callback.message.edit_text("Что ищем? Например: кофе или >1000")
    await callback.answer()


@router.callback_query(F.data == "month_report")
async def month_report_callback(callback: CallbackQuery) -> None:
    if not await require_pro_callback(callback):
        return

    await callback.message.edit_text(
        monthly_report_text(callback.from_user.id),
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer()


@router.callback_query(F.data == "recurring")
async def recurring_callback(callback: CallbackQuery) -> None:
    if not await require_pro_callback(callback):
        return

    await callback.message.edit_text(recurring_text(callback.from_user.id), reply_markup=main_keyboard(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data.startswith("repeat:add:"))
async def repeat_add_callback(callback: CallbackQuery) -> None:
    if not await require_pro_callback(callback):
        return

    expense_id = int(callback.data.rsplit(":", 1)[1])
    expense = create_recurring_from_expense(callback.from_user.id, expense_id)
    if expense is None:
        await callback.answer("Расход не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"Добавил повтор: {expense['title']} — {money(expense['amount_cents'])} каждый месяц.",
        reply_markup=main_keyboard(callback.from_user.id),
    )
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("cat:"))
async def category_callback(callback: CallbackQuery) -> None:
    _, pending_id, category_id = callback.data.split(":")
    category_id_int = int(category_id)
    pending = save_expense(callback.from_user.id, int(pending_id), category_id_int)

    if pending is None:
        await callback.answer("Расход не найден", show_alert=True)
        return

    expense = get_last_expense(callback.from_user.id)
    warning = limit_warning(callback.from_user.id, category_id_int) if is_pro(callback.from_user.id) else None
    await callback.message.edit_text(
        f"Сохранил: {pending['title']} — {money(pending['amount_cents'])}{warning or ''}",
        reply_markup=success_keyboard(callback.from_user.id, expense["id"] if expense else None),
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


@router.message(ProInput.waiting_for_limit, F.text)
async def limit_text_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        set_limit_from_text(message.from_user.id, message.text),
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.message(ProInput.waiting_for_search, F.text)
async def search_text_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(search_text(message.from_user.id, message.text), reply_markup=main_keyboard(message.from_user.id))


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

    expense = get_last_expense(message.from_user.id)
    warning = limit_warning(message.from_user.id, category.id) if is_pro(message.from_user.id) else None
    await message.answer(
        f"Сохранил: {pending['title']} — {money(pending['amount_cents'])}\n"
        f"Категория: {category.label}{warning or ''}",
        reply_markup=success_keyboard(message.from_user.id, expense["id"] if expense else None),
    )


@router.message(CustomCategory.waiting_for_name)
async def custom_category_bad_message(message: Message) -> None:
    await message.answer("Напиши категорию текстом. Например: 🚕 Такси")


@router.message(F.text)
async def expense_message(message: Message) -> None:
    await run_user_automations(message)
    parsed = parse_expense(message.text)

    if parsed is None:
        await message.answer("Не понял. Напиши так: кофе 300")
        return

    title, amount_cents = parsed
    predicted = predict_category(message.from_user.id, title)

    if predicted is not None:
        expense_id = insert_expense(message.from_user.id, title, amount_cents, predicted.id)
        learn_category(message.from_user.id, title, predicted.id)
        warning = limit_warning(message.from_user.id, predicted.id) if is_pro(message.from_user.id) else None
        await message.answer(
            f"Сохранил автоматически: {title} — {money(amount_cents)}\n"
            f"Категория: {predicted.label}{warning or ''}",
            reply_markup=success_keyboard(message.from_user.id, expense_id),
        )
        return

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
