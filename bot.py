import os
import json
import logging
from datetime import datetime, timedelta, time as dt_time

import gspread
import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config from environment variables ─────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID      = int(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY        = os.environ["OPENAI_API_KEY"]
GOOGLE_SHEET_ID       = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_REFRESH_TOKEN  = os.environ["GOOGLE_REFRESH_TOKEN"]
WEEKLY_REPORT_DAY     = int(os.environ.get("WEEKLY_REPORT_DAY", "0"))   # 0=Mon … 6=Sun
WEEKLY_REPORT_HOUR    = int(os.environ.get("WEEKLY_REPORT_HOUR", "9"))  # hour in TIMEZONE
TIMEZONE              = os.environ.get("TIMEZONE", "UTC")


# ── Google Sheets helpers ──────────────────────────────────────────────────────
def get_sheet():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    creds.refresh(Request())  # exchange refresh token for a fresh access token

    gc = gspread.authorize(creds)
    worksheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

    # Create header row if sheet is empty
    if not worksheet.get_all_values():
        worksheet.append_row(["Date", "Description", "Amount"])

    return worksheet


def add_expense_to_sheet(date: str, description: str, amount: float):
    sheet = get_sheet()
    sheet.append_row([date, description, round(amount, 2)])


def get_expenses_for_range(start_date, end_date):
    """Return rows whose Date falls in [start_date, end_date] (datetime.date objects)."""
    sheet = get_sheet()
    rows = sheet.get_all_records()
    result = []
    for row in rows:
        raw = str(row.get("Date", "")).strip()
        if not raw:
            continue
        try:
            row_date = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if start_date <= row_date <= end_date:
            result.append(row)
    return result


# ── OpenAI helpers ─────────────────────────────────────────────────────────────
openai_client = OpenAI(api_key=OPENAI_API_KEY)


def parse_expense(user_text: str) -> dict | None:
    """Ask GPT to extract {amount, description, date} from free-form text."""
    today     = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    prompt = f"""You are a JSON extractor for an expense-tracking bot.
Today is {today}. Yesterday was {yesterday}.

Extract the expense from the user's message and return ONLY a valid JSON object
with these fields:
  amount      – a positive number (no currency symbols)
  description – a short label for the expense (1–4 words)
  date        – the date in YYYY-MM-DD format

If you cannot find an amount, return exactly: {{"amount": null}}

Examples:
  "coffee 3.5"            → {{"amount": 3.5,  "description": "coffee",    "date": "{today}"}}
  "spent 20 on lunch"    → {{"amount": 20.0, "description": "lunch",     "date": "{today}"}}
  "taxi yesterday 18"    → {{"amount": 18.0, "description": "taxi",      "date": "{yesterday}"}}
  "groceries $45.50"     → {{"amount": 45.5, "description": "groceries", "date": "{today}"}}

User message: "{user_text}"

JSON:"""

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
        timeout=20,
    )
    raw = resp.choices[0].message.content.strip()

    try:
        data = json.loads(raw)
        if data.get("amount") and float(data["amount"]) > 0:
            return {
                "amount":      float(data["amount"]),
                "description": str(data.get("description", "expense")).strip(),
                "date":        str(data.get("date", datetime.now().strftime("%Y-%m-%d"))),
            }
    except Exception as e:
        logger.warning(f"parse_expense JSON error: {e} | raw: {raw}")

    return None


def generate_advice(expenses: list, total: float) -> str:
    """Ask GPT to generate personalised spending advice based on last week's data."""
    lines = "\n".join(
        f"  {e['Date']} – {e['Description']} (${float(e['Amount']):.2f})"
        for e in expenses
    )
    prompt = f"""You are a friendly personal finance coach.

Here are my expenses from last week:
{lines}
Total: ${total:.2f}

Give me exactly 3 short, specific, friendly tips to help me spend less next week,
based on these actual purchases. Be conversational and encouraging — not preachy.
Each tip should be 1–2 sentences. No bullet points or numbers, just paragraphs."""

    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
        timeout=30,
    )
    return resp.choices[0].message.content.strip()


# ── Bot command handlers ───────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hey! I'm your personal expense bot.*\n\n"
        "Just tell me what you spent in plain English:\n"
        "• `coffee 3.5`\n"
        "• `spent 45 on groceries`\n"
        "• `lunch $12.50`\n"
        "• `taxi yesterday 18`\n\n"
        "I'll log everything to your Google Sheet 📊\n"
        "Every Monday morning you'll get a weekly summary + spending tips!\n\n"
        "You can also type /report anytime to get last week's summary.",
        parse_mode="Markdown",
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual trigger: /report"""
    await _send_weekly_report(context.bot)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only respond to the authorised user
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Sorry, I only work for my owner! 🔒")
        return

    text = update.message.text
    thinking = await update.message.reply_text("⏳ Logging…")

    try:
        logger.info(f"Parsing message: {text}")
        expense = parse_expense(text)
        logger.info(f"Parsed expense: {expense}")
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        await thinking.edit_text(f"❌ AI error: {e}")
        return

    if not expense:
        await thinking.edit_text(
            "❓ I couldn't find an amount in that message.\n\n"
            "Try something like:\n"
            "• `coffee 3.5`\n"
            "• `spent 20 on lunch`\n"
            "• `groceries $45`",
            parse_mode="Markdown",
        )
        return

    try:
        logger.info(f"Writing to Google Sheets...")
        add_expense_to_sheet(expense["date"], expense["description"], expense["amount"])
        logger.info(f"Successfully logged: {expense}")
        await thinking.edit_text(
            f"✅ *Logged!*\n"
            f"📝 {expense['description'].capitalize()}\n"
            f"💵 ${expense['amount']:.2f}\n"
            f"📅 {expense['date']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
        await thinking.edit_text(
            f"❌ Google Sheets error: {e}"
        )


# ── Weekly report ──────────────────────────────────────────────────────────────
async def _send_weekly_report(bot):
    today      = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())  # this Monday
    week_end   = today                                     # today

    expenses = get_expenses_for_range(week_start, week_end)

    if not expenses:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                f"📊 *Weekly Report* ({week_start} → {week_end})\n\n"
                "No expenses logged last week — either you spent nothing 🎉 "
                "or forgot to log! Try to be consistent this week."
            ),
            parse_mode="Markdown",
        )
        return

    total = sum(float(e["Amount"]) for e in expenses)
    lines = "\n".join(
        f"• {e['Date']} – {e['Description']} — *${float(e['Amount']):.2f}*"
        for e in expenses
    )

    advice = generate_advice(expenses, total)

    report = (
        f"📊 *Weekly Expense Report*\n"
        f"_{week_start} → {week_end}_\n\n"
        f"{lines}\n\n"
        f"💰 *Total: ${total:.2f}*\n\n"
        f"💡 *Tips for next week:*\n{advice}"
    )

    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=report,
        parse_mode="Markdown",
    )
    logger.info(f"Weekly report sent. Total: ${total:.2f}")


async def scheduled_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Called by the job queue every week."""
    await _send_weekly_report(context.bot)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Schedule the weekly report
    tz = pytz.timezone(TIMEZONE)
    report_time = dt_time(hour=WEEKLY_REPORT_HOUR, minute=0, tzinfo=tz)
    app.job_queue.run_daily(
        scheduled_weekly_report,
        time=report_time,
        days=(WEEKLY_REPORT_DAY,),
        name="weekly_report",
    )

    logger.info(
        f"Bot running. Weekly report: day={WEEKLY_REPORT_DAY}, hour={WEEKLY_REPORT_HOUR} {TIMEZONE}"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
