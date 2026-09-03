import asyncio, json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import discord
import bot as B

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")
        print(f"  FAIL {label}")
    else:
        print(f"  ok   {label}")


print("-- parse_word_list --")
check("parens form", B.parse_word_list("(a, b, c)"), ["a", "b", "c"])
check("no parens", B.parse_word_list("a, b, c"), ["a", "b", "c"])
check("phrases + case", B.parse_word_list("(Good Morning, FOO)"), ["good morning", "foo"])
check("messy spacing", B.parse_word_list("(  a  b ,   c  )"), ["a b", "c"])
check("empties dropped", B.parse_word_list("(a,,  , b)"), ["a", "b"])
check("dupes collapsed", B.parse_word_list("(a, A, a)"), ["a"])
check("empty input", B.parse_word_list(""), [])
check("only parens", B.parse_word_list("()"), [])

print("-- split_message --")
check("short passthrough", B.split_message("hello"), ["hello"])
check("all chunks within limit", all(len(c) <= 2000 for c in B.split_message("y" * 5000)), True)
long_lines = "\n".join("line " + str(i) * 50 for i in range(100))
check("newline split lossless",
      "\n".join(B.split_message(long_lines)).replace("\n", ""), long_lines.replace("\n", ""))
check("empty text", B.split_message(""), [""])

print("-- build_chunks (prefix must ride with the content) --")
check("no prefix passes through", B.build_chunks("", "hello"), ["hello"])
check("prefix joins content", B.build_chunks("PFX", "hello"), ["PFX\nhello"])
check("empty body with prefix", B.build_chunks("PFX", ""), ["PFX"])
check("empty body, no prefix", B.build_chunks("", ""), [])
big = B.build_chunks("PFX", "z" * 5000)
check("prefix on first chunk", big[0].startswith("PFX\nz"), True)
check("prefix not stranded alone", len(big[0]) > 4, True)
check("webhook continuations are bare", all(not c.startswith("PFX") for c in big[1:]), True)
check("every chunk within limit", all(len(c) <= 2000 for c in big), True)
check("no content lost", "".join(big).replace("PFX\n", ""), "z" * 5000)
check("2000-char body does not 400",
      all(len(c) <= 2000 for c in B.build_chunks("PFX", "w" * 2000)), True)

uid = 1513256892569485405
rc = "… <@%d> (cont.):" % uid
rh = B.build_header(uid, B.REPLY_KNOWN, uid, False)
rh_plain = B.build_header(uid, B.REPLY_NONE, None, False)
check("cont longer than a plain header", len(rc) > len(rh_plain), True)
check("cont shorter than a reply header", len(rc) < len(rh), True)
for name, head in (("reply header", rh), ("plain header", rh_plain)):
    real = B.build_chunks(head, "q" * 6000, rc)
    check("fallback %s: all chunks fit" % name, all(len(c) <= 2000 for c in real), True)
    check("fallback %s: nothing lost" % name,
          "".join(real).replace(head + "\n", "").replace(rc + "\n", ""), "q" * 6000)
_jl = "https://discord.com/channels/%d/%d/%d" % (uid, uid, uid)
wh = B.build_chunks(B.build_reply_line(B.REPLY_KNOWN, uid, _jl, False), "e" * 6000)
check("real jump-link prefix: chunks fit", all(len(c) <= 2000 for c in wh), True)

print("-- build_reply_line (webhook mode) --")
JUMP = "https://discord.com/channels/1/2/3"
check("no reply -> no line", B.build_reply_line(B.REPLY_NONE, None, None, False), "")
check("known reply has mention and link",
      B.build_reply_line(B.REPLY_KNOWN, 42, JUMP, False),
      "↪ replying to <@42> — " + JUMP)
check("deleted target claims no link",
      B.build_reply_line(B.REPLY_DELETED, None, JUMP, False),
      "↪ replying to a message that no longer exists")
check("unknown target still links",
      B.build_reply_line(B.REPLY_UNKNOWN, None, JUMP, False),
      "↪ replying to an earlier message — " + JUMP)
