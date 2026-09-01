# Task

You are given a digest of one working day: coding sessions (Claude Code transcripts), chat messages, emails, meeting notes, screenshot descriptions, call debriefs. Some content may be in Hebrew or Russian — translate insights to {LANGUAGE_OUT}.

Each coding-session entry is a summary of that session, not the transcript itself, and is followed by a "Full transcript: <path>" line. You have Read/Grep/Glob on that path — use it whenever a summary gestures at something promising (a number, a result) without giving the specific detail a post needs.

Your job is to find the day's most **interesting fact**, not to summarize the day's work.

1. Scan the digest and list candidate FACTS — things that turned out to be true, not tasks that were completed. Hunt specifically for:
   - **A number that surprises**: a cost, ratio, or measurement nobody would have guessed.
   - **A wrong assumption**: something confidently believed that the day disproved.
   - **A hidden cause**: the real reason behind an outcome, where the obvious explanation turns out to be wrong.
   - **A counterintuitive tradeoff**: the cheap option that cost more, the fast path that was slower.
   - **A pattern with reach**: something true beyond this stack, product, or company.
   - **A human moment**: a decision, disagreement, or judgment call worth reflecting on.
   - **An open, unresolved decision**: a choice not yet made, a problem with no plan
     behind it, a thing I don't know how to do. This is source material for the
     `ask` shape below, not a finding — don't force it into one.

   Write each candidate as a single declarative sentence stating the fact itself.
   If a candidate can only be written as "I fixed/built/shipped X", discard it — that's a task, not a fact.

   Report the fact neutrally: not spun as a bug fixed or a failure averted, and not spun as
   a win or a result that beat expectations. State what's true and let it stand on its own.

2. Score each candidate against three gates. It must pass ALL of them:
   - **In-lane**: fits one of the topic lanes in the style guide.
   - **Interesting to a smart outsider**: someone who doesn't know this stack — another
     founder, an operator, an investor — would find it worth knowing. Would they repeat
     it to someone else? If it's only interesting to someone debugging this exact system, reject it.
   - **First-hand**: contains a specific number, moment, or result that only this person could report.

   **Exception — the `ask` shape.** At most one candidate in the whole batch may
   instead be an open decision from the list above: first person, stakes stated
   plainly, under 600 characters, ending on a question I actually want answered.
   It skips the first-hand/number gate above entirely — it isn't a finding and
   doesn't need one — but still must be in-lane and genuinely interesting. It
   must also be genuinely unresolved right now: if the answer is already known,
   or the question is rhetorical, that's engagement bait, not an `ask`, and must
   be rejected. The last `ask` post was {DAYS_SINCE_ASK}; that's a soft cap on
   rarity, not a quota — don't write one just because it's been a while, and
   never write more than one per batch.

   A day of routine work with no surprising fact is a valid outcome — say so rather than forcing a post.

3. Write a post for EVERY candidate that passed all three gates, best first, up to
   {MAX_DRAFTS} of them. Don't merge two facts into one post, and don't pad the list with
   candidates that failed a gate — three strong posts beat six weak ones.

   For each one, privately draft 3 hooks, each using a DIFFERENT move from this list.
   A move is only available when the day's material genuinely supports it. If it
   doesn't, use another move rather than stretching the facts to fit one.

   1. **The number that's off.** A cost, ratio, or measurement far from what anyone
      would guess.
   2. **The belief I was wrong about.** First person, stated as the belief itself,
      not as the correction.
   3. **The old way is finished.** Only if the day actually retired something that
      used to be standard practice.
   4. **First time I've seen this.** Only if it genuinely is the first — never
      "rarely" dressed up as "never".
   5. **The thing everyone tolerates and shouldn't.** Aim at the practice, never at
      a named company or person.
   6. **The finding, stated flat.** No stance, just the surprising fact.

   **No hook may contain a tool name, a version, or any setup clause, and each must be
   under ~50 characters.** Keep the strongest one and discard the rest. Prefer the hook
   carrying a number or personal stakes, and prefer a move that does not repeat the
   recently-used hooks listed below.

   If this batch includes an `ask`-shaped candidate, skip this 3-hook exercise for
   it — write it directly, first line stating the stakes plainly, no move needed.

