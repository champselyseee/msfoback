import asyncio
import os
import json
import logging
import time
import random
from datetime import datetime
from urllib.parse import urljoin
from typing import List, Dict, Optional, Set
from difflib import SequenceMatcher
from collections import defaultdict

from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters
)
from aiohttp import web

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))
COMPANIES_FILE = "russian_stocks_ids.json"
BASE_URL = "https://msfo.valiullin.uk"
CHECK_INTERVAL = 3600

# ==================== ГЛОБАЛЬНЫЕ ДАННЫЕ ====================
COMPANIES = []
BY_TICKER = {}
BY_NAME = {}

known_reports = defaultdict(set)
subscriptions = defaultdict(set)
parse_cache = {}
search_results = {}

# ==================== ЗАГРУЗКА КОМПАНИЙ ====================
def load_companies():
    global COMPANIES, BY_TICKER, BY_NAME
    
    try:
        with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
            companies = json.load(f)
        
        BY_TICKER = {}
        BY_NAME = {}
        COMPANIES = []
        
        for c in companies:
            if not isinstance(c, dict) or 'id' not in c:
                continue
            
            c['ticker'] = c.get('ticker', '').upper().strip()
            c['name'] = c.get('name', '').strip()
            
            if not c['name']:
                continue
            
            COMPANIES.append(c)
            if c['ticker']:
                BY_TICKER[c['ticker']] = c
            BY_NAME[c['name'].lower()] = c
        
        logger.info(f"Загружено компаний: {len(COMPANIES)}")
        return True
    except Exception as e:
        logger.error(f"Ошибка загрузки компаний: {e}")
        return False


def search_companies(query):
    if not query:
        return []
    
    query_upper = query.strip().upper()
    query_lower = query.strip().lower()
    results = []
    seen = set()
    
    if query_upper in BY_TICKER:
        c = BY_TICKER[query_upper]
        results.append(c)
        seen.add(c['id'])
    
    for ticker, c in BY_TICKER.items():
        if c['id'] not in seen and query_upper in ticker:
            results.append(c)
            seen.add(c['id'])
    
    for name, c in BY_NAME.items():
        if c['id'] not in seen and query_lower in name:
            results.append(c)
            seen.add(c['id'])
    
    results.sort(key=lambda c: SequenceMatcher(None, query_lower, c['name'].lower()).ratio(), reverse=True)
    return results[:20]


def get_company(company_id):
    for c in COMPANIES:
        if c['id'] == company_id:
            return c
    return None


def format_company(c):
    name = c['name']
    ticker = c['ticker']
    return f"<b>{name}</b> ({ticker})" if ticker else f"<b>{name}</b>"


