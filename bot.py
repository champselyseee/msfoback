import asyncio
import os
from datetime import datetime
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import sqlite3

# ==================== КОНФИГ ====================
url = "https://www.e-disclosure.ru/portal/files.aspx?id=38533&type=3"
BASE_URL = "https://msfo.valiullin.uk"
DB_FILE = "reports.db"
CHECK_INTERVAL = 3600  # 1 час

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            file_id TEXT PRIMARY KEY,
            report_num TEXT,
            doc_type TEXT,
            period TEXT,
            foundation_date TEXT,
            publish_date TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            subscribed_at TEXT
        )
    """)
    
    conn.commit()
    return conn


def get_known_ids(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT file_id FROM reports")
    return {row[0] for row in cursor.fetchall()}


def save_new_reports(conn, new_reports):
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for report in new_reports:
        cursor.execute("""
            INSERT OR IGNORE INTO reports 
            (file_id, report_num, doc_type, period, foundation_date, publish_date, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report['file_id'], report['num'], report['doc_type'],
            report['period'], report['foundation'], report['publish'],
            now, now
        ))
    
    conn.commit()


def update_last_seen(conn, file_ids):
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for file_id in file_ids:
        cursor.execute(
            "UPDATE reports SET last_seen = ? WHERE file_id = ?",
            (now, file_id)
        )
    
    conn.commit()


def get_subscribers(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM subscribers")
    return [row[0] for row in cursor.fetchall()]


def subscribe_user(conn, chat_id, username):
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT OR REPLACE INTO subscribers (chat_id, username, subscribed_at)
        VALUES (?, ?, ?)
    """, (str(chat_id), username or "unknown", now))
    conn.commit()


def unsubscribe_user(conn, chat_id):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscribers WHERE chat_id = ?", (str(chat_id),))
    conn.commit()


# ==================== ПАРСЕР ====================
async def parse_reports():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            # Три попытки загрузить страницу
            for attempt in range(3):
                try:
                    print(f"Попытка {attempt + 1} загрузить сайт...")
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(10000)  # ждём 10 секунд
                    
                    # Проверяем, есть ли таблица
                    await page.wait_for_selector("table tbody tr", timeout=30000)
                    print("Таблица найдена!")
                    break  # получилось — выходим из цикла попыток
                    
                except Exception as e:
                    print(f"Попытка {attempt + 1} не удалась: {e}")
                    if attempt == 2:  # последняя попытка
                        raise
                    await asyncio.sleep(5)  # ждём 5 сек перед следующей
            
            rows = await page.locator("table tbody tr").all()
            print(f"Найдено строк: {len(rows)}")
            reports = []
            
            for row in rows:
                try:
                    cells = await row.locator("td").all()
                    
                    if len(cells) < 6:
                        continue
                    
                    report_num = (await cells[0].inner_text()).strip()
                    doc_type = (await cells[1].inner_text()).strip()
                    period = (await cells[2].inner_text()).strip()
                    foundation_date = (await cells[3].inner_text()).strip()
                    publish_date = (await cells[4].inner_text()).strip()
                    
                    if "бухгалтер" not in doc_type.lower():
                        continue
                    
                    link = cells[5].locator("a")
                    if await link.count() == 0:
                        continue
                    
                    href = await link.first.get_attribute("href")
                    if not href:
                        continue
                    
                    file_url = urljoin(url, href)
                    if "Fileid=" not in file_url:
                        continue
                    
                    file_id = file_url.split("Fileid=")[1]
                    
                    reports.append({
                        "num": report_num,
                        "doc_type": doc_type,
                        "period": period,
                        "foundation": foundation_date,
                        "publish": publish_date,
                        "file_id": file_id
                    })
                    
                except Exception as e:
                    print(f"Ошибка парсинга строки: {e}")
            
            print(f"Найдено бухгалтерских отчётов: {len(reports)}")
            return reports
            
        finally:
            await browser.close()


# ==================== УВЕДОМЛЕНИЯ ПОДПИСЧИКАМ ====================
async def check_and_notify(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая проверка: парсит и шлёт ТОЛЬКО новые отчёты подписчикам"""
    conn = init_db()
    
    try:
        print(f"[{datetime.now()}] Проверяю отчёты...")
        reports = await parse_reports()
        
        if not reports:
            print("Отчёты не найдены")
            return
        
        known_ids = get_known_ids(conn)
        new_reports = [r for r in reports if r['file_id'] not in known_ids]
        
        all_file_ids = {r['file_id'] for r in reports}
        update_last_seen(conn, all_file_ids)
        
        if new_reports:
            save_new_reports(conn, new_reports)
            
            message = f"🔥 <b>Новые отчёты ({len(new_reports)}):</b>\n\n"
            
            for i, report in enumerate(new_reports, start=1):
                message += (
                    f"{i}. <b>{report['period']}</b>\n"
                    f"   📄 {report['doc_type']}\n"
                    f"   📅 {report['publish']}\n\n"
                )
            
            keyboard = []
            for i, report in enumerate(new_reports[:10], start=1):
                keyboard.append([
                    InlineKeyboardButton(
                        f"Открыть: {report['period']}",
                        callback_data=f"open_{report['file_id']}"
                    )
                ])
            
            subscribers = get_subscribers(conn)
            sent_count = 0
            
            for chat_id in subscribers:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                    )
                    sent_count += 1
                except Exception as e:
                    print(f"Не удалось отправить {chat_id}: {e}")
                    if "blocked" in str(e).lower() or "deactivated" in str(e).lower():
                        unsubscribe_user(conn, chat_id)
            
            print(f"Уведомлений отправлено: {sent_count}/{len(subscribers)}")
        else:
            print("Новых отчётов нет")
            
    except Exception as e:
        print(f"Ошибка при проверке: {e}")
    finally:
        conn.close()


