"""What kind of line a row in `messages` is, and the app's session id."""

# WP-42.3: what kind of line a message is. Everything she says lives in
# `messages` (Talk filters to chat, Margins shows the rest, asides are recent
# remarks), instead of a second table or matching text against the remark
# tables. `chat` is the conversation itself (his lines and her replies).
CHAT = "chat"
REMARK = "remark"  # a spoken screen reaction
EVENT = "event"  # a line reacting to a desktop event (song, guest, unhush…)
PROACTIVE = "proactive"  # a push she starts (morning greeting, calendar heads-up)
EMAIL = "email"  # her "btw, you've got mail from X" (emails.py)
ALARM = "alarm"
# WP-44: her line about a server/PC alert; lives in Systems, never in the
# Margins with her chatter (WP-42 2.8)
ALERT = "alert"
# her unprompted lines that can wait in Talk to be answered (asides);
# alarms just ring
ASIDE_KINDS = (REMARK, EVENT, PROACTIVE, EMAIL)

# Mario's one conversation, shared by the tray window and the app; lines she
# starts on her own (pushes, mail, alarms) belong to it too
APP_SESSION = "dashboard"
