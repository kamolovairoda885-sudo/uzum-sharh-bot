import asyncio
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
EMPLOYEE_PHONE = os.getenv("EMPLOYEE_PHONE", "+998 XX XXX XX XX")

UZUM_AUTHORIZATION = os.getenv("UZUM_AUTHORIZATION")
UZUM_COOKIE = os.getenv("UZUM_COOKIE")

PAYMENT_CARD = os.getenv("PAYMENT_CARD", "0000 0000 0000 0000")
PAYMENT_OWNER = os.getenv("PAYMENT_OWNER", "Ism Familiya")

SUBSCRIPTION_PRICE = int(os.getenv("SUBSCRIPTION_PRICE", "20000"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "review_bot.db"
UZ_TZ = timezone(timedelta(hours=5))

pending_payments = {}
pending_reviews = {}


# =========================
# DATABASE
# =========================

def now_dt():
    return datetime.now(UZ_TZ)


def now_text():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")


def db():
    return sqlite3.connect(DB_NAME)


def ensure_column(cur, table, column, column_type):
    cur.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]

    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            language TEXT DEFAULT 'uz',
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            store_id TEXT,
            store_name TEXT,
            subscription_until TEXT,
            auto_reply_enabled INTEGER DEFAULT 0,
            reply_mode TEXT DEFAULT 'template_low_confirm',
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS review_logs (
            review_id TEXT PRIMARY KEY,
            telegram_id INTEGER,
            store_id TEXT,
            rating INTEGER,
            status TEXT,
            reply_text TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            telegram_id INTEGER,
            store_row_id INTEGER,
            amount INTEGER,
            days INTEGER,
            status TEXT,
            created_at TEXT,
            approved_at TEXT
        )
    """)

    ensure_column(cur, "users", "language", "TEXT DEFAULT 'uz'")
    ensure_column(cur, "users", "username", "TEXT")
    ensure_column(cur, "users", "is_blocked", "INTEGER DEFAULT 0")
    ensure_column(cur, "users", "created_at", "TEXT")

    ensure_column(cur, "stores", "subscription_until", "TEXT")
    ensure_column(cur, "stores", "auto_reply_enabled", "INTEGER DEFAULT 0")
    ensure_column(cur, "stores", "reply_mode", "TEXT DEFAULT 'template_low_confirm'")

    for rating in range(1, 6):
        ensure_column(cur, "stores", f"template_{rating}_uz", "TEXT")
        ensure_column(cur, "stores", f"template_{rating}_ru", "TEXT")

    conn.commit()
    conn.close()


def add_user(user):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (user.id,))
    exists = cur.fetchone()

    if exists:
        cur.execute(
            "UPDATE users SET full_name = ?, username = ? WHERE telegram_id = ?",
            (user.full_name or "", user.username or "", user.id)
        )
    else:
        cur.execute(
            """
            INSERT INTO users (telegram_id, full_name, username, language, is_blocked, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user.id, user.full_name or "", user.username or "", "uz", 0, now_text())
        )

    conn.commit()
    conn.close()


def set_user_language(telegram_id, language):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET language = ? WHERE telegram_id = ?",
        (language, telegram_id)
    )
    conn.commit()
    conn.close()


def get_user_language(telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT language FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()

    if row and row[0] in ["uz", "ru"]:
        return row[0]

    return "uz"


def is_blocked(telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT is_blocked FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] == 1)


def save_store(telegram_id, store_id, store_name):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT id FROM stores WHERE telegram_id = ? AND store_id = ?",
        (telegram_id, store_id)
    )
    exists = cur.fetchone()

    if exists:
        cur.execute(
            "UPDATE stores SET store_name = ? WHERE telegram_id = ? AND store_id = ?",
            (store_name, telegram_id, store_id)
        )
    else:
        cur.execute(
            """
            INSERT INTO stores (
                telegram_id, store_id, store_name, subscription_until,
                auto_reply_enabled, reply_mode, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                store_id,
                store_name,
                None,
                0,
                "template_low_confirm",
                now_text()
            )
        )

    conn.commit()
    conn.close()


def store_used_by_other_user(store_id, telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_id FROM stores
        WHERE store_id = ? AND telegram_id != ?
        LIMIT 1
        """,
        (store_id, telegram_id)
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_user_stores(telegram_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, store_id, store_name, subscription_until,
               auto_reply_enabled, reply_mode
        FROM stores
        WHERE telegram_id = ?
        ORDER BY id DESC
        """,
        (telegram_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_store(row_id, telegram_id=None):
    conn = db()
    cur = conn.cursor()

    if telegram_id:
        cur.execute(
            """
            SELECT id, telegram_id, store_id, store_name, subscription_until,
                   auto_reply_enabled, reply_mode
            FROM stores
            WHERE id = ? AND telegram_id = ?
            """,
            (row_id, telegram_id)
        )
    else:
        cur.execute(
            """
            SELECT id, telegram_id, store_id, store_name, subscription_until,
                   auto_reply_enabled, reply_mode
            FROM stores
            WHERE id = ?
            """,
            (row_id,)
        )

    row = cur.fetchone()
    conn.close()
    return row


def get_store_by_shop_id(store_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, telegram_id, store_id, store_name, subscription_until,
               auto_reply_enabled, reply_mode
        FROM stores
        WHERE store_id = ?
        LIMIT 1
        """,
        (str(store_id),)
    )
    row = cur.fetchone()
    conn.close()
    return row


def update_store_name(row_id, store_name):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stores SET store_name = ? WHERE id = ?", (store_name, row_id))
    conn.commit()
    conn.close()


def update_auto_reply(row_id, enabled):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE stores SET auto_reply_enabled = ? WHERE id = ?",
        (1 if enabled else 0, row_id)
    )
    conn.commit()
    conn.close()


def update_reply_mode(row_id, mode):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stores SET reply_mode = ? WHERE id = ?", (mode, row_id))
    conn.commit()
    conn.close()


def update_template(row_id, rating, lang, text):
    if lang not in ["uz", "ru"]:
        lang = "uz"

    conn = db()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE stores SET template_{rating}_{lang} = ? WHERE id = ?",
        (text, row_id)
    )
    conn.commit()
    conn.close()


def get_template(row_id, rating, lang):
    if lang not in ["uz", "ru"]:
        lang = "uz"

    conn = db()
    cur = conn.cursor()
    cur.execute(
        f"SELECT template_{rating}_{lang} FROM stores WHERE id = ?",
        (row_id,)
    )
    row = cur.fetchone()
    conn.close()

    if row and row[0]:
        return row[0]

    return None


