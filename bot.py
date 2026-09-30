import os
import sqlite3
import logging
import asyncio
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def required(name):
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required variable: {name}"
        )

    return value


def integer(name):
    try:
        return int(required(name))

    except Exception:
        raise RuntimeError(
            f"{name} must be a valid integer"
        )


def int_set(value):
    result = set()

    for item in value.split(","):

        item = item.strip()

        if not item:
            continue

        try:
            result.add(int(item))

        except Exception:
            logging.warning(
                "Invalid integer ignored: %s",
                item
            )

    return result


# ============================================================
# ENV VARIABLES
# ============================================================

API_ID = integer("API_ID")
API_HASH = required("API_HASH")
BOT_TOKEN = required("BOT_TOKEN")

OWNER_ID = integer("OWNER_ID")
SOURCE_CHAT_ID = integer("SOURCE_CHAT_ID")

DB_FILE = os.getenv(
    "DB_FILE",
    "lex_publisher_2.db"
).strip()


# ============================================================
# ADMINS
# ============================================================

ADMIN_IDS = int_set(
    os.getenv("ADMIN_IDS", "")
)

ADMIN_IDS.add(OWNER_ID)


# ============================================================
# TARGET CHAT IDS
# ============================================================

TARGET_CHAT_IDS = list(
    int_set(
        os.getenv("TARGET_CHAT_IDS", "")
    )
)

if not TARGET_CHAT_IDS:

    raise RuntimeError(
        "TARGET_CHAT_IDS is empty"
    )


# ============================================================
# PERSONAL CHANNELS
# ============================================================

def parse_personal_channels(value):

    result = {}

    if not value:
        return result

    for item in value.split(";"):

        item = item.strip()

        if not item or ":" not in item:
            continue

        user_part, channel_part = item.split(
            ":",
            1
        )

        try:

            user_id = int(
                user_part.strip()
            )

            channel_id = int(
                channel_part.strip()
            )

            result[user_id] = channel_id

        except Exception:

            logging.warning(
                "Invalid PERSONAL_CHANNELS entry: %s",
                item
            )

    return result


PERSONAL_CHANNELS = parse_personal_channels(
    os.getenv(
        "PERSONAL_CHANNELS",
        ""
    )
)


# ============================================================
# USER BLOCKED TARGETS
# ============================================================

def parse_blocked_targets(value):

    result = {}

    if not value:
        return result

    for item in value.split(";"):

        item = item.strip()

        if not item or ":" not in item:
            continue

        user_part, targets_part = item.split(
            ":",
            1
        )

        try:

            user_id = int(
                user_part.strip()
            )

        except Exception:

            logging.warning(
                "Invalid blocked user: %s",
                user_part
            )

            continue

        blocked = set()

        for target in targets_part.split(","):

            target = target.strip()

            if not target:
                continue

            try:

                blocked.add(
                    int(target)
                )

            except Exception:

                logging.warning(
                    "Invalid blocked target: %s",
                    target
                )

        if blocked:

            result[user_id] = blocked

    return result


USER_BLOCKED_TARGETS = parse_blocked_targets(
    os.getenv(
        "USER_BLOCKED_TARGETS",
        ""
    )
)


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.execute(
    """
    CREATE TABLE IF NOT EXISTS published_messages (
        source_msg_id INTEGER NOT NULL,
        target_chat_id INTEGER NOT NULL,
        target_msg_id INTEGER NOT NULL,
        PRIMARY KEY (
            source_msg_id,
            target_chat_id
        )
    )
    """
)

db.commit()

db_lock = asyncio.Lock()


async def save_mapping(
    source_msg_id,
    target_chat_id,
    target_msg_id
):

    async with db_lock:

        db.execute(
            """
            INSERT OR REPLACE INTO published_messages
            (
                source_msg_id,
                target_chat_id,
                target_msg_id
            )
            VALUES (?, ?, ?)
            """,
            (
                source_msg_id,
                target_chat_id,
                target_msg_id
            )
        )

        db.commit()


