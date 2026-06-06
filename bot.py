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

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = 1009918541601980496  # Discord server ID
MAIN_ROLE_ID = 1506760833051394119
CLASS_ROLES = {
    # Will be fetched/cached
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

RULES_VC_ID = 1512641312724226108
VC_DB_PATH = "vc_consent.db"

RULES_EMBED_TEXT = (
    "By joining this voice channel, you agree to **abide by the main server rules** in <#1009920353604218930> "
    "and the [Berkeley Code of Student Conduct](https://conduct.berkeley.edu/code-of-conduct/).\n\n"
    "**No Mic Spamming:** Avoid spamming, echo effects, or annoying use of your microphone or soundboard.\n\n"
    "**No Unauthorized Recording:** Do not record or distribute any VC audio or video without the explicit, "
    "collective consent of all participants.\n\n"
    "**Moderation & Reporting:** Moderators will join the channel randomly to check. If you see any rule "
    "violations, you must ping a moderator and report immediately.\n\n"
    "**Accountability:** Participation is restricted to Verified Students. Misconduct may result in instant "
    "removal from the server and reporting to UC Berkeley administration."
)

# Global bot client instance
client: Optional[discord.Client] = None
guild: Optional[discord.Guild] = None
_bot_connect_task = None


async def init_discord_bot():
    """Initialize Discord bot client and connect to guild"""
    global client, _bot_connect_task
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in .env - Discord bot will not start")
        return False
    
    # Validate token format (should be a string)
    if not isinstance(BOT_TOKEN, str) or len(BOT_TOKEN) < 10:
        logger.error(f"BOT_TOKEN appears invalid (length: {len(BOT_TOKEN)})")
        return False
    
    try:
        # Create bot with minimal intents for efficiency
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        
        client = discord.Client(intents=intents)
        
        @client.event
        async def on_ready():
            global guild
            logger.info(f"✓ Discord bot connected as {client.user}")
            guild = client.get_guild(GUILD_ID)
            if guild:
                logger.info(f"✓ Connected to guild: {guild.name} ({guild.id})")
                await cache_class_roles()
            else:
                logger.warning(f"✗ Guild {GUILD_ID} not found. Bot may not be invited to the server.")
            await init_vc_db()

        @client.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ):
            """Fires whenever any guild member's voice state changes."""
            # Only care about someone newly joining our specific VC
            joined_target_vc = (
                after.channel is not None
                and after.channel.id == RULES_VC_ID
                and (before.channel is None or before.channel.id != RULES_VC_ID)
            )
            if not joined_target_vc:
                return

            # Skip bots
            if member.bot:
                return

            # Skip already-whitelisted members
            if await is_vc_whitelisted(member.id):
                logger.info(f"Whitelisted user {member.id} joined VC — skipping rules prompt")
                return

            # The voice channel itself is messageable in discord.py 2.x
            vc_channel = after.channel

            rules_embed = discord.Embed(
                title="📋 Voice Channel Rules",
                description=RULES_EMBED_TEXT,
                color=0xFDB515,  # Berkeley gold
            ).set_footer(
                text="You have 5 minutes to agree or you will be kicked from the channel."
            )

            view = VCRulesView(member=member)

            try:
                msg = await vc_channel.send(
                    content=f"{member.mention} Please read and agree to the rules below to remain in this voice channel.",
                    embed=rules_embed,
                    view=view,
                )
                view.message = msg  # Give the view a reference so on_timeout can edit it
                logger.info(f"Sent VC rules prompt to user {member.id} in channel {vc_channel.id}")
            except discord.Forbidden:
                logger.error(f"Missing permissions to send message in VC {vc_channel.id}")
            except Exception as e:
                logger.error(f"Error sending VC rules message: {e}")
        
        @client.event
        async def on_error(event, *args, **kwargs):
            logger.error(f"Discord bot error in {event}", exc_info=True)
        
        logger.info(f"Starting Discord bot connection (Token: {BOT_TOKEN[:20]}...)")
        # Start the bot in background (but don't await it)
        _bot_connect_task = asyncio.create_task(client.start(BOT_TOKEN))
        logger.info("Discord bot startup task created")
        
        return True
    
    except Exception as e:
        logger.error(f"Error initializing Discord bot: {str(e)}", exc_info=True)
        return False


