"""Declarative messaging-platform catalog.

Transport implementations stay behind ``GatewayAdapter``.  The catalog gives
the Deck one stable Hermes-style schema for built-ins and installable plugin
platforms without importing every platform SDK at startup.
"""

from __future__ import annotations

from typing import Any


def field(key: str, label: str, *, required: bool = False,
          secret: bool = False, advanced: bool = False,
          placeholder: str = "", config_key: str = "",
          value_type: str = "string") -> dict[str, Any]:
    return {"key": key, "label": label, "required": required,
            "secret": secret, "advanced": advanced,
            "placeholder": placeholder, "config_key": config_key,
            "value_type": value_type}


def platform(platform_id: str, name: str, description: str,
             fields: list[dict], *, docs: str = "", builtin: bool = False) -> dict:
    return {"id": platform_id, "name": name, "description": description,
            "docs_url": docs, "fields": fields, "builtin": builtin}


PLATFORMS: tuple[dict[str, Any], ...] = (
    platform("telegram", "Telegram", "Telegram bots, groups, topics, and media.", [
        field("TELEGRAM_BOT_TOKEN", "Bot token", required=True, secret=True, placeholder="Token from @BotFather"),
        field("TELEGRAM_ALLOWED_USERS", "Allowed user IDs", advanced=True,
              config_key="allowed_users", value_type="list"),
        field("TELEGRAM_PREFIX", "Required message prefix", advanced=True,
              config_key="prefix"),
        field("TELEGRAM_POLL_TIMEOUT", "Long-poll timeout", advanced=True,
              config_key="poll_timeout", value_type="integer", placeholder="25"),
    ], docs="https://t.me/BotFather", builtin=True),
    platform("discord", "Discord", "Discord DMs, servers, channels, and threads.", [
        field("DISCORD_BOT_TOKEN", "Bot token", required=True, secret=True),
        field("DISCORD_ALLOWED_USERS", "Allowed user IDs", advanced=True,
              config_key="allowed_users", value_type="list"),
        field("DISCORD_PREFIX", "Required message prefix", advanced=True,
              config_key="prefix"),
        field("DISCORD_MENTION_ONLY", "Require mention in servers", advanced=True,
              config_key="mention_only", value_type="boolean"),
    ], docs="https://discord.com/developers/applications", builtin=True),
    platform("slack", "Slack", "Slack channels and DMs through Socket Mode.", [
        field("SLACK_BOT_TOKEN", "Bot token", required=True, secret=True, placeholder="xoxb-…"),
        field("SLACK_APP_TOKEN", "App token", required=True, secret=True, placeholder="xapp-…"),
        field("SLACK_ALLOWED_USERS", "Allowed member IDs", advanced=True),
        field("SLACK_HOME_CHANNEL", "Home channel", advanced=True),
    ], docs="https://api.slack.com/apps"),
    platform("whatsapp", "WhatsApp", "WhatsApp Web through a local bridge.", [
        field("WHATSAPP_BRIDGE_URL", "Bridge URL", required=True, placeholder="http://127.0.0.1:3000"),
        field("WHATSAPP_BRIDGE_TOKEN", "Bridge token", secret=True),
        field("WHATSAPP_ALLOWED_USERS", "Allowed users", advanced=True),
        field("WHATSAPP_HOME_CHANNEL", "Home chat", advanced=True),
    ]),
    platform("whatsapp_cloud", "WhatsApp Business Cloud", "Official Meta WhatsApp Cloud API.", [
        field("WHATSAPP_CLOUD_ACCESS_TOKEN", "Access token", required=True, secret=True),
        field("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "Phone number ID", required=True),
        field("WHATSAPP_CLOUD_VERIFY_TOKEN", "Webhook verify token", required=True, secret=True),
        field("WHATSAPP_CLOUD_APP_SECRET", "App secret", secret=True),
    ], docs="https://developers.facebook.com/docs/whatsapp/cloud-api"),
    platform("signal", "Signal", "Signal messages through a signal-cli REST bridge.", [
        field("SIGNAL_API_URL", "signal-cli REST URL", required=True, placeholder="http://127.0.0.1:8080"),
        field("SIGNAL_PHONE_NUMBER", "Registered phone number", required=True),
        field("SIGNAL_ALLOWED_USERS", "Allowed phone numbers", advanced=True),
    ], docs="https://github.com/bbernhard/signal-cli-rest-api"),
    platform("bluebubbles", "BlueBubbles", "iMessage through a BlueBubbles server.", [
        field("BLUEBUBBLES_URL", "Server URL", required=True),
        field("BLUEBUBBLES_PASSWORD", "Server password", required=True, secret=True),
        field("BLUEBUBBLES_ALLOWED_USERS", "Allowed handles", advanced=True),
    ], docs="https://bluebubbles.app"),
    platform("photon", "iMessage via Photon", "Photon Spectrum's managed iMessage bridge.", [
        field("PHOTON_PROJECT_ID", "Spectrum project ID", required=True),
        field("PHOTON_PROJECT_SECRET", "Project secret", required=True, secret=True),
        field("PHOTON_ALLOWED_USERS", "Allowed phone numbers", advanced=True),
        field("PHOTON_SPECTRUM_HOST", "Spectrum API host", advanced=True),
    ], docs="https://app.photon.codes"),
    platform("email", "Email", "IMAP inbox polling with SMTP replies.", [
        field("EMAIL_ADDRESS", "Email address", required=True),
        field("EMAIL_PASSWORD", "Password or app password", required=True, secret=True),
        field("EMAIL_SMTP_HOST", "SMTP host", required=True),
        field("EMAIL_SMTP_PORT", "SMTP port", placeholder="587"),
        field("EMAIL_IMAP_HOST", "IMAP host"),
        field("EMAIL_ALLOWED_USERS", "Allowed senders", advanced=True),
    ]),
    platform("homeassistant", "Home Assistant", "State events and notifications through Home Assistant.", [
        field("HASS_TOKEN", "Long-lived access token", required=True, secret=True),
        field("HASS_URL", "Home Assistant URL", placeholder="http://homeassistant.local:8123"),
    ], docs="https://www.home-assistant.io/docs/authentication/"),
    platform("mattermost", "Mattermost", "Mattermost teams, channels, and direct messages.", [
        field("MATTERMOST_URL", "Server URL", required=True),
        field("MATTERMOST_TOKEN", "Bot token", required=True, secret=True),
        field("MATTERMOST_TEAM", "Team"),
        field("MATTERMOST_ALLOWED_USERS", "Allowed users", advanced=True),
        field("MATTERMOST_ALLOWED_CHANNELS", "Allowed channels", advanced=True),
    ]),
    platform("matrix", "Matrix", "Matrix rooms and encrypted-capable clients.", [
        field("MATRIX_HOMESERVER", "Homeserver URL", required=True),
        field("MATRIX_USER_ID", "Bot user ID", required=True),
        field("MATRIX_ACCESS_TOKEN", "Access token", required=True, secret=True),
        field("MATRIX_ALLOWED_USERS", "Allowed users", advanced=True),
    ], docs="https://matrix.org/docs/"),
    platform("dingtalk", "DingTalk", "DingTalk Stream Mode conversations.", [
        field("DINGTALK_CLIENT_ID", "Client ID", required=True),
        field("DINGTALK_CLIENT_SECRET", "Client secret", required=True, secret=True),
        field("DINGTALK_WEBHOOK_URL", "Robot webhook URL"),
        field("DINGTALK_ALLOWED_USERS", "Allowed users", advanced=True),
    ], docs="https://open-dev.dingtalk.com"),
    platform("feishu", "Feishu / Lark", "Feishu or Lark chats over the official event API.", [
        field("FEISHU_APP_ID", "App ID", required=True),
        field("FEISHU_APP_SECRET", "App secret", required=True, secret=True),
        field("FEISHU_DOMAIN", "Domain", placeholder="feishu or lark"),
        field("FEISHU_ALLOWED_USERS", "Allowed users", advanced=True),
    ], docs="https://open.feishu.cn/"),
    platform("wecom", "WeCom", "WeCom Smart Robot over WebSocket.", [
        field("WECOM_BOT_ID", "Bot ID", required=True),
        field("WECOM_SECRET", "Secret", required=True, secret=True),
        field("WECOM_WEBSOCKET_URL", "WebSocket URL"),
        field("WECOM_ALLOWED_USERS", "Allowed users", advanced=True),
    ]),
    platform("wecom_callback", "WeCom Callback", "WeCom self-built app callback mode.", [
        field("WECOM_CALLBACK_CORP_ID", "Corp ID", required=True),
        field("WECOM_CALLBACK_CORP_SECRET", "Corp secret", required=True, secret=True),
        field("WECOM_CALLBACK_AGENT_ID", "Agent ID", required=True),
        field("WECOM_CALLBACK_TOKEN", "Verification token", required=True, secret=True),
        field("WECOM_CALLBACK_ENCODING_AES_KEY", "Encoding AES key", required=True, secret=True),
    ]),
    platform("weixin", "Weixin", "Weixin bot messaging.", [
        field("WEIXIN_APP_ID", "App ID", required=True),
        field("WEIXIN_APP_SECRET", "App secret", required=True, secret=True),
        field("WEIXIN_TOKEN", "Verification token", secret=True),
        field("WEIXIN_ALLOWED_USERS", "Allowed users", advanced=True),
    ]),
    platform("qqbot", "QQBot", "QQ Open Platform bot messaging.", [
        field("QQBOT_APP_ID", "App ID", required=True),
        field("QQBOT_CLIENT_SECRET", "Client secret", required=True, secret=True),
        field("QQBOT_TOKEN", "Bot token", secret=True),
        field("QQBOT_ALLOWED_USERS", "Allowed users", advanced=True),
    ], docs="https://q.qq.com/"),
    platform("google_chat", "Google Chat", "Google Chat callbacks or Pub/Sub events.", [
        field("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", "Service-account JSON", required=True, secret=True),
        field("GOOGLE_CHAT_HTTP_EVENTS_URL", "HTTP event callback URL"),
        field("GOOGLE_CHAT_PROJECT_ID", "Google Cloud project ID"),
        field("GOOGLE_CHAT_SUBSCRIPTION_NAME", "Pub/Sub subscription"),
        field("GOOGLE_CHAT_ALLOWED_USERS", "Allowed emails", advanced=True),
    ], docs="https://console.cloud.google.com/"),
    platform("teams", "Microsoft Teams", "Microsoft Bot Framework conversations.", [
        field("TEAMS_CLIENT_ID", "Azure application client ID", required=True),
        field("TEAMS_CLIENT_SECRET", "Client secret", required=True, secret=True),
        field("TEAMS_TENANT_ID", "Tenant ID", required=True),
        field("TEAMS_PORT", "Webhook port", advanced=True, placeholder="3978"),
        field("TEAMS_ALLOWED_USERS", "Allowed users", advanced=True),
    ], docs="https://portal.azure.com/"),
    platform("line", "LINE", "LINE Messaging API callbacks and replies.", [
        field("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token", required=True, secret=True),
        field("LINE_CHANNEL_SECRET", "Channel secret", required=True, secret=True),
        field("LINE_PUBLIC_URL", "Public callback URL"),
        field("LINE_ALLOWED_USERS", "Allowed users", advanced=True),
    ], docs="https://developers.line.biz/console/"),
    platform("irc", "IRC", "Direct IRC connection using the standard protocol.", [
        field("IRC_SERVER", "Server", required=True, placeholder="irc.libera.chat"),
        field("IRC_CHANNEL", "Channel", required=True, placeholder="#variant-1"),
        field("IRC_NICKNAME", "Nickname", required=True, placeholder="variant1-bot"),
        field("IRC_PORT", "Port", advanced=True, placeholder="6697"),
        field("IRC_USE_TLS", "Use TLS", advanced=True, placeholder="true"),
        field("IRC_SERVER_PASSWORD", "Server password", secret=True, advanced=True),
        field("IRC_ALLOWED_USERS", "Allowed nicks", advanced=True),
    ]),
    platform("ntfy", "ntfy", "Lightweight ntfy topic subscription and replies.", [
        field("NTFY_TOPIC", "Subscribe topic", required=True),
        field("NTFY_SERVER_URL", "Server URL", placeholder="https://ntfy.sh"),
        field("NTFY_TOKEN", "Auth token", secret=True),
        field("NTFY_PUBLISH_TOPIC", "Reply topic"),
    ], docs="https://ntfy.sh/docs/"),
    platform("sms", "SMS (Twilio)", "SMS through Twilio REST and webhooks.", [
        field("TWILIO_ACCOUNT_SID", "Account SID", required=True),
        field("TWILIO_AUTH_TOKEN", "Auth token", required=True, secret=True),
        field("TWILIO_PHONE_NUMBER", "Twilio phone number", required=True),
        field("SMS_ALLOWED_USERS", "Allowed phone numbers", advanced=True),
    ], docs="https://www.twilio.com/"),
    platform("simplex", "SimpleX Chat", "SimpleX daemon WebSocket bridge.", [
        field("SIMPLEX_WS_URL", "Daemon WebSocket URL", required=True),
        field("SIMPLEX_ALLOWED_USERS", "Allowed contacts", advanced=True),
        field("SIMPLEX_AUTO_ACCEPT", "Auto-accept contacts", advanced=True),
    ]),
    platform("a2a", "A2A", "Linux Foundation Agent-to-Agent protocol endpoint.", [
        field("A2A_PEER_TOKENS", "Per-peer tokens", secret=True),
        field("A2A_BEARER_TOKEN", "Shared bearer token", secret=True),
        field("A2A_HOST", "Bind host", placeholder="127.0.0.1"),
        field("A2A_PORT", "Port", placeholder="9900"),
        field("A2A_AGENT_NAME", "Agent name"),
    ]),
    platform("buzz", "Buzz", "Buzz community relay over Nostr.", [
        field("BUZZ_RELAY_URL", "Relay URL", required=True),
        field("BUZZ_PRIVATE_KEY", "Nostr private key", required=True, secret=True),
        field("BUZZ_CHANNELS", "Channels", advanced=True),
        field("BUZZ_CLI_PATH", "Buzz CLI path", advanced=True),
    ]),
    platform("raft", "Raft", "Raft workspace wake-channel bridge.", [
        field("RAFT_PROFILE", "Raft profile", required=True),
    ]),
)

BY_ID = {item["id"]: item for item in PLATFORMS}


def definitions() -> list[dict[str, Any]]:
    import copy
    return [copy.deepcopy(item) for item in PLATFORMS]
