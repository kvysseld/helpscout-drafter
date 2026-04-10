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
  HELPSCOUT_MAILBOX_ID   - (optional) limit to one mailbox

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
MAILBOX_ID = os.environ.get("HELPSCOUT_MAILBOX_ID", "")
DRAFT_TAG = os.environ.get("DRAFT_TAG", "ai-drafted")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

HS_BASE = "https://api.helpscout.net/v2"
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
You are a friendly, professional support agent for Nucleus, a web design \
and hosting platform for churches. Your name is Kyle.

When drafting a reply:
- Be warm but concise. Church staff are busy.
- If the issue is technical (DNS, domain, email, site editor), give clear \
  step-by-step guidance.
- If you are unsure or the issue needs internal investigation, say so \
  honestly and let the customer know you will follow up.
- Never make up features or capabilities that Nucleus does not have.
- Sign off casually: "Let me know if you have any other questions!" or similar.
- Do NOT include a subject line or greeting like "Dear customer". Just write \
  the reply body. The customer's name will already be visible in Help Scout.
- Keep it under 200 words unless the situation genuinely requires more.
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

    params: dict = {
        "status": "active",
        "sortField": "customerWaitingSince",
        "sortOrder": "desc",
        "query": f"(assignedTo:{my_id})",
    }
    if MAILBOX_ID:
        params["mailbox"] = MAILBOX_ID

    data = hs_get("/conversations", params)
    conversations = data.get("_embedded", {}).get("conversations", [])

    needs_reply = []
    for convo in conversations:
        tags = [t.get("tag", "") if isinstance(t, dict) else t for t in convo.get("tags", [])]
        if DRAFT_TAG in tags:
            continue  # already drafted
        # Check if customer is waiting
        if convo.get("customerWaitingSince", {}).get("time"):
            needs_reply.append(convo)

    return needs_reply


def get_thread_history(conversation_id: int) -> str:
    """Fetch all threads for a conversation and format them for Claude."""
    data = hs_get(f"/conversations/{conversation_id}/threads")
    threads = data.get("_embedded", {}).get("threads", [])

    # Sort oldest-first so the conversation reads chronologically
    threads.sort(key=lambda t: t.get("createdAt", ""))

    parts = []
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

        label = "CUSTOMER" if author_type == "customer" else "AGENT"
        parts.append(f"[{label} - {author_name} - {timestamp}]\n{body}")

    return "\n\n---\n\n".join(parts)


def draft_reply_with_claude(subject: str, thread_history: str) -> str:
    """Send the conversation to Claude and get a draft reply."""
    user_message = (
        f"Here is a Help Scout support conversation. The subject line is: \"{subject}\"\n\n"
        f"--- CONVERSATION HISTORY ---\n\n{thread_history}\n\n"
        f"--- END ---\n\n"
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

        try:
            history = get_thread_history(convo_id)
            if not history:
                log.warning(f"  No usable threads found, skipping.")
                continue

            draft = draft_reply_with_claude(subject, history)
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
