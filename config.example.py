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

# Slack configuration
SLACK_TOKEN_FILE = "~/.claude/skills/slack-tools/config/tokens.json"
SLACK_CHANNELS_FILE = "~/.claude/skills/slack-tools/config/channels.json"
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
