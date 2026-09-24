"""The Minecraft block shop: the catalog's rules, the console client's reply
matching, and the `!mc link / verify / buy / sell` flows against a fake
console. No SSH anywhere — McConsole gets an injected connect() whose fake
process scripts the server's replies."""
import asyncio
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.minecraft_cog as mc_mod
import src.mc_console as console_mod
from src.cogs.minecraft_cog import MinecraftCog
from src.cogs.settings_cog import SettingsCog
from src.guild_config import get_guild_cfg
from src.mc_console import (
    CLEAR_OK, GIVE_OK, LIST_HEADER, NO_TARGET, ConsoleError, ConsoleRefused, McConsole, clean_line,
)
from src.mc_shop import BY_ID, CATEGORIES, ITEMS, MAX_TRADE_COUNT, find_item, fmt_blocks, items_in, match_category
from src.settings_hub import items_for

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

GID = 42
UID = 7


# ── catalog ───────────────────────────────────────────────────────────────────

def test_minerals_are_sell_only_and_priced_as_listed():
    for item in items_in("minerals"):
        assert item.buy is None and item.sell is not None, item.id
    assert BY_ID["coal"].sell == 25            # 2.5 blocks, in tenths
    assert BY_ID["diamond_block"].sell == 12600
    assert BY_ID["ancient_debris"].sell == 750  # "same as scrap", not half of 160
    assert BY_ID["quartz_block"].buy == 120 and BY_ID["quartz_block"].sell == 60
    for cid in ("copper_block", "exposed_copper", "waxed_oxidized_copper"):
        assert BY_ID[cid].sell == 135


def test_nothing_abusable_is_listed():
    ids = set(BY_ID)
    for banned in ("iron", "gold", "planks", "log", "_wood", "emerald", "redstone", "hay", "bone_block",
                   "slime", "honey", "dried_kelp", "gilded", "tnt", "bookshelf", "obsidian"):
        assert not any(banned in i for i in ids), banned
    # Farm products are never bought back.
    for cid in ("cobblestone", "stone", "basalt", "white_wool", "prismarine", "sea_lantern", "netherrack"):
        assert BY_ID[cid].sell is None and BY_ID[cid].buy is not None, cid


def test_sell_never_exceeds_half_of_buy():
    for item in ITEMS:
        if item.buy is not None and item.sell is not None:
            assert item.sell * 2 <= item.buy, item.id
        assert item.buy is None or item.buy > 0
        assert item.sell is None or item.sell > 0


def test_every_item_sits_in_a_listed_category_with_a_unique_id():
    keys = {k for k, _ in CATEGORIES}
    assert all(item.category in keys for item in ITEMS)
    assert len({item.id for item in ITEMS}) == len(ITEMS)
    assert all(items_in(k) for k in keys)


def test_find_item_takes_ids_names_and_aliases():
    assert find_item("stone_bricks") is BY_ID["stone_bricks"]
    assert find_item("Stone Bricks") is BY_ID["stone_bricks"]
    assert find_item("minecraft:stone-bricks") is BY_ID["stone_bricks"]
    assert find_item("block of coal") is BY_ID["coal_block"]
    assert find_item("coal block") is BY_ID["coal_block"]
    assert find_item("Block of Diamond") is BY_ID["diamond_block"]
    assert find_item("light gray glazed terracotta").id == "silver_glazed_terracotta"
    assert find_item("purpur pill") is BY_ID["purpur_pillar"]   # unique prefix
    assert find_item("polished") is None                        # ambiguous prefix
    assert find_item("") is None and find_item("nonsense") is None


def test_match_category():
    assert match_category("stone") == "stone"
    assert match_category("Sand, gravel, bricks & mud") == "earth"
    assert match_category("terr") == "terracotta"
    assert match_category("xyz") is None


def test_fmt_blocks():
    assert fmt_blocks(25) == "2.5 🟫"
    assert fmt_blocks(450) == "45 🟫"
    assert fmt_blocks(12600) == "1,260 🟫"
    assert fmt_blocks(0) == "0 🟫"


