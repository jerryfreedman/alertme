import os
import re
import json
import logging
from datetime import datetime, timedelta
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
# Configurable timezone — set USER_TIMEZONE env var to change (default: America/New_York)
TZ = pytz.timezone(os.environ.get("USER_TIMEZONE", "America/New_York"))

# Messages longer than this are treated as bulk pastes
BULK_THRESHOLD = 500
# Alert state is persisted in Supabase (events.alerted_30m/5m, tasks.alerted_at)
# No in-memory dedup needed — restarts are safe.

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
    "contact_email": "string or null",
    "contact_phone": "string or null",
    "interaction_type": "call|email|meeting|note",
    "interaction_date": "YYYY-MM-DD or null",
    "interaction_summary": "string or null",
    "next_steps": "string or null",
    "opportunity_name": "string or null",
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
IMPORTANT: Create ONE entity per distinct interaction event, not one per account.
If a company had a call on Monday and a meeting on Thursday, that is TWO entities.
{
  "intent": "BULK",
  "entities_list": [
    {
      "account_name": "string",
      "contact_first_name": "string or null",
      "contact_last_name": "string or null",
      "contact_title": "string or null",
      "contact_email": "string or null",
      "contact_phone": "string or null",
      "interaction_type": "call|email|meeting|note",
      "interaction_date": "YYYY-MM-DD or null",
      "interaction_summary": "string",
      "next_steps": "string or null",
      "opportunity_name": "string or null",
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

### EVENT — Scheduling a calendar event (call, meeting, demo, review)
Use when the user mentions a specific future time + who they're meeting.
{
  "intent": "EVENT",
  "event": {
    "title": "string",
    "start_at": "ISO8601 with timezone offset",
    "duration_minutes": number (default 30),
    "type": "call|meeting|demo|review|other",
    "account_name": "string or null",
    "contact_first_name": "string or null",
    "contact_last_name": "string or null",
    "location": "string or null",
    "notes": "string or null"
  },
  "confirmation": "📅 Scheduled: [title] — [day] at [time] ([duration] min). I'll brief you 30 min before."
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

Today's date: """ + datetime.now(TZ).strftime("%A, %B %d %Y") + """
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


def get_bulk_field_questions(entity: dict, idx: int) -> list[dict]:
    """
    Generate questions for the most important missing fields in a bulk entity.
    Returns at most 3 questions per entity, in priority order.
    Each dict: {question, type="field", entity_index, field_name}
    """
    questions = []
    acct = entity.get("account_name", f"Entry {idx + 1}")

    if not entity.get("opportunity_stage"):
        questions.append({
            "question": f"[{acct}] Deal stage? (prospecting / qualified / proposal / negotiation / closed_won)",
            "type": "field",
            "entity_index": idx,
            "field_name": "opportunity_stage",
        })
    if not entity.get("opportunity_value"):
        questions.append({
            "question": f"[{acct}] Any deal value or budget? (e.g. $50k — or n/a)",
            "type": "field",
            "entity_index": idx,
            "field_name": "opportunity_value",
        })
    if not entity.get("next_steps"):
        questions.append({
            "question": f"[{acct}] Next step and timing? (or n/a)",
            "type": "field",
            "entity_index": idx,
            "field_name": "next_steps",
        })
    contact_name = " ".join(filter(None, [
        entity.get("contact_first_name"), entity.get("contact_last_name")
    ]))
    if contact_name and not entity.get("contact_title"):
        questions.append({
            "question": f"[{acct}] What's {contact_name}'s title/role? (or n/a)",
            "type": "field",
            "entity_index": idx,
            "field_name": "contact_title",
        })
    return questions[:3]


def apply_field_answer(entity: dict, field_name: str, answer: str) -> dict:
    """
    Apply a user's free-text answer to the right field in an entity dict.
    Handles stage normalization, value parsing (50k → 50000), etc.
    """
    SKIP = {"n/a", "na", "skip", "none", "no", "unknown", "-", "not sure", "tbd", "?", ""}
    clean = answer.strip()
    if clean.lower() in SKIP:
        return entity

    if field_name == "opportunity_stage":
        stage_map = [
            ("prospect", "prospecting"),
            ("qualify", "qualified"),
            ("proposal", "proposal"),
            ("prop", "proposal"),
            ("negotiat", "negotiation"),
            ("closed_won", "closed_won"),
            ("closed won", "closed_won"),
            ("won", "closed_won"),
            ("win", "closed_won"),
            ("closed_lost", "closed_lost"),
            ("closed lost", "closed_lost"),
            ("lost", "closed_lost"),
        ]
        lower = clean.lower()
        matched = next((v for k, v in stage_map if k in lower), None)
        entity["opportunity_stage"] = matched or lower

    elif field_name == "opportunity_value":
        nums = re.findall(r"[\d]+\.?\d*", clean.replace(",", ""))
        if nums:
            num = float(nums[0])
            lower = clean.lower()
            # Check for multiplier suffix — be careful not to match "meeting"
            if re.search(r"\d\s*m(?:illion)?(?:\b|$)", lower):
                num *= 1_000_000
            elif re.search(r"\d\s*k(?:\b|$)", lower):
                num *= 1_000
            entity["opportunity_value"] = num

    elif field_name == "next_steps":
        entity["next_steps"] = clean
        # Auto-create a task if the answer looks like an action item
        action_words = ["follow up", "send", "call", "email", "schedule", "book", "prepare", "draft", "reach out"]
        if any(w in clean.lower() for w in action_words) and not entity.get("task_title"):
            entity["task_title"] = clean[:100]

    elif field_name == "contact_title":
        entity["contact_title"] = clean

    return entity


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


def find_or_create_contact(
    first: str,
    last: str,
    account_id: str = None,
    title: str = None,
    email: str = None,
    phone: str = None,
) -> str | None:
    """
    Find or create a contact, scoped to account_id when provided.
    - Searches within account first to prevent cross-account name collisions.
    - Enriches an existing contact with email/phone if they were missing.
    """
    if not first and not last:
        return None
    try:
        existing_id = None

        # Scoped search within account
        if account_id:
            q = supabase.table("contacts").select("id")
            if first:
                q = q.ilike("first_name", first)
            if last:
                q = q.ilike("last_name", last)
            scoped = q.eq("account_id", account_id).execute()
            if scoped.data:
                existing_id = scoped.data[0]["id"]

        # Global fallback if no account scope
        if not existing_id and not account_id:
            q = supabase.table("contacts").select("id")
            if first:
                q = q.ilike("first_name", first)
            if last:
                q = q.ilike("last_name", last)
            result = q.execute()
            if result.data:
                existing_id = result.data[0]["id"]

        if existing_id:
            # Enrich existing contact with new email/phone/title if we have them
            enrichment = {}
            if email:
                enrichment["email"] = email
            if phone:
                enrichment["phone"] = phone
            if title:
                enrichment["title"] = title
            if enrichment:
                supabase.table("contacts").update(enrichment).eq("id", existing_id).execute()
            return existing_id

        # Create new contact
        payload: dict = {
            "first_name": first or "",
            "last_name": last or "",
            "account_id": account_id,
        }
        if title:
            payload["title"] = title
        if email:
            payload["email"] = email
        if phone:
            payload["phone"] = phone
        new = supabase.table("contacts").insert(payload).execute()
        return new.data[0]["id"] if new.data else None

    except Exception as e:
        logger.error(f"Contact upsert error: {e}")
        return None


def find_or_update_opportunity(account_id: str, contact_id: str, entities: dict) -> str | None:
    """
    Find or create the right opportunity for this interaction.

    Matching priority:
    1. If opportunity_name provided → match by name within this account
    2. If only ONE open opp exists for this account → update it (safe)
    3. If MULTIPLE open opps exist and no name → log interaction without opp link
       (prevents silently overwriting the wrong deal)
    4. No open opps → create a new one with the provided name or a generated one
    """
    if not account_id:
        return None
    try:
        opp_name = (
            entities.get("opportunity_name")
            or f"{entities.get('account_name', 'Deal')} Opportunity"
        )
        update_fields = {}
        if entities.get("opportunity_stage"):
            update_fields["stage"] = entities["opportunity_stage"]
        if entities.get("opportunity_value"):
            update_fields["value"] = entities["opportunity_value"]
        if entities.get("opportunity_close_date"):
            update_fields["close_date"] = entities["opportunity_close_date"]

        # 1 — Exact name match within this account
        if entities.get("opportunity_name"):
            named = supabase.table("opportunities").select("id").eq(
                "account_id", account_id
            ).ilike("name", entities["opportunity_name"]).execute()
            if named.data:
                opp_id = named.data[0]["id"]
                if update_fields:
                    supabase.table("opportunities").update(update_fields).eq("id", opp_id).execute()
                return opp_id

        # 2/3 — Look at all open opportunities for this account
        open_opps = supabase.table("opportunities").select("id, name").eq(
            "account_id", account_id
        ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()

        if len(open_opps.data) == 1:
            # Exactly one open deal — safe to update
            opp_id = open_opps.data[0]["id"]
            if update_fields:
                supabase.table("opportunities").update(update_fields).eq("id", opp_id).execute()
            return opp_id

        if len(open_opps.data) > 1:
            # Multiple open deals and no name to disambiguate —
            # log the interaction without an opp link rather than corrupt the wrong deal
            logger.warning(
                f"Multiple open opps for account {account_id}, no opportunity_name given — "
                "interaction saved without opportunity link"
            )
            return None

        # 4 — No open opportunity → create one
        new = supabase.table("opportunities").insert({
            "name": opp_name,
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
            entities.get("contact_email"),
            entities.get("contact_phone"),
        )
        opportunity_id = find_or_update_opportunity(account_id, contact_id, entities)

        interaction_row = {
            "type": entities.get("interaction_type", "note"),
            "raw_text": raw_text,
            "summary": entities.get("interaction_summary", ""),
            "next_steps": entities.get("next_steps", ""),
            "account_id": account_id,
            "opportunity_id": opportunity_id,
            "contact_ids": [contact_id] if contact_id else [],
        }
        # Preserve the historical date when provided (e.g. from bulk import)
        if entities.get("interaction_date"):
            try:
                # Parse and store as timestamptz — keep time as midnight UTC if only date given
                interaction_row["created_at"] = entities["interaction_date"] + "T00:00:00+00:00"
            except Exception:
                pass  # fall through to default now()

        supabase.table("interactions").insert(interaction_row).execute()

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
# ─────────────────────────────────────────────────────────────────────────────
# CALENDAR HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def create_event_record(event_data: dict, account_id: str = None, contact_id: str = None, opportunity_id: str = None) -> str | None:
    """Insert a new calendar event into Supabase. Returns the event id."""
    try:
        row = {
            "title": event_data.get("title", "Untitled Event"),
            "start_at": event_data.get("start_at"),
            "duration_minutes": event_data.get("duration_minutes", 30),
            "type": event_data.get("type", "call"),
            "location": event_data.get("location"),
            "notes": event_data.get("notes"),
            "account_id": account_id,
            "opportunity_id": opportunity_id,
            "contact_ids": [contact_id] if contact_id else [],
        }
        # Compute end_at from start_at + duration
        if row["start_at"] and row["duration_minutes"]:
            try:
                start = datetime.fromisoformat(row["start_at"].replace("Z", "+00:00"))
                end = start + timedelta(minutes=row["duration_minutes"])
                row["end_at"] = end.isoformat()
            except Exception:
                pass
        result = supabase.table("events").insert(row).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        logger.error(f"Event create error: {e}")
        return None


def get_events_for_range(start: datetime, end: datetime) -> list[dict]:
    """Fetch events between start and end, ordered by start_at."""
    try:
        result = supabase.table("events").select(
            "id, title, start_at, end_at, duration_minutes, type, location, notes, "
            "account_id, accounts(name)"
        ).gte("start_at", start.isoformat()).lte("start_at", end.isoformat()).order("start_at").execute()
        return result.data or []
    except Exception as e:
        logger.error(f"Event fetch error: {e}")
        return []


def format_event_line(e: dict, now: datetime, show_date: bool = False) -> str:
    """Format a single event as a Telegram-ready line."""
    type_emoji = {"call": "📞", "meeting": "🤝", "demo": "🖥", "review": "📋"}.get(
        e.get("type", "call"), "📅"
    )
    acc = (e.get("accounts") or {}).get("name", "")
    dur = e.get("duration_minutes", 30)

    try:
        start = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).astimezone(TZ)
        time_str = start.strftime("%I:%M %p").lstrip("0")
        if show_date:
            time_str = start.strftime("%a %b %-d ") + time_str
    except Exception:
        time_str = "?"

    line = f"{type_emoji} *{time_str}* — {e['title']}"
    if acc:
        line += f" | {acc}"
    line += f" _({dur} min)_"
    if e.get("location"):
        line += f"\n   📍 {e['location']}"
    return line


async def generate_pre_meeting_brief(event: dict, app: Application) -> str:
    """
    Pull account + opportunity + interaction history for this event
    and ask Claude to generate a punchy pre-meeting brief.
    """
    try:
        acc_name = (event.get("accounts") or {}).get("name", "")
        db_ctx = get_crm_context(account_name=acc_name) if acc_name else get_crm_context()
        start = datetime.fromisoformat(event["start_at"].replace("Z", "+00:00")).astimezone(TZ)
        prompt = (
            f"I have a {event.get('type','call')} — \"{event['title']}\" — starting at "
            f"{start.strftime('%I:%M %p')} ({event.get('duration_minutes',30)} min). "
            "Write a punchy pre-meeting brief using the CRM context below. "
            "Include: deal status, last interaction summary, open tasks, key talking points, watch-outs. "
            "Max 10 lines. Use bullet points. No fluff."
        )
        result = ask_claude(prompt, db_ctx)
        return result.get("response", "No brief available — check account history manually.")
    except Exception as e:
        logger.error(f"Brief generation error: {e}")
        return ""


async def check_upcoming_events(app: Application):
    """
    Runs every 5 minutes. Uses DB columns (alerted_30m / alerted_5m) as the
    source of truth — restart-safe, no in-memory state needed.

    - 30 min before: sends full pre-meeting brief  → sets alerted_30m = true
    - 5 min before:  sends a quick nudge           → sets alerted_5m  = true
    """
    try:
        now = datetime.now(TZ)

        # ── 30-minute briefs ──────────────────────────────────────────────────
        # Events starting between 5 and 35 min from now that haven't been briefed
        win30_lo = (now + timedelta(minutes=5)).isoformat()
        win30_hi = (now + timedelta(minutes=35)).isoformat()
        events_30 = supabase.table("events").select(
            "id, title, start_at, duration_minutes, type, location, account_id, accounts(name)"
        ).eq("alerted_30m", False).gte("start_at", win30_lo).lte("start_at", win30_hi).execute()

        for e in (events_30.data or []):
            try:
                start = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).astimezone(TZ)
                mins_away = max(1, int((start - now).total_seconds() / 60))
            except Exception:
                continue

            acc      = (e.get("accounts") or {}).get("name", "")
            time_str = start.strftime("%I:%M %p").lstrip("0")

            brief  = await generate_pre_meeting_brief(e, app)
            header = (
                f"📅 *{e['title']}* in ~{mins_away} min ({time_str})\n"
                + (f"Account: {acc}\n" if acc else "")
                + (f"📍 {e['location']}\n" if e.get("location") else "")
            )
            msg = header + ("\n" + brief if brief else "")
            try:
                await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="Markdown")
            except Exception:
                await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=re.sub(r"[*_`]", "", msg))

            # Mark as alerted in DB — survives restarts
            supabase.table("events").update({"alerted_30m": True}).eq("id", e["id"]).execute()

        # ── 5-minute nudges ───────────────────────────────────────────────────
        # Events starting in the next 8 min that haven't been nudged
        win5_hi = (now + timedelta(minutes=8)).isoformat()
        events_5 = supabase.table("events").select(
            "id, title, start_at, accounts(name)"
        ).eq("alerted_5m", False).gte("start_at", now.isoformat()).lte("start_at", win5_hi).execute()

        for e in (events_5.data or []):
            try:
                start    = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).astimezone(TZ)
                mins_away = max(1, int((start - now).total_seconds() / 60))
            except Exception:
                continue

            acc   = (e.get("accounts") or {}).get("name", "")
            nudge = f"⏰ *{e['title']}* starts in ~{mins_away} min"
            if acc:
                nudge += f" — {acc}"
            try:
                await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=nudge, parse_mode="Markdown")
            except Exception:
                await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=re.sub(r"[*_`]", "", nudge))

            # Mark as nudged in DB
            supabase.table("events").update({"alerted_5m": True}).eq("id", e["id"]).execute()

    except Exception as e:
        logger.error(f"Event check error: {e}")


