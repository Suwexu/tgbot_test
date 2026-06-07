import asyncio
import logging
import os
from datetime import datetime
import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

if ADMIN_GROUP_ID:
    ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)

if not BOT_TOKEN or not ADMIN_ID:
    raise ValueError("BOT_TOKEN и ADMIN_ID обязательны")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

admin_states = {}

# ==================== БАЗА ДАННЫХ ====================
import sqlite3

class Database:
    def __init__(self):
        self.conn = sqlite3.connect('bot_database.db', check_same_thread=False)
        self.init_tables()
    
    def init_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS target_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE,
                name TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
                name TEXT,
                content_type TEXT,
                text TEXT,
                photo_file_id TEXT,
                schedule_type TEXT,
                hour INTEGER,
                minute INTEGER,
                interval_minutes INTEGER,
                start_hour INTEGER,
                start_minute INTEGER,
                button_text TEXT,
                edit_message TEXT,
                is_active INTEGER DEFAULT 1,
                last_sent_at TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS button_clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                button_text TEXT,
                reaction_time REAL,
                clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            )
        ''')
        self.conn.commit()
    
    def add_target_group(self, chat_id, name):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO target_groups (chat_id, name) VALUES (?, ?)', (chat_id, name))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_all_target_groups(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, chat_id, name, is_active FROM target_groups')
        return [{'id': r[0], 'chat_id': r[1], 'name': r[2], 'is_active': bool(r[3])} for r in cursor.fetchall()]
    
    def get_target_group(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, chat_id, name, is_active FROM target_groups WHERE id = ?', (group_id,))
        r = cursor.fetchone()
        return {'id': r[0], 'chat_id': r[1], 'name': r[2], 'is_active': bool(r[3])} if r else None
    
    def get_target_group_by_chat_id(self, chat_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, chat_id, name, is_active FROM target_groups WHERE chat_id = ?', (chat_id,))
        r = cursor.fetchone()
        return {'id': r[0], 'chat_id': r[1], 'name': r[2], 'is_active': bool(r[3])} if r else None
    
    def delete_target_group(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM broadcasts WHERE group_id = ?', (group_id,))
        cursor.execute('DELETE FROM target_groups WHERE id = ?', (group_id,))
        self.conn.commit()
    
    def toggle_target_group(self, group_id, is_active):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE target_groups SET is_active = ? WHERE id = ?', (is_active, group_id))
        self.conn.commit()
    
    def add_broadcast(self, group_id, name, content_type, schedule_type,
                      text=None, photo_file_id=None,
                      hour=None, minute=None,
                      interval_minutes=None,
                      start_hour=None, start_minute=None,
                      button_text=None, edit_message=None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO broadcasts
            (group_id, name, content_type, text, photo_file_id, schedule_type,
             hour, minute, interval_minutes, start_hour, start_minute, button_text, edit_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (group_id, name, content_type, text, photo_file_id, schedule_type,
              hour, minute, interval_minutes, start_hour, start_minute, button_text, edit_message))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_all_broadcasts(self, group_id=None):
        cursor = self.conn.cursor()
        if group_id:
            cursor.execute('SELECT * FROM broadcasts WHERE group_id = ?', (group_id,))
        else:
            cursor.execute('SELECT * FROM broadcasts')
        rows = cursor.fetchall()
        return [{
            'id': r[0], 'group_id': r[1], 'name': r[2], 'content_type': r[3],
            'text': r[4], 'photo_file_id': r[5], 'schedule_type': r[6],
            'hour': r[7], 'minute': r[8], 'interval_minutes': r[9],
            'start_hour': r[10], 'start_minute': r[11],
            'button_text': r[12], 'edit_message': r[13],
            'is_active': bool(r[14]), 'last_sent_at': r[15]
        } for r in rows]
    
    def get_broadcast(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM broadcasts WHERE id = ?', (broadcast_id,))
        r = cursor.fetchone()
        if r:
            return {
                'id': r[0], 'group_id': r[1], 'name': r[2], 'content_type': r[3],
                'text': r[4], 'photo_file_id': r[5], 'schedule_type': r[6],
                'hour': r[7], 'minute': r[8], 'interval_minutes': r[9],
                'start_hour': r[10], 'start_minute': r[11],
                'button_text': r[12], 'edit_message': r[13],
                'is_active': bool(r[14]), 'last_sent_at': r[15]
            }
        return None
    
    def update_broadcast(self, broadcast_id, **kwargs):
        cursor = self.conn.cursor()
        allowed = ['is_active']
        updates = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k} = ?")
                values.append(v)
        if updates:
            values.append(broadcast_id)
            cursor.execute(f"UPDATE broadcasts SET {', '.join(updates)} WHERE id = ?", values)
            self.conn.commit()
    
    def delete_broadcast(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM button_clicks WHERE broadcast_id = ?', (broadcast_id,))
        cursor.execute('DELETE FROM broadcasts WHERE id = ?', (broadcast_id,))
        self.conn.commit()
    
    def update_last_sent(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('UPDATE broadcasts SET last_sent_at = CURRENT_TIMESTAMP WHERE id = ?', (broadcast_id,))
        self.conn.commit()
    
    def save_click(self, broadcast_id, user_id, username, first_name, button_text, reaction_time, sent_at):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO button_clicks (broadcast_id, user_id, username, first_name, button_text, reaction_time, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (broadcast_id, user_id, username, first_name, button_text, reaction_time, sent_at))
        self.conn.commit()
    
    def get_clicks(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id, username, first_name, button_text, reaction_time, clicked_at, sent_at
            FROM button_clicks WHERE broadcast_id = ? ORDER BY clicked_at DESC
        ''', (broadcast_id,))
        return cursor.fetchall()
    
    def get_clicks_count(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM button_clicks WHERE broadcast_id = ?', (broadcast_id,))
        return cursor.fetchone()[0]
    
    def get_top_fastest(self, broadcast_id, limit=20):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT user_id, username, first_name, reaction_time, clicked_at
            FROM button_clicks
            WHERE broadcast_id = ? AND reaction_time IS NOT NULL
            ORDER BY reaction_time ASC
            LIMIT ?
        ''', (broadcast_id, limit))
        return cursor.fetchall()
    
    def get_user_best_time(self, broadcast_id, user_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT reaction_time, clicked_at
            FROM button_clicks
            WHERE broadcast_id = ? AND user_id = ?
            ORDER BY reaction_time ASC
            LIMIT 1
        ''', (broadcast_id, user_id))
        return cursor.fetchone()

