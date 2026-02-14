# bot.py
import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import shlex
from threading import Lock

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ✅ Railway volume path
DATA_FILE = "/data/worker_data.json"
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

# If bot was offline and alarm passed:
# - We do NOT send anything "late"
# - We simply delete expired alarms from JSON on startup
GRACE_SECONDS = 0  # keep 0 to never fire late

# ------------------ RUNTIME STORAGE ------------------
# alarms: {guild_id: {post_channel_id: {user_id: {time_str: {"task":..., "name":..., "bid":..., "end_datetime":...}}}}}
alarms = {}
dashboard_messages = {}  # {guild_id: {post_channel_id: discord.Message}}
dashboard_tasks = {}     # {guild_id: {post_channel_id: asyncio.Task}}
dashboard_locks = {}     # {(guild_id, post_channel_id): asyncio.Lock()}
data = {}                # persistent storage
data_file_lock = Lock()


# ------------------ DATA HANDLING ------------------
def load_data():
    global data
    with data_file_lock:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}


def save_data():
    with data_file_lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)


def ensure_guild(guild_id: int):
    if str(guild_id) not in data:
        data[str(guild_id)] = {
            "roles": [],
            "channel_setups": {},  # {command_channel_id: {post_channel_id, role_id}}
            "timezone": 0,         # GMT offset for entering HH:MM
            "alarms": {}           # {post_channel_id: {user_id: {HH:MM: {name,bid,end_utc}}}}
        }
    else:
        data[str(guild_id)].setdefault("alarms", {})


def persist_alarm(guild_id: int, post_channel_id: int, user_id: int, time_str: str,
                  name: str, bid: str, end_utc: datetime):
    ensure_guild(guild_id)
    g = data[str(guild_id)]
    g["alarms"].setdefault(str(post_channel_id), {})
    g["alarms"][str(post_channel_id)].setdefault(str(user_id), {})
    g["alarms"][str(post_channel_id)][str(user_id)][time_str] = {
        "name": name,
        "bid": bid,
        "end_utc": end_utc.isoformat()
    }
    save_data()


def remove_persisted_alarm(guild_id: int, post_channel_id: int, user_id: int, time_str: str):
    ensure_guild(guild_id)
    g = data[str(guild_id)]
    a = g.get("alarms", {})
    pc = a.get(str(post_channel_id), {})
    ua = pc.get(str(user_id), {})

    ua.pop(time_str, None)

    # cleanup empties
    if not ua:
        pc.pop(str(user_id), None)
    if not pc:
        a.pop(str(post_channel_id), None)

    save_data()


# ------------------ PERMISSION CHECK ------------------
def has_permission(member: discord.Member, guild_id: int) -> bool:
    ensure_guild(guild_id)
    if member.guild_permissions.administrator:
        return True
    allowed_roles = set(data[str(guild_id)]["roles"])
    return any(role.id in allowed_roles for role in member.roles)


# ------------------ TIME ------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_to_local(dt_utc: datetime, offset_hours: int) -> datetime:
    return dt_utc + timedelta(hours=offset_hours)


def local_naive_to_utc(dt_local_naive: datetime, offset_hours: int) -> datetime:
    return (dt_local_naive - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)


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
    ensure_guild(guild_id)
    setups = data[str(guild_id)]["channel_setups"]
    for _cmd_channel_id, info in setups.items():
        if info.get("post_channel_id") == post_channel_id:
            return info
    return None


# ------------------ DASHBOARD CLEANUP ON STARTUP ------------------
async def cleanup_old_dashboards():
    """
    Removes old dashboard embed messages after a restart, so you don't get duplicates.
    Keeps normal text warning messages ("10 minutes until ...") intact.
    """
    for guild in bot.guilds:
        guild_id = guild.id
        ensure_guild(guild_id)
        setups = data[str(guild_id)].get("channel_setups", {})

        # unique post channels
        post_channel_ids = {info.get("post_channel_id") for info in setups.values() if info.get("post_channel_id")}
        for post_channel_id in post_channel_ids:
            channel = await get_channel_safe(guild, int(post_channel_id))
            if channel is None:
                continue

            try:
                async for msg in channel.history(limit=50):
                    if msg.author == bot.user and msg.embeds:
                        title = msg.embeds[0].title or ""
                        if title == "🔔 Upcoming Workers":
                            await msg.delete()
            except:
                pass


