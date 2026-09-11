"""
Meeting transcript chunker for RAG.

Turns a Gemini meeting document into searchable chunks. A Gemini Doc has three
tabs; two carry content we want:

  "Full notes"  -> ### Summary / ### Next steps / ### Details
  "Transcript"  -> timestamped speaker turns

Those map onto four chunk types:

  meeting_summary        the Summary block (one chunk)
  meeting_action_item    one chunk per Next-steps item
  meeting_topic          one chunk per Details bullet, keeping its (HH:MM:SS) anchor
  transcript_turn_group  consecutive speaker turns, grouped to a target size

Raw transcript is too noisy to search on its own -- an hour of conversation puts
small talk and decisions at the same semantic distance from a query. The summary
and topic chunks are the dense retrieval layer; turn groups are for verification
and verbatim quoting.

Speaker attribution is NOT always trustworthy. When several people share a
conference room, Google Meet labels all of them with the room name, so every
statement and action item is credited to the room. Each chunk therefore carries
speaker_attribution:

  labeled   Meet named the speakers; trust it
  inferred  a split pass guessed the speakers from turn shape; treat as proposed
  room      nobody split it; the "speaker" is a conference room

Callers must surface `inferred` and `room` rather than presenting either as fact.
"""

import re
from typing import Dict, List, Optional

# **Name:** at line start. Names may be multi-word, hyphenated, or possessive,
# which is why a single-word pattern silently matches nothing on real files.
TURN_RE = re.compile(r"^\*\*([A-Z][A-Za-z.'\- ]{0,40}?):\*\*\s*(.*)$")

# Timestamps appear as a heading (### 00:01:43) or in bold (**00:01:43**).
TS_HEAD_RE = re.compile(r"^#+\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*$")
TS_BOLD_RE = re.compile(r"^\*\*(\d{1,2}:\d{2}(?::\d{2})?)\*\*\s*$")

# A trailing (00:03:20) anchor on a Details bullet.
ANCHOR_RE = re.compile(r"\((\d{1,2}:\d{2}(?::\d{2})?)\)[.\s]*$")

# Meet names a room like "S 116 - Hidden Canyon" or "Conf Rm 4".
ROOM_RE = re.compile(r"^(S\s*\d+\b|.*\b(conference|conf|room|rm)\b)", re.IGNORECASE)

ROOM_LABEL_RE = re.compile(r"\bS\s*\d+\s*[-\u2013]\s*[A-Z]\w+")

INFERRED_MARKERS = ("proposed, not authoritative", "attribution below is")

TARGET_CHARS = 1500
MAX_CHARS = 2600


def _detect_attribution(text: str, speakers: List[str]) -> str:
    """Classify how much to trust the speaker labels."""
    lowered = text[:4000].lower()
    if any(marker in lowered for marker in INFERRED_MARKERS):
        return "inferred"
    distinct = {s for s in speakers if s}
    if distinct and all(ROOM_RE.match(s) for s in distinct):
        return "room"
    # In a notes tab the room label is the subject of the prose, not a speaker
    # field, so a body scan is the only way to catch it.
    if ROOM_LABEL_RE.search(text):
        return "room"
    # One label across a long conversation means the room was the speaker.
    if len(distinct) == 1 and len(speakers) > 8:
        return "room"
    return "labeled"


def _chunk_attribution(text: str, doc_level: str, subject: str = "") -> str:
    """
    Classify one chunk's attribution.

    `subject` is the chunk's own speaker or action-item owner when known; it is
    the strongest signal, because Meet either named a person or named a room.
    Failing that, a room label appearing in the chunk's own prose means this
    chunk is about the room. Only when neither applies does the document-level
    verdict (which carries the `inferred` marker) apply.
    """
    if subject:
        return (
            "room"
            if (ROOM_RE.match(subject) or ROOM_LABEL_RE.search(subject))
            else "labeled"
        )
    if ROOM_LABEL_RE.search(text):
        return "room"
    return doc_level


def _timestamp_of(line: str) -> Optional[str]:
    for pattern in (TS_HEAD_RE, TS_BOLD_RE):
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def parse_turns(transcript: str) -> List[Dict]:
    """Split a transcript into {speaker, text, ts} turns, in order."""
    turns: List[Dict] = []
    current_ts = ""
    for raw_line in transcript.splitlines():
        line = raw_line.rstrip()
        stamp = _timestamp_of(line)
        if stamp:
            current_ts = stamp
            continue
        match = TURN_RE.match(line.strip())
        if match:
            turns.append(
                {
                    "speaker": match.group(1).strip(),
                    "text": match.group(2).strip(),
                    "ts": current_ts,
                }
            )
        elif turns and line.strip():
            # Continuation of the previous turn's paragraph.
            turns[-1]["text"] = (turns[-1]["text"] + " " + line.strip()).strip()
    return turns


