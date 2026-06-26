# TODO: Merge Slack thread chunks (symptom + fix fragmentation fix)

**Date logged:** 2026-04-22
**Status:** Not started
**Priority:** Medium — real retrieval-quality bug, not blocking

---

## Problem

Slack threads are indexed as fragmented chunks: one `thread_parent` chunk for the opener, one `thread_reply` chunk per reply. Opener and replies share `thread_ts` metadata but exist as independent retrieval units.

**Observed failure:** Query `"greenhouse no companies"` finds lpacheco's question (docker-support, 2026-01-14) at rank 8, distance 0.80. rshakespear's reply with the actual fix (`testing@bamboohr.com`/`Admin the all powerful` user setup) is in a *different* chunk and does not appear in the top 10 at all.

Diagnosis: the symptom chunk and the fix chunk share almost zero vocabulary. No retrieval algorithm, hybrid or otherwise, can match text that isn't in the candidate chunk. Hybrid BM25 + dense + RRF is already implemented correctly; it is retrieving the right chunks for the query, they just don't contain the answer.

## Why retrieval-algorithm fixes don't solve this

- Thread-aware post-retrieval (pull siblings by `thread_ts` after a chunk is retrieved) is a band-aid. If the symptom chunk is rank 8, users never click it; sibling pull doesn't help.
- Query expansion via LLM prepass adds vocabulary but doesn't bridge genuinely different chunks.
- Changing BM25 weights, reranker thresholds, etc. can't materialize words that aren't in the text.

The chunks themselves are wrong. That's the only place a fix lands.

## The fix: one chunk per thread

Concatenate opener + all replies into a single chunk at indexing time. Chunk now contains both symptom vocabulary (from opener) and fix vocabulary (from replies). Any query matching any part retrieves the whole thread.

**Content shape:**

```
# Thread started by @lpacheco in #docker-support
**Date:** 2026-01-14 12:38:24
**Replies:** 3

[opener text]

---

## Reply from @rshakespear
**Date:** 2026-01-14 12:42:00

[reply text]

---

## Reply from @rshakespear
**Date:** 2026-01-14 12:43:44

[reply text]

---

## Reply from @lpacheco
**Date:** 2026-01-14 13:04:00

[reply text]
```

**Metadata:** drop per-reply fields (`author_username` etc. from the reply scope), add thread-level `reply_authors: sorted list of usernames`. Keep `thread_ts`, `reply_count`, `channel_name`, opener's `author_username` as the thread author, `chunk_type: "thread"`.

## Code change

**File:** `scripts/slack_collector.py` lines 492-569 (`collect_slack_messages`, inside `if is_thread:` branch).

Replace the block that emits a parent chunk followed by a loop of reply chunks with a single block that builds `combined_content` from the opener + all replies and emits one chunk.

Sketch:

```python
if is_thread:
    replies = fetch_thread_replies(channel_id, msg_ts, tokens)
    resolved_msg_text = resolve_mentions_in_text(msg_text, tokens, user_cache)
    formatted_ts = format_timestamp(msg_ts)

    content_parts = [
        f"# Thread started by @{user_info['username']} in #{channel_name}\n"
        f"**Date:** {formatted_ts}\n"
        f"**Replies:** {reply_count}\n\n"
        f"{resolved_msg_text}"
    ]

    reply_authors = set()
    for reply in replies:
        reply_user = reply.get("user", "")
        reply_text = reply.get("text", "")
        reply_ts = reply.get("ts", "")
        reply_user_info = fetch_user_info(reply_user, tokens, user_cache)
        reply_formatted_ts = format_timestamp(reply_ts)
        resolved_reply_text = resolve_mentions_in_text(reply_text, tokens, user_cache)
        reply_authors.add(reply_user_info["username"])

        content_parts.append(
            f"\n---\n\n"
            f"## Reply from @{reply_user_info['username']}\n"
            f"**Date:** {reply_formatted_ts}\n\n"
            f"{resolved_reply_text}"
        )

    combined_content = "\n".join(content_parts)

    metadata = {
        "channel_name": channel_name,
        "channel_id": channel_id,
        "message_ts": msg_ts,
        "thread_ts": msg_ts,
        "author_username": user_info["username"],
        "author_display": user_info["display_name"],
        "reply_authors": sorted(reply_authors),
        "date": formatted_ts.split(" ")[0],
        "timestamp": formatted_ts,
        "reply_count": reply_count,
        "chunk_type": "thread",
        "filename": f"{channel_name}_{msg_ts}.slack",
        "filepath": f"slack://{channel_name}/{msg_ts}",
    }

    chunks.append({"content": combined_content, "metadata": metadata})
    processed_thread_parents.add(msg_ts)
```

