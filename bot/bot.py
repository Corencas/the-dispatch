import os
import discord
from discord import app_commands
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
SERVER_URL = os.getenv('SERVER_URL', 'http://localhost:5001')
GUILD_ID   = os.getenv('DISCORD_GUILD_ID')  # set for instant sync during dev

AMBER = 0xF5A623
RED   = 0xE53535
GREEN = 0x1FBA5A
DIM   = 0x1C2028

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


async def _get(path: str) -> dict | None:
    url = f"{SERVER_URL}{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                return await r.json() if r.status == 200 else None
    except Exception:
        return None


def _km(n)   -> str: return f"{int(n or 0):,} km"
def _cash(n) -> str: return f"${int(n or 0):,}"
def _num(n)  -> str: return f"{int(n or 0):,}"

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


# ─── /mystats ────────────────────────────────────────────────────

@tree.command(name="mystats", description="Your trucking stats from The Dispatch")
async def mystats(interaction: discord.Interaction):
    await interaction.response.defer()
    did = str(interaction.user.id)

    player, lb = await _get(f"/api/player/{did}"), await _get(f"/api/leaderboard?sort=distance&limit=500")

    if not player:
        embed = discord.Embed(
            description="You're not registered. Connect the client and push a save to get started.",
            color=RED,
        )
        await interaction.followup.send(embed=embed)
        return

    # Pull stats + rank from the leaderboard row (already has job_count, total_revenue)
    job_count = total_revenue = dist_rank = 0
    if lb:
        for row in lb.get("rows", []):
            if row["discord_id"] == did:
                job_count     = row["job_count"]
                total_revenue = row["total_revenue"]
                dist_rank     = row["rank"]
                break

    vtc_line = ""
    if player.get("vtc_id"):
        vtc = await _get(f"/api/vtc/{player['vtc_id']}")
        if vtc:
            vtc_line = f"[{vtc['tag']}] {vtc['name']}"

    net = (player.get("money") or 0) - (player.get("total_debt") or 0)

    embed = discord.Embed(title=f"📋  {player['discord_username']}", color=AMBER)
    if vtc_line:
        embed.description = f"**VTC:** {vtc_line}"

    embed.add_field(name="Distance",  value=_km(player["total_distance_km"]),   inline=True)
    embed.add_field(name="XP",        value=_num(player["experience_points"]),   inline=True)
    embed.add_field(name="Drivers",   value=_num(player["driver_count"]),        inline=True)
    embed.add_field(name="Cash",      value=_cash(player["money"]),              inline=True)
    embed.add_field(name="Debt",      value=_cash(player["total_debt"]),         inline=True)
    embed.add_field(name="Net",       value=_cash(net),                          inline=True)
    embed.add_field(name="Jobs",      value=_num(job_count),                     inline=True)
    embed.add_field(name="Earnings",  value=_cash(total_revenue),                inline=True)
    embed.add_field(
        name="Global Rank",
        value=f"#{dist_rank} by distance" if dist_rank else "Unranked",
        inline=True,
    )
    embed.set_footer(text="The Dispatch — Free VTC Fleet Management")
    await interaction.followup.send(embed=embed)


# ─── /leaderboard ────────────────────────────────────────────────