# ── console client ────────────────────────────────────────────────────────────

class _FakeStdout:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()

    async def readline(self):
        return await self._q.get()

    def feed(self, *lines):
        for line in lines:
            self._q.put_nowait(line + "\r\n")

    def feed_eof(self):
        self._q.put_nowait("")


class _FakeProc:
    """Scripted server: `replies` maps a sent command to the lines it prints
    (other log lines can be fed at any time)."""
    def __init__(self, replies):
        self.replies = replies
        self.stdout = _FakeStdout()
        self.sent: list[str] = []
        self.closed = False

        class _Stdin:
            def write(inner, text):
                self.sent.append(text.strip())
                for line in self.replies.get(text.strip(), ()):
                    self.stdout.feed(line)
        self.stdin = _Stdin()

    def close(self):
        self.closed = True
        self.stdout.feed_eof()


class _FakeConn:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    async def create_process(self, term_type=None):
        return self.proc

    def close(self):
        self.closed = True


def _console(replies) -> tuple[McConsole, _FakeProc]:
    proc = _FakeProc(replies)

    async def _connect(host, port, username, password, known_hosts):
        return _FakeConn(proc)
    return McConsole("bds", 2222, "pw", connect=_connect), proc


def test_clean_line_strips_ansi_and_cr():
    assert clean_line("\x1b[32mGave Stone * 64 to Steve\x1b[0m\r\n") == "Gave Stone * 64 to Steve"


def test_reply_patterns():
    assert GIVE_OK.match("Gave Stone * 64 to Steve Smith").group("count") == "64"
    assert CLEAR_OK.match("Cleared the inventory of Steve, removing 1 item").group("count") == "1"
    assert LIST_HEADER.match("There are 2/10 players online:").group("online") == "2"
    assert NO_TARGET.match("No targets matched selector")


async def test_give_matches_its_reply_between_log_lines(monkeypatch):
    monkeypatch.setattr(console_mod, "REPLY_TIMEOUT_SECS", 1.0)
    console, proc = _console({
        'give "Steve" minecraft:stone 64': [
            "[2026-09-23 10:00:00:000 INFO] Player connected: Alex, xuid: 1",
            "give \"Steve\" minecraft:stone 64",        # the PTY echo
            "Gave Stone * 64 to Steve",
        ],
    })
    assert await console.give("Steve", "stone", 64) == 64
    assert proc.sent == ['give "Steve" minecraft:stone 64']
    await console.close()


async def test_give_refuses_when_the_player_is_offline():
    console, _ = _console({'give "Steve" minecraft:stone 1': ["No targets matched selector"]})
    with pytest.raises(ConsoleRefused):
        await console.give("Steve", "stone", 1)
    await console.close()


async def test_clear_reports_removed_count_and_none():
    console, _ = _console({
        'clear "Steve" minecraft:coal -1 10': ["Cleared the inventory of Steve, removing 4 items"],
        'clear "Steve" minecraft:diamond -1 10': ["Could not clear the inventory of Steve, no items to remove"],
    })
    assert await console.clear("Steve", "coal", 10) == 4
    assert await console.clear("Steve", "diamond", 10) == 0
    await console.close()


async def test_list_reads_the_names_line():
    console, _ = _console({"list": ["There are 2/10 players online:", "Steve, Alex Jones"]})
    assert await console.online_players() == ["Steve", "Alex Jones"]
    await console.close()


async def test_list_with_nobody_online():
    console, _ = _console({"list": ["There are 0/10 players online:"]})
    assert await console.online_players() == []
    await console.close()


async def test_silence_is_a_timeout(monkeypatch):
    monkeypatch.setattr(console_mod, "REPLY_TIMEOUT_SECS", 0.05)
    console, _ = _console({})
    with pytest.raises(ConsoleError):
        await console.give("Steve", "stone", 1)
    await console.close()