About 30 lines added, 40 removed. Non-thread standalone message logic (lines 571-599) stays unchanged.

## Risks and mitigations

1. **Long threads dilute embeddings.**
   - Threads with 50+ replies exist in `#development`. Combined content > ~8KB starts hurting embedding quality.
   - Mitigation: cap combined content at N chars (say 6000). If exceeded, split across multiple chunks, BUT prefix every chunk with the opener text + "thread continues" marker so each chunk retains the question anchor.
   - Short support threads (the actual pain point) never hit this cap.

2. **Loss of per-reply retrievability.**
   - Today you could retrieve a single reply pinpointed. Merging gives up that granularity.
   - For Q&A channels (`#docker-support`, `#dev-help`), context is what helps — net win.
   - For longer-form channels (`#development`, `#random`), less obvious. Consider per-channel config: apply thread-merging only to selected channels.

3. **Metadata shape change breaks downstream filters.**
   - Existing callers using `chunk_type: "thread_reply"` filter will break.
   - Must audit before merging. See next section.

4. **Reindex required.**
   - ~5 seconds per `HOW_TO_USE.md`. Trivial.
   - Collection is blown away during reindex; if it fails midway, RAG is down. Run against a staging collection name first, swap atomically. Or just accept the ~5s downtime.

5. **Similar fragmentation likely exists in session JSONL chunks.**
   - Out of scope here. Note it and potentially tackle as a follow-up: long sessions have symptom in one exchange and resolution in another.

## Pre-work: audit callers of thread_reply / thread_parent filters

Before writing code, grep for consumers of these chunk_type values:

```
rg -n "thread_reply|thread_parent" ~/.claude/rag-system
```

If only `search.py` and MCP server reference them, change is clean. If other scripts filter on them, either update those callers or keep backward compat by emitting the merged chunk *in addition to* the individual chunks (doubles index size; avoid unless necessary).

## Test plan

1. Pick the lpacheco thread (`docker-support_1768412624.345509`) as canary.
2. Before: `"greenhouse no companies"` → lpacheco at rank 8, rshakespear fix-reply absent.
3. Apply change, reindex.
4. After: `"greenhouse no companies"` should return the merged thread at rank ≤ 3, containing both opener and rshakespear's fix in the content.
5. Also verify `"Admin the all powerful testing@bamboohr.com"` still returns the same thread (should, since fix text is still in the chunk).
6. Run a sanity query like `"greenhouse 502"` to confirm unrelated threads aren't pulled in spuriously.

## Success criteria

- `"greenhouse no companies"` returns a chunk containing both symptom and fix within the top 3 hits.
- No regressions on a small set of canary queries (pick 5-10 known-good queries, measure rank of expected hit before/after).
- Index size change < 30% (merged chunks are fewer but larger; should roughly balance).

## Out of scope for this TODO

- Session JSONL chunk merging (same pattern, separate work).
- Query-expansion preprocessing (orthogonal; would help even after this fix).
- Reranking layer (e.g. cross-encoder on top-N hits). Possibly useful later but not the current bottleneck.
