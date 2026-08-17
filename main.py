import asyncio
from datetime import datetime
import hashlib
import hmac
import html
import json
import logging
import os
import sqlite3
import urllib.parse
import aiohttp_cors
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

# Загрузка переменных окружения из .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004465872509"))
WEB_APP_BASE_URL = os.getenv(
    "WEB_APP_URL", "https://kichimura7.github.io/rfc_bot/"
)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

# Список ID администраторов (впишите сюда ваш реальный Telegram ID!)
ADMIN_IDS = [str(ADMIN_CHAT_ID), "8613061969", "670950582"]

SETTINGS_FILE = "settings.json"

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: BOT_TOKEN не указан в файле .env!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# === ГЛОБАЛЬНЫЙ CORS MIDDLEWARE ===
@web.middleware
async def cors_middleware(request, handler):
    # При браузном preflight (OPTIONS) сразу возвращаем 200 OK
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
        except Exception as e:
            logging.error(f"Неперехваченная ошибка: {e}")
            response = web.json_response({"error": str(e)}, status=500)

    # Принудительно добавляем CORS-заголовки к ЛЮБОМУ ответу
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, Authorization"
    return response


# === ПРОВЕРКА TELEGRAM INIT_DATA (HMAC-SHA256) ===
def verify_telegram_data(init_data_raw: str) -> dict | None:
    if not init_data_raw:
        return None
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data_raw, keep_blank_values=True))
        received_hash = parsed_data.pop('hash', None)
        if not received_hash:
            return None

        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if hmac.compare_digest(computed_hash, received_hash):
            user_data = parsed_data.get('user')
            if user_data:
                return json.loads(user_data)
            return {}
    except Exception as e:
        logging.error(f"Ошибка проверки initData: {e}")
    return None


# === РОТАЦИЯ РЕКВИЗИТОВ ===
REQUISITES_LIST = [
    {"phone": "+7 967 951 47 01", "recipient": "Езимат Т."},
    {"phone": "+7 964 063 88 08", "recipient": "Байали Т."},
    {"phone": "+7 963 593 73 87", "recipient": "Хамзат С."}
]

def get_current_requisites():
    current_hour = datetime.now().hour
    index = (current_hour // 2) % len(REQUISITES_LIST)
    return REQUISITES_LIST[index]


# === БАЗА ДАННЫХ ===
def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fighters (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                club TEXT,
                category TEXT,
                status TEXT DEFAULT 'searching',
                opponent_id INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                user_id INTEGER PRIMARY KEY,
                code TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()


init_db()


# === TELEGRAM ХЭНДЛЕРЫ ===
@dp.message(CommandStart())
async def cmd_start(message: Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🥊 Открыть RFC League",
                web_app=WebAppInfo(url=WEB_APP_BASE_URL),
            )
        ]]
    )
    await message.answer(
        "Привет! Нажми на кнопку ниже, чтобы открыть приложение:",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("pay_approve:"))
async def process_approve(callback: CallbackQuery):
    await callback.answer("Заявка подтверждена ✅")
    _, user_id_str, code = callback.data.split(":", 2)
    user_id = int(user_id_str)

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'confirmed')",
            (user_id, code),
        )
        conn.commit()

    web_app_url = f"{WEB_APP_BASE_URL}?paid=true"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🥊 Открыть RFC League", web_app=WebAppInfo(url=web_app_url)
            )
        ]]
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 <b>Оплата (код: {code}) подтверждена!</b>\n"
                "Вы успешно внесены в список участников турнира RFC."
            ),
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    admin_name = (
        f"@{callback.from_user.username}"
        if callback.from_user.username
        else callback.from_user.full_name
    )
    caption = (
        (callback.message.caption or callback.message.text or "")
        + f"\n\n✅ <b>ПОДТВЕРЖДЕНО</b> (Админ: {html.escape(admin_name)})"
    )

    if callback.message.photo:
        await callback.message.edit_caption(
            caption=caption, reply_markup=None, parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            text=caption, reply_markup=None, parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("pay_reject:"))
