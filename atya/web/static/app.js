// Small progressive enhancements. Every page works without this file.
document.documentElement.classList.add("js");
// Track the sticky top bar's height, so bars pinned under it sit right below it.
function setTopHeight() {
  var top = document.querySelector("header.top");
  if (top) document.documentElement.style.setProperty("--top-h", top.offsetHeight + "px");
}
document.addEventListener("DOMContentLoaded", setTopHeight);
window.addEventListener("resize", setTopHeight);
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
// Messages: show "Earlier messages above" while there is more thread above the
// view, and a "Latest" button while the newest message is out of view.
document.addEventListener("DOMContentLoaded", function () {
  var thread = document.getElementById("thread");
  var end = document.getElementById("latest");
  var hint = document.getElementById("scroll-hint");
  var jump = document.getElementById("jump-latest");
  if (!thread || !end || !("IntersectionObserver" in window)) return;
  var first = thread.firstElementChild;
  if (first && hint) {
    new IntersectionObserver(function (es) {
      hint.hidden = es[0].isIntersecting;
    }, { rootMargin: "-220px 0px 0px 0px" }).observe(first);
    hint.addEventListener("click", function () { window.scrollBy({ top: -window.innerHeight * 0.7, behavior: "smooth" }); });
  }
  if (jump) {
    new IntersectionObserver(function (es) { jump.hidden = es[0].isIntersecting; }).observe(end);
  }
});
// Chat composer: Enter sends, Shift+Enter adds a line; the box grows with text.
document.addEventListener("DOMContentLoaded", function () {
  var form = document.getElementById("composer");
  if (!form) return;
  var box = form.querySelector("textarea");
  var btn = form.querySelector(".sendbtn");
  function grow() { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 200) + "px"; }
  box.addEventListener("input", grow);
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (box.value.trim()) form.requestSubmit();
    }
  });
  form.addEventListener("submit", function () { btn.disabled = true; });
  if (location.hash === "#latest" || !location.hash) box.focus({ preventScroll: true });
});
// While a message is being worked on, check every 2 s and reload when done.
document.addEventListener("DOMContentLoaded", function () {
  var thread = document.getElementById("thread");
  if (!thread || !thread.dataset.pending) return;
  var ids = thread.dataset.pending, tries = 0;
  var timer = setInterval(function () {
    if (++tries > 90) { clearInterval(timer); return; }       // give up after 3 minutes
    fetch("/messages/pending?ids=" + encodeURIComponent(ids), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.pending.length < ids.split(",").length) { clearInterval(timer); location.reload(); }
      }).catch(function () {});
  }, 2000);
});
