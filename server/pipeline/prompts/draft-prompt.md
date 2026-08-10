# Task

You are given a digest of one working day: coding sessions (Claude Code transcripts), chat messages, emails, meeting notes, screenshot descriptions, call debriefs. Some content may be in Hebrew or Russian — translate insights to {LANGUAGE_OUT}.

Your job is to find the day's most **interesting fact**, not to summarize the day's work.

1. Scan the digest and list candidate FACTS — things that turned out to be true, not tasks that were completed. Hunt specifically for:
   - **A number that surprises**: a cost, ratio, or measurement nobody would have guessed.
   - **A wrong assumption**: something confidently believed that the day disproved.
   - **A hidden cause**: the real reason behind a problem, where the obvious reason was wrong.
   - **A counterintuitive tradeoff**: the cheap option that cost more, the fast path that was slower.
   - **A pattern with reach**: something true beyond this stack, product, or company.
   - **A human moment**: a decision, disagreement, or judgment call worth reflecting on.

   Write each candidate as a single declarative sentence stating the fact itself.
   If a candidate can only be written as "I fixed/built/shipped X", discard it — that's a task, not a fact.

2. Score each candidate against three gates. It must pass ALL of them:
   - **In-lane**: fits one of the topic lanes in the style guide.
   - **Interesting to a smart outsider**: someone who doesn't know this stack — another
     founder, an operator, an investor — would find it worth knowing. Would they repeat
     it to someone else? If it's only interesting to someone debugging this exact system, reject it.
   - **First-hand**: contains a specific number, moment, or result that only this person could report.

   A day of routine work with no surprising fact is a valid outcome — say so rather than forcing a post.

3. Pick the SINGLE best fact. Privately draft 3 different hooks that each state the fact
   a different way (the number / the wrong assumption / the stake). **No hook may contain a
   tool name, a version, or any setup clause.** Keep the strongest, discard the rest.

4. Write the post in {LANGUAGE_OUT}, obeying every rule in the style guide:
   - Lead with the hook. Then the finding, then why it matters.
   - Mechanism gets 1–2 sentences maximum, only if the fact is meaningless without it.
   - Cut the investigation narrative completely — no "I checked X, then Y".
   - Decide whether this post should end with a genuine invitation to the reader
     (how they'd approach it, what they've seen). Do that for roughly one post in three,
     and only when the question is real.
   - Target 600–1,200 characters.

5. Self-check and fix:
   - Does line 1 work as the entire post if nobody clicks "see more"?
   - Could a smart non-specialist explain the point back to me after 15 seconds?
   - Did I delete every sentence that only proves work happened?

6. Also produce up to 2 shorter alternates from different facts if the digest supports them.

7. **FINAL PASS — do this last, on the finished post and every alternate.**
   Rewrite the text; do not merely inspect it.

   a. **Strip every name.** Go word by word looking for products, vendors,
      platforms, frameworks, libraries, models, marketplaces, and data sources.
      Replace each with the generic category from the style guide. The hook and
      the alternates included. Only names on the style guide's allowed list survive.
      After this, re-read: does the fact still land without the brand? If it now
      reads vague, sharpen it with a number or a consequence, not a name.

   b. **De-AI it.** Hunt these and rewrite the whole sentence in plain spoken
      English — deleting the phrase alone leaves the machine rhythm behind:
      - "It's not X, it's Y" and every sibling contrast construction
      - more than one em dash in the post (rewrite as two sentences or a comma)
      - "Here's how/what/why", tidy rule-of-three lists, symmetrical clause pairs
      - "thrilled", "humbled", "unlock", "elevate", "delve", "game-changer"
      - every sentence that could have been written about any company by anyone
      - uniform sentence length; real speech varies short and long

   c. **Read it aloud in your head.** Any clause you would not say to a colleague
      over coffee gets rewritten or cut. Then confirm the post is still within
      600–1,200 characters, and trim if the rewrite pushed it over.

# Output

Return ONLY a JSON object, no other text:

{
  "story_rationale": "the fact you picked, in one sentence, and why it passes the gates",
  "post_text": "the full post, ready to publish",
  "alternates": ["alternate post 1", "alternate post 2"]
}

If no candidate passes all three gates, return:
{"story_rationale": "nothing post-worthy", "post_text": "", "alternates": []}