async def process_reject(callback: CallbackQuery):
    await callback.answer("Заявка отклонена ❌")
    _, user_id_str, code = callback.data.split(":", 2)
    user_id = int(user_id_str)

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'rejected')",
            (user_id, code),
        )
        conn.commit()

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                f"⚠️ <b>Оплата по коду {code} отклонена.</b>\n"
                "Перевод не найден. Свяжитесь с организаторами."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    admin_name = (
        f"@{callback.from_user.username}"
        if callback.from_user.username
        else callback.from_user.full_name
    )
    caption = (
        (callback.message.caption or callback.message.text or "")
        + f"\n\n❌ <b>ОТКЛОНЕНО</b> (Админ: {html.escape(admin_name)})"
    )

    if callback.message.photo:
        await callback.message.edit_caption(
            caption=caption, reply_markup=None, parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            text=caption, reply_markup=None, parse_mode="HTML"
        )


# === REST API ENDPOINTS ===
async def api_healthcheck(request):
    return web.json_response({"status": "ok", "message": "RFC Bot Server is running"})


async def api_get_requisites(request):
    req = get_current_requisites()
    return web.json_response({
        "phone": req["phone"],
        "recipient": req["recipient"],
        "status": "ok"
    })


async def api_get_settings(request):
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return web.json_response(data)
    
    return web.json_response({
        "fee": "1000",
        "fights_date": "29 июля",
        "main_card_date": "30 июля",
        "rules": ""
    })


async def api_save_settings(request):
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user_info = verify_telegram_data(init_data)

    if not user_info:
        return web.json_response(
            {"error": "Ошибка авторизации: поддельные или отсутствующие данные Telegram"},
            status=401
        )

    user_id = str(user_info.get('id', ''))
    if user_id not in ADMIN_IDS:
        return web.json_response(
            {"error": f"Доступ запрещен: ваш ID ({user_id}) не является администратором"},
            status=403
        )

    data = await request.json()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return web.json_response({"status": "ok"})


async def api_submit_receipt(request):
    reader = await request.multipart()
    file_bytes = None
    filename = "receipt.jpg"
    code, user_id, user_name = "Не указан", "0", "Участник"

    async for field in reader:
        if field.name == "file":
            filename = field.filename or "receipt.jpg"
            file_bytes = await field.read()
        elif field.name == "code":
            code = await field.text()
        elif field.name == "user_id":
            user_id = await field.text()
        elif field.name == "user_name":
            user_name = await field.text()

    if user_id != "0":
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'pending')",
                (int(user_id), code),
            )
            conn.commit()

    safe_user_name = html.escape(user_name)
    safe_code = html.escape(code)
    safe_user_id = html.escape(str(user_id))

    caption = (
        f"🔔 <b>Новая заявка на оплату из WebApp!</b>\n\n"
        f"👤 <b>Участник:</b> {safe_user_name}\n"
        f"🔑 <b>Код перевода:</b> <code>{safe_code}</code>\n"
        f"🆔 <b>TG ID:</b> <code>{safe_user_id}</code>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"pay_approve:{user_id}:{code}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"pay_reject:{user_id}:{code}",
            ),
        ]]
    )

    if file_bytes:
        photo = BufferedInputFile(file_bytes, filename=filename)
        await bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=photo,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    else:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=caption,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    return web.json_response({"status": "ok"})


async def api_check_payment(request):
    user_id = request.query.get("user_id")
    if not user_id:
        return web.json_response({"error": "No user_id"}, status=400)

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM payments WHERE user_id = ?", (int(user_id),)
        )
        row = cursor.fetchone()

    status = row["status"] if row else "not_found"
    return web.json_response({"status": status, "is_paid": status == "confirmed"})


# === ЗАПУСК ===
@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
        except Exception as e:
            logging.error(f"Error handling request: {e}")
            response = web.json_response({"error": str(e)}, status=500)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


async def main():
    app = web.Application(
        middlewares=[cors_middleware],
        client_max_size=1024 * 1024 * 100
    )

    app.router.add_get("/", api_healthcheck)
    app.router.add_get("/api/get_requisites", api_get_requisites)
    app.router.add_get("/api/get_settings", api_get_settings)
    app.router.add_post("/api/save_settings", api_save_settings)
    app.router.add_post("/api/submit_receipt", api_submit_receipt)
    app.router.add_get("/api/check_payment", api_check_payment)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    logging.info(f"REST API запущен на http://{HOST}:{PORT}")
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")