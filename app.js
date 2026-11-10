/* Orderspo app layer (app v1): service worker + install guide.
   Loaded by index.html and order.html only, never by admin.html.
   Language follows <html lang>, which each page sets in applyLang(). */
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }

  var ua = navigator.userAgent || "";
  var isIOS = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var isAndroid = /Android/i.test(ua);
  var inApp = /Instagram|FBAN|FBAV|FB_IAB|FBIOS|musical_ly|BytedanceWebview|TikTok|Snapchat|Line\//i.test(ua);
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) || navigator.standalone === true;

  // Keeps English button names readable inside right-to-left text.
  function en(label) { return "\u2066" + label + "\u2069"; }

  var DISMISS_KEY = "spo_app_dismiss";
  var DISMISS_DAYS = 3;

  var T = {
    ku: {
      title: "Orderspo لەسەر مۆبایلەکەت",
      sub: "ئەپەکە دابەزێنە و بە یەک کلیک بەدواداچوونی داواکاریەکەت بکە",
      install: "دابەزاندن",
      how: "چۆن؟",
      later: "دواتر",
      steps: ["دوگمەی " + en("Share") + " دابگرە", en("Add to Home Screen") + " هەڵبژێرە", en("Add") + " دابگرە"],
      inTitle: "ئەم پەڕەیە لە ناو ئەپێکی تر کراوەتەوە",
      inSub: "بۆ دابەزاندنی ئەپ، ⋯ لە سەرەوە دابگرە و «Open in browser» هەڵبژێرە، یان لینکەکە کۆپی بکە و لە Safari یان Chrome بیکەرەوە.",
      copy: "کۆپیکردنی لینک",
      copied: "لینک کۆپی کرا",
      chrome: "کردنەوە لە Chrome",
      close: "داخستن"
    },
    ar: {
      title: "Orderspo على هاتفك",
      sub: "ثبّت التطبيق وتابع طلبك بضغطة واحدة",
      install: "تثبيت",
      how: "كيف؟",
      later: "لاحقاً",
      steps: ["اضغط زر المشاركة " + en("Share"), "اختر «إضافة إلى الشاشة الرئيسية» " + en("Add to Home Screen"), "اضغط «إضافة» " + en("Add")],
      inTitle: "هذه الصفحة مفتوحة داخل تطبيق آخر",
      inSub: "لتثبيت التطبيق اضغط ⋯ في الأعلى واختر «فتح في المتصفح»، أو انسخ الرابط وافتحه في Safari أو Chrome.",
      copy: "نسخ الرابط",
      copied: "تم نسخ الرابط",
      chrome: "فتح في Chrome",
      close: "إغلاق"
    }
  };

  function lang() { return document.documentElement.lang === "ar" ? "ar" : "ku"; }
  function t() { return T[lang()]; }

  function dismissedRecently() {
    try {
      var at = Number(localStorage.getItem(DISMISS_KEY) || 0);
      return at && Date.now() - at < DISMISS_DAYS * 864e5;
    } catch (e) { return false; }
  }
  function rememberDismiss() {
    try { localStorage.setItem(DISMISS_KEY, String(Date.now())); } catch (e) {}
  }

  var css = [
    "@media (display-mode: standalone){body{padding-top:calc(24px + env(safe-area-inset-top))}}",
    ".spo-app{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;justify-content:center;",
    "padding:0 12px calc(12px + env(safe-area-inset-bottom));pointer-events:none}",
    ".spo-app .card{pointer-events:auto;width:100%;max-width:420px;background:var(--ink-deep,#0F1F24);",
    "border:1px solid var(--ink-line,#2C4249);border-radius:14px;padding:14px;color:#fff;",
    "box-shadow:0 10px 30px rgba(0,0,0,.45);font-family:inherit}",
    ".spo-app .top{display:flex;gap:12px;align-items:center}",
    ".spo-app img{width:44px;height:44px;border-radius:10px;flex:none}",
    ".spo-app .tt{font-size:14px;font-weight:600;line-height:1.5}",
    ".spo-app .ss{font-size:12px;color:var(--mute,#8FA3A8);line-height:1.7;margin-top:2px}",
    ".spo-app ol{margin:12px 0 0;padding:0 20px 0 0;font-size:13px;line-height:2;color:#fff}",
    ".spo-app .btns{display:flex;gap:8px;margin-top:12px}",
    ".spo-app button,.spo-app a.b{flex:1;height:42px;border-radius:10px;border:0;font:600 14px inherit;",
    "font-family:inherit;cursor:pointer;display:flex;align-items:center;justify-content:center;text-decoration:none}",
    ".spo-app .pri{background:var(--teal,#2AADA5);color:#06282B}",
    ".spo-app .sec{background:transparent;color:var(--mute,#8FA3A8);border:1px solid var(--ink-line,#2C4249)}"
  ].join("");

  var root = null, mode = null, deferred = null, showSteps = false, copiedFlag = false;

  function chromeIntent() {
    return "intent://" + location.host + location.pathname + location.search +
      "#Intent;scheme=https;package=com.android.chrome;end";
  }

  function copyLink() {
    var link = location.origin + location.pathname;
    function done() { copiedFlag = true; render(); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(done, fallback);
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = link; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  function hide(remember) {
    if (remember) rememberDismiss();
    if (root) { root.remove(); root = null; }
    document.body.style.paddingBottom = "";
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function render() {
    if (!root) return;
    var s = t();
    root.innerHTML = "";
    var card = el("div", "card");
    var top = el("div", "top");
    var img = el("img"); img.src = "/icons/icon-192.png"; img.alt = "";
    var txt = el("div");
    txt.appendChild(el("div", "tt", mode === "inapp" ? s.inTitle : s.title));
    txt.appendChild(el("div", "ss", mode === "inapp" ? s.inSub : s.sub));
    top.appendChild(img); top.appendChild(txt); card.appendChild(top);

    if (mode === "ios" && showSteps) {
      var ol = el("ol");
      s.steps.forEach(function (step) { ol.appendChild(el("li", "", step)); });
      card.appendChild(ol);
    }

    var btns = el("div", "btns");
    if (mode === "prompt") {
      var ins = el("button", "pri", s.install);
      ins.addEventListener("click", function () {
        if (!deferred) return;
        deferred.prompt();
        deferred.userChoice.then(function () { deferred = null; hide(false); });
      });
      btns.appendChild(ins);
    } else if (mode === "ios" && !showSteps) {
      var how = el("button", "pri", s.how);
      how.addEventListener("click", function () { showSteps = true; render(); });
      btns.appendChild(how);
    } else if (mode === "inapp") {
      if (isAndroid) {
        var a = el("a", "b pri", s.chrome); a.href = chromeIntent();
        btns.appendChild(a);
      }
      var cp = el("button", isAndroid ? "sec" : "pri", copiedFlag ? s.copied : s.copy);
      cp.addEventListener("click", copyLink);
      btns.appendChild(cp);
    }
    var later = el("button", "sec", (mode === "ios" && showSteps) ? s.close : s.later);
    later.addEventListener("click", function () { hide(true); });
    btns.appendChild(later);
    card.appendChild(btns);
    root.appendChild(card);
    document.body.style.paddingBottom = (root.offsetHeight + 24) + "px";
  }

  function show(m) {
    if (standalone || dismissedRecently()) return;
    mode = m;
    if (!root) {
      var st = document.getElementById("spo-app-css");
      if (!st) { st = el("style"); st.id = "spo-app-css"; st.textContent = css; document.head.appendChild(st); }
      root = el("div", "spo-app");
      root.setAttribute("role", "dialog");
      document.body.appendChild(root);
    }
    render();
  }

  // The standalone padding rule is needed even when no card is shown.
  if (standalone) {
    var st0 = el("style"); st0.id = "spo-app-css"; st0.textContent = css; document.head.appendChild(st0);
  }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    if (!inApp) show("prompt");
  });
  window.addEventListener("appinstalled", function () { deferred = null; hide(false); });

  function start() {
    if (inApp) show("inapp");
    else if (isIOS) show("ios");
    // Android Chrome and desktop: the card appears once the browser fires beforeinstallprompt.
    new MutationObserver(function () { render(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
