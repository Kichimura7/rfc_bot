import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("ОШИБКА: BOT_TOKEN не указан в файле .env!")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004465872509"))
WEB_APP_BASE_URL = os.getenv("WEB_APP_URL", "https://kichimura7.github.io/rfc_bot/")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
DATABASE_FILE = Path(os.getenv("DATABASE_FILE", "database.db"))
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "settings.json"))
MAX_RECEIPT_SIZE = 10 * 1024 * 1024
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "3600"))
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "8613061969,6709505823").split(",") if x.strip()}
parsed = urllib.parse.urlparse(WEB_APP_BASE_URL)
DEFAULT_ORIGIN = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
ALLOWED_ORIGINS = {x.strip().rstrip("/") for x in os.getenv("ALLOWED_ORIGINS", DEFAULT_ORIGIN).split(",") if x.strip()}

REQUISITES_LIST = [
    {"phone": "+7 967 951 47 01", "recipient": "Езимат Т."},
    {"phone": "+7 964 063 88 08", "recipient": "Байали Т."},
    {"phone": "+7 963 593 73 87", "recipient": "Хамзат С."},
]
DEFAULT_SETTINGS = {
    "fee": "1000",
    "location": "г. Грозный, Sport Hall Колизей",
    "weighin": "28 июля (допуск -1кг)",
    "fights": "29 июля",
    "maincard": "30 июля",
    "rules": "1. Допускаются участники с подтвержденной медицинской справкой и страховкой.\n2. Форма одежды: шорты ММА/грепплинг, рашгард, капа, бинты, защита голени и паха.\n3. Процедура взвешивания обязательна для всех бойцов. Перевес свыше допустимой нормы влечет дисквалификацию.\n4. Регламент раундов: 3 раунда по 3 минуты (для профессионалов) / 3 раунда по 2 минуты (для любителей).",
}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("rfc-bot")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def json_error(message, status=400, **extra):
    data = {"error": message}
    data.update(extra)
    return web.json_response(data, status=status)


def normalize(value, limit=5000):
    return "" if value is None else str(value).strip()[:limit]


def is_admin(user_id):
    return str(user_id) in ADMIN_IDS