# ------------------ DASHBOARD ------------------
async def update_dashboard(guild_id: int, post_channel: discord.TextChannel):
    # Prevent send/send race creating duplicate dashboard posts
    key = (guild_id, post_channel.id)
    lock = dashboard_locks.setdefault(key, asyncio.Lock())

    async with lock:
        post_alarms = alarms.get(guild_id, {}).get(post_channel.id, {})

        # Clean empty user dicts
        if post_alarms:
            empty_users = [uid for uid, ua in post_alarms.items() if not ua]
            for uid in empty_users:
                post_alarms.pop(uid, None)

        # Remove dashboard if no alarms left
        if not post_alarms:
            if guild_id in dashboard_messages and post_channel.id in dashboard_messages[guild_id]:
                try:
                    await dashboard_messages[guild_id][post_channel.id].delete()
                except:
                    pass
                dashboard_messages[guild_id].pop(post_channel.id, None)

            if guild_id in dashboard_tasks and post_channel.id in dashboard_tasks[guild_id]:
                dashboard_tasks[guild_id][post_channel.id].cancel()
                dashboard_tasks[guild_id].pop(post_channel.id, None)

            dashboard_locks.pop(key, None)
            return

        setup = find_setup_for_post_channel(guild_id, post_channel.id)
        role_mention = f"<@&{setup['role_id']}>" if setup else ""

        embed = discord.Embed(title="🔔 Upcoming Workers", color=discord.Color.blue())

        # Build blocks in chronological order
        items = []
        for _user_id, user_alarms in post_alarms.items():
            for _time_str, alarm_data in user_alarms.items():
                items.append(alarm_data)

        items.sort(key=lambda a: a["end_datetime"])  # UTC-aware datetime

        # Wider spacing using unicode EM SPACE (won't collapse)
        gap = "\u2003\u2003"  # 2 em-spaces
        gap2 = "\u2003\u2003\u2003"  # 3 em-spaces

        blocks = []
        for alarm_data in items:
            name = alarm_data["name"]
            bid = alarm_data["bid"]
            end_dt = alarm_data["end_datetime"]
            begin_dt = end_dt - timedelta(minutes=55)

            begin_ts = int(begin_dt.timestamp())
            end_ts = int(end_dt.timestamp())

            bid_line = f"Bid - {bid}\n" if bid else ""
            
            blocks.append(
                f"## {name}\n"
                f"{bid_line}"
                f"🟢 Start{gap}🏁 End{gap}⏳ Time left\n"
                f"<t:{begin_ts}:t>{gap}<t:{end_ts}:t>{gap}<t:{end_ts}:R>"
            )

        embed.description = "\n\n---\n".join(blocks)

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
    end_utc: datetime,
    time_str: str,
    name: str,
    bid: str,
    setup: dict | None
):
    role_mention = f"<@&{setup['role_id']}>" if setup else ""
    warnings = [10, 5, 1]  # minutes before end

    try:
        for minutes in warnings:
            wait_seconds = (end_utc - timedelta(minutes=minutes) - now_utc()).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                bid_part = f" ({bid})" if bid else ""
                unit = "minute" if minutes == 1 else "minutes"
                await post_channel.send(
                    f"{role_mention} {minutes} {unit} until **{name}**{bid_part}"
                )

        # ❌ No "0 minutes / starting now" message on purpose

    except asyncio.CancelledError:
        raise

    finally:
        alarms.get(guild_id, {}).get(post_channel.id, {}).get(user_id, {}).pop(time_str, None)
        remove_persisted_alarm(guild_id, post_channel.id, user_id, time_str)
        await update_dashboard(guild_id, post_channel)


