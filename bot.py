import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
from supabase import create_client, Client
import pytz

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Config ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])  # Your Telegram chat ID — bot ignores everyone else
DAILY_BRIEFING_HOUR = int(os.environ.get("DAILY_BRIEFING_HOUR", "7"))  # 7am default
EST = pytz.timezone("America/New_York")

# --- Clients ---
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Conversation state for disambiguation ---
pending_disambiguation = {}  # chat_id -> {choices: [...], original_message: str, field: str}


# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are SalesFlow, a personal AI CRM assistant. You help manage accounts, contacts, opportunities, and interactions for an account manager.

You have access to a Supabase database with these tables:
- accounts: id, name, industry, website, size, notes, created_at, updated_at
- contacts: id, first_name, last_name, email, phone, title, account_id, linkedin, notes, created_at, updated_at
- opportunities: id, name, account_id, primary_contact_id, stage, value, currency, close_date, probability, notes, created_at, updated_at
  - stages: prospecting / qualified / proposal / negotiation / closed_won / closed_lost
- interactions: id, type, raw_text, summary, next_steps, account_id, opportunity_id, contact_ids[], created_at
  - types: call / email / meeting / note / voicenote
- tasks: id, title, due_at, account_id, opportunity_id, contact_id, completed, created_at

You will receive the user's message and relevant database context. Respond in one of these structured JSON formats:

## Intent: STORE
User is logging new information (call notes, meeting notes, new contact, deal update, etc.)
{
  "intent": "STORE",
  "entities": {
    "account_name": "string or null",
    "contact_first_name": "string or null",
    "contact_last_name": "string or null",
    "contact_title": "string or null",
    "opportunity_name": "string or null",
    "opportunity_stage": "string or null",
    "opportunity_value": number or null,
    "opportunity_close_date": "YYYY-MM-DD or null",
    "interaction_type": "call|email|meeting|note",
    "interaction_summary": "clean 1-2 sentence summary",
    "next_steps": "string or null",
    "task_title": "string or null",
    "task_due_at": "ISO8601 or null"
  },
  "confirmation": "Short friendly confirmation message to show user (1-2 lines, use ✓ emoji)",
  "needs_disambiguation": false,
  "disambiguation_field": null,
  "disambiguation_choices": []
}

## Intent: QUERY
User is asking a question about their data.
{
  "intent": "QUERY",
  "query_type": "account|contact|opportunity|pipeline|task|general",
  "search_terms": ["term1", "term2"],
  "response": "Your full answer based on the provided database context. Be conversational and helpful."
}

## Intent: TASK
User wants to set a reminder or follow-up.
{
  "intent": "TASK",
  "task_title": "string",
  "task_due_at": "ISO8601 datetime or null",
  "account_name": "string or null",
  "contact_name": "string or null",
  "confirmation": "✓ Reminder set: [task] — [date]"
}

## Intent: DRAFT
User wants you to write an email or message.
{
  "intent": "DRAFT",
  "draft_type": "email|message",
  "subject": "string or null",
  "body": "The full draft text",
  "context_used": "Brief note on what context you pulled from"
}

## Intent: DISAMBIGUATE
A name or entity is ambiguous and you need the user to clarify.
{
  "intent": "DISAMBIGUATE",
  "field": "contact|account|opportunity",
  "question": "Which [thing] do you mean?",
  "choices": ["Option 1 description", "Option 2 description", "New contact/account"]
}

## Intent: GENERAL
Any general question or conversation not related to CRM data.
{
  "intent": "GENERAL",
  "response": "Your response as a helpful AI assistant."
}

