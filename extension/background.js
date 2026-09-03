// background.js — MV3 service worker. The only piece that talks to
// http://127.0.0.1:<port> (plan.md §3): the content script never fetches
// anything itself, since a direct https://x.com -> 127.0.0.1 fetch is
// blocked by Chrome regardless of host_permissions (verified empirically,
// plan.md §3 "Localhost access") and routing through here is what makes the
// request same-origin-exempt instead of CORS-preflighted.
//
// Never sets `targetAddressSpace` on any fetch — Chrome enforces it from
// extension origins and rejects the mismatch, which would break this path.

const DEFAULT_PORT = 8765;

async function getSettings() {
  const { token, port, ownHandle } = await chrome.storage.local.get(["token", "port", "ownHandle"]);
  return {
    token: token || "",
    port: port || DEFAULT_PORT,
    ownHandle: (ownHandle || "").replace(/^@/, "").toLowerCase(),
  };
}

async function localApiFetch(path, options = {}) {
  const { token, port } = await getSettings();
  if (!token) return { ok: false, error: "no local API token configured" };
  const url = `http://127.0.0.1:${port}${path}`;
  try {
    const res = await fetch(url, {
      ...options,
      headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    return await res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "radar_posts") {
    localApiFetch("/radar/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(msg.posts),
    }).then(sendResponse);
    return true; // keep the channel open for the async response
  }
  if (msg.type === "radar_suggestions") {
    localApiFetch(`/radar/suggestions?ids=${encodeURIComponent(msg.ids.join(","))}`)
      .then(sendResponse);
    return true;
  }
  if (msg.type === "radar_replied") {
    localApiFetch("/radar/replied", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ post_id: msg.postId }),
    }).then(sendResponse);
    return true;
  }
  if (msg.type === "radar_own_handle") {
    getSettings().then(({ ownHandle }) => sendResponse({ ownHandle }));
    return true;
  }
  return false;
});
