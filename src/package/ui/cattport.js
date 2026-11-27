// Runs in the DSM page, not the iframe, so saving finishes even though the restart closes the window.
Ext.ns("labs.morcatt.cattport");

(function () {
  const NS = labs.morcatt.cattport;
  const PACKAGE = "Cattport";
  const INSTANCE = "labs.morcatt.cattport.Instance";
  const TILES = "labs.morcatt.cattport.tile.";
  const BASE = "/webman/3rdparty/" + PACKAGE + "/";
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const token = () => decodeURIComponent(SYNO.SDS.Session.SynoToken);
  let currentWindow = null;
  let running = false;
  let listener = null;

  NS.version = "__VERSION__";

  Ext.define("labs.morcatt.cattport.Instance", {
    extend: "SYNO.SDS.AppInstance",
    appWindowName: "labs.morcatt.cattport.AppWindow",
    constructor: function () {
      this.callParent(arguments);
    }
  });

  Ext.define("labs.morcatt.cattport.AppWindow", {
    extend: "SYNO.SDS.AppWindow",
    constructor: function (config) {
      this.callParent([Ext.apply({
        resizable: true, maximizable: true, minimizable: true,
        width: 1040, height: 700, minWidth: 720, minHeight: 480,
        layout: "fit", border: false,
        items: [{
          xtype: "box",
          autoEl: { tag: "iframe", src: BASE + "index.html?v=" + NS.version, frameborder: "0", style: "width:100%;height:100%;border:none;" }
        }]
      }, config)]);
      currentWindow = this;
    },
    onDestroy: function () {
      if (currentWindow === this) currentWindow = null;
      return this.callParent(arguments);
    }
  });

  function notify(status) {
    if (listener) {
      try { listener(status); } catch (ignored) { /* the page may already be gone */ }
    }
  }

  async function api(action, payload) {
    const response = await fetch(BASE + "api.cgi", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-SYNO-TOKEN": token() },
      body: JSON.stringify(Object.assign({ action: action }, payload || {}))
    });
    const result = await response.json();
    if (!result.success) throw new Error(result.error || "internal");
    return result.data;
  }

  async function webapi(params) {
    const response = await fetch("/webapi/entry.cgi", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-SYNO-TOKEN": token() },
      body: new URLSearchParams(params)
    });
    return response.json();
  }

  const fnMap = () => (SYNO.SDS.Config && SYNO.SDS.Config.FnMap) || {};

  // Reads the menu DSM has loaded; get_ui_config can keep a tile that was already removed.
  function menuMatches(ids, known) {
    const map = fnMap();
    const wanted = new Set(ids);
    return ids.every((id) => map[id]) && known.every((id) => wanted.has(id) || !map[id]);
  }

  async function waitForMenu(ids, known) {
    for (let attempt = 0; attempt < 12; attempt++) {
      if (menuMatches(ids, known)) return true;
      await sleep(500);
    }
    return false;
  }

  // DSM refreshes the list every browser and user loads only when the package stops, which also closes this window.
  async function restartAndReopen() {
    const wasOpen = !!currentWindow;
    const stop = await webapi({ api: "SYNO.Core.Package.Control", version: "1", method: "stop", id: PACKAGE });
    const start = await webapi({ api: "SYNO.Core.Package.Control", version: "1", method: "start", id: PACKAGE });
    if (!stop.success || !start.success) return false;
    if (wasOpen) {
      await sleep(500);
      SYNO.SDS.AppLaunch(INSTANCE);
    }
    return true;
  }

  NS.restartAndReopen = restartAndReopen;

  // Who sees a tile limited to people: DSM's Application Privileges, written with the administrator's session.
  // "Everyone except" allows the group every local user is in, then denies the exceptions; a deny wins in DSM.
  const ALL_IPS = ["0.0.0.0"];

  function wantedRules(id, rule) {
    const allow = (type, name) => ({ entity_type: type, entity_name: name, app_id: id, allow_ip: ALL_IPS, deny_ip: [] });
    const deny = (type, name) => ({ entity_type: type, entity_name: name, app_id: id, allow_ip: [], deny_ip: ALL_IPS });
    if (rule.mode !== "people" && rule.mode !== "everyone") return [];
    const make = rule.mode === "people" ? allow : deny;
    const rules = rule.users.map((name) => make("user", name)).concat(rule.groups.map((name) => make("group", name)));
    if (rule.mode === "everyone") rules.push(allow("group", "users"));
    return rules;
  }

  const sameEntity = (a, b) => a.entity_type === b.entity_type && String(a.entity_name).toLowerCase() === String(b.entity_name).toLowerCase();
  const sameRule = (a, b) => sameEntity(a, b) && JSON.stringify(a.allow_ip || []) === JSON.stringify(b.allow_ip || []) && JSON.stringify(a.deny_ip || []) === JSON.stringify(b.deny_ip || []);

  async function applyRules(rules) {
    let ok = true;
    for (const id of Object.keys(rules || {})) {
      const wanted = wantedRules(id, rules[id]);
      const listed = await webapi({ api: "SYNO.Core.AppPriv.Rule", version: "1", method: "list", app_id: JSON.stringify(id) });
      // An admins-only tile DSM has no privileges for has nothing to clean up.
      if (!listed.success) { if (wanted.length) ok = false; continue; }
      // Rules left from an earlier choice would still let those people in.
      const stale = ((listed.data && listed.data.rules) || []).filter((rule) => !wanted.some((want) => sameRule(want, rule)))
        .map((rule) => ({ entity_type: rule.entity_type, entity_name: rule.entity_name, app_id: id, allow_ip: rule.allow_ip || [], deny_ip: rule.deny_ip || [] }));
      if (stale.length) {
        const removed = await webapi({ api: "SYNO.Core.AppPriv.Rule", version: "1", method: "delete", rules: JSON.stringify(stale) });
        if (!removed.success) ok = false;
      }
      if (!wanted.length) continue;
      const set = await webapi({ api: "SYNO.Core.AppPriv.Rule", version: "1", method: "set", rules: JSON.stringify(wanted) });
      if (!set.success) ok = false;
    }
    return ok;
  }

  NS.saveToDsm = async function () {
    if (running) return;
    running = true;
    notify("saving");
    try {
      const known = Object.keys(fnMap()).filter((id) => id.startsWith(TILES));
      const { ids, rules, fingerprint } = await api("publish");
      if (!(await restartAndReopen())) {
        notify("failed");
        return;
      }
      // After the restart, so DSM already knows the new tiles. If this fails, those tiles stay for admins only.
      let rulesSaved = true;
      try { rulesSaved = await applyRules(rules); } catch (error) { rulesSaved = false; }
      // Until DSM has the rules, Cattport keeps counting the change as unsaved.
      if (rulesSaved && fingerprint) await api("rules_applied", { fingerprint: fingerprint }).catch(() => {});
      if (typeof SYNO.SDS.reloadJSConfig === "function") SYNO.SDS.reloadJSConfig();
      // The reopened window needs a moment to start listening again.
      const inMenu = await waitForMenu(ids, known);
      await sleep(800);
      notify(!rulesSaved ? "privileges" : inMenu ? "saved" : "reload");
    } catch (error) {
      notify("failed");
    } finally {
      running = false;
    }
  };

  NS.listen = function (callback) {
    listener = callback;
  };

  NS.openPackageCenter = function () {
    SYNO.SDS.AppLaunch("SYNO.SDS.PkgManApp.Instance");
  };
})();
