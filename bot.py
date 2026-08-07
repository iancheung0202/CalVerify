import discord
import logging
import os
import asyncio
import json
import io
import aiosqlite
import matplotlib
import matplotlib.pyplot as plt
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from zoneinfo import ZoneInfo
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
matplotlib.use("Agg")

client: Optional[discord.Client] = None
guild: Optional[discord.Guild] = None
tree: Optional[app_commands.CommandTree] = None
bot_task = None

TOKEN = os.getenv("BOT_TOKEN")
GUILD = int(os.getenv("DISCORD_GUILD_ID", "0"))
MAIN_ROLE = int(os.getenv("DISCORD_MAIN_ROLE_ID", "0"))
ROLES = {
    "Class of 2030": None,
    "Class of 2029": None,
    "Class of 2028": None,
    "Class of 2027": None,
    "Class of 2026": None,
    "Class of 2025": None,
    "Class of 2024": None,
    "Class of 2023": None,
    "Transfer Class of 2028": None,
    "Transfer Class of 2027": None,
    "Transfer Class of 2026": None,
    "Transfer Class of 2025": None,
    "Transfer Class of 2024": None,
    "Grad & Professional Student": None,
    "UCEAP": None
}

VC_ID = int(os.getenv("DISCORD_VC_CHANNEL_ID", "0"))
VC_DB = "vc_whitelist.db"

RULES_TEXT = (
    "By joining this voice channel, in addition to the following rules, you agree to **abide by the main server rules** in <#1009920353604218930> "
    "and the [Berkeley Code of Student Conduct](https://conduct.berkeley.edu/code-of-conduct/).\n\n"
    "- Avoid spamming, echo effects, or annoying use of your microphone or soundboard.\n"
    "- Do not record or distribute any VC audio or video without the explicit, collective consent of all participants.\n"
    "- Moderators may join the VC randomly to perform checks. If a moderator is not present and you see any rule violations, you must ping a moderator and report immediately.\n"
    "- Only UC Berkeley students may join this voice channel. Misconduct may result in instant removal from the server and reporting to UC Berkeley administration.\n"
    "- Video, streaming, and soundboard are currently disabled and may be enabled in the future if the channel is proven to be a safe and responsible space."
)

