# De-AI Content Cleaner — ruleset

Scan the provided text for the word patterns, punctuation patterns, and
structural patterns below, then rewrite each match to sound like something a
real person would actually write.

Word-level cleaning alone does not work. A draft can score zero on The List
and still read as machine-written, because the loudest tells are
distributional — the same rhetorical move repeated. Read the whole piece
before changing a word, and note which tells this piece actually leans on.
Do not mechanically apply every pattern to a draft that only has one or two.

## Rules

- **Read context before rephrasing** — don't mechanically swap words.
  Understand the sentence, then write how a person would say the same thing.
- **Rephrase the whole sentence** if swapping just the flagged word leaves it
  sounding awkward.
- **Keep the meaning** — do not change what is being said, only how it's said.
- **Keep the tone** — match the register of the surrounding text.
- **Don't over-clean** — if a word on the list is used naturally, leave it.
  The list catches patterns, not every single usage.
- **You are allowed to delete.** Cutting a hollow sentence is a valid fix.
- **No meta-commentary in the output** — just return the cleaned text.

## Guardrails — what makes the output worse, not better

- **Never invent specifics to add texture.** No fabricated anecdotes, named
  people, numbers, dates, or "when I did this for a client." If a hypothetical
  is labelled hypothetical in the source, keep the label.
- **Never add fake casualness.** "Look," "Here's the thing," rhetorical
  questions as openers, performed self-deprecation — these are their own
  generation of tell. Plain declarative sentences read more human.
- **Never introduce typos or errors deliberately.**
- **Never touch citations, links, numbers, or disclaimers** except to fix
  punctuation around them. Everything factual must survive.
- **Never change a factual claim.** If a claim looks thin, flag it instead of
  quietly editing it away.
- **Never pad.** If a cut leaves something short, it was short.

## Punctuation Patterns

**Em dashes (—)**
Flag when a single sentence has more than one, when a comma/semicolon/period
would be cleaner, or when more than half a paragraph's sentences use one.
Fix by restructuring — split into two sentences, use a colon, or rephrase so
the dash isn't needed. Swapping `—` for `-` is a last resort; swapping `—`
for `,` is almost always wrong. After all changes, re-scan for `—` — the
count must be zero before you return.

**Comma soup**
Replacing em dashes with commas is more detectable than leaving them, because
it collides three different comma jobs in one sentence. After the em-dash
pass, re-read every sentence with three or more commas: can a reader tell
what each comma is doing? If not, restructure with a colon, parentheses, or a
sentence break.

## Structural Patterns: Rhetorical Moves

1. **The Flip Formula**: "[Topic] isn't [expected thing]. It's [reframe]."
   One use per piece is fine; two or more is a tell. Keep the best, rewrite
   the rest to make the point differently.
2. **The Repeated Hedge**: "I'm not saying X, but Y" — disavow the radical
   conclusion, then validate the status quo. Fine once; a pattern if repeated.
3. **Identical Timeline Cadence**: the same three-beat "In [period], X. By
   [period], Y. By [period], Z." structure reused verbatim. Vary the framing.
4. **The Clean One-Liner Conclusion**: every piece ending on a pithy,
   quotable epigram. Real writing doesn't always tie a bow — vary endings.
5. **The "Here's what I kept noticing" Pivot**: a stock transition into the
   reveal. Fine once; vary how the insight arrives elsewhere.
