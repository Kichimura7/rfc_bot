import os
import json
import logging
import sqlite3
import asyncio
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile

# Загружаем переменные из .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8892094938:AAH7ONLdQIigBn1DGjvBxYUY92r2GMo7cxc")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004465872509"))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_pending_codes = {}

# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ===
def init_db():
    conn = sqlite3.connect("database.db")
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
    conn.close()

init_db()

# === ХЭНДЛЕРЫ БОТА ===
@dp.message(CommandStart())
async def cmd_start(message: Message):
    web_app_url = "https://kichimura7.github.io/rfc_bot/" 
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🥊 Открыть RFC League", web_app=WebAppInfo(url=web_app_url))]
        ]
    )
    await message.answer("Привет! Нажми на кнопку ниже, чтобы открыть приложение и найти бой:", reply_markup=keyboard)


@dp.message(F.photo)
async def handle_photo_receipt(message: Message):
    user_id = message.from_user.id
    code = user_pending_codes.get(user_id, "Не указан")
    photo_id = message.photo[-1].file_id
    user_name = message.from_user.full_name
    username = f"@{message.from_user.username}" if message.from_user.username else "нет username"

    # Фиксируем статус ожидания в БД
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'pending')", (user_id, code))
    conn.commit()
    conn.close()

    caption = (
        f"🔔 <b>Новая заявка на оплату!</b>\n\n"
        f"👤 <b>Участник:</b> {user_name} ({username})\n"
        f"🔑 <b>Код перевода:</b> <code>{code}</code>\n"
        f"🆔 <b>TG ID:</b> <code>{user_id}</code>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pay_approve:{user_id}:{code}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay_reject:{user_id}:{code}")
        ]
    ])

    try:
        await bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=photo_id,
            caption=caption,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await message.answer("✅ Чек успешно передан организаторам. Ожидайте подтверждения!")
    except Exception as e:
        logging.error(f"Error sending photo to admin chat: {e}")
        await message.answer("⚠️ Ошибка при отправке чека администраторам.")


@dp.callback_query(F.data.startswith("pay_approve:"))
async def process_approve(callback: CallbackQuery):
    _, user_id_str, code = callback.data.split(":")
    user_id = int(user_id_str)

    # Обновляем статус оплаты в базе данных на confirmed
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'confirmed')", (user_id, code))
    conn.commit()
    conn.close()

    web_app_url = "https://kichimura7.github.io/rfc_bot/?paid=true"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥊 Открыть RFC League", web_app=WebAppInfo(url=web_app_url))]
    ])

    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"🎉 <b>Оплата (код: {code}) подтверждена!</b>\nВы успешно внесены в список участников турнира RFC.",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Failed to send approve message to user {user_id}: {e}")

    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n✅ <b>ПОДТВЕРЖДЕНО</b> (Админ: {admin_name})",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Заявка подтверждена")


@dp.callback_query(F.data.startswith("pay_reject:"))
async def process_reject(callback: CallbackQuery):
    _, user_id_str, code = callback.data.split(":")
    user_id = int(user_id_str)

    # Обновляем статус в БД на rejected
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'rejected')", (user_id, code))
    conn.commit()
    conn.close()

    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"⚠️ <b>Оплата по коду {code} отклонена.</b>\nПеревод не найден или сумма указана неверно. Пожалуйста, свяжитесь с организаторами.",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Failed to send reject message to user {user_id}: {e}")

    admin_name = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.full_name
    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ <b>ОТКЛОНЕНО</b> (Админ: {admin_name})",
        reply_markup=None,
        parse_mode="HTML"
    )
    await callback.answer("Заявка отклонена")


# === CORS ЗАГОЛОВКИ ===
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

async def handle_options(request):
    return web.Response(status=200, headers=CORS_HEADERS)