async def init_bot():
    global client, tree, bot_task
    if not TOKEN:
        logger.error("BOT_TOKEN not found in .env - Discord bot will not start")
        return False
    if not isinstance(TOKEN, str) or len(TOKEN) < 10:
        logger.error(f"BOT_TOKEN appears invalid (length: {len(TOKEN)})")
        return False
    if not GUILD or not MAIN_ROLE or not VC_ID:
        logger.error("DISCORD_GUILD_ID, DISCORD_MAIN_ROLE_ID, and DISCORD_VC_CHANNEL_ID must be set in .env")
        return False
    try:
        intents = discord.Intents.none()
        intents.voice_states = True
        intents.guilds = True
        client = discord.Client(intents=intents)
        tree = app_commands.CommandTree(client)

        @tree.command(name="activity", description="Show message activity stats for this server", guild=discord.Object(id=GUILD))
        @app_commands.describe(
            days="How many days back to analyze (default 90, max 365)",
            tz="IANA timezone for local time buckets, e.g. America/Los_Angeles (default)",
        )
        @app_commands.default_permissions(manage_guild=True)
        async def activity(
            interaction: discord.Interaction,
            days: app_commands.Range[int, 1, 365] = 90,
            tz: str = "America/Los_Angeles",
        ):
            await interaction.response.send("Generating activity stats, please wait...", ephemeral=True)
            stats = await get_message_activity(days=days, tz_name=tz)
            if not stats:
                await interaction.followup.send(
                    "Couldn't generate activity stats — the bot may not be fully ready. Check the logs.",
                    ephemeral=True,
                )
                return
            embed = build_activity_embed(stats).set_footer(text=f"Requested by {interaction.user.name}")

            hour_buf, weekday_buf = await asyncio.gather(
                asyncio.to_thread(
                    render_hour_histogram,
                    stats["hour_of_day"]["counts_by_hour"],
                    stats["hour_of_day"]["peak_hour"],
                ),
                asyncio.to_thread(
                    render_weekday_histogram,
                    stats["day_of_week"]["counts"],
                    stats["day_of_week"]["peak_day"],
                ),
            )
            hour_file = discord.File(hour_buf, filename="messages_by_hour.png")
            weekday_file = discord.File(weekday_buf, filename="messages_by_weekday.png")

            hour_embed = discord.Embed(color=0xFDB515).set_image(url="attachment://messages_by_hour.png")
            weekday_embed = discord.Embed(color=0xFDB515).set_image(url="attachment://messages_by_weekday.png")

            json_bytes = json.dumps(stats, indent=2).encode("utf-8")
            json_file = discord.File(io.BytesIO(json_bytes), filename=f"activity_{stats['timeframe_days']}d.json")

            await interaction.channel.send(
                embeds=[embed, hour_embed, weekday_embed],
                files=[hour_file, weekday_file, json_file],
            )

        @activity.error
        async def activity_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            logger.error(f"Error in /activity: {error}", exc_info=True)
            if isinstance(error, app_commands.MissingPermissions):
                message = "You need the **Manage Server** permission to run this."
            else:
                message = "Something went wrong generating activity stats."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

        @client.event
        async def on_ready():
            global guild
            logger.info(f"Discord bot connected as {client.user}")
            guild = client.get_guild(GUILD)
            if guild:
                logger.info(f"Connected to guild: {guild.name} ({guild.id})")
                await cache_roles()
                try:
                    synced = await tree.sync(guild=discord.Object(id=GUILD))
                    logger.info(f"Synced {len(synced)} slash command(s) to guild {GUILD}")
                except Exception as e:
                    logger.error(f"Error syncing slash commands: {e}")
            else:
                logger.warning(f"Guild {GUILD} not found.")
            await init_vc_db()

        @client.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            joined = after.channel is not None and after.channel.id == VC_ID and (before.channel is None or before.channel.id != VC_ID)
            if not joined:
                return
            if member.bot:
                return
            if await is_whitelisted(member.id):
                logger.info(f"Whitelisted user {member.id} joined VC")
                return
            channel = after.channel
            embed = discord.Embed(
                title="Voice Channel Rules",
                description=RULES_TEXT,
                color=0xFDB515,
            ).set_footer(text="You have 5 minutes to agree or you will be kicked from the channel.")
            view = RulesView(member=member)
            try:
                msg = await channel.send(
                    content=f"{member.mention} Please read and agree to the rules below to remain in this voice channel.",
                    embed=embed,
                    view=view,
                )
                view.message = msg
                logger.info(f"Sent VC rules prompt to user {member.id} in channel {channel.id}")
            except discord.Forbidden:
                logger.error(f"Missing permissions to send message in VC {channel.id}")
            except Exception as e:
                logger.error(f"Error sending VC rules message: {e}")

        @client.event
        async def on_error(event, *args, **kwargs):
            logger.error(f"Discord bot error in {event}", exc_info=True)

        logger.info("Starting Discord bot connection")
        bot_task = asyncio.create_task(client.start(TOKEN))
        logger.info("Discord bot startup task created")
        return True
    except Exception as e:
        logger.error(f"Error initializing Discord bot: {str(e)}", exc_info=True)
        return False


async def cache_roles():
    global ROLES
    if not guild:
        logger.warning("Guild not available for role caching")
        return
    try:
        role_list = await guild.fetch_roles()
        role_map = {role.name: role for role in role_list}
        for role_name in ROLES.keys():
            role = role_map.get(role_name)
            if role:
                ROLES[role_name] = role
            else:
                logger.warning(f"Role not found in guild: {role_name}")
    except Exception as e:
        logger.error(f"Error caching roles: {str(e)}")


