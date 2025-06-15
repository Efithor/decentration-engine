"""discord.py – Discord Input Adapter

Purpose
-------
Fetches a once-daily digest of messages from selected Discord servers and
channels.  Because automated scraping of Discord without an official bot
account violates the Terms of Service, the preferred approach is to register a
bot application and use the official Gateway API with a reduced polling rate.

Digest strategy
---------------
* Collect all messages in the last 24h.
* Deduplicate by author + content hash.
* Persist raw messages, but only surface summary text to the LLM.

Open questions
--------------
1. Handling multiple guilds with different privacy expectations.
2. Obfuscating user IDs for anonymization.

Implementation notes
--------------------
* Library: `discord.py` (async).
* Batch upload digests to the database once per day via an async cron job.
"""

# TODO: Implement Discord daily digest collector.
