import os
import json
import random
import shutil
import asyncio
import aiohttp
import discord
import threading
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta
from itertools import combinations

# =====================================================================
#  CONFIGURATION  (replace with your actual tokens)
# =====================================================================
TOKEN          = os.getenv("DISCORD_BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "YOUR_OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
WC_CHANNEL_ID  = 1516571809707724962
WC_ROLE_ID     = 1516569818814353574
WINNER_ROLE_ID = os.getenv("WINNER_ROLE_ID", "YOUR_ACTUAL_WINNER_ROLE_ID")  # MUST be different from WC_ROLE_ID
WC_EMOJI       = "<:2026worldcuplogo:1517178189258952704>"
DATA_FOLDER    = "database"

STATUS_OPEN = "OPEN"
STATUS_LOCKED = "LOCKED"
STATUS_FINISHED = "FINISHED"

# =====================================================================
#  48 OFFICIAL FIFA WORLD CUP 2026 NATIONS
# =====================================================================
def code_to_flag(code):
    return "".join(chr(127397 + ord(c)) for c in code.upper())

def subdivision_flag(region):
    flag = chr(0x1F3F4)
    for c in region:
        flag += chr(0xE0000 + ord(c))
    flag += chr(0xE007F)
    return flag

ENGLAND_FLAG  = subdivision_flag("gbeng")
SCOTLAND_FLAG = subdivision_flag("gbsct")

NATIONS = {
    "Algeria": "DZ",      "Argentina": "AR",    "Australia": "AU",
    "Austria": "AT",      "Belgium": "BE",      "Bosnia and Herzegovina": "BA",
    "Brazil": "BR",       "Canada": "CA",       "Cape Verde": "CV",
    "Colombia": "CO",     "Croatia": "HR",      "Curacao": "CW",
    "Czechia": "CZ",      "DR Congo": "CD",     "Ecuador": "EC",
    "Egypt": "EG",        "England": "ENG",     "France": "FR",
    "Germany": "DE",      "Ghana": "GH",        "Haiti": "HT",
    "Iran": "IR",         "Iraq": "IQ",         "Ivory Coast": "CI",
    "Japan": "JP",        "Jordan": "JO",       "Mexico": "MX",
    "Morocco": "MA",      "Netherlands": "NL",  "New Zealand": "NZ",
    "Norway": "NO",       "Panama": "PA",       "Paraguay": "PY",
    "Portugal": "PT",     "Qatar": "QA",        "Saudi Arabia": "SA",
    "Scotland": "SCO",    "Senegal": "SN",      "South Africa": "ZA",
    "South Korea": "KR",  "Spain": "ES",        "Sweden": "SE",
    "Switzerland": "CH",  "Tunisia": "TN",      "Turkiye": "TR",
    "Uruguay": "UY",      "USA": "US",          "Uzbekistan": "UZ",
}

def flag_for(country):
    if country == "England":  return ENGLAND_FLAG
    if country == "Scotland": return SCOTLAND_FLAG
    code = NATIONS.get(country)
    if code: return code_to_flag(code)
    return "?"

SORTED_NAMES  = sorted(NATIONS.keys())
PAGE_1 = SORTED_NAMES[:20]
PAGE_2 = SORTED_NAMES[20:40]
PAGE_3 = SORTED_NAMES[40:]

FLAG_TO_COUNTRY = {}
for name in NATIONS:
    f = flag_for(name)
    if f != "?":
        FLAG_TO_COUNTRY[f] = name

KNOCKOUT_DATES = {
    "R32": ["2026-07-01"],
    "R16": ["2026-07-05"],
    "QF": ["2026-07-09"],
    "SF": ["2026-07-13"],
    "FINAL": ["2026-07-17"]
}

# =====================================================================
#  OFFICIAL GROUPS  (FIFA World Cup 2026 Final Draw)
# =====================================================================
GROUPS = {
    "A": ["Mexico",      "South Africa",            "South Korea", "Czechia"],
    "B": ["Canada",      "Bosnia and Herzegovina",  "Qatar",       "Switzerland"],
    "C": ["Brazil",      "Morocco",                 "Haiti",       "Scotland"],
    "D": ["USA",         "Paraguay",                "Australia",   "Turkiye"],
    "E": ["Germany",     "Curacao",                 "Ivory Coast", "Ecuador"],
    "F": ["Netherlands", "Japan",                   "Sweden",      "Tunisia"],
    "G": ["Belgium",     "Egypt",                   "Iran",        "New Zealand"],
    "H": ["Spain",       "Cape Verde",              "Saudi Arabia","Uruguay"],
    "I": ["France",      "Senegal",                 "Iraq",        "Norway"],
    "J": ["Argentina",   "Algeria",                 "Austria",     "Jordan"],
    "K": ["Portugal",    "DR Congo",                "Uzbekistan",  "Colombia"],
    "L": ["England",     "Croatia",                 "Ghana",       "Panama"],
}

GROUP_VENUES = {
    "A": "Mexico City",
    "B": "Vancouver",
    "C": "Atlanta",
    "D": "Inglewood",
    "E": "Houston",
    "F": "Monterrey",
    "G": "Seattle",
    "H": "Miami",
    "I": "Philadelphia",
    "J": "Kansas City",
    "K": "Arlington",
    "L": "Toronto",
}

BASE_TIMES = ["17:00", "20:00", "23:00"]

def generate_group_matches(groups, start_date="2026-06-11"):
    schedule = []
    start = datetime.fromisoformat(start_date)

    for group, teams in groups.items():
        matches = list(combinations(teams, 2))

        day_offset = 0
        time_index = 0

        for i, (home, away) in enumerate(matches):
            match_date = start + timedelta(days=day_offset)
            kickoff_time = BASE_TIMES[time_index % len(BASE_TIMES)]

            schedule.append((
                f"{match_date.date()}T{kickoff_time}",
                group,
                home,
                away,
                GROUP_VENUES.get(group, "TBD")
            ))

            time_index += 1

            if (i + 1) % 3 == 0:
                day_offset += 1

    return schedule

# =====================================================================
#  DATABASE (thread-safe using threading.RLock for sync file ops)
# =====================================================================
class DB:
    _lock = threading.RLock()

    @staticmethod
    def ensure():
        with DB._lock:
            os.makedirs(DATA_FOLDER, exist_ok=True)
            defaults = {
                "players.json": {},
                "matches.json": {},
                "predictions.json": {},
                "tournament.json": {"stage": "registration", "groups": {}, "champion": None},
                "settings.json": {
                    "match_counter": 0,
                    "leaderboard_msg_id": None,
                    "country_messages": []
                },
            }
            for name, val in defaults.items():
                path = os.path.join(DATA_FOLDER, name)
                if not os.path.exists(path):
                    with open(path, "w") as f:
                        json.dump(val, f, indent=4)

    @staticmethod
    def load(file):
        with DB._lock:
            with open(os.path.join(DATA_FOLDER, file)) as f:
                return json.load(f)

    @staticmethod
    def save(file, data):
        with DB._lock:
            with open(os.path.join(DATA_FOLDER, file), "w") as f:
                json.dump(data, f, indent=4)

# =====================================================================
#  BOT SETUP
# =====================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")
LEADERBOARD_MSG_ID = None
COUNTRY_MSG_IDS    = []
aiohttp_session = None  # Global session for API calls

# =====================================================================
#  HELPERS
# =====================================================================
def parse_score(text):
    try:
        parts = text.strip().replace(" ", "").split("-")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None

def _parse_kickoff_for_sort(m):
    try:
        return datetime.fromisoformat(m.get("kickoff", "9999-12-31T00:00"))
    except Exception:
        return datetime.max

def get_next_match():
    matches = DB.load("matches.json")
    candidates = [(mid, m) for mid, m in matches.items()
                  if m["status"] == STATUS_OPEN and not m.get("thread_id")]
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: _parse_kickoff_for_sort(x[1]))
    return candidates[0]

