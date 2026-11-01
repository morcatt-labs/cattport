"use strict";
(function () {
  const ICON_SIZES = [16, 24, 32, 48, 64, 72, 256];
  const MAX_IMAGE = 3145728;
  const QUICK_ICONS = 3;
  const IMAGE_FILE = /\.(png|jpe?g|gif|webp|svg|ico)$/i;
  const VERSION = "__VERSION__";
  const $ = (id) => document.getElementById(id);
  const TEXT = window.CATTPORT_TEXT;
  const REPORT_URL = "https://github.com/morcatt-labs/cattport/issues/new";
  // DSM's language codes. A language shows once its translation exists; until then English does.
  const DSM_LANGUAGES = { enu: "en", chs: "zh-Hans", cht: "zh-Hant", csy: "cs", dan: "da", fre: "fr", ger: "de", hun: "hu", ita: "it", jpn: "ja", krn: "ko", nld: "nl", nor: "nb", plk: "pl", ptb: "pt-BR", ptg: "pt-PT", rus: "ru", spn: "es", sve: "sv", tha: "th", trk: "tr" };
  const LANGUAGE_NAMES = { en: "English", ro: "Română", "zh-Hans": "简体中文", "zh-Hant": "繁體中文", cs: "Čeština", da: "Dansk", fr: "Français", de: "Deutsch", hu: "Magyar", it: "Italiano", ja: "日本語", ko: "한국어", nl: "Nederlands", nb: "Norsk", pl: "Polski", "pt-BR": "Português (Brasil)", "pt-PT": "Português (Portugal)", ru: "Русский", es: "Español", sv: "Svenska", th: "ไทย", tr: "Türkçe" };

  let synoToken = "";
  let state = { apps: [], settings: {}, dirty: false };
  let lang = "en";
  let host = null;
  let containers = null;
  let dockerError = null;
  let dockerSelected = new Set();
  let draft = null;
  let suggestRun = 0;
  let suggestTimer = null;
  let statusTimer = null;
  let diagnostics = [];

  /* Text */

  function t(key, values) {
    let text = (TEXT[lang] && TEXT[lang][key]) || TEXT.en[key] || key;
    Object.entries(values || {}).forEach(([name, value]) => { text = text.replace("{" + name + "}", value); });
    return text;
  }

  // Only the language in use is downloaded.
  function loadLanguage() {
    const chosen = state.settings.language || "auto";
    const code = chosen === "auto" ? dsmLanguage() : chosen;
    if (TEXT[code] || !LANGUAGE_NAMES[code]) return Promise.resolve();
    return new Promise((resolve) => {
      const script = el("script", { src: "text/" + code + ".js", onload: resolve, onerror: resolve });
      document.head.append(script);
    });
  }

  function applyLanguage() {
    const chosen = state.settings.language || "auto";
    lang = chosen === "auto" ? dsmLanguage() : chosen;
    if (!TEXT[lang]) lang = "en";
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
    document.querySelectorAll("[data-i18n-label]").forEach((node) => { node.setAttribute("aria-label", t(node.dataset.i18nLabel)); });
    $("version").textContent = t("about.version", { v: state.version || VERSION });
    document.querySelectorAll(".report-link").forEach((link) => { link.href = reportUrl(); });
  }

  // DSM's own language first; "def" means DSM itself follows the browser.
  function dsmLanguage() {
    try {
      const value = window.top._S && window.top._S("lang");
      if (DSM_LANGUAGES[value]) return DSM_LANGUAGES[value];
    } catch (ignored) { /* another origin */ }
    const browser = navigator.language || "en";
    return Object.keys(LANGUAGE_NAMES).find((code) => code.toLowerCase() === browser.toLowerCase())
      || Object.keys(LANGUAGE_NAMES).find((code) => code.split("-")[0] === browser.split("-")[0].toLowerCase()) || "en";
  }

  function reportUrl() {
    return REPORT_URL + "?" + new URLSearchParams({ body: "Cattport " + (state.version || VERSION) + "\n\n" });
  }

  function applyTheme() {
    const chosen = state.settings.theme || "auto";
    const dark = chosen === "dark" || (chosen === "auto" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  }

  /* Calls */

  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    (children || []).forEach((child) => node.append(child));
    return node;
  }

  function errorText(error) {
    if (error.message === "session") return t("blocked.sessionText");
    return TEXT.en["error." + error.message] ? t("error." + error.message) : t("error.internal");
  }

  async function loadToken() {
    try {
      const inherited = window.top.SYNO.SDS.Session.SynoToken;
      if (inherited) { synoToken = decodeURIComponent(inherited); return; }
    } catch (ignored) { /* opened on its own */ }
    const response = await fetch("/webman/login.cgi", { credentials: "same-origin" });
    const match = (await response.text()).match(/"SynoToken"\s*:\s*"([^"]+)"/);
    if (match) synoToken = match[1];
  }

  async function api(action, payload) {
    const response = await fetch("api.cgi", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-SYNO-TOKEN": synoToken },
      body: JSON.stringify(Object.assign({ action: action }, payload || {})),
    });
    let result;
    try { result = await response.json(); } catch (ignored) { throw new Error("internal"); }
    if (!result.success) throw new Error(result.error || "internal");
    return result.data;
  }

  async function webapi(params) {
    const response = await fetch("/webapi/entry.cgi", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-SYNO-TOKEN": synoToken },
      body: new URLSearchParams(params),
    });
    const result = await response.json();
    if (!result.success) {
      const failure = new Error("dsm");
      failure.code = result.error && result.error.code;
      throw failure;
    }
    return result.data;
  }

  /* Publishing: the host in the DSM page does the work, so it survives this window closing. */

  function findHost() {
    try {
      const found = window.top.labs && window.top.labs.morcatt && window.top.labs.morcatt.cattport;
      if (found && found.version === VERSION && found.saveToDsm) return found;
      if (found) showStatus(t("status.outdated"), reloadAction(), true);
    } catch (ignored) { /* opened on its own */ }
    return null;
  }

  function reloadAction() {
    return { label: t("status.reloadNow"), run: () => window.top.location.reload() };
  }

  function onSave(status) {
    if (status === "saving") return showStatus(t("status.publishing"), null, true);
    if (status === "saved") return showStatus(t("status.published"));
    if (status === "reload") return showStatus(t("status.reload"), reloadAction(), true);
    renderSaveBar();
    return showStatus(t("status.failed"), { label: t("status.retry"), run: saveToDsm }, true);
  }

  function renderSaveBar() {
    const count = state.pending || 0;
    $("save-bar").hidden = !count;
    $("unsaved").textContent = t("apps.unsaved", { n: count });
  }

  function saveToDsm() {
    $("save-bar").hidden = true;
    if (host) { host.saveToDsm(); return; }
    // Opened outside DSM: nothing here can restart the package.
    api("publish").then(() => showStatus(t("status.reload"))).catch(() => { renderSaveBar(); showStatus(t("status.failed"), null, true); });
  }

  function showStatus(text, action, sticky) {
    clearTimeout(statusTimer);
    $("status").hidden = false;
    $("status-text").textContent = text;
    const button = $("status-action");
    button.hidden = !action;
    button.textContent = action ? action.label : "";
    button.onclick = action ? action.run : null;
    if (!sticky) statusTimer = setTimeout(() => { $("status").hidden = true; }, 2400);
  }

  /* Apps */

  // The published file costs the NAS nothing; only unpublished icons go through the API.
  function iconFor(app, image) {
    image.onerror = async () => {
      image.onerror = null;
      try { image.src = (await api("icon", { key: app.key })).dataUrl; } catch (ignored) { /* the tile stays blank */ }
    };
    image.src = "images/" + encodeURIComponent(app.key) + "." + app.icon_rev + "_256.png";
  }

  const onDomain = () => !window.CATTPORT_NETWORK.isPrivate(location.hostname);

  function renderDomainWarning() {
    $("domain-warn").hidden = !onDomain() || !!$("f-external").value.trim();
  }

  function renderApps() {
    renderSaveBar();
    const query = $("search").value.trim().toLowerCase();
    const apps = state.apps.filter((app) => app.name.toLowerCase().includes(query));
    const list = $("apps");
    list.replaceChildren();
    $("empty").hidden = state.apps.length > 0;
    $("no-results").hidden = !(state.apps.length && !apps.length);
    apps.forEach((app) => {
      const image = el("img", { alt: "" });
      iconFor(app, image);
      const noDomain = !app.hidden && !app.external && onDomain();
      const note = app.hidden ? t("apps.hidden") : noDomain ? t("apps.noDomain") : t(app.allUsers ? "apps.everyone" : "apps.admins");
      const tile = el("button", { className: "tile" + (app.hidden ? " off" : ""), type: "button", onclick: () => openEditor(app) }, [
        image, el("b", { textContent: app.name }), el("small", { textContent: note }),
      ]);
      list.append(el("li", {}, [tile]));
    });
  }

  function showScreen(name) {
    document.querySelectorAll(".screen").forEach((node) => { node.hidden = node.id !== "screen-" + name; });
    document.querySelectorAll(".nav").forEach((node) => {
      if (node.dataset.screen === name) node.setAttribute("aria-current", "page");
      else node.removeAttribute("aria-current");
    });
    if (name === "diagnostics") runDiagnostics($("checks"));
  }

  function renderSettings() {
    const pressed = (group, value) => $(group).querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.value === value)));
    pressed("theme", state.settings.theme);
    const select = $("language");
    select.replaceChildren(el("option", { value: "auto", textContent: t("settings.languageAuto") }),
      ...Object.keys(LANGUAGE_NAMES).map((code) => el("option", { value: code, textContent: LANGUAGE_NAMES[code] })));
    select.value = state.settings.language || "auto";
    $("online").setAttribute("aria-checked", String(state.settings.online === true));
    $("welcome-online").setAttribute("aria-checked", String(state.settings.online === true));
  }

  async function saveSettings(change) {
    state = await api("save_settings", { settings: change });
    await loadLanguage();
    applyTheme();
    applyLanguage();
    renderSettings();
    renderApps();
  }

  /* Docker: through the DSM API and the administrator's session; the package has no Docker socket access. */

  function portsOf(container) {
    const ports = new Set();
    const add = (value) => { const port = parseInt(value, 10); if (port > 0 && port < 65536) ports.add(port); };
    [container.port_bindings, container.Ports, container.ports].filter(Array.isArray).forEach((items) => items.forEach((item) => {
      if (item && typeof item === "object") add(item.host_port || item.PublicPort);
    }));
    const details = container.details || {};
    const network = details.NetworkSettings && details.NetworkSettings.Ports;
    Object.values(network || {}).forEach((items) => (items || []).forEach((item) => add(item.HostPort)));
    return Array.from(ports).sort((a, b) => a - b);
  }

  function iconLabels(container) {
    const labels = (container.details && container.details.Config && container.details.Config.Labels) || container.labels || container.Labels || {};
    const found = {};
    Object.keys(labels).forEach((key) => {
      if (/(^|[._-])(icon|logo)($|[._-])/i.test(key)) found[key] = labels[key];
    });
    return found;
  }

  function friendlyName(name) {
    const clean = String(name || "").replace(/^\//, "").replace(/[_-](server|web|app|main)$/i, "").replace(/[_-]+/g, " ");
    return clean.replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function serviceUrl(port) {
    return "http://" + (state.lanIp || location.hostname) + ":" + port;
  }

  // Tells apart: missing, stopped, or an API DSM changed. Reads the package list first.
  async function dockerAvailability() {
    let packageStatus = null;
    try {
      const data = await webapi({ api: "SYNO.Core.Package", version: "1", method: "list", additional: JSON.stringify(["status"]) });
      const item = (data.packages || []).find((pkg) => pkg.id === "ContainerManager" || pkg.id === "Docker");
      if (!item) return "missing";
      const status = item.additional && item.additional.status;
      packageStatus = status;
      if (status && status !== "running") return "stopped";
    } catch (ignored) { /* keep going with the Docker API */ }
    try {
      await webapi({ api: "SYNO.Docker.Container", version: "1", method: "list", limit: "1", offset: "0", type: "all" });
      return "running";
    } catch (error) {
      return packageStatus === "running" || packageStatus === null ? "changed" : "stopped";
    }
  }

  async function loadContainers(force) {
    if (containers && !force) return containers;
    const data = await webapi({ api: "SYNO.Docker.Container", version: "1", method: "list", limit: "-1", offset: "0", type: "all" });
    // A few at a time, so a NAS with hundreds of containers isn't flooded.
    const list = data.containers || [];
    const results = new Array(list.length);
    let next = 0;
    const worker = async () => {
      while (next < list.length) {
        const index = next++;
        const merged = Object.assign({}, list[index]);
        try {
          const detail = await webapi({ api: "SYNO.Docker.Container", version: "1", method: "get", name: merged.name });
          Object.assign(merged, detail.profile || {}, { details: detail.details || {} });
          merged.name = list[index].name;
          merged.status = list[index].status;
        } catch (ignored) { /* the list data stays */ }
        merged.webPorts = portsOf(merged);
        results[index] = merged;
      }
    };
    await Promise.all(Array.from({ length: 6 }, worker));
    containers = results;
    return containers;
  }

  function dockerMessage(kind) {
    const box = $("docker-message");
    const titles = { missing: "docker.missingTitle", stopped: "docker.offTitle", changed: "docker.brokenTitle" };
    const texts = { missing: "docker.missingText", stopped: "docker.offText", changed: "docker.brokenText" };
    const buttons = [];
    if (kind !== "changed" && host) buttons.push(el("button", { className: "btn primary", type: "button", textContent: t("docker.openCenter"), onclick: () => { $("docker-sheet").close(); host.openPackageCenter(); } }));
    if (kind !== "missing") buttons.push(el("button", { className: "btn soft", type: "button", textContent: t("docker.check"), onclick: openDocker }));
    buttons.push(el("button", { className: "btn soft", type: "button", textContent: t("add.link"), onclick: () => { $("docker-sheet").close(); openEditor(null); } }));
    box.replaceChildren(el("h3", { textContent: t(titles[kind]) }), el("p", { textContent: t(texts[kind]) }), el("div", { className: "row" }, buttons));
    box.hidden = false;
    $("docker-body").hidden = true;
  }

  function isAdded(container) {
    const name = friendlyName(container.name).toLowerCase();
    return state.apps.some((app) => app.name.toLowerCase() === name
      || container.webPorts.some((port) => { try { return Number(new URL(app.url).port) === port; } catch (ignored) { return false; } }));
  }

  function renderDocker() {
    const showAll = $("docker-all").checked;
    const list = $("docker-list");
    const filter = $("docker-filter").value.trim().toLowerCase();
    const rows = (containers || []).filter((item) => (showAll || item.webPorts.length) && item.name.toLowerCase().includes(filter)).sort((a, b) => a.name.localeCompare(b.name));
    list.replaceChildren();
    if (!rows.length) list.append(el("li", { className: "note", textContent: t("docker.none") }));
    rows.forEach((item) => {
      const added = isAdded(item);
      const running = item.status === "running";
      const box = el("input", { type: "checkbox", checked: added || dockerSelected.has(item.name), disabled: added });
      box.addEventListener("change", () => {
        if (box.checked) dockerSelected.add(item.name); else dockerSelected.delete(item.name);
        renderDockerButton();
      });
      const ports = item.webPorts.length ? t("docker.port", { p: item.webPorts.join(", ") }) : t("docker.noPort");
      const note = added ? t("docker.already") : t(running ? "docker.running" : "docker.stopped") + " · " + ports;
      list.append(el("li", {}, [el("label", { className: added ? "off" : "" }, [box, el("span", { className: "grow" }, [el("b", { textContent: friendlyName(item.name) }), el("small", { textContent: note })])])]));
    });
    renderDockerButton();
  }

  function renderDockerButton() {
    const count = dockerSelected.size;
    const button = $("docker-add");
    button.disabled = !count;
    button.textContent = count > 1 ? t("docker.addMany", { n: count }) : t("docker.add");
  }

  async function openDocker() {
    closeAll();
    dockerSelected = new Set();
    $("docker-filter").value = "";
    $("docker-message").hidden = true;
    $("docker-body").hidden = false;
    $("docker-list").replaceChildren(el("li", { className: "note", textContent: t("docker.loading") }));
    renderDockerButton();
    $("docker-sheet").showModal();
    const availability = await dockerAvailability();
    if (availability !== "running") { dockerMessage(availability); return; }
    try {
      await loadContainers(true);
      renderDocker();
    } catch (error) {
      dockerMessage("changed");
    }
  }

  // Containers with one port and a usable icon go straight in; the rest open in the Editor.
  async function addSelected() {
    const button = $("docker-add");
    button.disabled = true;
    const chosen = containers.filter((item) => dockerSelected.has(item.name));
    let needsEditor = null;
    for (const item of chosen) {
      const icon = item.webPorts.length ? await firstDockerIcon(item) : null;
      const name = friendlyName(item.name);
      const dataUrl = icon || (await markIcon(name));
      if (!item.webPorts.length) { needsEditor = item; continue; }
      try {
        state = await api("save_app", { key: null, app: { name: name, url: serviceUrl(item.webPorts[0]), allUsers: false, follow: true }, icons: await rasterize(dataUrl) });
      } catch (error) {
        showStatus(errorText(error), null, true);
      }
    }
    $("docker-sheet").close();
    renderApps();
    if (needsEditor) openEditor(null, needsEditor);
  }

  async function firstDockerIcon(container) {
    try {
      const data = await api("docker_icons", { ports: container.webPorts, labels: iconLabels(container) });
      if (data.results.length) return data.results[0].dataUrl;
    } catch (ignored) { /* a Docker icon is optional */ }
    try {
      const data = await api("internet_icons", { name: friendlyName(container.name) });
      if (data.results.length) return data.results[0].dataUrl;
    } catch (ignored) { /* so is an online one */ }
    return null;
  }

  // The fallback icon: the first letter on a colour derived from the name.
  function markIcon(name) {
    const palette = ["#0A5BD3", "#6B3FC9", "#B3261E", "#0B7A5A", "#B4540A", "#22577A"];
    let hash = 0;
    for (const char of name) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 256;
    const context = canvas.getContext("2d");
    context.fillStyle = palette[hash % palette.length];
    context.beginPath();
    context.roundRect(0, 0, 256, 256, 58);
    context.fill();
    context.fillStyle = "#FFFFFF";
    context.font = "600 128px -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI', sans-serif";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText((name.trim()[0] || "?").toUpperCase(), 128, 136);
    return Promise.resolve(canvas.toDataURL("image/png"));
  }

  /* Editor */

  function setPreview(dataUrl) {
    const image = $("preview-img");
    image.hidden = !dataUrl;
    $("preview-mark").hidden = !!dataUrl;
    if (dataUrl) image.src = dataUrl;
    $("preview-mark").textContent = ($("f-name").value.trim()[0] || "?").toUpperCase();
    document.querySelectorAll(".icons button").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.url === dataUrl)));
  }

  function chooseIcon(dataUrl) {
    draft.icon = dataUrl;
    draft.iconChanged = true;
    draft.touched = true;
    setPreview(dataUrl);
  }

  function fingerprint(dataUrl) {
    return new Promise((resolve) => {
      const image = new Image();
      image.onerror = () => resolve(null);
      image.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 16;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(image, 0, 0, 16, 16);
        resolve(context.getImageData(0, 0, 16, 16).data);
      };
      image.src = dataUrl;
    });
  }

  function looksSame(a, b) {
    let difference = 0;
    for (let index = 0; index < a.length; index++) difference += Math.abs(a[index] - b[index]);
    return difference / a.length < 12;
  }

  function suggestionButton(result, onChosen) {
    const button = el("button", { type: "button", title: result.label, onclick: () => { chooseIcon(result.dataUrl); if (onChosen) onChosen(); } }, [el("img", { src: result.dataUrl, alt: result.label })]);
    button.dataset.url = result.dataUrl;
    button.setAttribute("aria-pressed", String(draft.icon === result.dataUrl));
    return button;
  }

  // The editor keeps a short row for a quick change; the pencil's panel has them all.
  async function addSuggestions(results) {
    const quick = $("suggestions");
    const all = $("icon-suggestions");
    const current = draft;
    for (const result of results) {
      const print = await fingerprint(result.dataUrl);
      if (draft !== current) return;
      // The same logo often comes back as PNG and SVG, from several providers.
      if (print && current.prints.some((known) => looksSame(known, print))) continue;
      if (print) current.prints.push(print);
      all.append(suggestionButton(result, () => $("icon-sheet").close()));
      if (quick.children.length < QUICK_ICONS) quick.append(suggestionButton(result));
    }
    if (!draft.icon && results.length) {
      draft.icon = results[0].dataUrl;
      draft.iconChanged = true;
      setPreview(draft.icon);
    }
  }

  function matchContainer(name) {
    const needle = name.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (needle.length < 2 || !containers) return null;
    const found = containers.filter((item) => item.name.toLowerCase().replace(/[^a-z0-9]/g, "").includes(needle));
    found.sort((a, b) => b.webPorts.length - a.webPorts.length || a.name.length - b.name.length);
    return found[0] || null;
  }

  // Docker first, then the internet, as promised in the app map.
  async function suggest() {
    const run = ++suggestRun;
    const name = $("f-name").value.trim();
    const status = $("suggest-status");
    if (!name) { status.textContent = ""; return; }
    status.textContent = t("edit.searching");
    if (!containers) { try { await loadContainers(); } catch (ignored) { /* no Docker: keep going */ } }
    if (run !== suggestRun) return;
    const match = draft.container || matchContainer(name);
    let found = 0;
    if (match && (match.webPorts.length || Object.keys(iconLabels(match)).length)) {
      try {
        const data = await api("docker_icons", { ports: match.webPorts, labels: iconLabels(match) });
        if (run !== suggestRun) return;
        found += data.results.length;
        await addSuggestions(data.results);
      } catch (ignored) { /* optional */ }
    }
    let note = "";
    try {
      const data = await api("internet_icons", { name: name, url: $("f-url").value.trim() });
      if (run !== suggestRun) return;
      found += data.results.length;
      await addSuggestions(data.results);
      if (data.disabled) note = t("edit.onlineOff");
    } catch (ignored) {
      note = t("edit.offline");
    }
    if (run !== suggestRun) return;
    if (!draft.icon) {
      draft.icon = await markIcon(name);
      draft.iconChanged = true;
      setPreview(draft.icon);
    }
    status.textContent = note || (found ? "" : t("edit.noIcons"));
  }

  function scheduleSuggest() {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(suggest, 500);
  }

  function setAudience(value) {
    draft.everyone = value === "everyone";
    $("audience").querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.value === value)));
  }

  function iconError(text) {
    $("icon-error").textContent = text || "";
    $("icon-error").hidden = !text;
  }

  function editError(text) {
    $("edit-error").textContent = text || "";
    $("edit-error").hidden = !text;
  }

  async function openEditor(app, container) {
    closeAll();
    suggestRun++;
    const url = app ? app.url : container && container.webPorts.length ? serviceUrl(container.webPorts[0]) : "";
    draft = { key: app ? app.key : null, container: container || null, icon: null, iconChanged: false, touched: false, follow: app ? !!app.follow : !!container, originalUrl: url, everyone: false, prints: [] };
    $("edit-title").textContent = t(app ? "edit.titleEdit" : "edit.titleNew");
    $("f-name").value = app ? app.name : container ? friendlyName(container.name) : "";
    $("f-url").value = url;
    $("f-external").value = app ? app.external || "" : "";
    $("f-hidden").checked = !!(app && app.hidden);
    renderDomainWarning();
    $("icon-url").value = "";
    $("delete-app").hidden = !app;
    $("open-app").hidden = !app;
    if (app) $("open-app").href = app.url;
    $("suggestions").replaceChildren();
    $("icon-suggestions").replaceChildren();
    iconError("");
    $("suggest-status").textContent = "";
    $("edit-warn").hidden = true;
    editError("");
    setAudience(app && app.allUsers ? "everyone" : "admins");
    const ports = $("ports");
    ports.replaceChildren();
    ((container && container.webPorts.length > 1 && container.webPorts) || []).forEach((port) => {
      ports.append(el("button", { type: "button", textContent: t("edit.port", { p: port }), onclick: () => { $("f-url").value = serviceUrl(port); } }));
    });
    setPreview(null);
    $("edit-sheet").showModal();
    $("f-name").focus();
    if (app) {
      draft.icon = (await api("icon", { key: app.key })).dataUrl;
      setPreview(draft.icon);
    }
    if (app || container) suggest();
  }

  function rasterize(dataUrl) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onerror = () => reject(new Error("bad_image"));
      image.onload = () => {
        const width = image.naturalWidth || 256;
        const height = image.naturalHeight || 256;
        const icons = {};
        ICON_SIZES.forEach((size) => {
          const canvas = document.createElement("canvas");
          canvas.width = canvas.height = size;
          const context = canvas.getContext("2d");
          context.imageSmoothingQuality = "high";
          // The same transparent margin as DSM's own icons (about 88%); at 16 px every pixel counts.
          const area = size < 24 ? size : Math.round(size * 0.88);
          const scale = Math.min(area / width, area / height);
          context.drawImage(image, (size - width * scale) / 2, (size - height * scale) / 2, width * scale, height * scale);
          icons[size] = canvas.toDataURL("image/png").split(",")[1];
        });
        resolve({ icons: icons, small: Math.max(width, height) < 64 && !/^data:image\/svg/.test(dataUrl) });
      };
      image.src = dataUrl;
    }).then((result) => {
      lastRasterSmall = result.small;
      return result.icons;
    });
  }
  let lastRasterSmall = false;

  function readAsDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("bad_image"));
      reader.readAsDataURL(blob);
    });
  }

  async function useImage(dataUrl) {
    try {
      await rasterize(dataUrl);
    } catch (error) {
      iconError(t("edit.badFile"));
      return;
    }
    iconError("");
    chooseIcon(dataUrl);
    if ($("icon-sheet").open) $("icon-sheet").close();
    $("edit-warn").textContent = lastRasterSmall ? t("edit.lowRes") : "";
    $("edit-warn").hidden = !lastRasterSmall;
  }

  // Fills in a missing scheme. Anything already naming one, even a bad one, is left for the server to refuse.
  function withScheme(value, scheme) {
    const address = value.trim();
    if (!address || /^[a-z][a-z0-9+.-]*:(?!\d)/i.test(address)) return address;
    return scheme + "://" + address;
  }

  function normalizeField(field, scheme) {
    field.value = withScheme(field.value, scheme);
  }

  // Warns only: a service that is down right now can still be saved.
  async function checkAddress() {
    const url = $("f-url").value.trim();
    $("edit-warn").hidden = true;
    if (!/^https?:\/\/./.test(url)) return;
    let reachable = true;
    try { reachable = (await api("check_address", { url: url })).reachable; } catch (ignored) { /* no warning without an answer */ }
    if (!reachable && $("f-url").value.trim() === url) {
      $("edit-warn").textContent = t("edit.unreachable");
      $("edit-warn").hidden = false;
    }
  }

  async function saveEditor(event) {
    event.preventDefault();
    editError("");
    const name = $("f-name").value.trim();
    if (!name) { editError(t("edit.needName")); return; }
    const save = $("save-app");
    save.disabled = true;
    try {
      if (!draft.icon) { draft.icon = await markIcon(name); draft.iconChanged = true; }
      normalizeField($("f-url"), "http");
      normalizeField($("f-external"), "https");
      // A typed address on another host stays exactly as typed.
      const url = $("f-url").value;
      let follow = false;
      try { follow = draft.follow && (url === draft.originalUrl || new URL(url).hostname === location.hostname); } catch (ignored) { /* the server reports the bad address */ }
      state = await api("save_app", {
        key: draft.key,
        app: { name: name, url: url, external: $("f-external").value.trim(), allUsers: draft.everyone, hidden: $("f-hidden").checked, follow: follow },
        icons: draft.iconChanged ? await rasterize(draft.icon) : null,
      });
      suggestRun++;
      draft.touched = false;
      $("edit-sheet").close();
      renderApps();
    } catch (error) {
      editError(errorText(error));
    } finally {
      save.disabled = false;
    }
  }

  async function deleteApp() {
    try {
      state = await api("delete_app", { key: draft.key });
    } catch (error) {
      editError(errorText(error));
      return;
    }
    draft.touched = false;
    $("edit-sheet").close();
    renderApps();
    showStatus(t("status.deleted"), { label: t("status.undo"), run: undoDelete }, true);
    statusTimer = setTimeout(() => { $("status").hidden = true; }, 5000);
  }

  async function undoDelete() {
    try {
      state = await api("undo_delete");
      renderApps();
      $("status").hidden = true;
    } catch (error) {
      showStatus(errorText(error), null, true);
    }
  }

  /* File Station */

  const LAST_FOLDER = "cattport.lastFolder";
  let fsRun = 0;
  let fsFolder = "";
  let fsShares = [];
  let fsSearchTimer = null;

  function thumbnail(path) {
    const query = new URLSearchParams({ api: "SYNO.FileStation.Thumb", version: "2", method: "get", path: JSON.stringify(path), size: "small", SynoToken: synoToken });
    return "/webapi/entry.cgi?" + query;
  }

  function fsNote(text) {
    $("fs-list").replaceChildren(el("p", { className: "note", textContent: text }));
  }

  function fsItem(row) {
    const label = el("span", { textContent: row.name, title: row.name });
    if (row.isdir) return el("button", { className: "pick", type: "button", onclick: () => listFolder(row.path) }, [el("i", { className: "folder" }), label]);
    const image = el("img", { alt: "", loading: "lazy", src: thumbnail(row.path) });
    image.onerror = () => { image.replaceWith(el("i")); };
    return el("button", { className: "pick", type: "button", onclick: () => pickFile(row.path) }, [image, label]);
  }

  function fsShow(rows) {
    const shown = rows.filter((row) => row.isdir || IMAGE_FILE.test(row.name));
    if (!shown.length) { fsNote(t("files.none")); return; }
    $("fs-list").replaceChildren(...shown.map(fsItem));
  }

  function renderCrumbs(path) {
    const box = $("fs-path");
    const crumbs = [el("button", { type: "button", textContent: t("files.shared"), onclick: () => listFolder("") })];
    let current = "";
    path.split("/").filter(Boolean).forEach((part) => {
      current += "/" + part;
      const target = current;
      crumbs.push(el("button", { type: "button", textContent: part, onclick: () => listFolder(target) }));
    });
    box.replaceChildren(...crumbs);
  }

  async function listFolder(path) {
    const run = ++fsRun;
    fsFolder = path;
    $("fs-search").value = "";
    renderCrumbs(path);
    fsNote(t("files.loading"));
    try {
      let rows;
      if (path) {
        rows = (await webapi({ api: "SYNO.FileStation.List", version: "2", method: "list", folder_path: path, sort_by: "name", limit: "2000", offset: "0" })).files;
      } else {
        fsShares = (await webapi({ api: "SYNO.FileStation.List", version: "2", method: "list_share", sort_by: "name", limit: "1000", offset: "0" })).shares;
        rows = fsShares.map((share) => Object.assign(share, { isdir: true }));
      }
      if (run !== fsRun) return;
      // Folders first, so a folder full of pictures doesn't bury the way onward.
      rows.sort((a, b) => Number(!!b.isdir) - Number(!!a.isdir));
      fsShow(rows);
      try { localStorage.setItem(LAST_FOLDER, path); } catch (ignored) { /* a convenience only */ }
    } catch (error) {
      if (run !== fsRun) return;
      if (path) listFolder(""); else fsNote(errorText(error));
    }
  }

  // Looks through this folder and everything below it; on the top level, through every share.
  async function searchFiles(pattern) {
    const run = ++fsRun;
    fsNote(t("files.loading"));
    let taskid = null;
    try {
      if (!fsFolder && !fsShares.length) {
        fsShares = (await webapi({ api: "SYNO.FileStation.List", version: "2", method: "list_share", sort_by: "name", limit: "1000", offset: "0" })).shares;
      }
      const roots = fsFolder ? [fsFolder] : fsShares.map((share) => share.path);
      taskid = (await webapi({ api: "SYNO.FileStation.Search", version: "2", method: "start", folder_path: JSON.stringify(roots), recursive: "true", pattern: JSON.stringify(pattern), extension: JSON.stringify("png,jpg,jpeg,gif,webp,svg,ico") })).taskid;
      for (let attempt = 0; attempt < 40; attempt++) {
        const data = await webapi({ api: "SYNO.FileStation.Search", version: "2", method: "list", taskid: JSON.stringify(taskid), offset: "0", limit: "200" });
        if (run !== fsRun) return;
        fsShow(data.files || []);
        if (data.finished) return;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    } catch (error) {
      if (run === fsRun) fsNote(errorText(error));
    } finally {
      if (taskid) webapi({ api: "SYNO.FileStation.Search", version: "2", method: "clean", taskid: JSON.stringify(taskid) }).catch(() => {});
    }
  }

  function openFiles() {
    let last = "";
    try { last = localStorage.getItem(LAST_FOLDER) || ""; } catch (ignored) { /* start at the top */ }
    $("fs-sheet").showModal();
    listFolder(last);
  }

  async function pickFile(path) {
    const query = new URLSearchParams({ api: "SYNO.FileStation.Download", version: "2", method: "download", mode: "download", path: JSON.stringify([path]), SynoToken: synoToken });
    const note = (text) => $("fs-list").prepend(el("p", { className: "note", textContent: text }));
    try {
      const response = await fetch("/webapi/entry.cgi?" + query, { credentials: "same-origin" });
      if (!response.ok) throw new Error("dsm");
      const blob = await response.blob();
      if (blob.size > MAX_IMAGE) { note(t("edit.tooLarge")); return; }
      const typed = path.toLowerCase().endsWith(".svg") ? new Blob([blob], { type: "image/svg+xml" }) : blob;
      const dataUrl = await readAsDataUrl(typed);
      await rasterize(dataUrl);
      $("fs-sheet").close();
      await useImage(dataUrl);
    } catch (error) {
      note(error.message === "bad_image" ? t("edit.badFile") : errorText(error));
    }
  }

  /* Diagnostics */

  async function runDiagnostics(box) {
    const show = (rows) => {
      box.replaceChildren(...rows.map((row) => el("div", { className: "check-line" }, [
        el("span", { className: "dot " + row.level }), el("b", { textContent: row.name }), el("span", { textContent: row.value }),
      ])));
    };
    const pending = ["diag.dsm", "diag.publishing", "diag.docker", "diag.internet", "diag.data"].map((key) => ({ name: t(key), value: t("diag.checking"), level: "" }));
    show(pending);
    let dsm = "?";
    try {
      const info = await webapi({ api: "SYNO.Core.System", version: "1", method: "info" });
      dsm = String(info.firmware_ver || "?").replace(/^DSM\s*/, "");
    } catch (ignored) { /* keep the unknown mark */ }
    let reloadable = false;
    try { reloadable = typeof window.top.SYNO.SDS.reloadJSConfig === "function"; } catch (ignored) { /* opened on its own */ }
    const docker = await dockerAvailability().catch(() => "changed");
    const dockerRow = { running: ["diag.running", "ok"], missing: ["diag.missing", "warn"], stopped: ["diag.stopped", "warn"], changed: ["diag.changed", "bad"] }[docker];
    let internet = false;
    if (state.settings.online === true) {
      try { internet = (await api("check_address", { url: "https://cdn.jsdelivr.net/" })).reachable; } catch (ignored) { /* not connected */ }
    }
    diagnostics = [
      { id: "dsm", name: t("diag.dsm"), value: String(dsm), level: "ok" },
      { id: "publishing", name: t("diag.publishing"), value: t(reloadable ? "diag.ok" : host ? "diag.limited" : "diag.needsReload"), level: reloadable ? "ok" : "warn" },
      { id: "docker", name: t("diag.docker"), value: t(dockerRow[0]), level: dockerRow[1] },
      { id: "internet", name: t("diag.internet"), value: state.settings.online === true ? t(internet ? "diag.connected" : "diag.offline") : t("edit.onlineOff"), level: state.settings.online === true && !internet ? "warn" : "ok" },
      { id: "data", name: t("diag.data"), value: t("diag.healthy", { n: state.apps.length }), level: "ok" },
    ];
    show(diagnostics);
  }

  function copyReport() {
    const lines = ["Cattport " + (state.version || VERSION)].concat(diagnostics.map((row) => row.id + ": " + row.level + " (" + row.value + ")"));
    const area = el("textarea", { value: lines.join("\n") });
    document.body.append(area);
    area.select();
    // navigator.clipboard needs HTTPS, which many DSM addresses don't use.
    document.execCommand("copy");
    area.remove();
    showStatus(t("diag.copied"));
  }

  /* Backup */

  async function exportBackup() {
    try {
      const data = await api("export");
      const link = el("a", { href: URL.createObjectURL(new Blob([JSON.stringify(data)], { type: "application/json" })), download: "cattport-backup.json" });
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      showStatus(errorText(error), null, true);
    }
  }

  async function importBackup(file) {
    try {
      const backup = JSON.parse(await file.text());
      state = await api("import", { backup: backup });
      renderApps();
      showStatus(t("status.imported"));
    } catch (error) {
      showStatus(error instanceof SyntaxError ? t("error.invalid_backup") : errorText(error), null, true);
    }
  }

  /* Wiring */

  function closeAll() {
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  }

  function finishWelcome() {
    if (!state.settings.welcomed) saveSettings({ welcomed: true }).catch(() => {});
    $("welcome").close();
  }

  function closeSheet(sheet) {
    if (sheet.id === "edit-sheet" && draft && draft.touched && !window.confirm(t("edit.discard"))) return;
    suggestRun++;
    sheet.close();
  }

  function wire() {
    document.querySelectorAll(".nav").forEach((node) => node.addEventListener("click", () => showScreen(node.dataset.screen)));
    document.querySelectorAll("[data-close]").forEach((node) => node.addEventListener("click", () => closeSheet(node.closest("dialog"))));
    document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("cancel", (event) => {
      // A file picker's own cancel bubbles up here too; only Esc on the dialog should close it.
      if (event.target !== dialog) return;
      event.preventDefault();
      if (dialog.id === "welcome") finishWelcome(); else closeSheet(dialog);
    }));
    document.querySelectorAll("[data-open]").forEach((node) => node.addEventListener("click", () => (node.dataset.open === "docker" ? openDocker() : openEditor(null))));
    document.querySelectorAll("[data-welcome]").forEach((node) => node.addEventListener("click", () => {
      finishWelcome();
      if (node.dataset.welcome === "docker") openDocker(); else openEditor(null);
    }));
    $("save-dsm").addEventListener("click", saveToDsm);
    $("add").addEventListener("click", () => $("add-sheet").showModal());
    $("search").addEventListener("input", renderApps);
    $("docker-all").addEventListener("change", renderDocker);
    $("docker-filter").addEventListener("input", renderDocker);
    $("docker-add").addEventListener("click", addSelected);
    $("f-name").addEventListener("input", () => { draft.touched = true; setPreview(draft.icon); scheduleSuggest(); });
    $("f-url").addEventListener("input", () => { draft.touched = true; });
    $("f-external").addEventListener("input", () => { draft.touched = true; renderDomainWarning(); });
    $("f-url").addEventListener("change", () => { normalizeField($("f-url"), "http"); checkAddress(); scheduleSuggest(); });
    $("f-external").addEventListener("change", () => normalizeField($("f-external"), "https"));
    $("icon-url").addEventListener("change", () => normalizeField($("icon-url"), "https"));
    $("edit-form").addEventListener("submit", saveEditor);
    $("delete-app").addEventListener("click", deleteApp);
    $("audience").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => { draft.touched = true; setAudience(button.dataset.value); }));
    $("preview").addEventListener("click", () => { iconError(""); $("icon-sheet").showModal(); });
    $("pick-pc").addEventListener("click", () => $("pc-file").click());
    $("pc-file").addEventListener("change", async () => {
      const file = $("pc-file").files[0];
      $("pc-file").value = "";
      if (!file) return;
      if (file.size > MAX_IMAGE) { iconError(t("edit.tooLarge")); return; }
      await useImage(await readAsDataUrl(file));
    });
    $("pick-fs").addEventListener("click", openFiles);
    $("fs-search").addEventListener("input", () => {
      clearTimeout(fsSearchTimer);
      const pattern = $("fs-search").value.trim();
      fsSearchTimer = setTimeout(() => (pattern ? searchFiles(pattern) : listFolder(fsFolder)), 400);
    });
    $("icon-url-go").addEventListener("click", async () => {
      try {
        normalizeField($("icon-url"), "https");
        await useImage((await api("fetch_image", { url: $("icon-url").value })).dataUrl);
      } catch (error) {
        iconError(errorText(error));
      }
    });
    $("theme").querySelectorAll("button").forEach((button) => button.addEventListener("click", () => saveSettings({ theme: button.dataset.value })));
    $("language").addEventListener("change", () => saveSettings({ language: $("language").value }));
    const toggleOnline = async () => {
      await saveSettings({ online: state.settings.online !== true });
      if ($("welcome").open) runDiagnostics($("welcome-checks"));
      if (!$("screen-diagnostics").hidden) runDiagnostics($("checks"));
    };
    $("online").addEventListener("click", toggleOnline);
    $("welcome-online").addEventListener("click", toggleOnline);
    $("recheck").addEventListener("click", () => runDiagnostics($("checks")));
    $("copy-report").addEventListener("click", copyReport);
    $("export").addEventListener("click", exportBackup);
    $("import").addEventListener("click", () => $("import-file").click());
    $("import-file").addEventListener("change", () => {
      const file = $("import-file").files[0];
      $("import-file").value = "";
      if (file) importBackup(file);
    });
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  }

  function block(kind) {
    applyLanguage();
    $("shell").hidden = true;
    $("blocked").hidden = false;
    $("blocked-title").textContent = t("blocked." + kind + "Title");
    $("blocked-text").textContent = t("blocked." + kind + "Text");
  }

  (async function start() {
    applyTheme();
    wire();
    try {
      await loadToken();
      state = await api("state");
    } catch (error) {
      await loadLanguage();
      const kinds = { not_admin: "admin", session: "session", data_damaged: "data", newer_data: "newer" };
      block(kinds[error.message] || "session");
      return;
    }
    await loadLanguage();
    applyTheme();
    applyLanguage();
    renderSettings();
    renderApps();
    $("shell").hidden = false;
    host = findHost();
    if (host) host.listen(onSave);
    showScreen("apps");
    if (state.restored) showStatus(t("status.restored"), null, true);
    if (!state.settings.welcomed) {
      $("welcome").showModal();
      runDiagnostics($("welcome-checks"));
    }
  })();
})();