def extend_subscription(row_id):
    store = get_store(row_id)
    if not store:
        return None

    subscription_until = store[4]

    if subscription_until:
        try:
            old_until = datetime.fromisoformat(subscription_until)
        except Exception:
            old_until = now_dt()
    else:
        old_until = now_dt()

    start = old_until if old_until > now_dt() else now_dt()
    new_until = start + timedelta(days=SUBSCRIPTION_DAYS)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE stores SET subscription_until = ? WHERE id = ?",
        (new_until.isoformat(), row_id)
    )
    conn.commit()
    conn.close()

    return new_until


def subscription_active(subscription_until):
    if not subscription_until:
        return False

    try:
        until = datetime.fromisoformat(subscription_until)
        return until > now_dt()
    except Exception:
        return False


def log_review(review_id, telegram_id, store_id, rating, status, reply_text=""):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO review_logs
        (review_id, telegram_id, store_id, rating, status, reply_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (str(review_id), telegram_id, str(store_id), rating, status, reply_text, now_text())
    )
    conn.commit()
    conn.close()


def review_already_logged(review_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT review_id FROM review_logs WHERE review_id = ?", (str(review_id),))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def save_payment(payment_id, telegram_id, store_row_id, amount, days, status):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments
        (payment_id, telegram_id, store_row_id, amount, days, status, created_at, approved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (payment_id, telegram_id, store_row_id, amount, days, status, now_text(), None)
    )
    conn.commit()
    conn.close()


def update_payment_status(payment_id, status):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE payments SET status = ?, approved_at = ? WHERE payment_id = ?",
        (status, now_text(), payment_id)
    )
    conn.commit()
    conn.close()


def get_stats():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM stores")
    stores_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM stores WHERE auto_reply_enabled = 1")
    active_auto = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM review_logs WHERE status = 'sent'")
    sent_count = cur.fetchone()[0]

    conn.close()
    return users_count, stores_count, active_auto, sent_count


# =========================
# UZUM API
# =========================

def uzum_headers():
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://seller.uzum.uz",
        "Referer": "https://seller.uzum.uz/",
    }

    if UZUM_AUTHORIZATION:
        headers["Authorization"] = UZUM_AUTHORIZATION

    if UZUM_COOKIE:
        headers["Cookie"] = UZUM_COOKIE

    return headers


async def read_response(response):
    try:
        return await response.json(content_type=None)
    except Exception:
        text = await response.text()
        return {"raw": text[:1000]}


async def uzum_get_no_reply_reviews(page=0, size=20):
    url = f"https://api-seller.uzum.uz/api/seller/product-reviews?page={page}&size={size}"
    payload = {"filter": "NO_REPLY"}

    async with aiohttp.ClientSession(headers=uzum_headers()) as session:
        async with session.post(url, json=payload) as response:
            data = await read_response(response)

            if response.status != 200:
                raise Exception(f"Sharhlar ro‘yxatini olishda xato: {response.status}. Javob: {data}")

            return data.get("payload", [])


async def uzum_get_review_detail(review_id):
    url = f"https://api-seller.uzum.uz/api/seller/product-reviews/review/{review_id}"

    async with aiohttp.ClientSession(headers=uzum_headers()) as session:
        async with session.get(url) as response:
            data = await read_response(response)

            if response.status != 200:
                raise Exception(f"Sharh tafsilotini olishda xato: {response.status}. Javob: {data}")

            if isinstance(data, dict) and isinstance(data.get("payload"), dict):
                return data["payload"]

            return data


async def uzum_reply_review(review_id, content):
    url = "https://api-seller.uzum.uz/api/seller/product-reviews/reply/create"

    payload = [
        {
            "reviewId": int(review_id),
            "content": content
        }
    ]

    async with aiohttp.ClientSession(headers=uzum_headers()) as session:
        async with session.post(url, json=payload) as response:
            data = await read_response(response)

            if response.status != 200:
                raise Exception(f"Sharhga javob yuborishda xato: {response.status}. Javob: {data}")

            return data


# =========================
# REVIEW HELPERS
# =========================

def extract_review_id(review):
    if not isinstance(review, dict):
        return None

    return (
        review.get("reviewId")
        or review.get("id")
        or review.get("review", {}).get("id")
    )


def extract_rating(review):
    try:
        return int(review.get("rating") or 5)
    except Exception:
        return 5


def extract_shop_id(review):
    shop = review.get("shop") or {}
    return shop.get("id") or review.get("shopId")


def extract_shop_title(review):
    shop = review.get("shop") or {}
    return shop.get("title") or shop.get("name")


def extract_product_name(review):
    product = review.get("product") or {}
    return (
        product.get("title")
        or product.get("name")
        or product.get("productTitle")
        or "mahsulot"
    )


def extract_review_text(review):
    parts = []

    for key in ["content", "text", "comment", "reviewText", "message"]:
        if review.get(key):
            parts.append(str(review.get(key)))

    if review.get("pros"):
        parts.append("Afzalliklari: " + str(review.get("pros")))

    if review.get("cons"):
        parts.append("Kamchiliklari: " + str(review.get("cons")))

    if review.get("commentText"):
        parts.append(str(review.get("commentText")))

    return "\n".join(parts).strip() or "Matnsiz sharh"


def detect_review_language(text):
    text = (text or "").lower()

    russian_letters = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    ru_count = sum(1 for ch in text if ch in russian_letters)

    if ru_count >= 2:
        return "ru"

    return "uz"


def default_template(rating, lang="uz"):
    if lang == "ru":
        if rating >= 5:
            return "Спасибо за покупку и тёплый отзыв! Мы рады, что товар вам понравился 😊"
        if rating == 4:
            return "Спасибо за ваш отзыв! Мы обязательно учтём ваши пожелания и постараемся стать ещё лучше."
        if rating == 3:
            return "Спасибо за обратную связь. Мы обязательно учтём ваши замечания и постараемся улучшить качество обслуживания."
        if rating == 2:
            return "Спасибо за отзыв. Приносим извинения за неудобства, мы обязательно учтём ваши замечания."
        return "Спасибо за обратную связь. Приносим извинения за неудобства, мы обязательно примем ваш отзыв во внимание."

    if rating >= 5:
        return "Xaridingiz va iliq fikringiz uchun rahmat! Sizga xizmat qilishdan mamnunmiz 😊"
    if rating == 4:
        return "Fikringiz uchun rahmat! Keyingi safar yanada yaxshiroq xizmat ko‘rsatishga harakat qilamiz."
    if rating == 3:
        return "Fikringiz uchun rahmat. Bildirgan mulohazalaringizni albatta inobatga olamiz."
    if rating == 2:
        return "Fikringiz uchun rahmat. Noqulaylik uchun uzr so‘raymiz, kamchiliklarni bartaraf etishga harakat qilamiz."
    return "Fikringiz uchun rahmat. Noqulaylik uchun uzr so‘raymiz. Holatni albatta inobatga olamiz."


