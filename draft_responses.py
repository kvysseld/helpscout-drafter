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
  HELPSCOUT_MAILBOX_ID   - limit to one mailbox
  DRY_RUN                - set to "true" to print drafts without posting them
"""

import os
import sys
import json
import time
import logging
import html
import re
from datetime import datetime, timezone

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
# Customize this system prompt to match YOUR voice and your company's context.
# The more specific you are, the better the drafts will be.
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
"""

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
# Saved Replies
# ---------------------------------------------------------------------------
_saved_replies_cache: list[dict] = []


def list_mailboxes() -> None:
    """Log all available mailboxes so the user can find the right mailbox ID."""
    try:
        data = hs_get("/mailboxes")
        mailboxes = data.get("_embedded", {}).get("mailboxes", [])
        if mailboxes:
            log.info("Available mailboxes:")
            for mb in mailboxes:
                log.info(f"    ID: {mb.get('id')}  |  Name: \"{mb.get('name')}\"  |  Email: {mb.get('email', 'n/a')}")
            if not MAILBOX_ID:
                log.warning("  No HELPSCOUT_MAILBOX_ID set. Saved replies require a mailbox ID.")
    except Exception as e:
        log.warning(f"  Could not list mailboxes: {e}")


def load_saved_replies() -> list[dict]:
    """
    Fetch all saved replies for the configured mailbox.
    Returns list of {id, name, preview}.
    """
    global _saved_replies_cache
    if not MAILBOX_ID:
        log.info("  No MAILBOX_ID set, skipping saved replies.")
        return []

    try:
        data = hs_get(f"/mailboxes/{MAILBOX_ID}/saved-replies")
        # The response is a direct list, not embedded
        replies = data if isinstance(data, list) else data.get("_embedded", {}).get("saved-replies", data)
        if not isinstance(replies, list):
            replies = []
        _saved_replies_cache = replies
        log.info(f"Loaded {len(replies)} saved replies.")
        return replies
    except Exception as e:
        log.warning(f"  Could not load saved replies: {e}")
        return []


def get_saved_reply_full(reply_id: int) -> str:
    """Fetch the full HTML body of a saved reply and return as plain text."""
    if not MAILBOX_ID:
        return ""
    try:
        data = hs_get(f"/mailboxes/{MAILBOX_ID}/saved-replies/{reply_id}")
        html_text = data.get("text", "")
        return strip_html(html_text)
    except Exception as e:
        log.warning(f"  Could not fetch saved reply {reply_id}: {e}")
        return ""


def find_best_saved_reply(thread_history: str, saved_replies: list[dict]) -> dict | None:
    """
    Use Claude to pick the best saved reply for this conversation.
    Returns the saved reply dict if a good match is found, or None.
    """
    if not saved_replies:
        return None

    # Build a numbered list of saved reply names + previews for Claude
    reply_list = ""
    for i, r in enumerate(saved_replies):
        name = r.get("name", "Untitled")
        preview = r.get("preview", "")[:150]
        reply_list += f"{i+1}. \"{name}\" - {preview}\n"

    prompt = (
        f"Here is a customer support conversation:\n\n"
        f"{thread_history}\n\n"
        f"Below is a numbered list of saved replies available. Pick the ONE saved "
        f"reply that BEST answers the customer's latest question. If NONE of them "
        f"are a good fit, respond with just the word NONE.\n\n"
        f"Respond with ONLY the number of the best match, or NONE. Nothing else.\n\n"
        f"{reply_list}"
    )

    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        answer = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()

        if answer.upper() == "NONE":
            return None

        # Parse the number
        try:
            idx = int(answer.strip().rstrip(".")) - 1
            if 0 <= idx < len(saved_replies):
                match = saved_replies[idx]
                log.info(f"    Saved reply match: \"{match.get('name')}\"")
                return match
        except ValueError:
            pass

        return None
    except Exception as e:
        log.warning(f"  Error matching saved reply: {e}")
        return None


# ---------------------------------------------------------------------------
# Help Scout Docs API (separate API, uses Basic Auth with a Docs API key)
def list_docs_sites() -> None:
    """Log all available Docs sites so the user can find the right site ID."""
    if not HELPSCOUT_DOCS_API_KEY:
        return
    try:
        resp = requests.get(
            f"{DOCS_BASE}/sites",
            auth=(HELPSCOUT_DOCS_API_KEY, "X"),
        )
        if resp.ok:
            sites = resp.json().get("sites", {}).get("items", [])
            if sites:
                log.info("Available Docs sites:")
                for site in sites:
                    log.info(f"    ID: {site.get('id')}  |  Name: \"{site.get('title', 'Untitled')}\"  |  URL: {site.get('subDomain', 'n/a')}")
                if not HELPSCOUT_DOCS_SITE_ID:
                    log.warning("  No HELPSCOUT_DOCS_SITE_ID set. Searching ALL sites. Set it to limit to one product.")
    except Exception as e:
        log.warning(f"  Could not list Docs sites: {e}")


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
        data = resp.json()
        items = data.get("articles", {}).get("items", [])
        return items[:max_results]
    except Exception as e:
        log.warning(f"  Docs search error: {e}")
        return []