# === API: ПРИЕМ ЧЕКА ИЗ WEB APP ===
async def api_submit_receipt(request):
    try:
        reader = await request.multipart()
        
        file_bytes = None
        filename = "receipt.jpg"
        code = "Не указан"
        user_id = "0"
        user_name = "Участник"

        async for field in reader:
            if field.name == 'file':
                filename = field.filename or "receipt.jpg"
                file_bytes = await field.read()
            elif field.name == 'code':
                code = await field.text()
            elif field.name == 'user_id':
                user_id = await field.text()
            elif field.name == 'user_name':
                user_name = await field.text()

        # Сохраняем заявку в статус pending
        if user_id != "0":
            conn = sqlite3.connect("database.db")
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?, ?, 'pending')", (int(user_id), code))
            conn.commit()
            conn.close()

        caption = (
            f"🔔 <b>Новая заявка на оплату из WebApp!</b>\n\n"
            f"👤 <b>Участник:</b> {user_name}\n"
            f"🔑 <b>Код перевода:</b> <code>{code}</code>\n"
            f"🆔 <b>TG ID:</b> <code>{user_id}</code>"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"pay_approve:{user_id}:{code}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"pay_reject:{user_id}:{code}")
            ]
        ])

        if file_bytes:
            photo = BufferedInputFile(file_bytes, filename=filename)
            await bot.send_photo(
                chat_id=ADMIN_CHAT_ID,
                photo=photo,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        return web.json_response({"status": "ok"}, headers=CORS_HEADERS)

    except Exception as e:
        logging.error(f"Error in api_submit_receipt: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)


# === API: ПРОВЕРКА СТАТУСА ОПЛАТЫ ===
async def api_check_payment(request):
    try:
        user_id = request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400, headers=CORS_HEADERS)

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM payments WHERE user_id = ?", (int(user_id),))
        row = cursor.fetchone()
        conn.close()

        status = row[0] if row else "not_found"
        return web.json_response({"status": status, "is_paid": status == "confirmed"}, headers=CORS_HEADERS)

    except Exception as e:
        logging.error(f"Error in api_check_payment: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)


# === API: РЕГИСТРАЦИЯ И ПОИСК БОЯ ===
async def api_register(request):
    try:
        data = await request.json()
        user_id = data.get("user_id")
        full_name = data.get("full_name")
        club = data.get("club")
        category = data.get("category")

        if not all([user_id, full_name, club, category]):
            return web.json_response({"error": "Missing data"}, status=400, headers=CORS_HEADERS)

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT user_id, full_name, club FROM fighters 
            WHERE category = ? AND status = 'searching' AND user_id != ?
            LIMIT 1
        """, (category, user_id))
        
        opponent = cursor.fetchone()

        if opponent:
            opp_id, opp_name, opp_club = opponent
            
            cursor.execute("""
                INSERT OR REPLACE INTO fighters (user_id, full_name, club, category, status, opponent_id)
                VALUES (?, ?, ?, ?, 'paired', ?)
            """, (user_id, full_name, club, category, opp_id))

            cursor.execute("""
                UPDATE fighters SET status = 'paired', opponent_id = ? WHERE user_id = ?
            """, (user_id, opp_id))

            conn.commit()
            conn.close()

            return web.json_response({
                "status": "paired",
                "my_name": full_name,
                "my_club": club,
                "opponent_name": opp_name,
                "opponent_club": opp_club
            }, headers=CORS_HEADERS)

        else:
            cursor.execute("""
                INSERT OR REPLACE INTO fighters (user_id, full_name, club, category, status, opponent_id)
                VALUES (?, ?, ?, ?, 'searching', NULL)
            """, (user_id, full_name, club, category))

            conn.commit()
            conn.close()

            return web.json_response({"status": "searching"}, headers=CORS_HEADERS)

    except Exception as e:
        logging.error(f"Error in api_register: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)


# === API: ПРОВЕРКА СТАТУСА БОЯ ===
async def api_status(request):
    try:
        user_id = request.query.get("user_id")
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400, headers=CORS_HEADERS)

        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()

        cursor.execute("SELECT full_name, club, status, opponent_id FROM fighters WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()

        if not user:
            conn.close()
            return web.json_response({"status": "not_registered"}, headers=CORS_HEADERS)

        my_name, my_club, status, opponent_id = user

        if status == "paired" and opponent_id:
            cursor.execute("SELECT full_name, club FROM fighters WHERE user_id = ?", (opponent_id,))
            opp = cursor.fetchone()
            conn.close()

            if opp:
                return web.json_response({
                    "status": "paired",
                    "my_name": my_name,
                    "my_club": my_club,
                    "opponent_name": opp[0],
                    "opponent_club": opp[1]
                }, headers=CORS_HEADERS)

        conn.close()
        return web.json_response({"status": status}, headers=CORS_HEADERS)

    except Exception as e:
        logging.error(f"Error in api_status: {e}")
        return web.json_response({"error": str(e)}, status=500, headers=CORS_HEADERS)


# === ЗАПУСК СЕРВЕРА И БОТА ===
async def main():
    app = web.Application()

    # Регистрация и поиск
    app.router.add_post('/api/register', api_register)
    app.router.add_options('/api/register', handle_options)
    
    # Статус боя
    app.router.add_get('/api/status', api_status)
    app.router.add_options('/api/status', handle_options)

    # Оплата
    app.router.add_post('/api/submit_receipt', api_submit_receipt)
    app.router.add_options('/api/submit_receipt', handle_options)
    
    # Проверка оплаты
    app.router.add_get('/api/check_payment', api_check_payment)
    app.router.add_options('/api/check_payment', handle_options)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    
    print("🚀 API сервер запущен на порту 8080!")
    await site.start()

    print("🤖 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())