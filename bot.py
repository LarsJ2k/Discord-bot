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

bot = commands.Bot(command_prefix="!worker", intents=intents)

DATA_FILE = "worker_data.json"

# ------------------ RUNTIME STORAGE ------------------
alarms = {}  # {guild_id: {post_channel_id: {user_id: {end_time_str: {"task":..., "name":..., "bid":..., "end_datetime":...}}}}}
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
def human_readable_remaining(delta):
    if delta.total_seconds() > 3600:
        return f"in {int(delta.total_seconds() // 3600)} hours"
    elif delta.total_seconds() > 60:
        return f"in {int(delta.total_seconds() // 60)} minutes"
    elif delta.total_seconds() > 0:
        return "less than a minute"
    else:
        return "finished"


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
    for c_id, info in setups.items():
        if info["post_channel_id"] == post_channel.id:
            setup = info
            break

    role_mention = f"<@&{setup['role_id']}>" if setup else ""

    embed = discord.Embed(
        title=f"🔔 Upcoming Alarms — {role_mention}",
        color=discord.Color.blue()
    )

    for user_id in post_alarms:
        for end_time_str, alarm_data in post_alarms[user_id].items():
            name = alarm_data["name"]
            bid = alarm_data["bid"]
            end_datetime = alarm_data["end_datetime"]
            begin_datetime = end_datetime - timedelta(minutes=55)
            remaining = end_datetime - get_now(guild_id)
            time_left = human_readable_remaining(remaining)

            embed.add_field(
                name=f'"{name}"',
                value=f'Bid - "{bid}"\n{begin_datetime.strftime("%H:%M")}          {end_datetime.strftime("%H:%M")}        {time_left}',
                inline=False
            )

    embed.set_footer(text="Worker Alarm System")
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
async def run_alarm(guild_id, post_channel, end_datetime, time_str, name, bid, setup):
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
        await post_channel.send(
            f"{role_mention} ❌ — Alarm for **{name}** ({bid}) was cancelled."
        )
    finally:
        # Remove alarm from storage
        for user_id, user_alarms in alarms[guild_id][post_channel.id].items():
            if time_str in user_alarms:
                user_alarms.pop(time_str)
        await update_dashboard(guild_id, post_channel)


# ------------------ WORKER COMMAND ------------------
@bot.command()
async def worker(ctx, action=None, arg1=None, arg2=None):
    guild_id = ctx.guild.id
    ensure_guild(guild_id)

    # --- HELP ---
    if action == "help":
        await ctx.send(
            "**Worker Commands**\n"
            "`!worker + HH:MM Name [Bid]` — add alarm\n"
            "`!worker - HH:MM` — remove alarm\n"
            "`!worker setup #post-channel @Role` — set post channel & role\n"
            "`!worker timezone X` — set GMT offset\n"
            "`!worker AddRole @Role` — allow role to use bot\n"
            "`!worker RemoveRole @Role`\n"
            "`!worker ListRoles`\n"
            "`!worker help`"
        )
        return

    # --- SETUP ---
    if action == "setup":
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("Admin only command.")
            return
        if not isinstance(arg1, discord.TextChannel) or not isinstance(arg2, discord.Role):
            await ctx.send("Usage: `!worker setup #post-channel @Role`")
            return

        data[str(guild_id)]["channel_setups"][str(ctx.channel.id)] = {
            "post_channel_id": arg1.id,
            "role_id": arg2.id
        }
        save_data()
        await ctx.send(f"✅ Setup complete: Alarms will post in {arg1.mention} and ping {arg2.mention}")
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
    if str(ctx.channel.id) not in data[str(guild_id)]["channel_setups"]:
        await ctx.send("This command can only be used in a setup command channel.")
        return

    if not has_permission(ctx.author, guild_id):
        await ctx.send("You do not have permission.")
        return

    setup_info = data[str(guild_id)]["channel_setups"][str(ctx.channel.id)]
    post_channel = bot.get_channel(setup_info["post_channel_id"])

    # ADD ALARM
    if action == "+":
        try:
            parts = shlex.split(ctx.message.content)
            if len(parts) < 4:
                await ctx.send("Usage: !worker + HH:MM Name [Bid]")
                return
            time_value = parts[2]
            name = parts[3]
            bid = parts[4] if len(parts) > 4 else "No bid"
            end_time = datetime.strptime(time_value, "%H:%M").time()
        except:
            await ctx.send("Invalid format. Example: !worker + 19:55 Eiffel 3M")
            return

        now = get_now(guild_id)
        end_datetime = datetime.combine(now.date(), end_time)
        if end_datetime < now:
            end_datetime += timedelta(days=1)

        alarms.setdefault(guild_id, {})
        alarms[guild_id].setdefault(post_channel.id, {})
        alarms[guild_id][post_channel.id].setdefault(ctx.author.id, {})

        task = asyncio.create_task(
            run_alarm(guild_id, post_channel, end_datetime, time_value, name, bid, setup_info)
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

        await update_dashboard(guild_id, post_channel)

    # REMOVE ALARM
    elif action == "-":
        time_value = arg1
        user_alarms = alarms[guild_id][post_channel.id].get(ctx.author.id, {})
        if time_value in user_alarms:
            user_alarms[time_value]["task"].cancel()
            await update_dashboard(guild_id, post_channel)


# ------------------ BOT READY ------------------
@bot.event
async def on_ready():
    load_data()
    print(f"Logged in as {bot.user}")


# ------------------ RUN BOT ------------------
TOKEN = os.environ["DISCORD_BOT_TOKEN"]
bot.run(TOKEN)