async def send_evening_digest(app: Application):
    """
    6pm evening digest:
    • What you logged today
    • Tasks still open (overdue + due today)
    • Tomorrow's full schedule with times
    • Prep reminder for tomorrow's first event
    """
    try:
        now = datetime.now(TZ)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        tomorrow_end = tomorrow_start + timedelta(days=1)

        sections = [f"🌆 *Evening Wrap — {now.strftime('%A, %B %d')}*\n"]

        # ── What you logged today ────────────────────────────────────────────
        logged = supabase.table("interactions").select(
            "type, summary, accounts(name)"
        ).gte("created_at", today_start.isoformat()).lte("created_at", now.isoformat()).execute()

        if logged.data:
            sections.append(f"📝 *Logged today ({len(logged.data)}):*")
            for i in logged.data[:6]:
                acc = (i.get("accounts") or {}).get("name", "")
                itype = (i.get("type") or "note").capitalize()
                summary = (i.get("summary") or "")[:60]
                line = f"  • {itype}"
                if acc:
                    line += f" — {acc}"
                if summary:
                    line += f": {summary}"
                sections.append(line)
        else:
            sections.append("📝 Nothing logged today yet")

        # ── Still-open tasks ─────────────────────────────────────────────────
        open_tasks = supabase.table("tasks").select(
            "title, due_at, accounts(name)"
        ).eq("completed", False).lte("due_at", now.isoformat() if True else "").execute()

        # Actually get overdue + due today that are still open
        still_open = supabase.table("tasks").select(
            "title, due_at, accounts(name)"
        ).eq("completed", False).lt("due_at", tomorrow_start.isoformat()).execute()

        if still_open.data:
            sections.append(f"\n⚠️ *Still open ({len(still_open.data)}):*")
            for t in still_open.data[:5]:
                acc = (t.get("accounts") or {}).get("name", "")
                overdue = t.get("due_at", "") < now.isoformat()
                flag = "🔴" if overdue else "🟡"
                line = f"  {flag} {t['title']}"
                if acc:
                    line += f" — {acc}"
                sections.append(line)

        # ── Tomorrow's schedule ──────────────────────────────────────────────
        tomorrow_events = get_events_for_range(tomorrow_start, tomorrow_end)
        tomorrow_tasks = supabase.table("tasks").select(
            "title, due_at, accounts(name)"
        ).eq("completed", False).gte("due_at", tomorrow_start.isoformat()).lt(
            "due_at", tomorrow_end.isoformat()
        ).order("due_at").execute()

        if tomorrow_events or (tomorrow_tasks.data):
            sections.append(f"\n📅 *Tomorrow — {tomorrow_start.strftime('%A, %B %d')}:*")

            for e in tomorrow_events:
                sections.append("  " + format_event_line(e, now))

            for t in (tomorrow_tasks.data or [])[:4]:
                acc = (t.get("accounts") or {}).get("name", "")
                time_str = ""
                if t.get("due_at"):
                    try:
                        dt = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone(TZ)
                        time_str = f" by {dt.strftime('%I:%M %p').lstrip('0')}"
                    except Exception:
                        pass
                line = f"  ✅ {t['title']}{time_str}"
                if acc:
                    line += f" — {acc}"
                sections.append(line)
        else:
            sections.append("\n📅 *Tomorrow:* Nothing scheduled yet")

        msg = "\n".join(sections)
        if len(msg) > 4000:
            msg = msg[:3997] + "…"

        await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Evening digest error: {e}")
        try:
            await app.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text="⚠️ Evening digest failed — check Railway logs."
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# SLASH COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/done [keyword] — mark a task complete by title keyword, or list tasks if no keyword given."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    keyword = " ".join(context.args).strip() if context.args else ""

    try:
        pending = supabase.table("tasks").select(
            "id, title, due_at, accounts(name)"
        ).eq("completed", False).order("due_at").execute()

        if not pending.data:
            await update.message.reply_text("✅ No pending tasks — nothing to mark done.")
            return

        if not keyword:
            lines = ["Which task did you complete? Reply with the number:\n"]
            for i, t in enumerate(pending.data[:10], 1):
                acc = (t.get("accounts") or {}).get("name", "")
                due_raw = t.get("due_at", "")
                due_str = ""
                if due_raw:
                    try:
                        dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).astimezone(TZ)
                        due_str = f" | {dt.strftime('%b %d')}"
                    except Exception:
                        pass
                line = f"*{i}.* {t['title']}"
                if acc:
                    line += f" — {acc}"
                line += due_str
                lines.append(line)
            lines.append("\n_Or: /done [keyword] to match by title_")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return

        # Try keyword match first
        if keyword.isdigit():
            idx = int(keyword) - 1
            if 0 <= idx < len(pending.data):
                task = pending.data[idx]
            else:
                await update.message.reply_text(f"No task #{keyword} — run /done to see the list.")
                return
        else:
            # Fuzzy title match
            kw_lower = keyword.lower()
            task = next(
                (t for t in pending.data if kw_lower in t["title"].lower()),
                None
            )
            if not task:
                await update.message.reply_text(
                    f'No pending task matching "{keyword}". Run /done to see the full list.'
                )
                return

        supabase.table("tasks").update({"completed": True}).eq("id", task["id"]).execute()
        alerted_task_ids.discard(task["id"])  # clear alert tracking
        await update.message.reply_text(f"✅ *Done:* {task['title']}", parse_mode="Markdown")

    except Exception as e:
        logger.error(f"/done error: {e}")
        await update.message.reply_text("Couldn't mark that task done — try again.")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/today — today's full schedule: events in time order + tasks due today."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    now = datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    events = get_events_for_range(day_start, day_end)
    try:
        tasks = supabase.table("tasks").select(
            "title, due_at, accounts(name)"
        ).eq("completed", False).gte("due_at", day_start.isoformat()).lt(
            "due_at", day_end.isoformat()
        ).order("due_at").execute()
        task_data = tasks.data or []
    except Exception:
        task_data = []

    if not events and not task_data:
        await update.message.reply_text(
            f"📅 *{now.strftime('%A, %B %d')}* — Nothing scheduled today.\n\n"
            "_Add an event: \"Schedule a call with Sarah at Acme tomorrow at 2pm\"_",
            parse_mode="Markdown"
        )
        return

    lines = [f"📅 *{now.strftime('%A, %B %d')}*\n"]

    # Merge events and tasks into a single time-sorted list
    items = []
    for e in events:
        try:
            t = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).astimezone(TZ)
            items.append(("event", t, e))
        except Exception:
            pass
    for t in task_data:
        try:
            dt = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone(TZ)
            items.append(("task", dt, t))
        except Exception:
            pass
    items.sort(key=lambda x: x[1])

    past, upcoming = [], []
    for kind, dt, item in items:
        (past if dt < now else upcoming).append((kind, dt, item))

    if past:
        lines.append("*Earlier today:*")
        for kind, dt, item in past:
            if kind == "event":
                lines.append("  ✓ " + format_event_line(item, now).replace("*", ""))
            else:
                acc = (item.get("accounts") or {}).get("name", "")
                lines.append(f"  ✓ {item['title']}" + (f" — {acc}" if acc else ""))
        lines.append("")

    if upcoming:
        lines.append("*Coming up:*")
        for kind, dt, item in upcoming:
            if kind == "event":
                lines.append("  " + format_event_line(item, now))
            else:
                acc = (item.get("accounts") or {}).get("name", "")
                time_s = dt.strftime("%I:%M %p").lstrip("0")
                lines.append(f"  ✅ *{time_s}* — {item['title']}" + (f" | {acc}" if acc else ""))

    await safe_reply(update, "\n".join(lines))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/week — this week's calendar: events and tasks grouped by day."""
    if update.effective_chat.id != ALLOWED_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    now = datetime.now(TZ)
    # Show Mon–Sun of current week (or next 7 days if preferred)
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    events = get_events_for_range(week_start, week_end)
    try:
        tasks = supabase.table("tasks").select(
            "title, due_at, accounts(name)"
        ).eq("completed", False).gte("due_at", week_start.isoformat()).lt(
            "due_at", week_end.isoformat()
        ).order("due_at").execute()
        task_data = tasks.data or []
    except Exception:
        task_data = []

    if not events and not task_data:
        await update.message.reply_text(
            "📆 Nothing on the calendar for the next 7 days.\n\n"
            "_Add an event: \"Schedule a call with John at Globex Thursday at 3pm\"_",
            parse_mode="Markdown"
        )
        return

    # Group by day
    from collections import defaultdict
    by_day: dict[str, list] = defaultdict(list)

    for e in events:
        try:
            dt = datetime.fromisoformat(e["start_at"].replace("Z", "+00:00")).astimezone(TZ)
            day_key = dt.strftime("%Y-%m-%d")
            by_day[day_key].append(("event", dt, e))
        except Exception:
            pass
    for t in task_data:
        try:
            dt = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone(TZ)
            day_key = dt.strftime("%Y-%m-%d")
            by_day[day_key].append(("task", dt, t))
        except Exception:
            pass

    lines = ["📆 *Next 7 Days*\n"]
    for day_key in sorted(by_day.keys()):
        items = sorted(by_day[day_key], key=lambda x: x[1])
        day_dt = datetime.fromisoformat(day_key)
        is_today = day_dt.date() == now.date()
        day_label = ("*Today*" if is_today else f"*{day_dt.strftime('%A, %b %-d')}*")
        lines.append(day_label)
        for kind, dt, item in items:
            if kind == "event":
                lines.append("  " + format_event_line(item, now))
            else:
                acc = (item.get("accounts") or {}).get("name", "")
                time_s = dt.strftime("%I:%M %p").lstrip("0")
                lines.append(f"  ✅ *{time_s}* — {item['title']}" + (f" | {acc}" if acc else ""))
        lines.append("")

    await safe_reply(update, "\n".join(lines))


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

        now = datetime.now(TZ)
        lines = ["📋 *Pending Tasks*\n"]
        for t in result.data:
            acc = (t.get("accounts") or {}).get("name", "")
            due_raw = t.get("due_at") or ""
            due_str = ""
            if due_raw:
                try:
                    due_dt = datetime.fromisoformat(due_raw.replace("Z", "+00:00")).astimezone(TZ)
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
    """
    7am daily briefing — structured direct from Supabase, no Claude call needed.
    Sections: overdue tasks → due today → closing this week → stale deals → pipeline total.
    """
    try:
        now = datetime.now(TZ)
        today_iso = now.date().isoformat()
        eod_iso = now.replace(hour=23, minute=59, second=59).isoformat()
        seven_days_iso = (now + timedelta(days=7)).date().isoformat()
        fourteen_days_ago_iso = (now - timedelta(days=14)).isoformat()

        sections = [f"☀️ *Good morning — {now.strftime('%A, %B %d')}*\n"]

        # ── 1. Overdue tasks ─────────────────────────────────────────────────
        overdue = supabase.table("tasks").select(
            "id, title, due_at, accounts(name)"
        ).eq("completed", False).lt("due_at", now.isoformat()).order("due_at").execute()

        if overdue.data:
            sections.append(f"🔴 *Overdue ({len(overdue.data)}):*")
            for t in overdue.data[:6]:
                acc = (t.get("accounts") or {}).get("name", "")
                try:
                    dt = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone(TZ)
                    age = f"since {dt.strftime('%b %d')}"
                except Exception:
                    age = ""
                line = f"  • {t['title']}"
                if acc:
                    line += f" — {acc}"
                if age:
                    line += f" _({age})_"
                sections.append(line)

        # ── 2. Due today ─────────────────────────────────────────────────────
        due_today = supabase.table("tasks").select(
            "id, title, due_at, accounts(name)"
        ).eq("completed", False).gte("due_at", now.isoformat()).lte("due_at", eod_iso).order("due_at").execute()

        if due_today.data:
            sections.append(f"\n🟡 *Due Today ({len(due_today.data)}):*")
            for t in due_today.data[:6]:
                acc = (t.get("accounts") or {}).get("name", "")
                time_str = ""
                if t.get("due_at"):
                    try:
                        dt = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone(TZ)
                        time_str = f" at {dt.strftime('%I:%M %p')}"
                    except Exception:
                        pass
                line = f"  • {t['title']}{time_str}"
                if acc:
                    line += f" — {acc}"
                sections.append(line)

        if not overdue.data and not due_today.data:
            sections.append("✅ No tasks due today — clear calendar")

        # ── 3. Deals closing within 7 days ───────────────────────────────────
        closing = supabase.table("opportunities").select(
            "name, stage, value, close_date, accounts(name)"
        ).not_.in_("stage", ["closed_won", "closed_lost"]).gte(
            "close_date", today_iso
        ).lte("close_date", seven_days_iso).order("close_date").execute()

        if closing.data:
            sections.append(f"\n🗓 *Closing This Week ({len(closing.data)}):*")
            for o in closing.data:
                acc = (o.get("accounts") or {}).get("name", "?")
                days_left = ""
                if o.get("close_date"):
                    try:
                        d = (datetime.fromisoformat(o["close_date"]) - now.replace(tzinfo=None)).days + 1
                        days_left = f" ({d}d)"
                    except Exception:
                        pass
                val = f" | ${o.get('value'):,.0f}" if o.get("value") else ""
                urgency = "🔴" if "1" in days_left or "2" in days_left else "🟡"
                sections.append(f"  {urgency} {acc}{days_left}{val}")

        # ── 4. Stale open deals (no interaction in 14+ days) ─────────────────
        recent_interactions = supabase.table("interactions").select(
            "account_id"
        ).gte("created_at", fourteen_days_ago_iso).execute()
        active_ids = {i["account_id"] for i in (recent_interactions.data or []) if i.get("account_id")}

        stale = supabase.table("opportunities").select(
            "account_id, stage, accounts(name)"
        ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()
        stale_opps = [o for o in (stale.data or []) if o.get("account_id") not in active_ids]

        if stale_opps:
            sections.append(f"\n🕸 *Gone Quiet — 14+ days no activity ({len(stale_opps)}):*")
            for o in stale_opps[:5]:
                acc = (o.get("accounts") or {}).get("name", "?")
                stage = (o.get("stage") or "").replace("_", " ").title()
                sections.append(f"  • {acc} — {stage}")

        # ── 5. Today's calendar events ───────────────────────────────────────
        todays_events = get_events_for_range(now, now.replace(hour=23, minute=59))
        if todays_events:
            sections.append(f"\n📅 *Today's Schedule ({len(todays_events)} events):*")
            for e in todays_events:
                sections.append("  " + format_event_line(e, now))

        # ── 6. Pipeline snapshot ─────────────────────────────────────────────
        all_open = supabase.table("opportunities").select(
            "value, probability"
        ).not_.in_("stage", ["closed_won", "closed_lost"]).execute()

        if all_open.data:
            total = sum(o.get("value") or 0 for o in all_open.data)
            weighted = sum(
                (o.get("value") or 0) * ((o.get("probability") or 0) / 100)
                for o in all_open.data
            )
            sections.append(
                f"\n📊 *Pipeline:* {len(all_open.data)} open deals | "
                f"${total:,.0f} total | ${weighted:,.0f} weighted"
            )

        msg = "\n".join(sections)
        if len(msg) > 4000:
            msg = msg[:3997] + "…"

        await app.bot.send_message(chat_id=ALLOWED_CHAT_ID, text=msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Briefing error: {e}")
        try:
            await app.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text="⚠️ Daily briefing failed — check Railway logs."
            )
        except Exception:
            pass


async def check_due_tasks(app: Application):
    """
    Runs every 15 minutes. Alerts on tasks due within 60 min or overdue within 15 min.
    Uses tasks.alerted_at (persisted in DB) — fully restart-safe, no in-memory state.
    """
    try:
        now          = datetime.now(TZ)
        window_start = (now - timedelta(minutes=15)).isoformat()   # catch recently-overdue
        window_end   = (now + timedelta(minutes=60)).isoformat()   # look 60 min ahead

        # Only fetch tasks that haven't been alerted yet (alerted_at IS NULL)
        tasks = supabase.table("tasks").select(
            "id, title, due_at, accounts(name)"
        ).eq("completed", False).is_("alerted_at", "null").gte(
            "due_at", window_start
        ).lte("due_at", window_end).execute()

        for t in (tasks.data or []):
            acc = (t.get("accounts") or {}).get("name", "")
            try:
                due_dt = datetime.fromisoformat(t["due_at"].replace("Z", "+00:00")).astimezone(TZ)
                delta  = (due_dt - now).total_seconds()
                if delta < 0:
                    timing = "🔴 *OVERDUE*"
                elif delta < 3600:
                    mins   = max(1, int(delta / 60))
                    timing = f"⏰ *Due in {mins} min*"
                else:
                    timing = f"⏰ *Due at {due_dt.strftime('%I:%M %p').lstrip('0')}*"
            except Exception:
                timing = "⏰ *Due soon*"

            lines = [f"{timing} — {t['title']}"]
            if acc:
                lines.append(f"Account: {acc}")

            try:
                await app.bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text="\n".join(lines),
                    parse_mode="Markdown"
                )
            except Exception:
                await app.bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=re.sub(r"[*_`]", "", "\n".join(lines))
                )

            # Stamp alerted_at in DB — prevents re-alert on restart
            supabase.table("tasks").update(
                {"alerted_at": now.isoformat()}
            ).eq("id", t["id"]).execute()

    except Exception as e:
        logger.error(f"Due task check error: {e}")