# ==================== ПАРСЕР (ИСПРАВЛЕННЫЙ) ====================
async def parse_reports(company_id):
    url = f"https://www.e-disclosure.ru/portal/files.aspx?id={company_id}&type=3"
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu'
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow"
        )
        page = await context.new_page()
        
        try:
            logger.info(f"Загружаем компанию {company_id}...")
            
            # Пробуем загрузить с разными стратегиями
            for attempt in range(3):
                try:
                    if attempt == 0:
                        # Первая попытка - обычная загрузка
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    elif attempt == 1:
                        # Вторая попытка - ждем загрузки сети
                        await page.goto(url, wait_until="networkidle", timeout=60000)
                    else:
                        # Третья попытка - просто ждем
                        await page.goto(url, wait_until="load", timeout=60000)
                    
                    # Ждем подольше
                    await asyncio.sleep(10)
                    
                    # Проверяем, что страница загрузилась
                    title = await page.title()
                    logger.info(f"Заголовок страницы: {title}")
                    
                    # Пробуем найти таблицу разными селекторами
                    selectors = [
                        "table tbody tr",
                        "table tr",
                        ".table tbody tr",
                        "#ctl00_ContentPlaceHolder1_gvData tr",
                        "table[id*='gv'] tr",
                        "table[class*='table'] tr"
                    ]
                    
                    table_found = False
                    for selector in selectors:
                        try:
                            await page.wait_for_selector(selector, timeout=10000)
                            logger.info(f"Таблица найдена по селектору: {selector}")
                            table_found = True
                            break
                        except Exception:
                            continue
                    
                    if table_found:
                        break
                    else:
                        # Делаем скриншот для отладки
                        await page.screenshot(path=f"/tmp/debug_{company_id}.png")
                        logger.warning(f"Таблица не найдена, попытка {attempt + 1}")
                        
                except Exception as e:
                    logger.warning(f"Попытка {attempt + 1}: {e}")
                    if attempt == 2:
                        await page.screenshot(path=f"/tmp/error_{company_id}.png")
                        raise
                    await asyncio.sleep(5)
            
            # Ищем строки таблицы
            rows = []
            for selector in selectors:
                try:
                    rows = await page.locator(selector).all()
                    if rows:
                        break
                except Exception:
                    continue
            
            if not rows:
                logger.warning(f"Строки таблицы не найдены для компании {company_id}")
                # Пробуем получить весь HTML для анализа
                html = await page.content()
                logger.debug(f"HTML страницы (первые 1000 символов): {html[:1000]}")
                return []
            
            logger.info(f"Найдено строк: {len(rows)}")
            reports = []
            
            for row in rows:
                try:
                    cells = await row.locator("td").all()
                    if len(cells) < 6:
                        continue
                    
                    doc_type = (await cells[1].inner_text()).strip()
                    if "бухгалтер" not in doc_type.lower():
                        continue
                    
                    link = cells[5].locator("a")
                    if await link.count() == 0:
                        # Может быть в другой ячейке
                        for i in range(len(cells)):
                            link = cells[i].locator("a")
                            if await link.count() > 0:
                                break
                        else:
                            continue
                    
                    href = await link.first.get_attribute("href")
                    if not href:
                        continue
                    
                    # Пробуем разные способы получить ID файла
                    file_id = None
                    if "Fileid=" in href:
                        file_id = href.split("Fileid=")[1].split("&")[0]
                    elif "fileid=" in href.lower():
                        file_id = href.lower().split("fileid=")[1].split("&")[0]
                    elif "/file/" in href:
                        file_id = href.split("/file/")[1].split("/")[0]
                    
                    if not file_id:
                        continue
                    
                    reports.append({
                        "period": (await cells[2].inner_text()).strip(),
                        "doc_type": doc_type,
                        "publish": (await cells[4].inner_text()).strip(),
                        "file_id": file_id
                    })
                    
                except Exception as e:
                    logger.debug(f"Ошибка парсинга строки: {e}")
                    continue
            
            logger.info(f"Компания {company_id}: найдено {len(reports)} отчетов")
            return reports
            
        except Exception as e:
            logger.error(f"Ошибка парсинга компании {company_id}: {e}")
            try:
                await page.screenshot(path=f"/tmp/final_error_{company_id}.png")
            except Exception:
                pass
            return []
        finally:
            await context.close()
            await browser.close()


async def parse_reports_cached(company_id):
    now = time.time()
    if company_id in parse_cache:
        cached_time, data = parse_cache[company_id]
        if now - cached_time < 300:
            return data
    
    data = await parse_reports(company_id)
    if data:  # Кэшируем только если есть данные
        parse_cache[company_id] = (now, data)
    return data


# ==================== ПРОВЕРКА И УВЕДОМЛЕНИЯ ====================
async def check_and_notify(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Фоновая проверка...")
    
    company_ids = set()
    for comps in subscriptions.values():
        company_ids.update(comps)
    
    if not company_ids:
        return
    
    for company_id in company_ids:
        try:
            company = get_company(company_id)
            if not company:
                continue
            
            reports = await parse_reports(company_id)
            if not reports:
                continue
            
            known = known_reports.get(company_id, set())
            new_reports = [r for r in reports if r['file_id'] not in known]
            
            for r in reports:
                known_reports[company_id].add(r['file_id'])
            
            if new_reports:
                msg = f"🔥 <b>Новые отчёты {format_company(company)}</b> ({len(new_reports)}):\n\n"
                for i, r in enumerate(new_reports[:10], 1):
                    msg += f"{i}. <b>{r['period']}</b>\n   📄 {r['doc_type']}\n   📅 {r['publish']}\n\n"
                
                keyboard = []
                for r in new_reports[:5]:
                    keyboard.append([InlineKeyboardButton(
                        f"Открыть: {r['period'][:30]}",
                        callback_data=f"open_{r['file_id']}"
                    )])
                
                subscribers = [cid for cid, comps in subscriptions.items() if company_id in comps]
                for chat_id in subscribers:
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg[:4000],
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                        )
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        logger.error(f"Ошибка отправки {chat_id}: {e}")
                        if "blocked" in str(e).lower():
                            subscriptions[chat_id].discard(company_id)
                
                logger.info(f"Уведомлений: {len(subscribers)}")
            
            await asyncio.sleep(random.uniform(5, 8))
            
        except Exception as e:
            logger.error(f"Ошибка проверки {company_id}: {e}")
    
    parse_cache.clear()