async def test_tell_is_quiet_on_success_and_refuses_offline(monkeypatch):
    monkeypatch.setattr(console_mod, "SILENT_OK_SECS", 0.05)
    console, proc = _console({})
    await console.tell("Steve", 'code "A1"')
    assert proc.sent[-1].startswith('tellraw "Steve" {"rawtext":[{"text":"code \\"A1\\""}]}')
    proc.replies[proc.sent[-1]] = ["No targets matched selector"]
    with pytest.raises(ConsoleRefused):
        await console.tell("Steve", 'code "A1"')
    await console.close()


async def test_console_reconnects_after_the_session_drops(monkeypatch):
    monkeypatch.setattr(console_mod, "REPLY_TIMEOUT_SECS", 0.5)
    procs = []

    async def _connect(host, port, username, password, known_hosts):
        proc = _FakeProc({"list": ["There are 0/10 players online:"]})
        procs.append(proc)
        return _FakeConn(proc)
    console = McConsole("bds", 2222, "pw", connect=_connect)
    assert await console.online_players() == []
    procs[0].stdout.feed_eof()         # server closed the session
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not console.connected
    assert await console.online_players() == []
    assert len(procs) == 2
    await console.close()


async def test_commands_are_serialised(monkeypatch):
    """Two commands in flight would claim each other's replies; the lock
    makes the second wait for the first's answer."""
    monkeypatch.setattr(console_mod, "REPLY_TIMEOUT_SECS", 1.0)
    console, proc = _console({
        'give "A" minecraft:stone 1': ["Gave Stone * 1 to A"],
        'give "B" minecraft:sand 2': ["Gave Sand * 2 to B"],
    })
    a, b = await asyncio.gather(console.give("A", "stone", 1), console.give("B", "sand", 2))
    assert (a, b) == (1, 2)
    await console.close()


# ── cog flows ─────────────────────────────────────────────────────────────────

class _FakeShopConsole:
    """What the cog calls, scripted per test."""
    def __init__(self, *, online=("Steve",), give=None, clear=None):
        self.online = list(online)
        self._give = give
        self._clear = clear
        self.told: list[tuple[str, str]] = []
        self.gives: list[tuple] = []
        self.clears: list[tuple] = []

    async def online_players(self):
        return list(self.online)

    async def tell(self, gamertag, text):
        self.told.append((gamertag, text))

    async def give(self, gamertag, item_id, count):
        self.gives.append((gamertag, item_id, count))
        if isinstance(self._give, Exception):
            raise self._give
        return count if self._give is None else self._give

    async def clear(self, gamertag, item_id, count):
        self.clears.append((gamertag, item_id, count))
        if isinstance(self._clear, Exception):
            raise self._clear
        return count if self._clear is None else self._clear

    async def close(self):
        pass


def _cog(monkeypatch, console=None, *, on=True) -> MinecraftCog:
    monkeypatch.setattr(mc_mod, "MC_SERVER_HOST", "")
    cog = MinecraftCog(bot=None)
    cog.console = console if console is not None else _FakeShopConsole()
    if on:
        get_guild_cfg(GID)["mc_shop"] = True
    return cog


def _ctx(command: str, uid: int = UID) -> FakeCtx:
    return FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=GID), command_name=command)


def _link(uid: int = UID, gamertag: str = "Steve", blocks: int = 0):
    _state.mc_players[uid] = {"gamertag": gamertag, "blocks": blocks, "linked_at": 1}


async def test_shop_is_off_by_default(monkeypatch):
    cog = _cog(monkeypatch, on=False)
    ctx = _ctx("mc shop")
    await cog.cmd_mc_shop.callback(cog, ctx)
    assert "Turned Off" in ctx.sent_embeds[0].title
    assert "!settings minecraft-shop on" in ctx.sent_embeds[0].description


async def test_shop_needs_the_console_configured(monkeypatch):
    cog = _cog(monkeypatch)
    cog.console = None
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "stone")
    assert "isn't configured" in ctx.sent_embeds[0].description


