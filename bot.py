# bot.py
import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import shlex

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

# FIX: prefix must be just the prefix, not "prefix + command"
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "worker_data.json"

# ------------------ RUNTIME STORAGE ------------------
# alarms: {guild_id: {post_channel_id: {user_id: {time_str: {"task":..., "name":..., "bid":..., "end_datetime":...}}}}}
alarms = {}
dashboard_messages = {}  # {guild_id: {post_channel_id: discord.Message}}
dashboard_tasks = {}     # {guild_id: {post_channel_id: asyncio.Task}}
data = {}                # persistent storage (roles/setup/timezone only)


# ------------------ DATA HANDLING ------------------
def load_data():
    global data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}


def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def ensure_guild(guild_id: int):
    if str(guild_id) not in data:
        data[str(guild_id)] = {
            "roles": [],
            "channel_setups": {},  # keyed by command-channel-id (string)
            "timezone": 0
        }


# ------------------ PERMISSION CHECK ------------------
def has_permission(member: discord.Member, guild_id: int) -> bool:
    ensure_guild(guild_id)
    if member.guild_permissions.administrator:
        return True
    allowed_roles = set(data[str(guild_id)]["roles"])
    return any(role.id in allowed_roles for role in member.roles)


# ------------------ TIME ------------------
def get_now(guild_id: int) -> datetime:
    ensure_guild(guild_id)
    offset = int(data[str(guild_id)]["timezone"])
    return datetime.now(timezone.utc) + timedelta(hours=offset)