def atomic_json_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextmanager
def get_db():
    conn = sqlite3.connect(DATABASE_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fighters (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL DEFAULT '',
                club TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                age TEXT NOT NULL DEFAULT '',
                weight TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'searching',
                opponent_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                user_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                receipt_filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                confirmed_by INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fighter1 TEXT NOT NULL,
                fighter2 TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                age TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        migrations = {
            "fighters": {
                "age": "ALTER TABLE fighters ADD COLUMN age TEXT NOT NULL DEFAULT ''",
                "weight": "ALTER TABLE fighters ADD COLUMN weight TEXT NOT NULL DEFAULT ''",
                "created_at": "ALTER TABLE fighters ADD COLUMN created_at TEXT",
                "updated_at": "ALTER TABLE fighters ADD COLUMN updated_at TEXT",
            },
            "payments": {
                "receipt_filename": "ALTER TABLE payments ADD COLUMN receipt_filename TEXT",
                "created_at": "ALTER TABLE payments ADD COLUMN created_at TEXT",
                "updated_at": "ALTER TABLE payments ADD COLUMN updated_at TEXT",
                "confirmed_by": "ALTER TABLE payments ADD COLUMN confirmed_by INTEGER",
            },
        }
        for table, columns in migrations.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, sql in columns.items():
                if name not in existing:
                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError:
                        logger.warning("Миграция %s.%s не выполнена", table, name, exc_info=True)
        conn.execute("UPDATE fighters SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP), updated_at=COALESCE(updated_at,CURRENT_TIMESTAMP)")
        conn.execute("UPDATE payments SET created_at=COALESCE(created_at,CURRENT_TIMESTAMP), updated_at=COALESCE(updated_at,CURRENT_TIMESTAMP)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fighters_category ON fighters(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fighters_status ON fighters(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")


init_db()


def load_settings():
    result = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE.exists():
        return result
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            result.update(data)
            if "fights" not in data and "fights_date" in data:
                result["fights"] = data["fights_date"]
            if "maincard" not in data and "main_card_date" in data:
                result["maincard"] = data["main_card_date"]
    except Exception:
        logger.exception("Ошибка чтения settings.json")
    return result


def save_settings(data):
    settings = dict(DEFAULT_SETTINGS)
    for key in {"fee", "location", "weighin", "fights", "maincard", "rules"}:
        if key in data:
            settings[key] = normalize(data[key])
    try:
        fee = float(str(settings["fee"]).replace(",", "."))
        if fee < 0 or fee > 10_000_000:
            raise ValueError
        settings["fee"] = str(int(fee)) if fee.is_integer() else str(fee)
    except ValueError:
        raise ValueError("Некорректная сумма взноса")
    atomic_json_write(SETTINGS_FILE, settings)
    return settings


def get_current_requisites():
    return REQUISITES_LIST[(datetime.now().hour // 2) % len(REQUISITES_LIST)]


def verify_telegram_data(init_data_raw):
    if not init_data_raw:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = parsed.get("auth_date")
        if auth_date:
            now = int(datetime.now(timezone.utc).timestamp())
            if int(auth_date) > now + 60 or now - int(auth_date) > INIT_DATA_MAX_AGE:
                return None
        raw_user = parsed.get("user")
        return json.loads(raw_user) if raw_user else {}
    except Exception:
        logger.exception("Ошибка проверки Telegram initData")
        return None


def require_user(request):
    user = verify_telegram_data(request.headers.get("X-Telegram-Init-Data", ""))
    if not user or not user.get("id"):
        return None, json_error("Ошибка авторизации Telegram.", 401)
    return user, None


def require_admin(request):
    user, error = require_user(request)
    if error:
        return None, error
    if not is_admin(user["id"]):
        return None, json_error("Доступ запрещён.", 403)
    return user, None


def unique_payment_code(conn):
    for _ in range(100):
        code = str(secrets.randbelow(9000) + 1000)
        if not conn.execute("SELECT 1 FROM payments WHERE code=?", (code,)).fetchone():
            return code
    raise RuntimeError("Не удалось создать уникальный код оплаты")


def ensure_payment(user_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return row
        code = unique_payment_code(conn)
        conn.execute("INSERT INTO payments(user_id,code,status) VALUES(?,?,?)", (user_id, code, "pending"))
        return conn.execute("SELECT * FROM payments WHERE user_id=?", (user_id,)).fetchone()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🥊 Открыть RFC League", web_app=WebAppInfo(url=WEB_APP_BASE_URL))
    ]])
    await message.answer("Привет! Нажми на кнопку ниже, чтобы открыть приложение:", reply_markup=keyboard)


async def finalize_payment(callback: CallbackQuery, new_status):
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    try:
        _, user_id_raw, code = callback.data.split(":", 2)
        user_id = int(user_id_raw)
        code = normalize(code, 50)
    except Exception:
        await callback.answer("Некорректная заявка.", show_alert=True)
        return
    with get_db() as conn:
        payment = conn.execute("SELECT * FROM payments WHERE user_id=? AND code=?", (user_id, code)).fetchone()
        if not payment:
            await callback.answer("Платёж не найден.", show_alert=True)
            return
        if payment["status"] == "confirmed":
            await callback.answer("Оплата уже подтверждена.", show_alert=True)
            return
        conn.execute("UPDATE payments SET status=?, confirmed_by=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND code=?", (new_status, callback.from_user.id, user_id, code))
    await callback.answer("Заявка подтверждена ✅" if new_status == "confirmed" else "Заявка отклонена ❌")
    try:
        if new_status == "confirmed":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🥊 Открыть RFC League", web_app=WebAppInfo(url=f"{WEB_APP_BASE_URL}?paid=true"))
            ]])
            await bot.send_message(user_id, f"🎉 <b>Оплата (код: {html.escape(code)}) подтверждена!</b>\nВы успешно внесены в список участников турнира RFC.", reply_markup=keyboard, parse_mode="HTML")
        else:
            await bot.send_message(user_id, f"⚠️ <b>Оплата по коду {html.escape(code)} отклонена.</b>\nПеревод не найден. Свяжитесь с организаторами.", parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить уведомление пользователю %s", user_id)
    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    marker = "ПОДТВЕРЖДЕНО" if new_status == "confirmed" else "ОТКЛОНЕНО"
    caption = (callback.message.caption or callback.message.text or "") + f"\n\n<b>{marker}</b> (Админ: {html.escape(admin_name)})"
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=caption, reply_markup=None, parse_mode="HTML")
        else:
            await callback.message.edit_text(text=caption, reply_markup=None, parse_mode="HTML")
    except Exception:
        logger.warning("Не удалось обновить сообщение заявки", exc_info=True)