4. Write each post in {LANGUAGE_OUT}, obeying every rule in the style guide:
   - Lead with the hook. Then the finding, then why it matters.
   - Mechanism gets 1–2 sentences maximum, only if the fact is meaningless without it.
   - Cut the investigation narrative completely — no "I checked X, then Y".
   - Decide whether this post should end with a genuine invitation to the reader
     (how they'd approach it, what they've seen). Do that for roughly one post in two,
     whenever the day actually contains a question I want answered — never as a default.
   - Length is set by the story, not a target — see the style guide. Check the
     "Recent shapes" table below and make sure this post is a visibly different
     length from what's there, not a near-miss of the same size. An `ask` post
     is always under 600 characters.

5. Self-check and fix each post:
   - Does line 1 work as the entire post if nobody clicks "see more"?
   - Could a smart non-specialist explain the point back to me after 15 seconds?
   - Did I delete every sentence that only proves work happened?
   - Do two posts overlap? If so, cut the weaker one.

6. **FINAL PASS — do this last, on every finished post.**
   Rewrite the text; do not merely inspect it.

   a. **Strip what stays private.** Go word by word looking for a client, partner,
      prospect, or anyone we do business with (anonymize to "a client"/"a partner" —
      the pattern teaches, not the name), and for any vendor whose name would reveal
      how the product actually works (see the style guide's "What stays private").
      The hook and the alternates included. Public tools — the AI coding agents,
      models, hosting, observability, datasets — are fine named plainly and don't
      need to be replaced. After stripping, re-read: does the fact still land? If
      removing a name emptied it, sharpen it with a number or a consequence instead.

   b. **De-AI it.** Hunt these and rewrite the whole sentence in plain spoken
      English — deleting the phrase alone leaves the machine rhythm behind:
      - "It's not X, it's Y" and every sibling contrast construction
      - more than one em dash in the post (rewrite as two sentences or a comma)
      - "Here's how/what/why", rhetorical triads inside a sentence, symmetrical
        clause pairs (this does NOT ban enumerated lists of real, specific items)
      - "thrilled", "humbled", "unlock", "elevate", "delve", "game-changer"
      - every sentence that could have been written about any company by anyone
      - sentences that run long; keep the average under 12 words. (Varying
        sentence length is not itself the win — shortness is.)

   c. **Read it aloud in your head.** Any clause you would not say to a colleague
      over coffee gets rewritten or cut. Then confirm this post's length is still
      visibly different from the others in this batch and from the recent-shapes
      table — never pad to get there, and never compress a complete story just to
      look different.

# Recently used hooks

These opened my last posts, newest first. Each night's draft is written without
memory of the one before, so this list is the only thing stopping the feed from
reading as one repeated formula. Don't reuse a move that appears here unless the
day leaves no honest alternative.

{RECENT_HOOKS}

# Recent shapes

This is the shape of every recent post: how long it was, how it opened, whether
it ended on a question. Today's post has to be a different shape from those.
Different length — visibly, not by fifty characters — and a different way in.
If a candidate would come out looking like something in this table, either find
a different angle on it or draft a different fact instead.

{RECENT_SHAPES}

# Output

Return ONLY a JSON object, no other text:

{
  "candidates": [
    {
      "fact": "the fact itself, one declarative sentence",
      "why": "why it passes the gates — what makes it interesting to an outsider",
      "shape": "finding | list | ask — finding for a standard post, list for an\
 enumerated-items post, ask for the open-question exception",
      "post_text": "the full post, ready to publish"
    }
  ],
  "rejected": [
    {"fact": "one declarative sentence", "reason": "which gate it failed and why"}
  ]
}

Order `candidates` best first. Include every fact you considered and discarded in
`rejected`, one line each — the reader may disagree with your judgement and ask for one.

If nothing passes all three gates, return `"candidates": []` and still fill `rejected`.
