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

Optional env vars:
  DRAFT_TAG              - tag added to conversations after drafting (default: "ai-drafted")
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
MAILBOX_ID = os.environ.get("HELPSCOUT_MAILBOX_ID", "")
DRAFT_TAG = os.environ.get("DRAFT_TAG", "ai-drafted")
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


def hs_put(path: str, payload: dict) -> requests.Response:
    """Authenticated PUT against the Help Scout API."""
    token = get_hs_token()
    resp = requests.put(
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
# Help Scout Docs API (separate API, uses Basic Auth with a Docs API key)
# ---------------------------------------------------------------------------
def search_docs(query: str, max_results: int = 3) -> list[dict]:
    """Search Help Scout Docs for articles matching the query."""
    if not HELPSCOUT_DOCS_API_KEY:
        return []

    try:
        resp = requests.get(
            f"{DOCS_BASE}/search/articles",
            params={"query": query, "status": "published"},
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


def get_doc_article(article_id: str) -> str:
    """Fetch the full text of a Docs article by ID."""
    if not HELPSCOUT_DOCS_API_KEY:
        return ""

    try:
        resp = requests.get(
            f"{DOCS_BASE}/articles/{article_id}",
            auth=(HELPSCOUT_DOCS_API_KEY, "X"),
        )
        if not resp.ok:
            return ""
        article = resp.json().get("article", {})
        text = article.get("text", "")
        name = article.get("name", "Untitled")
        # Strip HTML from article body
        clean_text = strip_html(text)
        # Truncate very long articles to keep token usage reasonable
        if len(clean_text) > 3000:
            clean_text = clean_text[:3000] + "\n...(article truncated)"
        return f"ARTICLE: {name}\n\n{clean_text}"
    except Exception as e:
        log.warning(f"  Error fetching article {article_id}: {e}")
        return ""


def find_relevant_docs(subject: str, thread_history: str) -> str:
    """
    Search Help Scout Docs using keywords from the conversation.
    Returns formatted article text to include in Claude's prompt.
    """
    if not HELPSCOUT_DOCS_API_KEY:
        log.info("  No Docs API key set, skipping docs lookup.")
        return ""

    # Search using the subject line first (usually most descriptive)
    search_results = search_docs(subject)

    # If subject didn't find much, also try keywords from the last customer message
    if len(search_results) < 2:
        # Grab the last ~200 chars of thread history as a fallback search
        last_chunk = thread_history[-500:] if len(thread_history) > 500 else thread_history
        # Extract a rough search query from it (first 100 chars, no special chars)
        fallback_query = re.sub(r"[^a-zA-Z0-9 ]", " ", last_chunk[:150]).strip()
        if fallback_query:
            extra = search_docs(fallback_query, max_results=2)
            # Deduplicate by article ID
            seen_ids = {r["id"] for r in search_results}
            for item in extra:
                if item["id"] not in seen_ids:
                    search_results.append(item)
                    seen_ids.add(item["id"])

    if not search_results:
        log.info("  No relevant docs found.")
        return ""

    # Fetch full article text for top results
    articles = []
    for result in search_results[:3]:
        article_text = get_doc_article(result["id"])
        if article_text:
            articles.append(article_text)

    if not articles:
        return ""

    log.info(f"  Found {len(articles)} relevant help doc(s).")
    return "\n\n---\n\n".join(articles)


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
    Return active conversations ASSIGNED TO ME where the most recent thread
    was from a customer (i.e. waiting on us to reply) and that have NOT
    already been drafted by this script.
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

        tags = [t.get("tag", "") if isinstance(t, dict) else t for t in convo.get("tags", [])]
        if DRAFT_TAG in tags:
            continue  # already drafted
        # Check if customer is waiting
        if convo.get("customerWaitingSince", {}).get("time"):
            needs_reply.append(convo)

    return needs_reply


def get_thread_history(conversation_id: int) -> tuple[str, bool]:
    """
    Fetch all threads for a conversation and format them for Claude.
    Returns (formatted_history, agent_has_replied_before).
    """
    data = hs_get(f"/conversations/{conversation_id}/threads")
    threads = data.get("_embedded", {}).get("threads", [])

    # Sort oldest-first so the conversation reads chronologically
    threads.sort(key=lambda t: t.get("createdAt", ""))

    parts = []
    agent_has_replied = False
    for t in threads:
        thread_type = t.get("type", "unknown")
        # Skip internal notes from showing up as conversation context
        if thread_type == "note":
            continue

        created_by = t.get("createdBy", {})
        author_type = created_by.get("type", "unknown")  # "customer" or "user"
        author_name = f'{created_by.get("first", "")} {created_by.get("last", "")}'.strip()
        body = strip_html(t.get("body", ""))
        timestamp = t.get("createdAt", "")

        if not body:
            continue

        if author_type == "user":
            agent_has_replied = True

        label = "CUSTOMER" if author_type == "customer" else "AGENT"
        parts.append(f"[{label} - {author_name} - {timestamp}]\n{body}")

    return "\n\n---\n\n".join(parts), agent_has_replied


def draft_reply_with_claude(
    subject: str,
    thread_history: str,
    customer_name: str = "",
    is_first_reply: bool = True,
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

    if docs_context:
        user_message += (
            f"Here are relevant articles from our help documentation that may "
            f"help you craft an accurate response:\n\n"
            f"--- HELP DOCS ---\n\n{docs_context}\n\n"
            f"--- END HELP DOCS ---\n\n"
            f"Use information from these docs when relevant, but do NOT quote "
            f"them verbatim or say things like 'according to our help docs.' "
            f"Just use the knowledge naturally.\n\n"
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


def post_note(conversation_id: int, text: str) -> None:
    """Post the draft as an internal note on the conversation."""
    note_body = (
        f"<strong>🤖 AI-DRAFTED RESPONSE (review before sending):</strong>"
        f"<br><br>{text.replace(chr(10), '<br>')}"
    )
    hs_post(f"/conversations/{conversation_id}/notes", {"text": note_body})
    log.info(f"  Posted note on conversation {conversation_id}")


def tag_conversation(conversation_id: int, existing_tags: list[str]) -> None:
    """Add the DRAFT_TAG so we don't re-draft the same conversation."""
    all_tags = list(set(existing_tags + [DRAFT_TAG]))
    hs_put(f"/conversations/{conversation_id}", {"op": "replace", "path": "/tags", "value": all_tags})
    log.info(f"  Tagged conversation {conversation_id} with '{DRAFT_TAG}'")


def run() -> None:
    log.info("Starting Help Scout auto-drafter...")
    if DRY_RUN:
        log.info("DRY RUN mode -- drafts will be printed, not posted.")

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
            history, agent_has_replied = get_thread_history(convo_id)
            if not history:
                log.warning(f"  No usable threads found, skipping.")
                continue

            # Search help docs for relevant articles
            docs_context = find_relevant_docs(subject, history)

            draft = draft_reply_with_claude(
                subject,
                history,
                customer_name=customer_name,
                is_first_reply=not agent_has_replied,
                docs_context=docs_context,
            )
            if not draft:
                log.warning(f"  Claude returned empty draft, skipping.")
                continue

            if DRY_RUN:
                print(f"\n{'='*60}")
                print(f"CONVERSATION #{convo.get('number')}: {subject}")
                print(f"{'='*60}")
                print(draft)
                print()
            else:
                post_note(convo_id, draft)
                existing_tags = [
                    t.get("tag", "") if isinstance(t, dict) else t
                    for t in convo.get("tags", [])
                ]
                tag_conversation(convo_id, existing_tags)

        except Exception as e:
            log.error(f"  Error processing conversation {convo_id}: {e}")
            continue

    log.info("Done.")


if __name__ == "__main__":
    run()
