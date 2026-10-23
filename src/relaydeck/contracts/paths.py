"""Every `/v1` path, defined once: the gateway, the web app and every client call use these."""

from __future__ import annotations

import re
from urllib.parse import quote

# Pictures (companion).
PICTURES = "/v1/pictures"
PICTURE_CONTENT = "/v1/pictures/{identifier}/content"
PICTURE = "/v1/pictures/{identifier}"

# Pairing.
PAIRING_REQUESTS = "/v1/pairing/requests"
PAIRING_STATUS = "/v1/pairing/{request_id}/status"
PAIRING_REDEEM = "/v1/pairing/{request_id}/redeem"

# Sign-in.
AUTHORIZE = "/v1/authorize"
IDENTITY_CALLBACK = "/v1/identity/callback"
TOKEN = "/v1/token"  # noqa: S105 - URL path
LOGOUT = "/v1/logout"

# Devices and notifications.
DEVICES = "/v1/devices"
DEVICE_ROTATE = "/v1/devices/{device_id}/rotate"
DEVICE = "/v1/devices/{device_id}"
DEEP_LINK_RESOLVE = "/v1/deep-links/resolve"
PUSH_KEY = "/v1/push/web/key"
PUSH_SUBSCRIPTION = "/v1/push/web/subscription"
PUSH_RESOLVE = "/v1/push/web/resolve"
PUSH_PREFS = "/v1/push/prefs"

# Host enrollment and the connector tunnel.
ENROLLMENTS = "/v1/enrollments"
ENROLLMENT = "/v1/enrollments/{enrollment_id}"
ENROLLMENT_REDEEM = "/v1/enrollments/{enrollment_id}/redeem"
CONNECTORS = "/v1/connectors"

# Usage.
USAGE_LIMITS_REFRESH = "/v1/hosts/{host_id}/usage/limits/refresh"
USAGE = "/v1/hosts/{host_id}/usage"

# Server health.
METRICS = "/v1/metrics"

# Hosts, agents, events and commands.
ME = "/v1/me"
HOSTS = "/v1/hosts"
HOST = "/v1/hosts/{host_id}"
HOST_SNAPSHOT = "/v1/hosts/{host_id}/snapshot"
HOST_AGENTS = "/v1/hosts/{host_id}/agents"
AGENT_READ = "/v1/hosts/{host_id}/agents/{agent_id}/read"
EVENTS = "/v1/events"
HOST_COMMANDS = "/v1/hosts/{host_id}/commands"
HOST_COMMAND = "/v1/hosts/{host_id}/commands/{command_id}"

# Terminals.
TERMINAL_ATTACHMENTS = "/v1/hosts/{host_id}/terminals/{terminal_id}/attachments"
TERMINAL_CONTROL = "/v1/hosts/{host_id}/terminals/{terminal_id}/control"
ATTACHMENT_LEASES = "/v1/terminal-attachments/{attachment_id}/leases"
ATTACHMENT = "/v1/terminal-attachments/{attachment_id}"
LEASE = "/v1/terminal-leases/{lease_id}"
LEASE_RENEW = "/v1/terminal-leases/{lease_id}/renew"
TERMINAL_TICKETS = "/v1/terminal-tickets"
TERMINAL_SOCKET = "/v1/terminal/{lease_id}"

# The web app's own routes.
AUTH_RECOVERY_SCRIPT = "/v1/auth/recovery.js"
AUTH_PAIRING_BROWSER = "/v1/auth/pairing/browser"
AUTH_SESSION_RECOVERY = "/v1/auth/session/recovery"
AUTH_SESSION_CHECK = "/v1/auth/session/check"
UI_VISIBILITY = "/v1/ui/visibility"
AUTH_PAIRING_FINISH = "/v1/auth/pairing/finish"
AUTH_PAIRING_FINISH_SCRIPT = "/v1/auth/pairing/finish.js"
AUTH_PAIRING_COMPLETE = "/v1/auth/pairing/complete"
AUTH_DEV_LOGIN = "/v1/auth/dev-login"
AUTH_CALLBACK = "/v1/auth/callback"
AUTH_LOGOUT = "/v1/auth/logout"

# Matches a picture upload path; the grant pattern is the part of a picture URL after it.
PICTURE_CONTENT_PATTERN = r"/v1/pictures/[a-f0-9]{32}/content"
PICTURE_CONTENT_RE = re.compile(PICTURE_CONTENT_PATTERN)


def _segment(value: str) -> str:
    return quote(value, safe="")


# Builders fill a template. Ids are quoted where the call sites always did, and left as given
# elsewhere (those ids come from the gateway itself).
def picture(identifier: str) -> str:
    return PICTURE.format(identifier=identifier)


def picture_content(identifier: str) -> str:
    return PICTURE_CONTENT.format(identifier=identifier)


def pairing_status(request_id: str) -> str:
    return PAIRING_STATUS.format(request_id=request_id)


def pairing_redeem(request_id: str) -> str:
    return PAIRING_REDEEM.format(request_id=request_id)


def device(device_id: str) -> str:
    return DEVICE.format(device_id=_segment(device_id))


def enrollment(enrollment_id: str) -> str:
    return ENROLLMENT.format(enrollment_id=_segment(enrollment_id))


def enrollment_redeem(enrollment_id: str) -> str:
    return ENROLLMENT_REDEEM.format(enrollment_id=enrollment_id)


def usage(host_id: str) -> str:
    return USAGE.format(host_id=_segment(host_id))


def usage_limits_refresh(host_id: str) -> str:
    return USAGE_LIMITS_REFRESH.format(host_id=_segment(host_id))


def host(host_id: str) -> str:
    return HOST.format(host_id=_segment(host_id))


def host_snapshot(host_id: str) -> str:
    return HOST_SNAPSHOT.format(host_id=host_id)


def host_agents(host_id: str) -> str:
    return HOST_AGENTS.format(host_id=host_id)


def agent_read(host_id: str, agent_id: str) -> str:
    return AGENT_READ.format(host_id=_segment(host_id), agent_id=_segment(agent_id))


def host_commands(host_id: str) -> str:
    return HOST_COMMANDS.format(host_id=host_id)


def host_command(host_id: str, command_id: str) -> str:
    return HOST_COMMAND.format(host_id=_segment(host_id), command_id=_segment(command_id))


def terminal_attachments(host_id: str, terminal_id: str) -> str:
    return TERMINAL_ATTACHMENTS.format(host_id=host_id, terminal_id=terminal_id)


def terminal_control(host_id: str, terminal_id: str) -> str:
    return TERMINAL_CONTROL.format(host_id=host_id, terminal_id=terminal_id)


def attachment_leases(attachment_id: str) -> str:
    return ATTACHMENT_LEASES.format(attachment_id=attachment_id)


def attachment(attachment_id: str) -> str:
    return ATTACHMENT.format(attachment_id=attachment_id)


def lease(lease_id: str) -> str:
    return LEASE.format(lease_id=lease_id)


def lease_renew(lease_id: str) -> str:
    return LEASE_RENEW.format(lease_id=lease_id)


def terminal_socket(lease_id: str) -> str:
    return TERMINAL_SOCKET.format(lease_id=_segment(lease_id))
