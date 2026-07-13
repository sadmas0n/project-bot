
"""
Project Status Tracking Bot
============================
Установка:  pip install python-telegram-bot apscheduler
Запуск:     python project_bot.py
"""
 
import sqlite3
import logging
from datetime import date, datetime
 
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
 
BOT_TOKEN      = "8549647812:AAGk5t7-nLe5bmzH3FQ0UDeji-fXNhDWqrA"
MANAGER_ID     = 126009180
SUBORDINATE_ID = 1041337530
REMINDER_HOUR  = 4   # 04:00 UTC = 09:00 Tashkent (UTC+5)
REMINDER_MIN   = 0
TIMEZONE       = "Asia/Tashkent"
DB_PATH        = "projects.db"
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
 
 
# ── Формат даты ───────────────────────────────────────────────────────────────
 
def parse_date(text: str) -> date:
    """Принимает DD-MM-YY или DD-MM-YYYY, возвращает date."""
    text = text.strip()
    for fmt in ("%d-%m-%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Неверный формат даты: {text}")
 
def fmt_date(iso: str) -> str:
    """Из YYYY-MM-DD в DD-MM-YY для отображения."""
    try:
        return date.fromisoformat(iso).strftime("%d-%m-%y")
    except Exception:
        return iso
 
 
# ── База данных ────────────────────────────────────────────────────────────────
 
def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                deadline    TEXT    NOT NULL,
                last_status TEXT,
                updated_at  TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                status     TEXT,
                new_date   TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        con.commit()
 
def db_add_project(name, deadline_iso):
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("INSERT INTO projects (name, deadline) VALUES (?, ?)", (name, deadline_iso))
            con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
 
def db_remove_project(name):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("DELETE FROM projects WHERE name = ?", (name,))
        con.commit()
        return cur.rowcount > 0
 
def db_list_projects():
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT id, name, deadline, last_status, updated_at FROM projects ORDER BY deadline"
        ).fetchall()
 
def db_get_project_by_name(name):
    with sqlite3.connect(DB_PATH) as con:
        return con.execute("SELECT id, name, deadline FROM projects WHERE name = ?", (name,)).fetchone()
 
def db_update_status(project_id, status, new_deadline_iso):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE projects SET last_status=?, deadline=?, updated_at=datetime('now') WHERE id=?",
            (status, new_deadline_iso, project_id)
        )
        con.execute(
            "INSERT INTO history (project_id, status, new_date) VALUES (?,?,?)",
            (project_id, status, new_deadline_iso)
        )
        con.commit()
 
def db_projects_due_today():
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as con:
        return con.execute(
            "SELECT id, name, deadline FROM projects WHERE deadline = ?", (today,)
        ).fetchall()
 
def is_manager(uid): return uid == MANAGER_ID
def is_subordinate(uid): return uid == SUBORDINATE_ID
 
def format_project_list():
    rows = db_list_projects()
    if not rows:
        return "Проектов пока нет."
    today = date.today()
    lines = ["📋 *Список проектов:*\n"]
    for pid, name, deadline, last_status, updated_at in rows:
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
        st = f"\n    └ {last_status}" if last_status else ""
        upd = f" _(обновлено {fmt_date(updated_at[:10])})_" if updated_at else ""
        lines.append(f"{flag} *{name}*\n    Контроль: `{fmt_date(deadline)}` ({days_str}){upd}{st}")
    return "\n\n".join(lines)
 
 
# ── Состояние диалога ─────────────────────────────────────────────────────────
 
dialog_state = {}
 
 
# ── Команды менеджера ──────────────────────────────────────────────────────────
 
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_manager(uid):
        await update.message.reply_text(
            "👋 Привет! Ты подключён как *менеджер*.\n\n"
            "Команды:\n"
            "/add Название | ДД-ММ-ГГ — добавить проект\n"
            "/remove Название — удалить проект\n"
            "/list — все проекты со статусами\n"
            "/report — запросить обновление прямо сейчас\n"
            "/history Название — история по проекту",
            parse_mode="Markdown"
        )
    elif is_subordinate(uid):
        await update.message.reply_text(
            "👋 Привет! Я буду напоминать тебе об обновлении статусов в день контрольной даты."
        )
    else:
        await update.message.reply_text("⛔ У тебя нет доступа к этому боту.")
 
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    try:
        text = " ".join(ctx.args)
        name, date_str = [x.strip() for x in text.split("|", 1)]
        dl = parse_date(date_str)
    except Exception:
        await update.message.reply_text("⚠️ Формат: /add Название проекта | ДД-ММ-ГГ")
        return
    if db_add_project(name, dl.isoformat()):
        await update.message.reply_text(
            f"✅ Проект *{name}* добавлен. Контроль: `{fmt_date(dl.isoformat())}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"⚠️ Проект *{name}* уже существует.", parse_mode="Markdown")
 
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
    all_proj = db_list_projects()
    if not all_proj:
        await update.message.reply_text("📋 Нет активных проектов.")
        return
    await _send_update_request(ctx.bot, all_proj)
    await update.message.reply_text("📨 Запрос на обновление отправлен подчинённому.")
 
async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update.effective_user.id): return
    name = " ".join(ctx.args).strip()
    proj = db_get_project_by_name(name)
    if not proj:
        await update.message.reply_text(f"⚠️ Проект *{name}* не найден.", parse_mode="Markdown")
        return
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT status, new_date, created_at FROM history WHERE project_id=? ORDER BY created_at DESC LIMIT 10",
            (proj[0],)
        ).fetchall()
    if not rows:
        await update.message.reply_text(f"История по *{name}* пуста.", parse_mode="Markdown")
        return
    lines = [f"📜 *История: {name}*\n"]
    for status, new_date, created_at in rows:
        lines.append(f"`{fmt_date(created_at[:10])}` → контроль `{fmt_date(new_date)}`\n_{status}_")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
 
 
