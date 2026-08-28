# ezsocks

`ezsocks` is heavily based on
[`prettysocks`](https://github.com/twisteroidambassador/prettysocks), a
simplistic, dual-stack friendly SOCKS5 proxy server that implements Happy
Eyeballs for outgoing connections. All credit for the original design and
implementation goes to the upstream project; this fork builds on top of it.

## What's been updated

- **Throughput and concurrency** — optional `uvloop` event loop, larger relay
  buffer, `TCP_NODELAY` on both legs of each connection,
  `serve_forever()` instead of a polling loop, dropped per-chunk debug
  logging, and multi-process worker support (via `SO_REUSEPORT`) to use more
  than one CPU core.
- **Command line options** — every tunable (listen address/port, log level,
  Happy Eyeballs implementation and timing, worker count, relay buffer size,
  backlog) is now exposed as a CLI flag instead of requiring script edits.
- **TOML config file support** — settings can be kept in a config file
  (`/etc/prettysocks/config.toml` by default, or a path passed via
  `--config`), parsed with the stdlib `tomllib` (no new dependency).
  Precedence is defaults < config file < command line flags.
- **`-w auto` / `worker_processes = "auto"`** — resolves to `os.cpu_count()`
  worker processes at startup. The default remains 1 worker, so a bare
  invocation behaves exactly as before.
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
- **Silenced a third-party deprecation warning** — filtered out uvloop's
  internal use of the now-deprecated `asyncio.iscoroutinefunction()`
  (Python 3.13+), which is an upstream uvloop issue unrelated to this code.