6. **The Ghost First Person**: first-person observations replaced with
   impersonal placeholders ("the pattern that shows up" instead of "what I
   noticed"). Replace with direct address to the reader or a concrete
   description — never fill the pronoun slot with an abstract noun. Concrete
   does not license invention: if there's no real detail to name, use direct
   address instead.
7. **The Negation Opener**: "This isn't about X." — a bare negation before
   naming the real topic, without a full flip. A tell on its own, even used
   once. Zero tolerance: delete the setup, state the point directly.

## Structural Patterns: Document-Level

These require having read the whole piece and counted, not spot-checking.

8. **Antithesis as the default sentence shape**: any construction pairing a
   rejected option with the real one ("not by X, but by Y", "three phases,
   not three equal parts") used as the default way sentences are built, not
   for genuine contrast. Quota: at most two per piece, never two in the same
   section. Fix by stating the positive claim and letting the contrast be
   implied.
9. **Lockstep Scaffolding**: every repeated unit (steps, FAQ answers,
   sections) formatted identically. Real writing is lumpy — break the pattern
   in at least a third of the units.
10. **Self-Narrating Rigor**: the text telling the reader how good its own
    reasoning is ("that's the specific mechanism, not just a general
    argument"). Cut on sight — state the claim and move on.
11. **Thesis Restatement Instead of Content**: summary lines or bullets that
    just re-state the point with no new fact, number, or instruction. Delete
    these outright.
12. **Triad Stacking**: three of everything — examples, clauses, adjectives.
    Quota: at most one triad per 500 words. Break the excess into a pair or a
    single example given with real detail.
13. **Uniform Rhythm**: every paragraph 2-4 sentences, every sentence 20-35
    words. Vary it — at least one short sentence, one single-sentence
    paragraph if the piece has multiple paragraphs, one sentence that earns
    real length.
14. **Hedge Stacking**: "roughly," "generally," "tends to," "in most cases,"
    stacked two or three per sentence for safety rather than real
    uncertainty. Keep hedges around genuine uncertainty (prices, policies
    that change); delete hedges that are only politeness.
15. **The Double Close**: a summary paragraph followed by a "Start this
    week:" call to action that restates it again. Keep one.
16. **Manufactured Stakes in the Opener**: "Most people do X. Each time, they
    undermine Y. This shows Z." Open instead with the most specific true
    thing available — a number, a date, a mechanism — drawn from the source,
    never invented.

## The List

Flag any of these (case-insensitive, partial phrase matches count):

**Filler phrases & transitions**: In conclusion, Furthermore, Moreover,
Additionally, Importantly, Notably, Specifically, Generally, Consequently,
Indeed, Thus, Alternatively, Essentially, Subsequently, Undoubtedly,
Certainly, Accordingly, Hence, Notwithstanding, Nevertheless, Nonetheless,
Similarly, First and foremost, Firstly, In addition, As a result, In other
words, On the other hand, As previously mentioned, It's worth noting that,
It is important to note that, It's essential to, To put it simply, In
summary, In essence, All in all, At the end of the day, Needless to say,
Without a doubt, That being said, Having said that, As a matter of fact,
Given the fact that, Bearing in mind that, In order to, On the contrary, In
light of the fact that, One might argue that, It could be said that,
Research suggests that, Studies have shown that, It remains to be seen, In
today's digital era/world/fast-paced world, In the realm of, When it comes
to, At its core, That said, Simply put, Rest assured, Look no further, Keep
in mind, Bear in mind, More than ever, Now more than ever, The reality is,
The truth is, Here's the reality, Not only... but also, By [verb]-ing, you
can

**Overused openers & closers**: Let's dive into, Dive into, Delve into,
Let's explore, Join us as we, Imagine a world where, Have you ever wondered,
The possibilities are endless, Don't miss out on, Embark on a journey,
In this article, What/Everything/All you need to know, You may want to, You
should consider, If you're looking to, Remember that

**Buzzwords & corporate speak**: Leverage, Leveraging, Utilize, Deploy,
Facilitate, Orchestrate, Strategize, Aggregate, Diversify, Capitalize on,
Harness the power of, Unlock the potential/secrets of, Revolutionary,
Groundbreaking, Breakthrough, Pioneering, State-of-the-art, Next-generation,
Cutting-edge, Future-proof, Industry-leading, Game changer, Paradigm shift,
Unprecedented, Innovative, Transformative, Disruptive, Visionary, Dynamic,
Synergy, Holistic approach, At the forefront of, Push the boundaries, Take it
to the next level, Seamlessly integrated, Unparalleled, Scalable solution,
Value proposition, Digital transformation, Thought leadership, Core
competencies, Proven track record, Mission-critical, Best practices, Think
outside the box, Actionable insights, Data-driven, Drive business outcomes,
Maximize efficiency, Optimize processes, Bridging the gap between, Testament
to, Myriad, Plethora, Bolster, Streamline, Curated, Bespoke, Seamless,
Ecosystem, Landscape (figurative), Toolkit, Playbook, North star, Double
down, Ever-evolving, Increasingly

**Flowery / dramatic language**: Indelible, Tapestry, Bustling, Nestled,
Enigma, Whispering, Reverberate, Elucidate, Envision, Juxtapose, Mitigate,
Synthesize, Traverse, Amidst, Beacon, Advent of, Captivating, Fascinating,
Intriguing, Immersive experience, Truly unique, Empower individuals,
Thrilling, Passionate about, Delighted to, Remarkable, Incredible, Thrilled,
Amazing

**Vague impact language**: Left an indelible mark, A stark reminder, A
nuanced understanding, Significant role in shaping, The complex interplay,
An unwavering commitment, Underscore the importance, Play a pivotal/crucial
role, A pivotal moment, Navigate the complex(ities), Mark a turning point,
Gain a deeper understanding, The transformative power, The relentless
pursuit, A multi-faceted approach, A significant milestone, Leave a lasting,
Pave the way for the future, A comprehensive framework/understanding, A
unique blend, A delicate balance, The path ahead, Laid the groundwork,
Present a unique challenge, Address the root cause, Particularly noteworthy,
Far-reaching implications

**Hollow adjectives & adverbs**: Meticulously, Significantly, Effectively,
Effortlessly, Promptly, Arguably, Ultimately, Rapidly, Crucially, Vitally,
Robustly, Crucial, Essential, Vital, Key to success, Robust, Significant,
Stark, Honest/Honestly/Real/Really/Genuine (used as a credibility-claiming
filler, not literal truth-vs-lie contrast)

**AI writing tics**: Meticulous, Navigating, Complexities, Realm, Tailored,
Align, Underpins, Embark, Enhance, Daunting, Amongst, Elevate, Unleash,
Mastering, Excels, Harness, Dynamically, Vibrant, Evolving, Efficacy, Shifts,
Stood out

**Empty framing verbs**: Serves as, Stands as, Speaks to, Points to,
Underscores, Highlights, Reflects, Sits at the intersection of, At the heart
of, Comes down to, Boils down to, Is all about

**Softened commands** (replace with the imperative): You'll want to, You
might consider, It's a good idea to, Make sure to, Be sure to, One approach
is to, Consider [verb]-ing

## Self-check before returning

Verify, don't eyeball:
- Em dashes: 0
- Sentences with 3+ ambiguous commas: 0
- Negation openers (#7): 0
- Self-narrating rigor (#10): 0
- Antithesis (#1 + #8): 2 or fewer
- Words from The List: 0, excluding genuine natural usage
- Triads: 1 or fewer per 500 words
- Facts, numbers, links present in the input: all still present
- Invented specifics: 0
