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

bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "wrk_data.json"

# ------------------ RUNTIME STORAGE ------------------
alarms = {}  # {guild_id: {post_channel_id: {user_id: {time: {"task":..., "name":..., "bid":...}}}}}
dashboard_messages = {}  # {guild_id: {post_channel_id: message}}
dashboard_tasks = {}  # {guild_id: {post_channel_id: task}}
data = {}  # persistent storage


# ------------------ DATA HANDLING ------------------
def load_data():
    global data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}


def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)


def ensure_guild(guild_id):
    if str(guild_id) not in data:
        data[str(guild_id)] = {
            "roles": [],
            "channel_setups": {},
            "timezone": 0
        }


# ------------------ PERMISSION CHECK ------------------
def has_permission(member, guild_id):
    ensure_guild(guild_id)
    if member.guild_permissions.administrator:
        return True
    allowed_roles = data[str(guild_id)]["roles"]
    return any(role.id in allowed_roles for role in member.roles)


# ------------------ TIME ------------------
def get_now(guild_id):
    ensure_guild(guild_id)
    offset = data[str(guild_id)]["timezone"]
    return datetime.now(timezone.utc) + timedelta(hours=offset)


# ------------------ DASHBOARD ------------------
async def update_dashboard(guild_id, post_channel):
    post_alarms = alarms.get(guild_id, {}).get(post_channel.id, {})

    # Remove dashboard if empty
    if not any(post_alarms.values()):
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

    # Build embed
    setup = None
    ensure_guild(guild_id)
    setups = data[str(guild_id)]["channel_setups"]
    if str(post_channel.id) in [s["post_channel_id"] for s in setups.values()]:
        # Find the setup matching this post channel
        for c_id, info in setups.items():
            if info["post_channel_id"] == post_channel.id:
                setup = info
                break
    role_mention = f"<@&{setup['role_id']}>" if setup else ""

    embed = discord.Embed(
        title=f"🔔 Upcoming Alarms — {role_mention}",
        color=discord.Color.blue()
    )

    alarm_list = []
    for user_id in post_alarms:
        for time_str, alarm_data in post_alarms[user_id].items():
            name = alarm_data["name"]
            bid = alarm_data["bid"]
            remaining = alarm_data.get("target_datetime", get_now(guild_id)) - get_now(guild_id)
            h, rem = divmod(int(remaining.total_seconds()), 3600)
            m, s = divmod(rem, 60)
            time_left = f"{h:02d}:{m:02d}:{s:02d}"
            alarm_list.append(f"**{time_str}** — {name} ({bid}) — <@{user_id}> — {time_left}")

    alarm_list.sort()
    embed.description = "\n".join(alarm_list)
    embed.set_footer(text="WRK Alarm System")

    # Send or edit message
    if guild_id not in dashboard_messages:
        dashboard_messages[guild_id] = {}
    if post_channel.id not in dashboard_messages[guild_id]:
        msg = await post_channel.send(embed=embed)
        dashboard_messages[guild_id][post_channel.id] = msg
    else:
        await dashboard_messages[guild_id][post_channel.id].edit(embed=embed)


# ------------------ LIVE DASHBOARD TASK ------------------
async def live_dashboard_task(guild_id, post_channel):
    try:
        while True:
            await update_dashboard(guild_id, post_channel)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass


# ------------------ ALARM TASK ------------------
async def run_alarm(ctx, guild_id, post_channel, target_datetime, time_string, name, bid):
    setup = None
    for cmd_id, info in data[str(guild_id)]["channel_setups"].items():
        if info["post_channel_id"] == post_channel.id:
            setup = info
            break
    role_mention = f"<@&{setup['role_id']}>" if setup else ""

    warnings = [10, 5, 1]
    try:
        for minutes in warnings:
            wait_seconds = (target_datetime - timedelta(minutes=minutes) - get_now(guild_id)).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                await post_channel.send(
                    f"{role_mention} ⏳ {ctx.author.mention} — {minutes} minute(s) until **{name}** ({bid}) at {time_string}!"
                )

        wait_seconds = (target_datetime - get_now(guild_id)).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        await post_channel.send(
            f"{role_mention} 🚨 {ctx.author.mention} — ALARM for **{name}** ({bid}) — {time_string}!"
        )
    except asyncio.CancelledError:
        await post_channel.send(
            f"{role_mention} ❌ {ctx.author.mention} — Alarm for **{name}** ({bid}) was cancelled."
        )
    finally:
        alarms[guild_id][post_channel.id][ctx.author.id].pop(time_string, None)
        await update_dashboard(guild_id, post_channel)