async def get_user(username: str) -> Optional[discord.Member]:
    if not guild or not client or not client.is_ready():
        logger.error("Guild or Client not available")
        return None
    try:
        query = username.lstrip('@')
        qlower = query.lower()
        members = await guild.query_members(query=query, limit=100)
        if not members:
            logger.warning(f"Discord user not found via query: {username}")
            return None
        for member in members:
            if member.name.lower() == qlower:
                logger.info(f"Found Discord user by exact username: {query} (ID: {member.id})")
                return member
        if len(members) == 1:
            member = members[0]
            logger.info(f"Found single Discord member via query '{query}': {member.name} (ID: {member.id})")
            return member
        dmatch = [m for m in members if m.display_name.lower() == qlower]
        if len(dmatch) == 1:
            member = dmatch[0]
            logger.info(f"Found Discord user by display name '{query}' -> actual username: {member.name} (ID: {member.id})")
            return member
        logger.warning(f"Discord user not found via query: {username}")
        return None
    except Exception as e:
        logger.error(f"Error querying for Discord user {username}: {str(e)}")
        return None


async def get_profile(user_id: int) -> Optional[Dict[str, Any]]:
    if not guild:
        logger.error("Guild not available")
        return None
    if not client or not client.is_ready():
        logger.error("Discord bot not ready")
        return None
    try:
        member = await guild.fetch_member(user_id)
        if not member:
            logger.warning(f"Member not found in guild: {user_id}")
            return None
        user = member
        avatar = member.avatar.url if member.avatar else member.default_avatar.url
        banner = None
        accent = None
        if user.banner:
            banner = user.banner.url
        if user.accent_color:
            accent = str(user.accent_color)
        decoration = None
        if hasattr(user, 'avatar_decoration') and user.avatar_decoration:
            decoration = str(user.avatar_decoration)
        desktop = None
        mobile = None
        web = None
        if hasattr(member, 'desktop_status'):
            desktop = str(member.desktop_status)
        if hasattr(member, 'mobile_status'):
            mobile = str(member.mobile_status)
        if hasattr(member, 'web_status'):
            web = str(member.web_status)
        created = user.created_at.isoformat() if user.created_at else None
        joined = member.joined_at.isoformat() if member.joined_at else None
        primary = None
        if hasattr(user, 'primary_guild') and user.primary_guild:
            primary = str(user.primary_guild)
        role_names = [r.name for r in member.roles if r.name != "@everyone"]
        class_role = None
        for r in member.roles:
            if r.name in ROLES:
                class_role = r.name
                break
        status_text = str(member.status)
        profile = {
            "user_id": str(member.id),
            "username": member.name,
            "display_name": member.display_name,
            "avatar_url": avatar,
            "banner_url": banner,
            "accent_color": accent,
            "avatar_decoration": decoration,
            "created_at": created,
            "joined_at": joined,
            "raw_status": status_text,
            "desktop_status": desktop,
            "mobile_status": mobile,
            "web_status": web,
            "primary_guild": primary,
            "roles": role_names,
            "existing_class_role": class_role,
        }
        logger.info(f"Fetched profile for user {user_id}: {member.name}")
        return profile
    except Exception as e:
        logger.error(f"Error fetching user profile {user_id}: {str(e)}")
        return None