async def check_close_dates(app: Application):
    """
    Runs daily at 8am. Alerts on open deals whose close_date is within 7 days.
    Skips if no deals are closing soon (silent if nothing to report).
    """
    try:
        now = datetime.now(TZ)
        today_iso = now.date().isoformat()
        seven_days_iso = (now + timedelta(days=7)).date().isoformat()

        opps = supabase.table("opportunities").select(
            "name, stage, value, close_date, accounts(name)"
        ).not_.in_("stage", ["closed_won", "closed_lost"]).gte(
            "close_date", today_iso
        ).lte("close_date", seven_days_iso).order("close_date").execute()

        if not opps.data:
            return

        lines = ["🗓 *Deals closing within 7 days:*\n"]
        for o in opps.data:
            acc = (o.get("accounts") or {}).get("name", "?")
            stage = (o.get("stage") or "").replace("_", " ").title()
            val = f"${o.get('value'):,.0f}" if o.get("value") else ""
            days_left = ""
            urgency = "🟡"
            if o.get("close_date"):
                try:
                    d = (datetime.fromisoformat(o["close_date"]) - now.replace(tzinfo=None)).days + 1
                    days_left = f"({d}d left)"
                    if d <= 2:
                        urgency = "🔴"
                except Exception:
                    pass
            detail = " | ".join(filter(None, [stage, val, days_left]))
            lines.append(f"{urgency} *{acc}* — {o.get('name', 'Deal')}")
            if detail:
                lines.append(f"   {detail}")

        await app.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text="\n".join(lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Close date check error: {e}")


async def check_stale_deals(app: Application):
    """
    Runs daily at 9:00am. Proactively pushes any open deal that has had
    zero interactions in the past 14 days — separate from the morning briefing
    so it arrives as its own actionable alert.
    """
    try:
        now              = datetime.now(TZ)
        fourteen_ago     = (now - timedelta(days=14)).isoformat()
        open_stages      = ["prospecting", "qualified", "proposal", "negotiation"]

        opps = supabase.table("opportunities").select(
            "id, name, stage, value, account_id, accounts(name)"
        ).in_("stage", open_stages).execute()

        if not opps.data:
            return

        stale = []
        for o in opps.data:
            # Check whether any interaction exists for this account in the last 14 days
            recent = supabase.table("interactions").select("id").eq(
                "account_id", o["account_id"]
            ).gte("created_at", fourteen_ago).limit(1).execute()

            if not recent.data:
                stale.append(o)

        if not stale:
            return

        lines = [f"😴 *Stale deals — no activity in 14+ days:*\n"]
        for o in stale:
            acc   = (o.get("accounts") or {}).get("name", "?")
            stage = (o.get("stage") or "").replace("_", " ").title()
            val   = f"${o.get('value'):,.0f}" if o.get("value") else ""
            detail = " | ".join(filter(None, [stage, val]))
            lines.append(f"🔇 *{acc}* — {o.get('name', 'Deal')}")
            if detail:
                lines.append(f"   {detail}")

        lines.append("\nLog an interaction or update the stage to keep your pipeline clean.")

        try:
            await app.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text="\n".join(lines),
                parse_mode="Markdown"
            )
        except Exception:
            await app.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=re.sub(r"[*_`]", "", "\n".join(lines))
            )
    except Exception as e:
        logger.error(f"Stale deal check error: {e}")


