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
import random
from collections import deque

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ====================== НАСТРОЙКИ ======================
# Токен берётся из переменной окружения (в коде его хранить нельзя)
TOKEN = os.getenv("BOT_TOKEN")

# Тексты кнопок
BTN_SEARCH = "Искать собеседника"
BTN_STOP = "Завершить"
BTN_NEXT = "Следующий"

# Капча после медиа: если пользователь отправил фото, видео (или кружок) в диалоге
# и собеседник завершил диалог (/stop или /next), перед новым поиском нужна проверка.
# True — капча также нужна, если пользователь сам завершил диалог после видео.
CAPTCHA_ON_OWN_END = False

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
sent_video: set[int] = set()    # кто отправлял видео в текущем диалоге
need_captcha: set[int] = set()  # кому нужно пройти капчу перед новым поиском
captcha_answers: dict[int, int] = {}  # user_id -> правильный ответ на текущую капчу

router = Router()


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
async def safe_send(bot: Bot, chat_id: int, text: str, kb=None) -> bool:
    """Отправка сообщения; False, если пользователь заблокировал бота."""
    try:
        await bot.send_message(chat_id, text, reply_markup=kb)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


def make_captcha(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Создаёт простой пример со сложением и 4 вариантами ответа."""
    a, b = random.randint(2, 9), random.randint(2, 9)
    answer = a + b
    options = {answer}
    while len(options) < 4:
        options.add(max(2, answer + random.randint(-5, 5)))
    options = list(options)
    random.shuffle(options)
    captcha_answers[user_id] = answer
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=str(o), callback_data=f"cap:{o}") for o in options]]
    )
    return f"🤖 Проверка: сколько будет {a} + {b}?", kb


async def send_captcha(bot: Bot, user_id: int) -> None:
    text, kb = make_captcha(user_id)
    await safe_send(bot, user_id, text, kb)


async def notify_left(bot: Bot, partner: int) -> None:
    """Сообщает собеседнику, что диалог завершён (и просит капчу, если нужно)."""
    if partner in need_captcha:
        await safe_send(
            bot, partner,
            "Собеседник завершил диалог. Чтобы продолжить, пройдите быструю проверку 👇",
            kb_idle,
        )
        await send_captcha(bot, partner)
    else:
        await safe_send(bot, partner, "Собеседник завершил диалог.", kb_idle)


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
            # Собеседник отправлял видео, а диалог завершил другой -> капча
            if partner in sent_video:
                need_captcha.add(partner)
            if CAPTCHA_ON_OWN_END and user_id in sent_video:
                need_captcha.add(user_id)
        # Новый диалог начнётся с чистого листа
        sent_video.discard(user_id)
        if partner is not None:
            sent_video.discard(partner)
        return partner, was_waiting


async def start_search(user_id: int, bot: Bot) -> None:
    """Ставит пользователя в очередь или сразу соединяет с ожидающим."""
    # Сначала нужно пройти капчу, если она требуется
    if user_id in need_captcha:
        await safe_send(bot, user_id, "Сначала пройдите проверку 👇", kb_idle)
        await send_captcha(bot, user_id)
        return

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
        if user_id in need_captcha:  # только при CAPTCHA_ON_OWN_END = True
            await send_captcha(bot, user_id)
        await notify_left(bot, partner)
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
        await notify_left(bot, partner)
    # Сразу ищем нового (если нужна капча, start_search её запросит)
    await start_search(user_id, bot)


# ====================== КАПЧА (нажатие кнопки с ответом) ======================
@router.callback_query(F.data.startswith("cap:"))
async def on_captcha(call: CallbackQuery, bot: Bot) -> None:
    user_id = call.from_user.id
    expected = captcha_answers.get(user_id)

    if expected is None:
        await call.answer("Проверка уже пройдена или устарела.")
        return

    chosen = int(call.data.split(":")[1])
    if chosen == expected:
        captcha_answers.pop(user_id, None)
        need_captcha.discard(user_id)
        await call.answer("Верно!")
        try:
            await call.message.edit_text("✅ Проверка пройдена.")
        except TelegramBadRequest:
            pass
        await safe_send(bot, user_id, "Теперь можно искать собеседника.", kb_idle)
    else:
        # Неверно — выдаём новый пример
        text, kb = make_captcha(user_id)
        await call.answer("Неверно, попробуйте ещё раз.")
        try:
            await call.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass


# ====================== ПЕРЕСЫЛКА СООБЩЕНИЙ ======================
@router.message(F.chat.type == "private")
async def relay(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    partner = partners.get(user_id)

    if partner is None:
        if user_id in need_captcha:
            await send_captcha(bot, user_id)
            return
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
        # Запоминаем, что пользователь отправлял фото/видео/кружок в этом диалоге
        if message.photo or message.video or message.video_note:
            sent_video.add(user_id)
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