def chunk_transcript(transcript: str, base: Dict) -> List[Dict]:
    """Group consecutive turns into chunks, preferring timestamp boundaries."""
    turns = parse_turns(transcript)
    if not turns:
        return []

    # Document level carries only inferred-vs-labeled. A whole-transcript
    # room scan here would taint every group in a mixed meeting, where some
    # attendees share a room and others are in their own windows.
    doc_level = (
        "inferred" if _detect_attribution(transcript, []) == "inferred" else "labeled"
    )
    chunks: List[Dict] = []
    buffer: List[Dict] = []

    def flush() -> None:
        if not buffer:
            return
        body = "\n".join(f"{t['speaker']}: {t['text']}" for t in buffer if t["text"])
        if not body.strip():
            buffer.clear()
            return
        speakers = sorted({t["speaker"] for t in buffer})
        # `any`, not `all`: a group mixing a named person with the room still
        # contains statements that cannot be attributed. The Speakers line shows
        # the reader which is which.
        group_attribution = (
            "room"
            if any(ROOM_RE.match(s) or ROOM_LABEL_RE.search(s) for s in speakers)
            else doc_level
        )
        ts_start = next((t["ts"] for t in buffer if t["ts"]), "")
        header = f"# {base.get('title', 'Meeting')} — {base.get('meeting_date', '')}"
        if ts_start:
            header += f" @ {ts_start}"
        chunks.append(
            {
                "content": f"{header}\n\n{body}",
                "metadata": {
                    **base,
                    "chunk_type": "transcript_turn_group",
                    "ts_start": ts_start,
                    "speakers": ", ".join(speakers),
                    "speaker_attribution": group_attribution,
                    "turn_count": len(buffer),
                },
            }
        )
        buffer.clear()

    size = 0
    for turn in turns:
        turn_len = len(turn["speaker"]) + len(turn["text"]) + 2
        starts_new_ts = bool(turn["ts"]) and buffer and turn["ts"] != buffer[-1]["ts"]
        if buffer and (
            size + turn_len > MAX_CHARS or (starts_new_ts and size >= TARGET_CHARS)
        ):
            flush()
            size = 0
        buffer.append(turn)
        size += turn_len
    flush()
    return chunks


def _section(notes: str, heading: str) -> str:
    """Return the body under a '### <heading>' section, up to the next heading."""
    pattern = re.compile(
        rf"^#{{1,4}}\s*{re.escape(heading)}\s*$(.*?)(?=^#{{1,4}}\s|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(notes)
    return match.group(1).strip() if match else ""


def _list_items(block: str) -> List[str]:
    """Pull '-' / '*' / '- [ ]' list items out of a block, joining wrapped lines."""
    items: List[str] = []
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if re.match(r"^\s*[-*+]\s+", line):
            items.append(re.sub(r"^\s*[-*+]\s+(\[[ xX]\]\s*)?", "", line).strip())
        elif items and line.strip():
            items[-1] = (items[-1] + " " + line.strip()).strip()
    return [i for i in items if i]


def chunk_full_notes(notes: str, base: Dict) -> List[Dict]:
    """Chunk the Summary / Next steps / Details sections of a Gemini notes tab."""
    chunks: List[Dict] = []
    owners = [
        match.group(1).strip()
        for match in (
            re.match(r"^\[([^\]]+)\]", item)
            for item in _list_items(_section(notes, "Next steps"))
        )
        if match
    ]
    attribution = (
        "inferred" if _detect_attribution(notes, []) == "inferred" else "labeled"
    )
    title = base.get("title", "Meeting")
    date = base.get("meeting_date", "")

    summary = _section(notes, "Summary")
    if summary:
        # Gemini runs bold sub-headers inline with no separators; break them out
        # so the highest-value chunk is actually readable.
        summary = re.sub(r"\*\*(.+?)\*\*", r"\n\n**\1**\n", summary).strip()
        summary = re.sub(r"\n{3,}", "\n\n", summary)
        chunks.append(
            {
                "content": f"# {title} — {date}\n\nMeeting summary:\n\n{summary}",
                "metadata": {
                    **base,
                    "chunk_type": "meeting_summary",
                    # A summary covering a mixed meeting inherits the room label only
                    # if the room is what the summary talks about.
                    "speaker_attribution": _chunk_attribution(summary, attribution),
                },
            }
        )

    for index, item in enumerate(_list_items(_section(notes, "Next steps"))):
        owner_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", item)
        owner = owner_match.group(1).strip() if owner_match else ""
        body = owner_match.group(2).strip() if owner_match else item
        chunks.append(
            {
                "content": f"# {title} — {date}\n\nAction item"
                + (f" ({owner})" if owner else "")
                + f": {body}",
                "metadata": {
                    **base,
                    "chunk_type": "meeting_action_item",
                    "owner": owner,
                    "item_index": index,
                    "speaker_attribution": _chunk_attribution(body, attribution, owner),
                },
            }
        )

    for index, item in enumerate(_list_items(_section(notes, "Details"))):
        anchor = ANCHOR_RE.search(item)
        ts = anchor.group(1) if anchor else ""
        chunks.append(
            {
                "content": f"# {title} — {date}"
                + (f" @ {ts}" if ts else "")
                + f"\n\n{item}",
                "metadata": {
                    **base,
                    "chunk_type": "meeting_topic",
                    "ts_start": ts,
                    "topic_index": index,
                    "speaker_attribution": _chunk_attribution(item, attribution),
                },
            }
        )

    return chunks


def chunk_meeting(
    base: Dict,
    full_notes: str = "",
    transcript: str = "",
) -> List[Dict]:
    """Chunk one meeting from its notes and/or transcript text."""
    chunks: List[Dict] = []
    if full_notes.strip():
        chunks.extend(chunk_full_notes(full_notes, base))
    if transcript.strip():
        chunks.extend(chunk_transcript(transcript, base))
    return chunks