# ==================== КОМАНДЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Парсер бухгалтерских отчётов</b>\n\n"
        "/check Газпром — посмотреть отчёты\n"
        "/subscribe Газпром — подписаться\n"
        "/unsubscribe Газпром — отписаться\n"
        "/subscriptions — мои подписки\n"
        "/help — помощь",
        parse_mode="HTML"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 <b>Помощь</b>\n\n"
        "/check КОМПАНИЯ — отчёты\n"
        "/subscribe КОМПАНИЯ — подписка\n"
        "/unsubscribe КОМПАНИЯ — отписка\n"
        "/subscriptions — список подписок\n\n"
        "Поиск по тикеру: GAZP, SBER\n"
        "Поиск по названию: Газпром, Сбербанк\n"
        "Частичный поиск: газ, сбер",
        parse_mode="HTML"
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Укажите компанию: /check Газпром")
        return
    
    query = " ".join(context.args)
    results = search_companies(query)
    
    if not results:
        await update.message.reply_text(f"❌ Компания '{query}' не найдена")
        return
    
    if len(results) == 1:
        await show_reports(update, results[0])
    else:
        search_results[str(update.effective_chat.id)] = {
            "companies": results,
            "timestamp": time.time()
        }
        
        msg = f"🔍 <b>Найдено несколько компаний:</b>\n\n"
        for i, c in enumerate(results, 1):
            msg += f"{i}. {format_company(c)}\n"
        msg += "\nВведите номер или уточните запрос"
        
        await update.message.reply_text(msg, parse_mode="HTML")


async def show_reports(update: Update, company):
    chat_id = str(update.effective_chat.id)
    msg = await update.message.reply_text(f"🔍 Проверяю {format_company(company)}...", parse_mode="HTML")
    
    try:
        reports = await parse_reports_cached(company['id'])
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка загрузки: {str(e)[:100]}", parse_mode="HTML")
        return
    
    if not reports:
        await msg.edit_text(f"📊 {format_company(company)}\n❌ Отчёты не найдены или сайт недоступен", parse_mode="HTML")
        return
    
    known = known_reports.get(company['id'], set())
    new = [r for r in reports if r['file_id'] not in known]
    
    for r in reports:
        known_reports[company['id']].add(r['file_id'])
    
    text = f"📊 {format_company(company)}\n"
    if new:
        text += f"🔥 Новых: {len(new)}\n\n"
    else:
        text += "\n"
    
    text += f"📚 Всего отчётов: {len(reports)}\n\n"
    
    keyboard = []
    for i, r in enumerate(reports[:10], 1):
        star = "⭐ " if r['file_id'] not in known else ""
        text += f"{star}{i}. <b>{r['period']}</b>\n   📄 {r['doc_type']}\n   📅 {r['publish']}\n\n"
        keyboard.append([InlineKeyboardButton(
            f"{'🆕 ' if r['file_id'] not in known else ''}{r['period'][:30]}",
            callback_data=f"open_{r['file_id']}"
        )])
    
    await msg.delete()
    
    if len(text) > 4000:
        for part in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await update.message.reply_text(part, parse_mode="HTML")
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = update.message.text.strip()
    
    now = time.time()
    expired = [cid for cid, data in search_results.items() if now - data['timestamp'] > 300]
    for cid in expired:
        del search_results[cid]
    
    if text.lower() in ['отмена', 'cancel']:
        if chat_id in search_results:
            del search_results[chat_id]
            await update.message.reply_text("❌ Поиск отменен")
        return
    
    if chat_id in search_results:
        try:
            idx = int(text) - 1
            companies = search_results[chat_id]['companies']
            if 0 <= idx < len(companies):
                del search_results[chat_id]
                await show_reports(update, companies[idx])
                return
        except ValueError:
            del search_results[chat_id]


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("open_"):
        file_id = query.data[5:]
        await query.message.reply_text(
            f"🔗 <a href='{BASE_URL}/view/{file_id}'>Открыть отчёт</a>",
            parse_mode="HTML"
        )


