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

# Prefix is only the prefix
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "worker_data.json"

# ------------------ RUNTIME STORAGE ------------------
# alarms: {guild_id: {post_channel_id: {user_id: {time_str: {"task":..., "name":..., "bid":..., "end_datetime":...}}}}}
alarms = {}
dashboard_messages = {}  # {guild_id: {post_channel_id: discord.Message}}
dashboard_tasks = {}     # {guild_id: {post_channel_id: asyncio.Task}}
dashboard_locks = {}     # {(guild_id, post_channel_id): asyncio.Lock()}
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
def get_now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ------------------ HELPERS ------------------
async def get_channel_safe(guild: discord.Guild, channel_id: int) -> discord.TextChannel | None:
    ch = guild.get_channel(channel_id)
    if ch is not None:
        return ch
    try:
        fetched = await bot.fetch_channel(channel_id)
        return fetched if isinstance(fetched, discord.TextChannel) else None
    except (discord.NotFound, discord.Forbidden):
        return None


def find_setup_for_post_channel(guild_id: int, post_channel_id: int):
    """Return setup dict that has this post_channel_id, else None."""
    ensure_guild(guild_id)
    setups = data[str(guild_id)]["channel_setups"]
    for _cmd_channel_id, info in setups.items():
        if info.get("post_channel_id") == post_channel_id:
            return info
    return None


def local_offset_to_utc(dt_local: datetime, offset_hours: int) -> datetime:
    """
    Interpret dt_local as 'guild local time' = UTC + offset_hours.
    Convert to UTC-aware datetime.
    """
    # dt_local is naive; treat it as offset time and convert to UTC:
    return (dt_local - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)


def utc_to_local_for_display(dt_utc: datetime, offset_hours: int) -> datetime:
    """Only used for comparisons vs today in guild-local sense."""
    return dt_utc + timedelta(hours=offset_hours)


# ------------------ DASHBOARD ------------------
async def update_dashboard(guild_id: int, post_channel: discord.TextChannel):
    # Prevent double-post race (send vs send)
    key = (guild_id, post_channel.id)
    lock = dashboard_locks.setdefault(key, asyncio.Lock())

    async with lock:
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
        embed = discord.Embed(title="🔔 Upcoming Workers", color=discord.Color.blue())

        # ---- Build blocks in chronological order ----
        items = []
        for user_id, user_alarms in post_alarms.items():
            for time_str, alarm_data in user_alarms.items():
                items.append(alarm_data)

        items.sort(key=lambda a: a["end_datetime"])  # end_datetime is UTC-aware

        blocks = []
        for alarm_data in items:
            name = alarm_data["name"]
            bid = alarm_data["bid"]
            end_dt = alarm_data["end_datetime"]              # UTC-aware
            begin_dt = end_dt - timedelta(minutes=55)        # UTC-aware

            begin_ts = int(begin_dt.timestamp())
            end_ts = int(end_dt.timestamp())

            blocks.append(
                f"**{name}**\n"
                f"Bid - {bid}\n"
                f"<t:{begin_ts}:t>  <t:{end_ts}:t>  <t:{end_ts}:R>"
            )

        embed.description = "\n\n---\n\n".join(blocks)
        embed.set_footer(text="Worker Alarm System")

        dashboard_messages.setdefault(guild_id, {})

        # Send or edit dashboard message
        if post_channel.id not in dashboard_messages[guild_id]:
            msg = await post_channel.send(
                content=f"Upcoming workers {role_mention}",
                embed=embed
            )
            dashboard_messages[guild_id][post_channel.id] = msg
        else:
            try:
                await dashboard_messages[guild_id][post_channel.id].edit(embed=embed)
            except discord.NotFound:
                msg = await post_channel.send(
                    content=f"Upcoming workers {role_mention}",
                    embed=embed
                )
                dashboard_messages[guild_id][post_channel.id] = msg