async def reset_event_alert_flags(app: Application):
    """
    Runs at midnight. Resets alerted_30m and alerted_5m for all events
    that started more than 6 hours ago — so recurring events (daily standups,
    weekly calls) will alert again the next time they appear.
    """
    try:
        cutoff = (datetime.now(TZ) - timedelta(hours=6)).isoformat()
        supabase.table("events").update(
            {"alerted_30m": False, "alerted_5m": False}
        ).lt("start_at", cutoff).execute()
        logger.info("Midnight: reset event alert flags for past events.")
    except Exception as e:
        logger.error(f"Event flag reset error: {e}")


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

    # ── EVENT ─────────────────────────────────────────────────────────────────
    elif intent == "EVENT":
        ev = result.get("event", {})
        account_id = find_or_create_account(ev.get("account_name")) if ev.get("account_name") else None
        contact_id = find_or_create_contact(
            ev.get("contact_first_name"),
            ev.get("contact_last_name"),
            account_id,
        ) if ev.get("contact_first_name") or ev.get("contact_last_name") else None
        eid = create_event_record(ev, account_id=account_id, contact_id=contact_id)
        msg = result.get("confirmation", "📅 Event scheduled.")
        if not eid:
            msg = "⚠️ Couldn't save the event — please try again."
        await update.message.reply_text(msg, parse_mode="Markdown")

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

    # ── Per-entity: dedup check + missing field questions ───────────────────
    resolved_entities = []
    all_questions = []  # flat list of all question dicts (dedup + field), ordered

    for idx, entities in enumerate(entities_list):
        dedup_info, matches = check_for_duplicates(entities, entity_index=idx)

        # Silent exact-match normalization
        if "account_exact" in matches:
            entities["account_name"] = matches["account_exact"]["name"]
        if "contact_exact" in matches:
            c = matches["contact_exact"]
            entities["contact_first_name"] = c["first_name"]
            entities["contact_last_name"] = c["last_name"]

        resolved_entities.append(entities)
        all_questions.extend(dedup_info)                    # dedup first
        all_questions.extend(get_bulk_field_questions(entities, idx))  # then fields

    # ── Build the message ────────────────────────────────────────────────────
    lines = [f"📋 *Found {len(entities_list)} entries to import:*\n"]
    for i, e in enumerate(resolved_entities[:8], 1):
        acct = e.get("account_name", "Unknown Account")
        contact = " ".join(filter(None, [e.get("contact_first_name"), e.get("contact_last_name")]))
        itype = (e.get("interaction_type") or "note").capitalize()
        stage = e.get("opportunity_stage", "")
        val = f"${e.get('opportunity_value'):,.0f}" if e.get("opportunity_value") else ""
        missing_flags = []
        if not e.get("opportunity_stage"):   missing_flags.append("stage?")
        if not e.get("opportunity_value"):   missing_flags.append("value?")
        if not e.get("next_steps"):          missing_flags.append("next step?")

        line = f"*{i}.* {itype} — {acct}"
        if contact:
            line += f" ({contact})"
        details = " | ".join(filter(None, [stage, val]))
        if details:
            line += f" | {details}"
        if missing_flags:
            line += f"  _[missing: {', '.join(missing_flags)}]_"
        lines.append(line)

    if len(resolved_entities) > 8:
        lines.append(f"_...and {len(resolved_entities) - 8} more_")

    if all_questions:
        dedup_qs = [q for q in all_questions if q.get("type") != "field"]
        field_qs = [q for q in all_questions if q.get("type") == "field"]

        if dedup_qs:
            lines.append(f"\n⚠️ *Duplicate checks ({len(dedup_qs)}):*")
        if field_qs:
            lines.append(f"\n📝 *Missing info ({len(field_qs)} questions):*")

        lines.append("")
        q_num = 1
        for q in all_questions:
            lines.append(f"*{q_num}.* {q['question']}")
            q_num += 1

        lines.append("")
        lines.append("_Answer all by number. Type 'n/a' to skip any._")
        lines.append("_Or type 'save all as new' to skip dedup checks only._")

        conversation_state[chat_id] = {
            "mode": "bulk_confirm",
            "bulk_entities": resolved_entities,
            "bulk_raw": user_text,
            "bulk_all_questions": all_questions,
            "bulk_phase": "qa",
        }
    else:
        lines.append(f"\n✅ _All fields complete. Type *yes* to save {len(resolved_entities)} entries, or *cancel* to abort._")
        conversation_state[chat_id] = {
            "mode": "bulk_confirm",
            "bulk_entities": resolved_entities,
            "bulk_raw": user_text,
            "bulk_all_questions": [],
            "bulk_phase": "confirm",
        }

    await safe_reply(update, "\n".join(lines))