@dp.callback_query(F.data.startswith("pay_approve:"))
async def process_approve(callback: CallbackQuery):
    await finalize_payment(callback, "confirmed")


@dp.callback_query(F.data.startswith("pay_reject:"))
async def process_reject(callback: CallbackQuery):
    await finalize_payment(callback, "rejected")


async def api_healthcheck(request):
    return web.json_response({"status": "ok", "message": "RFC Bot Server is running"})


async def api_me(request):
    user, error = require_user(request)
    if error: return error
    uid = int(user["id"])
    with get_db() as conn:
        fighter = conn.execute("SELECT * FROM fighters WHERE user_id=?", (uid,)).fetchone()
        payment = conn.execute("SELECT * FROM payments WHERE user_id=?", (uid,)).fetchone()
    return web.json_response({"ok": True, "user": user, "is_admin": is_admin(uid), "fighter": dict(fighter) if fighter else None, "payment": dict(payment) if payment else None})


async def api_get_requisites(request):
    req = get_current_requisites()
    return web.json_response({"phone": req["phone"], "recipient": req["recipient"], "status": "ok"})


async def api_get_settings(request):
    return web.json_response(load_settings())


async def api_save_settings(request):
    _, error = require_admin(request)
    if error: return error
    try:
        data = await request.json()
        return web.json_response({"status": "ok", "settings": save_settings(data)})
    except json.JSONDecodeError:
        return json_error("Некорректный JSON.", 400)
    except ValueError as exc:
        return json_error(str(exc), 400)
    except Exception:
        logger.exception("Ошибка сохранения настроек")
        return json_error("Внутренняя ошибка сервера.", 500)


