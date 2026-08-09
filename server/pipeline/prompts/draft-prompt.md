# Task

You are given a digest of one working day: coding sessions (Claude Code transcripts), chat messages, emails, meeting notes, screenshots descriptions, call debriefs. Some content may be in Hebrew or Russian — translate insights to {LANGUAGE_OUT}.

1. Scan the digest and identify candidate stories: something built, debugged, decided, learned, or a surprising interaction. Prefer specific, technical, first-hand moments over generic busyness. A good story has tension (problem → attempt → outcome) or a crisp insight.
2. Pick the SINGLE best story for a LinkedIn post by this person, following the style guide above.
3. Write the post in {LANGUAGE_OUT}, obeying every rule in the style guide.
4. Also produce up to 2 shorter alternates from different stories (or angles) if the digest supports them.

# Output

Return ONLY a JSON object, no other text:

{
  "story_rationale": "one sentence: which story you picked and why",
  "post_text": "the full post, ready to publish",
  "alternates": ["alternate post 1", "alternate post 2"]
}

If the digest contains nothing post-worthy (e.g. only routine noise), return:
{"story_rationale": "nothing post-worthy", "post_text": "", "alternates": []}
