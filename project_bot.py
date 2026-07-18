"""
Project Status Tracking Bot
============================
Установка:  pip install python-telegram-bot apscheduler
Запуск:     python project_bot.py
"""

import sqlite3
import logging
from datetime import date, datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BOT_TOKEN     = "8549647812:AAGk5t7-nLe5bmzH3FQ0UDeji-fXNhDWqrA"
MANAGER_ID    = 126009180
TIMEZONE      = "Asia/Tashkent"
REMINDER_HOUR = 9
REMINDER_MIN  = 0
DB_PATH       = "projects.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

STATUS_POSTPONED    = "postponed"
STATUS_WON_UNSIGNED = "won_unsigned"
STATUS_WON_SIGNED   = "won_signed"
STATUS_LOST         = "lost"
STATUS_CUSTOM       = "custom"

STATUS_LABELS = {
    STATUS_POSTPONED:    "Перенос",
    STATUS_WON_UNSIGNED: "Выигран, договор еще не подписан",
    STATUS_WON_SIGNED:   "Выигран, договор подписан",
    STATUS_LOST:         "Закрыт, проигран",
    STATUS_CUSTOM:       "Свой вариант",
}

STATUS_ICONS = {
    STATUS_POSTPONED:    "🔄",
    STATUS_WON_UNSIGNED: "🏆",
    STATUS_WON_SIGNED:   "✅",
    STATUS_LOST:         "❌",
    STATUS_CUSTOM:       "💬",
}


# ── Формат даты ───────────────────────────────────────────────────────────────

def parse_date(text: str) -> date:
    text = text.strip()
    for fmt in ("%d-%m-%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Неверный формат: {text}")

def fmt_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d-%m-%y")
    except Exception:
        return iso


# ── База данных ────────────────────────────────────────────────────────────────