check("forward", B.build_reply_line(B.REPLY_NONE, None, None, True),
      "↪ forwarded a message")
check("raw url, not a masked link",
      "](" in B.build_reply_line(B.REPLY_KNOWN, 42, JUMP, False), False)

print("-- sanitize_webhook_username --")
S = B.sanitize_webhook_username
ZW = "​"
check("plain name", S("Alice"), "Alice")
check("whitespace collapsed", S("  Bob   Smith "), "Bob Smith")
check("empty falls back", S(""), "Unknown")
check("whitespace-only falls back", S("      "), "Unknown")
check("length capped at 80", len(S("x" * 200)), 80)
for banned in ("discord", "Discord", "DISCORD", "clyde", "Clyde"):
    out = S("cool " + banned + " guy")
    check("%r not literally present" % banned, banned.lower() in out.lower(), False)
    check("%r still readable once ZWSP stripped" % banned,
          banned.lower() in out.replace(ZW, "").lower(), True)
check("case preserved through sanitising", S("Discord").replace(ZW, ""), "Discord")

print("-- build_header --")
check("plain", B.build_header(1, B.REPLY_NONE, None, False), "\U0001f4e8 <@1> said:")
check("reply known", B.build_header(1, B.REPLY_KNOWN, 2, False),
      "\U0001f4e8 <@1> said, replying to <@2>:")
check("reply deleted", B.build_header(1, B.REPLY_DELETED, None, False),
      "\U0001f4e8 <@1> said, replying to a message that no longer exists:")
check("reply unknown is not claimed deleted", B.build_header(1, B.REPLY_UNKNOWN, None, False),
      "\U0001f4e8 <@1> said, replying to an earlier message:")
check("forward", B.build_header(1, B.REPLY_NONE, None, True),
      "\U0001f4e8 <@1> forwarded a message:")

print("-- forwarded / scannable text --")
msg = SimpleNamespace(content="look at this",
                      message_snapshots=[SimpleNamespace(content="secret foo inside")])
check("forwarded text extracted", B.forwarded_text(msg), "secret foo inside")
check("scannable includes snapshot", B.scannable_text(msg), "look at this\nsecret foo inside")
check("no snapshots", B.scannable_text(SimpleNamespace(content="hi", message_snapshots=[])), "hi")
check("snapshots attr missing", B.scannable_text(SimpleNamespace(content="hi")), "hi")

print("-- allowed_mentions: the mention_author=False trap --")
from discord.http import handle_message_parameters
prev = bot_default = B.bot._connection.allowed_mentions
check("bot has a non-default allowed_mentions", prev is not None, True)
p = handle_message_parameters(
    content="Added: `@everyone`", mention_author=False, previous_allowed_mentions=prev
)
check("reply payload is inert", p.payload["allowed_mentions"],
      {"parse": [], "replied_user": False})
user = SimpleNamespace(id=42)
am = discord.AllowedMentions(everyone=False, roles=False, users=[user], replied_user=False)
p2 = handle_message_parameters(content="hi <@42> @everyone", allowed_mentions=am,
                               previous_allowed_mentions=prev)
check("relay whitelists only the author", p2.payload["allowed_mentions"],
      {"parse": [], "users": [42]})
check("relay does not parse @everyone/roles", p2.payload["allowed_mentions"]["parse"], [])