# ------------------ HELPERS ------------------
def human_readable_remaining(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "finished"
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"in {hours}h {minutes}m"
    if seconds >= 60:
        minutes = seconds // 60
        return f"in {minutes} minutes"
    return "less than a minute"


async def get_channel_safe(guild: discord.Guild, channel_id: int) -> discord.TextChannel | None:
    # Try cache first
    ch = guild.get_channel(channel_id)
    if ch is not None:
        return ch
    # Fallback to API fetch
    try:
        fetched = await bot.fetch_channel(channel_id)
        if isinstance(fetched, discord.TextChannel):
            return fetched
        return None
    except discord.NotFound:
        return None
    except discord.Forbidden:
        return None


def find_setup_for_post_channel(guild_id: int, post_channel_id: int):
    """Return setup dict that has this post_channel_id, else None."""
    ensure_guild(guild_id)
    setups = data[str(guild_id)]["channel_setups"]
    for _cmd_channel_id, info in setups.items():
        if info.get("post_channel_id") == post_channel_id:
            return info
    return None


# ------------------ DASHBOARD ------------------
async def update_dashboard(guild_id: int, post_channel: discord.TextChannel):
    post_alarms = alarms.get(guild_id, {}).get(post_channel.id, {})

    # Clean empty user dicts
    if post_alarms:
        empty_users = [uid for uid, ua in post_alarms.items() if not ua]
        for uid in empty_users:
            post_alarms.pop(uid, None)

    # Remove dashboard if empty
    if not post_alarms:
        if guild_id in dashboard_messages and post_channel.id in dashboard_messages[guild_id]:
            try:
                await dashboard_messages[guild_id][post_channel.id].delete()
            except:
                pass
            dashboard_messages[guild_id].pop(post_channel.id, None)

        # Cancel live dashboard task
        if guild_id in dashboard_tasks and post_channel.id in dashboard_tasks[guild_id]:
            dashboard_tasks[guild_id][post_channel.id].cancel()
            dashboard_tasks[guild_id].pop(post_channel.id, None)
        return

    setup = find_setup_for_post_channel(guild_id, post_channel.id)
    role_mention = f"<@&{setup['role_id']}>" if setup else ""

    embed = discord.Embed(
        title=f"🔔 Upcoming Alarms — {role_mention}",
        color=discord.Color.blue()
    )

    # Discord embed limit: max 25 fields
    field_count = 0

    # Sort alarms by end time to keep dashboard readable
    rows = []
    for user_id, user_alarms in post_alarms.items():
        for time_str, alarm_data in user_alarms.items():
            rows.append((alarm_data["end_datetime"], user_id, time_str, alarm_data))
    rows.sort(key=lambda x: x[0])

    for end_datetime, user_id, time_str, alarm_data in rows:
        if field_count >= 25:
            embed.add_field(
                name="…",
                value="Too many alarms to display (max 25). Remove some alarms to see the rest.",
                inline=False
            )
            break

        name = alarm_data["name"]
        bid = alarm_data["bid"]
        begin_datetime = end_datetime - timedelta(minutes=55)
        remaining = end_datetime - get_now(guild_id)
        time_left = human_readable_remaining(remaining)

        embed.add_field(
            name=f'"{name}"',
            value=(
                f'Bid - "{bid}"\n'
                f'{begin_datetime.strftime("%H:%M")}          {end_datetime.strftime("%H:%M")}        {time_left}'
            ),
            inline=False
        )
        field_count += 1

    embed.set_footer(text="Worker Alarm System")

    dashboard_messages.setdefault(guild_id, {})

    if post_channel.id not in dashboard_messages[guild_id]:
        msg = await post_channel.send(embed=embed)
        dashboard_messages[guild_id][post_channel.id] = msg
    else:
        try:
            await dashboard_messages[guild_id][post_channel.id].edit(embed=embed)
        except discord.NotFound:
            # If someone deleted it
            msg = await post_channel.send(embed=embed)
            dashboard_messages[guild_id][post_channel.id] = msg


# ------------------ LIVE DASHBOARD TASK ------------------
async def live_dashboard_task(guild_id: int, post_channel: discord.TextChannel):
    try:
        while True:
            await update_dashboard(guild_id, post_channel)
            await asyncio.sleep(30)  # less spam than 10s
    except asyncio.CancelledError:
        pass


# ------------------ ALARM TASK ------------------
async def run_alarm(
    guild_id: int,
    post_channel: discord.TextChannel,
    user_id: int,
    end_datetime: datetime,
    time_str: str,
    name: str,
    bid: str,
    setup: dict | None
):
    role_mention = f"<@&{setup['role_id']}>" if setup else ""
    warnings = [10, 5, 1]  # minutes before end

    try:
        for minutes in warnings:
            wait_seconds = (end_datetime - timedelta(minutes=minutes) - get_now(guild_id)).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                await post_channel.send(
                    f"{role_mention} ⏳ — {minutes} minute(s) until **{name}** ({bid}) at {time_str}!"
                )

        wait_seconds = (end_datetime - get_now(guild_id)).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        await post_channel.send(
            f"{role_mention} 🚨 — ALARM for **{name}** ({bid}) — {time_str}!"
        )

    except asyncio.CancelledError:
        # Cancellation message is optional; comment out if too noisy
        await post_channel.send(
            f"{role_mention} ❌ — Alarm for **{name}** ({bid}) at {time_str} was cancelled."
        )
        raise

    finally:
        # Remove ONLY this user's alarm (fix for same HH:MM across users)
        alarms.get(guild_id, {}).get(post_channel.id, {}).get(user_id, {}).pop(time_str, None)
        await update_dashboard(guild_id, post_channel)


# ------------------ COMMANDS ------------------
@bot.command(name="worker_help")
async def worker_help(ctx: commands.Context):
    await ctx.send(
        "**Worker Commands**\n"
        "`!worker + HH:MM Name [Bid]` — add alarm\n"
        "`!worker - HH:MM` — remove alarm\n"
        "`!worker setup #post-channel @Role` — set post channel & role (admin)\n"
        "`!worker timezone X` — set GMT offset (admin)\n"
        "`!worker AddRole @Role` — allow role to use bot (admin)\n"
        "`!worker RemoveRole @Role` — remove allowed role (admin)\n"
        "`!worker ListRoles` — list allowed roles (admin)\n"
        "`!worker help` — show this help"
    )


@bot.command()
async def worker(ctx: commands.Context, action: str | None = None, arg1: str | None = None, arg2: str | None = None):
    if ctx.guild is None:
        await ctx.send("This bot only works in a server (guild).")
        return

    guild_id = ctx.guild.id
    ensure_guild(guild_id)

    # --- HELP ---
    if action in (None, "help"):
        await worker_help(ctx)
        return

    # --- SETUP (admin) ---
    if action == "setup":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("Admin only command.")
            return
        if not arg1 or not arg2:
            await ctx.send("Usage: `!worker setup #post-channel @Role`")
            return

        try:
            post_channel = await commands.TextChannelConverter().convert(ctx, arg1)
            role = await commands.RoleConverter().convert(ctx, arg2)
        except commands.BadArgument:
            await ctx.send("Usage: `!worker setup #post-channel @Role`")
            return

        data[str(guild_id)]["channel_setups"][str(ctx.channel.id)] = {
            "post_channel_id": post_channel.id,
            "role_id": role.id
        }
        save_data()
        await ctx.send(f"✅ Setup complete: Alarms will post in {post_channel.mention} and ping {role.mention}")
        return

    # --- ROLE MANAGEMENT (admin) ---
    if action in ("AddRole", "RemoveRole", "ListRoles"):
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("Admin only command.")
            return

        roles_list = data[str(guild_id)]["roles"]

        if action == "ListRoles":
            if not roles_list:
                await ctx.send("No allowed roles configured.")
                return
            mentions = [f"<@&{r}>" for r in roles_list]
            await ctx.send("Allowed roles:\n" + "\n".join(mentions))
            return

        if not arg1:
            await ctx.send(f"Usage: `!worker {action} @Role`")
            return

        try:
            role = await commands.RoleConverter().convert(ctx, arg1)
        except commands.BadArgument:
            await ctx.send(f"Usage: `!worker {action} @Role`")
            return

        if action == "AddRole":
            if role.id not in roles_list:
                roles_list.append(role.id)
                save_data()
            await ctx.send(f"Role {role.mention} added.")
        elif action == "RemoveRole":
            if role.id in roles_list:
                roles_list.remove(role.id)
                save_data()
            await ctx.send(f"Role {role.mention} removed.")
        return

    # --- TIMEZONE (admin) ---
    if action == "timezone":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("Admin only command.")
            return
        if not arg1:
            await ctx.send("Usage: `!worker timezone X` (X between -12 and +14)")
            return
        try:
            offset = int(arg1)
            if offset < -12 or offset > 14:
                raise ValueError
            data[str(guild_id)]["timezone"] = offset
            save_data()
            await ctx.send(f"Timezone set to GMT{offset:+}")
        except:
            await ctx.send("Use a number between -12 and +14.")
        return

    # --- ADD / REMOVE ALARM: only in a configured command channel ---
    if str(ctx.channel.id) not in data[str(guild_id)]["channel_setups"]:
        await ctx.send("This command can only be used in a setup command channel.")
        return

    if not has_permission(ctx.author, guild_id):
        await ctx.send("You do not have permission.")
        return

    setup_info = data[str(guild_id)]["channel_setups"][str(ctx.channel.id)]
    post_channel = await get_channel_safe(ctx.guild, setup_info["post_channel_id"])
    if post_channel is None:
        await ctx.send("Configured post channel not found or not accessible.")
        return

    # Ensure storage containers exist
    alarms.setdefault(guild_id, {})
    alarms[guild_id].setdefault(post_channel.id, {})
    alarms[guild_id][post_channel.id].setdefault(ctx.author.id, {})

    # --- ADD ALARM ---
    if action == "+":
        try:
            parts = shlex.split(ctx.message.content)
            # Example: !worker + 19:55 "Eiffel" 3M
            if len(parts) < 4:
                await ctx.send("Usage: `!worker + HH:MM Name [Bid]`")
                return

            time_value = parts[2]
            name = parts[3]
            bid = parts[4] if len(parts) > 4 else "No bid"

            end_time = datetime.strptime(time_value, "%H:%M").time()
        except:
            await ctx.send('Invalid format. Example: `!worker + 19:55 "Eiffel" 3M`')
            return

        now = get_now(guild_id)
        end_datetime = datetime.combine(now.date(), end_time, tzinfo=timezone.utc)  # keep tz-aware
        # Important: now is tz-aware (UTC + offset), end_datetime must align.
        # We'll treat end_datetime as "local offset time" stored as tz-aware UTC for comparisons.
        # Simplest approach: build naive local and compare with now naive-local:
        # But to keep minimal changes, we rebuild end_datetime using now's tzinfo.

        # Better: use now's tzinfo
        end_datetime = datetime.combine(now.date(), end_time, tzinfo=now.tzinfo)

        if end_datetime < now:
            end_datetime += timedelta(days=1)

        # If user already has an alarm at same time, cancel it first
        existing = alarms[guild_id][post_channel.id][ctx.author.id].get(time_value)
        if existing:
            existing["task"].cancel()

        task = asyncio.create_task(
            run_alarm(guild_id, post_channel, ctx.author.id, end_datetime, time_value, name, bid, setup_info)
        )

        alarms[guild_id][post_channel.id][ctx.author.id][time_value] = {
            "task": task,
            "name": name,
            "bid": bid,
            "end_datetime": end_datetime
        }

        # Start live dashboard if not running
        dashboard_tasks.setdefault(guild_id, {})
        if post_channel.id not in dashboard_tasks[guild_id]:
            dashboard_tasks[guild_id][post_channel.id] = asyncio.create_task(
                live_dashboard_task(guild_id, post_channel)
            )

        await ctx.send(f"✅ Alarm set for **{name}** ({bid}) at {time_value} (GMT{data[str(guild_id)]['timezone']:+})")
        await update_dashboard(guild_id, post_channel)
        return

    # --- REMOVE ALARM ---
    if action == "-":
        if not arg1:
            await ctx.send("Usage: `!worker - HH:MM`")
            return
        time_value = arg1

        user_alarms = alarms.get(guild_id, {}).get(post_channel.id, {}).get(ctx.author.id, {})
        if time_value not in user_alarms:
            await ctx.send("No alarm found for that time.")
            return

        user_alarms[time_value]["task"].cancel()
        # run_alarm finally will remove it + update dashboard
        await ctx.send(f"✅ Alarm {time_value} cancelled.")
        return

    await ctx.send("Unknown action. Try `!worker help`.")


# ------------------ BOT READY ------------------
@bot.event
async def on_ready():
    load_data()
    print(f"Logged in as {bot.user} (id={bot.user.id})")


# ------------------ RUN BOT ------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable not set.")
bot.run(TOKEN)
