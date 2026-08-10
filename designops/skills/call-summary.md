# Call summary — client email draft (skill)

On-demand pipeline: extract facts from a design client call transcript, then compose a
house-format confirmation email. **Never send** — store draft for human review.

## Hard rules (code-enforced)

- Never invent decisions; evidence quotes must fuzzy-match the transcript.
- Never state price / effort / scope expansion unless present in the source.
- Placeholders in body only: `[Figma link]`, `[link]`, `[date]`, `[name]`.
- Degraded transcripts → skeleton / coverage phrasing + reviewer_notes warning.

## House format

1. **Hello {Name},**
2. Thank you for the call earlier today / yesterday
3. As promised we are sharing … for you to review (artifacts / links)
4. **Major alignment only (optional)** — big decisions that impact many templates or the
   project (e.g. visual direction). Do **not** recap small changes
5. As next steps from your side, please: (bullets)
6. From our side we will proceed with: (bullets)
7. For the next review session we suggest …
8. Sign-off with owner name

No item in more than one section. Prefer omit empty sections.