async def cache_class_roles():
    """Cache class role objects from guild"""
    global CLASS_ROLES
    
    if not guild:
        logger.warning("Guild not available for role caching")
        return
    
    try:
        for role_name in CLASS_ROLES.keys():
            role = discord.utils.get(guild.roles, name=role_name)
            if role:
                CLASS_ROLES[role_name] = role
                # logger.info(f"Cached role: {role_name} (ID: {role.id})")
            else:
                logger.warning(f"Role not found in guild: {role_name}")
    except Exception as e:
        logger.error(f"Error caching roles: {str(e)}")


async def get_discord_user_by_username(username: str) -> Optional[discord.Member]:
    """
    Search for a Discord user by username in the guild.
    
    Args:
        username: Discord username to search for
    
    Returns:
        discord.Member object if found, None otherwise
    """
    if not guild:
        logger.error("Guild not available - Discord bot may not be connected")
        return None
    
    if not client or not client.is_ready():
        logger.error("Discord bot not ready")
        return None
    
    try:
        # discord.utils.get searches through the guild's cached members
        member = discord.utils.get(guild.members, name=username)
        
        if member:
            logger.info(f"Found Discord user: {username} (ID: {member.id})")
            return member
        else:
            logger.warning(f"Discord user not found: {username}")
            return None
    
    except Exception as e:
        logger.error(f"Error searching for Discord user {username}: {str(e)}")
        return None


