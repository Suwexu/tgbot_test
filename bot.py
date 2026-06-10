import asyncio
import logging
import os
from datetime import datetime, timedelta
import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_STR = os.getenv("ADMIN_ID", "0")
TOP_VIEWERS_STR = os.getenv("TOP_VIEWERS", "")
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# Преобразуем строки в списки чисел
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]
TOP_VIEWERS = [int(x.strip()) for x in TOP_VIEWERS_STR.split(",") if x.strip().isdigit()] if TOP_VIEWERS_STR else []

if ADMIN_GROUP_ID:
    ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)

if not BOT_TOKEN or not ADMIN_IDS:
    raise ValueError("BOT_TOKEN и ADMIN_ID обязательны")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

admin_states = {}

# ==================== НАСТРОЙКА ПУТИ К БАЗЕ ДАННЫХ ====================
VOLUME_PATH = '/app/data'
if os.path.exists(VOLUME_PATH) and os.path.isdir(VOLUME_PATH):
    DB_PATH = os.path.join(VOLUME_PATH, 'bot_database.db')
    logger.info(f"✅ Используется Volume для БД: {DB_PATH}")
else:
    DB_PATH = 'bot_database.db'
    logger.info(f"⚠️ Volume не найден, используется локальная БД: {DB_PATH}")

# ==================== БАЗА ДАННЫХ ====================
import sqlite3