db = Database()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id):
    return user_id == ADMIN_ID

async def send_to_admin(text):
    if ADMIN_GROUP_ID:
        try:
            await bot.send_message(ADMIN_GROUP_ID, text, parse_mode="Markdown")
        except:
            pass

def format_time(seconds):
    if seconds < 1:
        return f"{int(seconds * 1000)} мс"
    elif seconds < 60:
        return f"{seconds:.1f} сек"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins} мин {secs:.0f} сек"

def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Группы", callback_data="menu_groups")],
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="menu_create")],
        [InlineKeyboardButton(text="📋 Все рассылки", callback_data="menu_list")],
        [InlineKeyboardButton(text="📊 Статистика кнопок", callback_data="menu_stats_buttons")],
        [InlineKeyboardButton(text="🏆 Топ быстрых", callback_data="menu_top")],
        [InlineKeyboardButton(text="⏸ Вкл/Выкл все", callback_data="menu_toggle_all")],
        [InlineKeyboardButton(text="📈 Общая статистика", callback_data="menu_stats")]
    ])

# ==================== ОТПРАВКА РАССЫЛКИ ====================
async def send_broadcast(broadcast_id):
    logger.info(f"🚀 Запуск рассылки #{broadcast_id}")
    b = db.get_broadcast(broadcast_id)
    if not b or not b['is_active']:
        return
    group = db.get_target_group(b['group_id'])
    if not group or not group['is_active']:
        return
    
    try:
        chat_id = int(group['chat_id'])
        sent_at = datetime.now(pytz.timezone(TIMEZONE))
        
        reply_markup = None
        if b.get('button_text'):
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=b['button_text'], callback_data=f"btn_{broadcast_id}")]
            ])
        
        # Отправляем сообщение с кнопкой
        if b['content_type'] == 'text':
            msg = await bot.send_message(chat_id, b['text'], reply_markup=reply_markup)
        else:
            msg = await bot.send_photo(chat_id, b['photo_file_id'], caption=b['text'] or '', reply_markup=reply_markup)
        
        # Сохраняем информацию о времени отправки и ID сообщения
        db.update_last_sent(broadcast_id)
        
        # Сохраняем время отправки в отдельном месте (можно в broadcast)
        cursor = db.conn.cursor()
        cursor.execute('UPDATE broadcasts SET last_sent_at = ? WHERE id = ?', (sent_at.isoformat(), broadcast_id))
        db.conn.commit()
        
        logger.info(f"✅ Отправлено в {group['name']} в {sent_at.strftime('%H:%M:%S')}")
        
        if b['schedule_type'] == 'start_at_interval' and b['interval_minutes']:
            job_id = f"broadcast_{broadcast_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[broadcast_id], id=job_id)
            
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