Important rules:
- Always respond with valid JSON only — no markdown, no explanation outside JSON
- For STORE intents, extract as much structured data as possible
- For QUERY intents, base your answer strictly on the provided database context
- Be concise and friendly in confirmations and responses
- Today's date is: """ + datetime.now(EST).strftime("%Y-%m-%d") + """
- When dates are relative ("Friday", "next week", "end of month"), resolve them to actual dates
"""


# ─────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────

def get_crm_context(search_terms: list[str] = None) -> str:
    """Pull relevant CRM data to give Claude context."""
    try:
        context_parts = []

        # Recent interactions (last 30)
        interactions = supabase.table("interactions").select(
            "*, accounts(name), opportunities(name, stage, value)"
        ).order("created_at", desc=True).limit(30).execute()

        if interactions.data:
            context_parts.append("## Recent Interactions")
            for i in interactions.data:
                acc = i.get("accounts", {}) or {}
                opp = i.get("opportunities", {}) or {}
                context_parts.append(
                    f"- [{i['created_at'][:10]}] {i.get('type','note').upper()} | "
                    f"Account: {acc.get('name','?')} | "
                    f"Opp: {opp.get('name','?')} | "
                    f"Summary: {i.get('summary', i.get('raw_text',''))[:120]} | "
                    f"Next: {i.get('next_steps','')}"
                )

        # All accounts
        accounts = supabase.table("accounts").select("id, name, industry, notes").execute()
        if accounts.data:
            context_parts.append("\n## Accounts")
            for a in accounts.data:
                context_parts.append(f"- {a['name']} (ID: {a['id']}) | {a.get('industry','')} | {a.get('notes','')[:80]}")

        # All contacts
        contacts = supabase.table("contacts").select(
            "id, first_name, last_name, title, email, account_id, accounts(name)"
        ).execute()
        if contacts.data:
            context_parts.append("\n## Contacts")
            for c in contacts.data:
                acc = c.get("accounts", {}) or {}
                name = f"{c.get('first_name','')} {c.get('last_name','')}".strip()
                context_parts.append(
                    f"- {name} (ID: {c['id']}) | {c.get('title','')} @ {acc.get('name','')} | {c.get('email','')}"
                )

        # Open opportunities
        opps = supabase.table("opportunities").select(
            "id, name, stage, value, close_date, probability, accounts(name), contacts(first_name, last_name)"
        ).neq("stage", "closed_won").neq("stage", "closed_lost").execute()
        if opps.data:
            context_parts.append("\n## Open Opportunities")
            for o in opps.data:
                acc = o.get("accounts", {}) or {}
                context_parts.append(
                    f"- {o.get('name','?')} (ID: {o['id']}) | {acc.get('name','?')} | "
                    f"Stage: {o.get('stage','?')} | Value: ${o.get('value',0):,.0f} | "
                    f"Close: {o.get('close_date','?')} | Prob: {o.get('probability','?')}%"
                )

        # Pending tasks
        tasks = supabase.table("tasks").select(
            "id, title, due_at, accounts(name), contacts(first_name, last_name)"
        ).eq("completed", False).order("due_at").limit(20).execute()
        if tasks.data:
            context_parts.append("\n## Pending Tasks")
            for t in tasks.data:
                acc = t.get("accounts", {}) or {}
                context_parts.append(
                    f"- {t['title']} | Due: {t.get('due_at','?')[:10] if t.get('due_at') else 'no date'} | "
                    f"Account: {acc.get('name','')}"
                )

        return "\n".join(context_parts) if context_parts else "No CRM data yet."

    except Exception as e:
        logger.error(f"Error fetching CRM context: {e}")
        return "Error fetching CRM data."


def find_or_create_account(name: str) -> str | None:
    """Find account by name (fuzzy) or create it. Returns account ID."""
    if not name:
        return None
    try:
        # Try exact match first
        result = supabase.table("accounts").select("id, name").ilike("name", name).execute()
        if result.data:
            return result.data[0]["id"]
        # Create new
        new = supabase.table("accounts").insert({"name": name}).execute()
        return new.data[0]["id"] if new.data else None
    except Exception as e:
        logger.error(f"Error finding/creating account: {e}")
        return None


def find_or_create_contact(first: str, last: str, account_id: str = None, title: str = None) -> str | None:
    """Find contact by name or create them. Returns contact ID."""
    if not first and not last:
        return None
    try:
        query = supabase.table("contacts").select("id")
        if first:
            query = query.ilike("first_name", first)
        if last:
            query = query.ilike("last_name", last)
        result = query.execute()
        if result.data:
            return result.data[0]["id"]
        # Create new
        payload = {
            "first_name": first or "",
            "last_name": last or "",
            "account_id": account_id,
            "title": title,
        }
        new = supabase.table("contacts").insert(payload).execute()
        return new.data[0]["id"] if new.data else None
    except Exception as e:
        logger.error(f"Error finding/creating contact: {e}")
        return None


def find_or_create_opportunity(name: str, account_id: str, contact_id: str, entities: dict) -> str | None:
    """Find open opportunity for account or create it. Returns opportunity ID."""
    if not account_id:
        return None
    try:
        # Find existing open opp for this account
        result = supabase.table("opportunities").select("id").eq(
            "account_id", account_id
        ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()

        if result.data:
            opp_id = result.data[0]["id"]
            # Update with new info
            update = {}
            if entities.get("opportunity_stage"):
                update["stage"] = entities["opportunity_stage"]
            if entities.get("opportunity_value"):
                update["value"] = entities["opportunity_value"]
            if entities.get("opportunity_close_date"):
                update["close_date"] = entities["opportunity_close_date"]
            if update:
                supabase.table("opportunities").update(update).eq("id", opp_id).execute()
            return opp_id

        # Create new
        payload = {
            "name": name or f"Opportunity",
            "account_id": account_id,
            "primary_contact_id": contact_id,
            "stage": entities.get("opportunity_stage", "prospecting"),
            "value": entities.get("opportunity_value"),
            "close_date": entities.get("opportunity_close_date"),
        }
        new = supabase.table("opportunities").insert(payload).execute()
        return new.data[0]["id"] if new.data else None
    except Exception as e:
        logger.error(f"Error finding/creating opportunity: {e}")
        return None


def store_interaction(entities: dict, raw_text: str) -> bool:
    """Save a new interaction and related entities to the database."""
    try:
        account_id = find_or_create_account(entities.get("account_name"))
        contact_id = find_or_create_contact(
            entities.get("contact_first_name"),
            entities.get("contact_last_name"),
            account_id,
            entities.get("contact_title"),
        )
        opportunity_id = None
        if account_id:
            opportunity_id = find_or_create_opportunity(
                entities.get("opportunity_name"),
                account_id,
                contact_id,
                entities,
            )

        # Save interaction
        interaction_payload = {
            "type": entities.get("interaction_type", "note"),
            "raw_text": raw_text,
            "summary": entities.get("interaction_summary", ""),
            "next_steps": entities.get("next_steps", ""),
            "account_id": account_id,
            "opportunity_id": opportunity_id,
            "contact_ids": [contact_id] if contact_id else [],
        }
        supabase.table("interactions").insert(interaction_payload).execute()

        # Save task if present
        if entities.get("task_title"):
            task_payload = {
                "title": entities["task_title"],
                "due_at": entities.get("task_due_at"),
                "account_id": account_id,
                "opportunity_id": opportunity_id,
                "contact_id": contact_id,
            }
            supabase.table("tasks").insert(task_payload).execute()

        return True
    except Exception as e:
        logger.error(f"Error storing interaction: {e}")
        return False


def store_task(task_title: str, task_due_at: str, account_name: str = None, contact_name: str = None) -> bool:
    """Save a standalone task/reminder."""
    try:
        account_id = find_or_create_account(account_name) if account_name else None
        payload = {
            "title": task_title,
            "due_at": task_due_at,
            "account_id": account_id,
        }
        supabase.table("tasks").insert(payload).execute()
        return True
    except Exception as e:
        logger.error(f"Error storing task: {e}")
        return False


# ─────────────────────────────────────────────
# CLAUDE INTEGRATION
# ─────────────────────────────────────────────

def ask_claude(user_message: str, db_context: str) -> dict:
    """Send message to Claude with CRM context. Returns parsed JSON response."""
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"## Database Context\n{db_context}\n\n## User Message\n{user_message}"
                }
            ]
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON: {e}")
        return {"intent": "GENERAL", "response": "Sorry, I had trouble processing that. Can you rephrase?"}
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return {"intent": "GENERAL", "response": "I'm having trouble connecting right now. Try again in a moment."}


# ─────────────────────────────────────────────
# DAILY BRIEFING
# ─────────────────────────────────────────────

async def send_daily_briefing(app: Application):
    """Generate and send the morning briefing."""
    try:
        db_context = get_crm_context()
        briefing_prompt = (
            "Generate a daily morning briefing. Include: "
            "1) Tasks due today or overdue, "
            "2) Deals with no activity in 14+ days (check interactions), "
            "3) Pipeline summary (total open deals + total value). "
            "Format it cleanly with emojis. Keep it under 20 lines."
        )
        result = ask_claude(briefing_prompt, db_context)
        text = result.get("response", "Good morning! No briefing data available.")

        await app.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"☀️ *Good morning! Here's your daily briefing*\n\n{text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Briefing error: {e}")


# ─────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text(
        "👋 *SalesFlow is live!*\n\n"
        "Just talk to me naturally:\n\n"
        "• *Log a call:* \"Just got off with Sarah at Acme, 80k budget, Q3 close\"\n"
        "• *Query:* \"What's the status of Acme?\"\n"
        "• *Remind:* \"Remind me to follow up with John on Friday\"\n"
        "• *Draft:* \"Draft a follow-up email to Sarah about the proposal\"\n"
        "• *Pipeline:* \"Show me my open deals\"\n\n"
        "I'll remember everything. 🧠",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Security: only respond to your chat ID
    if chat_id != ALLOWED_CHAT_ID:
        logger.warning(f"Rejected message from unauthorized chat_id: {chat_id}")
        return

    user_text = update.message.text.strip()

    # Handle disambiguation response (numbered choice)
    if chat_id in pending_disambiguation and user_text.isdigit():
        await handle_disambiguation_choice(update, context, int(user_text))
        return

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Get DB context and ask Claude
    db_context = get_crm_context()
    result = ask_claude(user_text, db_context)
    intent = result.get("intent", "GENERAL")

    if intent == "STORE":
        entities = result.get("entities", {})

        # Check if disambiguation needed
        if result.get("needs_disambiguation"):
            pending_disambiguation[chat_id] = {
                "choices": result.get("disambiguation_choices", []),
                "original_message": user_text,
                "entities": entities,
                "field": result.get("disambiguation_field")
            }
            choices_text = "\n".join(
                f"{i+1}. {c}" for i, c in enumerate(result["disambiguation_choices"])
            )
            await update.message.reply_text(
                f"{result.get('confirmation', 'Which one do you mean?')}\n\n{choices_text}"
            )
            return

        success = store_interaction(entities, user_text)
        msg = result.get("confirmation", "✓ Got it, saved.")
        if not success:
            msg = "⚠️ Had trouble saving that — please try again."
        await update.message.reply_text(msg)

    elif intent == "DISAMBIGUATE":
        pending_disambiguation[chat_id] = {
            "choices": result.get("choices", []),
            "original_message": user_text,
            "field": result.get("field")
        }
        choices_text = "\n".join(
            f"{i+1}. {c}" for i, c in enumerate(result.get("choices", []))
        )
        await update.message.reply_text(
            f"{result.get('question', 'Which one?')}\n\n{choices_text}"
        )

    elif intent == "QUERY":
        await update.message.reply_text(result.get("response", "No data found."))

    elif intent == "TASK":
        store_task(
            result.get("task_title", "Follow up"),
            result.get("task_due_at"),
            result.get("account_name"),
            result.get("contact_name"),
        )
        await update.message.reply_text(result.get("confirmation", "✓ Reminder saved."))

    elif intent == "DRAFT":
        subject = result.get("subject", "")
        body = result.get("body", "")
        header = f"*Subject: {subject}*\n\n" if subject else ""
        await update.message.reply_text(
            f"📧 *Draft*\n\n{header}{body}",
            parse_mode="Markdown"
        )

    else:  # GENERAL
        await update.message.reply_text(result.get("response", "I'm not sure how to help with that."))


async def handle_disambiguation_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: int):
    """User picked a numbered option from a disambiguation prompt."""
    chat_id = update.effective_chat.id
    state = pending_disambiguation.pop(chat_id, None)
    if not state:
        return

    choices = state.get("choices", [])
    if choice < 1 or choice > len(choices):
        await update.message.reply_text("Invalid choice. Please try again.")
        return

    selected = choices[choice - 1]
    await update.message.reply_text(f"Got it — using: {selected}\nRe-processing your note...")

    # Re-ask Claude with the disambiguation resolved
    enriched = f"{state['original_message']} [Clarification: using {selected}]"
    db_context = get_crm_context()
    result = ask_claude(enriched, db_context)

    if result.get("intent") == "STORE":
        store_interaction(result.get("entities", {}), enriched)
        await update.message.reply_text(result.get("confirmation", "✓ Saved."))
    else:
        await update.message.reply_text(result.get("response", "Done."))


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def post_init(application):
        scheduler.add_job(
            send_daily_briefing,
            "cron",
            hour=DAILY_BRIEFING_HOUR,
            minute=0,
            timezone=EST,
            args=[application],
            id="daily_briefing",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(f"SalesFlow bot started. Daily briefing at {DAILY_BRIEFING_HOUR}:00 EST.")

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