# ── Взаимодействие с подчинённым ──────────────────────────────────────────────
 
async def _send_update_request(bot, projects):
    btns = [
        [InlineKeyboardButton(
            f"📝 {name}  |  {fmt_date(deadline)}",
            callback_data=f"update:{pid}:{name}"
        )]
        for pid, name, deadline, *_ in projects
    ]
    await bot.send_message(
        SUBORDINATE_ID,
        "📋 *Обнови статус по проектам.* Выбери проект:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown"
    )
 
async def callback_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != SUBORDINATE_ID:
        await query.answer("⛔ Нет доступа.")
        return
    await query.answer()
    parts = query.data.split(":", 2)
    proj_id, proj_name = int(parts[1]), parts[2]
    dialog_state[SUBORDINATE_ID] = {"project_id": proj_id, "project_name": proj_name, "step": "status"}
    await query.message.reply_text(
        f"✏️ *{proj_name}*\n\nНапиши статус (что сделано, что осталось, риски):",
        parse_mode="Markdown"
    )
 
async def handle_sub_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != SUBORDINATE_ID: return
    state = dialog_state.get(uid)
    if not state:
        await update.message.reply_text("Нажми кнопку проекта, чтобы обновить статус.")
        return
    if state["step"] == "status":
        state["status_text"] = update.message.text
        state["step"] = "date"
        await update.message.reply_text(
            "📅 Укажи следующую контрольную дату в формате *ДД-ММ-ГГ*:",
            parse_mode="Markdown"
        )
    elif state["step"] == "date":
        try:
            new_date = parse_date(update.message.text)
        except ValueError:
            await update.message.reply_text("⚠️ Неверный формат. Введи дату как ДД-ММ-ГГ (например 25-07-26):")
            return
        db_update_status(state["project_id"], state["status_text"], new_date.isoformat())
        dialog_state.pop(uid, None)
        await update.message.reply_text(
            f"✅ Статус сохранён! Следующий контроль: *{fmt_date(new_date.isoformat())}*",
            parse_mode="Markdown"
        )
        await ctx.bot.send_message(
            MANAGER_ID,
            f"📬 *Обновление статуса*\n\n"
            f"*Проект:* {state['project_name']}\n"
            f"*Статус:* {state['status_text']}\n"
            f"*Следующий контроль:* `{fmt_date(new_date.isoformat())}`",
            parse_mode="Markdown"
        )
 
 
# ── Планировщик ───────────────────────────────────────────────────────────────
 
async def morning_reminder(bot):
    due = db_projects_due_today()
    if not due: return
    log.info(f"Напоминание: {len(due)} проектов сегодня")
    btns = [
        [InlineKeyboardButton(f"📝 {name}", callback_data=f"update:{pid}:{name}")]
        for pid, name, _ in due
    ]
    await bot.send_message(
        SUBORDINATE_ID,
        f"🔔 *Сегодня контрольная дата по {len(due)} проект(ам)!*\nОбнови статус:",
        reply_markup=InlineKeyboardMarkup(btns),
        parse_mode="Markdown"
    )
    names = "\n".join(f"• {name}" for _, name, _ in due)
    await bot.send_message(
        MANAGER_ID,
        f"📅 *Сегодня контрольная дата:*\n{names}\n\nЗапрос на обновление отправлен.",
        parse_mode="Markdown"
    )
 
 
# ── Запуск ────────────────────────────────────────────────────────────────────
 
def main():
    db_init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("remove",  cmd_remove))
    app.add_handler(CommandHandler("list",    cmd_list))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CallbackQueryHandler(callback_update, pattern=r"^update:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sub_message))
 
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(morning_reminder, "cron", hour=REMINDER_HOUR, minute=REMINDER_MIN, args=[app.bot])
    scheduler.start()
 
    log.info("Бот запущен.")
    app.run_polling()
 
if __name__ == "__main__":
    main()
 
