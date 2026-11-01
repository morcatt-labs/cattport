"use strict";
// Whether DSM was reached on the local network or Tailscale, rather than through a domain.
window.CATTPORT_NETWORK = (function () {
  function isPrivate(host) {
    host = host.replace(/^\[|\]$/g, "").toLowerCase();
    if (host === "localhost" || !host.includes(".") && !host.includes(":")) return true;
    if (/\.(local|lan|home|internal|ts\.net)$/.test(host)) return true;
    if (/^(fc|fd|fe80|::1)/.test(host)) return true;
    const parts = host.split(".").map(Number);
    if (parts.length !== 4 || parts.some(Number.isNaN)) return false;
    return parts[0] === 10 || parts[0] === 127 || (parts[0] === 192 && parts[1] === 168) || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
      || (parts[0] === 169 && parts[1] === 254) || (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127);
  }

  return { isPrivate: isPrivate };
})();