async def get_message_activity(days: int = 90, tz_name: str = "America/Los_Angeles") -> Optional[Dict[str, Any]]:
    if not guild:
        logger.error("Guild not available")
        return None
    if not client or not client.is_ready():
        logger.error("Discord bot not ready")
        return None
    if days <= 0:
        logger.error(f"Invalid days value for message activity stats: {days}")
        return None

    DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.warning(f"Unknown timezone '{tz_name}', falling back to UTC")
        tz_name = "UTC"
        tz = timezone.utc

    now = datetime.now(timezone.utc)
    after = now - timedelta(days=days)

    hour_counts = [0] * 24
    weekday_counts = [0] * 7
    heatmap = [[0] * 24 for _ in range(7)]  # heatmap[weekday][hour], Monday=0..Sunday=6
    daily_counts: Dict[str, int] = {}
    channel_counts: Dict[str, int] = {}
    author_counts: Dict[int, int] = {}
    author_names: Dict[int, str] = {}

    total_messages = 0
    skipped_non_member = 0
    channels_scanned = 0
    channels_skipped_no_perms = []
    channels_failed = []

    channels_to_scan = list(guild.text_channels) + list(guild.threads)
    me = guild.me

    for channel in channels_to_scan:
        if me:
            perms = channel.permissions_for(me)
            if not perms.view_channel or not perms.read_message_history:
                channels_skipped_no_perms.append(channel.name)
                continue
        channel_total = 0
        try:
            channels_scanned += 1
            async for message in channel.history(after=after, limit=None):
                if message.author.bot or message.webhook_id is not None:
                    skipped_non_member += 1
                    continue
                if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
                    skipped_non_member += 1
                    continue

                local_dt = message.created_at.astimezone(tz)
                hour = local_dt.hour
                weekday = local_dt.weekday()
                date_key = local_dt.strftime("%Y-%m-%d")

                hour_counts[hour] += 1
                weekday_counts[weekday] += 1
                heatmap[weekday][hour] += 1
                daily_counts[date_key] = daily_counts.get(date_key, 0) + 1
                author_counts[message.author.id] = author_counts.get(message.author.id, 0) + 1
                author_names[message.author.id] = str(message.author)

                channel_total += 1
                total_messages += 1
            if channel_total:
                channel_counts[channel.name] = channel_total
        except discord.Forbidden:
            channels_failed.append(channel.name)
            logger.warning(f"No permission to read history in #{channel.name}")
        except Exception as e:
            channels_failed.append(channel.name)
            logger.error(f"Error fetching history for #{channel.name}: {str(e)}")

    day_occurrences = [0] * 7
    cursor = after.astimezone(tz).date()
    end_date = now.astimezone(tz).date()
    while cursor <= end_date:
        day_occurrences[cursor.weekday()] += 1
        cursor += timedelta(days=1)
    total_days_in_range = sum(day_occurrences) or 1

    def _safe_div(n, d):
        return round(n / d, 2) if d else 0.0

    peak_hour = max(range(24), key=lambda h: hour_counts[h]) if total_messages else None
    quietest_hour = min(range(24), key=lambda h: hour_counts[h]) if total_messages else None
    peak_weekday_idx = max(range(7), key=lambda d: weekday_counts[d]) if total_messages else None
    quietest_weekday_idx = min(range(7), key=lambda d: weekday_counts[d]) if total_messages else None

    peak_cell = None
    if total_messages:
        best_day, best_hour, best_count = 0, 0, -1
        for d in range(7):
            for h in range(24):
                if heatmap[d][h] > best_count:
                    best_day, best_hour, best_count = d, h, heatmap[d][h]
        peak_cell = {"day": DAYS[best_day], "hour": best_hour, "count": best_count}

    weekday_total = sum(weekday_counts[0:5])
    weekend_total = sum(weekday_counts[5:7])
    weekday_calendar_days = sum(day_occurrences[0:5])
    weekend_calendar_days = sum(day_occurrences[5:7])

    busiest_dates = sorted(daily_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    quietest_active_dates = sorted(daily_counts.items(), key=lambda kv: kv[1])[:5]
    top_members = sorted(author_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    result = {
        "timeframe_days": days,
        "timezone": tz_name,
        "range_start_utc": after.isoformat(),
        "range_end_utc": now.isoformat(),
        "generated_at_utc": now.isoformat(),
        "total_messages": total_messages,
        "non_member_messages_excluded": skipped_non_member,
        "unique_active_members": len(author_counts),
        "channels_scanned": channels_scanned,
        "channels_skipped_no_permission": channels_skipped_no_perms,
        "channels_failed": channels_failed,
        "hour_of_day": {
            "counts_by_hour": hour_counts,
            "peak_hour": peak_hour,
            "quietest_hour": quietest_hour,
        },
        "day_of_week": {
            "counts": {DAYS[i]: weekday_counts[i] for i in range(7)},
            "avg_messages_per_occurrence": {DAYS[i]: _safe_div(weekday_counts[i], day_occurrences[i]) for i in range(7)},
            "peak_day": DAYS[peak_weekday_idx] if peak_weekday_idx is not None else None,
            "quietest_day": DAYS[quietest_weekday_idx] if quietest_weekday_idx is not None else None,
            "weekday_total": weekday_total,
            "weekend_total": weekend_total,
            "weekday_avg_per_day": _safe_div(weekday_total, weekday_calendar_days),
            "weekend_avg_per_day": _safe_div(weekend_total, weekend_calendar_days),
        },
        "heatmap_day_by_hour": {
            "description": "matrix[day_index][hour] = message count; day_index 0=Monday..6=Sunday, hour is 0-23 local time",
            "matrix": heatmap,
            "day_labels": DAYS,
            "peak_cell": peak_cell,
        },
        "daily_trend": {
            "counts_by_date": dict(sorted(daily_counts.items())),
            "avg_messages_per_day": _safe_div(total_messages, total_days_in_range),
            "busiest_dates": [{"date": d, "count": c} for d, c in busiest_dates],
            "quietest_active_dates": [{"date": d, "count": c} for d, c in quietest_active_dates],
        },
        "channel_breakdown": dict(sorted(channel_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "top_active_members": [
            {"user_id": str(uid), "name": author_names[uid], "message_count": c} for uid, c in top_members
        ],
    }
    logger.info(
        f"Message activity stats: {total_messages} messages from {len(author_counts)} members "
        f"across {channels_scanned} channels over the last {days} days"
    )
    return result


def build_activity_embed(stats: Dict[str, Any]) -> discord.Embed:
    hod = stats["hour_of_day"]
    dow = stats["day_of_week"]
    trend = stats["daily_trend"]
    peak_cell = stats["heatmap_day_by_hour"]["peak_cell"]

    embed = discord.Embed(
        title=f"📊 Message Activity — last {stats['timeframe_days']} days",
        description=f"Timezone: `{stats['timezone']}`",
        color=0xFDB515,
    )
    embed.add_field(name="Total Messages", value=f"{stats['total_messages']:,}", inline=True)
    embed.add_field(name="Active Members", value=f"{stats['unique_active_members']:,}", inline=True)
    embed.add_field(name="Channels Scanned", value=str(stats["channels_scanned"]), inline=True)

    hour_value = (
        f"Peak: **{hod['peak_hour']:02d}:00**\nQuietest: **{hod['quietest_hour']:02d}:00**"
        if hod["peak_hour"] is not None else "No messages in range"
    )
    embed.add_field(name="Hour of Day", value=hour_value, inline=True)

    day_value = (
        f"Peak: **{dow['peak_day']}**\nQuietest: **{dow['quietest_day']}**"
        if dow["peak_day"] is not None else "No messages in range"
    )
    embed.add_field(name="Day of Week", value=day_value, inline=True)

    embed.add_field(
        name="Weekday vs Weekend (avg/day)",
        value=f"Weekday: **{dow['weekday_avg_per_day']}**\nWeekend: **{dow['weekend_avg_per_day']}**",
        inline=True,
    )
    embed.add_field(name="Avg Messages / Day", value=str(trend["avg_messages_per_day"]), inline=True)

    if peak_cell:
        embed.add_field(
            name="Busiest Hour Overall",
            value=f"{peak_cell['day']} @ {peak_cell['hour']:02d}:00 ({peak_cell['count']} msgs)",
            inline=True,
        )

    busiest_dates = trend.get("busiest_dates", [])
    if busiest_dates:
        embed.add_field(
            name="Busiest Dates",
            value="\n".join(f"{d['date']}: {d['count']}" for d in busiest_dates[:5]),
            inline=True,
        )

    top_members = stats.get("top_active_members", [])
    if top_members:
        embed.add_field(
            name="Top Active Members",
            value="\n".join(f"{i + 1}. {m['name']} — {m['message_count']}" for i, m in enumerate(top_members[:5])),
            inline=True,
        )

    channels = stats.get("channel_breakdown", {})
    if channels:
        embed.add_field(
            name="Top Channels",
            value="\n".join(f"#{name}: {count}" for name, count in list(channels.items())[:5]),
            inline=True,
        )

    footer_bits = []
    if stats.get("channels_skipped_no_permission"):
        footer_bits.append(f"{len(stats['channels_skipped_no_permission'])} channel(s) skipped (no permission)")
    if stats.get("channels_failed"):
        footer_bits.append(f"{len(stats['channels_failed'])} channel(s) failed to scan")
    if footer_bits:
        embed.set_footer(text=" • ".join(footer_bits))

    return embed

BERKELEY_GOLD = "#FDB515"
BERKELEY_BLUE = "#003262"

def _style_bar_axes(ax, title: str, xlabel: str) -> None:
    ax.set_ylabel("Messages")
    ax.set_xlabel(xlabel)
    ax.set_title(title, fontsize=13, fontweight="bold", color=BERKELEY_BLUE)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#999999")
    ax.spines["bottom"].set_color("#999999")
    ax.grid(axis="y", linestyle="--", alpha=0.3)
    ax.set_axisbelow(True)


def render_hour_histogram(counts_by_hour: List[int], peak_hour: Optional[int]) -> io.BytesIO:
    """Bar chart of message counts for each hour of the day (0-23, local time)."""
    hours = list(range(24))
    colors = [BERKELEY_BLUE if h == peak_hour else BERKELEY_GOLD for h in hours]

    fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
    ax.bar(hours, counts_by_hour, color=colors, edgecolor="white", linewidth=0.5)
    ax.set_xticks(hours)
    ax.set_xticklabels([f"{h:02d}" for h in hours], fontsize=7)
    _style_bar_axes(ax, "Message Distribution by Hour of Day", "Hour of day (local time)")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf


def render_weekday_histogram(counts_by_day: Dict[str, int], peak_day: Optional[str]) -> io.BytesIO:
    """Bar chart of message counts for each day of the week (Monday-Sunday)."""
    days = list(counts_by_day.keys())
    values = list(counts_by_day.values())
    colors = [BERKELEY_BLUE if d == peak_day else BERKELEY_GOLD for d in days]

    fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
    ax.bar(days, values, color=colors, edgecolor="white", linewidth=0.5)
    ax.tick_params(axis="x", labelsize=8)
    _style_bar_axes(ax, "Message Distribution by Day of Week", "Day of week")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf


async def assign_role(user_id: int, role_name: str, send_welcome_msg: bool = True) -> bool:
    if not guild:
        logger.error("Guild not available")
        return False
    if not client or not client.is_ready():
        logger.error("Discord bot not ready")
        return False
    if role_name not in ROLES:
        logger.error(f"Unknown role: {role_name}")
        return False
    try:
        member = await guild.fetch_member(user_id)
        if not member:
            logger.error(f"Member not found: {user_id}")
            return False
        role = ROLES[role_name]
        if not role:
            logger.error(f"Role object not cached: {role_name}")
            return False
        remove = []
        for existing in member.roles:
            if existing.name in ROLES and existing.name != role_name:
                remove.append(existing)
        already_has_role = role in member.roles
        tasks = []
        if remove:
            tasks.append(member.remove_roles(*remove, reason="Replacing with new class role"))
        main_role = discord.Object(id=MAIN_ROLE)
        if not already_has_role:
            tasks.append(member.add_roles(main_role, role, reason="Verified student on dashboard"))
        if tasks:
            await asyncio.gather(*tasks)
        if remove:
            logger.info(f"Removed {len(remove)} old class role(s) from user {user_id}")
        if already_has_role:
            logger.info(f"User {user_id} already has role {role_name}")
            return True
        logger.info(f"Assigned role '{role_name}' to user {user_id}")
        try:
            embed = discord.Embed(
                title="<:bearWave:1105561126164504576> Welcome to the UC Berkeley Discord Server!",
                description=(
                    f"Hi {member.mention}! You're **officially verified** as `{role_name}`! "
                    "We are incredibly delighted to welcome you into this community, built for students like you. Please kindly review the [server rules](https://discord.com/channels/1009918541601980496/1009920353604218930). 💛💙\n"
                    "### **Ready to jump in? Here is your quick-start guide:**\n"
                    "1. Head over to <#1106664283250626671> and drop a quick intro about yourself!\n"
                    "2. Say hi in <#1009928284173242448> and chat with other students.\n"
                    "3. Got questions about classes, housing, or campus life? Don't be shy to ask in chat or <#1383249847116759161>.\n\n"
                ),
                color=0xFDB515
            ).set_footer(text="If you need any help, feel free to reach out to the moderators! Go Bears! 🐻")
            view = discord.ui.View()
            view.add_item(discord.ui.Button(emoji="👋", label="Introduce Yourself", url="https://discord.com/channels/1009918541601980496/1106664283250626671", style=discord.ButtonStyle.link))
            view.add_item(discord.ui.Button(emoji="💬", label="Chat with Students", url="https://discord.com/channels/1009918541601980496/1009928284173242448", style=discord.ButtonStyle.link))
            view.add_item(discord.ui.Button(emoji="❓", label="Ask a Question", url="https://discord.com/channels/1009918541601980496/1383249847116759161", style=discord.ButtonStyle.link))
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            logger.warning(f"Could not send DM to {user_id} (DMs disabled)")
        except Exception as e:
            logger.error(f"Error sending DM to {user_id}: {str(e)}")
        if send_welcome_msg:
            try:
                channel = await client.fetch_channel(1009928284173242448)
                if channel:
                    await channel.send(content=f"<:bearWave:1105561126164504576> {member.mention} is our newest Golden Bear here! **Go Bears!** <:besties:1525744373906542714>")
                else:
                    logger.warning("Could not find #all-class-chat channel (ID 1009928284173242448)")
            except Exception as e:
                logger.error(f"Error sending welcome message to channel: {str(e)}")
        return True
    except Exception as e:
        logger.error(f"Error assigning role to {user_id}: {str(e)}")
        return False


async def init_vc_db():
    async with aiosqlite.connect(VC_DB) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS vc_whitelist (user_id INTEGER PRIMARY KEY, agreed_at TEXT NOT NULL)")
        await db.commit()
    logger.info("VC consent DB initialized")


async def is_whitelisted(user_id: int) -> bool:
    try:
        async with aiosqlite.connect(VC_DB) as db:
            async with db.execute("SELECT 1 FROM vc_whitelist WHERE user_id = ?", (user_id,)) as cursor:
                return await cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"DB error checking whitelist for {user_id}: {e}")
        return False


async def whitelist_user(user_id: int):
    try:
        async with aiosqlite.connect(VC_DB) as db:
            await db.execute(
                "INSERT OR IGNORE INTO vc_whitelist (user_id, agreed_at) VALUES (?, ?)",
                (user_id, datetime.now(timezone.utc).isoformat())
            )
            await db.commit()
        logger.info(f"User {user_id} added to VC whitelist")
    except Exception as e:
        logger.error(f"DB error whitelisting {user_id}: {e}")


class RulesView(discord.ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=300)
        self.member = member
        self.message: Optional[discord.Message] = None
        self.agreed = False

    @discord.ui.button(label="I agree", style=discord.ButtonStyle.success)
    async def agree_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.member.id:
            await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
            return
        self.agreed = True
        await whitelist_user(self.member.id)
        button.disabled = True
        button.label = "Agreed"
        button.style = discord.ButtonStyle.secondary
        done = discord.Embed(
            title="Voice Channel Rules",
            description=RULES_TEXT,
            color=0x57F287,
        ).set_footer(text="Thank you for agreeing to the rules and being responsible in the server!")
        await interaction.response.edit_message(embed=done, view=self)
        self.stop()
        logger.info(f"User {self.member.id} agreed to VC rules")

    async def on_timeout(self):
        if self.agreed:
            return
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                timeout_embed = discord.Embed(
                    title="Voice Channel Rules",
                    description=RULES_TEXT,
                    color=0xED4245,
                ).set_footer(text="You did not agree to the rules in time and have been kicked from the channel.")
                await self.message.edit(embed=timeout_embed, view=self)
                await self.message.reply(
                    f"{self.member.mention} You did not agree to the Voice Channel rules within "
                    f"5 minutes and have been kicked from the channel."
                )
            except discord.NotFound:
                logger.warning(f"Rules message not found for user {self.member.id} on timeout")
            except Exception as e:
                logger.error(f"Error editing rules message on timeout for {self.member.id}: {e}")
        try:
            if self.member.voice and self.member.voice.channel and self.member.voice.channel.id == VC_ID:
                await self.member.move_to(None, reason="Did not agree to VC rules within 5 minutes")
                logger.info(f"Kicked user {self.member.id} from VC for not agreeing to rules")
        except discord.Forbidden:
            logger.error(f"Missing permissions to move member {self.member.id}")
        except Exception as e:
            logger.error(f"Error kicking member {self.member.id} from VC: {e}")