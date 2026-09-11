"""
RAG System Configuration

Define which directories to index for each source type.
Copy this file to config.py and customize for your environment.
"""

import os
from pathlib import Path

# Load Jira credentials from MCP atlassian env file if it exists
_jira_env_file = Path.home() / ".mcp/server-data/env/mcp-atlassian.env"
if _jira_env_file.exists():
    with open(_jira_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value

# Sanctum: team knowledge, architecture, processes
SANCTUM_PATHS = [
    "~/.claude/sanctum",
    "~/.claude/docs",
]

# PHP Code: specify directories to index recursively
PHP_CODE_PATHS = [
    # "~/repos/my-php-project/app",
    # "~/repos/my-php-project/tests",
]

# JS/TS Code: specify directories to index recursively
JS_TS_CODE_PATHS = [
    # "~/repos/my-frontend",
]

# Python Code: specify directories to index recursively
PYTHON_CODE_PATHS = [
    # "~/repos/my-python-project",
]

# Puppet Code: custom modules and Hiera data
PUPPET_CODE_PATHS = [
    # "~/repos/puppet/site",
    # "~/repos/puppet/hieradata",
]

# Archive: old session logs, working memory archives
ARCHIVE_PATHS = [
    "~/.claude/archive/working-memory.md",
]

# Process docs: workflow documentation
PROCESS_PATHS = [
    # "~/.claude/processes",
]

# Agent research: research outputs
AGENT_RESEARCH_PATHS = [
    # "~/.claude/agent-research",
]

# JSONL sessions: Claude Code conversation archives
JSONL_SESSION_PATHS = [
    "~/.claude/session-archive",
]

# Jira configuration
JIRA_URL = os.getenv("JIRA_URL", "https://your-org.atlassian.net")
JIRA_EMAIL = os.getenv("JIRA_EMAIL") or os.getenv("JIRA_USERNAME")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

JIRA_PROJECTS = [
    {"project": "PROJ", "max_issues": 300},
]

# Slack configuration.
# Channel roster is committed at config/channels.json. tokens.json is only used
# by the dead xoxc/xoxd "stealth" backend; the default MCP connector path never
# reads it, so this path need not exist.
SLACK_TOKEN_FILE = str(Path(__file__).parent / "config" / "tokens.json")
SLACK_CHANNELS_FILE = str(Path(__file__).parent / "config" / "channels.json")
SLACK_MAX_AGE_DAYS = 30
SLACK_MAX_MESSAGES_PER_CHANNEL = 2000
# SLACK_CHANNELS: per-channel ingestion config.
# Keys are channel names (without the leading #). Each value is a dict of overrides
# for that channel; an empty dict means "use the global defaults".
# Supported override keys:
#   max_age_days: int, overrides SLACK_MAX_AGE_DAYS for this channel.
SLACK_CHANNELS = {
    # "team-channel": {},
    # "dev-channel": {},
    # "deep-history-channel": {"max_age_days": 180},
}

# Slite — team knowledge base (REST API)
# Get your API key from https://slite.com/api
# Root note IDs are the doc IDs from URLs like https://bamboohr.slite.com/app/docs/<ID>
SLITE_API_KEY = os.getenv("SLITE_API_KEY")
SLITE_ROOT_NOTE_IDS = [
    # "abc123xyz",  # e.g. "My Team" folder root note ID
]

# --- Meeting docs (Google Drive) -------------------------------------------
# Gemini writes a Google Doc for every Google Meet. This source indexes the
# "Full notes" tab (Summary / Next steps / Details) and the "Transcript" tab of
# every Doc whose title contains MEETING_DOC_QUERY -- both Docs you own and
# Docs shared with you.
#
# Auth: scripts/drive_collector.py reuses the OAuth client and refresh token
# already present for the google-docs MCP server:
#
#     ~/.claude.json                            client id + secret
#     ~/.config/google-docs-mcp/token.json      refresh token
#
# Override with GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in the environment.
# Sharing that credential means a revoke or re-consent on the MCP server also
# breaks this collector, and this read-only job inherits whatever write scopes
# the client was granted. A dedicated client scoped to drive.readonly +
# documents.readonly is better hygiene; point the collector at its token file.
#
# Attribution caveat: when attendees share a conference room, Google Meet
# labels all of them with the room name, so Gemini credits statements and
# action items to the room rather than a person. Every chunk carries
# speaker_attribution of "labeled", "inferred", or "room" -- surface that
# rather than presenting a room-attributed quote as somebody's words.
MEETING_DOCS_ENABLED = True
MEETING_DOC_QUERY = "Notes by Gemini"
MEETING_MAX_DOCS = 500