async def get_mappings(
    source_msg_id
):

    async with db_lock:

        cursor = db.execute(
            """
            SELECT
                target_chat_id,
                target_msg_id

            FROM published_messages

            WHERE source_msg_id = ?
            """,
            (
                source_msg_id,
            )
        )

        return cursor.fetchall()


async def delete_all_mappings(
    source_msg_id
):

    async with db_lock:

        db.execute(
            """
            DELETE FROM published_messages

            WHERE source_msg_id = ?
            """,
            (
                source_msg_id,
            )
        )

        db.commit()


# ============================================================
# BOT CLIENT
# ============================================================

bot_client = TelegramClient(
    "lex_publisher_bot_2",
    API_ID,
    API_HASH
)


# ============================================================
# USER CLIENT
# ============================================================

USER_SESSION = os.getenv(
    "USER_SESSION",
    ""
).strip()

if not USER_SESSION:

    raise RuntimeError(
        "USER_SESSION is required."
    )


user_client = TelegramClient(
    StringSession(USER_SESSION),
    API_ID,
    API_HASH
)


# ============================================================
# TARGETS
# ============================================================

def get_targets_for_user(
    sender_id: Optional[int]
):

    targets = list(
        TARGET_CHAT_IDS
    )

    if sender_id is not None:

        personal = PERSONAL_CHANNELS.get(
            sender_id
        )

        if personal:

            targets.append(
                personal
            )

    targets = list(
        dict.fromkeys(
            targets
        )
    )

    blocked = USER_BLOCKED_TARGETS.get(
        sender_id,
        set()
    )

    targets = [
        target
        for target in targets

        if target not in blocked
    ]

    return targets


# ============================================================
# SEND AS
# ============================================================

async def send_with_send_as(
    target_chat_id,
    message,
    reply_to=None
):

    try:

        sent = await user_client.send_message(
            entity=target_chat_id,
            message=message,
            reply_to=reply_to,
            send_as=target_chat_id
        )

        logging.info(
            "SEND AS SUCCESS -> %s",
            target_chat_id
        )

        return sent

    except FloodWaitError as e:

        logging.warning(
            "USER FLOOD WAIT: %s seconds",
            e.seconds
        )

        await asyncio.sleep(
            e.seconds
        )

        try:

            sent = await user_client.send_message(
                entity=target_chat_id,
                message=message,
                reply_to=reply_to,
                send_as=target_chat_id
            )

            return sent

        except Exception as retry_error:

            logging.error(
                "SEND AS RETRY FAILED -> %s: %s",
                target_chat_id,
                retry_error
            )

    except Exception as e:

        logging.warning(
            "SEND AS FAILED -> %s: %s",
            target_chat_id,
            e
        )

    return None


# ============================================================
# BOT FALLBACK
# ============================================================

async def send_with_bot(
    target_chat_id,
    message,
    reply_to=None
):

    try:

        return await bot_client.send_message(
            entity=target_chat_id,
            message=message,
            reply_to=reply_to
        )

    except FloodWaitError as e:

        logging.warning(
            "BOT FLOOD WAIT: %s seconds",
            e.seconds
        )

        await asyncio.sleep(
            e.seconds
        )

        try:

            return await bot_client.send_message(
                entity=target_chat_id,
                message=message,
                reply_to=reply_to
            )

        except Exception as retry_error:

            logging.error(
                "BOT RETRY FAILED -> %s: %s",
                target_chat_id,
                retry_error
            )

    except Exception as e:

        logging.error(
            "BOT SEND FAILED -> %s: %s",
            target_chat_id,
            e
        )

    return None


async def send_to_target(
    target_chat_id,
    message,
    reply_to=None
):

    sent = await send_with_send_as(
        target_chat_id,
        message,
        reply_to
    )

    if sent:

        return sent

    logging.info(
        "USING BOT FALLBACK -> %s",
        target_chat_id
    )

    return await send_with_bot(
        target_chat_id,
        message,
        reply_to
    )