def apply_variables(template, review, store_name):
    rating = extract_rating(review)
    product = extract_product_name(review)

    return (
        template
        .replace("{product}", product)
        .replace("{rating}", str(rating))
        .replace("{shop}", store_name)
    )


def template_for_store(store, review):
    row_id = store[0]
    store_name = store[3]

    rating = extract_rating(review)
    review_text = extract_review_text(review)
    lang = detect_review_language(review_text)

    template = get_template(row_id, rating, lang)

    if not template:
        template = default_template(rating, lang)

    return apply_variables(template, review, store_name)


async def ai_generate_reply(review, store_name):
    rating = extract_rating(review)
    text = extract_review_text(review)
    product = extract_product_name(review)
    lang = detect_review_language(text)

    if not OPENAI_API_KEY:
        return default_template(rating, lang)

    prompt = (
        "Siz Uzum Market seller do‘koni nomidan xaridor sharhiga javob yozasiz.\n"
        "Sharh qaysi tilda yozilgan bo‘lsa, javobni ham shu tilda yozing.\n"
        "Agar sharh rus tilida bo‘lsa, javob rus tilida bo‘lsin.\n"
        "Agar sharh o‘zbek tilida bo‘lsa, javob o‘zbek tilida bo‘lsin.\n"
        "Javob muloyim, qisqa va professional bo‘lsin.\n"
        "Juda uzun yozmang. Emoji ko‘p ishlatmang.\n"
        "Agar sharh salbiy bo‘lsa, uzr so‘rang va mulohaza inobatga olinishini ayting.\n\n"
        f"Do‘kon: {store_name}\n"
        f"Mahsulot: {product}\n"
        f"Baholash: {rating} yulduz\n"
        f"Sharh: {text}\n"
    )

    url = "https://api.openai.com/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "Siz mijoz sharhlariga sotuvchi nomidan javob yozadigan yordamchisiz."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.5,
        "max_tokens": 180
    }

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as response:
                data = await read_response(response)

                if response.status != 200:
                    return default_template(rating, lang)

                return data["choices"][0]["message"]["content"].strip()

    except Exception:
        return default_template(rating, lang)


async def build_reply_text(store, review):
    mode = store[6]

    if mode.startswith("ai"):
        return await ai_generate_reply(review, store[3])

    return template_for_store(store, review)


def needs_confirmation(mode, rating):
    if mode.endswith("_confirm") and mode not in ["template_low_confirm", "ai_low_confirm"]:
        return True

    if mode in ["template_low_confirm", "ai_low_confirm"] and rating in [1, 2]:
        return True

    return False


# =========================
# TEXTS AND KEYBOARDS
# =========================

START_CHOOSE_LANG = (
    "🌐 Tilni tanlang / Выберите язык\n\n"
    "Botdan foydalanishni boshlash uchun tilni tanlang."
)

UZ_INSTRUCTION = (
    "⭐ Uzum Sharh Bot\n\n"
    "Bu bot Uzum Seller’dagi sharhlarga avtomatik javob berishga yordam beradi.\n\n"
    "📌 Qanday ishlaydi?\n\n"
    "1️⃣ Avval do‘koningizni botga qo‘shasiz.\n"
    "2️⃣ Har bir do‘kon uchun 30 kunlik obuna faollashtirasiz.\n"
    "3️⃣ Har xil yulduzli sharhlar uchun o‘zbekcha va ruscha shablon yozasiz.\n"
    "4️⃣ Javob rejimini tanlaysiz.\n"
    "5️⃣ Avto-javobni yoqasiz.\n"
    "6️⃣ Bot javobsiz sharhlarni tekshiradi va ratingga qarab javob yuboradi.\n\n"
    "✅ 5⭐, 4⭐, 3⭐ sharhlarga avtomatik javob berish mumkin.\n"
    "⚠️ 1⭐ va 2⭐ sharhlarni avval tasdiqlab yuboradigan qilish mumkin.\n"
    "🤖 Xohlasangiz AI javob yozadigan rejimni ham yoqishingiz mumkin.\n\n"
    "💰 Obuna narxi:\n"
    "1 ta do‘kon uchun 30 kun — 20 000 so‘m\n\n"
    "Boshlash uchun pastdagi menyudan “➕ Do‘kon qo‘shish” tugmasini bosing."
)

RU_INSTRUCTION = (
    "⭐ Uzum Review Bot\n\n"
    "Этот бот помогает автоматически отвечать на отзывы в Uzum Seller.\n\n"
    "📌 Как это работает?\n\n"
    "1️⃣ Сначала вы добавляете свой магазин в бот.\n"
    "2️⃣ Активируете подписку на 30 дней для каждого магазина.\n"
    "3️⃣ Настраиваете шаблоны ответов на узбекском и русском языке.\n"
    "4️⃣ Выбираете режим ответа.\n"
    "5️⃣ Включаете автоответ.\n"
    "6️⃣ Бот проверяет отзывы без ответа и отправляет ответ по рейтингу.\n\n"
    "✅ На отзывы 5⭐, 4⭐, 3⭐ можно отвечать автоматически.\n"
    "⚠️ Отзывы 1⭐ и 2⭐ можно отправлять только после вашего подтверждения.\n"
    "🤖 Также можно включить режим AI-ответов.\n\n"
    "💰 Стоимость подписки:\n"
    "1 магазин на 30 дней — 20 000 сум\n\n"
    "Чтобы начать, нажмите кнопку “➕ Добавить магазин” в меню ниже."
)


def language_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🇺🇿 O‘zbekcha", callback_data="set_lang:uz")
    kb.button(text="🇷🇺 Русский", callback_data="set_lang:ru")
    kb.adjust(1)
    return kb.as_markup()