async def get_discord_user_profile(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Fetch Discord user profile information with detailed metadata.
    
    Args:
        user_id: Discord user ID
    
    Returns:
        Dictionary with user profile data or None if user not found
    """
    if not guild:
        logger.error("Guild not available")
        return None
    
    if not client or not client.is_ready():
        logger.error("Discord bot not ready")
        return None
    
    try:
        member = guild.get_member(user_id)
        
        if not member:
            logger.warning(f"Member not found in guild: {user_id}")
            return None
        
        # Get user object for additional details
        user = member
        
        # Get avatar URL
        avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
        
        # Get banner URL (account banner, not server banner)
        banner_url = None
        accent_color = None
        if user.banner:
            banner_url = user.banner.url
        if user.accent_color:
            accent_color = str(user.accent_color)
        
        # Get avatar decoration if available
        avatar_decoration = None
        if hasattr(user, 'avatar_decoration') and user.avatar_decoration:
            avatar_decoration = str(user.avatar_decoration)
        
        # Get status details
        desktop_status = None
        mobile_status = None
        web_status = None
        if hasattr(member, 'desktop_status'):
            desktop_status = str(member.desktop_status)
        if hasattr(member, 'mobile_status'):
            mobile_status = str(member.mobile_status)
        if hasattr(member, 'web_status'):
            web_status = str(member.web_status)
        
        # Get created_at and joined_at
        created_at = user.created_at.isoformat() if user.created_at else None
        joined_at = member.joined_at.isoformat() if member.joined_at else None
        
        # Get primary guild (if available)
        primary_guild = None
        if hasattr(user, 'primary_guild') and user.primary_guild:
            primary_guild = str(user.primary_guild)
        
        # Get all roles
        all_roles = [role.name for role in member.roles[1:]]  # Exclude @everyone
        
        # Find existing class roles
        existing_class_role = None
        for role in member.roles:
            if role.name in CLASS_ROLES:
                existing_class_role = role.name
                break
        
        # Get raw status
        raw_status = str(member.status)
        
        # Compile profile data
        profile = {
            "user_id": str(member.id),
            "username": member.name,
            "display_name": member.display_name,
            "avatar_url": avatar_url,
            "banner_url": banner_url,
            "accent_color": accent_color,
            "avatar_decoration": avatar_decoration,
            "created_at": created_at,
            "joined_at": joined_at,
            "raw_status": raw_status,
            "desktop_status": desktop_status,
            "mobile_status": mobile_status,
            "web_status": web_status,
            "primary_guild": primary_guild,
            "roles": all_roles,
            "existing_class_role": existing_class_role,
        }
        
        logger.info(f"Fetched profile for user {user_id}: {member.name}")
        return profile
    
    except Exception as e:
        logger.error(f"Error fetching user profile {user_id}: {str(e)}")
        return None


async def assign_role_to_user(user_id: int, role_name: str, send_welcome_msg: bool = True) -> bool:
    """
    Assign a class role to a user. Removes any existing class roles first to ensure only one.
    
    Args:
        user_id: Discord user ID
        role_name: Name of the role
        send_welcome_msg: Whether to send a public welcome message in #all-class-chat
    
    Returns:
        True if successful, False otherwise
    """
    if not guild:
        logger.error("Guild not available")
        return False
    
    if not client or not client.is_ready():
        logger.error("Discord bot not ready")
        return False
    
    if role_name not in CLASS_ROLES:
        logger.error(f"Unknown role: {role_name}")
        return False
    
    try:
        member = guild.get_member(user_id)
        if not member:
            logger.error(f"Member not found: {user_id}")
            return False
        
        role = CLASS_ROLES[role_name]
        if not role:
            logger.error(f"Role object not cached: {role_name}")
            return False
        
        # Remove any existing class roles from the member
        roles_to_remove = []
        for existing_role in member.roles:
            if existing_role.name in CLASS_ROLES and existing_role.name != role_name:
                roles_to_remove.append(existing_role)
        
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Replacing with new class role - CalVerify")
            logger.info(f"Removed {len(roles_to_remove)} old class role(s) from user {user_id}")
        
        # Check if user already has the target role
        if role in member.roles:
            logger.info(f"User {user_id} already has role {role_name}")
            return True
        
        # Assign new role
        await member.add_roles(discord.utils.get(guild.roles, id=MAIN_ROLE_ID), reason="Verified student on dashboard")
        await member.add_roles(role, reason="Verified student on dashboard")
        logger.info(f"Assigned role '{role_name}' to user {user_id}")
        
        # Send welcome DM
        try:
            embed = discord.Embed(
                title="<:bearWave:1105561126164504576> Welcome to the UC Berkeley Discord Server!",
                description=(
                    f"Hi {member.mention}! You're **officially verified** as `{role_name}`! "
                    "We are incredibly delighted to welcome you into this community, built **for students like you**. 💛💙\n"
                    "### **Ready to jump in? Here is your quick-start guide:**\n"
                    "1. Head over to <#1106664283250626671> and drop a quick intro about yourself!\n"
                    "2. Say hi to and talk with your peers in <#1009928284173242448>.\n"
                    "3. Got questions about classes, housing, or campus life? Don't be shy to ask right in chat or in <#1383249847116759161>.\n\n"
                ),
                color=0xFDB515
            ).set_footer(text="If you need any help, feel free to reach out to the moderators! Go Bears! 🐻")
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="👋 Introduce Yourself", url="https://discord.com/channels/1009918541601980496/1106664283250626671", style=discord.ButtonStyle.link))
            view.add_item(discord.ui.Button(label="💬 Chat with Students", url="https://discord.com/channels/1009918541601980496/1009928284173242448", style=discord.ButtonStyle.link))
            view.add_item(discord.ui.Button(label="❓ Ask a Question", url="https://discord.com/channels/1009918541601980496/1383249847116759161", style=discord.ButtonStyle.link))
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            logger.warning(f"Could not send DM to {user_id} (DMs disabled)")
        except Exception as e:
            logger.error(f"Error sending DM to {user_id}: {str(e)}")

        # Send server welcome message
        if send_welcome_msg:
            try:
                channel = client.get_channel(1009928284173242448)
                if channel:
                    await channel.send(f"Welcome {member.mention} as our newest Golden Bear! <:bearWave:1105561126164504576> ")
                else:
                    logger.warning("Could not find #all-class-chat channel (ID 1009928284173242448)")
            except Exception as e:
                logger.error(f"Error sending welcome message to channel: {str(e)}")

        return True
    
    except Exception as e:
        logger.error(f"Error assigning role to {user_id}: {str(e)}")
        return False

async def init_vc_db():
    """Initialize the SQLite DB for VC consent tracking."""
    async with aiosqlite.connect(VC_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vc_whitelist (
                user_id INTEGER PRIMARY KEY,
                agreed_at TEXT NOT NULL
            )
        """)
        await db.commit()
    logger.info("✓ VC consent DB initialized")


