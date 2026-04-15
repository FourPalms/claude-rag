"""
Jira Issue Collector for RAG - Fetch issues via REST API.

Fetches Jira tickets using JQL queries and returns them as searchable chunks.
"""

import os
import requests
from typing import List, Dict
from pathlib import Path


def collect_jira_issues(
    jira_url: str, email: str, api_token: str, project: str, max_issues: int = 100
) -> List[Dict]:
    """
    Fetch Jira issues for a project using REST API.

    Args:
        jira_url: Base Jira URL (e.g., "https://your-org.atlassian.net")
        email: Jira account email
        api_token: Jira API token
        project: Project key (e.g., "SKY", "PATH")
        max_issues: Maximum number of issues to fetch (default 100)

    Returns:
        List of chunks, each with:
        - content: Formatted issue text (summary + description + key details)
        - metadata: issue_key, summary, status, assignee, created, updated, project
    """
    chunks = []

    # Build JQL query
    jql = f"project = {project} ORDER BY key DESC"

    # Jira REST API endpoint
    api_url = f"{jira_url}/rest/api/3/search/jql"

    # Authentication
    auth = (email, api_token)

    # Request parameters
    params = {
        "jql": jql,
        "maxResults": min(max_issues, 100),  # Jira limits to 100 per request
        "fields": "summary,description,status,assignee,created,updated,comment",
        "startAt": 0,
    }

    try:
        print(f"  Fetching {max_issues} issues from {project}...")

        total_fetched = 0

        while total_fetched < max_issues:
            # Make API request
            response = requests.get(api_url, auth=auth, params=params)
            response.raise_for_status()

            data = response.json()
            issues = data.get("issues", [])

            if not issues:
                break

            # Process each issue
            for issue in issues:
                if total_fetched >= max_issues:
                    break

                issue_key = issue["key"]
                fields = issue["fields"]

                # Extract fields
                summary = fields.get("summary", "")
                description = fields.get("description", "") or ""
                status = fields.get("status", {}).get("name", "Unknown")
                assignee_data = fields.get("assignee")
                assignee = (
                    assignee_data.get("displayName", "Unassigned")
                    if assignee_data
                    else "Unassigned"
                )
                created = fields.get("created", "")[:10]  # YYYY-MM-DD
                updated = fields.get("updated", "")[:10]  # YYYY-MM-DD

                # Format description (handle Atlassian Document Format)
                if isinstance(description, dict):
                    # ADF format - extract text content
                    description = _extract_text_from_adf(description)

                # Create title chunk (just metadata, no description or comments)
                title_content = f"""# {issue_key}: {summary}

**Status:** {status}
**Assignee:** {assignee}
**Created:** {created}
**Updated:** {updated}
"""

                title_metadata = {
                    "issue_key": issue_key,
                    "summary": summary,
                    "status": status,
                    "assignee": assignee,
                    "created": created,
                    "updated": updated,
                    "project": project,
                    "chunk_type": "title",
                    "filename": f"{issue_key}.jira",
                    "filepath": f"jira://{project}/{issue_key}",
                }

                chunks.append({"content": title_content, "metadata": title_metadata})

                # Create description chunk (if description exists)
                if description.strip():
                    description_content = f"""# {issue_key}: {summary} (Description)

{description}
"""

                    description_metadata = {
                        "issue_key": issue_key,
                        "summary": summary,
                        "status": status,
                        "assignee": assignee,
                        "created": created,
                        "updated": updated,
                        "project": project,
                        "chunk_type": "description",
                        "filename": f"{issue_key}_description.jira",
                        "filepath": f"jira://{project}/{issue_key}#description",
                    }

                    chunks.append(
                        {
                            "content": description_content,
                            "metadata": description_metadata,
                        }
                    )

                # Create separate chunk for each comment (first 5 comments)
                comments = fields.get("comment", {}).get("comments", [])
                for i, comment in enumerate(comments[:5]):
                    comment_id = comment.get("id", f"comment_{i}")
                    author = comment.get("author", {}).get("displayName", "Unknown")
                    created_date = comment.get("created", "")[:10]
                    body = comment.get("body", "")

                    if isinstance(body, dict):
                        body = _extract_text_from_adf(body)

                    # Create chunk for this comment
                    comment_content = f"""# {issue_key}: {summary} (Comment)

**Comment by:** {author}
**Date:** {created_date}

{body}
"""

                    comment_metadata = {
                        "issue_key": issue_key,
                        "summary": summary,
                        "status": status,
                        "assignee": assignee,
                        "created": created,
                        "updated": updated,
                        "project": project,
                        "chunk_type": "comment",  # Identifies this as a comment chunk
                        "comment_id": comment_id,
                        "comment_author": author,
                        "filename": f"{issue_key}_comment_{i+1}.jira",
                        "filepath": f"jira://{project}/{issue_key}#comment-{comment_id}",
                    }

                    chunks.append(
                        {"content": comment_content, "metadata": comment_metadata}
                    )

                total_fetched += 1

            # Check if there are more results
            if total_fetched >= max_issues or len(issues) < params["maxResults"]:
                break

            # Prepare for next page
            params["startAt"] += params["maxResults"]

        print(f"  ✓ Fetched {total_fetched} issues from {project}")

    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  Error fetching Jira issues: {e}")
        return []

    return chunks


def _extract_text_from_adf(adf: dict) -> str:
    """
    Extract plain text from Atlassian Document Format (ADF).

    Args:
        adf: ADF document structure (nested dict)

    Returns:
        Plain text content
    """
    if not isinstance(adf, dict):
        return str(adf)

    text_parts = []

    # ADF has 'content' array with nodes
    content = adf.get("content", [])

    for node in content:
        node_type = node.get("type", "")

        # Paragraphs
        if node_type == "paragraph":
            para_text = _extract_text_from_content(node.get("content", []))
            if para_text:
                text_parts.append(para_text)

        # Headings
        elif node_type.startswith("heading"):
            heading_text = _extract_text_from_content(node.get("content", []))
            if heading_text:
                level = node.get("attrs", {}).get("level", 1)
                text_parts.append("#" * level + " " + heading_text)

        # Lists
        elif node_type in ["bulletList", "orderedList"]:
            list_items = node.get("content", [])
            for item in list_items:
                item_text = _extract_text_from_adf(item)
                if item_text:
                    text_parts.append("- " + item_text)

        # Code blocks
        elif node_type == "codeBlock":
            code_text = _extract_text_from_content(node.get("content", []))
            if code_text:
                text_parts.append(f"```\n{code_text}\n```")

        # Recursively handle other content
        else:
            sub_content = node.get("content", [])
            if sub_content:
                sub_text = _extract_text_from_adf(node)
                if sub_text:
                    text_parts.append(sub_text)

    return "\n\n".join(text_parts)


def _extract_text_from_content(content: list) -> str:
    """
    Extract text from ADF content array.

    Args:
        content: List of content nodes

    Returns:
        Plain text
    """
    text_parts = []

    for node in content:
        if isinstance(node, dict):
            node_type = node.get("type", "")

            # Text nodes
            if node_type == "text":
                text_parts.append(node.get("text", ""))

            # Other nodes with nested content
            elif "content" in node:
                sub_text = _extract_text_from_content(node["content"])
                if sub_text:
                    text_parts.append(sub_text)

    return "".join(text_parts)
