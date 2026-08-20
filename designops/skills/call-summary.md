# Call summary — client follow-up email draft (v3)

On-demand pipeline: extract facts → critic audit → compose house-format follow-up.
**Never send** — store draft for human review.

Prompts live in code defaults (`call_summary_prompts.py`) and can be overridden in
admin at `/call-summary?tab=prompts` without a deploy.

## Hard rules (code-enforced)

- Never invent decisions; evidence quotes must fuzzy-match the transcript (stitched
  multi-turn evidence joined with ` / ` is allowed).
- Never state price / effort / scope expansion unless present in the source.
- Placeholders in body only: `[Figma link]`, `[link]`, `[date]`, `[name]`,
  `[option 1]`, `[option 2]`.
- Thanks line is computed in code (`THANKS_LINE`) — never guessed by the model.
- Greeting names from transcript attendee names only — never email local-parts.
- Sign-off is `SENDER_FIRST_NAME` (first token only).
- Degraded transcripts → skeleton / coverage phrasing + reviewer_notes warning.
- Urgent flags must not appear in the body; surface `separate_email_recommended`.

## House format (v3)

1. **Hello {CLIENT_FIRST_NAMES},**
2. `{THANKS_LINE}` (deterministic)
3. Sharing — only artifacts `shared_already` / `will_share_after_changes`
   (`referenced_only` never appears); "As promised" at most once
4. As next steps from your side, please: (client_actions owner=client + open
   questions owed by client, including missing_parameter)
5. From our side we will proceed with: (our_commitments as gerund/noun phrases)
6. Alignment recap — project / multi_template / reversals / rejections; max 6
7. Closer (next session, slot options, or timing fallback) — never end on bullets
8. Best regards / {SENDER_FIRST_NAME}

No item in more than one section. Prefer omit empty sections.
Subject: `{PROJECT_NAME} - follow-up from our call`.
