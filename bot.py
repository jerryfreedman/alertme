import os
import json
import logging
from datetime import datetime
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

# ─── Config ───────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"])
DAILY_BRIEFING_HOUR = int(os.environ.get("DAILY_BRIEFING_HOUR", "7"))
EST = pytz.timezone("America/New_York")

# ─── Clients ──────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Conversation State ────────────────────────
# Tracks multi-turn intake sessions and disambiguation
conversation_state = {}
# Structure per chat_id:
# {
#   "mode": "intake" | "disambiguation" | None,
#   "partial_entities": {},       # extracted so far
#   "raw_message": str,           # original user message
#   "questions": [],              # questions bot asked
#   "answers": [],                # user's answers so far
#   "account_context": str,       # existing DB context for this account
#   "disambiguation_choices": [], # for disambiguation mode
#   "disambiguation_field": str,
# }


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are SalesFlow, a personal AI CRM assistant and intelligent account management partner. You actively help capture, structure, and surface sales intelligence — asking smart follow-up questions to ensure every interaction is fully documented.

## Database Schema
- accounts: id, name, industry, website, size, notes
- contacts: id, first_name, last_name, email, phone, title, account_id, linkedin, notes
- opportunities: id, name, account_id, primary_contact_id, stage, value, currency, close_date, probability, notes
  - stages: prospecting / qualified / proposal / negotiation / closed_won / closed_lost
- interactions: id, type (call/email/meeting/note), raw_text, summary, next_steps, account_id, opportunity_id, contact_ids[]
- tasks: id, title, due_at, account_id, opportunity_id, contact_id, completed

## Your Core Behavior
You are an active interviewer, not a passive recorder. When someone logs a call or meeting, you ALWAYS ask 2-3 smart follow-up questions to fill in missing deal intelligence. Your questions should be:
- Specific to this account/deal based on existing DB context
- Focused on the highest-value missing data (stage, value, blockers, next steps, timeline)
- Conversational and brief — not a form

## Response Formats (always valid JSON, no markdown)

### INTAKE — Use for ANY new interaction log (call, meeting, email, note)
Extract what you can, then ask 2-3 targeted follow-up questions.
{
  "intent": "INTAKE",
  "partial_entities": {
    "account_name": "string or null",
    "contact_first_name": "string or null",
    "contact_last_name": "string or null",
    "contact_title": "string or null",
    "interaction_type": "call|email|meeting|note",
    "interaction_summary": "string or null",
    "next_steps": "string or null",
    "opportunity_stage": "string or null",
    "opportunity_value": number or null,
    "opportunity_close_date": "YYYY-MM-DD or null",
    "task_title": "string or null",
    "task_due_at": "ISO8601 or null"
  },
  "intro": "Short friendly opener referencing what you heard (1 line)",
  "questions": [
    "Question 1 — most important missing field",
    "Question 2 — second most important",
    "Question 3 — optional, only if genuinely needed"
  ]
}

### STORE — Use ONLY when finalizing after intake answers, or if message already has complete info
{
  "intent": "STORE",
  "entities": { ...same fields as partial_entities... },
  "confirmation": "✓ Saved summary (2-3 lines max, use bullet points for key details)"
}

### QUERY — User asking a question about their data
{
  "intent": "QUERY",
  "response": "Full answer based on DB context. Be specific, reference actual data."
}

### TASK — Setting a reminder only (no interaction to log)
{
  "intent": "TASK",
  "task_title": "string",
  "task_due_at": "ISO8601 or null",
  "account_name": "string or null",
  "confirmation": "✓ Reminder set: [task] — [date/time]"
}

### DRAFT — Writing an email or message
{
  "intent": "DRAFT",
  "subject": "string or null",
  "body": "Full draft text",
  "context_used": "Brief note on context pulled"
}

### DISAMBIGUATE — Name/entity is ambiguous
{
  "intent": "DISAMBIGUATE",
  "field": "contact|account|opportunity",
  "question": "Which [thing] do you mean?",
  "choices": ["Option 1", "Option 2", "New"]
}

### GENERAL — General question or conversation
{
  "intent": "GENERAL",
  "response": "Your helpful response."
}