async def handle_bulk_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str, state: dict):
    """
    Two-phase bulk confirmation:
    Phase 'qa'      — user answers dedup + missing-field questions
    Phase 'confirm' — user types yes to save everything
    """
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    lower = user_text.lower().strip()

    if lower in ["cancel", "stop", "no"]:
        conversation_state.pop(chat_id, None)
        await update.message.reply_text("Cancelled — nothing was saved.")
        return

    entities_list = state.get("bulk_entities", [])
    all_questions = state.get("bulk_all_questions", [])
    phase = state.get("bulk_phase", "confirm")

    # ── Phase 1: QA — resolve dedup + fill missing fields ───────────────────
    if phase == "qa":
        skip_dedup = lower == "save all as new"
        raw_answers = parse_numbered_answers(user_text)
        if len(raw_answers) < 2:
            raw_answers = [user_text]

        for j, q_info in enumerate(all_questions):
            answer = raw_answers[j].strip() if j < len(raw_answers) else ""
            eidx = q_info.get("entity_index", 0)
            qtype = q_info.get("type", "")

            if qtype == "field":
                entities_list[eidx] = apply_field_answer(
                    entities_list[eidx], q_info["field_name"], answer
                )
            elif not skip_dedup:
                # Dedup resolution
                ans_lower = answer.lower()
                if qtype == "account" and ans_lower in ("yes", "y"):
                    entities_list[eidx]["account_name"] = q_info["existing_name"]
                elif qtype == "account_multi" and ans_lower not in ("new", "no", "n"):
                    candidates = q_info.get("candidates", [])
                    matched = next(
                        (c for c in candidates if c.lower() == ans_lower),
                        next((c for c in candidates if ans_lower in c.lower()), None)
                    )
                    if matched:
                        entities_list[eidx]["account_name"] = matched
                elif qtype == "contact" and ans_lower in ("yes", "y"):
                    parts = q_info["existing_name"].split(" ", 1)
                    entities_list[eidx]["contact_first_name"] = parts[0]
                    entities_list[eidx]["contact_last_name"] = parts[1] if len(parts) > 1 else ""

        state["bulk_entities"] = entities_list
        state["bulk_phase"] = "confirm"
        conversation_state[chat_id] = state

        # Build final resolved summary for the user to review
        summary_lines = [f"✅ *Resolved — here's what I'm about to save:*\n"]
        for i, e in enumerate(entities_list[:10], 1):
            acct = e.get("account_name", "Unknown")
            contact = " ".join(filter(None, [e.get("contact_first_name"), e.get("contact_last_name")]))
            itype = (e.get("interaction_type") or "note").capitalize()
            stage = (e.get("opportunity_stage") or "").replace("_", " ").title()
            val = f"${e.get('opportunity_value'):,.0f}" if e.get("opportunity_value") else ""
            nxt = (e.get("next_steps") or "")[:50]

            line = f"*{i}.* {itype} — *{acct}*"
            if contact:
                line += f" ({contact})"
            if e.get("contact_title"):
                line += f", {e['contact_title']}"
            details = " | ".join(filter(None, [stage, val]))
            if details:
                line += f"\n   {details}"
            if nxt:
                line += f"\n   Next: {nxt}"
            summary_lines.append(line)

        if len(entities_list) > 10:
            summary_lines.append(f"_...and {len(entities_list) - 10} more_")

        summary_lines.append(f"\nType *yes* to save all {len(entities_list)} entries, or *cancel* to abort.")
        await safe_reply(update, "\n".join(summary_lines))
        return

    # ── Phase 2: Final save ──────────────────────────────────────────────────
    if lower in ["yes", "y", "save", "confirm", "go", "do it", "save all", "save all as new"]:
        saved = 0
        failed = 0
        for i, entities in enumerate(entities_list):
            raw = (
                f"Bulk import entry {i + 1}: "
                f"{entities.get('account_name', '')} — "
                f"{entities.get('interaction_summary', '')}"
            )
            if store_complete_interaction(entities, raw):
                saved += 1
            else:
                failed += 1

        conversation_state.pop(chat_id, None)

        unique_accounts = len(set(e.get("account_name", "") for e in entities_list if e.get("account_name")))
        contacts_created = sum(1 for e in entities_list if e.get("contact_first_name") or e.get("contact_last_name"))
        opps_set = sum(1 for e in entities_list if e.get("opportunity_stage"))
        tasks_set = sum(1 for e in entities_list if e.get("task_title"))

        result_lines = [
            "✅ *Bulk import complete!*\n",
            f"• {saved} interactions saved",
            f"• {unique_accounts} accounts processed",
            f"• {contacts_created} contacts processed",
            f"• {opps_set} opportunities updated",
        ]
        if tasks_set:
            result_lines.append(f"• {tasks_set} tasks created")
        if failed:
            result_lines.append(f"\n⚠️ {failed} entries failed — try re-entering those manually.")

        await safe_reply(update, "\n".join(result_lines))

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
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def post_init(application):
        # 7:00am — full daily briefing (includes today's events)
        scheduler.add_job(
            send_daily_briefing, "cron",
            hour=DAILY_BRIEFING_HOUR, minute=0, timezone=TZ,
            args=[application], id="daily_briefing", replace_existing=True,
        )
        # 7:15am — deal close-date warnings
        scheduler.add_job(
            check_close_dates, "cron",
            hour=DAILY_BRIEFING_HOUR, minute=15, timezone=TZ,
            args=[application], id="close_date_check", replace_existing=True,
        )
        # 6:00pm — evening digest + tomorrow's schedule
        scheduler.add_job(
            send_evening_digest, "cron",
            hour=18, minute=0, timezone=TZ,
            args=[application], id="evening_digest", replace_existing=True,
        )
        # Every 15 min — task due-date alerts (DB-backed, restart-safe)
        scheduler.add_job(
            check_due_tasks, "interval",
            minutes=15, args=[application],
            id="task_due_check", replace_existing=True,
        )
        # Every 5 min — pre-meeting event briefs (DB-backed, restart-safe)
        scheduler.add_job(
            check_upcoming_events, "interval",
            minutes=5, args=[application],
            id="event_check", replace_existing=True,
        )
        # 9:00am daily — proactive stale deal push
        scheduler.add_job(
            check_stale_deals, "cron",
            hour=9, minute=0, timezone=TZ,
            args=[application], id="stale_deal_check", replace_existing=True,
        )
        # Midnight — reset event alert flags so recurring events re-alert tomorrow
        scheduler.add_job(
            reset_event_alert_flags, "cron",
            hour=0, minute=1, timezone=TZ,
            args=[application], id="event_flag_reset", replace_existing=True,
        )
        scheduler.start()
        logger.info(
            f"SalesFlow started. TZ={TZ}. Briefing {DAILY_BRIEFING_HOUR}:00, "
            "stale-deal check 9:00, evening digest 18:00. "
            "Task alerts every 15 min, event alerts every 5 min (DB-backed, restart-safe)."
        )

    app.post_init = post_init
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