async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    
    if not context.args:
        await update.message.reply_text("❌ Укажите компанию: /subscribe Газпром")
        return
    
    query = " ".join(context.args)
    results = search_companies(query)
    
    if not results:
        await update.message.reply_text(f"❌ Компания '{query}' не найдена")
        return
    
    if len(results) == 1:
        c = results[0]
        subscriptions[chat_id].add(c['id'])
        ticker = c['ticker']
        hint = f" /unsubscribe {ticker}" if ticker else ""
        await update.message.reply_text(
            f"✅ Подписка на {format_company(c)}{hint}",
            parse_mode="HTML"
        )
    else:
        msg = f"🔍 <b>Найдено несколько компаний:</b>\n\n"
        for i, c in enumerate(results, 1):
            subbed = "✅" if c['id'] in subscriptions[chat_id] else "➕"
            msg += f"{i}. {subbed} {format_company(c)}\n"
        msg += "\nУточните запрос"
        await update.message.reply_text(msg, parse_mode="HTML")


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    
    if not context.args:
        subs = subscriptions.get(chat_id, set())
        if not subs:
            await update.message.reply_text("❌ Нет подписок")
            return
        
        msg = "📬 <b>Ваши подписки:</b>\n\n"
        for cid in subs:
            c = get_company(cid)
            if c:
                msg += f"• {format_company(c)}\n"
                if c['ticker']:
                    msg += f"  /unsubscribe {c['ticker']}\n"
        await update.message.reply_text(msg, parse_mode="HTML")
        return
    
    query = " ".join(context.args)
    results = search_companies(query)
    
    if not results:
        await update.message.reply_text(f"❌ Компания '{query}' не найдена")
        return
    
    if len(results) == 1:
        c = results[0]
        subscriptions[chat_id].discard(c['id'])
        ticker = c['ticker']
        hint = f" /subscribe {ticker}" if ticker else ""
        await update.message.reply_text(
            f"❌ Отписка от {format_company(c)}{hint}",
            parse_mode="HTML"
        )
    else:
        subs = subscriptions.get(chat_id, set())
        subbed = [c for c in results if c['id'] in subs]
        
        if not subbed:
            await update.message.reply_text(f"❌ Нет подписок по запросу '{query}'")
        elif len(subbed) == 1:
            c = subbed[0]
            subscriptions[chat_id].discard(c['id'])
            await update.message.reply_text(f"❌ Отписка от {format_company(c)}", parse_mode="HTML")
        else:
            msg = f"🔍 <b>Найдено несколько подписок:</b>\n\n"
            for i, c in enumerate(subbed, 1):
                msg += f"{i}. {format_company(c)}\n"
                if c['ticker']:
                    msg += f"   /unsubscribe {c['ticker']}\n"
            msg += "\nУточните запрос"
            await update.message.reply_text(msg, parse_mode="HTML")


async def subscriptions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = subscriptions.get(chat_id, set())
    
    if not subs:
        await update.message.reply_text("❌ Нет подписок\n/subscribe КОМПАНИЯ — подписаться")
        return
    
    msg = "📬 <b>Ваши подписки:</b>\n\n"
    for cid in subs:
        c = get_company(cid)
        if c:
            msg += f"• {format_company(c)}\n"
            if c['ticker']:
                msg += f"  /unsubscribe {c['ticker']}\n"
    
    msg += f"\nВсего: {len(subs)}"
    await update.message.reply_text(msg, parse_mode="HTML")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ Произошла ошибка")
        except Exception:
            pass


async def post_init(app: Application):
    app.job_queue.run_repeating(check_and_notify, interval=CHECK_INTERVAL, first=10)
    logger.info("Фоновая проверка запущена")


async def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан!")
        return
    
    logger.info("Загрузка компаний...")
    if not load_companies():
        logger.error("Не удалось загрузить компании")
        return
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("subscriptions", subscriptions_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    
    async def health(request):
        return web.Response(text="OK")
    
    web_app = web.Application()
    web_app.router.add_get("/", health)
    
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health-check на порту {PORT}")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("✅ Бот запущен!")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
