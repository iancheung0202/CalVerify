import discord
import logging
import os
import asyncio
import aiosqlite
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

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

client: Optional[discord.Client] = None
guild: Optional[discord.Guild] = None
bot_task = None


async def init_bot():
    global client, bot_task
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

        @client.event
        async def on_ready():
            global guild
            logger.info(f"Discord bot connected as {client.user}")
            guild = client.get_guild(GUILD)
            if guild:
                logger.info(f"Connected to guild: {guild.name} ({guild.id})")
                await cache_roles()
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
        try:
            member_data = await client.http.request(
                discord.http.Route("GET", "/guilds/{guild_id}/members/{user_id}", guild_id=GUILD, user_id=user_id)
            )
            rids = set(member_data.get("roles", []))
        except Exception as e:
            logger.warning(f"Could not fetch role IDs via REST for {user_id}: {e}")
            rids = set()
        role_map = {str(r.id): r.name for r in guild.roles}
        role_names = [role_map[rid] for rid in rids if rid in role_map]
        class_role = None
        for rid in rids:
            name = role_map.get(rid)
            if name in ROLES:
                class_role = name
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
        if remove:
            await member.remove_roles(*remove, reason="Replacing with new class role - CalVerify")
            logger.info(f"Removed {len(remove)} old class role(s) from user {user_id}")
        if role in member.roles:
            logger.info(f"User {user_id} already has role {role_name}")
            return True
        main_role = discord.Object(id=MAIN_ROLE)
        await member.add_roles(main_role, reason="Verified student on dashboard")
        await member.add_roles(role, reason="Verified student on dashboard")
        logger.info(f"Assigned role '{role_name}' to user {user_id}")
        try:
            embed = discord.Embed(
                title="<:bearWave:1105561126164504576> Welcome to the UC Berkeley Discord Server!",
                description=(
                    f"Hi {member.mention}! You're **officially verified** as `{role_name}`! "
                    "We are incredibly delighted to welcome you into this community, built **for students like you**. Please kindly review the [server rules](https://discord.com/channels/1009918541601980496/1009920353604218930). 💛💙\n"
                    "### **Ready to jump in? Here is your quick-start guide:**\n"
                    "1. Head over to <#1106664283250626671> and drop a quick intro about yourself!\n"
                    "2. Say hi in <#1009928284173242448> and chat with other students.\n"
                    "3. Got questions about classes, housing, or campus life? Don't be shy to ask right in chat or in <#1383249847116759161>.\n\n"
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