def get_doc_article(article_id: str) -> dict | None:
    """Fetch the full text of a Docs article by ID. Returns dict with name, url, text."""
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
        text = article.get("text", "")
        name = article.get("name", "Untitled")
        url = article.get("publicUrl", "")
        # Strip HTML from article body
        clean_text = strip_html(text)
        # Allow more content for richer context
        if len(clean_text) > 6000:
            clean_text = clean_text[:6000] + "\n...(article truncated)"
        return {"name": name, "url": url, "text": clean_text}
    except Exception as e:
        log.warning(f"  Error fetching article {article_id}: {e}")
        return None


def find_relevant_docs(subject: str, thread_history: str) -> str:
    """
    Search Help Scout Docs using the customer's latest message.
    Returns formatted article text to include in Claude's prompt.
    """
    if not HELPSCOUT_DOCS_API_KEY:
        log.info("  No Docs API key set, skipping docs lookup.")
        return ""

    # Extract the last customer message from the thread history
    # (thread history format: [CUSTOMER - Name - Date]\nmessage text)
    last_customer_msg = ""
    blocks = thread_history.split("\n\n---\n\n")
    for block in reversed(blocks):
        if block.strip().startswith("[CUSTOMER"):
            # Grab just the message body (skip the header line)
            lines = block.strip().split("\n", 1)
            if len(lines) > 1:
                last_customer_msg = lines[1].strip()
            break

    # Build search queries: prioritize the actual customer message
    search_results = []
    seen_ids: set = set()

    # Primary search: customer's latest message (first 150 chars, cleaned)
    if last_customer_msg:
        msg_query = re.sub(r"[^a-zA-Z0-9 ]", " ", last_customer_msg[:150]).strip()
        if msg_query:
            log.info(f"    Searching docs for: \"{msg_query[:80]}...\"")
            results = search_docs(msg_query)
            for item in results:
                if item["id"] not in seen_ids:
                    search_results.append(item)
                    seen_ids.add(item["id"])

    # Secondary search: subject line (if it looks meaningful, not "This is a test" etc.)
    subject_clean = re.sub(r"[^a-zA-Z0-9 ]", " ", subject).strip()
    generic_subjects = {"test", "this is a test", "help", "question", "hi", "hello", "hey"}
    if subject_clean.lower() not in generic_subjects and len(subject_clean) > 5:
        results = search_docs(subject_clean, max_results=2)
        for item in results:
            if item["id"] not in seen_ids:
                search_results.append(item)
                seen_ids.add(item["id"])

    if not search_results:
        log.info("  No relevant docs found.")
        return ""

    # Fetch full article text for top results
    articles = []
    for result in search_results[:3]:
        name = result.get("name", "Untitled")
        url = result.get("url", "no URL")
        log.info(f"    Doc match: \"{name}\" - {url}")
        article_data = get_doc_article(result["id"])
        if article_data:
            articles.append(article_data)

    if not articles:
        return ""

    log.info(f"  Using {len(articles)} help doc(s) for context.")

    # Format with URLs so Claude can link them in the response
    parts = []
    for art in articles:
        parts.append(
            f"ARTICLE: {art['name']}\n"
            f"URL: {art['url']}\n\n"
            f"{art['text']}"
        )
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def strip_html(text: str) -> str:
    """Rough HTML-to-plain-text conversion for thread bodies."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def get_my_user_id() -> int:
    """Get the user ID of the authenticated user (resource owner)."""
    data = hs_get("/users/me")
    user_id = data.get("id")
    log.info(f"Authenticated as: {data.get('firstName', '')} {data.get('lastName', '')} (ID: {user_id})")
    return user_id


def get_conversations_needing_reply() -> list[dict]:
    """
    Return all active conversations ASSIGNED TO ME where the customer
    is waiting on a response.
    """
    my_id = get_my_user_id()

    params: dict = {"status": "active"}
    if MAILBOX_ID:
        params["mailbox"] = MAILBOX_ID

    data = hs_get("/conversations", params)
    conversations = data.get("_embedded", {}).get("conversations", [])

    needs_reply = []
    for convo in conversations:
        # Only process conversations assigned to me
        assignee = convo.get("assignee")
        if not assignee or assignee.get("id") != my_id:
            continue

        # Check if customer is waiting
        if convo.get("customerWaitingSince", {}).get("time"):
            needs_reply.append(convo)

    return needs_reply


def get_thread_history(conversation_id: int) -> tuple[str, bool, bool]:
    """
    Fetch all threads for a conversation and format them for Claude.
    Returns (formatted_history, agent_has_replied_before, needs_new_draft).

    needs_new_draft is True if:
      - There is no AI-drafted note on this conversation, OR
      - The customer's latest message is newer than the last AI-drafted note
    """
    data = hs_get(f"/conversations/{conversation_id}/threads")
    threads = data.get("_embedded", {}).get("threads", [])

    # Sort oldest-first so the conversation reads chronologically
    threads.sort(key=lambda t: t.get("createdAt", ""))

    parts = []
    agent_has_replied = False
    last_customer_time = ""
    last_ai_note_time = ""

    for t in threads:
        thread_type = t.get("type", "unknown")
        timestamp = t.get("createdAt", "")

        # Track AI-drafted notes (but don't include them in history sent to Claude)
        if thread_type == "note":
            body = t.get("body", "")
            if "AI-DRAFTED RESPONSE" in body:
                last_ai_note_time = timestamp
            continue

        created_by = t.get("createdBy", {})
        author_type = created_by.get("type", "unknown")  # "customer" or "user"
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

    # Determine if we need a new draft
    if not last_ai_note_time:
        needs_new_draft = True  # No AI note exists yet
    elif last_customer_time > last_ai_note_time:
        needs_new_draft = True  # Customer replied after the last AI note
    else:
        needs_new_draft = False  # AI note is still current

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
    elif docs_context:
        user_message += (
            f"Here are relevant articles from our help documentation. Use the "
            f"article content as your PRIMARY source for answering. Pull specific "
            f"steps, details, and instructions directly from these docs.\n\n"
            f"--- HELP DOCS ---\n\n{docs_context}\n\n"
            f"--- END HELP DOCS ---\n\n"
            f"IMPORTANT instructions for using these docs:\n"
            f"- Base your answer on the doc content. Be detailed and specific.\n"
            f"- At the end of your response, link the SINGLE most relevant article "
            f"naturally, like: \"For more details, check out this guide: [Article Name](URL)\"\n"
            f"- Only link ONE article, whichever is most helpful for this specific question.\n"
            f"- Do NOT say things like 'according to our documentation' in the body "
            f"of your reply. Just use the knowledge naturally, then link at the end.\n\n"
        )

    user_message += (
        f"Please draft a reply to the customer's most recent message. "
        f"Write ONLY the reply body, nothing else."
    )

    resp = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        },
    )
    resp.raise_for_status()
    data = resp.json()

    # Extract text from content blocks
    return "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()


def post_note(conversation_id: int, text: str, source_label: str = "") -> None:
    """Post the draft as an internal note on the conversation."""
    source_line = f"<br><em>({source_label})</em>" if source_label else ""
    note_body = (
        f"<strong>🤖 AI-DRAFTED RESPONSE (review before sending):</strong>"
        f"{source_line}"
        f"<br><br>{text.replace(chr(10), '<br>')}"
    )
    hs_post(f"/conversations/{conversation_id}/notes", {"text": note_body})
    log.info(f"  Posted note on conversation {conversation_id}")



def run() -> None:
    log.info("Starting Help Scout auto-drafter...")
    if DRY_RUN:
        log.info("DRY RUN mode -- drafts will be printed, not posted.")

    # Log available mailboxes and Docs sites (helps with setup)
    list_mailboxes()
    list_docs_sites()

    # Load saved replies once at startup
    saved_replies = load_saved_replies()

    conversations = get_conversations_needing_reply()
    log.info(f"Found {len(conversations)} conversations needing a reply.")

    for convo in conversations:
        convo_id = convo["id"]
        subject = convo.get("subject", "(no subject)")
        log.info(f"Processing #{convo.get('number', '?')}: {subject}")

        # Extract customer first name from conversation data
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

            # STEP 1: Try to match a saved reply
            saved_reply_text = ""
            matched_reply = find_best_saved_reply(history, saved_replies)
            if matched_reply:
                log.info(f"  Using saved reply: \"{matched_reply.get('name')}\"")
                saved_reply_text = get_saved_reply_full(matched_reply["id"])

            # STEP 2: If no saved reply matched, search help docs
            docs_context = ""
            if not saved_reply_text:
                log.info(f"  No saved reply match, searching help docs...")
                docs_context = find_relevant_docs(subject, history)

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

            # Label the source in the note so you know what it was based on
            if saved_reply_text:
                source_label = f"Based on saved reply: \"{matched_reply.get('name', 'unknown')}\""
            elif docs_context:
                source_label = "Based on help docs"
            else:
                source_label = "Based on general knowledge"

            if DRY_RUN:
                print(f"\n{'='*60}")
                print(f"CONVERSATION #{convo.get('number')}: {subject}")
                print(f"[{source_label}]")
                print(f"{'='*60}")
                print(draft)
                print()
            else:
                post_note(convo_id, draft, source_label=source_label)

        except Exception as e:
            log.error(f"  Error processing conversation {convo_id}: {e}")
            continue

    log.info("Done.")


if __name__ == "__main__":
    run()