async def api_register(request):
    user, error = require_user(request)
    if error: return error
    try:
        data = await request.json()
    except Exception:
        return json_error("Некорректный JSON.", 400)
    fullname = normalize(data.get("fullname") or data.get("full_name"), 200)
    club = normalize(data.get("club") or "Самостоятельно", 200)
    age = normalize(data.get("age"), 30)
    weight = normalize(data.get("weight"), 50)
    if not fullname: return json_error("ФИО обязательно.", 400)
    uid = int(user["id"])
    with get_db() as conn:
        existing = conn.execute("SELECT status FROM fighters WHERE user_id=?", (uid,)).fetchone()
        if existing and existing["status"] in {"confirmed", "paid"}:
            return json_error("Подтверждённую заявку нельзя перезаписать.", 409)
        conn.execute("""
            INSERT INTO fighters(user_id,full_name,club,category,age,weight,status,updated_at)
            VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name=excluded.full_name, club=excluded.club,
                category=excluded.category, age=excluded.age,
                weight=excluded.weight, updated_at=CURRENT_TIMESTAMP
        """, (uid, fullname, club, weight, age, weight, "searching"))
    payment = ensure_payment(uid)
    try:
        await bot.send_message(ADMIN_CHAT_ID,
            "📝 <b>Новая заявка RFC</b>\n\n"
            f"👤 <b>ФИО:</b> {html.escape(fullname)}\n"
            f"🏋️ <b>Клуб:</b> {html.escape(club)}\n"
            f"⚖️ <b>Вес:</b> {html.escape(weight)}\n"
            f"🎂 <b>Возраст:</b> {html.escape(age)}\n"
            f"🆔 <b>TG ID:</b> <code>{uid}</code>", parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить уведомление о регистрации")
    application = {"user_id": uid, "full_name": fullname, "fullname": fullname, "club": club, "category": weight, "age": age, "weight": weight}
    return web.json_response({"status": "ok", "application": application, "payment": dict(payment)})


async def api_application(request):
    user, error = require_user(request)
    if error: return error
    uid = int(user["id"])
    with get_db() as conn:
        fighter = conn.execute("SELECT * FROM fighters WHERE user_id=?", (uid,)).fetchone()
        payment = conn.execute("SELECT * FROM payments WHERE user_id=?", (uid,)).fetchone()
    return web.json_response({"application": dict(fighter) if fighter else None, "payment": dict(payment) if payment else None})


async def api_check_payment(request):
    user, error = require_user(request)
    if error: return error
    uid = int(user["id"])
    with get_db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return web.json_response({"status": "not_found", "is_paid": False, "code": None, "payment": None})
    return web.json_response({"status": row["status"], "is_paid": row["status"] == "confirmed", "code": row["code"], "payment": dict(row)})


def detect_image(data):
    if data.startswith(b"\xff\xd8\xff"): return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "webp"
    return None


async def api_submit_receipt(request):
    user, error = require_user(request)
    if error: return error
    uid = int(user["id"])
    reader = await request.multipart()
    file_bytes = None
    filename = "receipt.jpg"
    submitted_code = ""
    async for field in reader:
        if field.name == "file":
            filename = normalize(field.filename or "receipt.jpg", 255)
            chunks, total = [], 0
            while True:
                chunk = await field.read_chunk(64 * 1024)
                if not chunk: break
                total += len(chunk)
                if total > MAX_RECEIPT_SIZE: return json_error("Файл слишком большой.", 413)
                chunks.append(chunk)
            file_bytes = b"".join(chunks)
        elif field.name == "code":
            submitted_code = normalize(await field.text(), 50)
    if not file_bytes: return json_error("Файл чека не найден.", 400)
    if not detect_image(file_bytes): return json_error("Разрешены только JPG, PNG или WEBP.", 400)
    with get_db() as conn:
        payment = conn.execute("SELECT * FROM payments WHERE user_id=?", (uid,)).fetchone()
        fighter = conn.execute("SELECT full_name FROM fighters WHERE user_id=?", (uid,)).fetchone()
    if not payment: return json_error("Сначала отправьте заявку.", 409)
    if payment["status"] == "confirmed": return json_error("Оплата уже подтверждена.", 409)
    if not submitted_code or not hmac.compare_digest(submitted_code, payment["code"]): return json_error("Неверный код оплаты.", 400)
    name = fighter["full_name"] if fighter else f"{user.get('first_name','')} {user.get('last_name','')}".strip() or "Участник"
    caption = ("🔔 <b>Новая заявка на оплату из WebApp!</b>\n\n"
               f"👤 <b>Участник:</b> {html.escape(name)}\n"
               f"🔑 <b>Код перевода:</b> <code>{html.escape(submitted_code)}</code>\n"
               f"🆔 <b>TG ID:</b> <code>{uid}</code>")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pay_approve:{uid}:{submitted_code}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay_reject:{uid}:{submitted_code}")
    ]])
    try:
        await bot.send_photo(ADMIN_CHAT_ID, photo=BufferedInputFile(file_bytes, filename=filename), caption=caption, reply_markup=keyboard, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отправить чек в админ-чат")
        return json_error("Не удалось передать чек организаторам.", 502)
    with get_db() as conn:
        conn.execute("UPDATE payments SET status='pending',receipt_filename=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (filename, uid))
    return web.json_response({"status": "ok", "code": payment["code"]})


async def api_opponents(request):
    user, error = require_user(request)
    if error: return error
    uid = int(user["id"])
    with get_db() as conn:
        me = conn.execute("SELECT * FROM fighters WHERE user_id=?", (uid,)).fetchone()
        if not me: return json_error("Сначала отправьте заявку.", 409)
        rows = conn.execute("""
            SELECT user_id,full_name,club,category,age,weight,status
            FROM fighters
            WHERE user_id!=? AND status!='rejected' AND category=? AND weight=?
            ORDER BY created_at ASC LIMIT 100
        """, (uid, me["category"], me["weight"])).fetchall()
    return web.json_response({"status": "ok", "opponents": [dict(r) for r in rows]})


async def api_applications(request):
    _, error = require_admin(request)
    if error: return error
    with get_db() as conn:
        rows = conn.execute("""
            SELECT f.*,p.code AS payment_code,p.status AS payment_status
            FROM fighters f LEFT JOIN payments p ON p.user_id=f.user_id
            ORDER BY f.created_at DESC
        """).fetchall()
    return web.json_response({"status": "ok", "applications": [dict(r) for r in rows]})


async def api_delete_application(request):
    _, error = require_admin(request)
    if error: return error
    try: uid = int(request.match_info["user_id"])
    except Exception: return json_error("Некорректный user_id.", 400)
    with get_db() as conn:
        conn.execute("DELETE FROM payments WHERE user_id=?", (uid,))
        cur = conn.execute("DELETE FROM fighters WHERE user_id=?", (uid,))
    if cur.rowcount == 0: return json_error("Заявка не найдена.", 404)
    return web.json_response({"status": "ok"})


async def api_reset_me(request):
    user, error = require_user(request)
    if error: return error
    uid = int(user["id"])
    with get_db() as conn:
        conn.execute("DELETE FROM payments WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM fighters WHERE user_id=?", (uid,))
    return web.json_response({"status": "ok"})


async def api_matches(request):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM matches ORDER BY id ASC").fetchall()
    return web.json_response({"status": "ok", "matches": [dict(r) for r in rows]})


async def api_add_match(request):
    _, error = require_admin(request)
    if error: return error
    try: data = await request.json()
    except Exception: return json_error("Некорректный JSON.", 400)
    f1 = normalize(data.get("fighter1") or data.get("f1"), 200)
    f2 = normalize(data.get("fighter2") or data.get("f2"), 200)
    cat = normalize(data.get("category"), 100)
    age = normalize(data.get("age"), 50)
    if not f1 or not f2: return json_error("Заполните ФИО обоих бойцов.", 400)
    if f1.casefold() == f2.casefold(): return json_error("Боец не может быть соперником самому себе.", 400)
    with get_db() as conn:
        cur = conn.execute("INSERT INTO matches(fighter1,fighter2,category,age) VALUES(?,?,?,?)", (f1,f2,cat,age))
    return web.json_response({"status":"ok","match":{"id":cur.lastrowid,"fighter1":f1,"fighter2":f2,"category":cat,"age":age}})


async def api_delete_match(request):
    _, error = require_admin(request)
    if error: return error
    try: mid = int(request.match_info["match_id"])
    except Exception: return json_error("Некорректный ID боя.", 400)
    with get_db() as conn: cur = conn.execute("DELETE FROM matches WHERE id=?", (mid,))
    if cur.rowcount == 0: return json_error("Бой не найден.", 404)
    return web.json_response({"status":"ok"})


@web.middleware
async def cors_middleware(request, handler):
    origin = request.headers.get("Origin", "").rstrip("/")
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try: response = await handler(request)
        except web.HTTPException as exc: response = exc
        except Exception:
            logger.exception("Неперехваченная ошибка %s %s", request.method, request.path)
            response = json_error("Внутренняя ошибка сервера.", 500)
    if origin and origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Telegram-Init-Data,Authorization"
    response.headers["Access-Control-Max-Age"] = "600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


async def main():
    app = web.Application(middlewares=[cors_middleware], client_max_size=MAX_RECEIPT_SIZE + 1024*1024)
    routes = [
        ("GET","/",api_healthcheck),("GET","/health",api_healthcheck),
        ("GET","/api/me",api_me),("GET","/api/application",api_application),
        ("GET","/api/get_requisites",api_get_requisites),("GET","/api/get_settings",api_get_settings),
        ("POST","/api/save_settings",api_save_settings),("POST","/api/register",api_register),
        ("POST","/api/submit_receipt",api_submit_receipt),("GET","/api/check_payment",api_check_payment),
        ("GET","/api/opponents",api_opponents),("GET","/api/applications",api_applications),
        ("DELETE","/api/applications/{user_id}",api_delete_application),("DELETE","/api/reset_me",api_reset_me),
        ("GET","/api/matches",api_matches),("POST","/api/matches",api_add_match),("DELETE","/api/matches/{match_id}",api_delete_match),
    ]
    for method, path, handler in routes: app.router.add_route(method, path, handler)
    runner = web.AppRunner(app, access_log=logger)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT, reuse_address=True)
    await site.start()
    logger.info("REST API запущен на http://%s:%s", HOST, PORT)
    logger.info("Администраторы: %s", sorted(ADMIN_IDS))
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Бот остановлен.")