class Database:
    def __init__(self):
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"📁 Создана директория для БД: {db_dir}")
        
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.init_tables()
        logger.info(f"✅ База данных подключена: {DB_PATH}")
    
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
                text TEXT,
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
        logger.info("✅ Таблицы базы данных инициализированы")
    
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
    
    def add_broadcast(self, group_id, name, text, schedule_type,
                      hour=None, minute=None,
                      interval_minutes=None,
                      start_hour=None, start_minute=None,
                      button_text=None, edit_message=None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO broadcasts
            (group_id, name, text, schedule_type,
             hour, minute, interval_minutes, start_hour, start_minute, button_text, edit_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (group_id, name, text, schedule_type,
              hour, minute, interval_minutes, start_hour, start_minute, button_text, edit_message))
        self.conn.commit()
        return cursor.lastrowid
    
    def update_broadcast(self, broadcast_id, **kwargs):
        cursor = self.conn.cursor()
        allowed = ['name', 'text', 'schedule_type', 'hour', 'minute', 
                   'interval_minutes', 'start_hour', 'start_minute', 
                   'button_text', 'edit_message', 'is_active']
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
    
    def get_all_broadcasts(self, group_id=None):
        cursor = self.conn.cursor()
        if group_id:
            cursor.execute('SELECT * FROM broadcasts WHERE group_id = ?', (group_id,))
        else:
            cursor.execute('SELECT * FROM broadcasts')
        rows = cursor.fetchall()
        return [{
            'id': r[0], 'group_id': r[1], 'name': r[2], 'text': r[3],
            'schedule_type': r[4],
            'hour': r[5], 'minute': r[6], 'interval_minutes': r[7],
            'start_hour': r[8], 'start_minute': r[9],
            'button_text': r[10], 'edit_message': r[11],
            'is_active': bool(r[12]), 'last_sent_at': r[13]
        } for r in rows]
    
    def get_broadcast(self, broadcast_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM broadcasts WHERE id = ?', (broadcast_id,))
        r = cursor.fetchone()
        if r:
            return {
                'id': r[0], 'group_id': r[1], 'name': r[2], 'text': r[3],
                'schedule_type': r[4],
                'hour': r[5], 'minute': r[6], 'interval_minutes': r[7],
                'start_hour': r[8], 'start_minute': r[9],
                'button_text': r[10], 'edit_message': r[11],
                'is_active': bool(r[12]), 'last_sent_at': r[13]
            }
        return None
    
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
    
    def get_top_fastest_all(self, limit=20):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                user_id, 
                username, 
                first_name, 
                ROUND(AVG(reaction_time), 2) as avg_time,
                COUNT(*) as clicks_count
            FROM button_clicks
            WHERE reaction_time IS NOT NULL
            GROUP BY user_id
            ORDER BY avg_time ASC
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()
    
    def get_top_fastest_by_group(self, group_id, limit=20):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                bc.user_id, 
                bc.username, 
                bc.first_name, 
                ROUND(AVG(bc.reaction_time), 2) as avg_time,
                COUNT(*) as clicks_count
            FROM button_clicks bc
            JOIN broadcasts b ON bc.broadcast_id = b.id
            WHERE b.group_id = ? AND bc.reaction_time IS NOT NULL
            GROUP BY bc.user_id
            ORDER BY avg_time ASC
            LIMIT ?
        ''', (group_id, limit))
        return cursor.fetchall()
    
    def get_user_stats_by_group(self, group_id, user_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                ROUND(AVG(bc.reaction_time), 2) as avg_time,
                COUNT(*) as clicks_count
            FROM button_clicks bc
            JOIN broadcasts b ON bc.broadcast_id = b.id
            WHERE b.group_id = ? AND bc.user_id = ? AND bc.reaction_time IS NOT NULL
        ''', (group_id, user_id))
        return cursor.fetchone()
    
    def get_total_clicks_by_group(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) 
            FROM button_clicks bc
            JOIN broadcasts b ON bc.broadcast_id = b.id
            WHERE b.group_id = ? AND bc.reaction_time IS NOT NULL
        ''', (group_id,))
        return cursor.fetchone()[0]
    
    def get_total_users_by_group(self, group_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT COUNT(DISTINCT bc.user_id)
            FROM button_clicks bc
            JOIN broadcasts b ON bc.broadcast_id = b.id
            WHERE b.group_id = ? AND bc.reaction_time IS NOT NULL
        ''', (group_id,))
        return cursor.fetchone()[0]
    
    def get_top_fastest(self, broadcast_id, limit=20):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT 
                user_id, 
                username, 
                first_name, 
                ROUND(AVG(reaction_time), 2) as avg_time,
                COUNT(*) as clicks_count
            FROM button_clicks
            WHERE broadcast_id = ? AND reaction_time IS NOT NULL
            GROUP BY user_id
            ORDER BY avg_time ASC
            LIMIT ?
        ''', (broadcast_id, limit))
        return cursor.fetchall()

db = Database()

# ==================== ФУНКЦИИ ПРОВЕРКИ ПРАВ ====================
def is_admin(user_id):
    return user_id in ADMIN_IDS

def can_view_top(user_id):
    return user_id in ADMIN_IDS or user_id in TOP_VIEWERS

def get_user_mention(user):
    if user.username:
        return f"@{user.username}"
    else:
        return f'<a href="tg://user?id={user.id}">{user.first_name}</a>'

def format_time(seconds):
    if seconds is None:
        return "? сек"
    if seconds < 1:
        return f"{int(seconds * 1000)} мс"
    elif seconds < 60:
        return f"{seconds:.1f} сек"
    else:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins} мин {secs:.0f} сек"

def get_schedule_info(broadcast):
    """Формирует текстовое описание расписания"""
    if broadcast['schedule_type'] == 'fixed':
        return f"⏰ {broadcast['hour']:02d}:{broadcast['minute']:02d} ежедневно"
    elif broadcast['schedule_type'] == 'interval':
        mins = broadcast['interval_minutes']
        hours = mins // 60
        minutes = mins % 60
        if hours > 0:
            return f"⏰ каждые {hours}ч {minutes}мин" if minutes > 0 else f"⏰ каждые {hours}ч"
        else:
            return f"⏰ каждые {minutes}мин"
    else:
        mins = broadcast['interval_minutes']
        hours = mins // 60
        minutes = mins % 60
        if hours > 0:
            interval_str = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
        else:
            interval_str = f"каждые {minutes}мин"
        return f"🚀 старт {broadcast['start_hour']:02d}:{broadcast['start_minute']:02d}, затем {interval_str}"

def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Группы", callback_data="menu_groups")],
        [InlineKeyboardButton(text="➕ Создать рассылку", callback_data="menu_create")],
        [InlineKeyboardButton(text="📋 Все рассылки", callback_data="menu_list")],
        [InlineKeyboardButton(text="📊 Статистика кнопок", callback_data="menu_stats_buttons")],
        [InlineKeyboardButton(text="📈 Список реакции", callback_data="menu_top")],
        [InlineKeyboardButton(text="⏸ Вкл/Выкл все", callback_data="menu_toggle_all")],
        [InlineKeyboardButton(text="📈 Общая статистика", callback_data="menu_stats")]
    ])

# ==================== ОТПРАВКА РАССЫЛКИ ====================
async def send_broadcast(broadcast_id):
    logger.info(f"🚀 ЗАПУСК РАССЫЛКИ #{broadcast_id}")
    
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
        
        await bot.send_message(chat_id, b['text'], reply_markup=reply_markup, parse_mode="HTML")
        
        db.update_last_sent(broadcast_id)
        cursor = db.conn.cursor()
        cursor.execute('UPDATE broadcasts SET last_sent_at = ? WHERE id = ?', (sent_at.isoformat(), broadcast_id))
        db.conn.commit()
        
        logger.info(f"✅ Отправлено в {group['name']}")
        
        if b['schedule_type'] == 'start_at_interval' and b['interval_minutes']:
            job_id = f"broadcast_{broadcast_id}"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            scheduler.add_job(send_broadcast, IntervalTrigger(minutes=b['interval_minutes']), args=[broadcast_id], id=job_id)
            logger.info(f"🔄 Рассылка #{broadcast_id} перепланирована: следующая через {b['interval_minutes']} мин")
            
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")

# ==================== ЗАГРУЗКА РАССЫЛОК ====================
async def load_broadcasts():
    logger.info("📋 Загрузка рассылок из БД...")
    broadcasts = db.get_all_broadcasts()
    logger.info(f"📋 Найдено рассылок: {len(broadcasts)}")
    
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    
    for b in broadcasts:
        if not b['is_active']:
            continue
        
        job_id = f"broadcast_{b['id']}"
        
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        
        try:
            if b['schedule_type'] == 'fixed' and b['hour'] is not None:
                schedule_time = now.replace(hour=b['hour'], minute=b['minute'], second=0, microsecond=0)
                
                if schedule_time < now:
                    schedule_time += timedelta(days=1)
                    logger.info(f"⏰ Время рассылки #{b['id']} уже прошло сегодня, переносим на завтра")
                
                trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
                scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                logger.info(f"📅 Загружена fixed: {b['name']} в {b['hour']:02d}:{b['minute']:02d}")
                
            elif b['schedule_type'] == 'interval' and b['interval_minutes']:
                trigger = IntervalTrigger(minutes=b['interval_minutes'])
                scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                logger.info(f"⏱ Загружена interval: {b['name']} каждые {b['interval_minutes']} мин")
                
            elif b['schedule_type'] == 'start_at_interval' and b['start_hour'] is not None:
                start_time = now.replace(hour=b['start_hour'], minute=b['start_minute'], second=0, microsecond=0)
                
                if start_time < now:
                    logger.info(f"⚠️ Время старта рассылки #{b['id']} уже прошло, НЕ запускаем пропущенную")
                    if b['interval_minutes']:
                        trigger = IntervalTrigger(minutes=b['interval_minutes'])
                        scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                        logger.info(f"🔄 Рассылка #{b['id']} будет отправляться каждые {b['interval_minutes']} мин")
                else:
                    trigger = CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE)
                    scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                    logger.info(f"🚀 Загружена start_at_interval: {b['name']} старт {b['start_hour']:02d}:{b['start_minute']:02d}")
                
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки рассылки #{b['id']}: {e}")
    
    jobs = scheduler.get_jobs()
    logger.info(f"📋 Всего задач в планировщике: {len(jobs)}")

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
    edit_template = broadcast.get('edit_message') or "✅ Нажал: {mention}\n🆔 ID: {user_id}\n🕐 Время: {time}\n⚡ Реакция: {reaction}"
    
    click_time = datetime.now(pytz.timezone(TIMEZONE))
    
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
    
    db.save_click(
        broadcast_id=broadcast_id,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        button_text=button_text,
        reaction_time=reaction_time,
        sent_at=sent_at_str
    )
    
    mention = get_user_mention(user)
    user_name = user.first_name or user.username or str(user.id)
    time_str = click_time.strftime('%H:%M')
    reaction_str = format_time(reaction_time) if reaction_time else "неизвестно"
    
    edit_text = edit_template.format(
        mention=mention,
        name=user_name,
        user_id=user.id,
        username=user.username or "нет",
        time=time_str,
        reaction=reaction_str,
        button=button_text
    )
    
    try:
        await call.message.edit_text(edit_text, parse_mode="HTML")
        await call.answer(f"✅ Ваш голос учтён! Время реакции: {reaction_str}", show_alert=False)
    except Exception as e:
        logger.error(f"Ошибка редактирования: {e}")
        await call.answer(f"✅ Спасибо, {user_name}!", show_alert=False)
    
    logger.info(f"🔘 Нажатие: {user.id} ({user_name}) на рассылку #{broadcast_id}, реакция: {reaction_str}")

# ==================== КОМАНДЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Команда `/start` доступна только администраторам бота.", parse_mode="Markdown")
        logger.warning(f"⚠️ Неавторизованная попытка доступа к /start от {message.from_user.id}")
        return
    
    if message.chat.type in ['group', 'supergroup']:
        chat_id = str(message.chat.id)
        if not db.get_target_group_by_chat_id(chat_id):
            db.add_target_group(chat_id, message.chat.title or f"Группа {chat_id}")
            await message.answer("✅ Группа добавлена! Теперь администратор может создавать рассылки.")
            logger.info(f"➕ Новая группа: {message.chat.title} (ID: {chat_id})")
        else:
            await message.answer("✅ Группа уже добавлена.")
    else:
        await message.answer(
            f"✅ **Бот для рассылок с кнопками!**\n\n"
            f"📌 Добавь бота в группу, сделай админом и отправь /start\n"
            f"👨‍💻 Администраторы управляют через /admin\n\n"
            f"📊 Команда `/top` — список времени реакции сотрудников\n"
            f"✏️ Теперь можно редактировать рассылки!\n"
            f"💾 Команды бэкапа: `/backup` и `/restore`\n"
            f"🕐 Часовой пояс: {TIMEZONE}",
            parse_mode="Markdown"
        )

@dp.message(Command("top"))
async def cmd_top(message: Message):
    if not can_view_top(message.from_user.id):
        await message.answer(
            "⛔ **У вас нет доступа к команде `/top`.**\n\n"
            "Эта команда доступна только администраторам и специально назначенным сотрудникам.",
            parse_mode="Markdown"
        )
        logger.warning(f"⚠️ Неавторизованная попытка доступа к /top от {message.from_user.id}")
        return
    
    if message.chat.type not in ['group', 'supergroup']:
        await message.answer("📊 **Команда `/top` работает только в группах!**", parse_mode="Markdown")
        return
    
    chat_id = str(message.chat.id)
    group = db.get_target_group_by_chat_id(chat_id)
    
    if not group:
        await message.answer(
            "❌ **Группа не зарегистрирована в боте!**\n\n"
            "Пожалуйста, попросите администратора зарегистрировать группу через команду `/start`.",
            parse_mode="Markdown"
        )
        return
    
    if not group['is_active']:
        await message.answer(
            "⛔ **Группа отключена администратором**\n\n"
            "Рассылки в эту группу временно не отправляются.",
            parse_mode="Markdown"
        )
        return
    
    top = db.get_top_fastest_by_group(group['id'], 20)
    
    if not top:
        await message.answer(
            f"📭 **Нет данных о реакции сотрудников в группе {group['name']}**\n\n"
            f"Пока никто не нажимал на кнопки в рассылках этой группы.\n"
            f"Дождитесь следующей рассылки с кнопкой.",
            parse_mode="Markdown"
        )
        return
    
    total_clicks = db.get_total_clicks_by_group(group['id'])
    total_users = db.get_total_users_by_group(group['id'])
    
    text = f"📊 **Список времени реакции сотрудников**\n\n"
    text += f"📢 Группа: **{group['name']}**\n"
    text += f"👆 Всего нажатий: {total_clicks}\n"
    text += f"👥 Участников: {total_users}\n\n"
    text += "**Рейтинг (по средней скорости):**\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        user_id, username, first_name, avg_time, clicks_count = row
        name = first_name or username or str(user_id)
        medal = medals[i] if i < 3 else f"{i+1}."
        avg_str = format_time(avg_time)
        if len(name) > 20:
            name = name[:17] + "..."
        text += f"{medal} **{name}** — {avg_str} ({clicks_count} наж.)\n"
    
    user_stats = db.get_user_stats_by_group(group['id'], message.from_user.id)
    if user_stats and user_stats[1] > 0:
        avg_time, clicks_count = user_stats
        text += f"\n📊 **Ваша статистика:** {format_time(avg_time)} ({clicks_count} наж.)"
    else:
        text += f"\n📊 Вы ещё не нажимали на кнопки в этой группе."
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к этой команде.", parse_mode="Markdown")
        logger.warning(f"⚠️ Неавторизованная попытка доступа к /admin от {message.from_user.id}")
        return
    
    if ADMIN_GROUP_ID and message.chat.id != ADMIN_GROUP_ID and message.chat.type != 'private':
        await message.answer(f"⛔ Управление доступно только в админской группе или личных сообщениях.\n📢 ID админской группы: `{ADMIN_GROUP_ID}`", parse_mode="Markdown")
        return
    
    await message.answer("🔧 **Панель администратора**", reply_markup=get_main_menu(), parse_mode="Markdown")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    user_id = message.from_user.id
    is_admin_status = "✅ Да" if is_admin(user_id) else "❌ Нет"
    can_view_top_status = "✅ Да" if can_view_top(user_id) else "❌ Нет"
    
    await message.answer(
        f"🆔 **Информация**\n\n"
        f"📝 Ваш ID: `{user_id}`\n"
        f"👑 Администратор: {is_admin_status}\n"
        f"📊 Доступ к /top: {can_view_top_status}\n\n"
        f"🆔 ID чата: `{message.chat.id}`",
        parse_mode="Markdown"
    )

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа. Команда `/debug` только для администраторов.", parse_mode="Markdown")
        return
    
    broadcasts = db.get_all_broadcasts()
    jobs = scheduler.get_jobs()
    
    text = f"🔍 **ДИАГНОСТИКА**\n\n"
    text += f"🕐 Часовой пояс: `{TIMEZONE}`\n"
    text += f"📅 Текущее время: `{datetime.now(pytz.timezone(TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
    text += f"📋 Рассылок в БД: `{len(broadcasts)}`\n"
    text += f"⏰ Задач в планировщике: `{len(jobs)}`\n"
    text += f"💾 Путь к БД: `{DB_PATH}`\n\n"
    
    if broadcasts:
        text += "**📋 Рассылки:**\n"
        for b in broadcasts:
            status = "✅" if b['is_active'] else "⛔"
            schedule_info = get_schedule_info(b)
            btn = f" 🔘 {b['button_text']}" if b.get('button_text') else ""
            text += f"{status} ID:{b['id']} **{b['name']}**{btn}\n   {schedule_info}\n"
    
    if jobs:
        text += "\n**⏰ Задачи в планировщике:**\n"
        for job in jobs:
            next_run = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job.next_run_time else "None"
            text += f"• `{job.id}` -> {next_run}\n"
    else:
        text += "\n⚠️ **НЕТ ЗАДАЧ В ПЛАНИРОВЩИКЕ!**\n"
    
    groups = db.get_all_target_groups()
    text += f"\n**📢 Группы:** {len(groups)}\n"
    for g in groups:
        text += f"• {g['name']} (`{g['chat_id']}`)\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа")
        return
    
    try:
        with open(DB_PATH, 'rb') as f:
            await bot.send_document(
                message.chat.id, 
                types.BufferedInputFile(f.read(), filename=f'bot_database_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'),
                caption=f"📦 Бэкап базы данных от {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n💾 Путь: {DB_PATH}"
            )
        await message.answer("✅ Бэкап базы данных создан и отправлен!")
        logger.info(f"📦 Бэкап БД создан администратором {message.from_user.id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error(f"Ошибка создания бэкапа: {e}")

@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа")
        return
    
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.answer(
            "📦 **Как восстановить бэкап:**\n\n"
            "1. Отправьте файл бэкапа боту\n"
            "2. Ответьте на это сообщение командой `/restore`\n\n"
            "⚠️ **ВНИМАНИЕ!** Текущая база данных будет заменена!",
            parse_mode="Markdown"
        )
        return
    
    try:
        file_id = message.reply_to_message.document.file_id
        file = await bot.get_file(file_id)
        downloaded_file = await bot.download_file(file.file_path)
        
        with open(DB_PATH, 'wb') as f:
            f.write(downloaded_file.getvalue())
        
        await message.answer(
            f"✅ **База данных восстановлена!**\n\n"
            f"💾 Путь: {DB_PATH}\n"
            f"🔄 Перезапустите бота для применения изменений.",
            parse_mode="Markdown"
        )
        logger.info(f"📦 БД восстановлена из бэкапа админом {message.from_user.id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка восстановления: {e}")

@dp.message(Command("add_viewer"))
async def cmd_add_viewer(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа")
        return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer(
            "📝 **Как добавить наблюдателя:**\n\n"
            "Отправьте: `/add_viewer 123456789`\n\n"
            "Где 123456789 — Telegram ID пользователя.",
            parse_mode="Markdown"
        )
        return
    
    try:
        new_viewer_id = int(args[1])
        
        if new_viewer_id in TOP_VIEWERS:
            await message.answer(f"❌ Пользователь `{new_viewer_id}` уже имеет доступ", parse_mode="Markdown")
            return
        
        if new_viewer_id in ADMIN_IDS:
            await message.answer(f"⚠️ Пользователь уже является администратором", parse_mode="Markdown")
            return
        
        TOP_VIEWERS.append(new_viewer_id)
        
        await message.answer(
            f"✅ **Пользователь добавлен!**\n\n"
            f"🆔 ID: `{new_viewer_id}`\n\n"
            f"⚠️ Для постоянного сохранения добавьте ID в переменную `TOP_VIEWERS` в Railway.",
            parse_mode="Markdown"
        )
        
        try:
            await bot.send_message(new_viewer_id, "✅ **Вам открыт доступ к команде `/top`!**", parse_mode="Markdown")
        except:
            pass
            
    except ValueError:
        await message.answer("❌ Неверный формат ID.")

# ==================== РЕДАКТИРОВАНИЕ РАССЫЛКИ ====================
async def update_broadcast_and_reload(broadcast_id, **kwargs):
    """Обновляет рассылку и перезагружает задачу в планировщике"""
    db.update_broadcast(broadcast_id, **kwargs)
    
    b = db.get_broadcast(broadcast_id)
    job_id = f"broadcast_{broadcast_id}"
    
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    if b['is_active']:
        if b['schedule_type'] == 'fixed' and b['hour'] is not None:
            trigger = CronTrigger(hour=b['hour'], minute=b['minute'], timezone=TIMEZONE)
            scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)
        elif b['schedule_type'] == 'interval' and b['interval_minutes']:
            trigger = IntervalTrigger(minutes=b['interval_minutes'])
            scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)
        elif b['schedule_type'] == 'start_at_interval' and b['start_hour'] is not None:
            tz = pytz.timezone(TIMEZONE)
            now = datetime.now(tz)
            start_time = now.replace(hour=b['start_hour'], minute=b['start_minute'], second=0, microsecond=0)
            if start_time < now:
                if b['interval_minutes']:
                    trigger = IntervalTrigger(minutes=b['interval_minutes'])
                    scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)
            else:
                trigger = CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE)
                scheduler.add_job(send_broadcast, trigger, args=[broadcast_id], id=job_id)

async def show_broadcast_edit_menu(message, broadcast_id):
    """Показывает меню редактирования рассылки"""
    b = db.get_broadcast(broadcast_id)
    if not b:
        await message.answer("❌ Рассылка не найдена")
        return
    
    group = db.get_target_group(b['group_id'])
    group_name = group['name'] if group else "?"
    
    schedule_info = get_schedule_info(b)
    status = "✅ Активна" if b['is_active'] else "⛔ Отключена"
    btn_text = b.get('button_text') or "❌ нет"
    edit_msg = b.get('edit_message') or "стандартный"
    
    text = f"✏️ **Редактирование рассылки**\n\n"
    text += f"📢 Название: `{b['name']}`\n"
    text += f"📬 Группа: {group_name}\n"
    text += f"📝 Текст: {b['text'][:100]}...\n" if len(b['text']) > 100 else f"📝 Текст: {b['text']}\n"
    text += f"{schedule_info}\n"
    text += f"🔘 Кнопка: `{btn_text}`\n"
    text += f"✏️ Текст после нажатия: {edit_msg[:50]}...\n" if len(edit_msg) > 50 else f"✏️ Текст после нажатия: {edit_msg}\n"
    text += f"📊 Статус: {status}\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_name_{broadcast_id}"),
         InlineKeyboardButton(text="📝 Текст", callback_data=f"edit_text_{broadcast_id}")],
        [InlineKeyboardButton(text="🔘 Кнопка", callback_data=f"edit_button_{broadcast_id}"),
         InlineKeyboardButton(text="✏️ Текст после нажатия", callback_data=f"edit_editmsg_{broadcast_id}")],
        [InlineKeyboardButton(text="⏰ Расписание", callback_data=f"edit_schedule_{broadcast_id}"),
         InlineKeyboardButton(text="🔄 Статус", callback_data=f"edit_status_{broadcast_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"edit_delete_{broadcast_id}")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="menu_list")]
    ])
    
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

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
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    if not broadcasts:
        text += "Нет рассылок"
    else:
        for b in broadcasts:
            status = "✅" if b['is_active'] else "⛔"
            schedule_info = get_schedule_info(b)
            btn = f" 🔘 {b['button_text']}" if b.get('button_text') else ""
            text += f"{status} **{b['name']}**{btn}\n   ⏰ {schedule_info}\n\n"
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=f"✏️ {b['name'][:20]}", callback_data=f"broadcast_show_{b['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"broadcast_delete_{b['id']}")
            ])
    
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="➕ Создать рассылку", callback_data=f"select_group_{group_id}")])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад к группам", callback_data="menu_groups")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_broadcasts(message):
    all_b = db.get_all_broadcasts()
    if not all_b:
        await message.edit_text("📭 **Нет рассылок**", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main")]]))
        return
    
    # Группируем рассылки по группам
    broadcasts_by_group = {}
    for b in all_b:
        group_id = b['group_id']
        if group_id not in broadcasts_by_group:
            broadcasts_by_group[group_id] = []
        broadcasts_by_group[group_id].append(b)
    
    text = "📋 **Все рассылки**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    
    # Сортируем группы и выводим рассылки по группам
    for group_id, broadcasts in broadcasts_by_group.items():
        group = db.get_target_group(group_id)
        group_name = group['name'] if group else f"Группа {group_id}"
        
        text += f"📢 **{group_name}**\n"
        
        for b in broadcasts:
            status = "✅" if b['is_active'] else "⛔"
            btn = f" [{b['button_text']}]" if b.get('button_text') else ""
            text += f"   {status} **{b['name']}**{btn}\n"
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(text=f"✏️ {b['name'][:20]}", callback_data=f"broadcast_show_{b['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"broadcast_delete_{b['id']}")
            ])
        text += "\n"
    
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
    groups = db.get_all_target_groups()
    broadcasts = [b for b in db.get_all_broadcasts() if b.get('button_text')]
    
    text = "📊 **Выберите режим просмотра**\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Общий топ (все группы)", callback_data="top_all")]
    ])
    
    if groups:
        text += "**Или выберите группу:**\n\n"
        for g in groups:
            cnt = db.get_total_clicks_by_group(g['id'])
            status = "✅" if g['is_active'] else "⛔"
            text += f"{status} **{g['name']}** — {cnt} нажатий\n"
            keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"📊 {g['name'][:25]}", callback_data=f"top_group_{g['id']}")])
    
    if broadcasts:
        text += "\n**Или выберите конкретную рассылку:**\n\n"
        for b in broadcasts:
            cnt = db.get_clicks_count(b['id'])
            group = db.get_target_group(b['group_id'])
            gname = group['name'] if group else "?"
            text += f"🔘 {b['name']} ({gname}) — {cnt} нажатий\n"
            keyboard.inline_keyboard.append([InlineKeyboardButton(text=f"📨 {b['name'][:25]}", callback_data=f"top_{b['id']}")])
    
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
    
    text = f"📊 **Статистика по рассылке**\n\n"
    text += f"📢 Рассылка: **{b['name']}**\n"
    text += f"🔘 Кнопка: `{b['button_text']}`\n\n"
    text += "**Рейтинг (по средней скорости):**\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        user_id, username, first_name, avg_time, clicks_count = row
        name = first_name or username or str(user_id)
        medal = medals[i] if i < 3 else f"{i+1}."
        avg_str = format_time(avg_time)
        if len(name) > 20:
            name = name[:17] + "..."
        text += f"{medal} **{name}** — {avg_str} ({clicks_count} наж.)\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="menu_top")]
    ])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_group_top(message, group_id):
    group = db.get_target_group(group_id)
    if not group:
        await message.answer("❌ Группа не найдена")
        return
    
    top = db.get_top_fastest_by_group(group_id, 20)
    
    if not top:
        await message.edit_text(f"📭 Нет данных о реакции в группе {group['name']}", parse_mode="Markdown")
        return
    
    total_clicks = db.get_total_clicks_by_group(group_id)
    total_users = db.get_total_users_by_group(group_id)
    
    text = f"📊 **Список времени реакции сотрудников**\n\n"
    text += f"📢 Группа: **{group['name']}**\n"
    text += f"👆 Всего нажатий: {total_clicks}\n"
    text += f"👥 Участников: {total_users}\n\n"
    text += "**Рейтинг (по средней скорости):**\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        user_id, username, first_name, avg_time, clicks_count = row
        name = first_name or username or str(user_id)
        medal = medals[i] if i < 3 else f"{i+1}."
        avg_str = format_time(avg_time)
        if len(name) > 20:
            name = name[:17] + "..."
        text += f"{medal} **{name}** — {avg_str} ({clicks_count} наж.)\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к выбору", callback_data="menu_top")]
    ])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def show_all_broadcasts_top(message):
    top = db.get_top_fastest_all(20)
    
    if not top:
        await message.edit_text("📭 **Нет данных о реакции сотрудников**", parse_mode="Markdown")
        return
    
    total_clicks = db.get_total_clicks_all()
    total_users = db.get_total_users_all()
    
    text = f"📊 **Общий список времени реакции сотрудников**\n\n"
    text += f"📅 Данные собраны за всё время\n"
    text += f"👆 Всего нажатий: {total_clicks}\n"
    text += f"👥 Участников: {total_users}\n\n"
    text += "**Рейтинг (по средней скорости):**\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(top):
        user_id, username, first_name, avg_time, clicks_count = row
        name = first_name or username or str(user_id)
        medal = medals[i] if i < 3 else f"{i+1}."
        avg_str = format_time(avg_time)
        if len(name) > 20:
            name = name[:17] + "..."
        text += f"{medal} **{name}** — {avg_str} ({clicks_count} наж.)\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к выбору", callback_data="menu_top")]
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
        [InlineKeyboardButton(text="📈 Список реакции", callback_data=f"top_{broadcast_id}")],
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
                tz = pytz.timezone(TIMEZONE)
                now = datetime.now(tz)
                start_time = now.replace(hour=b['start_hour'], minute=b['start_minute'], second=0, microsecond=0)
                if start_time < now:
                    if b['interval_minutes']:
                        trigger = IntervalTrigger(minutes=b['interval_minutes'])
                        scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
                else:
                    trigger = CronTrigger(hour=b['start_hour'], minute=b['start_minute'], timezone=TIMEZONE)
                    scheduler.add_job(send_broadcast, trigger, args=[b['id']], id=job_id)
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
    broadcast_id = state.get("broadcast_id")
    
    # === РЕДАКТИРОВАНИЕ ===
    if step == "edit_name":
        new_name = message.text
        await update_broadcast_and_reload(broadcast_id, name=new_name)
        await message.answer(f"✅ Название рассылки изменено на: {new_name}")
        del admin_states[message.from_user.id]
        await show_broadcasts(message)
    
    elif step == "edit_text":
        new_text = message.text
        await update_broadcast_and_reload(broadcast_id, text=new_text)
        await message.answer(f"✅ Текст рассылки изменён")
        del admin_states[message.from_user.id]
        await show_broadcasts(message)
    
    elif step == "edit_button":
        if message.text.lower() in ["пропустить", "skip", "-", "нет"]:
            new_button_text = None
            await message.answer("✅ Кнопка убрана")
        else:
            new_button_text = message.text[:50]
            await message.answer(f"✅ Текст кнопки изменён на: {new_button_text}")
        await update_broadcast_and_reload(broadcast_id, button_text=new_button_text)
        del admin_states[message.from_user.id]
        await show_broadcasts(message)
    
    elif step == "edit_editmsg":
        if message.text.lower() in ["пропустить", "skip", "-"]:
            new_edit_msg = "✅ Нажал: {mention}\n🆔 ID: {user_id}\n🕐 Время: {time}\n⚡ Реакция: {reaction}"
            await message.answer("✅ Текст после нажатия сброшен на стандартный")
        else:
            new_edit_msg = message.text
            await message.answer("✅ Текст после нажатия изменён")
        await update_broadcast_and_reload(broadcast_id, edit_message=new_edit_msg)
        del admin_states[message.from_user.id]
        await show_broadcasts(message)
    
    elif step == "edit_schedule_type":
        if message.text == "1":
            state["new_schedule_type"] = "fixed"
            state["step"] = "edit_fixed_time"
            await message.answer(f"⏰ Введите время в формате `HH:MM` (часовой пояс {TIMEZONE})", parse_mode="Markdown")
        elif message.text == "2":
            state["new_schedule_type"] = "interval"
            state["step"] = "edit_interval"
            await message.answer("⏰ Введите интервал в минутах:", parse_mode="Markdown")
        elif message.text == "3":
            state["new_schedule_type"] = "start_at_interval"
            state["step"] = "edit_start_time"
            await message.answer(f"🚀 Введите первое время в формате `HH:MM` (часовой пояс {TIMEZONE})", parse_mode="Markdown")
        else:
            await message.answer("❌ Отправьте 1, 2 или 3")
    
    elif step == "edit_fixed_time":
        time_str = message.text.strip().replace(' ', '')
        if ':' not in time_str:
            await message.answer("❌ Неверный формат. Используйте разделитель `:`\n\nПример: `14:30`", parse_mode="Markdown")
            return
        
        parts = time_str.split(':')
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте формат `HH:MM`", parse_mode="Markdown")
            return
        
        try:
            h = int(parts[0])
            m = int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                await update_broadcast_and_reload(
                    broadcast_id,
                    schedule_type="fixed",
                    hour=h,
                    minute=m,
                    interval_minutes=None,
                    start_hour=None,
                    start_minute=None
                )
                await message.answer(f"✅ Расписание изменено: ежедневно в {h:02d}:{m:02d}")
                del admin_states[message.from_user.id]
                await show_broadcasts(message)
            else:
                await message.answer("❌ Неверное время. Часы от 0 до 23, минуты от 0 до 59.", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите числа.", parse_mode="Markdown")
    
    elif step == "edit_interval":
        try:
            interval = int(message.text.strip())
            if interval > 0:
                await update_broadcast_and_reload(
                    broadcast_id,
                    schedule_type="interval",
                    interval_minutes=interval,
                    hour=None,
                    minute=None,
                    start_hour=None,
                    start_minute=None
                )
                await message.answer(f"✅ Расписание изменено: каждые {interval} минут")
                del admin_states[message.from_user.id]
                await show_broadcasts(message)
            else:
                await message.answer("❌ Введите положительное число (больше 0).", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите целое число.", parse_mode="Markdown")
    
    elif step == "edit_start_time":
        time_str = message.text.strip().replace(' ', '')
        if ':' not in time_str:
            await message.answer("❌ Неверный формат. Используйте разделитель `:`\n\nПример: `14:30`", parse_mode="Markdown")
            return
        
        parts = time_str.split(':')
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте формат `HH:MM`", parse_mode="Markdown")
            return
        
        try:
            h = int(parts[0])
            m = int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                state["new_start_hour"] = h
                state["new_start_minute"] = m
                state["step"] = "edit_interval_after_start"
                await message.answer(
                    "⏰ **Интервал повторения**\n\n"
                    "Введите интервал в минутах (через сколько минут повторять):\n\n"
                    "Примеры:\n"
                    "`120` - каждые 2 часа\n"
                    "`60` - каждый час\n\n"
                    "Отправьте число:",
                    parse_mode="Markdown"
                )
            else:
                await message.answer("❌ Неверное время. Часы от 0 до 23, минуты от 0 до 59.", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите числа.", parse_mode="Markdown")
    
    elif step == "edit_interval_after_start":
        try:
            interval = int(message.text.strip())
            if interval > 0:
                await update_broadcast_and_reload(
                    broadcast_id,
                    schedule_type="start_at_interval",
                    interval_minutes=interval,
                    start_hour=state["new_start_hour"],
                    start_minute=state["new_start_minute"],
                    hour=None,
                    minute=None
                )
                await message.answer(f"✅ Расписание изменено: старт {state['new_start_hour']:02d}:{state['new_start_minute']:02d}, затем каждые {interval} минут")
                del admin_states[message.from_user.id]
                await show_broadcasts(message)
            else:
                await message.answer("❌ Введите положительное число (больше 0).", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите целое число.", parse_mode="Markdown")
    
    # === ДОБАВЛЕНИЕ ГРУППЫ ===
    elif step == "add_group_id":
        try:
            chat_id = int(message.text.strip())
            if db.get_target_group_by_chat_id(str(chat_id)):
                await message.answer("❌ Группа уже добавлена")
            else:
                db.add_target_group(str(chat_id), f"Группа {chat_id}")
                await message.answer(f"✅ Группа `{chat_id}` добавлена", parse_mode="Markdown")
                logger.info(f"➕ Добавлена группа вручную: {chat_id}")
            del admin_states[message.from_user.id]
            await show_groups(message)
        except:
            await message.answer("❌ Неверный ID. Введите число.")
    
    # === СОЗДАНИЕ НОВОЙ РАССЫЛКИ ===
    elif step == "name":
        state["name"] = message.text
        state["step"] = "text"
        await message.answer("📝 **Введите текст рассылки**", parse_mode="Markdown")
    
    elif step == "text":
        state["text"] = message.text
        state["step"] = "button"
        await message.answer(
            "🔘 **Добавить кнопку?**\n\n"
            "Отправьте текст кнопки или `пропустить`",
            parse_mode="Markdown"
        )
    
    elif step == "button":
        if message.text.lower() in ["пропустить", "skip", "-"]:
            state["button_text"] = None
            state["edit_message"] = None
            state["step"] = "schedule_type"
            await message.answer(
                "⏰ **Тип расписания**\n\n"
                "`1` - В определённое время (ежедневно)\n"
                "`2` - Простой интервал\n"
                "`3` - Старт в указанное время + интервал\n\n"
                "Отправьте 1, 2 или 3:",
                parse_mode="Markdown"
            )
        else:
            state["button_text"] = message.text[:50]
            state["step"] = "edit_message"
            await message.answer(
                "✏️ **Текст после нажатия**\n\n"
                "Доступные переменные:\n"
                "`{mention}` - упоминание\n"
                "`{name}` - имя\n"
                "`{user_id}` - ID\n"
                "`{time}` - время (ЧЧ:ММ)\n"
                "`{reaction}` - время реакции\n\n"
                "Отправьте текст или `пропустить`",
                parse_mode="Markdown"
            )
    
    elif step == "edit_message":
        if message.text.lower() in ["пропустить", "skip", "-"]:
            state["edit_message"] = "✅ Нажал: {mention}\n🕐 Время: {time}\n⚡ Реакция: {reaction}"
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
    
    elif step == "schedule_type":
        if message.text == "1":
            state["schedule_type"] = "fixed"
            state["step"] = "fixed_time"
            await message.answer(f"⏰ Введите время в формате `HH:MM` (часовой пояс {TIMEZONE})", parse_mode="Markdown")
        elif message.text == "2":
            state["schedule_type"] = "interval"
            state["step"] = "interval"
            await message.answer("⏰ Введите интервал в минутах:", parse_mode="Markdown")
        elif message.text == "3":
            state["schedule_type"] = "start_at_interval"
            state["step"] = "start_time"
            await message.answer(f"🚀 Введите первое время в формате `HH:MM` (часовой пояс {TIMEZONE})", parse_mode="Markdown")
        else:
            await message.answer("❌ Отправьте 1, 2 или 3")
    
    elif step == "fixed_time":
        time_str = message.text.strip().replace(' ', '')
        if ':' not in time_str:
            await message.answer("❌ Неверный формат. Используйте разделитель `:`\n\nПример: `14:30`", parse_mode="Markdown")
            return
        
        parts = time_str.split(':')
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте формат `HH:MM`", parse_mode="Markdown")
            return
        
        try:
            h = int(parts[0])
            m = int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                state["hour"] = h
                state["minute"] = m
                await save_broadcast(message, state)
            else:
                await message.answer("❌ Неверное время. Часы от 0 до 23, минуты от 0 до 59.", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите числа.", parse_mode="Markdown")
    
    elif step == "interval":
        try:
            interval = int(message.text.strip())
            if interval > 0:
                state["interval_minutes"] = interval
                await save_broadcast(message, state)
            else:
                await message.answer("❌ Введите положительное число (больше 0).", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите целое число.", parse_mode="Markdown")
    
    elif step == "start_time":
        time_str = message.text.strip().replace(' ', '')
        if ':' not in time_str:
            await message.answer("❌ Неверный формат. Используйте разделитель `:`\n\nПример: `14:30`", parse_mode="Markdown")
            return
        
        parts = time_str.split(':')
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте формат `HH:MM`", parse_mode="Markdown")
            return
        
        try:
            h = int(parts[0])
            m = int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                state["start_hour"] = h
                state["start_minute"] = m
                state["step"] = "interval_after_start"
                await message.answer(
                    "⏰ **Интервал повторения**\n\n"
                    "Введите интервал в минутах (через сколько минут повторять):\n\n"
                    "Примеры:\n"
                    "`120` - каждые 2 часа\n"
                    "`60` - каждый час\n\n"
                    "Отправьте число:",
                    parse_mode="Markdown"
                )
            else:
                await message.answer("❌ Неверное время. Часы от 0 до 23, минуты от 0 до 59.", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите числа.", parse_mode="Markdown")
    
    elif step == "interval_after_start":
        try:
            interval = int(message.text.strip())
            if interval > 0:
                state["interval_minutes"] = interval
                await save_broadcast(message, state)
            else:
                await message.answer("❌ Введите положительное число (больше 0).", parse_mode="Markdown")
        except ValueError:
            await message.answer("❌ Неверный формат. Введите целое число.", parse_mode="Markdown")

async def save_broadcast(message, state):
    broadcast_id = db.add_broadcast(
        group_id=state["group_id"],
        name=state["name"],
        text=state["text"],
        schedule_type=state["schedule_type"],
        hour=state.get("hour"),
        minute=state.get("minute"),
        interval_minutes=state.get("interval_minutes"),
        start_hour=state.get("start_hour"),
        start_minute=state.get("start_minute"),
        button_text=state.get("button_text"),
        edit_message=state.get("edit_message")
    )
    
    await update_broadcast_and_reload(broadcast_id, is_active=True)
    
    group = db.get_target_group(state["group_id"])
    
    btn_info = f"\n🔘 Кнопка: `{state['button_text']}`" if state.get('button_text') else ""
    
    if state["schedule_type"] == "fixed":
        schedule_info = f"⏰ {state['hour']:02d}:{state['minute']:02d} ежедневно"
    elif state["schedule_type"] == "interval":
        mins = state['interval_minutes']
        hours = mins // 60
        minutes = mins % 60
        if hours > 0:
            schedule_info = f"⏰ каждые {hours}ч {minutes}мин" if minutes > 0 else f"⏰ каждые {hours}ч"
        else:
            schedule_info = f"⏰ каждые {minutes}мин"
    else:
        mins = state['interval_minutes']
        hours = mins // 60
        minutes = mins % 60
        if hours > 0:
            interval_str = f"каждые {hours}ч {minutes}мин" if minutes > 0 else f"каждые {hours}ч"
        else:
            interval_str = f"каждые {minutes}мин"
        schedule_info = f"🚀 старт {state['start_hour']:02d}:{state['start_minute']:02d}, затем {interval_str}"
    
    # Отправляем сообщение с кнопкой возврата в админ-панель
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Вернуться в админ-панель", callback_data="back_to_main")]
    ])
    
    await message.answer(
        f"✅ **Рассылка создана!**\n\n"
        f"📢 Название: {state['name']}\n"
        f"📬 Группа: {group['name']}\n"
        f"{schedule_info}{btn_info}\n\n"
        f"✏️ Редактировать рассылку можно через меню 'Все рассылки'",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    
    logger.info(f"➕ Создана рассылка: {state['name']} в группу {group['name']} | Тип: {state['schedule_type']}")
    
    del admin_states[message.from_user.id]

# ==================== ЗАПУСК БОТА ====================
async def main():
    logger.info("🚀 БОТ ЗАПУСКАЕТСЯ...")
    logger.info(f"📅 Часовой пояс: {TIMEZONE}")
    logger.info(f"👑 Администраторы: {ADMIN_IDS}")
    logger.info(f"👁 Наблюдатели (/top): {TOP_VIEWERS}")
    logger.info(f"💾 Путь к базе данных: {DB_PATH}")
    
    if os.path.exists(VOLUME_PATH):
        logger.info(f"✅ Volume найден по пути: {VOLUME_PATH}")
        if os.access(VOLUME_PATH, os.W_OK):
            logger.info(f"✅ Есть права на запись в Volume")
        else:
            logger.warning(f"⚠️ Нет прав на запись в Volume!")
    else:
        logger.warning(f"⚠️ Volume не найден, БД будет сохранена локально")
    
    await load_broadcasts()
    scheduler.start()
    
    logger.info("✅ БОТ ГОТОВ К РАБОТЕ!")
    logger.info(f"✅ Бот запущен! База данных: {DB_PATH}")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())