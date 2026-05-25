import os
import re
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

# Messages longer than this are treated as bulk pastes
BULK_THRESHOLD = 500

# ─── Clients ──────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─── Conversation State ────────────────────────
conversation_state = {}
# Per chat_id:
# {
#   "mode": "intake" | "bulk_confirm" | "disambiguation" | None,
#   "partial_entities": {},
#   "raw_message": str,
#   "questions": [],           # all questions shown to user (dedup + intake)
#   "dedup_info": [],          # list of dedup question dicts
#   "dedup_question_count": 0,
#   "answers": [],
#   "account_context": str,
#   "disambiguation_choices": [],
#   "disambiguation_field": str,
#   "bulk_entities": [],       # resolved entity list for bulk mode
#   "bulk_dedup_info": [],     # dedup questions for bulk (with entity_index)
#   "bulk_dedup_pending": bool,
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

## Response Formats — always return valid JSON with NO markdown code fences

### INTAKE — New interaction log (call, meeting, email, note)
Extract all you can, then ask 2-3 targeted follow-up questions about what's missing.
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
    "Question 3 — optional third"
  ]
}

### STORE — Finalizing after intake answers, or all info is already complete
{
  "intent": "STORE",
  "entities": { ...same fields as partial_entities... },
  "confirmation": "✓ Saved: [2-3 bullet points with key details]"
}

### BULK — Large paste containing multiple distinct interactions or accounts
Use ONLY when the input clearly contains multiple separate interactions/accounts.
{
  "intent": "BULK",
  "entities_list": [
    {
      "account_name": "string",
      "contact_first_name": "string or null",
      "contact_last_name": "string or null",
      "contact_title": "string or null",
      "interaction_type": "call|email|meeting|note",
      "interaction_summary": "string",
      "next_steps": "string or null",
      "opportunity_stage": "string or null",
      "opportunity_value": number or null,
      "opportunity_close_date": "YYYY-MM-DD or null",
      "task_title": "string or null",
      "task_due_at": "ISO8601 or null"
    }
  ],
  "summary": "Found X accounts, Y contacts, Z interactions"
}

### QUERY — User asking a question about their pipeline or contacts
{
  "intent": "QUERY",
  "response": "Full answer based on DB context. Reference actual names and numbers."
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
  "context_used": "Brief note on what context was used"
}

### DISAMBIGUATE — A name or entity is genuinely ambiguous
{
  "intent": "DISAMBIGUATE",
  "field": "contact|account|opportunity",
  "question": "Which [thing] do you mean?",
  "choices": ["Option 1", "Option 2", "New"]
}

### GENERAL — Anything else: general questions, conversation
{
  "intent": "GENERAL",
  "response": "Your helpful response."
}

## Smart Question Guidelines
- Reference existing DB context: "You mentioned pricing was a concern last time — did that come up?"
- If stage unknown: "Where does this deal sit — evaluating, or ready to move?"
- If no value captured: "Any deal size or budget mentioned?"
- If no next step: "What's the next step, and when should I remind you?"
- If new contact: "What's their role, and are they the decision maker?"
- NEVER ask what's already clearly stated
- Max 3 questions — keep them punchy
- For BULK intent: extract everything you can; dedup is handled externally so don't add questions