# --- DELETE SETUP (admin) ---
if action == "delete":
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Admin only command.")
        return

    if not arg1:
        await ctx.send("Usage: `!worker delete #post-channel`")
        return

    try:
        post_channel = await commands.TextChannelConverter().convert(ctx, arg1)
    except commands.BadArgument:
        await ctx.send("Usage: `!worker delete #post-channel`")
        return

    # Find matching setup (command channel → post channel)
    setups = data[str(guild_id)]["channel_setups"]
    cmd_channel_to_remove = None

    for cmd_channel_id, info in setups.items():
        if info.get("post_channel_id") == post_channel.id:
            cmd_channel_to_remove = cmd_channel_id
            break

    if not cmd_channel_to_remove:
        await ctx.send("No setup found for that channel.")
        return

    # ---- STOP DASHBOARD TASK ----
    if guild_id in dashboard_tasks and post_channel.id in dashboard_tasks[guild_id]:
        dashboard_tasks[guild_id][post_channel.id].cancel()
        dashboard_tasks[guild_id].pop(post_channel.id, None)

    # ---- DELETE DASHBOARD MESSAGE ----
    if guild_id in dashboard_messages and post_channel.id in dashboard_messages[guild_id]:
        try:
            await dashboard_messages[guild_id][post_channel.id].delete()
        except:
            pass
        dashboard_messages[guild_id].pop(post_channel.id, None)

    # ---- CANCEL ALL ALARMS IN THIS CHANNEL ----
    if guild_id in alarms and post_channel.id in alarms[guild_id]:
        for user_id, user_alarms in alarms[guild_id][post_channel.id].items():
            for alarm in user_alarms.values():
                alarm["task"].cancel()
        alarms[guild_id].pop(post_channel.id, None)

    # ---- REMOVE LOCK ----
    dashboard_locks.pop((guild_id, post_channel.id), None)

    # ---- REMOVE FROM PERSISTENT DATA ----
    setups.pop(cmd_channel_to_remove)
    save_data()

    await ctx.send(f"✅ Worker setup removed for {post_channel.mention}")
    return


# ------------------ LIVE DASHBOARD TASK ------------------
async def live_dashboard_task(guild_id: int, post_channel: discord.TextChannel):
    try:
        while True:
            await update_dashboard(guild_id, post_channel)
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass


# ------------------ ALARM TASK ------------------
async def run_alarm(
    guild_id: int,
    post_channel: discord.TextChannel,
    user_id: int,
    end_datetime_utc: datetime,
    time_str: str,
    name: str,
    bid: str,
    setup: dict | None
):
    role_mention = f"<@&{setup['role_id']}>" if setup else ""
    warnings = [10, 5, 1]  # minutes before end

    try:
        for minutes in warnings:
            wait_seconds = (end_datetime_utc - timedelta(minutes=minutes) - get_now_utc()).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                await post_channel.send(
                    f"{role_mention} ⏳ — {minutes} minute(s) until **{name}** ({bid}) at {time_str}!"
                )

        wait_seconds = (end_datetime_utc - get_now_utc()).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        await post_channel.send(
            f"{role_mention} 🚨 — ALARM for **{name}** ({bid}) — {time_str}!"
        )

    except asyncio.CancelledError:
        # Silent cancel (command handler already confirms)
        raise

    finally:
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
        "`!worker timezone X` — set GMT offset (admin, -12..+14)\n"
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

    # Ensure runtime containers exist
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

        offset = int(data[str(guild_id)]["timezone"])

        # Build "guild local" datetime (naive), then convert to UTC aware
        now_utc = get_now_utc()
        now_local = utc_to_local_for_display(now_utc, offset)

        end_local_naive = datetime.combine(now_local.date(), end_time)  # naive local
        # If time already passed in local time, schedule for next day
        if end_local_naive < now_local.replace(tzinfo=None):
            end_local_naive += timedelta(days=1)

        end_utc = local_offset_to_utc(end_local_naive, offset)

        # Replace existing alarm for this user+time, if any
        existing = alarms[guild_id][post_channel.id][ctx.author.id].get(time_value)
        if existing:
            existing["task"].cancel()

        task = asyncio.create_task(
            run_alarm(guild_id, post_channel, ctx.author.id, end_utc, time_value, name, bid, setup_info)
        )

        alarms[guild_id][post_channel.id][ctx.author.id][time_value] = {
            "task": task,
            "name": name,
            "bid": bid,
            "end_datetime": end_utc
        }

        # Start live dashboard if not running (race-safe due to dashboard lock)
        dashboard_tasks.setdefault(guild_id, {})
        if post_channel.id not in dashboard_tasks[guild_id]:
            dashboard_tasks[guild_id][post_channel.id] = asyncio.create_task(
                live_dashboard_task(guild_id, post_channel)
            )

        await ctx.send(f"✅ Alarm set for **{name}** ({bid}) at {time_value} (GMT{offset:+})")
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
        await ctx.send(f"✅ Alarm {time_value} cancelled.")
        await update_dashboard(guild_id, post_channel)
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