# ============================================================
# REPLY MAPPING
# ============================================================

async def find_reply_target(
    source_reply_msg_id,
    target_chat_id
):

    mappings = await get_mappings(
        source_reply_msg_id
    )

    for chat_id, target_msg_id in mappings:

        if int(chat_id) == int(
            target_chat_id
        ):

            return target_msg_id

    return None


# ============================================================
# PUBLISH
# ============================================================

async def publish_message(
    message,
    sender_id=None
):

    if not message:
        return

    source_msg_id = message.id

    # ========================================================
    # LOCAL ONLY
    #
    # Ø£ÙŠ Ø±Ø³Ø§Ù„Ø© ØªØ¨Ø¯Ø£ Ø¨Ù€ "." Ù„Ø§ ÙŠØªÙ… Ù†Ø´Ø±Ù‡Ø§
    # ÙÙŠ Ø£ÙŠ Target ÙˆØªØ¨Ù‚Ù‰ ÙÙ‚Ø· ÙÙŠ SOURCE_CHAT_ID
    # ========================================================

    if (
        message.text
        and message.text.strip().startswith(".")
    ):

        logging.info(
            "LOCAL ONLY MESSAGE -> %s | sender=%s",
            source_msg_id,
            sender_id
        )

        return

    # ========================================================
    # NORMAL TARGETS
    # ========================================================

    targets = get_targets_for_user(
        sender_id
    )

    if not targets:

        return

    reply_source_id = None

    try:

        if message.is_reply:

            reply_msg = await message.get_reply_message()

            if reply_msg:

                reply_source_id = reply_msg.id

    except Exception as e:

        logging.warning(
            "REPLY DETECTION FAILED: %s",
            e
        )

    for target_chat_id in targets:

        try:

            reply_to = None

            if reply_source_id:

                reply_to = await find_reply_target(
                    reply_source_id,
                    target_chat_id
                )

            sent = await send_to_target(
                target_chat_id,
                message,
                reply_to
            )

            if sent:

                await save_mapping(
                    source_msg_id,
                    target_chat_id,
                    sent.id
                )

                logging.info(
                    "PUBLISHED %s -> %s -> %s",
                    source_msg_id,
                    target_chat_id,
                    sent.id
                )

        except Exception as e:

            logging.error(
                "PUBLISH ERROR -> %s: %s",
                target_chat_id,
                e
            )


# ============================================================
# CONTROL COMMANDS
# ============================================================

def is_control_command(message):

    if not message or not message.text:

        return False

    try:

        first_word = (
            message.text
            .strip()
            .split()[0]
            .lower()
        )

    except Exception:

        return False

    return first_word.startswith(
        (
            "/del",
            "/status",
            "/id",
            "/help"
        )
    )


# ============================================================
# NEW MESSAGE
# ============================================================

@bot_client.on(
    events.NewMessage(
        chats=SOURCE_CHAT_ID
    )
)
async def source_new_message(event):

    try:

        message = event.message

        if not message:

            return

        if is_control_command(message):

            return

        await publish_message(
            message,
            sender_id=event.sender_id
        )

    except Exception:

        logging.exception(
            "SOURCE NEW MESSAGE ERROR"
        )


# ============================================================
# EDIT
# ============================================================

