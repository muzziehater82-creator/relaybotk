"""k! relay bot — deletes messages containing watched words and reposts them with attribution."""

import asyncio
import io
import json
import logging
import os
import re
import time
import weakref
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

PREFIX = "k!"
AUTHORIZED_USER_IDS = {
    1513256892569485405,
    1464639433276915825,
    1414684138996236344,
}

WORDS_PATH = Path(__file__).with_name("words.json")
MAX_WORD_LEN = 100
MESSAGE_LIMIT = 2000
# Hard ceiling on how much attachment data is buffered for a single relay.
MAX_RELAY_BYTES = 25 * 1024 * 1024
# The webhook the bot creates per channel to speak as the original author.
WEBHOOK_NAME = "k-relay"
# Discord rejects webhook usernames containing these, and caps them at 80 chars.
BANNED_NAME_BITS = ("discord", "clyde")
WEBHOOK_NAME_LIMIT = 80
# Seconds between "not authorized" replies to the same user, so the denial
# path cannot be used to make the bot spam on demand.
DENIAL_COOLDOWN = 30.0

# How the message being relayed related to another message.
REPLY_NONE = "none"
REPLY_KNOWN = "known"
REPLY_DELETED = "deleted"
REPLY_UNKNOWN = "unknown"

log = logging.getLogger("relay")


def normalize(word):
    """Canonical form of a watched word: lowercased, whitespace collapsed."""
    return " ".join(str(word).split()).lower()


class StoreWriteError(Exception):
    """The word list could not be persisted."""


class WordStore:
    """Watched words, persisted to JSON and compiled into a single match regex."""

    def __init__(self, path):
        self.path = path
        self.words = []
        self._pattern = None
        self._lock = asyncio.Lock()
        self.degraded = False  # set when the on-disk file was unreadable

    @staticmethod
    def _compile(words):
        if not words:
            return None
        # Longest first so "good morning" wins over "good".
        ordered = sorted(words, key=len, reverse=True)
        alts = [r"\s+".join(re.escape(tok) for tok in w.split()) for w in ordered]
        return re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE)

    def load(self):
        if self.path.exists():
            try:
                # utf-8-sig: Windows editors and PowerShell write a BOM, and a
                # BOM makes json.loads fail.
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                # Normalise on read so a hand-edited file still behaves.
                seen = set()
                words = []
                for raw in data.get("words", []):
                    w = normalize(raw)
                    if w and w not in seen:
                        seen.add(w)
                        words.append(w)
                self.words = words
            except (json.JSONDecodeError, OSError, AttributeError, TypeError):
                # Preserve the unreadable file instead of letting the next write
                # silently overwrite it.
                backup = self.path.with_suffix(".json.corrupt")
                try:
                    os.replace(self.path, backup)
                    log.exception("could not read %s — moved it to %s, starting empty",
                                  self.path, backup)
                except OSError:
                    log.exception("could not read or preserve %s — starting empty", self.path)
                self.degraded = True
                self.words = []
        self._pattern = self._compile(self.words)

    def _write(self, words):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"words": words}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    async def _commit(self, words):
        """Persist first, adopt in memory only once the write succeeded."""
        try:
            await asyncio.to_thread(self._write, words)
        except OSError as exc:
            log.exception("failed to write %s", self.path)
            raise StoreWriteError(str(exc)) from exc
        self.words = words
        self._pattern = self._compile(words)

    async def add(self, words):
        """Returns (added, already_present, rejected)."""
        added, dupes, rejected = [], [], []
        async with self._lock:
            existing = set(self.words)
            for w in (normalize(w) for w in words):
                if not w:
                    continue
                if len(w) > MAX_WORD_LEN:
                    rejected.append(w)
                elif w in existing or w in added:
                    dupes.append(w)
                else:
                    added.append(w)
            if added:
                await self._commit(self.words + added)
        return added, dupes, rejected

    async def remove(self, words):
        """Returns (removed, missing)."""
        removed, missing = [], []
        async with self._lock:
            pending = list(self.words)
            for w in (normalize(w) for w in words):
                if w in pending:
                    pending.remove(w)
                    removed.append(w)
                else:
                    missing.append(w)
            if removed:
                await self._commit(pending)
        return removed, missing

    async def clear(self):
        async with self._lock:
            count = len(self.words)
            await self._commit([])
        return count

    def find(self, content):
        """The first watched word present in content, or None."""
        if not self._pattern or not content:
            return None
        m = self._pattern.search(content)
        return " ".join(m.group(0).split()).lower() if m else None