async def is_vc_whitelisted(user_id: int) -> bool:
    """Check if a user has previously agreed to VC rules."""
    try:
        async with aiosqlite.connect(VC_DB_PATH) as db:
            async with db.execute(
                "SELECT 1 FROM vc_whitelist WHERE user_id = ?", (user_id,)
            ) as cursor:
                return await cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"DB error checking whitelist for {user_id}: {e}")
        return False


async def add_to_vc_whitelist(user_id: int):
    """Permanently whitelist a user after they agree to VC rules."""
    try:
        async with aiosqlite.connect(VC_DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO vc_whitelist (user_id, agreed_at) VALUES (?, ?)",
                (user_id, datetime.now(timezone.utc).isoformat())
            )
            await db.commit()
        logger.info(f"User {user_id} added to VC whitelist")
    except Exception as e:
        logger.error(f"DB error whitelisting {user_id}: {e}")


class VCRulesView(discord.ui.View):
    """
    View with an 'I agree' button for VC rules.
    - On agree: edits message to confirmed state, whitelists user, stops.
    - On timeout (5 min): disables button, edits message, replies pinging user, kicks from VC.
    """

    def __init__(self, member: discord.Member):
        super().__init__(timeout=300)  # 5 minutes
        self.member = member
        self.message: Optional[discord.Message] = None  # Set after sending
        self.agreed = False

    @discord.ui.button(label="I agree", style=discord.ButtonStyle.success)
    async def agree_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Only the target member can click
        if interaction.user.id != self.member.id:
            await interaction.response.send_message(
                "This prompt is not for you.", ephemeral=True
            )
            return

        self.agreed = True
        await add_to_vc_whitelist(self.member.id)

        button.disabled = True
        button.label = "✅  Agreed"
        button.style = discord.ButtonStyle.secondary

        confirmed_embed = discord.Embed(
            title="📋 Voice Channel Rules",
            description=RULES_EMBED_TEXT,
            color=0x57F287,  # Green
        ).set_footer(text="Thank you for agreeing to the rules and being responsible in the server!")

        await interaction.response.edit_message(embed=confirmed_embed, view=self)
        self.stop()
        logger.info(f"User {self.member.id} agreed to VC rules")

    async def on_timeout(self):
        """Fires after 5 minutes if user never clicked I Agree."""
        if self.agreed:
            return  # Already handled, race condition guard

        # Disable the button on the original message
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                timed_out_embed = discord.Embed(
                    title="📋 Voice Channel Rules",
                    description=RULES_EMBED_TEXT,
                    color=0xED4245,  # Red
                ).set_footer(
                    text="You did not agree to the rules in time and have been kicked from the channel."
                )
                await self.message.edit(embed=timed_out_embed, view=self)

                # Reply to the rules message pinging the user
                await self.message.reply(
                    f"{self.member.mention} You did not agree to the Voice Channel rules within "
                    f"5 minutes and have been kicked from the channel."
                )
            except discord.NotFound:
                logger.warning(f"Rules message not found for user {self.member.id} on timeout")
            except Exception as e:
                logger.error(f"Error editing rules message on timeout for {self.member.id}: {e}")

        # Kick the member from the VC if they're still in it
        try:
            if (
                self.member.voice
                and self.member.voice.channel
                and self.member.voice.channel.id == RULES_VC_ID
            ):
                await self.member.move_to(
                    None, reason="Did not agree to VC rules within 5 minutes"
                )
                logger.info(f"Kicked user {self.member.id} from VC for not agreeing to rules")
        except discord.Forbidden:
            logger.error(f"Missing permissions to move member {self.member.id}")
        except Exception as e:
            logger.error(f"Error kicking member {self.member.id} from VC: {e}")