@bot_client.on(
    events.MessageEdited(
        chats=SOURCE_CHAT_ID
    )
)
async def source_message_edited(event):

    try:

        message = event.message

        if not message:

            return

        if is_control_command(message):

            return

        # ----------------------------------------------------
        # Ø§Ù„Ø±Ø³Ø§Ø¦Ù„ Ø§Ù„ØªÙŠ ØªØ¨Ø¯Ø£ Ø¨Ù€ "." Ù„Ø§ ØªÙˆØ¬Ø¯ Ù„Ù‡Ø§ Ù†Ø³Ø® Targets
        # ----------------------------------------------------

        if (
            message.text
            and message.text.strip().startswith(".")
        ):

            return

        mappings = await get_mappings(
            message.id
        )

        if not mappings:

            return

        for target_chat_id, target_msg_id in mappings:

            try:

                edited = False

                try:

                    await user_client.edit_message(
                        entity=target_chat_id,
                        message=target_msg_id,
                        text=message.text
                    )

                    edited = True

                except Exception:

                    pass

                if not edited:

                    await bot_client.edit_message(
                        entity=target_chat_id,
                        message=target_msg_id,
                        text=message.text
                    )

            except Exception as e:

                logging.error(
                    "EDIT FAILED -> %s/%s: %s",
                    target_chat_id,
                    target_msg_id,
                    e
                )

    except Exception:

        logging.exception(
            "SOURCE EDIT ERROR"
        )


# ============================================================
# DELETE SOURCE
# ============================================================

@bot_client.on(
    events.MessageDeleted(
        chats=SOURCE_CHAT_ID
    )
)
async def source_message_deleted(event):

    try:

        for source_msg_id in event.deleted_ids:

            mappings = await get_mappings(
                source_msg_id
            )

            for target_chat_id, target_msg_id in mappings:

                try:

                    deleted = False

                    try:

                        await user_client.delete_messages(
                            entity=target_chat_id,
                            message_ids=[
                                target_msg_id
                            ]
                        )

                        deleted = True

                    except Exception:

                        pass

                    if not deleted:

                        await bot_client.delete_messages(
                            entity=target_chat_id,
                            message_ids=[
                                target_msg_id
                            ]
                        )

                except Exception as e:

                    logging.error(
                        "DELETE FAILED -> %s/%s: %s",
                        target_chat_id,
                        target_msg_id,
                        e
                    )

            await delete_all_mappings(
                source_msg_id
            )

    except Exception:

        logging.exception(
            "SOURCE DELETE ERROR"
        )


# ============================================================
# /id
# ============================================================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/id(?:@\w+)?$"
    )
)
async def command_id(event):

    if event.chat_id != SOURCE_CHAT_ID:

        return

    if event.sender_id not in ADMIN_IDS:

        return

    await event.reply(
        f"Chat ID: `{event.chat_id}`\n"
        f"User ID: `{event.sender_id}`"
    )


# ============================================================
# /status
# ============================================================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/status(?:@\w+)?$"
    )
)
async def command_status(event):

    if event.chat_id != SOURCE_CHAT_ID:

        return

    if event.sender_id not in ADMIN_IDS:

        return

    sender_id = event.sender_id

    targets = get_targets_for_user(
        sender_id
    )

    blocked = USER_BLOCKED_TARGETS.get(
        sender_id,
        set()
    )

    personal = PERSONAL_CHANNELS.get(
        sender_id
    )

    text = (
        "LEX AUTO PUBLISHER PRO\n\n"
        f"User: {sender_id}\n"
        "Admin: True\n"
        f"Allowed targets: {len(targets)}\n"
        f"Blocked targets: {len(blocked)}\n"
        f"Send As account connected: "
        f"{user_client.is_connected()}\n"
    )

    if personal:

        text += (
            f"Personal channel: {personal}\n"
        )

    await event.reply(
        text
    )