Today's date: """ + datetime.now(EST).strftime("%A, %B %d %Y") + """
"""


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_crm_context(account_name: str = None) -> str:
    """Pull CRM context. Deep dive for a specific account, general pipeline otherwise."""
    try:
        parts = []

        if account_name:
            accs = supabase.table("accounts").select("*").ilike("name", f"%{account_name}%").execute()
            if accs.data:
                acc = accs.data[0]
                parts.append(f"## Account: {acc['name']}")
                parts.append(f"Industry: {acc.get('industry','')} | Notes: {acc.get('notes','')}")

                contacts = supabase.table("contacts").select("*").eq("account_id", acc["id"]).execute()
                if contacts.data:
                    parts.append("\n### Contacts")
                    for c in contacts.data:
                        parts.append(f"- {c.get('first_name','')} {c.get('last_name','')} | {c.get('title','')} | {c.get('email','')}")

                opps = supabase.table("opportunities").select("*").eq("account_id", acc["id"]).execute()
                if opps.data:
                    parts.append("\n### Opportunities")
                    for o in opps.data:
                        parts.append(f"- {o.get('name','?')} | Stage: {o.get('stage','?')} | Value: ${o.get('value') or 0:,.0f} | Close: {o.get('close_date','?')}")

                ints = supabase.table("interactions").select("*").eq("account_id", acc["id"]).order("created_at", desc=True).limit(15).execute()
                if ints.data:
                    parts.append("\n### Interaction History")
                    for i in ints.data:
                        parts.append(f"- [{i['created_at'][:10]}] {i.get('type','').upper()}: {i.get('summary', i.get('raw_text',''))[:100]} | Next: {i.get('next_steps','')[:60]}")

                tasks = supabase.table("tasks").select("*").eq("account_id", acc["id"]).eq("completed", False).execute()
                if tasks.data:
                    parts.append("\n### Pending Tasks")
                    for t in tasks.data:
                        parts.append(f"- {t['title']} | Due: {str(t.get('due_at',''))[:10]}")

        else:
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


def check_for_duplicates(entities: dict, entity_index: int = 0) -> tuple[list[dict], dict]:
    """
    Fuzzy-check Supabase for potential duplicate accounts and contacts.

    Returns:
      dedup_info  — list of question dicts: {question, type, existing_name,
                    candidates, new_name, entity_index}
      matches     — {account_exact, contact_exact} for silent normalization
    """
    dedup_info = []
    matches = {}

    # ── Account check ────────────────────────────────────────────────────────
    account_name = (entities.get("account_name") or "").strip()
    if account_name:
        try:
            # Exact case-insensitive match — silently normalize, no question
            exact = supabase.table("accounts").select("id, name").ilike("name", account_name).execute()
            if exact.data:
                matches["account_exact"] = exact.data[0]
            else:
                # Fuzzy: search on the first substantial word
                words = [w for w in account_name.split() if len(w) > 3]
                if words:
                    fuzzy = supabase.table("accounts").select("id, name").ilike(
                        "name", f"%{words[0]}%"
                    ).limit(5).execute()
                    if fuzzy.data:
                        candidates = [r["name"] for r in fuzzy.data]
                        if len(candidates) == 1:
                            dedup_info.append({
                                "question": (
                                    f'⚠️ *Possible duplicate:* Is "{account_name}" '
                                    f'the same as existing account "{candidates[0]}"? (yes / no)'
                                ),
                                "type": "account",
                                "existing_name": candidates[0],
                                "new_name": account_name,
                                "entity_index": entity_index,
                            })
                        else:
                            names_str = " / ".join(f'"{n}"' for n in candidates[:3])
                            dedup_info.append({
                                "question": (
                                    f'⚠️ *Similar accounts found:* Does "{account_name}" '
                                    f'match any of these? {names_str} '
                                    f'— or is it new? (type exact name or "new")'
                                ),
                                "type": "account_multi",
                                "existing_name": candidates[0],
                                "candidates": candidates[:3],
                                "new_name": account_name,
                                "entity_index": entity_index,
                            })
        except Exception as e:
            logger.error(f"Account dedup error: {e}")

    # ── Contact check ────────────────────────────────────────────────────────
    first = (entities.get("contact_first_name") or "").strip()
    last = (entities.get("contact_last_name") or "").strip()
    if last:
        try:
            fuzzy_c = supabase.table("contacts").select(
                "id, first_name, last_name, accounts(name)"
            ).ilike("last_name", last).limit(5).execute()

            if fuzzy_c.data:
                # Exact first+last match → silent normalization
                exact_c = [
                    c for c in fuzzy_c.data
                    if not first or c["first_name"].lower() == first.lower()
                ]
                if exact_c:
                    matches["contact_exact"] = exact_c[0]
                else:
                    # Same last name, first 2 chars of first name match → probably same person
                    similar = [
                        c for c in fuzzy_c.data
                        if first and c["first_name"].lower()[:2] == first.lower()[:2]
                    ]
                    if similar:
                        s = similar[0]
                        existing_name = f"{s['first_name']} {s['last_name']}"
                        existing_co = (s.get("accounts") or {}).get("name", "unknown company")
                        input_name = f"{first} {last}".strip()
                        dedup_info.append({
                            "question": (
                                f'⚠️ *Possible duplicate:* Is "{input_name}" '
                                f'the same person as "{existing_name}" at {existing_co}? (yes / no)'
                            ),
                            "type": "contact",
                            "existing_name": existing_name,
                            "new_name": input_name,
                            "existing_id": s["id"],
                            "entity_index": entity_index,
                        })
                        matches["contact_candidate"] = s
        except Exception as e:
            logger.error(f"Contact dedup error: {e}")

    return dedup_info, matches


def resolve_dedup_answers(dedup_info: list[dict], raw_answers: list[str]) -> dict:
    """
    Given dedup question dicts and user's yes/no answers, return entity field overrides.
    e.g. {"account_name": "Acme Corporation"} if user confirmed an existing account.
    """
    overrides = {}
    for i, info in enumerate(dedup_info):
        if i >= len(raw_answers):
            break
        answer = raw_answers[i].strip().lower()
        dtype = info.get("type", "")

        if dtype == "account":
            if answer in ("yes", "y"):
                overrides["account_name"] = info["existing_name"]
            # "no" → keep new_name (no override needed)

        elif dtype == "account_multi":
            if answer not in ("new", "no", "n"):
                candidates = info.get("candidates", [])
                matched = next(
                    (c for c in candidates if c.lower() == answer),
                    next((c for c in candidates if answer in c.lower()), None)
                )
                if matched:
                    overrides["account_name"] = matched

        elif dtype == "contact":
            if answer in ("yes", "y"):
                parts = info["existing_name"].split(" ", 1)
                overrides["contact_first_name"] = parts[0]
                overrides["contact_last_name"] = parts[1] if len(parts) > 1 else ""
            # "no" → keep original

    return overrides


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
        logger.error(f"Account upsert error: {e}")
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
        logger.error(f"Contact upsert error: {e}")
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
        logger.error(f"Opportunity upsert error: {e}")
        return None


def store_complete_interaction(entities: dict, raw_text: str) -> bool:
    """Save a fully-formed interaction and all related entities to Supabase."""
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

def ask_claude(user_message: str, db_context: str, extra_instructions: str = "") -> dict:
    try:
        content = f"## Database Context\n{db_context}\n\n## User Message\n{user_message}"
        if extra_instructions:
            content += f"\n\n## Additional Instructions\n{extra_instructions}"
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if Claude wrapped anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude returned invalid JSON")
        return {"intent": "GENERAL", "response": "I had trouble processing that — try rephrasing?"}
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return {"intent": "GENERAL", "response": "Having trouble connecting right now. Try again in a moment."}


async def safe_reply(update: Update, text: str) -> None:
    """
    Send a reply safely:
    - Splits messages that exceed Telegram's 4096-char limit
    - Falls back to plain text if Markdown causes a parse error
      (happens when account names contain special chars like & . ( ) -)
    """
    MAX = 4000
    # Split long text at newlines
    chunks: list[str] = []
    while text:
        if len(text) <= MAX:
            chunks.append(text)
            break
        split = text.rfind("\n", 0, MAX)
        if split < 1:
            split = MAX
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")

    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            # Strip Markdown syntax and retry as plain text
            clean = re.sub(r"[*_`]", "", chunk)
            clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean)
            await update.message.reply_text(clean)


def format_questions(intro: str, questions: list[str]) -> str:
    """Format numbered questions as a clean Telegram message."""
    lines = [intro, ""]
    for i, q in enumerate(questions, 1):
        lines.append(f"*{i}.* {q}")
    lines.append("")
    lines.append("_Answer by number, or skip any with 'n/a'_")
    return "\n".join(lines)


def parse_numbered_answers(text: str) -> list[str]:
    """Split 'yes\n2. no\n3. Proposal' into ['yes', 'no', 'Proposal']."""
    parts = re.split(r"\n?\s*\d+[\.\)]\s*", text.strip())
    if len(parts) > 1 and parts[0] == "":
        parts = parts[1:]
    return [p.strip() for p in parts if p.strip()]


def extract_account_hint(text: str) -> str | None:
    """Heuristic to pull a company name for targeted DB lookup."""
    patterns = [
        r"\bwith\s+\w+\s+at\s+([A-Z][a-zA-Z0-9\s&\.,]+?)(?:\s*[,\.\-\n]|$)",
        r"\bat\s+([A-Z][a-zA-Z0-9\s&\.,]+?)(?:\s*[,\.\-\n]|$)",
        r"\bfrom\s+([A-Z][a-zA-Z0-9\s&\.,]+?)(?:\s*[,\.\-\n]|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SLASH COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — clear any active intake or disambiguation state."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    had_state = bool(conversation_state.pop(update.effective_chat.id, None))
    msg = "↩️ Cancelled — ready for a fresh start." if had_state else "Nothing to cancel — all good!"
    await update.message.reply_text(msg)


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tasks — list all pending (incomplete) tasks."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        result = supabase.table("tasks").select(
            "title, due_at, accounts(name)"
        ).eq("completed", False).order("due_at").execute()

        if not result.data:
            await update.message.reply_text("✅ No pending tasks — you're all clear!")
            return

        now = datetime.now(EST)
        lines = ["📋 *Pending Tasks*\n"]
        for t in result.data:
            acc = (t.get("accounts") or {}).get("name", "")
            due_raw = t.get("due_at") or ""
            due_str = ""
            if due_raw:
                try:
                    due_dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).astimezone(EST)
                    if due_dt < now:
                        due_str = f"🔴 OVERDUE ({due_dt.strftime('%b %d')})"
                    elif due_dt.date() == now.date():
                        due_str = f"🟡 Today {due_dt.strftime('%I:%M %p')}"
                    else:
                        due_str = due_dt.strftime("%b %d")
                except Exception:
                    due_str = due_raw[:10]
            line = f"• {t['title']}"
            if acc:
                line += f" — {acc}"
            if due_str:
                line += f" | {due_str}"
            lines.append(line)

        await safe_reply(update, "\n".join(lines))
    except Exception as e:
        logger.error(f"/tasks error: {e}")
        await update.message.reply_text("Couldn't fetch tasks right now.")


async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pipeline — show all open opportunities with stage and value."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        result = supabase.table("opportunities").select(
            "name, stage, value, close_date, probability, accounts(name)"
        ).not_.in_("stage", ["closed_won", "closed_lost"]).order("value", desc=True).execute()

        if not result.data:
            await update.message.reply_text("📭 No open opportunities yet.")
            return

        stage_emoji = {
            "prospecting": "🔵", "qualified": "🟡",
            "proposal": "🟠", "negotiation": "🔴",
        }
        total = sum(o.get("value") or 0 for o in result.data)
        weighted = sum((o.get("value") or 0) * ((o.get("probability") or 0) / 100) for o in result.data)
        lines = [
            f"📊 *Open Pipeline*",
            f"Total: ${total:,.0f}  |  Weighted: ${weighted:,.0f}\n",
        ]
        for o in result.data:
            acc = (o.get("accounts") or {}).get("name", "Unknown")
            stage = o.get("stage", "prospecting")
            emoji = stage_emoji.get(stage, "⚪️")
            val = f"${o.get('value'):,.0f}" if o.get("value") else "no value"
            close = (o.get("close_date") or "")[:10]
            prob = f"{o.get('probability')}%" if o.get("probability") else ""
            detail = " | ".join(filter(None, [val, prob, f"Close: {close}" if close else ""]))
            lines.append(f"{emoji} *{acc}* — {stage.replace('_', ' ').title()}")
            lines.append(f"   {detail}")
        await safe_reply(update, "\n".join(lines))
    except Exception as e:
        logger.error(f"/pipeline error: {e}")
        await update.message.reply_text("Couldn't fetch pipeline right now.")


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/accounts — list all accounts with open opportunity count."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        accounts = supabase.table("accounts").select("id, name, industry").order("name").execute()
        if not accounts.data:
            await update.message.reply_text("No accounts yet — log your first interaction to get started.")
            return

        # Get open opp counts per account
        opps = supabase.table("opportunities").select(
            "account_id"
        ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()
        opp_counts: dict[str, int] = {}
        for o in (opps.data or []):
            aid = o["account_id"]
            opp_counts[aid] = opp_counts.get(aid, 0) + 1

        lines = [f"🏢 *Accounts ({len(accounts.data)})*\n"]
        for a in accounts.data:
            industry = f" ({a['industry']})" if a.get("industry") else ""
            open_opps = opp_counts.get(a["id"], 0)
            opp_note = f" | {open_opps} open opp{'s' if open_opps != 1 else ''}" if open_opps else ""
            lines.append(f"• *{a['name']}*{industry}{opp_note}")

        await safe_reply(update, "\n".join(lines))
    except Exception as e:
        logger.error(f"/accounts error: {e}")
        await update.message.reply_text("Couldn't fetch accounts right now.")


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
        "Talk to me naturally:\n\n"
        "• *Log a call:* \"Just got off with Sarah at Acme Corp\"\n"
        "• *Bulk paste:* Drop in a full summary from ChatGPT or Claude\n"
        "• *I check for duplicates immediately* and ask before saving\n"
        "• *Query:* \"What's the status of Acme?\"\n"
        "• *Remind:* \"Remind me to follow up with John on Friday\"\n"
        "• *Draft:* \"Draft a follow-up email to Sarah\"\n"
        "• *Pipeline:* \"Show me open deals\"\n\n"
        "The more you use me, the smarter I get about your accounts. 🧠",
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

    # ── Active conversation modes ────────────────────────────────────────────
    if mode == "intake":
        await handle_intake_answer(update, context, user_text, state)
        return

    if mode == "bulk_confirm":
        await handle_bulk_confirm(update, context, user_text, state)
        return

    if mode == "disambiguation" and user_text.isdigit():
        await handle_disambiguation_choice(update, context, int(user_text), state)
        return

    # ── Fresh message ────────────────────────────────────────────────────────
    if len(user_text) >= BULK_THRESHOLD:
        await handle_bulk_paste(update, context, user_text)
        return

    account_hint = extract_account_hint(user_text)
    db_context = get_crm_context(account_name=account_hint)
    result = ask_claude(user_text, db_context)
    intent = result.get("intent", "GENERAL")

    # ── INTAKE ───────────────────────────────────────────────────────────────
    if intent == "INTAKE":
        partial = result.get("partial_entities", {})
        dedup_info, matches = check_for_duplicates(partial)

        # Silent exact-match normalization
        if "account_exact" in matches:
            partial["account_name"] = matches["account_exact"]["name"]
        if "contact_exact" in matches:
            c = matches["contact_exact"]
            partial["contact_first_name"] = c["first_name"]
            partial["contact_last_name"] = c["last_name"]

        all_questions = [d["question"] for d in dedup_info] + result.get("questions", [])[:3]

        conversation_state[chat_id] = {
            "mode": "intake",
            "partial_entities": partial,
            "raw_message": user_text,
            "questions": all_questions,
            "dedup_info": dedup_info,
            "dedup_question_count": len(dedup_info),
            "answers": [],
            "account_context": db_context,
        }
        await update.message.reply_text(
            format_questions(result.get("intro", "Got it — a few quick questions:"), all_questions),
            parse_mode="Markdown"
        )

    # ── STORE (Claude decided it's already complete) ─────────────────────────
    elif intent == "STORE":
        entities = result.get("entities", {})
        dedup_info, matches = check_for_duplicates(entities)

        if "account_exact" in matches:
            entities["account_name"] = matches["account_exact"]["name"]
        if "contact_exact" in matches:
            c = matches["contact_exact"]
            entities["contact_first_name"] = c["first_name"]
            entities["contact_last_name"] = c["last_name"]

        if dedup_info:
            # Gate on dedup confirmation before storing
            conversation_state[chat_id] = {
                "mode": "intake",
                "partial_entities": entities,
                "raw_message": user_text,
                "questions": [d["question"] for d in dedup_info],
                "dedup_info": dedup_info,
                "dedup_question_count": len(dedup_info),
                "answers": [],
                "account_context": db_context,
                "pending_confirmation": result.get("confirmation", "✓ Saved."),
            }
            await update.message.reply_text(
                format_questions("Quick dedup check before I save:", [d["question"] for d in dedup_info]),
                parse_mode="Markdown"
            )
        else:
            success = store_complete_interaction(entities, user_text)
            conversation_state.pop(chat_id, None)
            msg = result.get("confirmation", "✓ Saved.") if success else "⚠️ Had trouble saving — please try again."
            await update.message.reply_text(msg, parse_mode="Markdown")

    # ── DISAMBIGUATE ─────────────────────────────────────────────────────────
    elif intent == "DISAMBIGUATE":
        conversation_state[chat_id] = {
            "mode": "disambiguation",
            "original_message": user_text,
            "disambiguation_choices": result.get("choices", []),
            "disambiguation_field": result.get("field"),
        }
        choices_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(result.get("choices", [])))
        await update.message.reply_text(
            f"{result.get('question', 'Which one do you mean?')}\n\n{choices_text}",
            parse_mode="Markdown"
        )

    # ── QUERY ────────────────────────────────────────────────────────────────
    elif intent == "QUERY":
        await safe_reply(update, result.get("response", "No data found."))

    # ── TASK ─────────────────────────────────────────────────────────────────
    elif intent == "TASK":
        try:
            account_id = (
                find_or_create_account(result.get("account_name"))
                if result.get("account_name") else None
            )
            supabase.table("tasks").insert({
                "title": result.get("task_title", "Follow up"),
                "due_at": result.get("task_due_at"),
                "account_id": account_id,
            }).execute()
        except Exception as e:
            logger.error(f"Task save error: {e}")
        await update.message.reply_text(result.get("confirmation", "✓ Reminder saved."), parse_mode="Markdown")

    # ── DRAFT ─────────────────────────────────────────────────────────────────
    elif intent == "DRAFT":
        subject = result.get("subject", "")
        body = result.get("body", "")
        header = f"*Subject: {subject}*\n\n" if subject else ""
        await update.message.reply_text(f"📧 *Draft*\n\n{header}{body}", parse_mode="Markdown")

    # ── GENERAL ──────────────────────────────────────────────────────────────
    else:
        await safe_reply(update, result.get("response", "I'm not sure how to help with that."))


# ─────────────────────────────────────────────────────────────────────────────
# INTAKE ANSWER HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_intake_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, state: dict):
    """Process answers to intake + dedup questions, then finalize the store."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    if user_text.lower() in ["cancel", "stop", "nevermind", "never mind"]:
        conversation_state.pop(chat_id, None)
        await update.message.reply_text("Cancelled — nothing was saved.")
        return

    original = state.get("raw_message", "")
    partial = state.get("partial_entities", {})
    questions = state.get("questions", [])
    db_context = state.get("account_context", "")
    dedup_info = state.get("dedup_info", [])
    dedup_count = state.get("dedup_question_count", 0)

    # Split numbered answers if user formatted them (e.g., "1. yes\n2. Proposal")
    raw_answers = parse_numbered_answers(user_text)
    if len(raw_answers) < 2:
        raw_answers = [user_text]  # treat as single block

    dedup_answers = raw_answers[:dedup_count]
    intake_answers = raw_answers[dedup_count:] if len(raw_answers) > dedup_count else [user_text]

    # Resolve dedup and apply overrides
    overrides = resolve_dedup_answers(dedup_info, dedup_answers)
    resolved = {**partial, **overrides}

    # Build a clear dedup resolution context for the synthesis call
    dedup_context_lines = []
    for i, info in enumerate(dedup_info):
        ans = dedup_answers[i].strip() if i < len(dedup_answers) else "(no answer)"
        resolution = ""
        if overrides.get("account_name") and info.get("type") in ("account", "account_multi"):
            resolution = f"→ Use account name \"{overrides['account_name']}\""
        elif overrides.get("contact_first_name") and info.get("type") == "contact":
            resolution = f"→ Use existing contact \"{overrides.get('contact_first_name')} {overrides.get('contact_last_name', '')}\""
        else:
            resolution = f"→ Create as new ({info.get('new_name', '')})"
        dedup_context_lines.append(f"  Dedup Q: {info['question']}\n  Answer: {ans}\n  Resolution: {resolution}")

    dedup_context = "\n".join(dedup_context_lines)
    intake_answer_text = "\n".join(intake_answers)

    synthesis_prompt = (
        f"Original message: {original}\n\n"
        f"Partial data extracted: {json.dumps(resolved)}\n\n"
        f"Intake questions asked: {json.dumps(questions[dedup_count:])}\n\n"
        f"User's intake answers: {intake_answer_text}\n\n"
        + (f"Dedup resolutions (use these exact names when building STORE entities):\n{dedup_context}\n\n" if dedup_context else "")
        + "Synthesize all of this into a STORE response. "
          "Use the exact entity names from dedup resolutions. "
          "Leave fields null if user said 'n/a' or 'skip'. "
          "Confirmation should bullet-point the key details captured."
    )

    result = ask_claude(synthesis_prompt, db_context)

    # Allow one more intake round if Claude needs it
    if result.get("intent") == "INTAKE":
        conversation_state[chat_id] = {
            **state,
            "partial_entities": result.get("partial_entities", resolved),
            "questions": result.get("questions", []),
            "dedup_info": [],
            "dedup_question_count": 0,
            "answers": state.get("answers", []) + [user_text],
        }
        await update.message.reply_text(
            format_questions(result.get("intro", "Almost there — a couple more:"), result.get("questions", [])),
            parse_mode="Markdown"
        )
        return

    entities = result.get("entities", resolved)
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


