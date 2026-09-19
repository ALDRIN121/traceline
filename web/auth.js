/* Release-mode browser authentication bootstrap.
 *
 * The install owner opens the already-authenticated UI with a one-time
 * `#install_token=<bearer>` fragment. Fragments are not sent to the server;
 * this script consumes the token before the application starts, removes it
 * from the address bar/history entry, and retains it in this closure only.
 * Local-owner mode has no fragment and sends no Authorization header.
 */
(function () {
  "use strict";

  const fragment = new URLSearchParams(location.hash.startsWith("#") ? location.hash.slice(1) : "");
  const bearerToken = (fragment.get("install_token") || "").trim();
  if (fragment.has("install_token")) {
    fragment.delete("install_token");
    const remaining = fragment.toString();
    history.replaceState({}, "", `${location.pathname}${location.search}${remaining ? `#${remaining}` : ""}`);
  }

  window.__tracelineAuthHeaders = function (headers = {}) {
    const next = { ...headers };
    const hasAuthorization = Object.keys(next).some((key) => key.toLowerCase() === "authorization");
    if (bearerToken && !hasAuthorization) next.Authorization = `Bearer ${bearerToken}`;
    return next;
  };
})();
