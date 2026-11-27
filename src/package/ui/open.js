"use strict";
// Opens a tile's local or outside address, depending on how DSM was reached.
// Cattport's API answers only for someone signed in to DSM who may see the tile.
(function () {
  const VERSION = "__VERSION__";
  const key = new URLSearchParams(location.search).get("app") || "";

  function destination(link) {
    if (!window.CATTPORT_NETWORK.isPrivate(location.hostname) && link.external) return link.external;
    if (!link.port) return link.url;
    return link.protocol + "://" + location.hostname + ":" + link.port + "/";
  }

  function language() {
    const codes = ["en", "ro", "cs", "da", "de", "es", "fr", "hu", "it", "ja", "ko", "nb", "nl", "pl", "pt-BR", "pt-PT", "ru", "sv", "th", "tr", "zh-Hans", "zh-Hant"];
    const browser = (navigator.language || "en").toLowerCase();
    return codes.find((code) => code.toLowerCase() === browser) || codes.find((code) => code.split("-")[0] === browser.split("-")[0]) || "en";
  }

  function show(name) {
    const code = language();
    const say = () => {
      const texts = window.CATTPORT_TEXT || {};
      const text = (texts[code] && texts[code][name]) || (texts.en && texts.en[name]) || name;
      const box = document.createElement("p");
      box.textContent = text;
      document.body.replaceChildren(box);
    };
    const script = document.createElement("script");
    script.src = "text/" + code + ".js?v=" + VERSION;
    script.onload = say;
    script.onerror = say;
    document.head.append(script);
  }

  async function token() {
    try {
      const inherited = window.opener && window.opener.SYNO && window.opener.SYNO.SDS.Session.SynoToken;
      if (inherited) return decodeURIComponent(inherited);
    } catch (ignored) { /* another window or origin */ }
    const response = await fetch("/webman/login.cgi", { credentials: "same-origin", cache: "no-store" });
    const match = (await response.text()).match(/"SynoToken"\s*:\s*"([^"]+)"/);
    return match ? match[1] : "";
  }

  (async function open() {
    let result;
    try {
      const response = await fetch("api.cgi", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json", "X-SYNO-TOKEN": await token() },
        body: JSON.stringify({ action: "open", key: key }),
      });
      result = await response.json();
    } catch (error) {
      show("open.failed");
      return;
    }
    if (result.success) { location.replace(destination(result.data)); return; }
    show({ session: "open.signIn", not_allowed: "open.denied", app_gone: "open.gone" }[result.error] || "open.failed");
  })();
})();
