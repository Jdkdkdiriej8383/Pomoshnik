# main.py
import asyncio
import sqlite3
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# === НАСТРОЙКИ ИЗ config.py ===
import config

BOT_TOKEN = config.BOT_TOKEN
TEACHER_ID = config.TEACHER_ID
TEACHER_TIMEZONE_OFFSET = config.TEACHER_TIMEZONE_OFFSET
DEFAULT_CHANNEL = config.CHANNEL_ID

# === БОТ И ДИСПЕТЧЕР ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === СОСТОЯНИЕ БОТА ===
bot_active = True

# === ТЕКУЩИЙ КАНАЛ ===
current_channel = DEFAULT_CHANNEL

# === БАЗА ДАННЫХ ===
conn = sqlite3.connect("school_bot.db", check_same_thread=False)
cursor = conn.cursor()

# Основная таблица пользователей
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        role TEXT,
        status TEXT,  -- present, absent, unknown
        reason TEXT,
        approved INTEGER DEFAULT 0
    )
''')

# Список дежурных (очередь)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS duty_roster (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    )
''')

# Хранение message_id сообщения о дежурстве
cursor.execute('''
    CREATE TABLE IF NOT EXISTS duty_message (
        id INTEGER PRIMARY KEY,
        message_id INTEGER
    )
''')