# ==================== ОБРАБОТЧИК НАЖАТИЙ КНОПОК ====================
@dp.callback_query(lambda c: c.data and c.data.startswith("btn_"))
async def handle_button_click(call: CallbackQuery):
    broadcast_id = int(call.data.split("_")[1])
    user = call.from_user
    broadcast = db.get_broadcast(broadcast_id)
    
    if not broadcast:
        await call.answer("❌ Рассылка не найдена", show_alert=True)
        return
    
    button_text = broadcast['button_text']
    edit_template = broadcast.get('edit_message') or "✅ Нажал: {name}\n🆔 ID: {user_id}\n🕐 Время: {time}\n⚡ Реакция: {reaction}"
    
    # Время нажатия
    click_time = datetime.now(pytz.timezone(TIMEZONE))
    
    # Вычисляем время реакции
    reaction_time = None
    sent_at_str = broadcast.get('last_sent_at')
    if sent_at_str:
        try:
            sent_at = datetime.fromisoformat(sent_at_str)
            if sent_at.tzinfo is None:
                sent_at = pytz.timezone(TIMEZONE).localize(sent_at)
            reaction_time = (click_time - sent_at).total_seconds()
        except:
            pass
    
    # Сохраняем нажатие
    db.save_click(
        broadcast_id=broadcast_id,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        button_text=button_text,
        reaction_time=reaction_time,
        sent_at=sent_at_str
    )
    
    # Формируем текст для редактирования
    user_name = user.first_name or user.username or str(user.id)
    time_str = click_time.strftime('%d.%m.%Y %H:%M:%S')
    reaction_str = format_time(reaction_time) if reaction_time else "неизвестно"
    
    edit_text = edit_template.format(
        name=user_name,
        user_id=user.id,
        username=user.username or "нет",
        time=time_str,
        reaction=reaction_str,
        button=button_text
    )
    
    # Редактируем сообщение
    try:
        await call.message.edit_text(edit_text, parse_mode="Markdown")
        await call.answer(f"✅ Ваш голос учтён! Время реакции: {reaction_str}", show_alert=False)
    except Exception as e:
        logger.error(f"Ошибка редактирования: {e}")
        await call.answer(f"✅ Спасибо, {user_name}!", show_alert=False)
    
    # Уведомляем админа
    await send_to_admin(
        f"🔘 **Нажатие на кнопку!**\n\n"
        f"📢 Рассылка: {broadcast['name']}\n"
        f"👤 Пользователь: {user.first_name} (@{user.username or 'нет'})\n"
        f"🆔 ID: `{user.id}`\n"
        f"🔘 Кнопка: {button_text}\n"
        f"⚡ Время реакции: {reaction_str}\n"
        f"🕐 Время нажатия: {time_str}"
    )
    
    logger.info(f"🔘 Нажатие: {user.id} на рассылку #{broadcast_id}, реакция: {reaction_str}")

# ==================== ЗАГРУЗКА РАССЫЛОК ====================
async def load_broadcasts():
    for b in db.get_all_broadcasts():
        if not b['is_active']:
            continue
        job_id = f"broadcast_{b['id']}"
        if b['schedule_type'] == 'fixed' and b['hour'] is not None:
            scheduler.add_job(send_broadcast, CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE), args=[b['id']], id=job_id)
            logger.info(f"📅 Загружена fixed: {b['name']} в {b['hour']:02d}:{b['minute']:02d}")
        elif b['schedule_type'] == 'interval' and b['interval_minutes']:
            scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[b['id']], id=job_id)
            logger.info(f"⏱ Загружена interval: {b['name']} каждые {b['interval_minutes']} мин")
        elif b['schedule_type'] == 'start_at_interval' and b['start_hour'] is not None:
            scheduler.add_job(send_broadcast, CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE), args=[b['id']], id=job_id)
            logger.info(f"🚀 Загружена start_at_interval: {b['name']} старт {b['start_hour']:02d}:{b['start_minute']:02d}")

# ==================== КОМАНДЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.chat.type in ['group', 'supergroup']:
        chat_id = str(message.chat.id)
        if not db.get_target_group_by_chat_id(chat_id):
            db.add_target_group(chat_id, message.chat.title or f"Группа {chat_id}")
            await message.answer("✅ Группа добавлена! Теперь админ может создавать рассылки.")
            await send_to_admin(f"➕ Новая группа: {message.chat.title}\nID: `{chat_id}`")
        else:
            await message.answer("✅ Группа уже добавлена.")
    else:
        await message.answer(
            f"✅ **Бот для рассылок с кнопками!**\n\n"
            f"📌 Добавь бота в группу, сделай админом и отправь /start\n"
            f"👨‍💻 Затем используй /admin\n\n"
            f"🏆 **Новое:** При нажатии на кнопку замеряется время реакции!\n"
            f"📊 Команда `/top` — показать самых быстрых\n\n"
            f"🕐 Часовой пояс: {TIMEZONE}",
            parse_mode="Markdown"
        )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа")
        return
    if ADMIN_GROUP_ID and message.chat.id != ADMIN_GROUP_ID and message.chat.type != 'private':
        await message.answer(f"⛔ Управление только в админ-группе: {ADMIN_GROUP_ID}")
        return
    await message.answer("🔧 **Панель администратора**", reply_markup=get_main_menu(), parse_mode="Markdown")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 ID чата: `{message.chat.id}`", parse_mode="Markdown")

