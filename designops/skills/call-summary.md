# Call summary — client follow-up email draft (v2)

On-demand pipeline: extract facts → critic audit → compose house-format follow-up.
**Never send** — store draft for human review.

## Hard rules (code-enforced)

- Never invent decisions; evidence quotes must fuzzy-match the transcript (stitched
  multi-turn evidence joined with ` / ` is allowed).
- Never state price / effort / scope expansion unless present in the source.
- Placeholders in body only: `[Figma link]`, `[link]`, `[date]`, `[name]`,
  `[option 1]`, `[option 2]`.
- Thanks line is computed in code (`THANKS_LINE`) — never guessed by the model.
- Degraded transcripts → skeleton / coverage phrasing + reviewer_notes warning.
- Urgent flags must not appear in the body; surface `separate_email_recommended`.

## House format (v2)

1. **Hello {CLIENT_FIRST_NAMES},**
2. `{THANKS_LINE}` (deterministic)
3. Sharing — driven by artifact `state` (shared already vs will share after changes;
   never both for the same artifact)
4. As next steps from your side, please: (client_actions + open questions owed by client)
5. From our side we will proceed with: (our_commitments + timing_verbatim)
6. Alignment recap — only `project` / `multi_template` / reversals; max 5 bullets
7. For the next review session we suggest …
8. Best regards / owner name

No item in more than one section. Prefer omit empty sections.
Subject: `{PROJECT_NAME} - follow-up from our call`.
