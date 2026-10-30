# ezsocks

`ezsocks` is heavily based on
[`prettysocks`](https://github.com/twisteroidambassador/prettysocks), a
simplistic, dual-stack friendly SOCKS5 proxy server that implements Happy
Eyeballs for outgoing connections. All credit for the original design and
implementation goes to the upstream project; ezsocks builds on top of it.

## What it is

ezsocks is a small SOCKS5 proxy server, a single Python script with one
dependency. Programs that support SOCKS5 (browsers, `curl`, `ssh`, most
download and chat clients) connect to it and ask it to open a TCP
connection somewhere; ezsocks makes that connection and passes data back
and forth.

- **Dual stack** — it listens on IPv4 and IPv6 (`127.0.0.1` and `::1` by
  default) and connects out over whichever one works. A client that only
  speaks IPv4 to the proxy can still reach IPv6-only servers, and the other
  way around.
- **Happy Eyeballs** ([RFC 8305](https://www.rfc-editor.org/rfc/rfc8305)) —
  when a host name has both IPv6 and IPv4 addresses, ezsocks resolves both
  at the same time and races connection attempts, preferring IPv6 but
  falling back to IPv4 within a fraction of a second. A broken IPv6 route
  costs a short delay instead of a long timeout.
- **Name resolution on the proxy side** — clients can hand over host names
  instead of addresses (`socks5h://` in `curl`, "Proxy DNS when using SOCKS
  v5" in Firefox), so DNS lookups happen where the proxy runs, on its
  network, not on the client's.
- **CONNECT only, no authentication** — it supports outgoing TCP
  connections (the SOCKS5 `CONNECT` command), not `BIND` or UDP, and
  accepts any client that can reach it. Keep it on localhost, or behind a
  firewall, rather than exposing it to a network.

## What it's used for

ezsocks gives programs a way out through a network other than the one
they're on, one program at a time, without changing the system's routing:

- **Per-application VPN with [eznetns](https://github.com/kalken/eznetns)**
  — eznetns sets up network namespaces with their own WireGuard VPN and
  routing, without touching the host's default route. Run ezsocks inside
  such a namespace, bind a port on the host to it (eznetns includes a
  systemd socket setup for that), and point only the programs that should
  use the VPN at the proxy. Everything else keeps using the normal
  connection, and DNS lookups for proxied programs go through the VPN too,
  since ezsocks resolves names inside the namespace. The
  [eznixpkgs](https://github.com/kalken/eznixpkgs) NixOS module sets this up
  for you, with one ezsocks instance per eznetns namespace.
- **Reaching another network** — run it on a machine in another network
  (at home, on a server) and reach it over an SSH tunnel or similar, to
  browse or connect as if from there.
- **Better IPv6/IPv4 behaviour for programs that don't handle it well** —
  programs that only do IPv4, or that wait a long time on a broken IPv6
  route, get dual-stack connections with Happy Eyeballs through the proxy.

## Running it

ezsocks needs Python 3.11 or newer and the
[`async-stagger`](https://pypi.org/project/async-stagger/) package:

```sh
pip install async-stagger
python3 ezsocks.py
```

This listens on `127.0.0.1:1080` and `[::1]:1080`. To try it:

```sh
curl --socks5-hostname 127.0.0.1:1080 https://example.com
```

Without `async-stagger`, pass `--happy-eyeballs-impl builtin` to use
Python's own Happy Eyeballs implementation instead (the DNS cache is not
available then). Run `python3 ezsocks.py --help` for all options; each
one can also be set in a TOML config file, see below.

## What's been updated

- **Throughput and concurrency** — larger relay buffer, `TCP_NODELAY` on
  both legs of each connection, `serve_forever()` instead of a polling loop,
  dropped per-chunk debug logging, and multi-process worker support (via
  `SO_REUSEPORT`) to use more than one CPU core.
- **Command line options** — every tunable (listen address/port, log level,
  Happy Eyeballs implementation and timing, worker count, relay buffer size,
  backlog, keepalive idle time, DNS cache lifetime, handshake timeout) is now
  exposed as a CLI flag instead of requiring script edits.
- **TOML config file support** — settings can be kept in a config file
  (`/etc/ezsocks/config.toml` by default, or a path passed via
  `--config`), parsed with the stdlib `tomllib` (no new dependency).
  Precedence is defaults < config file < command line flags.
- **`-w auto` / `worker_processes = "auto"`** — resolves to `os.cpu_count()`
  worker processes at startup. The default remains 1 worker, so a bare
  invocation behaves exactly as before.
- **TCP keepalive on both legs** — clients and upstream servers that
  disappear without closing (sleep, lost network, power loss) are detected
  and their connections dropped, instead of being held open until restart.
  Probing starts after `--keepalive-idle` / `keepalive_idle` seconds of
  silence (default 300, `0` disables); live idle connections are unaffected.
- **Faster relaying** — once a connection is set up, data is passed
  between the two sockets directly from the event loop's callbacks instead
  of through asyncio streams and a pair of coroutines, removing extra copies
  and task wake-ups per chunk. Backpressure is preserved: reading from one
  side pauses while the other side's write buffer is over
  `--relay-buffer-size` / `relay_buffer_size` bytes (now 256 KiB by
  default).
- **DNS cache** — with the async-stagger Happy Eyeballs implementation (the
  default), address lookups are cached for `--dns-cache-ttl` /
  `dns_cache_ttl` seconds (default 60, `0` disables), and concurrent lookups
  of the same name share a single query. The real DNS TTL isn't available
  through `getaddrinfo()`, so this is a fixed lifetime. Failed lookups are
  not cached, and each worker process keeps its own cache of up to 1024
  names.
- **Handshake timeout** — clients that don't complete the SOCKS5
  negotiation within `--handshake-timeout` / `handshake_timeout` seconds
  (default 30, `0` disables) are disconnected, so connections that send
  nothing can't pile up and use up file descriptors. Connecting to the
  upstream server is not counted.
- **Resolved configuration logging** — the fully resolved configuration
  (defaults + config file + CLI flags) is logged at `DEBUG` level on startup.
- **Clean shutdown on `SIGTERM`** — the main task is now cancelled instead of
  calling `sys.exit()` from the signal handler, letting `asyncio.run()` tear
  everything down through its normal cancellation path instead of crashing
  mid-callback.
- **Fixed orphaned connection tasks during shutdown** — per-connection
  handler tasks and relay sub-tasks are now properly referenced and awaited
  on cancellation, preventing the garbage collector from reaping pending
  tasks and producing spurious `GeneratorExit`-related crashes under load.