# ------------------ COMMANDS ------------------
@bot.command(name="worker_help")
async def worker_help(ctx: commands.Context):
    await ctx.send(
        "**Worker Commands**\n"
        "`!worker + HH:MM Name [Bid]` — add alarm\n"
        "`!worker - HH:MM` — remove alarm\n"
        "`!worker setup #post-channel @Role` — set post channel & role (admin)\n"
        "`!worker delete #post-channel` — remove setup for a post channel (admin)\n"
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

        setups = data[str(guild_id)]["channel_setups"]

        cmd_channel_to_remove = None
        for cmd_channel_id, info in setups.items():
            if info.get("post_channel_id") == post_channel.id:
                cmd_channel_to_remove = cmd_channel_id
                break

        if not cmd_channel_to_remove:
            await ctx.send("No setup found for that post channel.")
            return

        # Stop live dashboard task
        if guild_id in dashboard_tasks and post_channel.id in dashboard_tasks[guild_id]:
            dashboard_tasks[guild_id][post_channel.id].cancel()
            dashboard_tasks[guild_id].pop(post_channel.id, None)

        # Delete dashboard message
        if guild_id in dashboard_messages and post_channel.id in dashboard_messages[guild_id]:
            try:
                await dashboard_messages[guild_id][post_channel.id].delete()
            except:
                pass
            dashboard_messages[guild_id].pop(post_channel.id, None)

        # Cancel all alarms for that post channel (runtime)
        if guild_id in alarms and post_channel.id in alarms[guild_id]:
            for _user_id, user_alarms in alarms[guild_id][post_channel.id].items():
                for alarm in user_alarms.values():
                    alarm["task"].cancel()
            alarms[guild_id].pop(post_channel.id, None)

        # Remove persisted alarms for this post channel
        data[str(guild_id)]["alarms"].pop(str(post_channel.id), None)

        # Remove lock
        dashboard_locks.pop((guild_id, post_channel.id), None)

        # Remove persistent setup mapping
        setups.pop(cmd_channel_to_remove, None)
        save_data()

        await ctx.send(f"✅ Worker setup removed for {post_channel.mention}")
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
            bid = parts[4] if len(parts) > 4 else None
            end_time = datetime.strptime(time_value, "%H:%M").time()
        except:
            await ctx.send('Invalid format. Example: `!worker + 19:55 "Eiffel" 3M`')
            return

        offset = int(data[str(guild_id)]["timezone"])

        # Determine "today" in guild-local time
        nu = now_utc()
        nu_local = utc_to_local(nu, offset)

        # Build naive local end datetime, then convert to UTC
        end_local_naive = datetime.combine(nu_local.date(), end_time)

        # If time already passed in local, schedule next day
        if end_local_naive < nu_local.replace(tzinfo=None):
            end_local_naive += timedelta(days=1)

        end_utc = local_naive_to_utc(end_local_naive, offset)

        # Replace existing alarm for this user+time, if any
        existing = alarms[guild_id][post_channel.id][ctx.author.id].get(time_value)
        if existing:
            existing["task"].cancel()
            remove_persisted_alarm(guild_id, post_channel.id, ctx.author.id, time_value)

        task = asyncio.create_task(
            run_alarm(guild_id, post_channel, ctx.author.id, end_utc, time_value, name, bid, setup_info)
        )

        alarms[guild_id][post_channel.id][ctx.author.id][time_value] = {
            "task": task,
            "name": name,
            "bid": bid,
            "end_datetime": end_utc  # UTC-aware for Discord <t:...> per viewer
        }

        # Persist alarm
        persist_alarm(guild_id, post_channel.id, ctx.author.id, time_value, name, bid, end_utc)

        # Start live dashboard if not running
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
        remove_persisted_alarm(guild_id, post_channel.id, ctx.author.id, time_value)

        await ctx.send(f"✅ Alarm {time_value} cancelled.")
        await update_dashboard(guild_id, post_channel)
        return

    await ctx.send("Unknown action. Try `!worker help`.")


# ------------------ RESTORE ON STARTUP ------------------
async def restore_persisted_alarms():
    for guild in bot.guilds:
        guild_id = guild.id
        ensure_guild(guild_id)

        persisted = data[str(guild_id)].get("alarms", {})
        if not persisted:
            continue

        for post_channel_id_str, users in list(persisted.items()):
            post_channel = await get_channel_safe(guild, int(post_channel_id_str))
            if post_channel is None:
                continue

            setup_info = find_setup_for_post_channel(guild_id, post_channel.id)

            alarms.setdefault(guild_id, {})
            alarms[guild_id].setdefault(post_channel.id, {})

            restored_any_for_channel = False

            for user_id_str, times in list(users.items()):
                user_id = int(user_id_str)
                alarms[guild_id][post_channel.id].setdefault(user_id, {})

                for time_str, a in list(times.items()):
                    try:
                        end_utc = datetime.fromisoformat(a["end_utc"])
                        if end_utc.tzinfo is None:
                            end_utc = end_utc.replace(tzinfo=timezone.utc)
                    except Exception:
                        remove_persisted_alarm(guild_id, post_channel.id, user_id, time_str)
                        continue

                    # Expired -> delete silently (no late messages)
                    if end_utc <= now_utc():
                        remove_persisted_alarm(guild_id, post_channel.id, user_id, time_str)
                        continue

                    name = a.get("name", "Unknown")
                    bid = a.get("bid")  # can be None

                    task = asyncio.create_task(
                        run_alarm(guild_id, post_channel, user_id, end_utc, time_str, name, bid, setup_info)
                    )
                    alarms[guild_id][post_channel.id][user_id][time_str] = {
                        "task": task,
                        "name": name,
                        "bid": bid,
                        "end_datetime": end_utc
                    }
                    restored_any_for_channel = True

            if restored_any_for_channel:
                dashboard_tasks.setdefault(guild_id, {})
                if post_channel.id not in dashboard_tasks[guild_id]:
                    dashboard_tasks[guild_id][post_channel.id] = asyncio.create_task(
                        live_dashboard_task(guild_id, post_channel)
                    )
                await update_dashboard(guild_id, post_channel)


# ------------------ BOT READY ------------------
@bot.event
async def on_ready():
    load_data()
    print("DATA FILE PATH:", os.path.abspath(DATA_FILE))
    print("FILE EXISTS:", os.path.exists(DATA_FILE))
    print(f"Logged in as {bot.user} (id={bot.user.id})")

    # remove old dashboard embeds left behind by previous runs
    await cleanup_old_dashboards()

    # restore alarms + restart dashboard loops
    await restore_persisted_alarms()


# ------------------ RUN BOT ------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN environment variable not set.")
bot.run(TOKEN)
