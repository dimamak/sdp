const FIELDS = ["token", "port", "ownHandle", "autoscroll",
                "activeHoursStart", "activeHoursEnd", "maxScreensPerHour"];

function $(id) {
  return document.getElementById(id);
}

chrome.storage.local.get(FIELDS, (v) => {
  $("token").value = v.token || "";
  $("port").value = v.port || 8765;
  $("ownHandle").value = v.ownHandle || "";
  $("autoscroll").checked = !!v.autoscroll;
  $("activeHoursStart").value = v.activeHoursStart || "";
  $("activeHoursEnd").value = v.activeHoursEnd || "";
  $("maxScreensPerHour").value = v.maxScreensPerHour || 40;
});

$("save").addEventListener("click", () => {
  chrome.storage.local.set({
    token: $("token").value.trim(),
    port: parseInt($("port").value, 10) || 8765,
    ownHandle: $("ownHandle").value.trim().replace(/^@/, ""),
    autoscroll: $("autoscroll").checked,
    activeHoursStart: $("activeHoursStart").value.trim(),
    activeHoursEnd: $("activeHoursEnd").value.trim(),
    maxScreensPerHour: parseInt($("maxScreensPerHour").value, 10) || 40,
  }, () => {
    $("status").textContent = "Saved.";
    setTimeout(() => { $("status").textContent = ""; }, 1500);
  });
});