@tree.command(name="leaderboard", description="Top drivers on The Dispatch")
@app_commands.describe(
    sort="What to rank by (default: distance)",
    scope="Global rankings or your VTC only",
)
@app_commands.choices(
    sort=[
        app_commands.Choice(name="Distance", value="distance"),
        app_commands.Choice(name="Jobs",     value="jobs"),
        app_commands.Choice(name="Earnings", value="earnings"),
    ],
    scope=[
        app_commands.Choice(name="Global", value="global"),
        app_commands.Choice(name="My VTC", value="vtc"),
    ],
)
async def leaderboard(
    interaction: discord.Interaction,
    sort: str = "distance",
    scope: str = "global",
):
    await interaction.response.defer()
    did = str(interaction.user.id)

    vtc_info = None
    if scope == "vtc":
        player = await _get(f"/api/player/{did}")
        if player and player.get("vtc_id"):
            vtc_info = await _get(f"/api/vtc/{player['vtc_id']}")
            data = await _get(f"/api/leaderboard/vtc/{player['vtc_id']}?sort={sort}&limit=10") if vtc_info else None
        else:
            data = None
        if not data:
            # fallback to global silently
            scope = "global"
            data = await _get(f"/api/leaderboard?sort={sort}&limit=10")
    else:
        data = await _get(f"/api/leaderboard?sort={sort}&limit=10")

    if not data or not data.get("rows"):
        embed = discord.Embed(description="No drivers on record yet.", color=DIM)
        await interaction.followup.send(embed=embed)
        return

    sort_label = {"distance": "Distance", "jobs": "Jobs", "earnings": "Earnings"}.get(sort, sort.title())
    if vtc_info:
        title = f"[{vtc_info['tag']}] {vtc_info['name']} — {sort_label}"
    else:
        title = f"Global — {sort_label} Leaderboard"

    lines = []
    for row in data["rows"][:10]:
        prefix  = MEDALS.get(row["rank"], f"`{row['rank']:>2}.`")
        name    = row["discord_username"]
        you     = row["discord_id"] == did
        display = f"**{name}** ◀" if you else name

        if sort == "distance":
            val = _km(row["total_distance_km"])
        elif sort == "jobs":
            val = f"{_num(row['job_count'])} jobs"
        else:
            val = _cash(row["total_revenue"])

        tag = f" [{row['vtc_tag']}]" if row.get("vtc_tag") and scope == "global" else ""
        lines.append(f"{prefix} {display}{tag} — {val}")

    embed = discord.Embed(title=title, description="\n".join(lines), color=AMBER)
    embed.set_footer(text="The Dispatch  ·  /mystats  ·  /fleet")
    await interaction.followup.send(embed=embed)


# ─── /fleet ──────────────────────────────────────────────────────

@tree.command(name="fleet", description="Fleet status for your VTC")
async def fleet(interaction: discord.Interaction):
    await interaction.response.defer()
    did = str(interaction.user.id)

    player = await _get(f"/api/player/{did}")
    if not player:
        embed = discord.Embed(
            description="You're not registered. Connect the client and push a save first.",
            color=RED,
        )
        await interaction.followup.send(embed=embed)
        return

    if not player.get("vtc_id"):
        embed = discord.Embed(
            description="You're not in a VTC. Create or join one on the website.",
            color=RED,
        )
        await interaction.followup.send(embed=embed)
        return

    vtc = await _get(f"/api/vtc/{player['vtc_id']}")
    if not vtc:
        embed = discord.Embed(description="Could not fetch VTC data. Is the server running?", color=RED)
        await interaction.followup.send(embed=embed)
        return

    members     = vtc.get("members", [])
    total_dist  = sum(m.get("total_distance_km") or 0 for m in members)
    total_jobs  = sum(m.get("job_count") or 0 for m in members)
    total_xp    = sum(m.get("experience_points") or 0 for m in members)

    embed = discord.Embed(title=f"[{vtc['tag']}]  {vtc['name']}", color=AMBER)
    embed.add_field(name="Members",        value=str(vtc["member_count"]), inline=True)
    embed.add_field(name="Fleet Distance", value=_km(total_dist),          inline=True)
    embed.add_field(name="Fleet Jobs",     value=_num(total_jobs),         inline=True)
    embed.add_field(name="Fleet XP",       value=_num(total_xp),           inline=True)
    embed.add_field(name="Access Code",    value=f"`{vtc['access_code']}`", inline=True)
    embed.add_field(name="​",         value="​",                 inline=True)

    # Roster sorted by distance, up to 8
    top = sorted(members, key=lambda m: m.get("total_distance_km") or 0, reverse=True)[:8]
    roster = []
    for m in top:
        crown = " 👑" if m.get("is_owner") else ""
        dist  = _km(m.get("total_distance_km") or 0)
        jobs  = m.get("job_count") or 0
        roster.append(f"**{m['discord_username']}**{crown} — {dist} · {jobs} jobs")

    if roster:
        embed.add_field(name="Roster", value="\n".join(roster), inline=False)

    embed.set_footer(text="The Dispatch — Free VTC Fleet Management")
    await interaction.followup.send(embed=embed)


# ─── STARTUP ─────────────────────────────────────────────────────

@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"Commands synced to guild {GUILD_ID}")
    else:
        await tree.sync()
        print("Commands synced globally (may take up to 1 hour)")
    print(f"Bot ready — {client.user}")


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN not set in bot/.env")
    client.run(BOT_TOKEN)