## Smart Question Guidelines
When writing INTAKE questions, use the DB context to be specific:
- If account exists: reference what you already know ("You mentioned pricing was a concern last time — did that come up?")
- If deal stage is unknown: ask ("Where does this deal sit — are they evaluating, or ready to move?")
- If no value captured: ask ("Any budget or deal size mentioned?")
- If no follow-up: ask ("What's the next step, and when should I remind you?")
- If new contact: ask ("What's [name]'s role, and are they the decision maker?")

Never ask about something already clearly stated. Max 3 questions. Keep them punchy.

Today's date: """ + datetime.now(EST).strftime("%A, %B %d %Y") + """
"""


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_crm_context(account_name: str = None) -> str:
    """Pull CRM data. If account_name given, fetch deep context for that account."""
    try:
        parts = []

        if account_name:
            # Deep dive on specific account
            accs = supabase.table("accounts").select("*").ilike("name", f"%{account_name}%").execute()
            if accs.data:
                acc = accs.data[0]
                parts.append(f"## Account: {acc['name']}")
                parts.append(f"Industry: {acc.get('industry','')} | Notes: {acc.get('notes','')}")

                # Contacts at this account
                contacts = supabase.table("contacts").select("*").eq("account_id", acc["id"]).execute()
                if contacts.data:
                    parts.append("\n### Contacts")
                    for c in contacts.data:
                        parts.append(f"- {c.get('first_name','')} {c.get('last_name','')} | {c.get('title','')} | {c.get('email','')}")

                # Open opportunities
                opps = supabase.table("opportunities").select("*").eq("account_id", acc["id"]).execute()
                if opps.data:
                    parts.append("\n### Opportunities")
                    for o in opps.data:
                        parts.append(f"- {o.get('name','?')} | Stage: {o.get('stage','?')} | Value: ${o.get('value') or 0:,.0f} | Close: {o.get('close_date','?')}")

                # Interaction history
                ints = supabase.table("interactions").select("*").eq("account_id", acc["id"]).order("created_at", desc=True).limit(15).execute()
                if ints.data:
                    parts.append("\n### Interaction History")
                    for i in ints.data:
                        parts.append(f"- [{i['created_at'][:10]}] {i.get('type','').upper()}: {i.get('summary', i.get('raw_text',''))[:100]} | Next: {i.get('next_steps','')[:60]}")

                # Pending tasks
                tasks = supabase.table("tasks").select("*").eq("account_id", acc["id"]).eq("completed", False).execute()
                if tasks.data:
                    parts.append("\n### Pending Tasks")
                    for t in tasks.data:
                        parts.append(f"- {t['title']} | Due: {str(t.get('due_at',''))[:10]}")

        else:
            # General context: recent interactions + pipeline
            interactions = supabase.table("interactions").select(
                "*, accounts(name), opportunities(name, stage, value)"
            ).order("created_at", desc=True).limit(25).execute()
            if interactions.data:
                parts.append("## Recent Interactions")
                for i in interactions.data:
                    acc = i.get("accounts") or {}
                    parts.append(f"- [{i['created_at'][:10]}] {acc.get('name','?')} | {i.get('type','').upper()}: {i.get('summary', i.get('raw_text',''))[:100]}")

            accounts = supabase.table("accounts").select("id, name, industry").execute()
            if accounts.data:
                parts.append("\n## Accounts")
                for a in accounts.data:
                    parts.append(f"- {a['name']} (ID: {a['id']})")

            contacts = supabase.table("contacts").select(
                "id, first_name, last_name, title, accounts(name)"
            ).execute()
            if contacts.data:
                parts.append("\n## Contacts")
                for c in contacts.data:
                    acc = c.get("accounts") or {}
                    parts.append(f"- {c.get('first_name','')} {c.get('last_name','')} | {c.get('title','')} @ {acc.get('name','')}")

            opps = supabase.table("opportunities").select(
                "id, name, stage, value, close_date, accounts(name)"
            ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()
            if opps.data:
                parts.append("\n## Open Pipeline")
                for o in opps.data:
                    acc = o.get("accounts") or {}
                    parts.append(f"- {acc.get('name','?')} | {o.get('stage','?')} | ${o.get('value') or 0:,.0f} | Close: {o.get('close_date','?')}")

            tasks = supabase.table("tasks").select(
                "id, title, due_at, accounts(name)"
            ).eq("completed", False).order("due_at").limit(15).execute()
            if tasks.data:
                parts.append("\n## Pending Tasks")
                for t in tasks.data:
                    acc = t.get("accounts") or {}
                    parts.append(f"- {t['title']} | {acc.get('name','')} | Due: {str(t.get('due_at',''))[:10]}")

        return "\n".join(parts) if parts else "No CRM data yet — this is your first entry!"

    except Exception as e:
        logger.error(f"CRM context error: {e}")
        return "Could not fetch CRM data."


def find_or_create_account(name: str) -> str | None:
    if not name:
        return None
    try:
        result = supabase.table("accounts").select("id").ilike("name", name).execute()
        if result.data:
            return result.data[0]["id"]
        new = supabase.table("accounts").insert({"name": name}).execute()
        return new.data[0]["id"] if new.data else None
    except Exception as e:
        logger.error(f"Account error: {e}")
        return None


def find_or_create_contact(first: str, last: str, account_id: str = None, title: str = None) -> str | None:
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
        new = supabase.table("contacts").insert({
            "first_name": first or "",
            "last_name": last or "",
            "account_id": account_id,
            "title": title,
        }).execute()
        return new.data[0]["id"] if new.data else None
    except Exception as e:
        logger.error(f"Contact error: {e}")
        return None


def find_or_update_opportunity(account_id: str, contact_id: str, entities: dict) -> str | None:
    if not account_id:
        return None
    try:
        result = supabase.table("opportunities").select("id").eq(
            "account_id", account_id
        ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()

        if result.data:
            opp_id = result.data[0]["id"]
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

        new = supabase.table("opportunities").insert({
            "name": f"{entities.get('account_name', 'Deal')} Opportunity",
            "account_id": account_id,
            "primary_contact_id": contact_id,
            "stage": entities.get("opportunity_stage", "prospecting"),
            "value": entities.get("opportunity_value"),
            "close_date": entities.get("opportunity_close_date"),
        }).execute()
        return new.data[0]["id"] if new.data else None
    except Exception as e:
        logger.error(f"Opportunity error: {e}")
        return None


def store_complete_interaction(entities: dict, raw_text: str) -> bool:
    """Save fully-formed interaction + related entities to Supabase."""
    try:
        account_id = find_or_create_account(entities.get("account_name"))
        contact_id = find_or_create_contact(
            entities.get("contact_first_name"),
            entities.get("contact_last_name"),
            account_id,
            entities.get("contact_title"),
        )
        opportunity_id = find_or_update_opportunity(account_id, contact_id, entities)

        supabase.table("interactions").insert({
            "type": entities.get("interaction_type", "note"),
            "raw_text": raw_text,
            "summary": entities.get("interaction_summary", ""),
            "next_steps": entities.get("next_steps", ""),
            "account_id": account_id,
            "opportunity_id": opportunity_id,
            "contact_ids": [contact_id] if contact_id else [],
        }).execute()

        if entities.get("task_title"):
            supabase.table("tasks").insert({
                "title": entities["task_title"],
                "due_at": entities.get("task_due_at"),
                "account_id": account_id,
                "opportunity_id": opportunity_id,
                "contact_id": contact_id,
            }).execute()

        return True
    except Exception as e:
        logger.error(f"Store error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE
# ─────────────────────────────────────────────────────────────────────────────

def ask_claude(user_message: str, db_context: str) -> dict:
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"## Database Context\n{db_context}\n\n## User Message\n{user_message}"
            }]
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude returned invalid JSON")
        return {"intent": "GENERAL", "response": "Sorry, I had trouble processing that. Try rephrasing?"}
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return {"intent": "GENERAL", "response": "Having trouble connecting right now. Try again in a moment."}


def format_intake_message(intro: str, questions: list[str]) -> str:
    """Format the intake questions into a clean Telegram message."""
    lines = [intro, ""]
    for i, q in enumerate(questions, 1):
        lines.append(f"*{i}.* {q}")
    lines.append("")
    lines.append("_Answer all at once or skip any with 'n/a'_")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# DAILY BRIEFING
# ─────────────────────────────────────────────────────────────────────────────

async def send_daily_briefing(app: Application):
    try:
        db_context = get_crm_context()
        result = ask_claude(
            "Generate a daily morning briefing. Include: "
            "1) Tasks due today or overdue, "
            "2) Deals with no activity in 14+ days, "
            "3) Pipeline summary (open deals + total value). "
            "Format cleanly with emojis. Under 20 lines.",
            db_context
        )
        text = result.get("response", "Good morning! No briefing data available.")
        await app.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"☀️ *Good morning! Daily Briefing*\n\n{text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Briefing error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text(
        "👋 *SalesFlow is live!*\n\n"
        "Just talk to me naturally:\n\n"
        "• *Log a call:* \"Just got off with Sarah at Acme\"\n"
        "• *I'll ask follow-up questions* to capture everything\n"
        "• *Query:* \"What's the status of Acme?\"\n"
        "• *Remind:* \"Remind me to follow up with John on Friday\"\n"
        "• *Draft:* \"Draft a follow-up email to Sarah\"\n"
        "• *Pipeline:* \"Show me my open deals\"\n\n"
        "The more you use me, the smarter I get about your deals. 🧠",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id != ALLOWED_CHAT_ID:
        return

    user_text = update.message.text.strip()
    state = conversation_state.get(chat_id, {})
    mode = state.get("mode")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # ── INTAKE MODE: user is answering follow-up questions ──────────────────
    if mode == "intake":
        await handle_intake_answer(update, context, user_text, state)
        return

    # ── DISAMBIGUATION MODE: user picked a numbered option ──────────────────
    if mode == "disambiguation" and user_text.isdigit():
        await handle_disambiguation_choice(update, context, int(user_text), state)
        return

    # ── FRESH MESSAGE ────────────────────────────────────────────────────────
    # Get relevant context — if account name is detectable, go deep on it
    account_hint = extract_account_hint(user_text)
    db_context = get_crm_context(account_name=account_hint)

    result = ask_claude(user_text, db_context)
    intent = result.get("intent", "GENERAL")

    if intent == "INTAKE":
        # Save partial state and ask follow-up questions
        conversation_state[chat_id] = {
            "mode": "intake",
            "partial_entities": result.get("partial_entities", {}),
            "raw_message": user_text,
            "questions": result.get("questions", []),
            "answers": [],
            "account_context": db_context,
        }
        msg = format_intake_message(
            result.get("intro", "Got it — a couple quick questions:"),
            result.get("questions", [])
        )
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif intent == "STORE":
        # Direct store (Claude decided no questions needed)
        success = store_complete_interaction(result.get("entities", {}), user_text)
        conversation_state.pop(chat_id, None)
        msg = result.get("confirmation", "✓ Saved.")
        if not success:
            msg = "⚠️ Had trouble saving — please try again."
        await update.message.reply_text(msg, parse_mode="Markdown")

    elif intent == "DISAMBIGUATE":
        conversation_state[chat_id] = {
            "mode": "disambiguation",
            "original_message": user_text,
            "disambiguation_choices": result.get("choices", []),
            "disambiguation_field": result.get("field"),
        }
        choices_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(result.get("choices", [])))
        await update.message.reply_text(
            f"{result.get('question', 'Which one?')}\n\n{choices_text}",
            parse_mode="Markdown"
        )

    elif intent == "QUERY":
        await update.message.reply_text(result.get("response", "No data found."), parse_mode="Markdown")

    elif intent == "TASK":
        try:
            account_id = find_or_create_account(result.get("account_name")) if result.get("account_name") else None
            supabase.table("tasks").insert({
                "title": result.get("task_title", "Follow up"),
                "due_at": result.get("task_due_at"),
                "account_id": account_id,
            }).execute()
        except Exception as e:
            logger.error(f"Task save error: {e}")
        await update.message.reply_text(result.get("confirmation", "✓ Reminder saved."), parse_mode="Markdown")

    elif intent == "DRAFT":
        subject = result.get("subject", "")
        body = result.get("body", "")
        header = f"*Subject: {subject}*\n\n" if subject else ""
        await update.message.reply_text(f"📧 *Draft*\n\n{header}{body}", parse_mode="Markdown")

    else:  # GENERAL
        await update.message.reply_text(result.get("response", "I'm not sure how to help with that."), parse_mode="Markdown")


async def handle_intake_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, state: dict):
    """Process user's answers to intake questions, then finalize the store."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Check for cancel
    if user_text.lower() in ["cancel", "stop", "nevermind", "never mind"]:
        conversation_state.pop(chat_id, None)
        await update.message.reply_text("No problem — entry cancelled.")
        return

    # Build full context: original message + partial entities + questions + answers
    original = state.get("raw_message", "")
    partial = state.get("partial_entities", {})
    questions = state.get("questions", [])
    db_context = state.get("account_context", "")

    synthesis_prompt = (
        f"Original message: {original}\n\n"
        f"Partial data already extracted: {json.dumps(partial)}\n\n"
        f"Questions you asked: {json.dumps(questions)}\n\n"
        f"User's answers: {user_text}\n\n"
        "Now synthesize all of this into a complete STORE response. "
        "Fill in as many fields as possible from both the original message and the answers. "
        "If the user said 'n/a' or 'skip' for something, leave that field null. "
        "Write a confirmation that summarizes the key captured details."
    )

    result = ask_claude(synthesis_prompt, db_context)

    # Force to STORE if Claude returned something else
    if result.get("intent") == "INTAKE":
        # Claude wants to ask more questions — allow one more round
        conversation_state[chat_id] = {
            **state,
            "partial_entities": result.get("partial_entities", partial),
            "questions": result.get("questions", []),
            "answers": state.get("answers", []) + [user_text],
        }
        msg = format_intake_message(
            result.get("intro", "Just a couple more:"),
            result.get("questions", [])
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    entities = result.get("entities", partial)
    raw_full = f"{original}\n\nFollow-up answers: {user_text}"
    success = store_complete_interaction(entities, raw_full)
    conversation_state.pop(chat_id, None)

    if success:
        await update.message.reply_text(
            result.get("confirmation", "✓ Saved — all details captured."),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Had trouble saving — please try again.")


async def handle_disambiguation_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: int, state: dict):
    """User picked a numbered option from disambiguation."""
    chat_id = update.effective_chat.id
    choices = state.get("disambiguation_choices", [])

    if choice < 1 or choice > len(choices):
        await update.message.reply_text("Invalid choice — please try again.")
        return

    selected = choices[choice - 1]
    conversation_state.pop(chat_id, None)

    enriched = f"{state.get('original_message', '')} [Clarification: {selected}]"
    account_hint = extract_account_hint(enriched)
    db_context = get_crm_context(account_name=account_hint)
    result = ask_claude(enriched, db_context)

    if result.get("intent") == "INTAKE":
        conversation_state[chat_id] = {
            "mode": "intake",
            "partial_entities": result.get("partial_entities", {}),
            "raw_message": enriched,
            "questions": result.get("questions", []),
            "answers": [],
            "account_context": db_context,
        }
        msg = format_intake_message(result.get("intro", "Got it:"), result.get("questions", []))
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif result.get("intent") == "STORE":
        store_complete_interaction(result.get("entities", {}), enriched)
        await update.message.reply_text(result.get("confirmation", "✓ Saved."), parse_mode="Markdown")
    else:
        await update.message.reply_text(result.get("response", "Done."), parse_mode="Markdown")


def extract_account_hint(text: str) -> str | None:
    """Quick heuristic to pull a possible account name for targeted DB lookup."""
    # Look for "at [Company]" or "with [Name] at [Company]" patterns
    import re
    patterns = [
        r'\bat\s+([A-Z][a-zA-Z\s&]+?)(?:\s*[,\.\-]|$)',
        r'\bfrom\s+([A-Z][a-zA-Z\s&]+?)(?:\s*[,\.\-]|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

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
        logger.info(f"SalesFlow started. Briefing at {DAILY_BRIEFING_HOUR}:00 EST.")

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