# Настройки (хранение флагов)
cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
''')

# === Загрузка настроек ===
def load_setting(key: str, default: str):
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cursor.fetchone()
    return row[0] if row else default

def save_setting(key: str, value: str):
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

# Инициализация канала
current_channel = load_setting("channel", DEFAULT_CHANNEL)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_duty_list():
    """Получить список дежурных по порядку"""
    cursor.execute("SELECT name FROM duty_roster ORDER BY id ASC")
    return [row[0] for row in cursor.fetchall()]

def add_to_duty_roster(name: str):
    """Добавить в конец списка"""
    cursor.execute("INSERT INTO duty_roster (name) VALUES (?)", (name,))
    conn.commit()

def remove_from_duty_roster(name: str):
    """Удалить из списка дежурных"""
    cursor.execute("DELETE FROM duty_roster WHERE name=?", (name,))
    conn.commit()

def clear_duty_roster():
    """Очистить список"""
    cursor.execute("DELETE FROM duty_roster")
    conn.commit()

def remove_first_from_duty():
    """Удалить и вернуть первого"""
    names = get_duty_list()
    if not names:
        return None
    first = names[0]
    cursor.execute("DELETE FROM duty_roster WHERE rowid IN (SELECT rowid FROM duty_roster LIMIT 1)")
    conn.commit()
    return first

def add_to_end_of_duty(name: str):
    """Добавить в конец после отчёта"""
    cursor.execute("INSERT INTO duty_roster (name) VALUES (?)", (name,))
    conn.commit()

def save_duty_message_id(message_id: int):
    """Сохранить ID сообщения в канале"""
    cursor.execute("INSERT OR REPLACE INTO duty_message (id, message_id) VALUES (1, ?)", (message_id,))
    conn.commit()

def get_duty_message_id() -> int:
    """Получить сохранённый message_id"""
    cursor.execute("SELECT message_id FROM duty_message WHERE id=1")
    row = cursor.fetchone()
    return row[0] if row else None

# === СОСТОЯНИЯ FSM ===
class Registration(StatesGroup):
    awaiting_name = State()
    awaiting_reason = State()
    awaiting_duty_name = State()
    awaiting_delete_name = State()
    awaiting_delete_confirm = State()

# === КЛАВИАТУРЫ ===
def get_student_kb():
    if bot_active:
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="✅ Приду в школу")],
            [KeyboardButton(text="❌ Не приду")],
            [KeyboardButton(text="🧹 Отчитаться о дежурстве")]
        ])
    else:
        return types.ReplyKeyboardRemove()

def get_teacher_kb():
    if bot_active:
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="📋 Список класса")],
            [KeyboardButton(text="➕ Добавить дежурного")],
            [KeyboardButton(text="🗑️ Удалить ученика")],
            [KeyboardButton(text="📤 Повторить отчёт в канал")],
            [KeyboardButton(text="🔴 Стоп")],
            [KeyboardButton(text="ℹ️ Помощь")]
        ])
    else:
        return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
            [KeyboardButton(text="🟢 Старт")]
        ])

def get_approval_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Согласить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{user_id}")
        ]
    ])

def get_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить всё", callback_data="confirm_delete_all"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")
        ]
    ])

# === ПРОВЕРКА ВЫХОДНЫХ ===
def is_weekend():
    return datetime.now().weekday() >= 5

# === НАЗНАЧЕНИЕ ДЕЖУРНОГО В 8:25 ===
async def assign_daily_duty():
    if not bot_active or is_weekend():
        return

    # Отмечаем, что ротация началась
    save_setting("rotation_started", "true")

    roster = get_duty_list()
    if not roster:
        await bot.send_message(TEACHER_ID, "⚠️ Список дежурных пуст.")
        return

    # Получаем тех, кто сегодня придёт
    cursor.execute("SELECT name FROM users WHERE status='present' AND approved=1")
    present_names = [row[0] for row in cursor.fetchall()]

    if not present_names:
        msg = "🧹 Дежурства на сегодня:\nНикто не приходит."
        try:
            await bot.send_message(current_channel, msg)
        except Exception as e:
            await bot.send_message(TEACHER_ID, f"❌ Ошибка в канале: {e}")
        await bot.send_message(TEACHER_ID, "🚫 Сегодня никто не приходит — дежурных нет.")
        return

    # Находим первого в списке, кто приходит
    daily_duty = None
    for name in roster:
        if name in present_names:
            daily_duty = name
            break

    if not daily_duty:
        daily_duty = present_names[0]  # Если все дежурные отсутствуют
        await bot.send_message(TEACHER_ID, f"⚠️ Назначен дежурным (из пришедших): {daily_duty}")

    # Удаляем из начала очереди
    remove_first_from_duty()

    # Находим user_id
    cursor.execute("SELECT user_id FROM users WHERE name=?", (daily_duty,))
    row = cursor.fetchone()
    if not row:
        await bot.send_message(TEACHER_ID, f"❌ Ошибка: {daily_duty} не найден.")
        return
    user_id = row[0]

    # Отправляем в канал
    msg = f"🧹 Дежурства на сегодня:\nДежурит: {daily_duty}"
    try:
        sent = await bot.send_message(current_channel, msg)
        save_duty_message_id(sent.message_id)
    except Exception as e:
        await bot.send_message(TEACHER_ID, f"❌ Ошибка в канале: {e}")

    # Личное сообщение
    try:
        await bot.send_message(user_id, "🧹 Вы дежурный сегодня! Не забудьте отчитаться о дежурстве.")
    except Exception as e:
        await bot.send_message(TEACHER_ID, f"⚠️ Не удалось оповестить {daily_duty}: {e}")

    # Уведомление учителю
    await bot.send_message(TEACHER_ID, f"✅ Дежурный назначен: <b>{daily_duty}</b>", parse_mode="HTML")

# === ПЛАНИРОВЩИК ===
async def run_scheduler():
    while True:
        if bot_active:
            now = datetime.now()
            hour_local = (now.hour + TEACHER_TIMEZONE_OFFSET) % 24
            minute, second = now.minute, now.second

            if not is_weekend():
                if hour_local == 8 and minute == 25 and second < 10:
                    await assign_daily_duty()
                    await asyncio.sleep(60)
        await asyncio.sleep(10)

# === /start ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if user_id == TEACHER_ID:
        cursor.execute("INSERT OR IGNORE INTO users (user_id, name, role, approved) VALUES (?, 'Классный руководитель', 'teacher', 1)", (user_id,))
        conn.commit()
        await message.answer("👨‍🏫 Добро пожаловать!", reply_markup=get_teacher_kb())
        return

    cursor.execute("SELECT role, approved FROM users WHERE user_id=?", (user_id,))
    result = cursor.fetchone()

    if result:
        role, approved = result
        if role == "student":
            kb = get_student_kb() if approved else None
            await message.answer(
                "🎓 Добро пожаловать!" if approved else "⏳ Заявка на рассмотрении.",
                reply_markup=kb
            )
        return

    await message.answer("👋 Введите имя (например: Иван Иванов):")
    await state.set_state(Registration.awaiting_name)

# === Регистрация имени ===
@dp.message(Registration.awaiting_name)
async def process_name(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте.")
        await state.clear()
        return

    name = message.text.strip()
    if not re.fullmatch(r"^[А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+)+$", name, re.IGNORECASE):
        await message.answer("📛 Имя: две части, кириллица. Пример: Анна Петрова")
        return

    user_id = message.from_user.id
    cursor.execute("INSERT OR REPLACE INTO users (user_id, name, role, status) VALUES (?, ?, 'student', 'unknown')", (user_id, name))
    conn.commit()

    await bot.send_message(
        TEACHER_ID,
        f"🆕 Заявка:\nИмя: {name}\nЮзер: @{message.from_user.username or 'нет'}",
        reply_markup=get_approval_kb(user_id)
    )
    await message.answer("📨 Заявка отправлена.")
    await state.clear()

# === Одобрение / Отклонение ===
@dp.callback_query(F.data.startswith("approve_"))
async def approve_student(callback: types.CallbackQuery):
    if not bot_active:
        await callback.answer("🔴 Бот остановлен.", show_alert=True)
        return
    user_id = int(callback.data.split("_")[1])
    cursor.execute("UPDATE users SET approved=1 WHERE user_id=?", (user_id,))
    conn.commit()

    cursor.execute("SELECT name FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Ошибка")
        return
    name = row[0]

    # Добавляем в список
    add_to_duty_roster(name)

    # Если это первый — ок, иначе проверим, нужно ли сортировать
    rotation_started = load_setting("rotation_started", "false")
    if rotation_started == "false" and len(get_duty_list()) > 1:
        sorted_names = sorted(get_duty_list())
        clear_duty_roster()
        for n in sorted_names:
            add_to_duty_roster(n)
        await bot.send_message(TEACHER_ID, "📋 Список дежурных отсортирован по алфавиту.")

    await bot.send_message(user_id, "✅ Вы приняты! Вы в списке дежурных.", reply_markup=get_student_kb())
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ Принято.")
    await callback.answer("Принято")

@dp.callback_query(F.data.startswith("decline_"))
async def decline_student(callback: types.CallbackQuery):
    if not bot_active:
        await callback.answer("🔴 Бот остановлен.", show_alert=True)
        return
    user_id = int(callback.data.split("_")[1])
    cursor.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    conn.commit()
    await bot.send_message(user_id, "❌ Ваша заявка отклонена.")
    await callback.message.edit_text(f"{callback.message.text}\n\n❌ Отклонено.")
    await callback.answer("Отклонено")

# === Учитель: Команды ===
@dp.message(F.text == "📋 Список класса")
async def list_students(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Но вы можете посмотреть список.", reply_markup=get_teacher_kb())
        return
    cursor.execute("SELECT name, status, reason FROM users WHERE role='student' AND approved=1 ORDER BY name ASC")
    students = cursor.fetchall()
    if not students:
        await message.answer("📚 Класс пуст.")
        return
    lines = []
    for name, status, reason in students:
        if status == "present":
            lines.append(f"{name} — ✅ придёт")
        elif status == "absent":
            lines.append(f"{name} — ❌ не придёт ({reason})")
        else:
            lines.append(f"{name} — ❓ неизвестно")
    report = "\n".join(lines)
    await message.answer(f"📋 Список класса:\n\n{report}")

@dp.message(F.text == "➕ Добавить дежурного")
async def prompt_duty_name(message: types.Message, state: FSMContext):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        return
    await message.answer("✏️ Введите имя ученика:")
    await state.set_state(Registration.awaiting_duty_name)

@dp.message(Registration.awaiting_duty_name)
async def set_duty(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        await state.clear()
        return
    name = message.text.strip()
    cursor.execute("SELECT user_id FROM users WHERE name=? AND approved=1", (name,))
    row = cursor.fetchone()
    if row:
        await bot.send_message(row[0], "🧹 Вы назначены дежурным!")
        await message.answer(f"🧹 {name} назначен дежурным.")
    else:
        await message.answer("❌ Ученик не найден.")
    await state.clear()

@dp.message(F.text == "🗑️ Удалить ученика")
async def prompt_delete_name(message: types.Message, state: FSMContext):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        return
    await message.answer("✏️ Введите имя или <code>@all</code>:", parse_mode="HTML")
    await state.set_state(Registration.awaiting_delete_name)

@dp.message(Registration.awaiting_delete_name)
async def delete_student(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        await state.clear()
        return
    name = message.text.strip()
    if name == "@all":
        await message.answer("⚠️ Точно удалить всех?", reply_markup=get_confirm_kb(), parse_mode="HTML")
        await state.set_state(Registration.awaiting_delete_confirm)
    else:
        cursor.execute("SELECT user_id FROM users WHERE name=? AND role='student'", (name,))
        row = cursor.fetchone()
        if row:
            user_id = row[0]
            try:
                await bot.send_message(user_id, "🚫 Вы удалены из класса.", reply_markup=types.ReplyKeyboardRemove())
            except Exception as e:
                print(f"[Ошибка] {e}")
        cursor.execute("DELETE FROM users WHERE name=? AND role='student'", (name,))
        remove_from_duty_roster(name)
        conn.commit()
        await message.answer(f"✅ Удалён: {name}" if cursor.rowcount else "❌ Не найден.")
        await state.clear()

@dp.callback_query(F.data == "confirm_delete_all")
async def confirm_delete_all(callback: types.CallbackQuery, state: FSMContext):
    cursor.execute("SELECT user_id FROM users WHERE role='student'")
    students = cursor.fetchall()
    cursor.execute("DELETE FROM users WHERE role='student'")
    clear_duty_roster()
    conn.commit()
    for (user_id,) in students:
        try:
            await bot.send_message(user_id, "🚫 Все данные сброшены.", reply_markup=types.ReplyKeyboardRemove())
        except Exception as e:
            print(f"[Ошибка] {e}")
    await callback.message.edit_text("✅ Все ученики и список дежурных удалены.")
    await callback.answer("Готово")
    await state.clear()

@dp.callback_query(F.data == "cancel_delete")
async def cancel_delete(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text
    await callback.message.edit_text("❌ Отменено")
    await callback.answer("Отмена")
    await state.clear()

# === Повторить отчёт в канал ===
@dp.message(F.text == "📤 Повторить отчёт в канал")
async def resend_channel_report(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return
    if not bot_active:
        await message.answer("🔴 Бот остановлен.", reply_markup=get_teacher_kb())
        return
    await assign_daily_duty()
    await message.answer("📤 Запрос на назначение дежурного отправлен учителю.")

# === Управление: Стоп / Старт ===
@dp.message(F.text == "🔴 Стоп")
async def stop_bot(message: types.Message):
    global bot_active
    if message.from_user.id != TEACHER_ID:
        return
    bot_active = False
    await message.answer("🔴 Бот остановлен. Ученики больше не могут отмечаться.", reply_markup=get_teacher_kb())

@dp.message(F.text == "🟢 Старт")
async def start_bot(message: types.Message):
    global bot_active
    if message.from_user.id != TEACHER_ID:
        return
    bot_active = True
    await message.answer("🟢 Бот запущен. Ученики могут отмечаться.", reply_markup=get_teacher_kb())

# === Команда: /set_channel ===
@dp.message(Command("set_channel"))
async def set_channel(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.answer("📌 Используйте: <code>/set_channel @название_канала</code>", parse_mode="HTML")
        return
    channel = args[1].strip()
    if not (channel.startswith("@") or channel.startswith("https://t.me/")):
        await message.answer("📛 Некорректное имя канала. Пример: <code>@my_school_class</code>", parse_mode="HTML")
        return
    global current_channel
    current_channel = channel
    save_setting("channel", current_channel)
    await message.answer(f"✅ Канал изменён: {current_channel}")

# === Помощь — только для учителя ===
@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def teacher_help(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return

    help_text = """
👨‍🏫 <b>Помощь для классного руководителя</b>

📌 <b>Команды:</b>

/start — запустить бота  
/set_channel @канал — изменить канал  
/help — это сообщение  
/reset_duty_list — сброс к алфавиту  
/next_duty — кто следующий в очереди

📌 <b>Кнопки:</b>

📋 Список класса — кто приходит/не приходит  
➕ Добавить дежурного — вручную  
🗑️ Удалить ученика — по имени или @all  
📤 Повторить отчёт в канал — выбрать дежурного сейчас  
🔴 Стоп / 🟢 Старт — включить/выключить  
ℹ️ Помощь — эта подсказка

⏰ В 8:25 — автоматически назначается дежурный из пришедших  
🧹 После «отчитаться» — ученик перемещается в конец очереди
"""

    await message.answer(help_text, parse_mode="HTML")

# === Сброс списка к алфавиту ===
@dp.message(Command("reset_duty_list"))
async def cmd_reset_duty_list(message: types.Message):
    if message.from_user.id != TEACHER_ID:
        return

    names = get_duty_list()
    if not names:
        await message.answer("📋 Список дежурных пуст.")
        return

    # Сортируем по алфавиту
    sorted_names = sorted(names)
    clear_duty_roster()
    for name in sorted_names:
        add_to_duty_roster(name)

    # Сбрасываем флаг ротации
    save_setting("rotation_started", "false")

    numbered = "\n".join([f"{i+1}. {name}" for i, name in enumerate(sorted_names)])
    await message.answer(f"✅ Список сброшен к алфавитному порядку:\n\n{numbered}")

# === Кто следующий в очереди? ===
@dp.message(Command("next_duty"))
async def cmd_next_duty(message: types.Message):
    names = get_duty_list()
    if not names:
        await message.answer("📋 Список дежурных пуст.")
        return

    next_name = names[0]

    # Проверяем статус
    cursor.execute("SELECT status FROM users WHERE name=? AND approved=1", (next_name,))
    row = cursor.fetchone()
    if not row:
        status_text = " (неизвестно)"
    else:
        status = row[0]
        status_text = " ✅ придёт" if status == "present" else " ❌ не придёт" if status == "absent" else " ❓ неизвестно"

    await message.answer(f"➡️ Следующий в очереди: <b>{next_name}</b>{status_text}", parse_mode="HTML")

# === Ученик: Команды ===
@dp.message(F.text == "✅ Приду в школу")
async def mark_present(message: types.Message):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте команды от классного руководителя.")
        return
    cursor.execute("UPDATE users SET status='present', reason=NULL WHERE user_id=?", (message.from_user.id,))
    conn.commit()
    await message.answer("✅ Вы отметились как 'приду'.")

@dp.message(F.text == "❌ Не приду")
async def prompt_absent_reason(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте.")
        return
    await message.answer("📝 Укажите причину отсутствия:")
    await state.set_state(Registration.awaiting_reason)

@dp.message(Registration.awaiting_reason)
async def mark_absent(message: types.Message, state: FSMContext):
    if not bot_active:
        await message.answer("🔴 Бот остановлен.")
        await state.clear()
        return
    reason = message.text.strip()
    cursor.execute("UPDATE users SET status='absent', reason=? WHERE user_id=?", (reason, message.from_user.id))
    conn.commit()
    await message.answer(f"❌ Вы отмечены как 'не приду'. Причина: {reason}")
    await state.clear()

@dp.message(F.text == "🧹 Отчитаться о дежурстве")
async def report_duty(message: types.Message):
    if not bot_active:
        await message.answer("🔴 Бот остановлен. Ожидайте.")
        return

    cursor.execute("SELECT name FROM users WHERE user_id=?", (message.from_user.id,))
    row = cursor.fetchone()
    if not row:
        await message.answer("❌ Вы не зарегистрированы.")
        return
    name = row[0]

    await message.answer("🧹 Вы отчитались о дежурстве! Молодец! 💪")

    # Редактируем сообщение в канале
    msg_id = get_duty_message_id()
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=current_channel,
                message_id=msg_id,
                text="🧹 Дежурства на сегодня:\nДежурный не назначен"
            )
        except Exception as e:
            print(f"[Ошибка редактирования] {e}")

    # Перемещаем в конец очереди
    add_to_end_of_duty(name)

# === ЗАПУСК ===
async def main():
    asyncio.create_task(run_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