# =====================================================================
#  AI ENGINE  (OpenRouter / commentary / analysis / headlines)
# =====================================================================
async def ask_ai(prompt):
    global aiohttp_session
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    payload = {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": prompt}]}
    try:
        if aiohttp_session is None or aiohttp_session.closed:
            aiohttp_session = aiohttp.ClientSession()
        async with aiohttp_session.post(OPENROUTER_URL, headers=headers, json=payload, timeout=15) as r:
            data = await r.json()
            try:
                return data["choices"][0]["message"]["content"]
            except Exception:
                return "AI unavailable."
    except Exception:
        return "AI unavailable."

async def ai_hype(home, away, stage="Group Stage"):
    return await ask_ai(
        f"Write a short, hype FIFA World Cup 2026 {stage} match preview "
        f"for {home} vs {away}. Sound like a professional broadcaster. "
        "Max 3 sentences. No hashtags."
    )

async def ai_commentary(home, away, score):
    return await ask_ai(
        f"Write a short post-match reaction for {home} {score} {away} "
        "at the 2026 FIFA World Cup. Sound like a journalist. Max 3 sentences."
    )

async def ai_headline(home, away, score):
    return await ask_ai(
        f"Write one dramatic World Cup news headline for the result "
        f"{home} {score} {away}. Max 15 words. No quotes."
    )

async def ai_analysis(country):
    return await ask_ai(
        f"Give a brief 2-sentence tactical analysis of {country}'s "
        "World Cup 2026 campaign so far. Sound like a pundit."
    )

# =====================================================================
#  POINTS ENGINE  (+2 correct outcome, +3 bonus exact score)
# =====================================================================
def process_result(match_id, hg, ag):
    players = DB.load("players.json")
    preds   = DB.load("predictions.json").get(match_id, {})
    matches = DB.load("matches.json")
    match   = matches.get(match_id)
    if not match: return
    actual = "home" if hg > ag else ("away" if ag > hg else "draw")
    for uid, pred in preds.items():
        p = players.get(uid)
        if not p: continue
        predicted = "home" if pred["home"] > pred["away"] else (
            "away" if pred["away"] > pred["home"] else "draw")
        if predicted == actual:
            p["points"] = p.get("points", 0) + 2
        if pred["home"] == hg and pred["away"] == ag:
            p["points"] = p.get("points", 0) + 3
    match["score"]  = f"{hg}-{ag}"
    match["status"] = STATUS_FINISHED
    matches[match_id] = match
    DB.save("matches.json", matches)
    DB.save("players.json", players)

# =====================================================================
#  TOURNAMENT ENGINE  (real schedule, deterministic bracket)
# =====================================================================
def load_schedule(force=False):
    settings = DB.load("settings.json")

    # ALWAYS wipe old fixtures when regenerating
    if force:
        DB.save("matches.json", {})
        settings["match_counter"] = 0
        DB.save("settings.json", settings)

    matches = DB.load("matches.json")

    if matches and len(matches) > 0 and not force:
        return len(matches)

    schedule = generate_group_matches(GROUPS)
    created = 0

    for dt, grp, home, away, city in schedule:
        settings["match_counter"] = settings.get("match_counter", 0) + 1
        mid = str(settings["match_counter"])

        matches[mid] = {
            "home": home,
            "away": away,
            "kickoff": dt,
            "status": STATUS_OPEN,
            "score": None,
            "thread_id": None,
            "stage": "GROUP",
            "group": grp,
            "city": city,
            "announced": False,
        }

        created += 1

    DB.save("matches.json", matches)
    DB.save("settings.json", settings)

    return created


def init_group_tables():
    t = DB.load("tournament.json")
    t["groups"] = {}
    for g, teams in GROUPS.items():
        table = {}
        for team in teams:
            table[team] = {"played":0,"won":0,"drawn":0,"lost":0,"gf":0,"ga":0,"gd":0,"points":0}
        t["groups"][g] = table
    t["stage"] = "GROUP"
    DB.save("tournament.json", t)

def update_group_table(match_id, hg, ag):
    matches = DB.load("matches.json")
    m = matches.get(match_id)
    if not m or m.get("stage") != "GROUP": return
    t = DB.load("tournament.json")
    g = m.get("group")
    if not g or g not in t["groups"]: return
    tb = t["groups"][g]
    h, a = m["home"], m["away"]
    if h not in tb or a not in tb: return
    tb[h]["played"] += 1; tb[a]["played"] += 1
    tb[h]["gf"] += hg; tb[h]["ga"] += ag
    tb[a]["gf"] += ag; tb[a]["ga"] += hg
    tb[h]["gd"] = tb[h]["gf"] - tb[h]["ga"]
    tb[a]["gd"] = tb[a]["gf"] - tb[a]["ga"]
    if hg > ag:
        tb[h]["won"] += 1; tb[h]["points"] += 3; tb[a]["lost"] += 1
    elif ag > hg:
        tb[a]["won"] += 1; tb[a]["points"] += 3; tb[h]["lost"] += 1
    else:
        tb[h]["drawn"] += 1; tb[h]["points"] += 1
        tb[a]["drawn"] += 1; tb[a]["points"] += 1
    DB.save("tournament.json", t)

def group_standings(group):
    t = DB.load("tournament.json")
    tb = t["groups"].get(group, {})
    return sorted(tb.items(), key=lambda x: (x[1]["points"],x[1]["gd"],x[1]["gf"]), reverse=True)

def get_qualifiers():
    """Top 2 per group + 8 best thirds = 32 teams."""
    t = DB.load("tournament.json")
    direct, thirds = [], []
    for g in sorted(t["groups"].keys()):
        s = group_standings(g)
        if len(s) >= 2: direct += [s[0][0], s[1][0]]
        if len(s) >= 3: thirds.append(s[2])
    thirds.sort(key=lambda x: (x[1]["points"],x[1]["gd"],x[1]["gf"]), reverse=True)
    return direct + [x[0] for x in thirds[:8]]

def create_knockout(teams, stage_code):
    """Deterministic seeded bracket  1 vs 32, 2 vs 31, etc."""
    settings = DB.load("settings.json")
    matches  = DB.load("matches.json")
    dates    = KNOCKOUT_DATES.get(stage_code, [])
    created  = 0
    n = len(teams)
    for i in range(n // 2):
        settings["match_counter"] = settings.get("match_counter", 0) + 1
        mid = str(settings["match_counter"])
        day = dates[created % len(dates)] if dates else "2026-07-01"
        kickoff = f"{day}T{17 + (created % 3) * 3:02d}:00"
        matches[mid] = {
            "home": teams[i], "away": teams[n - 1 - i],
            "kickoff": kickoff, "status": STATUS_OPEN,
            "score": None, "thread_id": None, "stage": stage_code,
            "announced": False,
        }
        created += 1
    DB.save("matches.json", matches)
    DB.save("settings.json", settings)
    return created

def match_winner(m):
    if not m.get("score"): return None
    h, a = map(int, m["score"].split("-"))
    if h > a: return m["home"]
    if a > h: return m["away"]
    return random.choice([m["home"], m["away"]])

def stage_done(code):
    ms = DB.load("matches.json")
    sm = [m for m in ms.values() if m.get("stage") == code]
    return bool(sm) and all(m["status"] == STATUS_FINISHED for m in sm)

def advance_tournament():
    ms = DB.load("matches.json")
    t  = DB.load("tournament.json")
    order = ["GROUP","R32","R16","QF","SF","3RD","FINAL"]
    nexts = {"GROUP":"R32","R32":"R16","R16":"QF","QF":"SF","SF":"FINAL"}
    for stg in order:
        if not stage_done(stg): continue
        nxt = nexts.get(stg)
        if nxt and any(m.get("stage") == nxt for m in ms.values()): continue
        if stg == "GROUP":
            q = get_qualifiers()
            if q:
                create_knockout(q, "R32")
                t["stage"] = "R32"
        elif stg == "SF":
            sf = [m for m in ms.values() if m.get("stage") == "SF"]
            winners = [match_winner(m) for m in sf if match_winner(m)]
            losers  = []
            for m in sf:
                w = match_winner(m)
                if not w: continue
                losers.append(m["home"] if w == m["away"] else m["away"])
            if len(losers) >= 2:
                create_knockout(losers, "3RD")
            if len(winners) >= 2:
                create_knockout(winners, "FINAL")
                t["stage"] = "FINAL"
        elif stg == "FINAL":
            # Final is complete, tournament finished
            t["stage"] = "FINISHED"
        else:
            sm = [m for m in ms.values() if m.get("stage") == stg]
            winners = [match_winner(m) for m in sm if match_winner(m)]
            if winners:
                create_knockout(winners, nxt)
                t["stage"] = nxt
        DB.save("tournament.json", t)
        return True
    return False

# =====================================================================
#  ANNOUNCE HELPER  (used by .start, auto-announce, and post-result)
# =====================================================================
async def announce_match(mid, match):
    """Post match embed + prediction thread in the WC channel."""
    channel = bot.get_channel(WC_CHANNEL_ID)
    if not channel: return
    home, away = match["home"], match["away"]
    stage_label = match.get("stage", "GROUP")
    if match.get("group"): stage_label = f"Group {match['group']}"
    hype = await ai_hype(home, away, stage_label)
    try:
        kickoff_ts = int(datetime.fromisoformat(match["kickoff"]).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        kickoff_ts = 0
    embed = discord.Embed(title=f"{WC_EMOJI}  MATCH ANNOUNCEMENT", description=hype, color=0xFFCC00)
    embed.add_field(name=f"{flag_for(home)} Home", value=home, inline=True)
    embed.add_field(name="vs", value="\u2694\uFE0F", inline=True)
    embed.add_field(name=f"{flag_for(away)} Away", value=away, inline=True)
    embed.add_field(name="Kickoff", value=f"<t:{kickoff_ts}:R>", inline=False)
    embed.add_field(name="Predictions", value="Submit your score in the thread!", inline=False)
    if match.get("city"):
        embed.set_footer(text=f"VTX WC 2026  {stage_label}  #{mid}  {match['city']}")
    else:
        embed.set_footer(text=f"VTX WC 2026  {stage_label}  #{mid}")
    msg = await channel.send(f"<@&{WC_ROLE_ID}>", embed=embed)
    thread = await msg.create_thread(name=f"{flag_for(home)} {home} vs {away} {flag_for(away)}")
    await thread.send(
        f"Submit your prediction for **{home} vs {away}**!\n"
        "Format: 2-1  or  0-0\n"
        "Predictions lock automatically at kickoff."
    )
    matches = DB.load("matches.json")
    matches[mid]["thread_id"] = thread.id
    matches[mid]["announced"] = True
    DB.save("matches.json", matches)

# =====================================================================
#  EVENTS
# =====================================================================
@bot.event
async def on_ready():
    global LEADERBOARD_MSG_ID, COUNTRY_MSG_IDS, aiohttp_session
    DB.ensure()
    settings = DB.load("settings.json")
    LEADERBOARD_MSG_ID = settings.get("leaderboard_msg_id")
    COUNTRY_MSG_IDS = settings.get("country_messages", [])
    if aiohttp_session is None or aiohttp_session.closed:
        aiohttp_session = aiohttp.ClientSession()
    if not autolock.is_running():  autolock.start()
    if not autolb.is_running():    autolb.start()
    if not autoannounce.is_running(): autoannounce.start()
    print(f"VTX 2026 FIFA World Cup Bot  ONLINE")

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or reaction.message.id not in COUNTRY_MSG_IDS: return
    flag = str(reaction.emoji)
    country = FLAG_TO_COUNTRY.get(flag)
    if not country:
        try: await reaction.remove(user)
        except Exception: pass
        return
    players = DB.load("players.json")
    uid = str(user.id)
    if uid in players and players[uid].get("country"):
        try: await reaction.remove(user)
        except Exception: pass
        return
    for p in players.values():
        if p.get("country") == country:
            try: await reaction.remove(user)
            except Exception: pass
            return
    if uid not in players:
        players[uid] = {"country":None,"points":0,"correct_predictions":0,"exact_scores":0}
    players[uid]["country"] = country
    DB.save("players.json", players)
    try:
        await user.send(f"{flag_for(country)} You claimed **{country}** for VTX World Cup 2026!")
    except Exception:
        pass

@bot.event
async def on_message(message):
    if message.author.bot: return
    await bot.process_commands(message)
    matches = DB.load("matches.json")
    for mid, m in matches.items():
        if m.get("thread_id") == message.channel.id and m["status"] == STATUS_OPEN:
            score = parse_score(message.content)
            if not score:
                continue
            h, a = score
            # Check if user has registered and claimed a country
            players = DB.load("players.json")
            uid = str(message.author.id)
            if uid not in players or not players[uid].get("country"):
                try:
                    await message.reply("⚠️ Claim a country first before making predictions!")
                except Exception:
                    pass
                continue
            preds = DB.load("predictions.json")
            preds.setdefault(mid, {})[uid] = {"home": h, "away": a}
            DB.save("predictions.json", preds)
            try:
                await message.add_reaction("\u2705")
            except Exception:
                pass
            return

# =====================================================================
#  COMMANDS  (short, simple, no underscores)
# =====================================================================
@bot.command()
@commands.has_permissions(administrator=True)
async def countries(ctx):
    """Post 3 country-selection embeds (20/20/8 flags)."""
    global COUNTRY_MSG_IDS
    if COUNTRY_MSG_IDS:
        return await ctx.send("⚠️ Country selection already exists. Use `.reset` to clear and start over.")
    COUNTRY_MSG_IDS = []
    pages = [
        ("Country Selection  Page 1/3", PAGE_1),
        ("Country Selection  Page 2/3", PAGE_2),
        ("Country Selection  Page 3/3", PAGE_3),
    ]
    for title, names in pages:
        lines = [f"{flag_for(n)} {n}" for n in names]
        embed = discord.Embed(
            title=f"{WC_EMOJI} {title}",
            description="React with a flag to claim your nation.\n\n" + "\n".join(lines),
            color=0xFFD700,
        )
        embed.set_footer(text="VTX World Cup 2026  48 Nations, One Champion")
        msg = await ctx.send(embed=embed)
        COUNTRY_MSG_IDS.append(msg.id)
        for n in names:
            try: await msg.add_reaction(flag_for(n))
            except Exception: pass
        await asyncio.sleep(1)
    # Persist country message IDs
    settings = DB.load("settings.json")
    settings["country_messages"] = COUNTRY_MSG_IDS
    DB.save("settings.json", settings)

@bot.command()
@commands.has_permissions(administrator=True)
async def schedule(ctx):
    """Load ALL group-stage fixtures from the FIFA schedule."""
    t = DB.load("tournament.json")
    # Only initialize groups if they don't exist yet
    if not t.get("groups"):
        init_group_tables()
    # Force wipe old matches and reset settings to ensure fresh schedule
    DB.save("matches.json", {})
    DB.save("settings.json", {
        "match_counter": 0,
        "leaderboard_msg_id": None,
        "country_messages": []
    })
    count = load_schedule(force=True)
    channel = bot.get_channel(WC_CHANNEL_ID)
    if channel:
        await channel.send(
            f"<@&{WC_ROLE_ID}> {WC_EMOJI} The **2026 FIFA World Cup** schedule has been loaded!\n"
            f"**{count}** group-stage fixtures."
        )
    await ctx.send(f"{WC_EMOJI} Loaded **{count}** fixtures!")

@bot.command()
@commands.has_permissions(administrator=True)
async def start(ctx):
    """Announce the next upcoming match."""
    mid, match = get_next_match()
    if not mid:
        return await ctx.send("No upcoming matches to announce.")
    await announce_match(mid, match)
    await ctx.send(f"Announced match #{mid}: {match['home']} vs {match['away']}")

@bot.command()
@commands.has_permissions(administrator=True)
async def result(ctx, mid: str, score: str):
    """Record the real result. Usage: .result 1 2-1"""
    parsed = parse_score(score)
    if not parsed: return await ctx.send("Invalid format. Use: .result 1 2-1")
    hg, ag = parsed
    matches = DB.load("matches.json")
    match = matches.get(mid)
    if not match: return await ctx.send(f"Match #{mid} not found.")
    if match["status"] == STATUS_FINISHED: return await ctx.send("Already finished.")
    if match.get("score"): return await ctx.send("Score already recorded.")
    home, away = match["home"], match["away"]
    process_result(mid, hg, ag)
    update_group_table(mid, hg, ag)
    # Archive thread (safe check)
    if match.get("thread_id"):
        try:
            thread = await bot.fetch_channel(int(match["thread_id"]))
            if isinstance(thread, discord.Thread):
                try:
                    await thread.send(f"FULL TIME: **{home} {hg}-{ag} {away}**")
                except Exception:
                    pass
                if not getattr(thread, "locked", False):
                    try:
                        await thread.edit(locked=True, archived=True)
                    except Exception:
                        pass
        except Exception:
            pass
    # AI commentary + headline posted to WC channel
    channel = bot.get_channel(WC_CHANNEL_ID)
    commentary = await ai_commentary(home, away, f"{hg}-{ag}")
    headline   = await ai_headline(home, away, f"{hg}-{ag}")
    embed = discord.Embed(
        title=f"{WC_EMOJI}  {headline}",
        description=commentary,
        color=0x00FF88,
    )
    embed.add_field(
        name="Result",
        value=f"{flag_for(home)} **{home}** {hg} - {ag} **{away}** {flag_for(away)}",
        inline=False,
    )
    if channel:
        await channel.send(embed=embed)
    else:
        await ctx.send(embed=embed)
    # Advance tournament if stage complete
    advance_tournament()
    # Update leaderboard
    await update_leaderboard()
    # AUTO-ANNOUNCE next match
    next_mid, next_match = get_next_match()
    if next_mid:
        now = datetime.now(timezone.utc)
        try:
            ko = datetime.fromisoformat(next_match["kickoff"]).replace(tzinfo=timezone.utc)
            hours_until = (ko - now).total_seconds() / 3600
            if hours_until <= 6:
                await asyncio.sleep(3)
                await announce_match(next_mid, next_match)
        except Exception:
            pass

@bot.command()
@commands.has_permissions(administrator=True)
async def settime(ctx, mid: str, *, dt: str):
    """Adjust a match kickoff. Usage: .settime 5 2026-06-14T20:00"""
    matches = DB.load("matches.json")
    if mid not in matches: return await ctx.send(f"Match #{mid} not found.")
    matches[mid]["kickoff"] = dt
    DB.save("matches.json", matches)
    await ctx.send(f"Match #{mid} kickoff updated to {dt}")

@bot.command()
@commands.has_permissions(administrator=True)
async def advance(ctx):
    """Manually advance to the next stage."""
    if advance_tournament():
        t = DB.load("tournament.json")
        await ctx.send(f"{WC_EMOJI} Advanced to **{t['stage']}**!")
    else:
        await ctx.send("Cannot advance — current stage not complete.")

@bot.command()
@commands.has_permissions(administrator=True)
async def winner(ctx):
    """Announce the champion and award role."""
    if isinstance(WINNER_ROLE_ID, str) and WINNER_ROLE_ID == "YOUR_ACTUAL_WINNER_ROLE_ID":
        return await ctx.send("⚠️ WINNER_ROLE_ID not configured. Please set it via environment variable.")
    matches = DB.load("matches.json")
    finals = [m for m in matches.values() if m.get("stage")=="FINAL" and m["status"]==STATUS_FINISHED]
    if not finals: return await ctx.send("The Final has not been played yet.")
    champ = match_winner(finals[0])
    if not champ: return await ctx.send("Could not determine champion.")
    embed = discord.Embed(
        title=f"{WC_EMOJI}  VTX WORLD CUP 2026 CHAMPION",
        description=f"**{flag_for(champ)} {champ}** has conquered the World Cup!",
        color=0xFFD700,
    )
    channel = bot.get_channel(WC_CHANNEL_ID)
    if channel:
        await channel.send(f"<@&{WC_ROLE_ID}>", embed=embed)
    players = DB.load("players.json")
    try:
        winner_role_id = int(WINNER_ROLE_ID)
        for guild in bot.guilds:
            role = guild.get_role(winner_role_id)
            if not role: continue
            for member in guild.members:
                ud = players.get(str(member.id))
                if ud and ud.get("country") == champ:
                    try:
                        await member.add_roles(role)
                    except Exception:
                        pass
                    try: await member.send(f"Congratulations! {champ} won the VTX World Cup 2026!")
                    except Exception: pass
    except ValueError:
        pass
    t = DB.load("tournament.json")
    t["stage"] = "FINISHED"; t["champion"] = champ
    DB.save("tournament.json", t)

@bot.command()
@commands.has_permissions(administrator=True)
async def reset(ctx):
    """Reset all tournament data."""
    # Stop background tasks
    autolock.cancel()
    autoannounce.cancel()
    autolb.cancel()
    # Clear data
    if os.path.exists(DATA_FOLDER): shutil.rmtree(DATA_FOLDER)
    DB.ensure()
    global COUNTRY_MSG_IDS, LEADERBOARD_MSG_ID
    COUNTRY_MSG_IDS = []
    LEADERBOARD_MSG_ID = None
    # Restart background tasks
    if not autolock.is_running():  autolock.start()
    if not autoannounce.is_running(): autoannounce.start()
    if not autolb.is_running():    autolb.start()
    await ctx.send(f"{WC_EMOJI} Tournament **reset**. All data cleared.")

# ── INFO COMMANDS ─────────────────────────────────────────
@bot.command()
async def table(ctx, group: str = None):
    """Show group standings. Usage: .table A"""
    t = DB.load("tournament.json")
    if not t.get("groups"): return await ctx.send("Group stage not started.")
    for g in ([group.upper()] if group else sorted(t["groups"].keys())):
        if g not in t["groups"]: continue
        s = group_standings(g)
        lines = []
        for i, (team, st) in enumerate(s):
            lines.append(
                f"`{i+1}.` {flag_for(team)} **{team}**  "
                f"P:{st['played']} W:{st['won']} D:{st['drawn']} L:{st['lost']} "
                f"GD:{st['gd']:+d} **{st['points']}pts**"
            )
        embed = discord.Embed(title=f"Group {g}", description="\n".join(lines) or "No data", color=0x00CCFF)
        await ctx.send(embed=embed)

@bot.command()
async def stats(ctx, member: discord.Member = None):
    """Show player stats."""
    target = member or ctx.author
    uid = str(target.id)
    players = DB.load("players.json")
    p = players.get(uid)
    if not p: return await ctx.send(f"{target.display_name} is not registered.")
    country = p.get("country", "Not assigned")
    embed = discord.Embed(title=f"{flag_for(country)} {target.display_name}", color=0xFFD700)
    embed.add_field(name="Country", value=country, inline=True)
    embed.add_field(name="Points",  value=p.get("points",0), inline=True)
    embed.add_field(name="Correct", value=p.get("correct_predictions",0), inline=True)
    embed.add_field(name="Exact",   value=p.get("exact_scores",0), inline=True)
    # AI analysis
    if country != "Not assigned":
        analysis = await ai_analysis(country)
        embed.add_field(name="AI Analysis", value=analysis, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def lb(ctx):
    """Show the leaderboard."""
    players = DB.load("players.json")
    if not players: return await ctx.send("No players registered.")
    ranked = sorted(players.items(),
        key=lambda x: (x[1].get("points",0),x[1].get("exact_scores",0)), reverse=True)
    medals = ["1st","2nd","3rd"]
    lines = []
    for i, (uid, d) in enumerate(ranked[:15]):
        country = d.get("country","?")
        rank = medals[i] if i < 3 else f"#{i+1}"
        lines.append(
            f"{rank}  {flag_for(country)} **{country}**  <@{uid}>\n"
            f"   {d.get('points',0)} pts  {d.get('exact_scores',0)} exact  "
            f"{d.get('correct_predictions',0)} correct"
        )
    embed = discord.Embed(
        title=f"{WC_EMOJI} VTX World Cup 2026  Leaderboard",
        description="\n\n".join(lines), color=0xFFD700)
    await ctx.send(embed=embed)

@bot.command()
async def stage(ctx):
    """Show tournament status."""
    t = DB.load("tournament.json")
    ms = DB.load("matches.json")
    ps = DB.load("players.json")
    labels = {"registration":"Registration","GROUP":"Group Stage",
        "R32":"Round of 32","R16":"Round of 16","QF":"Quarter-Finals","SF":"Semi-Finals",
        "3RD":"Third Place","FINAL":"Final","FINISHED":"Complete"}
    cur = t.get("stage","registration")
    embed = discord.Embed(title=f"{WC_EMOJI} Tournament Status", color=0x00FFCC)
    embed.add_field(name="Stage",    value=labels.get(cur,cur), inline=True)
    embed.add_field(name="Players",  value=str(len(ps)), inline=True)
    embed.add_field(name="Champion", value=t.get("champion") or "TBD", inline=True)
    embed.add_field(name="Open",     value=sum(1 for m in ms.values() if m["status"]==STATUS_OPEN), inline=True)
    embed.add_field(name="Live",     value=sum(1 for m in ms.values() if m["status"]==STATUS_LOCKED), inline=True)
    embed.add_field(name="Finished", value=sum(1 for m in ms.values() if m["status"]==STATUS_FINISHED), inline=True)
    await ctx.send(embed=embed)

@bot.command()
async def help(ctx):
    """Show all commands."""
    embed = discord.Embed(title=f"{WC_EMOJI} VTX World Cup 2026  Commands", color=0xFFD700)
    embed.add_field(name="Registration", value="`.countries` Post flag selection", inline=False)
    embed.add_field(name="Tournament", value=(
        "`.schedule` Load real FIFA fixtures\n"
        "`.start` Announce next match\n"
        "`.result <id> <score>` Enter real result\n"
        "`.settime <id> <datetime>` Adjust kickoff\n"
        "`.advance` Advance stage\n"
        "`.winner` Announce champion"
    ), inline=False)
    embed.add_field(name="Info", value=(
        "`.table [group]` Standings\n"
        "`.stats [@user]` Player stats + AI\n"
        "`.lb` Leaderboard\n"
        "`.stage` Status"
    ), inline=False)
    embed.add_field(name="Admin", value="`.reset` Reset all\n`.help` This message", inline=False)
    await ctx.send(embed=embed)

# =====================================================================
#  BACKGROUND AUTOMATION
# =====================================================================
@tasks.loop(seconds=30)
async def autolock():
    """Lock threads at kickoff."""
    matches = DB.load("matches.json")
    now = datetime.now(timezone.utc)
    changed = False
    for mid, m in matches.items():
        if m["status"] != STATUS_OPEN: continue
        try:
            ko = datetime.fromisoformat(m["kickoff"]).replace(tzinfo=timezone.utc)
        except Exception: continue
        if now >= ko:
            m["status"] = STATUS_LOCKED; changed = True
            if m.get("thread_id"):
                try:
                    thread = await bot.fetch_channel(m["thread_id"])
                    try:
                        await thread.send("Predictions are now **LOCKED**! The match is live!")
                    except Exception:
                        pass
                    if not getattr(thread, "locked", False):
                        await thread.edit(locked=True)
                except Exception:
                    pass
    if changed: DB.save("matches.json", matches)

@tasks.loop(minutes=5)
async def autoannounce():
    """Auto-announce matches 3 hours before kickoff."""
    matches = DB.load("matches.json")
    now = datetime.now(timezone.utc)
    for mid, m in sorted(matches.items(), key=lambda x: _parse_kickoff_for_sort(x[1])):
        if m["status"] != STATUS_OPEN or m.get("thread_id") or m.get("announced"): continue
        try:
            ko = datetime.fromisoformat(m["kickoff"]).replace(tzinfo=timezone.utc)
        except Exception: continue
        hours_until = (ko - now).total_seconds() / 3600
        if 0 < hours_until <= 3:
            await announce_match(mid, m)
            await asyncio.sleep(5)

@tasks.loop(minutes=2)
async def autolb():
    """Refresh pinned leaderboard."""
    await update_leaderboard()

async def update_leaderboard():
    global LEADERBOARD_MSG_ID
    channel = bot.get_channel(WC_CHANNEL_ID)
    if not channel: return
    players = DB.load("players.json")
    if not players: return
    ranked = sorted(players.items(),
        key=lambda x: (x[1].get("points",0),x[1].get("exact_scores",0)), reverse=True)
    medals = ["1st","2nd","3rd"]
    lines = []
    for i, (uid, d) in enumerate(ranked[:10]):
        country = d.get("country","?")
        rank = medals[i] if i < 3 else f"#{i+1}"
        lines.append(
            f"{rank}  {flag_for(country)} **{country}**  <@{uid}>\n"
            f"{d.get('points',0)} pts  {d.get('exact_scores',0)} exact  "
            f"{d.get('correct_predictions',0)} correct"
        )
    embed = discord.Embed(
        title=f"{WC_EMOJI} VTX World Cup 2026  Live Leaderboard",
        description="\n\n".join(lines) or "No data",
        color=0xFFD700,
    )
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    embed.set_footer(text=f"Updated: {ts}")
    settings = DB.load("settings.json")
    if LEADERBOARD_MSG_ID:
        try:
            msg = await channel.fetch_message(LEADERBOARD_MSG_ID)
            await msg.edit(embed=embed)
            return
        except Exception:
            pass
    msg = await channel.send(embed=embed)
    LEADERBOARD_MSG_ID = msg.id
    settings["leaderboard_msg_id"] = LEADERBOARD_MSG_ID
    DB.save("settings.json", settings)

# =====================================================================
#  RUN
# =====================================================================
if __name__ == "__main__":
    DB.ensure()
    bot.run(TOKEN)
