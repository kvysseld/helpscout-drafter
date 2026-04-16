#!/usr/bin/env python3
"""
Help Scout Auto-Drafter
-----------------------
Scans your Help Scout inbox for conversations awaiting a reply,
sends each thread history to Claude, and posts the drafted response
back as an internal NOTE (yellow, never seen by the customer).

Run on a schedule (cron, GitHub Action, etc.) so drafts are waiting
for you each morning.

Required env vars:
  HELPSCOUT_APP_ID       - from Help Scout > Your Profile > My Apps
  HELPSCOUT_APP_SECRET   - same place
  ANTHROPIC_API_KEY      - from console.anthropic.com

Optional env vars:
  HELPSCOUT_DOCS_API_KEY - from Help Scout > Your Profile > Authentication > API Keys
  HELPSCOUT_DOCS_SITE_ID - limit docs search to one site (e.g. Nucleus 2)
  HELPSCOUT_MAILBOX_ID   - required for saved replies
  DRY_RUN                - set to "true" to print drafts without posting them
"""

import os
import time
import logging
import html
import re

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HELPSCOUT_APP_ID = os.environ["HELPSCOUT_APP_ID"]
HELPSCOUT_APP_SECRET = os.environ["HELPSCOUT_APP_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
HELPSCOUT_DOCS_API_KEY = os.environ.get("HELPSCOUT_DOCS_API_KEY", "")
HELPSCOUT_DOCS_SITE_ID = os.environ.get("HELPSCOUT_DOCS_SITE_ID", "")
MAILBOX_ID = os.environ.get("HELPSCOUT_MAILBOX_ID", "")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Conversations with these subject lines (case-insensitive) will be skipped
SKIP_SUBJECTS = [
    "makeover application submitted",
]

HS_BASE = "https://api.helpscout.net/v2"
DOCS_BASE = "https://docsapi.helpscout.net/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-20250514"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("hs-drafter")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are Kyle, a support agent for Nucleus, a web design and hosting platform \
built specifically for churches. You genuinely care about helping church staff \
succeed online.

=== NUCLEUS 5 PILLARS OF CUSTOMER SUCCESS ===

1. ANSWER THE QUESTION BENEATH THE QUESTION
   Don't stop at the surface-level question. Think about what the customer is \
   really trying to accomplish and what they'll need to know next. Anticipate \
   natural follow-up questions and address them proactively. Be curious about \
   what else might come up.

2. MEET THEM WHERE THEY ARE
   Many customers are newer to the platform. Remember what it's like to explore \
   something for the first time and engage from that perspective. Never assume \
   they know where a setting is or what a term means. With frustrated customers, \
   validate their feelings and make them feel heard before jumping into solutions.

3. GET CREATIVE WITH SOLUTIONS
   Before responding, explore multiple approaches to the problem. Let curiosity \
   guide you into solving things with ingenuity. If the obvious answer doesn't \
   fully help, think around the corner.

4. BRING THE ENERGY
   Be genuinely friendly, positive, and thorough. Enthusiasm shows up in how \
   carefully you explain things, how willing you are to go the extra step, and \
   how you make the customer feel valued. Church staff deal with a lot. Be a \
   bright spot in their day.

5. BE HELPFUL, NOT JUST CORRECT
   A technically true answer that leaves the customer confused is not helpful. \
   Give clear, specific instructions. Reduce confusion. Minimize unnecessary \
   back-and-forth. If you reference a setting or page, tell them exactly how \
   to get there.

=== RESPONSE GUIDELINES ===

- FIRST RESPONSE to a customer: Always open with \
  "Hey [customer first name], Kyle here 👋 Project Manager for Makeovers" \
  (the customer's name will be provided to you). Then continue with your reply.
- FOLLOW-UP RESPONSES (you've already replied before in this thread): Skip the \
  full intro. Just use a casual greeting like "Hey [name]!" or jump right in.
- ANSWER FIRST. After the greeting, lead with the direct answer to their \
  question. Don't bury it behind context-setting, restating their question, or \
  pleasantries like "Great question!". Get to the answer in the first sentence or two.
- If there's a helpful next step, mention it after the answer (e.g., "and if \
  you run into issues, here's where to look").
- If the issue is technical (DNS, domains, email, site editor, migrations), give \
  clear step-by-step guidance. Number your steps. Be specific about where to \
  click and what to look for.
- If you are unsure or the issue needs internal investigation, say so honestly. \
  Never guess or make up features. Say something like "Let me dig into this a \
  bit more and get back to you" rather than fabricating an answer.
- Sign off warmly but casually: "Let me know if you have any other questions!" \
  or "Happy to help with anything else!" or similar.
- Do NOT include a subject line. Do NOT use "Dear customer" or overly formal \
  greetings. The customer's name is already visible in Help Scout.
- Keep it under 200 words unless the situation genuinely requires more detail. \
  Thoroughness matters, but so does respecting their time.
- Never use the phrase "I understand your frustration" robotically. If a customer \
  is upset, acknowledge their specific situation in your own words.
- Use plain language. Avoid jargon unless the customer used it first.
- FORMATTING: Write in plain text only. Do NOT use markdown formatting like \
  **bold**, *italics*, or ### headers. Just write naturally. For numbered steps, \
  use plain numbers (1. 2. 3.) without any bold or special formatting.
"""

# ---------------------------------------------------------------------------
# Anthropic API helper with retry logic
# ---------------------------------------------------------------------------
def call_claude(messages: list[dict], system: str = "", max_tokens: int = 1024) -> str:
    """Call the Anthropic API with automatic retry on 429 rate limit errors."""
    payload: dict = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    for attempt in range(5):
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            log.warning(f"  Rate limited, waiting {wait}s (attempt {attempt+1}/5)...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()

    log.error("  Exhausted all retries for Anthropic API.")
    return ""


# ---------------------------------------------------------------------------
# Help Scout auth + helpers
# ---------------------------------------------------------------------------
_token_cache: dict = {}


def get_hs_token() -> str:
    """Get or refresh a Help Scout OAuth2 token (Client Credentials flow)."""
    now = time.time()
    if _token_cache.get("token") and _token_cache.get("expires_at", 0) > now + 60:
        return _token_cache["token"]

    log.info("Fetching new Help Scout access token...")
    resp = requests.post(
        f"{HS_BASE}/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": HELPSCOUT_APP_ID,
            "client_secret": HELPSCOUT_APP_SECRET,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 7200)
    return _token_cache["token"]


def hs_get(path: str, params: dict | None = None) -> dict:
    """Authenticated GET against the Help Scout API."""
    token = get_hs_token()
    resp = requests.get(
        f"{HS_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
    )
    if not resp.ok:
        log.error(f"Help Scout API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    return resp.json()


def hs_post(path: str, payload: dict) -> requests.Response:
    """Authenticated POST against the Help Scout API."""
    token = get_hs_token()
    resp = requests.post(
        f"{HS_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def strip_html(text: str) -> str:
    """Rough HTML-to-plain-text conversion."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def extract_last_customer_message(thread_history: str) -> str:
    """Pull the most recent customer message from formatted thread history."""
    blocks = thread_history.split("\n\n---\n\n")
    for block in reversed(blocks):
        if block.strip().startswith("[CUSTOMER"):
            lines = block.strip().split("\n", 1)
            if len(lines) > 1:
                return lines[1].strip()
    return ""


def strip_markdown(text: str) -> str:
    """Remove common markdown formatting from text."""
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    # Italic: *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # Headers: ### text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text


def parse_confidence_and_draft(raw: str) -> tuple[str, str]:
    """
    Parse Claude's response into (confidence_label, draft_body).
    Also strips any markdown formatting.
    """
    confidence = ""
    draft = raw

    for tag in ["[READY TO SEND]", "[LIGHT EDIT]", "[NEEDS ATTENTION]"]:
        if raw.strip().startswith(tag):
            confidence = tag.strip("[]")
            draft = raw.strip()[len(tag):].strip()
            break

    draft = strip_markdown(draft)
    return confidence, draft


# ---------------------------------------------------------------------------
# Saved Replies
# ---------------------------------------------------------------------------
def load_saved_replies() -> list[dict]:
    """Fetch all saved replies for the configured mailbox."""
    if not MAILBOX_ID:
        log.info("  No MAILBOX_ID set, skipping saved replies.")
        return []
    try:
        data = hs_get(f"/mailboxes/{MAILBOX_ID}/saved-replies")
        replies = data if isinstance(data, list) else data.get("_embedded", {}).get("saved-replies", data)
        if not isinstance(replies, list):
            replies = []
        log.info(f"Loaded {len(replies)} saved replies.")
        return replies
    except Exception as e:
        log.warning(f"  Could not load saved replies: {e}")
        return []


def get_saved_reply_full(reply_id: int) -> str:
    """Fetch the full body of a saved reply as plain text."""
    if not MAILBOX_ID:
        return ""
    try:
        data = hs_get(f"/mailboxes/{MAILBOX_ID}/saved-replies/{reply_id}")
        return strip_html(data.get("text", ""))
    except Exception as e:
        log.warning(f"  Could not fetch saved reply {reply_id}: {e}")
        return ""


def find_best_saved_reply(customer_message: str, saved_replies: list[dict]) -> dict | None:
    """Use Claude to pick the best saved reply for the customer's question."""
    if not saved_replies or not customer_message:
        return None

    reply_list = "\n".join(f"{i+1}. {r.get('name', 'Untitled')}" for i, r in enumerate(saved_replies))

    prompt = (
        f"Customer's question:\n{customer_message[:500]}\n\n"
        f"Pick the ONE saved reply that BEST answers this question. "
        f"If NONE fit, respond NONE. Respond with ONLY the number or NONE.\n\n"
        f"{reply_list}"
    )

    try:
        answer = call_claude(messages=[{"role": "user", "content": prompt}], max_tokens=20)
        if not answer or answer.upper() == "NONE":
            return None
        idx = int(answer.strip().rstrip(".")) - 1
        if 0 <= idx < len(saved_replies):
            match = saved_replies[idx]
            log.info(f"    Saved reply match: \"{match.get('name')}\"")
            return match
    except (ValueError, Exception) as e:
        log.warning(f"  Error matching saved reply: {e}")

    return None


# ---------------------------------------------------------------------------
# Help Scout Docs API
# ---------------------------------------------------------------------------
def search_docs(query: str, max_results: int = 3) -> list[dict]:
    """Search Help Scout Docs for articles matching the query."""
    if not HELPSCOUT_DOCS_API_KEY:
        return []
    try:
        params: dict = {"query": query, "status": "published", "visibility": "public"}
        if HELPSCOUT_DOCS_SITE_ID:
            params["siteId"] = HELPSCOUT_DOCS_SITE_ID
        resp = requests.get(
            f"{DOCS_BASE}/search/articles",
            params=params,
            auth=(HELPSCOUT_DOCS_API_KEY, "X"),
        )
        if not resp.ok:
            log.warning(f"  Docs search failed ({resp.status_code}): {resp.text[:200]}")
            return []
        items = resp.json().get("articles", {}).get("items", [])
        return items[:max_results]
    except Exception as e:
        log.warning(f"  Docs search error: {e}")
        return []


def get_doc_article(article_id: str) -> dict | None:
    """Fetch the full text of a Docs article by ID."""
    if not HELPSCOUT_DOCS_API_KEY:
        return None
    try:
        resp = requests.get(
            f"{DOCS_BASE}/articles/{article_id}",
            auth=(HELPSCOUT_DOCS_API_KEY, "X"),
        )
        if not resp.ok:
            return None
        article = resp.json().get("article", {})
        clean_text = strip_html(article.get("text", ""))
        if len(clean_text) > 6000:
            clean_text = clean_text[:6000] + "\n...(article truncated)"
        return {
            "name": article.get("name", "Untitled"),
            "url": article.get("publicUrl", ""),
            "text": clean_text,
        }
    except Exception as e:
        log.warning(f"  Error fetching article {article_id}: {e}")
        return None


def find_relevant_docs(customer_message: str, subject: str) -> str:
    """Search Help Scout Docs and return formatted article context."""
    if not HELPSCOUT_DOCS_API_KEY:
        log.info("  No Docs API key set, skipping docs lookup.")
        return ""

    search_results = []
    seen_ids: set = set()

    # Primary search: customer's actual message
    if customer_message:
        msg_query = re.sub(r"[^a-zA-Z0-9 ]", " ", customer_message[:150]).strip()
        if msg_query:
            log.info(f"    Searching docs for: \"{msg_query[:80]}...\"")
            for item in search_docs(msg_query):
                if item["id"] not in seen_ids:
                    search_results.append(item)
                    seen_ids.add(item["id"])

    # Secondary search: subject line (if meaningful)
    subject_clean = re.sub(r"[^a-zA-Z0-9 ]", " ", subject).strip()
    generic_subjects = {"test", "this is a test", "help", "question", "hi", "hello", "hey"}
    if subject_clean.lower() not in generic_subjects and len(subject_clean) > 5:
        for item in search_docs(subject_clean, max_results=2):
            if item["id"] not in seen_ids:
                search_results.append(item)
                seen_ids.add(item["id"])

    if not search_results:
        log.info("  No relevant docs found.")
        return ""

    # Fetch the single best article (first result is highest relevance)
    best = search_results[0]
    log.info(f"    Best doc match: \"{best.get('name', 'Untitled')}\" - {best.get('url', '')}")
    article = get_doc_article(best["id"])
    if not article:
        return ""

    log.info(f"  Using help doc for context.")
    return f"ARTICLE: {article['name']}\nURL: {article['url']}\n\n{article['text']}"


# ---------------------------------------------------------------------------
# Conversation processing
# ---------------------------------------------------------------------------
def get_my_user_id() -> int:
    """Get the user ID of the authenticated user."""
    data = hs_get("/users/me")
    user_id = data.get("id")
    log.info(f"Authenticated as: {data.get('firstName', '')} {data.get('lastName', '')} (ID: {user_id})")
    return user_id


def get_conversations_needing_reply() -> list[dict]:
    """Return all active conversations assigned to me where the customer is waiting."""
    my_id = get_my_user_id()

    params: dict = {"status": "active"}
    if MAILBOX_ID:
        params["mailbox"] = MAILBOX_ID

    # Paginate through all pages of results
    all_conversations = []
    page = 1
    while True:
        params["page"] = page
        data = hs_get("/conversations", params)
        conversations = data.get("_embedded", {}).get("conversations", [])
        all_conversations.extend(conversations)

        total_pages = data.get("page", {}).get("totalPages", 1)
        if page >= total_pages:
            break
        page += 1

    needs_reply = []
    for convo in all_conversations:
        assignee = convo.get("assignee")
        if not assignee or assignee.get("id") != my_id:
            continue
        if convo.get("customerWaitingSince", {}).get("time"):
            needs_reply.append(convo)

    return needs_reply


def get_thread_history(conversation_id: int) -> tuple[str, bool, bool]:
    """
    Fetch all threads for a conversation.
    Returns (formatted_history, agent_has_replied_before, needs_new_draft).
    """
    data = hs_get(f"/conversations/{conversation_id}/threads")
    threads = data.get("_embedded", {}).get("threads", [])
    threads.sort(key=lambda t: t.get("createdAt", ""))

    parts = []
    agent_has_replied = False
    last_customer_time = ""
    last_ai_note_time = ""

    for t in threads:
        thread_type = t.get("type", "unknown")
        timestamp = t.get("createdAt", "")

        if thread_type == "note":
            body = t.get("body", "")
            # Check multiple markers to reliably detect our own AI drafts
            is_ai_draft = (
                "auto-drafted" in body
                or "AI-DRAFTED RESPONSE" in body
                or "🟢 Ready to send" in body
                or "🟡 Light edit" in body
                or "🔴 Needs attention" in body
                or "🤖 AI Draft" in body
            )
            if is_ai_draft:
                last_ai_note_time = timestamp
            continue

        created_by = t.get("createdBy", {})
        author_type = created_by.get("type", "unknown")
        author_name = f'{created_by.get("first", "")} {created_by.get("last", "")}'.strip()
        body = strip_html(t.get("body", ""))

        if not body:
            continue

        if author_type == "user":
            agent_has_replied = True
        elif author_type == "customer":
            last_customer_time = timestamp

        label = "CUSTOMER" if author_type == "customer" else "AGENT"
        parts.append(f"[{label} - {author_name} - {timestamp}]\n{body}")

    if not last_ai_note_time:
        needs_new_draft = True
    elif last_customer_time > last_ai_note_time:
        needs_new_draft = True
    else:
        needs_new_draft = False

    return "\n\n---\n\n".join(parts), agent_has_replied, needs_new_draft


def draft_reply_with_claude(
    subject: str,
    thread_history: str,
    customer_name: str = "",
    is_first_reply: bool = True,
    saved_reply_text: str = "",
    docs_context: str = "",
) -> str:
    """Send the conversation to Claude and get a draft reply."""
    user_message = (
        f"Here is a Help Scout support conversation. The subject line is: \"{subject}\"\n"
        f"Customer's first name: {customer_name or 'unknown'}\n"
        f"Is this your first reply in this thread? {'YES' if is_first_reply else 'NO'}\n\n"
        f"--- CONVERSATION HISTORY ---\n\n{thread_history}\n\n"
        f"--- END ---\n\n"
    )

    if saved_reply_text:
        user_message += (
            f"We have a SAVED REPLY that matches this customer's question. Use it "
            f"as the foundation for your response. Personalize it for this specific "
            f"conversation: adapt the tone, fill in any placeholders, add the customer's "
            f"name, and adjust details to match what the customer actually asked. "
            f"Do NOT copy it word-for-word. Make it feel natural and personal.\n\n"
            f"--- SAVED REPLY ---\n\n{saved_reply_text}\n\n"
            f"--- END SAVED REPLY ---\n\n"
        )

    if docs_context:
        if saved_reply_text:
            # Docs supplement the saved reply
            user_message += (
                f"Here is also a relevant help doc article for additional context. "
                f"Use it to fill in details or answer parts of the customer's message "
                f"that the saved reply doesn't cover.\n\n"
                f"--- HELP DOC ---\n\n{docs_context}\n\n"
                f"--- END HELP DOC ---\n\n"
                f"If the help doc adds useful info beyond the saved reply, link it at "
                f"the end like: \"For more details, check out this guide: [Article Name](URL)\"\n\n"
            )
        else:
            # Docs are the only source
            user_message += (
                f"Here is a relevant article from our help documentation. Use it as your "
                f"PRIMARY source for answering. Pull specific steps, details, and "
                f"instructions directly from this doc.\n\n"
                f"--- HELP DOC ---\n\n{docs_context}\n\n"
                f"--- END HELP DOC ---\n\n"
                f"IMPORTANT:\n"
                f"- Base your answer on the doc content. Be detailed and specific.\n"
                f"- At the end of your response, link the article naturally, like: "
                f"\"For more details, check out this guide: [Article Name](URL)\"\n"
                f"- Do NOT say 'according to our documentation' in the body of your reply.\n\n"
            )

    user_message += (
        f"Please draft a reply to the customer's most recent message.\n\n"
        f"FORMAT: Start your response with a confidence tag on its own line, then "
        f"a blank line, then the reply body. The tag must be one of:\n"
        f"  [READY TO SEND] - you are confident this draft is accurate and complete\n"
        f"  [LIGHT EDIT] - mostly good but Kyle should tweak a detail or two\n"
        f"  [NEEDS ATTENTION] - you are unsure about the answer or the question is complex\n\n"
        f"Example format:\n"
        f"[READY TO SEND]\n\n"
        f"Hey Sarah, Kyle here 👋 Project Manager for Makeovers\n\n"
        f"Here's how to do that...\n\n"
        f"Write ONLY the confidence tag and reply body, nothing else."
    )

    return call_claude(
        messages=[{"role": "user", "content": user_message}],
        system=SYSTEM_PROMPT,
        max_tokens=1024,
    )


def post_note(conversation_id: int, text: str, confidence: str = "", source_label: str = "") -> None:
    """Post the draft as an internal note on the conversation."""
    # Build a compact header line with emoji indicators
    if confidence == "READY TO SEND":
        badge = "🟢 Ready to send"
    elif confidence == "LIGHT EDIT":
        badge = "🟡 Light edit needed"
    elif confidence == "NEEDS ATTENTION":
        badge = "🔴 Needs attention"
    else:
        badge = "🤖 AI Draft"

    source_part = f" | <em>{source_label}</em>" if source_label else ""
    note_body = (
        f"<strong>{badge}</strong>{source_part}"
        f"<br><br>{text.replace(chr(10), '<br>')}"
        f"<br><br><em style='color:#999;font-size:11px;'>[auto-drafted]</em>"
    )
    hs_post(f"/conversations/{conversation_id}/notes", {"text": note_body})
    log.info(f"  Posted note on conversation {conversation_id}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run() -> None:
    log.info("Starting Help Scout auto-drafter...")
    if DRY_RUN:
        log.info("DRY RUN mode -- drafts will be printed, not posted.")

    saved_replies = load_saved_replies()
    conversations = get_conversations_needing_reply()
    log.info(f"Found {len(conversations)} conversations needing a reply.")

    for convo in conversations:
        convo_id = convo["id"]
        subject = convo.get("subject", "(no subject)")
        log.info(f"Processing #{convo.get('number', '?')}: {subject}")

        # Skip automated/form conversations
        if subject.lower().strip() in SKIP_SUBJECTS:
            log.info(f"  Skipping (subject is in skip list).")
            continue

        primary = convo.get("primaryCustomer", {}) or convo.get("createdBy", {})
        customer_name = primary.get("first", "") or ""

        try:
            history, agent_has_replied, needs_new_draft = get_thread_history(convo_id)
            if not history:
                log.warning(f"  No usable threads found, skipping.")
                continue

            if not needs_new_draft:
                log.info(f"  Already has an up-to-date AI draft, skipping.")
                continue

            # Extract customer's latest message once for reuse
            customer_msg = extract_last_customer_message(history)

            # STEP 1: Try to match a saved reply
            saved_reply_text = ""
            matched_reply = find_best_saved_reply(customer_msg, saved_replies)
            if matched_reply:
                log.info(f"  Using saved reply: \"{matched_reply.get('name')}\"")
                saved_reply_text = get_saved_reply_full(matched_reply["id"])

            # STEP 2: Always search help docs for additional context
            docs_context = find_relevant_docs(customer_msg, subject)

            draft = draft_reply_with_claude(
                subject,
                history,
                customer_name=customer_name,
                is_first_reply=not agent_has_replied,
                saved_reply_text=saved_reply_text,
                docs_context=docs_context,
            )
            if not draft:
                log.warning(f"  Claude returned empty draft, skipping.")
                continue

            # Parse confidence label from draft
            confidence, draft_body = parse_confidence_and_draft(draft)
            if confidence:
                log.info(f"  Confidence: {confidence}")

            # Label the source in the note
            if saved_reply_text and docs_context:
                source_label = f"Saved reply: \"{matched_reply.get('name', 'unknown')}\" + help docs"
            elif saved_reply_text:
                source_label = f"Saved reply: \"{matched_reply.get('name', 'unknown')}\""
            elif docs_context:
                source_label = "Help docs"
            else:
                source_label = "General knowledge"

            if DRY_RUN:
                print(f"\n{'='*60}")
                print(f"CONVERSATION #{convo.get('number')}: {subject}")
                print(f"[{confidence or 'no label'}] [{source_label}]")
                print(f"{'='*60}")
                print(draft_body)
                print()
            else:
                post_note(convo_id, draft_body, confidence=confidence, source_label=source_label)

        except Exception as e:
            log.error(f"  Error processing conversation {convo_id}: {e}")
            continue

        # Delay between conversations to avoid rate limits
        time.sleep(10)

    log.info("Done.")


if __name__ == "__main__":
    run()