def main_menu(lang="uz"):
    if lang == "ru":
        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text="🏬 Мои магазины"),
                    KeyboardButton(text="➕ Добавить магазин"),
                ],
                [
                    KeyboardButton(text="💳 Подписка"),
                    KeyboardButton(text="🔍 Отзывы без ответа"),
                ],
                [
                    KeyboardButton(text="▶️ Включить автоответ"),
                    KeyboardButton(text="⏸ Остановить автоответ"),
                ],
                [
                    KeyboardButton(text="⚙️ Режим ответа"),
                    KeyboardButton(text="📝 Шаблоны"),
                ],
                [
                    KeyboardButton(text="📊 Статистика"),
                    KeyboardButton(text="🌐 Изменить язык"),
                ],
            ],
            resize_keyboard=True
        )

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🏬 Do‘konlarim"),
                KeyboardButton(text="➕ Do‘kon qo‘shish"),
            ],
            [
                KeyboardButton(text="💳 Obuna"),
                KeyboardButton(text="🔍 Javobsiz sharhlar"),
            ],
            [
                KeyboardButton(text="▶️ Avto-javobni yoqish"),
                KeyboardButton(text="⏸ Avto-javobni to‘xtatish"),
            ],
            [
                KeyboardButton(text="⚙️ Javob rejimi"),
                KeyboardButton(text="📝 Shablonlar"),
            ],
            [
                KeyboardButton(text="📊 Statistika"),
                KeyboardButton(text="🌐 Tilni o‘zgartirish"),
            ],
        ],
        resize_keyboard=True
    )


def user_menu(user_id):
    return main_menu(get_user_language(user_id))


def stores_keyboard(stores, prefix):
    kb = InlineKeyboardBuilder()

    for row in stores:
        row_id, store_id, store_name, subscription_until, auto_enabled, mode = row
        sub = "✅" if subscription_active(subscription_until) else "❌"
        auto = "🟢" if auto_enabled else "⚪"
        kb.button(
            text=f"{sub} {auto} {store_name}",
            callback_data=f"{prefix}:{row_id}"
        )

    kb.adjust(1)
    return kb.as_markup()


def mode_keyboard(row_id):
    kb = InlineKeyboardBuilder()

    kb.button(text="✍️ Shablon + avtomatik", callback_data=f"set_mode:{row_id}:template_auto")
    kb.button(text="⚠️ Shablon + 1-2⭐ tasdiqlash", callback_data=f"set_mode:{row_id}:template_low_confirm")
    kb.button(text="👀 Shablon + hammasini tasdiqlash", callback_data=f"set_mode:{row_id}:template_confirm")

    kb.button(text="🤖 AI + avtomatik", callback_data=f"set_mode:{row_id}:ai_auto")
    kb.button(text="⚠️ AI + 1-2⭐ tasdiqlash", callback_data=f"set_mode:{row_id}:ai_low_confirm")
    kb.button(text="🤖 AI + hammasini tasdiqlash", callback_data=f"set_mode:{row_id}:ai_confirm")

    kb.adjust(1)
    return kb.as_markup()


def payment_admin_keyboard(payment_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Tasdiqlash", callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Rad etish", callback_data=f"pay_no:{payment_id}")
    kb.adjust(2)
    return kb.as_markup()


