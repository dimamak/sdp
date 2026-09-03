// Plain-Node test for selectors.js (plan.md §13's "jsdom fixture of a
// captured x.com tweet node"). Deliberately not real jsdom — this repo has
// no npm/JS toolchain, and a hand-rolled fake DOM covering the handful of
// querySelector() calls selectors.js actually makes is enough to exercise
// the part that matters: exact integers parsed from the aria-label, not the
// abbreviated "1.2K" visible text.
//
// Run with: node extension/selectors.test.js
"use strict";
const assert = require("node:assert");
const RadarSelectors = require("./selectors.js");

function fakeArticle({ href, tweetText, datetime, verified, ariaLabel, socialContext,
                        textContent } = {}) {
  const elements = {
    'a[href*="/status/"]': href ? { getAttribute: (a) => (a === "href" ? href : null) } : null,
    '[data-testid="tweetText"]': tweetText != null
      ? { innerText: tweetText, textContent: tweetText } : null,
    "time[datetime]": datetime ? { getAttribute: (a) => (a === "datetime" ? datetime : null) }
      : null,
    'svg[data-testid="icon-verified"]': verified ? {} : null,
    'div[role="group"]': ariaLabel
      ? { getAttribute: (a) => (a === "aria-label" ? ariaLabel : null) } : null,
    '[data-testid="socialContext"]': socialContext != null ? { textContent: socialContext }
      : null,
  };
  return { querySelector: (sel) => elements[sel] || null, textContent: textContent || "" };
}

{
  const article = fakeArticle({
    href: "/someauthor/status/1234567890123456789",
    tweetText: "hello world",
    datetime: "2026-09-01T12:00:00.000Z",
    ariaLabel: "12 replies, 340 reposts, 1834 likes, 56 bookmarks, 92123 views",
  });
  assert.deepStrictEqual(RadarSelectors.permalink(article),
    { authorHandle: "someauthor", id: "1234567890123456789" });
  assert.strictEqual(RadarSelectors.text(article), "hello world");
  assert.strictEqual(RadarSelectors.timestamp(article), "2026-09-01T12:00:00.000Z");
  assert.deepStrictEqual(RadarSelectors.counts(article),
    { replies: 12, reposts: 340, likes: 1834, bookmarks: 56, views: 92123 });
  console.log("ok - exact integers parsed from the aria-label, not '1.2K' text");
}

{
  const article = fakeArticle({ ariaLabel: "1,234 replies, 2,000,000 views" });
  const counts = RadarSelectors.counts(article);
  assert.strictEqual(counts.replies, 1234);
  assert.strictEqual(counts.views, 2000000);
  console.log("ok - comma-separated large counts parsed");
}

{
  const article = fakeArticle({});
  assert.strictEqual(RadarSelectors.permalink(article), null);
  assert.strictEqual(RadarSelectors.timestamp(article), null);
  assert.deepStrictEqual(RadarSelectors.counts(article), {});
  console.log("ok - missing fields degrade to null/{} instead of throwing");
}

{
  assert.strictEqual(RadarSelectors.isRepost(fakeArticle({ socialContext: "Jane Doe reposted" })),
    true);
  assert.strictEqual(RadarSelectors.isRepost(fakeArticle({})), false);
  console.log("ok - repost detection off the socialContext line");
}

{
  assert.strictEqual(
    RadarSelectors.isReply(fakeArticle({ textContent: "Replying to @someone\nhello" })), true);
  assert.strictEqual(RadarSelectors.isReply(fakeArticle({ textContent: "hello world" })), false);
  console.log("ok - reply detection off the leading 'Replying to' text");
}

console.log("selectors.test.js: all assertions passed");
