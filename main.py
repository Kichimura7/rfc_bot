import asyncio
from datetime import datetime
import json
import logging
import os
import sqlite3
from aiohttp import web
from aiogram import Bot, CallbackQuery, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "-1004465872509"))
WEB_APP_BASE_URL = os.getenv(
    "WEB_APP_URL", "https://kichimura7.github.io/rfc_bot/"
)
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
  raise ValueError("ОШИБКА: BOT_TOKEN не указан в файле .env!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === РОТАЦИЯ РЕКВИЗИТОВ (МЕНЯЮТСЯ КАЖДЫЕ 2 ЧАСА) ===
PHONE_NUMBERS = [
    "+7 939 768 50 75",  # Номер 1 (00:00-02:00, 06:00-08:00, ...)
    "+7 900 111 22 33",  # Номер 2 (02:00-04:00, 08:00-10:00, ...) (Укажите нужный)
    "+7 900 444 55 66",  # Номер 3 (04:00-06:00, 10:00-12:00, ...) (Укажите нужный)
]


def get_current_phone():
  current_hour = datetime.now().hour
  index = (current_hour // 2) % len(PHONE_NUMBERS)
  return PHONE_NUMBERS[index]


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


# Одобрение оплаты админом
@dp.callback_query(F.data.startswith("pay_approve:"))
async def process_approve(callback: CallbackQuery):
  await callback.answer("Заявка подтверждена ✅")
  _, user_id_str, code = callback.data.split(":")
  user_id = int(user_id_str)

  # 1. Обновляем статус в БД на confirmed
  with get_db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?,"
        " ?, 'confirmed')",
        (user_id, code),
    )
    conn.commit()

  # 2. Отправляем уведомление пользователю
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
            f"🎉 <b>Оплата (код: {code}) подтверждена!</b>\nВы успешно внесены"
            " в список участников турнира RFC."
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )
  except Exception as e:
    logging.error(
        f"Не удалось отправить сообщение пользователю {user_id}: {e}"
    )

  # 3. Обновляем сообщение в админ-чате
  admin_name = (
      f"@{callback.from_user.username}"
      if callback.from_user.username
      else callback.from_user.full_name
  )
  caption = (
      (callback.message.caption or callback.message.text or "")
      + f"\n\n✅ <b>ПОДТВЕРЖДЕНО</b> (Админ: {admin_name})"
  )

  if callback.message.photo:
    await callback.message.edit_caption(
        caption=caption, reply_markup=None, parse_mode="HTML"
    )
  else:
    await callback.message.edit_text(
        text=caption, reply_markup=None, parse_mode="HTML"
    )


# Отклонение оплаты админом
@dp.callback_query(F.data.startswith("pay_reject:"))
async def process_reject(callback: CallbackQuery):
  await callback.answer("Заявка отклонена ❌")
  _, user_id_str, code = callback.data.split(":")
  user_id = int(user_id_str)

  with get_db() as conn:
    conn.execute(
        "INSERT OR REPLACE INTO payments (user_id, code, status) VALUES (?,"
        " ?, 'rejected')",
        (user_id, code),
    )
    conn.commit()

  try:
    await bot.send_message(
        chat_id=user_id,
        text=(
            f"⚠️ <b>Оплата по коду {code} отклонена.</b>\nПеревод не найден."
            " Свяжитесь с организаторами."
        ),
        parse_mode="HTML",
    )
  except Exception as e:
    logging.error(
        f"Не удалось отправить сообщение пользователю {user_id}: {e}"
    )

  admin_name = (
      f"@{callback.from_user.username}"
      if callback.from_user.username
      else callback.from_user.full_name
  )
  caption = (
      (callback.message.caption or callback.message.text or "")
      + f"\n\n❌ <b>ОТКЛОНЕНО</b> (Админ: {admin_name})"
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
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def handle_options(request):
  return web.Response(status=200, headers=CORS_HEADERS)


# Выдача текущего актуального номера телефона для WebApp
async def api_get_requisites(request):
  return web.json_response(
      {"phone": get_current_phone(), "status": "ok"}, headers=CORS_HEADERS
  )


# Прием чека из WebApp и отправка в чат админов
async def api_submit_receipt(request):
  try:
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
            "INSERT OR REPLACE INTO payments (user_id, code, status) VALUES"
            " (?, ?, 'pending')",
            (int(user_id), code),
        )
        conn.commit()

    caption = (
        f"🔔 <b>Новая заявка на оплату из WebApp!</b>\n\n"
        f"👤 <b>Участник:</b> {user_name}\n"
        f"🔑 <b>Код перевода:</b> <code>{code}</code>\n"
        f"🆔 <b>TG ID:</b> <code>{user_id}</code>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Подтвердить",
                callback_data=f"pay_approve:{user_id}:{code}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить", callback_data=f"pay_reject:{user_id}:{code}"
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

    return web.json_response({"status": "ok"}, headers=CORS_HEADERS)
  except Exception as e:
    logging.error(f"Error in submit_receipt: {e}")
    return web.json_response(
        {"error": str(e)}, status=500, headers=CORS_HEADERS
    )


# Проверка статуса оплаты для фронтенда WebApp
async def api_check_payment(request):
  user_id = request.query.get("user_id")
  if not user_id:
    return web.json_response(
        {"error": "No user_id"}, status=400, headers=CORS_HEADERS
    )

  with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status FROM payments WHERE user_id = ?", (int(user_id),)
    )
    row = cursor.fetchone()

  status = row["status"] if row else "not_found"
  return web.json_response(
      {"status": status, "is_paid": status == "confirmed"}, headers=CORS_HEADERS
  )


# === ЗАПУСК ===
async def main():
  app = web.Application()

  # Роуты реквизитов
  app.router.add_get("/api/get_requisites", api_get_requisites)
  app.router.add_options("/api/get_requisites", handle_options)

  # Остальные роуты
  app.router.add_post("/api/submit_receipt", api_submit_receipt)
  app.router.add_options("/api/submit_receipt", handle_options)
  app.router.add_get("/api/check_payment", api_check_payment)
  app.router.add_options("/api/check_payment", handle_options)

  runner = web.AppRunner(app)
  await runner.setup()
  site = web.TCPSite(runner, HOST, PORT)
  await site.start()

  logging.info(f"🚀 REST API запущен на {HOST}:{PORT}")
  logging.info("🤖 Бот запущен!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    logging.info("Бот остановлен.")