@dp.message(Command("top"))
async def cmd_top(message: Message):
    """Показать топ самых быстрых нажатий по последней рассылке"""
    broadcasts = db.get_all_broadcasts()
    if not broadcasts:
        await message.answer("📭 Нет рассылок")
        return
    
    # Берём последнюю активную рассылку с кнопкой
    last_broadcast = None
    for b in reversed(broadcasts):
        if b.get('button_text') and b['is_active']:
            last_broadcast = b
            break
    
    if not last_broadcast:
        await message.answer("📭 Нет рассылок с кнопками")
        return
    
    top = db.get_top_fastest(last_broadcast['id'], 20)
    
    if not top:
        await message.answer(f"📭 Нет нажатий на кнопку рассылки **{last_broadcast['name']}**", parse_mode="Markdown")
        return
    
    text = f"🏆 **Самые быстрые пальцы!**\n\n"
    text += f"📢 Рассылка: **{last_broadcast['name']}**\n"
    text += f"🔘 Кнопка: `{last_broadcast['button_text']}`\n\n"
    text += "**Рейтинг:**\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        user_id, username, first_name, reaction_time, clicked_at = row
        name = first_name or username or str(user_id)
        medal = medals[i] if i < 3 else f"{i+1}."
        time_str = format_time(reaction_time) if reaction_time else "? сек"
        
        # Обрезаем слишком длинные имена
        if len(name) > 20:
            name = name[:17] + "..."
        
        text += f"{medal} **{name}** — {time_str}\n"
    
    # Добавляем информацию о пользователе, если он есть в топе
    user_top = db.get_user_best_time(last_broadcast['id'], message.from_user.id)
    if user_top:
        reaction_time, clicked_at = user_top
        text += f"\n📊 **Ваш лучший результат:** {format_time(reaction_time)}"
    else:
        text += f"\n📊 Вы ещё не нажимали на кнопку этой рассылки."
    
    text += f"\n\n👆 Всего нажатий: {db.get_clicks_count(last_broadcast['id'])}"
    
    await message.answer(text, parse_mode="Markdown")

# ==================== CALLBACK ОБРАБОТЧИК МЕНЮ ====================
@dp.callback_query()
async def handle_callback(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    
    data = call.data
    await call.answer()
    
    if data == "menu_groups":
        await show_groups(call.message)
    elif data == "menu_create":
        await start_create(call.message)
    elif data == "menu_list":
        await show_broadcasts(call.message)
    elif data == "menu_stats_buttons":
        await show_buttons_stats(call.message)
    elif data == "menu_top":
        await show_top_selector(call.message)
    elif data == "menu_toggle_all":
        await toggle_all(call.message)
    elif data == "menu_stats":
        await show_stats(call.message)
    elif data == "back_to_main":
        await call.message.edit_text("🔧 **Панель администратора**", reply_markup=get_main_menu(), parse_mode="Markdown")
    
    # Добавление группы
    elif data == "add_group":
        admin_states[call.from_user.id] = {"step": "add_group_id"}
        await call.message.answer("📢 Введите ID группы (число):\nПример: -1001234567890")
    
    # Управление группами
    elif data.startswith("group_toggle_"):
        gid = int(data.split("_")[2])
        group = db.get_target_group(gid)
        if group:
            new_status = not group['is_active']
            db.toggle_target_group(gid, 1 if new_status else 0)
            for b in db.get_all_broadcasts(gid):
                db.update_broadcast(b['id'], is_active=new_status)
                job_id = f"broadcast_{b['id']}"
                if new_status:
                    if b['schedule_type'] == 'fixed' and b['hour']:
                        scheduler.add_job(send_broadcast, CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE), args=[b['id']], id=job_id)
                    elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                        scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[b['id']], id=job_id)
                    elif b['schedule_type'] == 'start_at_interval' and b['start_hour']:
                        scheduler.add_job(send_broadcast, CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE), args=[b['id']], id=job_id)
                else:
                    if scheduler.get_job(job_id):
                        scheduler.remove_job(job_id)
            await call.message.answer(f"🔄 Группа {group['name']}: {'включена ✅' if new_status else 'отключена ⛔'}")
            await show_groups(call.message)
    
    elif data.startswith("group_delete_"):
        gid = int(data.split("_")[2])
        group = db.get_target_group(gid)
        if group:
            for b in db.get_all_broadcasts(gid):
                if scheduler.get_job(f"broadcast_{b['id']}"):
                    scheduler.remove_job(f"broadcast_{b['id']}")
            db.delete_target_group(gid)
            await call.message.answer(f"🗑 Группа {group['name']} удалена")
            await show_groups(call.message)
    
    elif data.startswith("group_show_"):
        gid = int(data.split("_")[2])
        await show_group_broadcasts(call.message, gid)
    
    # Управление рассылками
    elif data.startswith("broadcast_toggle_"):
        bid = int(data.split("_")[2])
        b = db.get_broadcast(bid)
        if b:
            new_status = not b['is_active']
            db.update_broadcast(bid, is_active=new_status)
            job_id = f"broadcast_{bid}"
            if new_status:
                if b['schedule_type'] == 'fixed' and b['hour']:
                    scheduler.add_job(send_broadcast, CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE), args=[bid], id=job_id)
                elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                    scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[bid], id=job_id)
                elif b['schedule_type'] == 'start_at_interval' and b['start_hour']:
                    scheduler.add_job(send_broadcast, CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE), args=[bid], id=job_id)
            else:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
            await call.message.answer(f"🔄 Рассылка {b['name']}: {'включена ✅' if new_status else 'отключена ⛔'}")
            await show_broadcasts(call.message)
    
    elif data.startswith("broadcast_delete_"):
        bid = int(data.split("_")[2])
        b = db.get_broadcast(bid)
        if b:
            if scheduler.get_job(f"broadcast_{bid}"):
                scheduler.remove_job(f"broadcast_{bid}")
            db.delete_broadcast(bid)
            await call.message.answer(f"🗑 Рассылка {b['name']} удалена")
            await show_broadcasts(call.message)
    
    # Выбор группы для создания рассылки
    elif data.startswith("select_group_"):
        gid = int(data.split("_")[2])
        group = db.get_target_group(gid)
        if group:
            admin_states[call.from_user.id] = {
                "step": "name",
                "group_id": gid,
                "group_name": group['name']
            }
            await call.message.answer(f"📝 Создание рассылки для **{group['name']}**\n\nВведите название:", parse_mode="Markdown")
    
    # Статистика кнопок
    elif data.startswith("stats_"):
        bid = int(data.split("_")[1])
        await show_broadcast_stats(call.message, bid)
    
    # Топ для конкретной рассылки
    elif data.startswith("top_"):
        bid = int(data.split("_")[1])
        await show_broadcast_top(call.message, bid)

