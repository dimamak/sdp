// autoscroll.js — opt-in "radar tab" auto-scroll (plan.md §3, off by
// default via radar.extension.autoscroll). Scrolls only. Never clicks,
// likes, follows, opens a post, or posts — that's enforced simply by this
// file calling nothing but window.scrollBy().
//
// Randomised interval (20-90s) and distance (1-3 viewports) are deliberate:
// uniform cadence with zero dwell variance is itself a fingerprint the 2026
// purge analysis (plan.md §3) reports X scoring for.
window.RadarAutoscroll = (() => {
  const state = { timer: null, pausedUntil: 0, screensThisHour: [], started: false };

  function inWindow(startHHMM, endHHMM, now) {
    if (!startHHMM || !endHHMM) return true; // unset = always on
    const [sh, sm] = startHHMM.split(":").map(Number);
    const [eh, em] = endHHMM.split(":").map(Number);
    const start = sh * 60 + sm;
    const end = eh * 60 + em;
    const cur = now.getHours() * 60 + now.getMinutes();
    return start <= end ? cur >= start && cur <= end : cur >= start || cur <= end;
  }

  function prune(list, now) {
    const hourAgo = now - 3600_000;
    while (list.length && list[0] < hourAgo) list.shift();
  }

  function randomBetween(a, b) {
    return a + Math.random() * (b - a);
  }

  function scheduleNext(settings) {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => tick(settings), randomBetween(20, 90) * 1000);
  }

  function tick(settings) {
    const now = Date.now();
    if (document.hidden) return scheduleNext(settings); // foreground-visible tab only
    if (now < state.pausedUntil) return scheduleNext(settings);
    if (!inWindow(settings.activeHoursStart, settings.activeHoursEnd, new Date())) {
      return scheduleNext(settings);
    }
    prune(state.screensThisHour, now);
    if (state.screensThisHour.length >= (settings.maxScreensPerHour || 40)) {
      return scheduleNext(settings);
    }
    window.scrollBy({ top: window.innerHeight * randomBetween(1, 3), behavior: "smooth" });
    state.screensThisHour.push(now);
    scheduleNext(settings);
  }

  function pauseOnInput() {
    state.pausedUntil = Date.now() + 30_000; // real user input wins for 30s
  }

  function start(settings) {
    if (state.started) return;
    state.started = true;
    ["keydown", "mousedown", "wheel", "touchstart"].forEach((ev) =>
      document.addEventListener(ev, pauseOnInput, { passive: true }));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) state.pausedUntil = Date.now();
    });
    scheduleNext(settings);
  }

  function stop() {
    clearTimeout(state.timer);
    state.timer = null;
    state.started = false;
  }

  return { start, stop };
})();
