"use strict";
// Opens a tile's local or outside address, depending on how DSM was reached. Only saved apps can be opened.
(function () {
  function destination(link) {
    if (!window.CATTPORT_NETWORK.isPrivate(location.hostname) && link.external) return link.external;
    if (!link.port) return link.url;
    return link.protocol + "://" + location.hostname + ":" + link.port + "/";
  }

  const key = new URLSearchParams(location.search).get("app");
  const fail = () => { document.body.textContent = "This app is no longer in Cattport. Reload DSM."; };
  fetch("links.json", { cache: "no-store" }).then((response) => response.json()).then((links) => {
    const link = Object.prototype.hasOwnProperty.call(links, key) ? links[key] : null;
    if (link) location.replace(destination(link)); else fail();
  }).catch(fail);
})();
