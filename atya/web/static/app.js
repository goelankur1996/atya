// Small progressive enhancements. Every page works without this file.
document.documentElement.classList.add("js");
// Selects marked data-autosubmit save as soon as they change.
document.addEventListener("change", function (e) {
  var el = e.target;
  if (el && el.matches && el.matches("select[data-autosubmit]") && el.form) el.form.submit();
});
// "/" focuses the search box, unless typing somewhere already.
document.addEventListener("keydown", function (e) {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target, tag = t && t.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
  var box = document.getElementById("topq");
  if (box) { e.preventDefault(); box.focus(); }
});
// Highlight chosen items in multi-pickers as they're toggled.
document.addEventListener("change", function (e) {
  var el = e.target;
  if (el && el.closest && el.closest(".picker") && el.type === "checkbox") {
    el.closest("label").classList.toggle("on", el.checked);
  }
});
// Messages open at the newest message (bottom), like a chat.
document.addEventListener("DOMContentLoaded", function () {
  var end = document.getElementById("latest");
  if (end && !location.hash) end.scrollIntoView({ block: "end" });
});