# ------------------ WRK COMMAND ------------------
@bot.command()
async def wrk(ctx, action=None, arg1=None, arg2=None):
    guild_id = ctx.guild.id
    ensure_guild(guild_id)

    # --- HELP ---
    if action == "help":
        await ctx.send(
            "**WRK Commands**\n"
            "`!wrk + HH:MM Name [Bid]`\n"
            "`!wrk - HH:MM`\n"
            "`!wrk timezone X`\n"
            "`!wrk setup #post-channel @Role`\n"
            "`!wrk AddRole @Role`\n"
            "`!wrk RemoveRole @Role`\n"
            "`!wrk ListRoles`\n"
            "`!wrk help`"
        )
        return

    # --- SETUP ---
    if action == "setup":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("Admin only command.")
            return
        if not isinstance(arg1, discord.TextChannel) or not isinstance(arg2, discord.Role):
            await ctx.send("Usage: `!wrk setup #post-channel @Role`")
            return

        data[str(guild_id)]["channel_setups"][str(ctx.channel.id)] = {
            "post_channel_id": arg1.id,
            "role_id": arg2.id
        }
        save_data()
        await ctx.send(f"✅ Setup complete: Alarms will post in {arg1.mention} and tag {arg2.mention}")
        return

    # --- ROLE MANAGEMENT ---
    if action in ["AddRole", "RemoveRole", "ListRoles"]:
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("Admin only command.")
            return
        roles_list = data[str(guild_id)]["roles"]
        if action == "AddRole" and isinstance(arg1, discord.Role):
            roles_list.append(arg1.id)
            save_data()
            await ctx.send(f"Role {arg1.name} added.")
        elif action == "RemoveRole" and isinstance(arg1, discord.Role):
            roles_list.remove(arg1.id)
            save_data()
            await ctx.send(f"Role {arg1.name} removed.")
        elif action == "ListRoles":
            mentions = [f"<@&{r}>" for r in roles_list]
            await ctx.send("Allowed roles:\n" + "\n".join(mentions))
        return

    # --- TIMEZONE ---
    if action == "timezone":
        try:
            offset = int(arg1)
            data[str(guild_id)]["timezone"] = offset
            save_data()
            await ctx.send(f"Timezone set to GMT{offset:+}")
        except:
            await ctx.send("Use a number between -12 and +14.")
        return

    # --- ADD / REMOVE ALARM ---
    # Restricted to command channels that have a setup
    if str(ctx.channel.id) not in data[str(guild_id)]["channel_setups"]:
        await ctx.send("This command can only be used in a setup command channel.")
        return

    if not has_permission(ctx.author, guild_id):
        await ctx.send("You do not have permission.")
        return

    setup_info = data[str(guild_id)]["channel_setups"][str(ctx.channel.id)]
    post_channel = bot.get_channel(setup_info["post_channel_id"])

    # ADD
    if action == "+":
        try:
            parts = shlex.split(ctx.message.content)
            if len(parts) < 4:
                await ctx.send("Usage: !wrk + HH:MM Name [Bid]")
                return
            time_value = parts[2]
            name = parts[3]
            bid = parts[4] if len(parts) > 4 else "No bid"
            target_time = datetime.strptime(time_value, "%H:%M").time()
        except:
            await ctx.send("Invalid format. Example: !wrk + 19:55 Eiffel 3M")
            return

        now = get_now(guild_id)
        target_datetime = datetime.combine(now.date(), target_time)
        if target_datetime < now:
            target_datetime += timedelta(days=1)

        alarms.setdefault(guild_id, {})
        alarms[guild_id].setdefault(post_channel.id, {})
        alarms[guild_id][post_channel.id].setdefault(ctx.author.id, {})

        task = asyncio.create_task(
            run_alarm(ctx, guild_id, post_channel, target_datetime, time_value, name, bid)
        )
        alarms[guild_id][post_channel.id][ctx.author.id][time_value] = {
            "task": task,
            "name": name,
            "bid": bid,
            "target_datetime": target_datetime
        }

        # Start live dashboard if not running
        dashboard_tasks.setdefault(guild_id, {})
        if post_channel.id not in dashboard_tasks[guild_id]:
            dashboard_tasks[guild_id][post_channel.id] = asyncio.create_task(
                live_dashboard_task(guild_id, post_channel)
            )

        await update_dashboard(guild_id, post_channel)

    # REMOVE
    elif action == "-":
        time_value = arg1
        if time_value in alarms[guild_id][post_channel.id][ctx.author.id]:
            alarms[guild_id][post_channel.id][ctx.author.id][time_value]["task"].cancel()
            await update_dashboard(guild_id, post_channel)


# ------------------ BOT READY ------------------
@bot.event
async def on_ready():
    load_data()
    print(f"Logged in as {bot.user}")


bot.run("YOUR_BOT_TOKEN_HERE")
