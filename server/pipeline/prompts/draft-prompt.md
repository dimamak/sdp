# Task

You are given a digest of one working day: coding sessions (Claude Code transcripts), chat messages, emails, meeting notes, screenshot descriptions, call debriefs. Some content may be in Hebrew or Russian — translate insights to {LANGUAGE_OUT}.

1. Scan the digest and list candidate stories. Archetypes worth hunting for:
   - **Teardown**: a specific problem solved today, with the real steps and numbers.
   - **Failure → lesson**: what broke, what it cost, what changed. (These build the most trust.)
   - **Contrarian take**: today's experience contradicting common wisdom in the field.
   - **Field story**: a real customer/user/partner pattern (anonymized) that teaches.
   - **Behind-the-scenes**: a concrete moment that shows how the work actually gets done.
   - **Timely commentary**: today's work intersecting with current industry news.

2. Score each candidate. It must pass ALL three gates:
   - **In-lane**: fits one of the topic lanes in the style guide.
   - **Tension or insight**: problem → attempt → outcome, or a genuinely crisp realization.
   - **Only-they-could-write-it**: contains at least one first-hand specific — a number,
     a name (of a tool/tech, not a person), a moment, a measurable result.
   Reject anything generic. A day of routine work with no story is a valid outcome.

3. Pick the SINGLE best story. Privately draft 3 different hooks for it (different
   angles: the number, the moment, the surprise); keep the strongest, discard the rest.
   The hook must work within the first 210 characters and must not spoil the payoff.

4. Write the post in {LANGUAGE_OUT}, obeying every rule in the style guide.
   Open with the moment, not the lesson. Before finalizing, run the style guide's
   AI-tells find-and-delete pass over your own text.

5. Also produce up to 2 shorter alternates from different stories (or a different
   angle on the main story) if the digest supports them.

# Output

Return ONLY a JSON object, no other text:

{
  "story_rationale": "one sentence: which story you picked and why it passed the gates",
  "post_text": "the full post, ready to publish",
  "alternates": ["alternate post 1", "alternate post 2"]
}

If no candidate passes all three gates, return:
{"story_rationale": "nothing post-worthy", "post_text": "", "alternates": []}