# ==================== ОТОБРАЖЕНИЕ МЕНЮ ====================
async def show_groups(message):
    groups = db.get_all_target_groups()
    text = "📢 **Группы для рассылок**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for g in groups:
        status = "✅" if g['is_active'] else "⛔"
        cnt = len(db.get_all_broadcasts(g['id']))
        text += f"{status} **{g['name']}**\n   🆔 `{g['chat_id']}`\n   📋 {cnt} рассылок\n\n"
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=f"📋 {g['name'][:15]}", callback_data=f"group_show_{g['id']}"),
            InlineKeyboardButton(text="🔘", callback_data=f"group_toggle_{g['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"group_delete_{g['id']}")
        ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data="add_group")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_group_broadcasts(message, group_id):
    group = db.get_target_group(group_id)
    if not group:
        return
    broadcasts = db.get_all_broadcasts(group_id)
    text = f"📋 **Рассылки группы:** {group['name']}\n\n"
    
    if not broadcasts:
        text += "Нет рассылок"
    else:
        for b in broadcasts:
            status = "✅" if b['is_active'] else "⛔"
            if b['schedule_type'] == 'fixed':
                t = f"{b['hour']:02d}:{b['minute']:02d} ежедневно"
            elif b['schedule_type'] == 'interval':
                mins = b['interval_minutes']
                h = mins // 60
                m = mins % 60
                t = f"каждые {h}ч {m}мин" if h else f"каждые {m}мин"
            else:
                t = f"старт {b['start_hour']:02d}:{b['start_minute']:02d}, затем каждые {b['interval_minutes']} мин"
            btn = f" 🔘 {b['button_text']}" if b.get('button_text') else ""
            text += f"{status} **{b['name']}**{btn}\n   ⏰ {t}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data=f"select_group_{group_id}")],
        [InlineKeyboardButton(text="◀️ Назад к группам", callback_data="menu_groups")]
    ])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_broadcasts(message):
    all_b = db.get_all_broadcasts()
    if not all_b:
        await message.edit_text("📭 **Нет рассылок**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]]))
        return
    
    text = "📋 **Все рассылки**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in all_b:
        group = db.get_target_group(b['group_id'])
        gname = group['name'] if group else "?"
        status = "✅" if b['is_active'] else "⛔"
        btn = f" [{b['button_text']}]" if b.get('button_text') else ""
        text += f"{status} **{b['name']}**{btn} → {gname}\n"
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=f"{status} {b['name'][:20]}", callback_data=f"broadcast_toggle_{b['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"broadcast_delete_{b['id']}")
        ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_buttons_stats(message):
    broadcasts = [b for b in db.get_all_broadcasts() if b.get('button_text')]
    if not broadcasts:
        await message.edit_text("📭 **Нет рассылок с кнопками**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]]))
        return
    
    text = "📊 **Статистика по кнопкам**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in broadcasts:
        cnt = db.get_clicks_count(b['id'])
        text += f"🔘 **{b['name']}**\n   📢 Кнопка: `{b['button_text']}`\n   👆 Нажатий: {cnt}\n\n"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"📊 {b['name'][:20]} ({cnt})", callback_data=f"stats_{b['id']}")])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_top_selector(message):
    broadcasts = [b for b in db.get_all_broadcasts() if b.get('button_text')]
    if not broadcasts:
        await message.edit_text("📭 **Нет рассылок с кнопками**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]]))
        return
    
    text = "🏆 **Выберите рассылку для топа**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    for b in broadcasts:
        cnt = db.get_clicks_count(b['id'])
        text += f"🔘 {b['name']} — {cnt} нажатий\n"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"🏆 {b['name'][:25]}", callback_data=f"top_{b['id']}")])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_broadcast_top(message, broadcast_id):
    b = db.get_broadcast(broadcast_id)
    if not b:
        await message.answer("❌ Рассылка не найдена")
        return
    
    top = db.get_top_fastest(broadcast_id, 20)
    
    if not top:
        await message.edit_text(f"📭 Нет нажатий на кнопку рассылки **{b['name']}**", parse_mode="Markdown")
        return
    
    text = f"🏆 **Самые быстрые пальцы!**\n\n"
    text += f"📢 Рассылка: **{b['name']}**\n"
    text += f"🔘 Кнопка: `{b['button_text']}`\n\n"
    text += "**Рейтинг:**\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        user_id, username, first_name, reaction_time, clicked_at = row
        name = first_name or username or str(user_id)
        medal = medals[i] if i < 3 else f"{i+1}."
        time_str = format_time(reaction_time) if reaction_time else "? сек"
        
        if len(name) > 20:
            name = name[:17] + "..."
        
        text += f"{medal} **{name}** — {time_str}\n"
    
    text += f"\n👆 Всего нажатий: {db.get_clicks_count(broadcast_id)}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="menu_top")]
    ])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_broadcast_stats(message, broadcast_id):
    b = db.get_broadcast(broadcast_id)
    if not b:
        return
    
    clicks = db.get_clicks(broadcast_id)
    text = f"📊 **Статистика рассылки**\n\n"
    text += f"📢 Название: {b['name']}\n"
    text += f"🔘 Кнопка: `{b['button_text']}`\n"
    text += f"👆 Всего нажатий: {len(clicks)}\n\n"
    
    if clicks:
        text += "**Последние нажатия:**\n"
        for click in clicks[:20]:
            _, uid, username, first_name, _, clicked_at, sent_at = click
            name = first_name or username or str(uid)
            text += f"• {name} (@{username or '-'}) — {clicked_at[:16]}\n"
        if len(clicks) > 20:
            text += f"\n...и ещё {len(clicks) - 20} нажатий"
    else:
        text += "Пока нет нажатий на кнопку."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Топ быстрых", callback_data=f"top_{broadcast_id}")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="menu_stats_buttons")]
    ])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def toggle_all(message):
    all_b = db.get_all_broadcasts()
    active = any(b['is_active'] for b in all_b)
    
    for b in all_b:
        new_status = not active
        db.update_broadcast(b['id'], is_active=new_status)
        job_id = f"broadcast_{b['id']}"
        if new_status:
            if b['schedule_type'] == 'fixed' and b['hour']:
                scheduler.add_job(send_broadcast, CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE), args=[b['id']], id=job_id)
            elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[b['id']], id=job_id)
            elif b['schedule_type'] == 'start_at_interval' and b['start_hour']:
                scheduler.add_job(send_broadcast, CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE), args=[b['id']], id=job_id)
        else:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
    
    await message.answer(f"⏸ Все рассылки {'отключены' if not active else 'включены'}")
    await show_broadcasts(message)