# ─────────────────────────────────────────────────────────────────────────────
# BULK PASTE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_bulk_paste(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    """Handle a large paste containing multiple interactions/accounts/contacts."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    db_context = get_crm_context()
    result = ask_claude(
        user_text,
        db_context,
        extra_instructions=(
            "This is a bulk paste — the user is importing a large summary. "
            "Extract EVERY distinct interaction, account, contact, and opportunity. "
            "Return BULK intent with entities_list. Be thorough: capture every named "
            "person, company, deal, action item, stage, and value you can find. "
            "If there is only one interaction, use INTAKE instead."
        )
    )

    intent = result.get("intent", "GENERAL")

    # Claude may return INTAKE for a focused single entry
    if intent == "INTAKE":
        partial = result.get("partial_entities", {})
        dedup_info, matches = check_for_duplicates(partial)
        if "account_exact" in matches:
            partial["account_name"] = matches["account_exact"]["name"]
        all_questions = [d["question"] for d in dedup_info] + result.get("questions", [])[:3]
        conversation_state[chat_id] = {
            "mode": "intake",
            "partial_entities": partial,
            "raw_message": user_text,
            "questions": all_questions,
            "dedup_info": dedup_info,
            "dedup_question_count": len(dedup_info),
            "answers": [],
            "account_context": db_context,
        }
        await update.message.reply_text(
            format_questions(result.get("intro", "Got it — a few quick questions:"), all_questions),
            parse_mode="Markdown"
        )
        return

    if intent != "BULK":
        await update.message.reply_text(
            result.get("response", result.get("summary", "Processed.")),
            parse_mode="Markdown"
        )
        return

    entities_list = result.get("entities_list", [])
    if not entities_list:
        await update.message.reply_text(
            "Couldn't extract structured data from that paste. "
            "Try formatting it more explicitly — e.g., one entry per account.",
        )
        return

    # Run dedup on each entity, collect all dedup questions with entity_index
    all_dedup_info = []
    resolved_entities = []

    for idx, entities in enumerate(entities_list):
        dedup_info, matches = check_for_duplicates(entities, entity_index=idx)
        if "account_exact" in matches:
            entities["account_name"] = matches["account_exact"]["name"]
        if "contact_exact" in matches:
            c = matches["contact_exact"]
            entities["contact_first_name"] = c["first_name"]
            entities["contact_last_name"] = c["last_name"]
        resolved_entities.append(entities)
        all_dedup_info.extend(dedup_info)

    # Build preview
    lines = [f"📋 *Found {len(entities_list)} entries to import:*\n"]
    for i, e in enumerate(resolved_entities[:8], 1):
        acct = e.get("account_name", "Unknown Account")
        contact = " ".join(filter(None, [e.get("contact_first_name"), e.get("contact_last_name")]))
        itype = (e.get("interaction_type") or "note").capitalize()
        stage = e.get("opportunity_stage", "")
        val = f"${e.get('opportunity_value'):,.0f}" if e.get("opportunity_value") else ""

        line = f"*{i}.* {itype} — {acct}"
        if contact:
            line += f" ({contact})"
        details = " | ".join(filter(None, [stage, val]))
        if details:
            line += f" | {details}"
        lines.append(line)

    if len(resolved_entities) > 8:
        lines.append(f"_...and {len(resolved_entities) - 8} more_")

    if all_dedup_info:
        lines.append(f"\n⚠️ *{len(all_dedup_info)} duplicate check(s) needed before saving:*\n")
        for i, d in enumerate(all_dedup_info, 1):
            lines.append(f"*{i}.* {d['question']}")
        lines.append("\n_Answer the checks above, then I'll save everything._")
        lines.append("_Or type 'save all as new' to skip dedup and treat everything as new._")

        conversation_state[chat_id] = {
            "mode": "bulk_confirm",
            "bulk_entities": resolved_entities,
            "bulk_raw": user_text,
            "bulk_dedup_info": all_dedup_info,
            "bulk_dedup_pending": True,
        }
    else:
        lines.append(f"\n_No duplicates detected. Type *yes* to save all {len(resolved_entities)} entries, or *cancel* to abort._")

        conversation_state[chat_id] = {
            "mode": "bulk_confirm",
            "bulk_entities": resolved_entities,
            "bulk_raw": user_text,
            "bulk_dedup_info": [],
            "bulk_dedup_pending": False,
        }

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, state: dict):
    """Handle bulk mode — dedup resolution and final save confirmation."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    lower = user_text.lower().strip()

    if lower in ["cancel", "stop", "no"]:
        conversation_state.pop(chat_id, None)
        await update.message.reply_text("Cancelled — nothing was saved.")
        return

    entities_list = state.get("bulk_entities", [])
    bulk_dedup_info = state.get("bulk_dedup_info", [])

    # ── Dedup answer phase ───────────────────────────────────────────────────
    if state.get("bulk_dedup_pending") and bulk_dedup_info:
        if lower != "save all as new":
            raw_answers = parse_numbered_answers(user_text)
            if len(raw_answers) < 2:
                raw_answers = [user_text]

            for j, info in enumerate(bulk_dedup_info):
                if j >= len(raw_answers):
                    break
                answer = raw_answers[j].strip().lower()
                eidx = info.get("entity_index", 0)
                dtype = info.get("type", "")

                if dtype == "account" and answer in ("yes", "y"):
                    entities_list[eidx]["account_name"] = info["existing_name"]

                elif dtype == "account_multi" and answer not in ("new", "no", "n"):
                    candidates = info.get("candidates", [])
                    matched = next(
                        (c for c in candidates if c.lower() == answer),
                        next((c for c in candidates if answer in c.lower()), None)
                    )
                    if matched:
                        entities_list[eidx]["account_name"] = matched

                elif dtype == "contact" and answer in ("yes", "y"):
                    parts = info["existing_name"].split(" ", 1)
                    entities_list[eidx]["contact_first_name"] = parts[0]
                    entities_list[eidx]["contact_last_name"] = parts[1] if len(parts) > 1 else ""

        state["bulk_entities"] = entities_list
        state["bulk_dedup_pending"] = False
        conversation_state[chat_id] = state

        await update.message.reply_text(
            f"✓ Dedup resolved. Ready to save *{len(entities_list)} entries*.\n\n"
            "Type *yes* to confirm, or *cancel* to abort.",
            parse_mode="Markdown"
        )
        return

    # ── Final confirmation — save everything ─────────────────────────────────
    if lower in ["yes", "y", "save", "confirm", "go", "do it", "save all", "save all as new"]:
        saved = 0
        failed = 0
        for i, entities in enumerate(entities_list):
            raw = (
                f"Bulk import entry {i+1}: "
                f"{entities.get('account_name', '')} — "
                f"{entities.get('interaction_summary', '')}"
            )
            if store_complete_interaction(entities, raw):
                saved += 1
            else:
                failed += 1

        conversation_state.pop(chat_id, None)

        unique_accounts = len(set(e.get("account_name", "") for e in entities_list if e.get("account_name")))
        contacts_with_data = sum(
            1 for e in entities_list
            if e.get("contact_first_name") or e.get("contact_last_name")
        )
        opps_with_data = sum(1 for e in entities_list if e.get("opportunity_stage"))

        summary = (
            f"✅ *Bulk import complete!*\n\n"
            f"• {saved} interactions saved\n"
            f"• {unique_accounts} accounts processed\n"
            f"• {contacts_with_data} contacts processed\n"
            f"• {opps_with_data} opportunities updated\n"
        )
        if failed:
            summary += f"\n⚠️ {failed} entries failed — try re-entering those manually."

        await update.message.reply_text(summary, parse_mode="Markdown")

    else:
        await update.message.reply_text(
            "Type *yes* to save all entries, or *cancel* to abort.",
            parse_mode="Markdown"
        )


# ─────────────────────────────────────────────────────────────────────────────
# DISAMBIGUATION HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_disambiguation_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, choice: int, state: dict):
    """User picked a numbered option from a disambiguation prompt."""
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
        partial = result.get("partial_entities", {})
        dedup_info, matches = check_for_duplicates(partial)
        if "account_exact" in matches:
            partial["account_name"] = matches["account_exact"]["name"]
        all_questions = [d["question"] for d in dedup_info] + result.get("questions", [])[:3]
        conversation_state[chat_id] = {
            "mode": "intake",
            "partial_entities": partial,
            "raw_message": enriched,
            "questions": all_questions,
            "dedup_info": dedup_info,
            "dedup_question_count": len(dedup_info),
            "answers": [],
            "account_context": db_context,
        }
        await update.message.reply_text(
            format_questions(result.get("intro", "Got it:"), all_questions),
            parse_mode="Markdown"
        )
    elif result.get("intent") == "STORE":
        entities = result.get("entities", {})
        dedup_info, matches = check_for_duplicates(entities)
        if "account_exact" in matches:
            entities["account_name"] = matches["account_exact"]["name"]
        store_complete_interaction(entities, enriched)
        await update.message.reply_text(result.get("confirmation", "✓ Saved."), parse_mode="Markdown")
    else:
        await update.message.reply_text(result.get("response", "Done."), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
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