def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS subordinates (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id    INTEGER NOT NULL UNIQUE,
                name     TEXT,
                username TEXT,
                added_at TEXT DEFAULT (datetime('now'))
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                deadline    TEXT NOT NULL,
                last_status TEXT,
                status_type TEXT,
                updated_at  TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS project_assignees (
                project_id INTEGER,
                sub_tg_id  INTEGER,
                PRIMARY KEY (project_id, sub_tg_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER,
                status      TEXT,
                status_type TEXT,
                new_date    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        # pending_reminders: отслеживает неотвеченные напоминания
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending_reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_tg_id  INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                sent_at    TEXT NOT NULL,
                retry_sent INTEGER DEFAULT 0
            )
        """)
        con.commit()

# — Подчинённые —

def db_add_subordinate(tg_id, name, username):
    with sqlite3.connect(DB_PATH) as con:
        try:
            con.execute(
                "INSERT INTO subordinates (tg_id, name, username) VALUES (?,?,?)",
                (tg_id, name, username)
            )
        except sqlite3.IntegrityError:
            con.execute(
                "UPDATE subordinates SET name=?, username=? WHERE tg_id=?",
                (name, username, tg_id)
            )
        con.commit()

def db_remove_subordinate(tg_id):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM subordinates WHERE tg_id=?", (tg_id,))
        con.commit()
        return cur.rowcount > 0

def db_list_subordinates():
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT tg_id, name, username FROM subordinates ORDER BY name"
        ).fetchall()

def db_is_subordinate(tg_id):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT 1 FROM subordinates WHERE tg_id=?", (tg_id,)).fetchone() is not None

# — Проекты —

def db_add_project(name, deadline_iso):
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("INSERT INTO projects (name, deadline) VALUES (?,?)", (name, deadline_iso))
            con.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def db_get_project_id(name):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT id FROM projects WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

def db_remove_project(name):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM projects WHERE name=?", (name,))
        con.commit()
        return cur.rowcount > 0

def db_list_projects():
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT id, name, deadline, last_status, status_type, updated_at FROM projects ORDER BY deadline"
        ).fetchall()

def db_get_project_by_name(name):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT id, name, deadline FROM projects WHERE name=?", (name,)).fetchone()

def db_update_status(project_id, status, status_type, new_deadline_iso):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE projects SET last_status=?, status_type=?, deadline=?, updated_at=datetime('now') WHERE id=?",
            (status, status_type, new_deadline_iso, project_id)
        )
        con.execute(
            "INSERT INTO history (project_id, status, status_type, new_date) VALUES (?,?,?,?)",
            (project_id, status, status_type, new_deadline_iso)
        )
        con.commit()

def db_projects_due_today():
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT id, name, deadline FROM projects WHERE deadline=?", (today,)
        ).fetchall()

# — Назначения —

def db_set_assignees(project_id, sub_ids):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM project_assignees WHERE project_id=?", (project_id,))
        for sid in sub_ids:
            con.execute("INSERT OR IGNORE INTO project_assignees VALUES (?,?)", (project_id, sid))
        con.commit()

def db_get_assignees(project_id):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT sub_tg_id FROM project_assignees WHERE project_id=?", (project_id,)
        ).fetchall()
        return [r[0] for r in rows]

def db_all_projects_for_sub(sub_tg_id):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("""
            SELECT p.id, p.name, p.deadline, p.last_status, p.status_type, p.updated_at
            FROM projects p
            JOIN project_assignees pa ON pa.project_id = p.id
            WHERE pa.sub_tg_id=?
            ORDER BY p.deadline
        """, (sub_tg_id,)).fetchall()

def db_projects_due_today_for_sub(sub_tg_id):
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("""
            SELECT p.id, p.name, p.deadline
            FROM projects p
            JOIN project_assignees pa ON pa.project_id = p.id
            WHERE pa.sub_tg_id=? AND p.deadline=?
        """, (sub_tg_id, today)).fetchall()

# — Отслеживание неотвеченных напоминаний —

def db_add_pending(sub_tg_id, project_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO pending_reminders (sub_tg_id, project_id, sent_at) VALUES (?,?,datetime('now'))",
            (sub_tg_id, project_id)
        )
        con.commit()

def db_clear_pending(sub_tg_id, project_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM pending_reminders WHERE sub_tg_id=? AND project_id=?",
            (sub_tg_id, project_id)
        )
        con.commit()

def db_clear_all_pending_for_sub(sub_tg_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM pending_reminders WHERE sub_tg_id=?", (sub_tg_id,))
        con.commit()

def db_get_overdue_pending(minutes=30):
    """Напоминания, отправленные более N минут назад, без повторного напоминания."""
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("""
            SELECT id, sub_tg_id, project_id FROM pending_reminders
            WHERE retry_sent=0
              AND datetime(sent_at, '+' || ? || ' minutes') <= datetime('now')
        """, (minutes,)).fetchall()

def db_mark_retry_sent(reminder_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE pending_reminders SET retry_sent=1 WHERE id=?", (reminder_id,))
        con.commit()


def is_manager(uid): return uid == MANAGER_ID

def sub_display(name, username):
    return f"@{username}" if username else name

def format_project_list():
    rows = db_list_projects()
    if not rows:
        return "Проектов пока нет."
    today = date.today()
    lines = ["📋 *Список проектов:*\n"]
    for pid, name, deadline, last_status, status_type, updated_at in rows:
        try:
            dl = date.fromisoformat(deadline)
            diff = (dl - today).days
            if diff < 0:    flag = "🔴"
            elif diff == 0: flag = "🔔"
            elif diff <= 3: flag = "🟡"
            else:           flag = "🟢"
            days_str = f"через {diff} дн." if diff > 0 else ("сегодня" if diff == 0 else f"просрочен {-diff} дн.")
        except Exception:
            flag, days_str = "❓", ""
        icon = STATUS_ICONS.get(status_type or "", "")
        st = f"\n    └ {icon} {last_status}" if last_status else ""
        upd = f" _(обновлено {fmt_date(updated_at[:10])})_" if updated_at else ""
        subs = db_list_subordinates()
        sub_map = {s[0]: sub_display(s[1], s[2]) for s in subs}
        assignees = db_get_assignees(pid)
        assigned_str = ", ".join(sub_map[a] for a in assignees if a in sub_map)
        assigned_line = f"\n    👤 {assigned_str}" if assigned_str else ""
        lines.append(f"{flag} *{name}*\n    Контроль: `{fmt_date(deadline)}` ({days_str}){upd}{assigned_line}{st}")
    return "\n\n".join(lines)


# ── Состояние диалога ─────────────────────────────────────────────────────────

dialog_state  = {}  # sub_id -> state
manager_state = {}  # менеджер: состояние создания проекта


# ── Вспомогательные ───────────────────────────────────────────────────────────

async def save_and_notify(bot, sub_id, state, status_text, status_type, new_date_iso):
    db_update_status(state["project_id"], status_text, status_type, new_date_iso)
    db_clear_pending(sub_id, state["project_id"])
    dialog_state.pop(sub_id, None)
    icon = STATUS_ICONS.get(status_type, "")
    await bot.send_message(
        MANAGER_ID,
        f"📬 *Обновление статуса*\n\n"
        f"*Проект:* {state['project_name']}\n"
        f"{icon} *Статус:* {status_text}\n"
        f"*Следующий контроль:* `{fmt_date(new_date_iso)}`",
        parse_mode="Markdown"
    )

def build_assignee_keyboard(selected_ids: set):
    subs = db_list_subordinates()
    btns = []
    for tg_id, name, username in subs:
        label = sub_display(name, username)
        check = "✅ " if tg_id in selected_ids else ""
        btns.append([InlineKeyboardButton(f"{check}{label}", callback_data=f"assign_toggle:{tg_id}")])
    btns.append([InlineKeyboardButton("Сохранить →", callback_data="assign_done")])
    return InlineKeyboardMarkup(btns)

async def _send_update_request(bot, sub_id, projects):
    btns = [
        [InlineKeyboardButton(
            f"{name}  |  {fmt_date(deadline)}",
            callback_data=f"update:{pid}:{name}"
        )]
        for pid, name, deadline, *_ in projects
    ]
    await bot.send_message(
        sub_id,
        "📋 *Обнови статус по проектам.* Выбери проект:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown"
    )
    # Фиксируем каждый проект как ожидающий ответа
    for pid, *_ in projects:
        db_add_pending(sub_id, pid)


# ── Команды ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = update.effective_user

    if is_manager(uid):
        await update.message.reply_text(
            "👋 Привет! Ты подключён как *менеджер*.\n\n"
            "Команды:\n"
            "/add — добавить проект\n"
            "/remove Название — удалить проект\n"
            "/list — все проекты\n"
            "/report — запросить обновление\n"
            "/history Название — история проекта\n"
            "/subordinates — список подчинённых\n"
            "/remove\\_sub ID — удалить подчинённого",
            parse_mode="Markdown"
        )
        return

    if db_is_subordinate(uid):
        await update.message.reply_text("👋 Привет! Ожидай напоминаний от менеджера.")
        return

    # Новый пользователь — уведомляем менеджера
    name = user.full_name or "Без имени"
    username = user.username or ""
    ctx.bot_data[f"pending_{uid}"] = {"name": name, "username": username}

    btns = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Добавить", callback_data=f"approve:{uid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{uid}"),
    ]])
    uname_str = f"@{username}" if username else "нет username"
    await ctx.bot.send_message(
        MANAGER_ID,
        f"👤 *Запрос на доступ:*\n\nИмя: {name}\nUsername: {uname_str}\nID: `{uid}`",
        reply_markup=btns,
        parse_mode="Markdown"
    )
    await update.message.reply_text("Твой запрос отправлен менеджеру. Ожидай подтверждения.")

async def cmd_subordinates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    rows = db_list_subordinates()
    if not rows:
        await update.message.reply_text("Подчинённых пока нет. Попроси их написать /start боту.")
        return
    lines = ["👥 *Список подчинённых:*\n"]
    for tg_id, name, username in rows:
        uname_str = f"@{username}" if username else "—"
        lines.append(f"• *{name}* ({uname_str})\n  ID: `{tg_id}`")
    lines.append("\nДля удаления: /remove\\_sub ID")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def cmd_remove_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text("⚠️ Формат: /remove_sub TELEGRAM_ID")
        return
    try:
        tg_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID должен быть числом.")
        return
    if db_remove_subordinate(tg_id):
        await update.message.reply_text(f"🗑 Подчинённый `{tg_id}` удалён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ Подчинённый с ID `{tg_id}` не найден.", parse_mode="Markdown")

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Начинает диалог добавления проекта: сначала спрашивает название."""
    if not is_manager(update.effective_user.id): return
    manager_state[MANAGER_ID] = {"step": "ask_name"}
    await update.message.reply_text("Введи название проекта:")

async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("⚠️ Формат: /remove Название проекта")
        return
    if db_remove_project(name):
        await update.message.reply_text(f"🗑 Проект *{name}* удалён.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ Проект *{name}* не найден.", parse_mode="Markdown")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    await update.message.reply_text(format_project_list(), parse_mode="Markdown")

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    subs = db_list_subordinates()
    if not subs:
        await update.message.reply_text("Подчинённых пока нет.")
        return
    sent = 0
    for sub_id, name, username in subs:
        proj = db_all_projects_for_sub(sub_id)
        if proj:
            await _send_update_request(ctx.bot, sub_id, proj)
            sent += 1
    if sent:
        await update.message.reply_text(f"📨 Запрос отправлен {sent} подчинённым.")
    else:
        await update.message.reply_text("Ни один подчинённый не назначен ни на один проект.")

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    name = " ".join(ctx.args).strip()
    proj = db_get_project_by_name(name)
    if not proj:
        await update.message.reply_text(f"⚠️ Проект *{name}* не найден.", parse_mode="Markdown")
        return
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT status, status_type, new_date, created_at FROM history WHERE project_id=? ORDER BY created_at DESC LIMIT 10",
            (proj[0],)
        ).fetchall()
    if not rows:
        await update.message.reply_text(f"История по *{name}* пуста.", parse_mode="Markdown")
        return
    lines = [f"📜 *История: {name}*\n"]
    for status, status_type, new_date, created_at in rows:
        icon = STATUS_ICONS.get(status_type or "", "")
        lines.append(f"`{fmt_date(created_at[:10])}` → контроль `{fmt_date(new_date)}`\n{icon} _{status}_")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


# ── Текстовые сообщения менеджера (диалог создания проекта) ───────────────────

async def handle_manager_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_manager(uid): return

    state = manager_state.get(MANAGER_ID)
    if not state:
        return

    step = state.get("step")

    if step == "ask_name":
        name = update.message.text.strip()
        if not name:
            await update.message.reply_text("Название не может быть пустым. Введи название проекта:")
            return
        state["name"] = name
        state["step"] = "ask_date"
        manager_state[MANAGER_ID] = state
        await update.message.reply_text(
            f"Проект: *{name}*\n\nТеперь введи дату контроля в формате ДД-ММ-ГГ:",
            parse_mode="Markdown"
        )

    elif step == "ask_date":
        try:
            dl = parse_date(update.message.text)
        except ValueError:
            await update.message.reply_text("Неверный формат. Введи дату как ДД-ММ-ГГ (например 25-07-26):")
            return
        if dl < date.today():
            await update.message.reply_text("Нельзя назначить контроль на прошедшую дату. Введи другую дату:")
            return

        name = state["name"]
        if not db_add_project(name, dl.isoformat()):
            await update.message.reply_text(f"⚠️ Проект *{name}* уже существует.", parse_mode="Markdown")
            manager_state.pop(MANAGER_ID, None)
            return

        proj_id = db_get_project_id(name)
        subs = db_list_subordinates()

        if not subs:
            manager_state.pop(MANAGER_ID, None)
            await update.message.reply_text(
                f"✅ Проект *{name}* добавлен. Контроль: `{fmt_date(dl.isoformat())}`\n"
                f"_(подчинённых нет, назначения пропущены)_",
                parse_mode="Markdown"
            )
            return

        state["project_id"] = proj_id
        state["deadline"] = dl.isoformat()
        state["selected"] = set()
        state["step"] = "assign"
        manager_state[MANAGER_ID] = state

        await update.message.reply_text(
            f"✅ Проект *{name}* создан. Контроль: `{fmt_date(dl.isoformat())}`\n\n"
            f"Выбери подчинённых для этого проекта:",
            reply_markup=build_assignee_keyboard(set()),
            parse_mode="Markdown"
        )


# ── Обработчик кнопок ─────────────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    data = query.data
    await query.answer()

    # Одобрить/отклонить нового пользователя
    if data.startswith("approve:") and is_manager(uid):
        new_uid = int(data.split(":")[1])
        pending = ctx.bot_data.get(f"pending_{new_uid}", {})
        name = pending.get("name", "Неизвестно")
        username = pending.get("username", "")
        db_add_subordinate(new_uid, name, username)
        ctx.bot_data.pop(f"pending_{new_uid}", None)
        await query.edit_message_text(f"✅ {name} добавлен как подчинённый.")
        await ctx.bot.send_message(new_uid, "✅ Менеджер дал тебе доступ к боту. Ожидай напоминаний!")

    elif data.startswith("reject:") and is_manager(uid):
        new_uid = int(data.split(":")[1])
        pending = ctx.bot_data.get(f"pending_{new_uid}", {})
        name = pending.get("name", "Неизвестно")
        ctx.bot_data.pop(f"pending_{new_uid}", None)
        await query.edit_message_text(f"❌ {name} отклонён.")
        await ctx.bot.send_message(new_uid, "К сожалению, менеджер не дал доступ к боту.")

    # Выбор назначенных
    elif data.startswith("assign_toggle:") and is_manager(uid):
        sub_id = int(data.split(":")[1])
        state = manager_state.get(MANAGER_ID)
        if not state or state.get("step") != "assign": return
        selected = state["selected"]
        if sub_id in selected:
            selected.discard(sub_id)
        else:
            selected.add(sub_id)
        await query.edit_message_reply_markup(reply_markup=build_assignee_keyboard(selected))

    elif data == "assign_done" and is_manager(uid):
        state = manager_state.pop(MANAGER_ID, None)
        if not state: return
        db_set_assignees(state["project_id"], state["selected"])
        subs = db_list_subordinates()
        sub_map = {s[0]: sub_display(s[1], s[2]) for s in subs}
        assigned_names = ", ".join(sub_map[s] for s in state["selected"] if s in sub_map) or "никто"
        await query.edit_message_text(
            f"✅ Проект *{state['name']}* сохранён.\n"
            f"Контроль: `{fmt_date(state['deadline'])}`\n"
            f"Назначены: {assigned_names}",
            parse_mode="Markdown"
        )

    # Подчинённый выбрал проект
    elif data.startswith("update:") and db_is_subordinate(uid):
        parts = data.split(":", 2)
        proj_id, proj_name = int(parts[1]), parts[2]
        dialog_state[uid] = {
            "project_id": proj_id,
            "project_name": proj_name,
            "step": "choose_status"
        }
        btns = [
            [InlineKeyboardButton(STATUS_LABELS[STATUS_POSTPONED],    callback_data=f"status:{STATUS_POSTPONED}")],
            [InlineKeyboardButton(STATUS_LABELS[STATUS_WON_UNSIGNED], callback_data=f"status:{STATUS_WON_UNSIGNED}")],
            [InlineKeyboardButton(STATUS_LABELS[STATUS_WON_SIGNED],   callback_data=f"status:{STATUS_WON_SIGNED}")],
            [InlineKeyboardButton(STATUS_LABELS[STATUS_LOST],         callback_data=f"status:{STATUS_LOST}")],
            [InlineKeyboardButton(STATUS_LABELS[STATUS_CUSTOM],       callback_data=f"status:{STATUS_CUSTOM}")],
        ]
        await query.message.reply_text(
            f"*{proj_name}*\n\nВыбери статус:",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode="Markdown"
        )

    # Подчинённый выбрал статус
    elif data.startswith("status:") and db_is_subordinate(uid):
        status_type = data.split(":", 1)[1]
        state = dialog_state.get(uid, {})
        state["status_type"] = status_type
        today_iso = date.today().isoformat()

        if status_type == STATUS_LOST:
            await save_and_notify(ctx.bot, uid, state, "Закрыт, проигран", STATUS_LOST, today_iso)
            await query.message.reply_text("Статус сохранён: проект закрыт.")

        elif status_type == STATUS_WON_SIGNED:
            await save_and_notify(ctx.bot, uid, state, "Выигран, договор подписан", STATUS_WON_SIGNED, today_iso)
            await query.message.reply_text("Статус сохранён: договор подписан.")

        elif status_type == STATUS_WON_UNSIGNED:
            state["status_text"] = "Выигран, договор еще не подписан"
            state["step"] = "ask_date"
            dialog_state[uid] = state
            await query.message.reply_text("Укажи следующую контрольную дату в формате ДД-ММ-ГГ:")

        elif status_type == STATUS_POSTPONED:
            state["step"] = "ask_text"
            dialog_state[uid] = state
            await query.message.reply_text("Укажи причину переноса:")

        elif status_type == STATUS_CUSTOM:
            state["step"] = "ask_text"
            dialog_state[uid] = state
            await query.message.reply_text("Напиши свой статус по проекту:")


# ── Текстовые сообщения подчинённых ───────────────────────────────────────────

async def handle_sub_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db_is_subordinate(uid): return

    state = dialog_state.get(uid)
    if not state:
        await update.message.reply_text("Нажми кнопку проекта, чтобы обновить статус.")
        return

    step = state.get("step")

    if step == "ask_text":
        state["status_text"] = update.message.text
        state["step"] = "ask_date"
        dialog_state[uid] = state
        await update.message.reply_text("Укажи следующую контрольную дату в формате ДД-ММ-ГГ:")

    elif step == "ask_date":
        try:
            new_date = parse_date(update.message.text)
        except ValueError:
            await update.message.reply_text("Неверный формат. Введи дату как ДД-ММ-ГГ (например 25-07-26):")
            return
        if new_date < date.today():
            await update.message.reply_text("Нельзя назначить контроль на прошедшую дату. Введи другую дату:")
            return
        await save_and_notify(ctx.bot, uid, state, state.get("status_text", ""), state["status_type"], new_date.isoformat())
        await update.message.reply_text(
            f"Статус сохранён! Следующий контроль: *{fmt_date(new_date.isoformat())}*",
            parse_mode="Markdown"
        )


# ── Общий обработчик текста ───────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_manager(uid):
        await handle_manager_message(update, ctx)
    elif db_is_subordinate(uid):
        await handle_sub_message(update, ctx)


# ── Планировщик ───────────────────────────────────────────────────────────────

async def morning_reminder(bot):
    subs = db_list_subordinates()
    if not subs: return
    sent = 0
    for sub_id, name, username in subs:
        due = db_projects_due_today_for_sub(sub_id)
        if due:
            btns = [
                [InlineKeyboardButton(pname, callback_data=f"update:{pid}:{pname}")]
                for pid, pname, _ in due
            ]
            await bot.send_message(
                sub_id,
                f"🔔 Сегодня контрольная дата по {len(due)} проект(ам)! Обнови статус:",
                reply_markup=InlineKeyboardMarkup(btns)
            )
            for pid, *_ in due:
                db_add_pending(sub_id, pid)
            sent += 1
    if sent:
        due_all = db_projects_due_today()
        names = "\n".join(f"• {n}" for _, n, _ in due_all)
        await bot.send_message(
            MANAGER_ID,
            f"📅 *Сегодня контрольная дата:*\n{names}\n\nЗапрос отправлен подчинённым.",
            parse_mode="Markdown"
        )

async def retry_reminder(bot):
    """Каждые 5 минут проверяем неотвеченные напоминания старше 30 минут."""
    overdue = db_get_overdue_pending(minutes=30)
    if not overdue: return

    # Группируем по подчинённому
    by_sub = {}
    for rem_id, sub_tg_id, project_id in overdue:
        by_sub.setdefault(sub_tg_id, []).append((rem_id, project_id))

    for sub_tg_id, items in by_sub.items():
        # Собираем проекты для повторного напоминания
        proj_ids = [pid for _, pid in items]
        with sqlite3.connect(DB_PATH) as con:
            placeholders = ",".join("?" * len(proj_ids))
            projects = con.execute(
                f"SELECT id, name, deadline FROM projects WHERE id IN ({placeholders})",
                proj_ids
            ).fetchall()

        if projects:
            btns = [
                [InlineKeyboardButton(f"{name}  |  {fmt_date(dl)}", callback_data=f"update:{pid}:{name}")]
                for pid, name, dl in projects
            ]
            await bot.send_message(
                sub_tg_id,
                "⚠️ Напоминание: ты ещё не обновил статус по проектам. Пожалуйста, сделай это сейчас:",
                reply_markup=InlineKeyboardMarkup(btns)
            )

        for rem_id, _ in items:
            db_mark_retry_sent(rem_id)


# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("subordinates", cmd_subordinates))
    app.add_handler(CommandHandler("remove_sub",   cmd_remove_sub))
    app.add_handler(CommandHandler("add",          cmd_add))
    app.add_handler(CommandHandler("remove",       cmd_remove))
    app.add_handler(CommandHandler("list",         cmd_list))
    app.add_handler(CommandHandler("report",       cmd_report))
    app.add_handler(CommandHandler("history",      cmd_history))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(morning_reminder, "cron", hour=REMINDER_HOUR, minute=REMINDER_MIN, args=[app.bot])
    scheduler.add_job(retry_reminder, "interval", minutes=5, args=[app.bot])
    scheduler.start()

    log.info("Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