async def show_stats(message):
    groups = db.get_all_target_groups()
    broadcasts = db.get_all_broadcasts()
    active = sum(1 for b in broadcasts if b['is_active'])
    total_clicks = sum(db.get_clicks_count(b['id']) for b in broadcasts)
    buttons_count = sum(1 for b in broadcasts if b.get('button_text'))
    
    await message.answer(
        f"📊 **Общая статистика**\n\n"
        f"📢 Групп: {len(groups)}\n"
        f"📋 Рассылок: {len(broadcasts)}\n"
        f"✅ Активных: {active}\n"
        f"🔘 С кнопками: {buttons_count}\n"
        f"👆 Всего нажатий: {total_clicks}\n"
        f"🕐 Часовой пояс: {TIMEZONE}",
        parse_mode="Markdown"
    )

# ==================== СОЗДАНИЕ РАССЫЛКИ ====================
async def start_create(message):
    groups = [g for g in db.get_all_target_groups() if g['is_active']]
    if not groups:
        await message.answer("❌ Нет активных групп. Добавьте группу через /start в ней.")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for g in groups:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"📢 {g['name']}", callback_data=f"select_group_{g['id']}")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")])
    
    await message.edit_text("📝 **Выберите группу для рассылки**", reply_markup=keyboard, parse_mode="Markdown")

