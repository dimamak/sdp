// selectors.js — every x.com DOM selector the radar depends on, in one
// place (plan.md §3). data-testid churn is the real maintenance risk here,
// not bans, so each lookup logs loudly the first time it comes up empty
// instead of failing silently.
//
// Only the fields in plan.md §3's table are verified present on every tweet
// node with no hover, no click, and no extra request: permalink, tweetText,
// time[datetime], the role="group" aria-label counts, and the verified
// badge. isReply()/isRepost() below are best-effort heuristics layered on
// top — X doesn't expose a stable testid for either — and are documented as
// such rather than claimed verified.
const RadarSelectors = (() => {
  const warned = new Set();
  function warnOnce(key, msg) {
    if (!warned.has(key)) {
      warned.add(key);
      console.warn(`[radar] ${msg}`);
    }
  }

  const TWEET = 'article[data-testid="tweet"]';
  const TWEET_TEXT = '[data-testid="tweetText"]';
  const VERIFIED = 'svg[data-testid="icon-verified"]';
  const GROUP = 'div[role="group"]';

  function permalink(article) {
    const a = article.querySelector('a[href*="/status/"]');
    if (!a) {
      warnOnce("permalink", "no status permalink found on a tweet node");
      return null;
    }
    const m = a.getAttribute("href").match(/^\/([^/]+)\/status\/(\d+)/);
    return m ? { authorHandle: m[1], id: m[2] } : null;
  }

  function text(article) {
    const el = article.querySelector(TWEET_TEXT);
    return el ? (el.innerText || el.textContent || "") : "";
  }

  function timestamp(article) {
    const t = article.querySelector("time[datetime]");
    if (!t) {
      warnOnce("time", "no time[datetime] found on a tweet node");
      return null;
    }
    return t.getAttribute("datetime");
  }

  function verified(article) {
    return !!article.querySelector(VERIFIED);
  }

  // "12 replies, 340 reposts, 1834 likes, 56 bookmarks, 92123 views" — the
  // visible text is the abbreviated "1.2K" form; the aria-label spells out
  // the exact integer, which is why this is parsed instead (plan.md §3).
  function counts(article) {
    const group = article.querySelector(GROUP);
    const label = group && group.getAttribute("aria-label");
    if (!label) {
      warnOnce("counts", "no role=group aria-label found on a tweet node");
      return {};
    }
    const out = {};
    const pairs = [
      ["replies", /([\d,]+)\s+repl(?:y|ies)/i],
      ["reposts", /([\d,]+)\s+repost/i],
      ["likes", /([\d,]+)\s+like/i],
      ["bookmarks", /([\d,]+)\s+bookmark/i],
      ["views", /([\d,]+)\s+view/i],
    ];
    for (const [key, re] of pairs) {
      const m = label.match(re);
      if (m) out[key] = parseInt(m[1].replace(/,/g, ""), 10);
    }
    return out;
  }

  // Best-effort: replies show a "Replying to" line with no stable testid to
  // anchor on, so this matches the visible text instead.
  function isReply(article) {
    return /^\s*Replying to/.test(article.textContent || "");
  }

  // Best-effort: a repost shows a "socialContext" line above the tweet
  // (e.g. "so-and-so reposted").
  function isRepost(article) {
    const ctx = article.querySelector('[data-testid="socialContext"]');
    return !!(ctx && /repost|retweet/i.test(ctx.textContent || ""));
  }

  return { TWEET, TWEET_TEXT, permalink, text, timestamp, verified, counts, isReply, isRepost };
})();

// CommonJS export for the plain-Node test below; `module` doesn't exist in
// the content-script/browser context this file otherwise runs in.
if (typeof module !== "undefined") module.exports = RadarSelectors;