async def test_shop_refuses_dms(monkeypatch):
    cog = _cog(monkeypatch)
    ctx = _ctx("mc sell")
    ctx.guild = None
    await cog.cmd_mc_sell.callback(cog, ctx, "coal")
    assert "not in DMs" in ctx.sent_embeds[0].description


async def test_shop_overview_and_category(monkeypatch):
    cog = _cog(monkeypatch)
    ctx = _ctx("mc shop")
    await cog.cmd_mc_shop.callback(cog, ctx)
    names = [f.name for f in ctx.sent_embeds[0].fields]
    assert "⛏️ Minerals & ores" in names and "🧱 Concrete" in names
    await cog.cmd_mc_shop.callback(cog, ctx, "minerals")
    text = "\n".join(f.value for f in ctx.sent_embeds[1].fields)
    assert "**Coal** · not for sale · sells for 2.5 🟫" in text
    await cog.cmd_mc_shop.callback(cog, ctx, "concrete")
    assert all(len(f.value) <= 1024 for f in ctx.sent_embeds[2].fields)
    await cog.cmd_mc_shop.callback(cog, ctx, "stone", "bricks")
    assert "**Stone Bricks** · buy 2 🟫 · not bought" in ctx.sent_embeds[3].description


async def test_link_sends_a_code_in_game_and_verify_links(monkeypatch):
    console = _FakeShopConsole(online=("Alex", "steve"))
    cog = _cog(monkeypatch, console)
    ctx = _ctx("mc link")
    await cog.cmd_mc_link.callback(cog, ctx, "Steve")
    assert console.told and console.told[0][0] == "steve"   # the server's spelling
    code = cog._link_codes[UID][1]
    assert code in console.told[0][1]
    assert "sent **steve** a code" in ctx.sent_embeds[0].description

    ctx2 = _ctx("mc verify")
    await cog.cmd_mc_verify.callback(cog, ctx2, "nope")
    assert "not the code" in ctx2.sent_embeds[0].description
    await cog.cmd_mc_verify.callback(cog, ctx2, code.lower())
    assert _state.mc_players[UID]["gamertag"] == "steve"
    assert UID not in cog._link_codes
    assert "Gamertag Linked" in ctx2.sent_embeds[1].title


async def test_link_refuses_offline_taken_and_already_linked(monkeypatch):
    console = _FakeShopConsole(online=("Alex",))
    cog = _cog(monkeypatch, console)
    ctx = _ctx("mc link")
    await cog.cmd_mc_link.callback(cog, ctx, "Steve")
    assert "isn't on the server" in ctx.sent_embeds[-1].description and not console.told

    _link(uid=99, gamertag="alex")
    await cog.cmd_mc_link.callback(cog, ctx, "Alex")
    assert "already linked to <@99>" in ctx.sent_embeds[-1].description

    _link(uid=UID, gamertag="Steve")
    await cog.cmd_mc_link.callback(cog, ctx, "Other")
    assert "already linked as **Steve**" in ctx.sent_embeds[-1].description


async def test_verify_code_expires(monkeypatch):
    cog = _cog(monkeypatch)
    cog._link_codes[UID] = ("Steve", "ABCDEF", time.time() - 1)
    ctx = _ctx("mc verify")
    await cog.cmd_mc_verify.callback(cog, ctx, "ABCDEF")
    assert "No code is waiting" in ctx.sent_embeds[0].description
    assert UID not in cog._link_codes and UID not in _state.mc_players


async def test_unlink_keeps_the_purse(monkeypatch):
    cog = _cog(monkeypatch)
    _link(blocks=120)
    ctx = _ctx("mc unlink")
    await cog.cmd_mc_unlink.callback(cog, ctx)
    assert _state.mc_players[UID] == {"gamertag": None, "blocks": 120, "linked_at": None}
    assert "12 🟫 stay" in ctx.sent_embeds[0].description