# ==================== КОМАНДЫ БОТА ====================
async def start_command(update, context):
    await update.message.reply_text(
        "👋 Привет! Я парсер бухгалтерских отчётов.\n\n"
        "/check — посмотреть что есть сейчас\n"
        "/subscribe — подписаться на уведомления\n"
        "/unsubscribe — отписаться"
    )


async def check_command(update, context):
    """Показывает все отчёты с сайта"""
    msg = await update.message.reply_text("🔍 Проверяю сайт...")
    
    try:
        reports = await parse_reports()
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка при загрузке: {str(e)[:100]}")
        return
    
    if not reports:
        await msg.edit_text("❌ Не удалось загрузить отчёты. Возможно сайт недоступен.")
        return
    
    conn = init_db()
    try:
        known_ids = get_known_ids(conn)
        new_reports = [r for r in reports if r['file_id'] not in known_ids]
        
        all_file_ids = {r['file_id'] for r in reports}
        update_last_seen(conn, all_file_ids)
        
        if new_reports:
            save_new_reports(conn, new_reports)
        
        # Показываем все отчёты, новые помечаем звёздочкой
        if new_reports:
            message = f"🔥 Новых: {len(new_reports)}\n\n"
        else:
            message = ""
        
        message += f"📚 <b>Всего отчётов: {len(reports)}</b>\n\n"
        
        keyboard = []
        for i, report in enumerate(reports, start=1):
            is_new = "⭐ " if report['file_id'] not in known_ids else ""
            message += (
                f"{is_new}{i}. <b>{report['period']}</b>\n"
                f"   📄 {report['doc_type']}\n"
                f"   📅 {report['publish']}\n\n"
            )
            
            if i <= 10:  # максимум 10 кнопок
                keyboard.append([
                    InlineKeyboardButton(
                        f"{'🆕 ' if report['file_id'] not in known_ids else ''}{report['period']}",
                        callback_data=f"open_{report['file_id']}"
                    )
                ])
        
        await msg.delete()
        await update.message.reply_text(
            message,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
        )
        
    finally:
        conn.close()


async def button_callback(update, context):
    """Обработчик кнопок"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("open_"):
        file_id = query.data.replace("open_", "")
        link = f"{BASE_URL}/view/{file_id}"
        await query.message.reply_text(
            f"🔗 <a href='{link}'>Открыть отчёт</a>",
            parse_mode="HTML"
        )


async def subscribe_command(update, context):
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name
    
    conn = init_db()
    try:
        subscribe_user(conn, chat_id, username)
        await update.message.reply_text("✅ Подписан! Новые отчёты будут приходить автоматически.\n/unsubscribe — отписаться")
    finally:
        conn.close()


async def unsubscribe_command(update, context):
    chat_id = update.effective_chat.id
    
    conn = init_db()
    try:
        unsubscribe_user(conn, chat_id)
        await update.message.reply_text("❌ Отписан.\n/subscribe — подписаться снова")
    finally:
        conn.close()


# ==================== ЗАПУСК ====================
async def post_init(application: Application):
    application.job_queue.run_repeating(
        check_and_notify,
        interval=CHECK_INTERVAL,
        first=10
    )
    print("Фоновая проверка запущена")


async def main():
    if not TOKEN:
        print("ОШИБКА: BOT_TOKEN не задан!")
        return
    
    init_db()
    
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    from aiohttp import web
    
    async def health_check(request):
        return web.Response(text="OK")
    
    app = web.Application()
    app.router.add_get("/", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health-check на порту {PORT}")
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    print("Бот запущен!")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
