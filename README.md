# ezsocks

`ezsocks` is heavily based on
[`prettysocks`](https://github.com/twisteroidambassador/prettysocks), a
simplistic, dual-stack friendly SOCKS5 proxy server that implements Happy
Eyeballs for outgoing connections. All credit for the original design and
implementation goes to the upstream project; this fork builds on top of it.

## What's been updated

- **Throughput and concurrency** — larger relay buffer, `TCP_NODELAY` on
  both legs of each connection, `serve_forever()` instead of a polling loop,
  dropped per-chunk debug logging, and multi-process worker support (via
  `SO_REUSEPORT`) to use more than one CPU core.
- **Command line options** — every tunable (listen address/port, log level,
  Happy Eyeballs implementation and timing, worker count, relay buffer size,
  backlog, keepalive idle time, DNS cache lifetime) is now exposed as a CLI
  flag instead of requiring script edits.
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
