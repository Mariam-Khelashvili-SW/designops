# Call summary — client email draft (skill)

On-demand pipeline: extract facts from a design client call transcript, then compose a
house-format confirmation email. **Never send** — store draft for human review.

## Hard rules (code-enforced)

- Never invent decisions; evidence quotes must fuzzy-match the transcript.
- Never state price / effort / scope expansion unless present in the source.
- Placeholders in body only: `[Figma link]`, `[link]`, `[date]`, `[name]`.
- Degraded transcripts → skeleton / coverage phrasing + reviewer_notes warning.

## House format

1. Thanks + "quick summary of what we covered"
2. **What we aligned on** — exact agreed change points (+ next call if agreed)
3. **Pending items from scandiweb**
4. **Pending items from your side**
5. **Next steps** only if not already covered
6. Sign-off with owner name

No item in more than one section.