async def test_blocks_shows_purse_and_gamertag(monkeypatch):
    cog = _cog(monkeypatch)
    _link(blocks=1265)
    ctx = _ctx("mc blocks")
    await cog.cmd_mc_blocks.callback(cog, ctx)
    assert "**126.5 🟫**" in ctx.sent_embeds[0].description and "**Steve**" in ctx.sent_embeds[0].description
    await cog.cmd_mc_blocks.callback(cog, ctx, FakeMember(uid=5, display_name="Nobody"))
    assert "Nobody's Blocks" in ctx.sent_embeds[1].title and "**0 🟫**" in ctx.sent_embeds[1].description


async def test_buy_charges_and_gives(monkeypatch):
    console = _FakeShopConsole()
    cog = _cog(monkeypatch, console)
    _link(blocks=2000)
    saved = []

    async def _save(uid):
        saved.append(dict(_state.mc_players[uid]))
    monkeypatch.setattr(_persistence, "save_mc_player", _save)
    logged = []

    async def _log(**kw):
        logged.append(kw)
    monkeypatch.setattr(_persistence, "log_mc_trade", _log)

    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "stone", "bricks", "64")
    assert console.gives == [("Steve", "stone_bricks", 64)]
    assert _state.mc_players[UID]["blocks"] == 2000 - 20 * 64
    assert saved[0]["blocks"] == 2000 - 20 * 64          # charged before the give
    assert logged[0]["kind"] == "buy" and logged[0]["count"] == 64 and logged[0]["blocks"] == 1280
    assert "**64 × Stone Bricks** given to **Steve** for 128 🟫" in ctx.sent_embeds[0].description


async def test_buy_count_first_and_default_one(monkeypatch):
    console = _FakeShopConsole()
    cog = _cog(monkeypatch, console)
    _link(blocks=1000)
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "3", "glass")
    await cog.cmd_mc_buy.callback(cog, ctx, "sand")
    assert console.gives == [("Steve", "glass", 3), ("Steve", "sand", 1)]


async def test_buy_refuses_minerals_unknown_items_and_short_purses(monkeypatch):
    console = _FakeShopConsole()
    cog = _cog(monkeypatch, console)
    _link(blocks=10)
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "diamond")
    assert "isn't for sale" in ctx.sent_embeds[-1].description
    await cog.cmd_mc_buy.callback(cog, ctx, "iron", "block")
    assert "I don't know" in ctx.sent_embeds[-1].description
    await cog.cmd_mc_buy.callback(cog, ctx, "glass", "64")
    assert "costs 192 🟫; you have 1 🟫" in ctx.sent_embeds[-1].description
    await cog.cmd_mc_buy.callback(cog, ctx, "glass", "0")
    assert "at least 1" in ctx.sent_embeds[-1].description
    assert console.gives == [] and _state.mc_players[UID]["blocks"] == 10


async def test_buy_needs_a_linked_gamertag(monkeypatch):
    cog = _cog(monkeypatch)
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "glass")
    assert "Link your gamertag first" in ctx.sent_embeds[0].description


async def test_buy_refunds_when_the_server_refuses(monkeypatch):
    console = _FakeShopConsole(give=ConsoleRefused("No targets matched selector"))
    cog = _cog(monkeypatch, console)
    _link(blocks=100)
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "glass", "2")
    assert _state.mc_players[UID]["blocks"] == 100
    assert "not on the server right now" in ctx.sent_embeds[0].description

    console._give = ConsoleError("down")
    await cog.cmd_mc_buy.callback(cog, ctx, "glass", "2")
    assert _state.mc_players[UID]["blocks"] == 100
    assert "couldn't reach the server console" in ctx.sent_embeds[1].description

    console._give = ConsoleRefused('Syntax error: Unexpected "x"')
    await cog.cmd_mc_buy.callback(cog, ctx, "glass", "2")
    assert "The server refused: `Syntax error" in ctx.sent_embeds[2].description
    assert _state.mc_players[UID]["blocks"] == 100


