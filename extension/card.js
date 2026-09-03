// card.js — Shadow-DOM card mounted under a scored post, showing the
// drafted reply (or the follow-up question) with an "Insert reply" button
// (plan.md §7). It never clicks Post — it opens the native reply composer
// and prefills it; sending is always a manual, deliberate tap.
window.RadarCard = (() => {
  function mount(article, data) {
    if (article.dataset.radarCardMounted) return;
    article.dataset.radarCardMounted = "1";

    const host = document.createElement("div");
    host.className = "radar-card-host";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        .card { border: 1px solid #536471; border-radius: 12px; margin: 8px 0;
                padding: 10px 12px; font: 14px system-ui, sans-serif; color: #e7e9ea;
                background: #16181c; }
        .label { opacity: 0.7; font-size: 12px; margin-bottom: 6px; }
        .draft { white-space: pre-wrap; margin-bottom: 8px; }
        button { background: #1d9bf0; color: #fff; border: none; border-radius: 999px;
                 padding: 6px 14px; font-weight: 600; cursor: pointer; }
        button:hover { background: #1a8cd8; }
      </style>
      <div class="card">
        <div class="label">${data.question ? "❓ radar question" : "⚡ radar draft"}</div>
        <div class="draft"></div>
        <button type="button">${data.question ? "Answer" : "Insert reply"}</button>
      </div>
    `;
    root.querySelector(".draft").textContent = data.reply || data.question || "";
    if (!data.question) {
      root.querySelector("button").addEventListener(
        "click", () => insertReply(article, data.reply));
    } else {
      // The question flow's answer is a free-text Telegram reply
      // (plan.md §6 step 2b) — there is no in-page answer path, so the
      // button here just points the user there instead of duplicating it.
      root.querySelector("button").addEventListener(
        "click", () => window.alert("Answer this in the sdp Telegram chat — " +
          "the draft will follow from your reply."));
    }
    article.insertAdjacentElement("afterend", host);
  }

  // document.execCommand('insertText') is the one method that fires
  // beforeinput/input with isTrusted: true, which React's synthetic event
  // system honours — setting textContent desyncs React state and the text
  // vanishes on send (plan.md §7).
  function insertReply(article, text) {
    if (!text) return;
    const replyButton = article.querySelector('[data-testid="reply"]');
    if (replyButton) replyButton.click(); // opens the native composer inline
    setTimeout(() => {
      const box = document.querySelector('[data-testid="tweetTextarea_0"]');
      if (!box) {
        console.warn("[radar] could not find the reply composer textarea");
        return;
      }
      box.focus();
      document.execCommand("insertText", false, text);
    }, 300);
  }

  return { mount };
})();