async def main():
    print("-- WordStore matching --")
    with tempfile.TemporaryDirectory() as d:
        s = B.WordStore(Path(d) / "words.json")
        s.load()
        check("no words -> no match", s.find("anything at all"), None)
        await s.add(["foo", "good morning", "c++", "a.b"])
        check("exact", s.find("foo"), "foo")
        check("case insensitive", s.find("FOO"), "foo")
        check("NOT substring 'food'", s.find("i like food"), None)
        check("NOT substring 'buffoon'", s.find("what a buffoon"), None)
        check("phrase match", s.find("say good morning to her"), "good morning")
        check("phrase collapses spacing", s.find("good    morning"), "good morning")
        check("regex chars literal", s.find("i know c++ well"), "c++")
        check("dot not a wildcard", s.find("axb"), None)
        check("newline boundary", s.find("hey\nfoo\nbye"), "foo")
        a2, d2, _ = await s.add(["FOO"])
        check("dupe after normalise", (a2, d2), ([], ["foo"]))
        check("remove normalises", await s.remove(["  C++  "]), (["c++"], []))

        s2 = B.WordStore(Path(d) / "words.json")
        s2.load()
        check("reload from disk", sorted(s2.words), ["a.b", "foo", "good morning"])
        check("not degraded", s2.degraded, False)

        print("-- persist-before-mutate --")
        s3 = B.WordStore(Path(d) / "sub" / "words.json")  # parent dir does not exist
        s3.load()
        try:
            await s3.add(["boom"])
            check("write failure raises", False, True)
        except B.StoreWriteError:
            check("write failure raises", True, True)
        check("memory untouched after failed write", s3.words, [])
        check("no match after failed write", s3.find("boom"), None)

        print("-- corrupt file handling --")
        bad = Path(d) / "bad.json"
        bad.write_text("{not json at all", encoding="utf-8")
        s4 = B.WordStore(bad)
        s4.load()
        check("degraded flag set", s4.degraded, True)
        check("starts empty", s4.words, [])
        check("bad file preserved", bad.with_suffix(".json.corrupt").exists(), True)
        check("original moved away", bad.exists(), False)
        await s4.add(["fresh"])
        check("can still write after corruption", s4.words, ["fresh"])
        check("corrupt backup still there", bad.with_suffix(".json.corrupt").exists(), True)

        print("-- caps --")
        s5 = B.WordStore(Path(d) / "w5.json")
        s5.load()
        a, _, r = await s5.add(["x" * 101, "ok"])
        check("over-long rejected", (a, r), (["ok"], ["x" * 101]))
        s6 = B.WordStore(Path(d) / "w6.json")
        s6.load()
        added6, _, rejected6 = await s6.add([f"w{i}" for i in range(2000)])
        check("no word-count cap", (len(added6), rejected6), (2000, []))
        check("no MAX_WORDS constant left", hasattr(B, "MAX_WORDS"), False)
        check("huge list still matches", s6.find("hey w1999 there"), "w1999")
        check("huge list rejects non-members", s6.find("hey w2000 there"), None)
        check("huge list still whole-word", s6.find("xw1999y"), None)
        import time as _t
        t0 = _t.perf_counter()
        for _ in range(200):
            s6.find("an ordinary sentence with none of the watched words in it")
        elapsed = _t.perf_counter() - t0
        check(f"2000-word regex stays fast ({elapsed*1000:.0f}ms/200 scans)", elapsed < 1.0, True)

    print("-- channel locks --")
    l1 = B.channel_lock(1)
    check("same lock for same channel", B.channel_lock(1) is l1, True)
    check("different lock per channel", B.channel_lock(2) is not l1, True)
    del l1
    check("weak map drops unheld locks", len(B._channel_locks) <= 2, True)

    print("-- registration --")
    check("commands", sorted(c.name for c in B.bot.commands),
          ["addwords", "clearwords", "help", "listwords", "removewords"])
    check("no slash commands", len(B.bot.tree.get_commands()), 0)
    check("message_content intent", B.bot.intents.message_content, True)
    check("prefix", B.bot.command_prefix, "k!")
    check("edit handler is raw only", hasattr(B, "on_message_edit"), False)
    check("raw edit handler present", callable(getattr(B, "on_raw_message_edit", None)), True)
    check("authorized ids", B.AUTHORIZED_USER_IDS,
          {1513256892569485405, 1464639433276915825, 1414684138996236344})


asyncio.run(main())
print()
if fails:
    print(f"{len(fails)} FAILURE(S):")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL TESTS PASSED")