store = WordStore(WORDS_PATH)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    # Without a non-default value here, passing mention_author=False makes
    # discord.py synthesise a permissive AllowedMentions() for that message.
    allowed_mentions=discord.AllowedMentions.none(),
)

# One lock per channel so simultaneous triggers relay in the order they arrived.
# Weak values: a lock is dropped once nothing holds or awaits it.
_channel_locks = weakref.WeakValueDictionary()
_denial_sent_at = {}
# channel_id -> Webhook, so we don't refetch on every relay.
_webhook_cache = {}


def channel_lock(channel_id):
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock


def parse_word_list(raw):
    """`(a, b, c)` or `a, b, c` -> ['a', 'b', 'c'], normalised and lowercased."""
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    out, seen = [], set()
    for part in raw.split(","):
        word = normalize(part)
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def split_message(text, limit=MESSAGE_LIMIT):
    """Split text into Discord-sized chunks, preferring line breaks."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


def build_chunks(prefix, body, cont_prefix=""):
    """Chunks for one relay; `prefix` always rides on the first chunk of content."""
    if not body:
        return [prefix] if prefix else []
    if not prefix and not cont_prefix:
        return split_message(body)
    # Size against the longer prefix so a continuation chunk still fits.
    room = MESSAGE_LIMIT - max(len(prefix), len(cont_prefix)) - 1
    if room < 200:  # absurdly long prefix; degrade rather than misbehave
        return (split_message(prefix) if prefix else []) + split_message(body)
    parts = split_message(body, room)
    out = [f"{prefix}\n{parts[0]}" if prefix else parts[0]]
    for part in parts[1:]:
        out.append(f"{cont_prefix}\n{part}" if cont_prefix else part)
    return out


def sanitize_webhook_username(name):
    """Discord rejects webhook usernames containing 'discord' or 'clyde'."""
    cleaned = " ".join(str(name).split())
    for bad in BANNED_NAME_BITS:
        # Break the banned substring with a zero-width space; still reads right.
        cleaned = re.sub(
            re.escape(bad),
            lambda m: m.group(0)[0] + "​" + m.group(0)[1:],
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = cleaned[:WEBHOOK_NAME_LIMIT].strip()
    return cleaned or "Unknown"


async def get_webhook(channel):
    """A webhook the bot owns on this channel (or its parent), or None."""
    parent = channel.parent if isinstance(channel, discord.Thread) else channel
    if parent is None or not hasattr(parent, "webhooks"):
        return None
    hook = _webhook_cache.get(parent.id)
    if hook is not None:
        return hook
    try:
        for existing in await parent.webhooks():
            if existing.token and existing.user and existing.user.id == bot.user.id:
                _webhook_cache[parent.id] = existing
                return existing
        hook = await parent.create_webhook(name=WEBHOOK_NAME, reason="k! relay bot")
    except (discord.Forbidden, discord.HTTPException):
        log.warning("cannot get or create a webhook in #%s — falling back", parent)
        return None
    _webhook_cache[parent.id] = hook
    return hook


def forwarded_text(message):
    """Text carried by a forwarded message, which is not in message.content."""
    snapshots = getattr(message, "message_snapshots", None) or []
    return "\n".join(
        s.content for s in snapshots if getattr(s, "content", "")
    )


def scannable_text(message):
    """Everything a watched word could hide in."""
    return "\n".join(p for p in (message.content, forwarded_text(message)) if p)


def is_forward(message):
    ref = message.reference
    return ref is not None and getattr(ref, "type", None) == discord.MessageReferenceType.forward


async def collect_attachments(message):
    """Download attachments before the original is deleted.

    Returns a list of discord.File, or None if anything could not be preserved —
    in which case the caller must not delete the original.
    """
    if not message.attachments:
        return []
    budget = min(message.guild.filesize_limit, MAX_RELAY_BYTES)
    if sum(a.size for a in message.attachments) > budget:
        log.warning("attachments exceed the %d byte relay budget — not relaying", budget)
        return None
    files = []
    for att in message.attachments:
        try:
            data = await asyncio.wait_for(att.read(), timeout=60)
        except (discord.HTTPException, discord.NotFound, asyncio.TimeoutError, OSError):
            log.exception("could not download %s — not relaying", att.filename)
            return None
        files.append(
            discord.File(
                io.BytesIO(data),
                filename=att.filename,
                spoiler=att.is_spoiler(),
                description=att.description,
            )
        )
    return files


async def resolve_reply_target(message):
    """(state, author_id, jump_url) describing the message this one replied to."""
    ref = message.reference
    if ref is None or ref.message_id is None or is_forward(message):
        return REPLY_NONE, None, None
    # jump_url is built from the reference itself, so it works even when the
    # target cannot be fetched.
    jump = ref.jump_url
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return REPLY_KNOWN, resolved.author.id, resolved.jump_url
    if isinstance(resolved, discord.DeletedReferencedMessage):
        return REPLY_DELETED, None, jump
    try:
        target = await message.channel.fetch_message(ref.message_id)
    except discord.NotFound:
        return REPLY_DELETED, None, jump
    except (discord.Forbidden, discord.HTTPException):
        # We simply cannot see it; do not claim it was deleted.
        return REPLY_UNKNOWN, None, jump
    return REPLY_KNOWN, target.author.id, target.jump_url


def build_reply_line(state, reply_author_id, jump_url, forwarded):
    """The context line placed above relayed content, or '' when there is none."""
    if forwarded:
        return "↪ forwarded a message"
    if state == REPLY_NONE or not jump_url:
        return ""
    if state == REPLY_KNOWN and reply_author_id is not None:
        return f"↪ replying to <@{reply_author_id}> — {jump_url}"
    if state == REPLY_DELETED:
        return "↪ replying to a message that no longer exists"
    return f"↪ replying to an earlier message — {jump_url}"


def build_header(author_id, state, reply_author_id, forwarded):
    """Attribution line for the fallback path, where no webhook is available."""
    who = f"\U0001f4e8 <@{author_id}>"
    if forwarded:
        return f"{who} forwarded a message:"
    if state == REPLY_KNOWN and reply_author_id is not None:
        return f"{who} said, replying to <@{reply_author_id}>:"
    if state == REPLY_DELETED:
        return f"{who} said, replying to a message that no longer exists:"
    if state == REPLY_UNKNOWN:
        return f"{who} said, replying to an earlier message:"
    return f"{who} said:"


def relay_blocked_reason(message):
    """Why this message must not be relayed, or None if it may be."""
    # Deleting a message that owns a thread destroys the thread and every reply
    # in it; the same goes for the starter post of a thread or forum post.
    if message.thread is not None:
        return "it starts a thread"
    if isinstance(message.channel, discord.Thread) and message.channel.id == message.id:
        return "it is a thread/forum starter post"
    try:
        perms = message.channel.permissions_for(message.guild.me)
    except discord.ClientException:
        return "the parent channel is not cached, so permissions are unknown"
    can_send = (
        perms.send_messages_in_threads
        if isinstance(message.channel, discord.Thread)
        else perms.send_messages
    )
    if not perms.manage_messages:
        return "missing Manage Messages"
    if not can_send:
        return "missing Send Messages"
    if message.attachments and not perms.attach_files:
        return "missing Attach Files and the message has attachments"
    return None


async def send_via_webhook(message, body, files, reply_line):
    """Post as the original author. True if posted, None to fall back."""
    hook = await get_webhook(message.channel)
    if hook is None:
        return None
    parent = message.channel.parent if isinstance(message.channel, discord.Thread) else message.channel
    extra = {"thread": message.channel} if isinstance(message.channel, discord.Thread) else {}

    chunks = build_chunks(reply_line, body)
    if not chunks:
        if not files:
            return False
        chunks = [""]

    for i, chunk in enumerate(chunks):
        try:
            await hook.send(
                content=chunk if chunk else discord.utils.MISSING,
                username=sanitize_webhook_username(message.author.display_name),
                avatar_url=message.author.display_avatar.url,
                files=files if i == 0 else [],
                allowed_mentions=discord.AllowedMentions.none(),
                wait=True,
                **extra,
            )
        except discord.NotFound:
            # Someone deleted the webhook out from under us.
            _webhook_cache.pop(parent.id, None)
            if i == 0:
                return None
            log.warning("webhook vanished partway through a relay in #%s", message.channel)
            return True
        except discord.HTTPException:
            if i == 0:
                log.exception("webhook relay failed in #%s — falling back", message.channel)
                return None
            log.exception("webhook relay failed partway (chunk %d) in #%s", i, message.channel)
            return True  # something is already posted; do not duplicate it
    return True


async def send_as_bot(message, body, files, state, reply_author_id, forwarded):
    """Fallback when no webhook is available: post as the bot, with attribution."""
    header = build_header(message.author.id, state, reply_author_id, forwarded)
    cont_header = f"… <@{message.author.id}> (cont.):"

    reference = None
    if state == REPLY_KNOWN and message.reference is not None:
        reference = discord.MessageReference(
            message_id=message.reference.message_id,
            channel_id=message.channel.id,
            guild_id=message.guild.id,
            fail_if_not_exists=False,
        )
    # Only the author is pingable; anything inside the relayed content is inert.
    mentions = discord.AllowedMentions(
        everyone=False, roles=False, users=[message.author], replied_user=False
    )

    chunks = build_chunks(header, body, cont_header)
    for i, chunk in enumerate(chunks):
        try:
            await message.channel.send(
                chunk,
                files=files if i == 0 else [],
                reference=reference if i == 0 else None,
                allowed_mentions=mentions,
            )
        except discord.HTTPException:
            log.exception(
                "failed to send relay chunk %d in #%s — leaving the original in place",
                i, message.channel,
            )
            return i > 0  # partial post still counts; never duplicate
    return True


async def relay(message, matched):
    blocked = relay_blocked_reason(message)
    if blocked:
        log.warning("not relaying a message in #%s: %s", message.channel, blocked)
        return

    async with channel_lock(message.channel.id):
        forwarded = is_forward(message)
        state, reply_author_id, jump_url = await resolve_reply_target(message)

        files = await collect_attachments(message)
        if files is None:
            return  # could not preserve attachments; leave the original alone

        body = message.content or ""
        fwd = forwarded_text(message)
        if fwd:
            quoted = "\n".join("> " + line for line in fwd.splitlines())
            body = f"{body}\n{quoted}" if body else quoted
        if message.stickers:
            body += "\n*(stickers: " + ", ".join(s.name for s in message.stickers) + ")*"

        reply_line = build_reply_line(state, reply_author_id, jump_url, forwarded)

        # Post the relay BEFORE deleting the original — if a send fails, the
        # user's message must still be there.
        posted = await send_via_webhook(message, body, files, reply_line)
        if posted is None:
            posted = await send_as_bot(
                message, body, files, state, reply_author_id, forwarded
            )
        if not posted:
            return  # nothing was posted; leave the original alone

        try:
            await message.delete()
        except discord.NotFound:
            pass  # already gone
        except (discord.Forbidden, discord.HTTPException):
            log.exception("relayed but could not delete the original in #%s", message.channel)
        log.info("relayed a message from %s (matched %r)", message.author, matched)


def authorized():
    async def predicate(ctx):
        return ctx.author.id in AUTHORIZED_USER_IDS

    return commands.check(predicate)


async def reply_chunked(ctx, text):
    for chunk in split_message(text):
        await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())


@bot.event
async def on_ready():
    log.info("logged in as %s — watching %d word(s)", bot.user, len(store.words))
    if store.degraded:
        log.warning("the word list failed to load and started empty — check words.json.corrupt")


@bot.event
async def on_message(message):
    if message.author.bot or message.webhook_id is not None:
        return
    await bot.process_commands(message)
    if message.guild is None:
        return
    if message.content.startswith(PREFIX):
        return  # never relay our own command invocations
    matched = store.find(scannable_text(message))
    if matched:
        await relay(message, matched)


@bot.event
async def on_raw_message_edit(payload):
    """Raw, so that edits to messages older than the cache are still caught.

    discord.py dispatches raw_message_edit for cached and uncached messages
    alike, so this must be the only edit handler or edits would relay twice.
    """
    content = payload.data.get("content")
    if content is None:
        return  # embed/attachment metadata update, not a content edit
    cached = payload.cached_message
    if cached is not None and cached.content == content:
        return
    if content.startswith(PREFIX):
        return
    message = payload.message
    if message is None or message.guild is None:
        return
    if message.author.bot or message.webhook_id is not None:
        return
    matched = store.find(scannable_text(message))
    if matched:
        await relay(message, matched)


@bot.command(name="addwords")
@authorized()
async def addwords(ctx, *, raw: str = ""):
    words = parse_word_list(raw)
    if not words:
        await ctx.reply(f"Usage: `{PREFIX}addwords (word one, word two, ...)`", mention_author=False)
        return
    try:
        added, dupes, rejected = await store.add(words)
    except StoreWriteError as exc:
        await ctx.reply(f"Could not save the word list, nothing was changed: `{exc}`", mention_author=False)
        return
    lines = []
    if added:
        lines.append(f"Added {len(added)}: " + ", ".join(f"`{w}`" for w in added))
    if dupes:
        lines.append("Already on the list: " + ", ".join(f"`{w}`" for w in dupes))
    if rejected:
        lines.append(
            f"Rejected (over {MAX_WORD_LEN} characters): "
            + ", ".join(f"`{w}`" for w in rejected)
        )
    await reply_chunked(ctx, "\n".join(lines))


@bot.command(name="removewords")
@authorized()
async def removewords(ctx, *, raw: str = ""):
    words = parse_word_list(raw)
    if not words:
        await ctx.reply(f"Usage: `{PREFIX}removewords (word one, word two, ...)`", mention_author=False)
        return
    try:
        removed, missing = await store.remove(words)
    except StoreWriteError as exc:
        await ctx.reply(f"Could not save the word list, nothing was changed: `{exc}`", mention_author=False)
        return
    lines = []
    if removed:
        lines.append(f"Removed {len(removed)}: " + ", ".join(f"`{w}`" for w in removed))
    if missing:
        lines.append("Not on the list: " + ", ".join(f"`{w}`" for w in missing))
    await reply_chunked(ctx, "\n".join(lines))


@bot.command(name="listwords")
@authorized()
async def listwords(ctx):
    if not store.words:
        await ctx.reply("The word list is empty.", mention_author=False)
        return
    await reply_chunked(
        ctx,
        f"**{len(store.words)} watched word(s):**\n" + ", ".join(f"`{w}`" for w in store.words),
    )


@bot.command(name="clearwords")
@authorized()
async def clearwords(ctx):
    try:
        count = await store.clear()
    except StoreWriteError as exc:
        await ctx.reply(f"Could not save the word list, nothing was changed: `{exc}`", mention_author=False)
        return
    await ctx.reply(f"Cleared {count} word(s).", mention_author=False)


@bot.command(name="help")
@authorized()
async def help_command(ctx):
    await ctx.reply(
        f"`{PREFIX}addwords (a, b, c)` — watch these words\n"
        f"`{PREFIX}removewords (a, b)` — stop watching them\n"
        f"`{PREFIX}listwords` — show the list\n"
        f"`{PREFIX}clearwords` — empty the list\n\n"
        "Any message containing a watched word is deleted and reposted by me, "
        "credited to its author and to whoever they were replying to.",
        mention_author=False,
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        # Rate-limited: otherwise anyone could use this path to make the bot post.
        now = time.monotonic()
        last = _denial_sent_at.get(ctx.author.id, 0.0)
        if now - last >= DENIAL_COOLDOWN:
            _denial_sent_at[ctx.author.id] = now
            await ctx.reply("You are not authorized to use this command.", mention_author=False)
        return
    log.exception("command %s failed", ctx.command, exc_info=error)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set — put it in a .env file or the host's env vars.")
    store.load()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
