"""slack.py – Slack Input Adapter

Purpose
-------
Consumes messages, reactions, and file uploads from Slack workspaces where the
user is an administrator.  The adapter will rely on Slack's Events API over
HTTP or Socket Mode to stream events into the agent.

Key design considerations
-------------------------
1. **Thread aggregation** – Collate threaded discussions into a single unit so
   the summarizer can treat them as cohesive conversations.
2. **Rate-limiting** – Respect Slack's API quotas; maintain a backlog queue if
   necessary.
3. **Privacy** – Exclude private channels / DMs unless explicitly whitelisted.
4. **Entity linking** – Map Slack user IDs to `OtherPeople` entries.

Implementation hints
--------------------
* Use `slack_sdk` (official Python SDK).
* Run as an asyncio background task triggered by `create_app`.
* Store events in a `slack_events` table or JSONB column.
"""

# TODO: Implement Slack event consumer (see above).
