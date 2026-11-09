"""
Анонимный Telegram-бот для общения 1 на 1 (aiogram 3).

КАК ЗАПУСТИТЬ
1. Создайте бота у @BotFather командой /newbot и скопируйте токен.
2. Задайте токен в переменной окружения BOT_TOKEN
   (Linux/macOS: export BOT_TOKEN="ваш_токен", Windows: set BOT_TOKEN=ваш_токен;
   на Koyeb — в разделе Environment variables).
3. Установите зависимость:   pip install -U aiogram
4. Запустите:               python main.py
"""

import asyncio
import logging
import os
from collections import deque

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

# ====================== НАСТРОЙКИ ======================
# Токен берётся из переменной окружения (в коде его хранить нельзя)
TOKEN = os.getenv("BOT_TOKEN")

# Тексты кнопок
BTN_SEARCH = "Искать собеседника"
BTN_STOP = "Завершить"
BTN_NEXT = "Следующий"

# ====================== КЛАВИАТУРЫ ======================
# Когда пользователь не в диалоге
kb_idle = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_SEARCH)]],
    resize_keyboard=True,
)
# Когда пользователь в диалоге
kb_chat = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_NEXT), KeyboardButton(text=BTN_STOP)]],
    resize_keyboard=True,
)
# Когда пользователь ждёт в очереди (кнопка «Завершить» отменяет поиск)
kb_wait = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_STOP)]],
    resize_keyboard=True,
)

# ====================== СОСТОЯНИЕ (в памяти) ======================
waiting: deque[int] = deque()   # очередь ожидающих собеседника
partners: dict[int, int] = {}   # user_id -> user_id собеседника (в обе стороны)
lock = asyncio.Lock()           # защита от гонок при одновременных запросах

router = Router()


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
async def safe_send(bot: Bot, chat_id: int, text: str, kb=None) -> bool:
    """Отправка сообщения; False, если пользователь заблокировал бота."""
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


async def leave(user_id: int) -> tuple[int | None, bool]:
    """
    Убирает пользователя из очереди и/или из диалога.
    Возвращает (id бывшего собеседника или None, был ли в очереди).
    """
    async with lock:
        was_waiting = user_id in waiting
        if was_waiting:
            waiting.remove(user_id)
        partner = partners.pop(user_id, None)
        if partner is not None:
            partners.pop(partner, None)
        return partner, was_waiting


async def start_search(user_id: int, bot: Bot) -> None:
    """Ставит пользователя в очередь или сразу соединяет с ожидающим."""
    async with lock:
        if user_id in partners:
            state = "in_chat"
        elif user_id in waiting:
            state = "already_waiting"
        elif waiting:
            partner = waiting.popleft()
            partners[user_id] = partner
            partners[partner] = user_id
            state = "matched"
        else:
            waiting.append(user_id)
            state = "queued"
            partner = None

    if state == "in_chat":
        await safe_send(bot, user_id, "Вы уже в диалоге. Нажмите «Следующий» или «Завершить».", kb_chat)
    elif state == "already_waiting":
        await safe_send(bot, user_id, "Вы уже в очереди, ищем собеседника…", kb_wait)
    elif state == "queued":
        await safe_send(bot, user_id, "🔍 Ищем собеседника…", kb_wait)
    else:  # matched
        text = "✅ Собеседник найден! Можете общаться. Все сообщения анонимны."
        ok_me = await safe_send(bot, user_id, text, kb_chat)
        ok_partner = await safe_send(bot, partner, text, kb_chat)
        # Если кто-то из двоих заблокировал бота — разрываем пару
        if not ok_me or not ok_partner:
            await leave(user_id)
            if ok_me:
                await safe_send(bot, user_id, "Собеседник недоступен. Нажмите «Искать собеседника».", kb_idle)
            if ok_partner:
                await safe_send(bot, partner, "Собеседник недоступен. Нажмите «Искать собеседника».", kb_idle)


# ====================== КОМАНДЫ И КНОПКИ ======================
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Это анонимный чат. Нажмите «Искать собеседника» (или /search), "
        "чтобы найти случайного собеседника.\n\n"
        "/search — поиск\n/next — следующий собеседник\n/stop — завершить диалог",
        reply_markup=kb_idle,
    )


@router.message(Command("search"), F.chat.type == "private")
@router.message(F.text == BTN_SEARCH, F.chat.type == "private")
async def cmd_search(message: Message, bot: Bot) -> None:
    await start_search(message.from_user.id, bot)


@router.message(Command("stop"), F.chat.type == "private")
@router.message(F.text == BTN_STOP, F.chat.type == "private")
async def cmd_stop(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner, was_waiting = await leave(user_id)

    if partner is not None:
        await safe_send(bot, user_id, "Диалог завершён.", kb_idle)
        await safe_send(bot, partner, "Собеседник завершил диалог.", kb_idle)
    elif was_waiting:
        await safe_send(bot, user_id, "Поиск отменён.", kb_idle)
    else:
        await safe_send(bot, user_id, "Вы сейчас не в диалоге.", kb_idle)


@router.message(Command("next"), F.chat.type == "private")
@router.message(F.text == BTN_NEXT, F.chat.type == "private")
async def cmd_next(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner, _ = await leave(user_id)
    if partner is not None:
        await safe_send(bot, partner, "Собеседник завершил диалог.", kb_idle)
    # Сразу ищем нового
    await start_search(user_id, bot)


# ====================== ПЕРЕСЫЛКА СООБЩЕНИЙ ======================
@router.message(F.chat.type == "private")
async def relay(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner = partners.get(user_id)

    if partner is None:
        # Неизвестная команда или сообщение вне диалога
        await message.answer("Вы не в диалоге. Нажмите «Искать собеседника».", reply_markup=kb_idle)
        return

    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. Доступно: /search, /next, /stop.")
        return

    try:
        # copy_to отправляет копию сообщения БЕЗ пометки «переслано от…»,
        # поэтому имя, username и ID отправителя полностью скрыты.
        # Работает для текста, стикеров, фото, голосовых, видео, кружков, файлов и т.д.
        await message.copy_to(partner)
    except TelegramForbiddenError:
        # Собеседник заблокировал бота — завершаем диалог
        await leave(user_id)
        await message.answer("Собеседник недоступен. Диалог завершён.", reply_markup=kb_idle)
    except TelegramBadRequest:
        await message.answer("⚠️ Этот тип сообщения нельзя передать собеседнику.")


# ====================== ЗАПУСК ======================
async def start_web() -> None:
    """Мини HTTP-сервер для health check (нужен хостингам типа Koyeb Web Service)."""
    async def health(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await start_web()
    # Обязательно перед стартом поллинга: убираем вебхук и старые апдейты
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