async def test_concurrent_buys_cannot_overdraw(monkeypatch):
    """The charge lands before the console call, so two buys racing on one
    purse can't both pass the balance check."""
    class _SlowConsole(_FakeShopConsole):
        async def give(self, gamertag, item_id, count):
            await asyncio.sleep(0)
            return await super().give(gamertag, item_id, count)
    console = _SlowConsole()
    cog = _cog(monkeypatch, console)
    _link(blocks=30)   # 3 blocks: one 3-block glass, not two
    ctx1, ctx2 = _ctx("mc buy"), _ctx("mc buy")
    await asyncio.gather(cog.cmd_mc_buy.callback(cog, ctx1, "glass"), cog.cmd_mc_buy.callback(cog, ctx2, "glass"))
    assert len(console.gives) == 1
    assert _state.mc_players[UID]["blocks"] == 0
    titles = sorted(e.title for e in ctx1.sent_embeds + ctx2.sent_embeds)
    assert titles == ["⛏️ Bought", "⛏️ Buy"]


async def test_buy_caps_the_count(monkeypatch):
    console = _FakeShopConsole()
    cog = _cog(monkeypatch, console)
    _link(blocks=10 ** 9)
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx, "sand", "1m")
    assert console.gives == [("Steve", "sand", MAX_TRADE_COUNT)]


async def test_sell_credits_what_was_actually_removed(monkeypatch):
    console = _FakeShopConsole(clear=4)
    cog = _cog(monkeypatch, console)
    _link(blocks=0)
    logged = []

    async def _log(**kw):
        logged.append(kw)
    monkeypatch.setattr(_persistence, "log_mc_trade", _log)
    ctx = _ctx("mc sell")
    await cog.cmd_mc_sell.callback(cog, ctx, "coal", "10")
    assert console.clears == [("Steve", "coal", 10)]
    assert _state.mc_players[UID]["blocks"] == 4 * 25
    assert logged[0]["kind"] == "sell" and logged[0]["count"] == 4 and logged[0]["blocks"] == 100
    assert "**4 × Coal** taken from **Steve** for 10 🟫 (you had 4, not 10)" in ctx.sent_embeds[0].description


async def test_sell_all_and_nothing_to_sell(monkeypatch):
    console = _FakeShopConsole(clear=0)
    cog = _cog(monkeypatch, console)
    _link(blocks=5)
    ctx = _ctx("mc sell")
    await cog.cmd_mc_sell.callback(cog, ctx, "all", "diamond")
    assert console.clears == [("Steve", "diamond", MAX_TRADE_COUNT)]
    assert "don't have any **Diamond**" in ctx.sent_embeds[0].description
    assert _state.mc_players[UID]["blocks"] == 5


async def test_sell_refuses_farm_products_and_buy_only_blocks(monkeypatch):
    console = _FakeShopConsole()
    cog = _cog(monkeypatch, console)
    _link()
    ctx = _ctx("mc sell")
    await cog.cmd_mc_sell.callback(cog, ctx, "cobblestone", "64")
    assert "doesn't buy **Cobblestone**" in ctx.sent_embeds[0].description
    await cog.cmd_mc_sell.callback(cog, ctx, "white wool")
    assert "doesn't buy **White Wool**" in ctx.sent_embeds[1].description
    assert console.clears == []


async def test_sell_reports_a_refusal_without_crediting(monkeypatch):
    console = _FakeShopConsole(clear=ConsoleRefused("No targets matched selector"))
    cog = _cog(monkeypatch, console)
    _link(blocks=0)
    ctx = _ctx("mc sell")
    await cog.cmd_mc_sell.callback(cog, ctx, "coal")
    assert _state.mc_players[UID]["blocks"] == 0
    assert "not on the server right now" in ctx.sent_embeds[0].description