# ==================== ОБРАБОТКА ВВОДА АДМИНА ====================
@dp.message()
async def handle_input(message: Message):
    if not is_admin(message.from_user.id):
        return
    if message.from_user.id not in admin_states:
        return
    
    state = admin_states[message.from_user.id]
    step = state.get("step")
    
    # Добавление группы по ID
    if step == "add_group_id":
        try:
            chat_id = int(message.text.strip())
            if db.get_target_group_by_chat_id(str(chat_id)):
                await message.answer("❌ Группа уже добавлена")
            else:
                db.add_target_group(str(chat_id), f"Группа {chat_id}")
                await message.answer(f"✅ Группа `{chat_id}` добавлена", parse_mode="Markdown")
                await send_to_admin(f"➕ Добавлена группа вручную: `{chat_id}`")
            del admin_states[message.from_user.id]
            await show_groups(message)
        except:
            await message.answer("❌ Неверный ID. Введите число.")
    
    # Шаг 1: Название
    elif step == "name":
        state["name"] = message.text
        state["step"] = "type"
        await message.answer("📝 **Тип контента**\nОтправьте `текст` или `фото`", parse_mode="Markdown")
    
    # Шаг 2: Тип контента
    elif step == "type":
        if message.text.lower() in ["текст", "text"]:
            state["content_type"] = "text"
            state["step"] = "text"
            await message.answer("📝 Отправьте текст рассылки:")
        elif message.text.lower() in ["фото", "photo"]:
            state["content_type"] = "photo"
            state["step"] = "photo"
            await message.answer("🖼 Отправьте фото (можно с подписью):")
        else:
            await message.answer("❌ Отправьте 'текст' или 'фото'")
    
    # Шаг 3a: Текст
    elif step == "text":
        state["text"] = message.text
        state["step"] = "button"
        await message.answer("🔘 **Добавить кнопку?**\n\nОтправьте текст кнопки (например: `Нажми меня!`)\nИли отправьте `пропустить`", parse_mode="Markdown")
    
    # Шаг 3b: Фото
    elif step == "photo":
        if message.photo:
            state["photo_file_id"] = message.photo[-1].file_id
            state["text"] = message.caption or ""
            state["step"] = "button"
            await message.answer("🔘 **Добавить кнопку?**\n\nОтправьте текст кнопки\nИли отправьте `пропустить`", parse_mode="Markdown")
        else:
            await message.answer("❌ Отправьте фото")
    
    # Шаг 4: Кнопка
    elif step == "button":
        if message.text.lower() in ["пропустить", "skip", "-"]:
            state["button_text"] = None
            state["edit_message"] = None
            state["step"] = "schedule_type"
            await message.answer(
                "⏰ **Тип расписания**\n\n"
                "`1` - В определённое время (ежедневно)\n"
                "`2` - Простой интервал (каждые X минут)\n"
                "`3` - Старт в указанное время + интервал\n\n"
                "Отправьте 1, 2 или 3:",
                parse_mode="Markdown"
            )
        else:
            state["button_text"] = message.text[:50]
            state["step"] = "edit_message"
            await message.answer(
                "✏️ **Текст после нажатия**\n\n"
                "Отправьте текст, который будет показан после нажатия на кнопку.\n\n"
                "**Доступные переменные:**\n"
                "`{name}` - имя пользователя\n"
                "`{user_id}` - ID пользователя\n"
                "`{username}` - username\n"
                "`{time}` - время нажатия\n"
                "`{reaction}` - время реакции\n"
                "`{button}` - текст кнопки\n\n"
                "Пример:\n"
                "`✅ Спасибо, {name}!\\n🕐 Время: {time}\\n⚡ Реакция: {reaction}`\n\n"
                "Или отправьте `пропустить` для стандартного текста",
                parse_mode="Markdown"
            )
    
    # Шаг 4b: Текст после нажатия
    elif step == "edit_message":
        if message.text.lower() in ["пропустить", "skip", "-"]:
            state["edit_message"] = "✅ Нажал: {name}\n🆔 ID: {user_id}\n🕐 Время: {time}\n⚡ Реакция: {reaction}"
        else:
            state["edit_message"] = message.text
        state["step"] = "schedule_type"
        await message.answer(
            "⏰ **Тип расписания**\n\n"
            "`1` - В определённое время (ежедневно)\n"
            "`2` - Простой интервал (каждые X минут)\n"
            "`3` - Старт в указанное время + интервал\n\n"
            "Отправьте 1, 2 или 3:",
            parse_mode="Markdown"
        )
    
    # Шаг 5: Выбор типа расписания
    elif step == "schedule_type":
        if message.text == "1":
            state["schedule_type"] = "fixed"
            state["step"] = "fixed_time"
            await message.answer(f"⏰ Введите время в формате `HH:MM` (часовой пояс {TIMEZONE})", parse_mode="Markdown")
        elif message.text == "2":
            state["schedule_type"] = "interval"
            state["step"] = "interval"
            await message.answer("⏰ Введите интервал в минутах (например, 60, 30, 120):")
        elif message.text == "3":
            state["schedule_type"] = "start_at_interval"
            state["step"] = "start_time"
            await message.answer(f"🚀 Введите **первое время** отправки в формате `HH:MM` (часовой пояс {TIMEZONE})", parse_mode="Markdown")
        else:
            await message.answer("❌ Отправьте 1, 2 или 3")
    
    # Шаг 6a: Фиксированное время
    elif step == "fixed_time":
        try:
            h, m = map(int, message.text.split(':'))
            if 0 <= h <= 23 and 0 <= m <= 59:
                state["hour"] = h
                state["minute"] = m
                await save_broadcast(message, state)
            else:
                raise ValueError
        except:
            await message.answer("❌ Неверный формат. Пример: 14:30")
    
    # Шаг 6b: Простой интервал
    elif step == "interval":
        try:
            interval = int(message.text)
            if interval > 0:
                state["interval_minutes"] = interval
                await save_broadcast(message, state)
            else:
                raise ValueError
        except:
            await message.answer("❌ Введите положительное число")
    
    # Шаг 6c: Стартовое время для интервальной рассылки
    elif step == "start_time":
        try:
            h, m = map(int, message.text.split(':'))
            if 0 <= h <= 23 and 0 <= m <= 59:
                state["start_hour"] = h
                state["start_minute"] = m
                state["step"] = "interval_after_start"
                await message.answer("⏰ **Интервал повторения** (в минутах)\n\nПример: 120 → каждые 2 часа\nОтправьте число:")
            else:
                raise ValueError
        except:
            await message.answer("❌ Неверный формат. Пример: 14:30")
    
    # Шаг 7: Интервал после стартового времени
    elif step == "interval_after_start":
        try:
            interval = int(message.text)
            if interval > 0:
                state["interval_minutes"] = interval
                await save_broadcast(message, state)
            else:
                raise ValueError
        except:
            await message.answer("❌ Введите положительное число")

