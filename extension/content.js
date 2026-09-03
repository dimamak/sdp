// content.js — reads scored fields off rendered timeline/profile/search
// tweet nodes and forwards them to the background service worker in small
// batches (plan.md §3). Never clicks, hovers, or fetches anything itself
// except opening the native reply composer via card.js's "Insert reply",
// which never clicks Post.
(() => {
  const sentIds = new Set(); // avoid re-sending a post already forwarded this page-load
  const articlesById = new Map(); // post id -> its current article node, for card.js
  let pending = [];
  let flushTimer = null;
  let settings = { ownHandle: "", autoscroll: false, activeHoursStart: "", activeHoursEnd: "",
                    maxScreensPerHour: 40 };

  function loadSettings() {
    chrome.storage.local.get(
      ["ownHandle", "autoscroll", "activeHoursStart", "activeHoursEnd", "maxScreensPerHour"],
      (v) => {
        settings = {
          ownHandle: (v.ownHandle || "").replace(/^@/, "").toLowerCase(),
          autoscroll: !!v.autoscroll,
          activeHoursStart: v.activeHoursStart || "",
          activeHoursEnd: v.activeHoursEnd || "",
          maxScreensPerHour: v.maxScreensPerHour || 40,
        };
        maybeStartAutoscroll();
      });
  }

  function maybeStartAutoscroll() {
    // Autoscroll is meant for a dedicated "radar tab" left on the home feed
    // (plan.md §3's autoscroll_source) — it's agnostic to the For You /
    // Following sub-tab, since X doesn't expose that choice in the URL.
    if (settings.autoscroll && location.pathname === "/home") {
      window.RadarAutoscroll.start(settings);
    } else {
      window.RadarAutoscroll.stop();
    }
  }

  function extract(article) {
    const link = RadarSelectors.permalink(article);
    if (!link) return null;
    const ts = RadarSelectors.timestamp(article);
    if (!ts) return null;
    const c = RadarSelectors.counts(article);
    return {
      id: link.id,
      author_handle: link.authorHandle,
      text: RadarSelectors.text(article),
      created_at: ts,
      views: c.views || 0,
      likes: c.likes || 0,
      reposts: c.reposts || 0,
      replies: c.replies || 0,
      is_own: !!(settings.ownHandle && link.authorHandle.toLowerCase() === settings.ownHandle),
      is_reply: RadarSelectors.isReply(article),
      is_repost: RadarSelectors.isRepost(article),
    };
  }

  function queue(article) {
    const post = extract(article);
    if (!post) return;
    articlesById.set(post.id, article);
    if (sentIds.has(post.id)) return;
    sentIds.add(post.id);
    pending.push(post);
    if (!flushTimer) flushTimer = setTimeout(flush, 2000);
  }

  function flush() {
    flushTimer = null;
    if (!pending.length) return;
    const batch = pending;
    pending = [];
    chrome.runtime.sendMessage({ type: "radar_posts", posts: batch }, () => {
      pollSuggestions(batch.map((p) => p.id));
    });
  }

  function pollSuggestions(ids) {
    if (!ids.length) return;
    chrome.runtime.sendMessage({ type: "radar_suggestions", ids }, (resp) => {
      if (!resp || !resp.suggestions) return;
      for (const s of resp.suggestions) {
        const article = articlesById.get(s.post_id);
        if (article && article.isConnected) window.RadarCard.mount(article, s);
      }
    });
  }

  // Suggestions take a few seconds (an LLM call) — re-check recently-seen
  // posts on a slow interval rather than only right after sending them.
  setInterval(() => pollSuggestions([...articlesById.keys()].slice(-50)), 15_000);

  function scan(root) {
    (root.querySelectorAll ? root.querySelectorAll(RadarSelectors.TWEET) : []).forEach(queue);
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches && node.matches(RadarSelectors.TWEET)) queue(node);
        else scan(node);
      }
    }
  });

  loadSettings();
  chrome.storage.onChanged.addListener(loadSettings);
  observer.observe(document.body, { childList: true, subtree: true });
  scan(document); // whatever's already rendered on load
})();