def review_confirm_keyboard(pending_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yuborish", callback_data=f"review_send:{pending_id}")
    kb.button(text="✏️ O‘zgartirish", callback_data=f"review_edit:{pending_id}")
    kb.button(text="❌ Yubormaslik", callback_data=f"review_skip:{pending_id}")
    kb.adjust(1)
    return kb.as_markup()


def template_question_text(store_name, rating, lang):
    if lang == "ru":
        lang_text = "ruscha"
        example = "Спасибо за покупку! Будем рады видеть вас снова 😊"
    else:
        lang_text = "o‘zbekcha"
        example = "Xaridingiz uchun rahmat! Sizga xizmat qilishdan mamnunmiz 😊"

    return (
        f"📝 Shablon sozlash\n\n"
        f"🏪 Do‘kon: {store_name}\n\n"
        f"⭐ {rating} yulduzli sharhlar uchun {lang_text} javob yozing.\n\n"
        f"Masalan:\n"
        f"{example}\n\n"
        f"O‘zgaruvchilar:\n"
        f"{{product}} — mahsulot nomi\n"
        f"{{rating}} — yulduz soni\n"
        f"{{shop}} — do‘kon nomi"
    )


# =========================
# STATES
# =========================

class AddStoreState(StatesGroup):
    waiting_store_id = State()


class PaymentState(StatesGroup):
    waiting_receipt = State()


class TemplateState(StatesGroup):
    waiting_text = State()


class EditReviewState(StatesGroup):
    waiting_text = State()


# =========================
# START
# =========================

@dp.message(CommandStart())
async def start(message: Message):
    add_user(message.from_user)

    if is_blocked(message.from_user.id):
        await message.answer("Siz botdan foydalanishdan bloklangansiz.")
        return

    await message.answer(
        START_CHOOSE_LANG,
        reply_markup=language_keyboard()
    )


@dp.callback_query(F.data.startswith("set_lang:"))
async def set_language(callback: CallbackQuery):
    lang = callback.data.split(":")[1]

    if lang not in ["uz", "ru"]:
        lang = "uz"

    add_user(callback.from_user)
    set_user_language(callback.from_user.id, lang)

    if lang == "ru":
        await callback.message.answer(
            RU_INSTRUCTION,
            reply_markup=main_menu("ru")
        )
    else:
        await callback.message.answer(
            UZ_INSTRUCTION,
            reply_markup=main_menu("uz")
        )

    await callback.answer()


@dp.message(F.text.in_(["🌐 Tilni o‘zgartirish", "🌐 Изменить язык"]))
async def change_language(message: Message):
    await message.answer(
        START_CHOOSE_LANG,
        reply_markup=language_keyboard()
    )


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Sizning Telegram ID: {message.from_user.id}")


@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Siz admin emassiz.")
        return

    users_count, stores_count, active_auto, sent_count = get_stats()

    await message.answer(
        "📊 Admin panel\n\n"
        f"👥 Userlar: {users_count}\n"
        f"🏪 Do‘konlar: {stores_count}\n"
        f"🟢 Avto-javob yoqilgan: {active_auto}\n"
        f"✅ Yuborilgan javoblar: {sent_count}"
    )


# =========================
# STORE
# =========================

@dp.message(F.text.in_(["➕ Do‘kon qo‘shish", "➕ Добавить магазин"]))
async def add_store_start(message: Message, state: FSMContext):
    add_user(message.from_user)
    lang = get_user_language(message.from_user.id)

    if lang == "ru":
        await message.answer(
            "🏪 Добавление магазина\n\n"
            "Сначала добавьте сотрудника бота в Uzum Seller.\n\n"
            f"📞 Телефон сотрудника: {EMPLOYEE_PHONE}\n\n"
            "После этого отправьте ID магазина."
        )
    else:
        await message.answer(
            "🏪 Do‘kon qo‘shish\n\n"
            "Avval Uzum Seller’da bot xodimini do‘koningizga qo‘shing.\n\n"
            f"📞 Xodim telefon raqami: {EMPLOYEE_PHONE}\n\n"
            "Keyin do‘kon ID raqamini yuboring."
        )

    await state.set_state(AddStoreState.waiting_store_id)


@dp.message(AddStoreState.waiting_store_id)
async def add_store_id(message: Message, state: FSMContext):
    lang = get_user_language(message.from_user.id)
    store_id = message.text.strip()

    if not store_id.isdigit():
        await message.answer(
            "ID магазина должен состоять только из цифр. Отправьте ещё раз:"
            if lang == "ru"
            else "Do‘kon ID faqat raqam bo‘lishi kerak. Qayta yuboring:"
        )
        return

    used = store_used_by_other_user(store_id, message.from_user.id)

    if used:
        await message.answer(
            "❌ Этот магазин уже привязан к другому Telegram аккаунту."
            if lang == "ru"
            else "❌ Bu do‘kon boshqa Telegram accountga ulangan."
        )
        await state.clear()
        return

    store_name = f"Do‘kon {store_id}"
    save_store(message.from_user.id, store_id, store_name)

    if lang == "ru":
        await message.answer(
            "✅ Магазин добавлен!\n\n"
            f"🏪 {store_name}\n"
            f"🆔 {store_id}\n\n"
            "Теперь активируйте подписку для этого магазина.",
            reply_markup=user_menu(message.from_user.id)
        )
    else:
        await message.answer(
            "✅ Do‘kon qo‘shildi!\n\n"
            f"🏪 {store_name}\n"
            f"🆔 {store_id}\n\n"
            "Endi shu do‘kon uchun obuna sotib oling.",
            reply_markup=user_menu(message.from_user.id)
        )

    await state.clear()


@dp.message(F.text.in_(["🏬 Do‘konlarim", "🏬 Мои магазины"]))
async def my_stores(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer(
            "У вас пока нет магазинов." if lang == "ru" else "Sizda hali do‘kon yo‘q.",
            reply_markup=user_menu(message.from_user.id)
        )
        return

    text = "🏬 Ваши магазины:\n\n" if lang == "ru" else "🏬 Do‘konlaringiz:\n\n"

    for row in stores:
        row_id, store_id, store_name, subscription_until, auto_enabled, mode = row

        if subscription_active(subscription_until):
            until = datetime.fromisoformat(subscription_until).strftime("%d.%m.%Y")
            sub_text = f"✅ Активна до {until}" if lang == "ru" else f"✅ Faol, {until} gacha"
        else:
            sub_text = "❌ Подписка не активна" if lang == "ru" else "❌ Obuna faol emas"

        auto_text = "🟢 Включен" if auto_enabled and lang == "ru" else "🟢 Yoqilgan" if auto_enabled else "⚪ Отключен" if lang == "ru" else "⚪ O‘chirilgan"

        text += (
            f"🏪 {store_name}\n"
            f"🆔 {store_id}\n"
            f"💳 Obuna/Подписка: {sub_text}\n"
            f"▶️ Auto: {auto_text}\n"
            f"⚙️ Mode: {mode}\n\n"
        )

    await message.answer(text, reply_markup=user_menu(message.from_user.id))


# =========================
# SUBSCRIPTION
# =========================

@dp.message(F.text.in_(["💳 Obuna", "💳 Подписка"]))
async def subscription_menu(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer(
            "Сначала добавьте магазин." if lang == "ru" else "Avval do‘kon qo‘shing.",
            reply_markup=user_menu(message.from_user.id)
        )
        return

    price_text = f"{SUBSCRIPTION_PRICE:,}".replace(",", " ")

    await message.answer(
        (
            "Для какого магазина оформляем подписку на 30 дней?\n\n"
            f"💰 Цена: {price_text} сум"
        ) if lang == "ru" else (
            "Qaysi do‘kon uchun 30 kunlik obuna olamiz?\n\n"
            f"💰 Narx: {price_text} so‘m"
        ),
        reply_markup=stores_keyboard(stores, "sub_store")
    )


@dp.callback_query(F.data.startswith("sub_store:"))
async def subscription_store_selected(callback: CallbackQuery, state: FSMContext):
    lang = get_user_language(callback.from_user.id)
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    await state.update_data(store_row_id=row_id)

    price_text = f"{SUBSCRIPTION_PRICE:,}".replace(",", " ")

    if lang == "ru":
        text = (
            "💳 Данные для оплаты:\n\n"
            f"🏪 Магазин: {store[3]}\n"
            f"🆔 ID магазина: {store[2]}\n"
            f"📅 Срок: {SUBSCRIPTION_DAYS} дней\n"
            f"💰 Сумма: {price_text} сум\n\n"
            f"💳 Карта: {PAYMENT_CARD}\n"
            f"👤 Владелец карты: {PAYMENT_OWNER}\n\n"
            "После оплаты отправьте чек сюда."
        )
    else:
        text = (
            "💳 To‘lov ma’lumotlari:\n\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"🆔 Do‘kon ID: {store[2]}\n"
            f"📅 Muddat: {SUBSCRIPTION_DAYS} kun\n"
            f"💰 Summa: {price_text} so‘m\n\n"
            f"💳 Karta: {PAYMENT_CARD}\n"
            f"👤 Karta egasi: {PAYMENT_OWNER}\n\n"
            "To‘lovdan so‘ng chek rasmini shu yerga yuboring."
        )

    await callback.message.answer(text)
    await state.set_state(PaymentState.waiting_receipt)
    await callback.answer()


@dp.message(PaymentState.waiting_receipt)
async def receive_payment_receipt(message: Message, state: FSMContext):
    lang = get_user_language(message.from_user.id)

    if not (message.photo or message.document):
        await message.answer("Отправьте фото или файл чека." if lang == "ru" else "Iltimos, chek rasmini yoki faylini yuboring.")
        return

    data = await state.get_data()
    store_row_id = data.get("store_row_id")
    store = get_store(store_row_id, message.from_user.id)

    if not store:
        await message.answer("Do‘kon topilmadi. Qayta urinib ko‘ring.")
        await state.clear()
        return

    payment_id = uuid.uuid4().hex[:8]

    pending_payments[payment_id] = {
        "telegram_id": message.from_user.id,
        "store_row_id": store_row_id,
        "store_name": store[3],
        "store_id": store[2],
    }

    save_payment(payment_id, message.from_user.id, store_row_id, SUBSCRIPTION_PRICE, SUBSCRIPTION_DAYS, "pending")

    await message.answer(
        "✅ Чек отправлен администратору. После подтверждения подписка активируется."
        if lang == "ru"
        else "✅ Chek adminga yuborildi. Tasdiqlangandan so‘ng obuna faollashadi.",
        reply_markup=user_menu(message.from_user.id)
    )

    if ADMIN_ID:
        price_text = f"{SUBSCRIPTION_PRICE:,}".replace(",", " ")

        await bot.send_message(
            ADMIN_ID,
            "🧾 Yangi obuna to‘lovi\n\n"
            f"Payment ID: {payment_id}\n"
            f"👤 User: {message.from_user.full_name}\n"
            f"🆔 User ID: {message.from_user.id}\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"🆔 Do‘kon ID: {store[2]}\n"
            f"💰 Summa: {price_text} so‘m\n"
            f"📅 Muddat: {SUBSCRIPTION_DAYS} kun",
            reply_markup=payment_admin_keyboard(payment_id)
        )

        try:
            await bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
        except Exception:
            pass

    await state.clear()


@dp.callback_query(F.data.startswith("pay_ok:"))
async def payment_ok(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = callback.data.split(":")[1]
    payment = pending_payments.pop(payment_id, None)

    if not payment:
        await callback.answer("To‘lov topilmadi yoki allaqachon ko‘rilgan.", show_alert=True)
        return

    new_until = extend_subscription(payment["store_row_id"])
    update_payment_status(payment_id, "approved")

    until_text = new_until.strftime("%d.%m.%Y") if new_until else "-"
    user_lang = get_user_language(payment["telegram_id"])

    await callback.message.answer(
        "✅ To‘lov tasdiqlandi.\n\n"
        f"🏪 Do‘kon: {payment['store_name']}\n"
        f"📅 Obuna muddati: {until_text} gacha"
    )

    try:
        await bot.send_message(
            payment["telegram_id"],
            (
                "✅ Подписка активирована!\n\n"
                f"🏪 Магазин: {payment['store_name']}\n"
                f"📅 Действует до: {until_text}"
            ) if user_lang == "ru" else (
                "✅ Obuna faollashtirildi!\n\n"
                f"🏪 Do‘kon: {payment['store_name']}\n"
                f"📅 Amal qilish muddati: {until_text} gacha"
            ),
            reply_markup=main_menu(user_lang)
        )
    except Exception:
        pass

    await callback.answer()


@dp.callback_query(F.data.startswith("pay_no:"))
async def payment_no(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Ruxsat yo‘q.", show_alert=True)
        return

    payment_id = callback.data.split(":")[1]
    payment = pending_payments.pop(payment_id, None)

    if not payment:
        await callback.answer("To‘lov topilmadi yoki allaqachon ko‘rilgan.", show_alert=True)
        return

    update_payment_status(payment_id, "rejected")
    user_lang = get_user_language(payment["telegram_id"])

    await callback.message.answer("❌ To‘lov rad etildi.")

    try:
        await bot.send_message(
            payment["telegram_id"],
            "❌ Оплата отклонена." if user_lang == "ru" else "❌ To‘lov rad etildi.",
            reply_markup=main_menu(user_lang)
        )
    except Exception:
        pass

    await callback.answer()


# =========================
# REPLY MODE
# =========================

@dp.message(F.text.in_(["⚙️ Javob rejimi", "⚙️ Режим ответа"]))
async def reply_mode_menu(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing." if lang == "uz" else "Сначала добавьте магазин.", reply_markup=user_menu(message.from_user.id))
        return

    await message.answer(
        "Qaysi do‘kon uchun javob rejimini o‘zgartiramiz?"
        if lang == "uz"
        else "Для какого магазина изменить режим ответа?",
        reply_markup=stores_keyboard(stores, "mode_store")
    )


@dp.callback_query(F.data.startswith("mode_store:"))
async def mode_store(callback: CallbackQuery):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    await callback.message.answer(
        f"⚙️ Javob rejimi\n\n"
        f"🏪 Do‘kon: {store[3]}\n\n"
        "Rejimni tanlang:",
        reply_markup=mode_keyboard(row_id)
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("set_mode:"))
async def set_mode(callback: CallbackQuery):
    _, row_id, mode = callback.data.split(":")
    row_id = int(row_id)

    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    update_reply_mode(row_id, mode)

    mode_names = {
        "template_auto": "✍️ Shablon + avtomatik",
        "template_low_confirm": "⚠️ Shablon + 1-2⭐ tasdiqlash",
        "template_confirm": "👀 Shablon + hammasini tasdiqlash",
        "ai_auto": "🤖 AI + avtomatik",
        "ai_low_confirm": "⚠️ AI + 1-2⭐ tasdiqlash",
        "ai_confirm": "🤖 AI + hammasini tasdiqlash",
    }

    await callback.message.answer(
        "✅ Javob rejimi o‘zgartirildi.\n\n"
        f"🏪 Do‘kon: {store[3]}\n"
        f"⚙️ Rejim: {mode_names.get(mode, mode)}",
        reply_markup=user_menu(callback.from_user.id)
    )

    await callback.answer()


# =========================
# TEMPLATES
# =========================

@dp.message(F.text.in_(["📝 Shablonlar", "📝 Шаблоны"]))
async def templates_menu(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing." if lang == "uz" else "Сначала добавьте магазин.", reply_markup=user_menu(message.from_user.id))
        return

    await message.answer(
        "Qaysi do‘kon uchun shablonlarni sozlaymiz?"
        if lang == "uz"
        else "Для какого магазина настроить шаблоны?",
        reply_markup=stores_keyboard(stores, "template_store")
    )


@dp.callback_query(F.data.startswith("template_store:"))
async def template_store(callback: CallbackQuery, state: FSMContext):
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    await state.update_data(store_row_id=row_id, rating=5, lang="uz")

    await callback.message.answer(template_question_text(store[3], 5, "uz"))

    await state.set_state(TemplateState.waiting_text)
    await callback.answer()


@dp.message(TemplateState.waiting_text)
async def template_text(message: Message, state: FSMContext):
    data = await state.get_data()

    row_id = data.get("store_row_id")
    rating = data.get("rating")
    lang = data.get("lang")

    store = get_store(row_id)

    if not store:
        await message.answer("Do‘kon topilmadi.")
        await state.clear()
        return

    update_template(row_id, rating, lang, message.text.strip())

    if lang == "uz":
        await state.update_data(lang="ru")

        await message.answer(
            f"✅ {rating} yulduz uchun o‘zbekcha shablon saqlandi.\n\n"
            f"Endi ⭐ {rating} yulduzli sharhlar uchun ruscha javob yozing.\n\n"
            "Masalan:\n"
            "Спасибо за покупку! Будем рады видеть вас снова 😊"
        )
        return

    next_rating = rating - 1

    if next_rating >= 1:
        await state.update_data(rating=next_rating, lang="uz")

        await message.answer(
            f"✅ {rating} yulduz uchun ruscha shablon saqlandi.\n\n"
            f"Endi ⭐ {next_rating} yulduzli sharhlar uchun o‘zbekcha javob yozing."
        )
        return

    await message.answer(
        "✅ Barcha shablonlar saqlandi.\n\n"
        "Endi bot sharh tiliga qarab mos shablonni ishlatadi:\n\n"
        "🇺🇿 O‘zbekcha sharh → o‘zbekcha shablon\n"
        "🇷🇺 Ruscha sharh → ruscha shablon",
        reply_markup=user_menu(message.from_user.id)
    )

    await state.clear()


# =========================
# AUTO REPLY
# =========================

@dp.message(F.text.in_(["▶️ Avto-javobni yoqish", "▶️ Включить автоответ"]))
async def enable_auto_menu(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing." if lang == "uz" else "Сначала добавьте магазин.", reply_markup=user_menu(message.from_user.id))
        return

    await message.answer(
        "Qaysi do‘kon uchun avto-javobni yoqamiz?"
        if lang == "uz"
        else "Для какого магазина включить автоответ?",
        reply_markup=stores_keyboard(stores, "auto_on")
    )


@dp.callback_query(F.data.startswith("auto_on:"))
async def auto_on(callback: CallbackQuery):
    lang = get_user_language(callback.from_user.id)
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    if not subscription_active(store[4]):
        await callback.message.answer(
            "❌ Bu do‘kon uchun obuna faol emas.\n\nAvval obuna sotib oling."
            if lang == "uz"
            else "❌ Для этого магазина подписка не активна.\n\nСначала оплатите подписку.",
            reply_markup=user_menu(callback.from_user.id)
        )
        await callback.answer()
        return

    update_auto_reply(row_id, True)

    await callback.message.answer(
        f"✅ Avto-javob yoqildi.\n\n🏪 Do‘kon: {store[3]}"
        if lang == "uz"
        else f"✅ Автоответ включен.\n\n🏪 Магазин: {store[3]}",
        reply_markup=user_menu(callback.from_user.id)
    )
    await callback.answer()


@dp.message(F.text.in_(["⏸ Avto-javobni to‘xtatish", "⏸ Остановить автоответ"]))
async def disable_auto_menu(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Avval do‘kon qo‘shing." if lang == "uz" else "Сначала добавьте магазин.", reply_markup=user_menu(message.from_user.id))
        return

    await message.answer(
        "Qaysi do‘kon uchun avto-javobni to‘xtatamiz?"
        if lang == "uz"
        else "Для какого магазина остановить автоответ?",
        reply_markup=stores_keyboard(stores, "auto_off")
    )


@dp.callback_query(F.data.startswith("auto_off:"))
async def auto_off(callback: CallbackQuery):
    lang = get_user_language(callback.from_user.id)
    row_id = int(callback.data.split(":")[1])
    store = get_store(row_id, callback.from_user.id)

    if not store:
        await callback.answer("Do‘kon topilmadi.", show_alert=True)
        return

    update_auto_reply(row_id, False)

    await callback.message.answer(
        f"⏸ Avto-javob to‘xtatildi.\n\n🏪 Do‘kon: {store[3]}"
        if lang == "uz"
        else f"⏸ Автоответ остановлен.\n\n🏪 Магазин: {store[3]}",
        reply_markup=user_menu(callback.from_user.id)
    )
    await callback.answer()


# =========================
# MANUAL CHECK
# =========================

@dp.message(F.text.in_(["🔍 Javobsiz sharhlar", "🔍 Отзывы без ответа"]))
async def manual_no_reply_reviews(message: Message):
    lang = get_user_language(message.from_user.id)
    await message.answer("🔄 Javobsiz sharhlar tekshirilmoqda..." if lang == "uz" else "🔄 Проверяем отзывы без ответа...")

    try:
        reviews = await uzum_get_no_reply_reviews(page=0, size=20)
    except Exception as e:
        await message.answer(f"❌ Xato: {e}")
        return

    user_store_ids = {str(row[1]): row for row in get_user_stores(message.from_user.id)}
    found = 0

    for review in reviews:
        review_id = extract_review_id(review)
        shop_id = str(extract_shop_id(review))

        if shop_id not in user_store_ids:
            continue

        store = get_store_by_shop_id(shop_id)
        if not store:
            continue

        rating = extract_rating(review)
        text = extract_review_text(review)
        product = extract_product_name(review)

        found += 1

        await message.answer(
            f"⭐ {rating} yulduzli javobsiz sharh\n\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"📦 Mahsulot: {product}\n"
            f"🆔 Review ID: {review_id}\n\n"
            f"💬 Sharh:\n{text}"
        )

        if found >= 5:
            break

    if found == 0:
        await message.answer(
            "Hozircha sizning do‘konlaringizda javobsiz sharh topilmadi."
            if lang == "uz"
            else "Пока нет отзывов без ответа по вашим магазинам."
        )


# =========================
# REVIEW CONFIRM
# =========================

@dp.callback_query(F.data.startswith("review_send:"))
async def review_send(callback: CallbackQuery):
    pending_id = callback.data.split(":")[1]
    item = pending_reviews.pop(pending_id, None)

    if not item:
        await callback.answer("Bu javob topilmadi yoki muddati tugagan.", show_alert=True)
        return

    try:
        await uzum_reply_review(item["review_id"], item["reply_text"])
        log_review(
            item["review_id"],
            item["telegram_id"],
            item["store_id"],
            item["rating"],
            "sent",
            item["reply_text"]
        )

        await callback.message.answer(
            "✅ Javob Uzum Seller’ga yuborildi.\n\n"
            f"🆔 Review ID: {item['review_id']}"
        )
    except Exception as e:
        await callback.message.answer(f"❌ Javob yuborishda xato: {e}")

    await callback.answer()


@dp.callback_query(F.data.startswith("review_skip:"))
async def review_skip(callback: CallbackQuery):
    pending_id = callback.data.split(":")[1]
    item = pending_reviews.pop(pending_id, None)

    if item:
        log_review(
            item["review_id"],
            item["telegram_id"],
            item["store_id"],
            item["rating"],
            "skipped",
            item["reply_text"]
        )

    await callback.message.answer("❌ Javob yuborilmadi.")
    await callback.answer()


@dp.callback_query(F.data.startswith("review_edit:"))
async def review_edit(callback: CallbackQuery, state: FSMContext):
    pending_id = callback.data.split(":")[1]

    if pending_id not in pending_reviews:
        await callback.answer("Bu javob topilmadi yoki muddati tugagan.", show_alert=True)
        return

    await state.update_data(pending_id=pending_id)

    await callback.message.answer(
        "✏️ Yangi javob matnini yuboring.\n\n"
        "Bot shu matnni Uzum Seller’ga yuboradi."
    )

    await state.set_state(EditReviewState.waiting_text)
    await callback.answer()


@dp.message(EditReviewState.waiting_text)
async def edit_review_text(message: Message, state: FSMContext):
    data = await state.get_data()
    pending_id = data.get("pending_id")

    item = pending_reviews.get(pending_id)

    if not item:
        await message.answer("Bu javob topilmadi yoki muddati tugagan.")
        await state.clear()
        return

    item["reply_text"] = message.text.strip()

    await message.answer(
        "Yangi javob:\n\n"
        f"{item['reply_text']}\n\n"
        "Yuboramizmi?",
        reply_markup=review_confirm_keyboard(pending_id)
    )

    await state.clear()


# =========================
# STATS
# =========================

@dp.message(F.text.in_(["📊 Statistika", "📊 Статистика"]))
async def user_stats(message: Message):
    lang = get_user_language(message.from_user.id)
    stores = get_user_stores(message.from_user.id)

    if not stores:
        await message.answer("Sizda hali do‘kon yo‘q." if lang == "uz" else "У вас пока нет магазинов.", reply_markup=user_menu(message.from_user.id))
        return

    text = "📊 Sizning statistikangiz:\n\n" if lang == "uz" else "📊 Ваша статистика:\n\n"

    conn = db()
    cur = conn.cursor()

    for row in stores:
        row_id, store_id, store_name, subscription_until, auto_enabled, mode = row

        cur.execute(
            "SELECT COUNT(*) FROM review_logs WHERE telegram_id = ? AND store_id = ? AND status = 'sent'",
            (message.from_user.id, str(store_id))
        )
        sent = cur.fetchone()[0]

        sub = "✅ Faol" if subscription_active(subscription_until) else "❌ Faol emas"
        auto = "🟢 Yoqilgan" if auto_enabled else "⚪ O‘chirilgan"

        if lang == "ru":
            sub = "✅ Активна" if subscription_active(subscription_until) else "❌ Не активна"
            auto = "🟢 Включен" if auto_enabled else "⚪ Отключен"

        text += (
            f"🏪 {store_name}\n"
            f"💳 Obuna/Подписка: {sub}\n"
            f"▶️ Auto: {auto}\n"
            f"✅ Javoblar/Ответы: {sent}\n\n"
        )

    conn.close()

    await message.answer(text, reply_markup=user_menu(message.from_user.id))


# =========================
# BACKGROUND WATCHER
# =========================

async def process_one_review(review):
    review_id = extract_review_id(review)

    if not review_id:
        return

    if review_already_logged(review_id):
        return

    shop_id = extract_shop_id(review)

    if not shop_id:
        return

    store = get_store_by_shop_id(str(shop_id))

    if not store:
        return

    row_id = store[0]
    telegram_id = store[1]
    store_id = store[2]
    store_name = store[3]
    subscription_until = store[4]
    auto_enabled = store[5]
    mode = store[6]
    user_lang = get_user_language(telegram_id)

    if not subscription_active(subscription_until):
        if auto_enabled:
            update_auto_reply(row_id, False)
            try:
                await bot.send_message(
                    telegram_id,
                    (
                        "⛔ Obuna muddati tugadi.\n\n"
                        f"🏪 Do‘kon: {store_name}\n"
                        "Avto-javob to‘xtatildi."
                    ) if user_lang == "uz" else (
                        "⛔ Срок подписки истёк.\n\n"
                        f"🏪 Магазин: {store_name}\n"
                        "Автоответ остановлен."
                    ),
                    reply_markup=main_menu(user_lang)
                )
            except Exception:
                pass
        return

    if not auto_enabled:
        return

    detail = review

    try:
        detail = await uzum_get_review_detail(review_id)
    except Exception:
        pass

    shop_title = extract_shop_title(detail)
    if shop_title and store_name.startswith("Do‘kon "):
        update_store_name(row_id, shop_title)
        store = get_store(row_id)

    rating = extract_rating(detail)
    review_text = extract_review_text(detail)
    product = extract_product_name(detail)
    reply_text = await build_reply_text(store, detail)

    if needs_confirmation(mode, rating):
        pending_id = uuid.uuid4().hex[:8]

        pending_reviews[pending_id] = {
            "review_id": review_id,
            "telegram_id": telegram_id,
            "store_id": store_id,
            "rating": rating,
            "reply_text": reply_text,
        }

        log_review(review_id, telegram_id, store_id, rating, "pending", reply_text)

        try:
            await bot.send_message(
                telegram_id,
                "🆕 Yangi javobsiz sharh topildi\n\n"
                f"🏪 Do‘kon: {store[3]}\n"
                f"📦 Mahsulot: {product}\n"
                f"⭐ Baho: {rating}\n"
                f"🆔 Review ID: {review_id}\n\n"
                f"💬 Sharh:\n{review_text}\n\n"
                f"✍️ Bot javobi:\n{reply_text}",
                reply_markup=review_confirm_keyboard(pending_id)
            )
        except Exception:
            pass

        return

    try:
        await uzum_reply_review(review_id, reply_text)

        log_review(review_id, telegram_id, store_id, rating, "sent", reply_text)

        await bot.send_message(
            telegram_id,
            "✅ Sharhga javob avtomatik yuborildi\n\n"
            f"🏪 Do‘kon: {store[3]}\n"
            f"⭐ Baho: {rating}\n"
            f"🆔 Review ID: {review_id}\n\n"
            f"✍️ Javob:\n{reply_text}"
        )

    except Exception as e:
        log_review(review_id, telegram_id, store_id, rating, "error", str(e))


async def check_reviews_once():
    reviews = await uzum_get_no_reply_reviews(page=0, size=20)

    for review in reviews:
        await process_one_review(review)
        await asyncio.sleep(0.3)


async def review_watcher():
    await asyncio.sleep(5)

    while True:
        try:
            await check_reviews_once()
        except Exception as e:
            print("review_watcher error:", e)

        await asyncio.sleep(CHECK_INTERVAL)


# =========================
# RUN
# =========================

async def main():
    init_db()
    asyncio.create_task(review_watcher())
    print("Uzum Sharh Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