# ============================================================
# /del
# ============================================================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/del(?:@\w+)?(?:\s+(\d+))?$"
    )
)
async def command_delete(event):

    if event.chat_id != SOURCE_CHAT_ID:

        return

    if event.sender_id not in ADMIN_IDS:

        return

    source_msg_id = None

    try:

        message_id_text = (
            event.pattern_match.group(1)
        )

    except Exception:

        message_id_text = None

    if message_id_text:

        try:

            source_msg_id = int(
                message_id_text
            )

        except Exception:

            source_msg_id = None

    if source_msg_id is None:

        try:

            if event.is_reply:

                replied = await event.get_reply_message()

                if replied:

                    source_msg_id = replied.id

        except Exception as e:

            logging.error(
                "REPLY DELETE DETECTION ERROR: %s",
                e
            )

    if source_msg_id is None:

        try:

            await bot_client.delete_messages(
                entity=SOURCE_CHAT_ID,
                message_ids=[
                    event.id
                ]
            )

        except Exception:

            pass

        return

    mappings = await get_mappings(
        source_msg_id
    )

    for target_chat_id, target_msg_id in mappings:

        try:

            deleted = False

            try:

                await user_client.delete_messages(
                    entity=target_chat_id,
                    message_ids=[
                        target_msg_id
                    ]
                )

                deleted = True

            except Exception:

                pass

            if not deleted:

                await bot_client.delete_messages(
                    entity=target_chat_id,
                    message_ids=[
                        target_msg_id
                    ]
                )

            logging.info(
                "DELETED TARGET -> %s/%s",
                target_chat_id,
                target_msg_id
            )

        except Exception as e:

            logging.error(
                "TARGET DELETE ERROR -> %s/%s: %s",
                target_chat_id,
                target_msg_id,
                e
            )

    await delete_all_mappings(
        source_msg_id
    )

    try:

        await bot_client.delete_messages(
            entity=SOURCE_CHAT_ID,
            message_ids=[
                source_msg_id
            ]
        )

    except Exception as e:

        logging.error(
            "SOURCE DELETE ERROR: %s",
            e
        )

    try:

        await bot_client.delete_messages(
            entity=SOURCE_CHAT_ID,
            message_ids=[
                event.id
            ]
        )

    except Exception:

        pass


# ============================================================
# /help
# ============================================================

@bot_client.on(
    events.NewMessage(
        pattern=r"^/help(?:@\w+)?$"
    )
)
async def command_help(event):

    if event.chat_id != SOURCE_CHAT_ID:

        return

    if event.sender_id not in ADMIN_IDS:

        return

    await event.reply(
        "LEX AUTO PUBLISHER PRO\n\n"
        "/id\n"
        "/status\n"
        "/del\n"
        "/del@Merchantdz_bot\n"
        "/del 123456\n"
        "/help\n\n"
        "Reply to a post and send /del "
        "to delete it everywhere.\n\n"
        ". message\n"
        "Messages starting with '.' "
        "stay only in the main group."
    )


# ============================================================
# STARTUP
# ============================================================

async def startup():

    logging.info(
        "======================================"
    )

    logging.info(
        "LEX AUTO PUBLISHER PRO - BOT 2"
    )

    logging.info(
        "Source: %s",
        SOURCE_CHAT_ID
    )

    logging.info(
        "Targets: %s",
        TARGET_CHAT_IDS
    )

    logging.info(
        "Admins: %s",
        sorted(ADMIN_IDS)
    )

    logging.info(
        "Personal channels: %s",
        PERSONAL_CHANNELS
    )

    logging.info(
        "Blocked targets: %s",
        USER_BLOCKED_TARGETS
    )

    logging.info(
        "Database: %s",
        DB_FILE
    )

    logging.info(
        "USER_SESSION configured: %s",
        bool(USER_SESSION)
    )

    logging.info(
        "======================================"
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    await bot_client.start(
        bot_token=BOT_TOKEN
    )

    await user_client.connect()

    if not await user_client.is_user_authorized():

        raise RuntimeError(
            "USER_SESSION is invalid or expired."
        )

    await startup()

    logging.info(
        "BOT + USER SESSION RUNNING"
    )

    await asyncio.gather(
        bot_client.run_until_disconnected(),
        user_client.run_until_disconnected()
    )


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logging.info(
            "Stopped."
        )

    except Exception:

        logging.exception(
            "Fatal error"
    )
  
