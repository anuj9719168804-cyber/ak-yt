// Preloaded with `node --require` (see entrypoint.sh). The bgutil 1.3.1 server has no
// --host flag and listens on all interfaces; this forces every TCP listener in the
// process onto 127.0.0.1 so the PO-token server is never reachable from outside.
const net = require("net");
const origListen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...args) {
  const a0 = args[0];
  if (a0 && typeof a0 === "object" && !Array.isArray(a0) && a0.path === undefined && a0.fd === undefined) {
    const { ipv6Only, ...rest } = a0;
    args[0] = { ...rest, host: "127.0.0.1" };
  } else if (typeof a0 === "number" || (typeof a0 === "string" && /^\d+$/.test(a0))) {
    // listen(port[, host][, backlog][, cb])
    if (typeof args[1] === "string") args[1] = "127.0.0.1";
    else args.splice(1, 0, "127.0.0.1");
  }
  return origListen.apply(this, args);
};