# ==================== СОХРАНЕНИЕ РАССЫЛКИ ====================
async def save_broadcast(message, state):
    broadcast_id = db.add_broadcast(
        group_id=state["group_id"],
        name=state["name"],
        content_type=state["content_type"],
        schedule_type=state["schedule_type"],
        text=state.get("text"),
        photo_file_id=state.get("photo_file_id"),
        hour=state.get("hour"),
        minute=state.get("minute"),
        interval_minutes=state.get("interval_minutes"),
        start_hour=state.get("start_hour"),
        start_minute=state.get("start_minute"),
        button_text=state.get("button_text"),
        edit_message=state.get("edit_message")
    )
    
    b = db.get_broadcast(broadcast_id)
    job_id = f"broadcast_{broadcast_id}"
    
    if b['schedule_type'] == 'fixed' and b['hour']:
        scheduler.add_job(send_broadcast, CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE), args=[broadcast_id], id=job_id)
    elif b['schedule_type'] == 'interval' and b['interval_minutes']:
        scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[broadcast_id], id=job_id)
    elif b['schedule_type'] == 'start_at_interval' and b['start_hour']:
        scheduler.add_job(send_broadcast, CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE), args=[broadcast_id], id=job_id)
    
    group = db.get_target_group(state["group_id"])
    
    btn_info = f"\n🔘 Кнопка: `{state['button_text']}`" if state.get('button_text') else ""
    edit_info = "\n✏️ После нажатия: сообщение редактируется" if state.get('button_text') else ""
    
    await message.answer(
        f"✅ **Рассылка создана!**\n\n"
        f"📢 Название: {state['name']}\n"
        f"📬 Группа: {group['name']}{btn_info}{edit_info}\n\n"
        f"📌 При нажатии на кнопку:\n"
        f"   • Замеряется время реакции\n"
        f"   • Сообщение заменяется на указанный текст\n"
        f"   • Ведётся статистика для топа\n\n"
        f"🏆 Команда `/top` — показать самых быстрых",
        parse_mode="Markdown"
    )
    
    admin_btn_line = f"\n🔘 Кнопка: {state['button_text']}" if state.get('button_text') else ""
    await send_to_admin(
        f"➕ **Новая рассылка**\n"
        f"📢 {state['name']}\n"
        f"📬 {group['name']}{admin_btn_line}"
    )
    
    del admin_states[message.from_user.id]

# ==================== ЗАПУСК БОТА ====================
async def main():
    logger.info("🚀 Бот запускается...")
    logger.info(f"📅 Часовой пояс: {TIMEZONE}")
    logger.info(f"👑 Админ ID: {ADMIN_ID}")
    logger.info(f"📢 Админская группа: {ADMIN_GROUP_ID if ADMIN_GROUP_ID else 'Не настроена'}")
    
    await load_broadcasts()
    scheduler.start()
    
    logger.info("✅ Бот готов к работе!")
    
    if ADMIN_GROUP_ID:
        await send_to_admin("✅ **Бот перезапущен и готов к работе!**\n\n🏆 Теперь при нажатии на кнопку замеряется время реакции!\n📊 Команда `/top` — показать самых быстрых")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())