async def test_bare_buy_opens_a_form(monkeypatch):
    console = _FakeShopConsole()
    cog = _cog(monkeypatch, console)
    _link(blocks=1000)

    async def _form(ctx, *, title, description, fields, **kw):
        assert [f.key for f in fields] == ["item", "count"]
        return {"item": "glass", "count": "5"}
    monkeypatch.setattr(mc_mod, "open_form", _form)
    ctx = _ctx("mc buy")
    await cog.cmd_mc_buy.callback(cog, ctx)
    assert console.gives == [("Steve", "glass", 5)]

    async def _dismissed(*a, **kw):
        return None
    monkeypatch.setattr(mc_mod, "open_form", _dismissed)
    await cog.cmd_mc_sell.callback(cog, ctx)
    assert console.clears == []


# ── settings ──────────────────────────────────────────────────────────────────

async def test_settings_minecraft_shop_toggles_and_warns_about_the_console(monkeypatch):
    import src.cogs.settings_cog as settings_mod

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(settings_mod, "save_guild_settings", _noop)
    monkeypatch.setattr(settings_mod, "MC_CONSOLE_HOST", "")
    cog = SettingsCog(bot=None)
    ctx = FakeCtx(author=FakeMember(1, administrator=True), guild=FakeGuild(gid=GID), command_name="settings minecraft-shop")
    assert not get_guild_cfg(GID).get("mc_shop")
    await cog.settings_minecraft_shop.callback(cog, ctx, "on")
    assert get_guild_cfg(GID)["mc_shop"] is True
    assert "console isn't configured" in ctx.sent_embeds[-1].description
    monkeypatch.setattr(settings_mod, "MC_CONSOLE_HOST", "bds")
    monkeypatch.setattr(settings_mod, "MC_CONSOLE_PASSWORD", "pw")
    await cog.settings_minecraft_shop.callback(cog, ctx, "off")
    assert get_guild_cfg(GID)["mc_shop"] is False
    assert "console isn't configured" not in ctx.sent_embeds[-1].description
    await cog.settings_minecraft_shop.callback(cog, ctx, "maybe")
    assert "Usage:" in ctx.sent_embeds[-1].description


async def test_settings_panel_and_overview_list_the_shop(monkeypatch):
    ctx = FakeCtx(author=FakeMember(1, administrator=True), guild=FakeGuild(gid=GID), command_name="settings")
    items = {i.key: i for i in items_for("games", ctx, models=None, bot=None)}
    assert items["mc-shop"].value == "❌ off" and items["mc-shop"].args == ("on",)
    get_guild_cfg(GID)["mc_shop"] = True
    items = {i.key: i for i in items_for("games", ctx, models=None, bot=None)}
    assert items["mc-shop"].value == "✅ on" and items["mc-shop"].args == ("off",)
    embed = SettingsCog(bot=None)._overview_embed(ctx)
    other = next(f.value for f in embed.fields if f.name == "🎛️ Other")
    assert "⛏️ Minecraft block shop: ✅ on" in other


# ── persistence ───────────────────────────────────────────────────────────────

async def test_mc_players_round_trip(db):
    _state.mc_players[UID] = {"gamertag": "Steve", "blocks": 125, "linked_at": 1700000000}
    _state.mc_players[8] = {"gamertag": None, "blocks": 30, "linked_at": None}
    await _persistence.save_mc_player(UID)
    await _persistence.save_mc_player(8)
    await _persistence.log_mc_trade(ts=1, user_id=UID, guild_id=GID, gamertag="Steve", kind="sell",
                                    item_id="coal", count=4, blocks=100)
    _state.mc_players.clear()
    await _persistence.init_db_state()
    assert _state.mc_players[UID] == {"gamertag": "Steve", "blocks": 125, "linked_at": 1700000000}
    assert _state.mc_players[8] == {"gamertag": None, "blocks": 30, "linked_at": None}
    async with _persistence.with_cursor() as cur:
        await cur.execute("SELECT kind, item_id, count, blocks FROM mc_trades")
        assert await cur.fetchall() == [("sell", "coal", 4, 100)]
