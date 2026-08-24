#!/usr/bin/env python3

"""Simplistic SOCKS5 proxy with Happy Eyeballs for outgoing connections.

This script requires Python 3.11+.

Two implementations of Happy Eyeballs can be used: either built-in with
Python, or from the `async-stagger` module (v0.4.0 and up).
"""

"""
Copyright (C) 2018 - 2024 twisteroid ambassador

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""


import argparse
import asyncio
import collections
import contextlib
import dataclasses
import enum
import errno
import ipaddress
import logging
import multiprocessing
import os
import signal
import socket
import sys
import tomllib
import warnings
from functools import partial
from collections.abc import Callable, Awaitable

try:
    import async_stagger
except ImportError:
    async_stagger = None

try:
    import uvloop
except ImportError:
    uvloop = None


# The values below are the defaults for all settings. Every one of them can
# also be overridden on the command line; run this script with --help for
# details. Editing the values here just changes what the command line
# defaults to.

# ========== Configuration ==========

LISTEN_HOST = ['127.0.0.1', '::1']
LISTEN_PORT = 1080
LOGLEVEL = logging.INFO

# ========== Tunables ==========

"""When set to True, use Python's built-in Happy Eyeballs implementation
(Only available on Python 3.8.1 and up), which does not support asynchronous
address resolution. When set to False, use implementation in `async_stagger`
module."""
USE_BUILTIN_HAPPY_EYEBALLS = False

# The following specify Happy Eyeballs behavior. Refer to RFC 8305 for their
# definitions.
# https://tools.ietf.org/html/rfc8305#section-8
RESOLUTION_DELAY = 0.05  # seconds
FIRST_ADDRESS_FAMILY_COUNT = 1
CONNECTION_ATTEMPT_DELAY = 0.25  # seconds

# Number of worker processes to run. Each worker binds its own listening
# socket to the same address/port with SO_REUSEPORT set, and the kernel
# distributes incoming connections between them, allowing the proxy to use
# multiple CPU cores. Set to e.g. os.cpu_count() to use all cores. Requires
# SO_REUSEPORT support (Linux, *BSD, macOS; not available on Windows).
WORKER_PROCESSES = 1

# Size, in bytes, of the buffer used to relay data between downstream and
# upstream connections. A larger buffer means fewer read/write syscalls per
# byte transferred, at the cost of more memory per connection.
RELAY_BUFFER_SIZE = 2 ** 16

# Backlog for listening socket(s).
LISTEN_BACKLOG = 512

# ==========


@dataclasses.dataclass
class ProxyConfig:
    listen_host: list[str]
    listen_port: int
    log_level: int
    use_builtin_happy_eyeballs: bool
    resolution_delay: float
    first_address_family_count: int
    connection_attempt_delay: float
    worker_processes: int
    relay_buffer_size: int
    listen_backlog: int


_LOG_LEVEL_NAMES = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

DEFAULT_CONFIG_PATH = '/etc/prettysocks/config.toml'


def _default_settings() -> dict:
    """Built-in defaults, keyed by ProxyConfig field name. This is also the
    set of keys accepted in the TOML config file."""
    return {
        'listen_host': list(LISTEN_HOST),
        'listen_port': LISTEN_PORT,
        'listen_backlog': LISTEN_BACKLOG,
        'log_level': logging.getLevelName(LOGLEVEL),
        'use_builtin_happy_eyeballs': USE_BUILTIN_HAPPY_EYEBALLS,
        'resolution_delay': RESOLUTION_DELAY,
        'first_address_family_count': FIRST_ADDRESS_FAMILY_COUNT,
        'connection_attempt_delay': CONNECTION_ATTEMPT_DELAY,
        'worker_processes': WORKER_PROCESSES,
        'relay_buffer_size': RELAY_BUFFER_SIZE,
    }


_SETTING_KEYS = frozenset(_default_settings())


def _load_config_file(path: str, *, explicit: bool) -> dict:
    """Load settings from a TOML config file.

    If the file does not exist and `path` was not explicitly requested
    (i.e. it's the default location), this is not an error and an empty
    dict is returned.
    """
    try:
        with open(path, 'rb') as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            raise
        return {}
    except tomllib.TOMLDecodeError as e:
        raise ValueError('invalid TOML in %s: %s' % (path, e)) from e
    unknown = set(data) - _SETTING_KEYS
    if unknown:
        raise ValueError('unknown setting(s) in %s: %s' % (
            path, ', '.join(sorted(unknown))))
    if ('use_builtin_happy_eyeballs' in data
            and not isinstance(data['use_builtin_happy_eyeballs'], bool)):
        raise ValueError(
            'use_builtin_happy_eyeballs in %s must be a boolean '
            '(true/false)' % path)
    if isinstance(data.get('listen_host'), str):
        data['listen_host'] = [data['listen_host']]
    return data


def _int_or_auto(value: str) -> int | str:
    if value == 'auto':
        return 'auto'
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            '%r is not an integer or "auto"' % value) from None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Simplistic SOCKS5 proxy with Happy Eyeballs for '
                     'outgoing connections.',
    )
    parser.add_argument(
        '-c', '--config', metavar='PATH',
        help='Path to a TOML config file. Settings there override the '
             'built-in defaults, and are themselves overridden by any '
             'other command line flags given. If this is not given '
             'explicitly and the default path does not exist, it is '
             'silently skipped. (default: %s)' % DEFAULT_CONFIG_PATH)
    parser.add_argument(
        '--listen-host', action='append', metavar='HOST',
        help='Address to listen on. Can be given multiple times to listen '
             'on multiple addresses. (default: %s)' % ', '.join(LISTEN_HOST))
    parser.add_argument(
        '-p', '--listen-port', type=int, metavar='PORT',
        help='Port to listen on. (default: %s)' % LISTEN_PORT)
    parser.add_argument(
        '--listen-backlog', type=int, metavar='N',
        help='Backlog for listening socket(s). (default: %s)' % LISTEN_BACKLOG)
    parser.add_argument(
        '--log-level', type=str.upper, choices=_LOG_LEVEL_NAMES,
        help='Logging verbosity. (default: %s)' % logging.getLevelName(LOGLEVEL))
    parser.add_argument(
        '--happy-eyeballs-impl', choices=['async-stagger', 'builtin'],
        help="Happy Eyeballs implementation to use: Python's built-in "
             "implementation (no asynchronous address resolution), or the "
             "async-stagger module. (default: %s)" % (
                 'builtin' if USE_BUILTIN_HAPPY_EYEBALLS else 'async-stagger'))
    parser.add_argument(
        '--resolution-delay', type=float, metavar='SECONDS',
        help='(async-stagger implementation only) Delay before resolving '
             'the next address family. See RFC 8305 section 8. '
             '(default: %s)' % RESOLUTION_DELAY)
    parser.add_argument(
        '--first-address-family-count', type=int, metavar='N',
        help='Number of addresses of the first resolved address family to '
             'try before interleaving with the other family. See RFC 8305 '
             'section 8. (default: %s)' % FIRST_ADDRESS_FAMILY_COUNT)
    parser.add_argument(
        '--connection-attempt-delay', type=float, metavar='SECONDS',
        help='Delay between successive connection attempts to different '
             'addresses. See RFC 8305 section 8. '
             '(default: %s)' % CONNECTION_ATTEMPT_DELAY)
    parser.add_argument(
        '-w', '--workers', type=_int_or_auto, metavar='N|auto',
        help='Number of worker processes to run. Each worker binds the '
             'listen address/port with SO_REUSEPORT set, so the kernel '
             'distributes connections between them across CPU cores. Pass '
             '"auto" to use one worker per CPU core (os.cpu_count()). '
             '(default: %s)' % WORKER_PROCESSES)
    parser.add_argument(
        '--relay-buffer-size', type=int, metavar='BYTES',
        help='Buffer size used to relay data between downstream and '
             'upstream connections. (default: %s)' % RELAY_BUFFER_SIZE)
    return parser


def parse_args(argv: list[str] | None = None) -> ProxyConfig:
    parser = build_arg_parser()
    ns = parser.parse_args(argv)

    settings = _default_settings()
    try:
        settings.update(_load_config_file(
            ns.config or DEFAULT_CONFIG_PATH, explicit=ns.config is not None))
    except FileNotFoundError:
        parser.error('config file not found: %s' % ns.config)
    except ValueError as e:
        parser.error(str(e))

    # Command line flags override both the config file and the built-in
    # defaults. Only flags the user actually passed (non-None) apply here.
    if ns.listen_host is not None:
        settings['listen_host'] = ns.listen_host
    if ns.listen_port is not None:
        settings['listen_port'] = ns.listen_port
    if ns.listen_backlog is not None:
        settings['listen_backlog'] = ns.listen_backlog
    if ns.log_level is not None:
        settings['log_level'] = ns.log_level
    if ns.happy_eyeballs_impl is not None:
        settings['use_builtin_happy_eyeballs'] = (
            ns.happy_eyeballs_impl == 'builtin')
    if ns.resolution_delay is not None:
        settings['resolution_delay'] = ns.resolution_delay
    if ns.first_address_family_count is not None:
        settings['first_address_family_count'] = ns.first_address_family_count
    if ns.connection_attempt_delay is not None:
        settings['connection_attempt_delay'] = ns.connection_attempt_delay
    if ns.workers is not None:
        settings['worker_processes'] = ns.workers
    if ns.relay_buffer_size is not None:
        settings['relay_buffer_size'] = ns.relay_buffer_size

    log_level_name = str(settings['log_level']).upper()
    if log_level_name not in _LOG_LEVEL_NAMES:
        parser.error('invalid log_level %r (must be one of %s)' % (
            settings['log_level'], ', '.join(_LOG_LEVEL_NAMES)))

    if settings['worker_processes'] == 'auto':
        settings['worker_processes'] = os.cpu_count() or 1

    try:
        return ProxyConfig(
            listen_host=list(settings['listen_host']),
            listen_port=int(settings['listen_port']),
            log_level=getattr(logging, log_level_name),
            use_builtin_happy_eyeballs=bool(
                settings['use_builtin_happy_eyeballs']),
            resolution_delay=float(settings['resolution_delay']),
            first_address_family_count=int(
                settings['first_address_family_count']),
            connection_attempt_delay=float(
                settings['connection_attempt_delay']),
            worker_processes=int(settings['worker_processes']),
            relay_buffer_size=int(settings['relay_buffer_size']),
            listen_backlog=int(settings['listen_backlog']),
        )
    except (TypeError, ValueError) as e:
        parser.error('invalid configuration: %s' % e)


IPAddressType = ipaddress.IPv4Address | ipaddress.IPv6Address
HostType = str | ipaddress.IPv4Address | ipaddress.IPv6Address
ConnectFnType = Callable[[HostType, int],
                         Awaitable[tuple[asyncio.StreamReader,
                                         asyncio.StreamWriter]]]
AcceptFnType = Callable[[asyncio.StreamReader,
                         asyncio.StreamWriter,
                         ConnectFnType],
                        Awaitable[tuple[HostType,
                                        int,
                                        asyncio.StreamReader,
                                        asyncio.StreamWriter]]]
RelayFnType = Callable[[asyncio.StreamReader,
                        asyncio.StreamWriter,
                        asyncio.StreamReader,
                        asyncio.StreamWriter,
                        str],
                       Awaitable[None]]

INADDR_ANY = ipaddress.IPv4Address(0)


class BytesEnum(bytes, enum.Enum):
    pass


class SOCKS5AuthType(BytesEnum):
    NO_AUTH = b'\x00'
    GSSAPI = b'\x01'
    USERNAME_PASSWORD = b'\x02'
    NO_OFFERS_ACCEPTABLE = b'\xff'


class SOCKS5Command(BytesEnum):
    CONNECT = b'\x01'
    BIND = b'\x02'
    UDP_ASSOCIATE = b'\x03'


class SOCKS5AddressType(BytesEnum):
    IPV4_ADDRESS = b'\x01'
    DOMAIN_NAME = b'\x03'
    IPV6_ADDRESS = b'\x04'


class SOCKS5Reply(BytesEnum):
    SUCCESS = b'\x00'
    GENERAL_FAILURE = b'\x01'
    CONNECTION_NOT_ALLOWED_BY_RULESET = b'\x02'
    NETWORK_UNREACHABLE = b'\x03'
    HOST_UNREACHABLE = b'\x04'
    CONNECTION_REFUSED = b'\x05'
    TTL_EXPIRED = b'\x06'
    COMMAND_NOT_SUPPORTED = b'\x07'
    ADDRESS_TYPE_NOT_SUPPORTED = b'\x08'


class SOCKS5Acceptor:
    """Negotiate with downstream SOCKS5 clients."""
    _logger = logging.getLogger('socks5')

    def _map_exception_to_socks5_reply(self, exc: Exception) -> SOCKS5Reply:
        if isinstance(exc, ExceptionGroup):
            replies = map(self._map_exception_to_socks5_reply, exc.exceptions)
            reply_counter = collections.Counter(replies)
            reply_counter.pop(SOCKS5Reply.GENERAL_FAILURE, None)
            if reply_counter:
                return reply_counter.most_common(1)[0][0]
            return SOCKS5Reply.GENERAL_FAILURE
        if isinstance(exc, socket.gaierror):
            return SOCKS5Reply.HOST_UNREACHABLE
        if isinstance(exc, TimeoutError):
            return SOCKS5Reply.TTL_EXPIRED
        if isinstance(exc, ConnectionRefusedError):
            return SOCKS5Reply.CONNECTION_REFUSED
        if isinstance(exc, OSError):
            if exc.errno == errno.ENETUNREACH:
                return SOCKS5Reply.NETWORK_UNREACHABLE
            elif exc.errno == errno.EHOSTUNREACH:
                return SOCKS5Reply.HOST_UNREACHABLE
            elif exc.errno == errno.ECONNREFUSED:
                return SOCKS5Reply.CONNECTION_REFUSED
            elif exc.errno == errno.ETIMEDOUT:
                return SOCKS5Reply.TTL_EXPIRED
            else:
                return SOCKS5Reply.GENERAL_FAILURE
        self._logger.warning('Unexpected exception', exc_info=exc)
        raise exc

    async def accept(
            self,
            dreader: asyncio.StreamReader,
            dwriter: asyncio.StreamWriter,
            connector: ConnectFnType,
    ) -> tuple[
        HostType,
        int,
        asyncio.StreamReader,
        asyncio.StreamWriter,
    ]:
        """Negotiate with downstream SOCKS5 client.

        Accepts CONNECT command only, uses `connector` to connect to the
        upstream destination, and returns (dest_host, dest_port,
        upstream_reader, upstream_writer).
        """
        dname = repr(dwriter.get_extra_info('peername'))
        log_name = '{!s} <=> ()'.format(dname)
        try:
            buf = await dreader.readexactly(2)  # ver, number of auth methods
            if buf[0] != 5:
                raise ValueError('Invalid client request version')
            buf = await dreader.readexactly(buf[1])  # offered auth methods
            if SOCKS5AuthType.NO_AUTH not in buf:
                dwriter.write(b'\x05' + SOCKS5AuthType.NO_OFFERS_ACCEPTABLE)
                dwriter.write_eof()
                await dwriter.drain()
                raise ValueError(
                    'Client did not offer "no auth", offers: %r' % buf)
            dwriter.write(b'\x05' + SOCKS5AuthType.NO_AUTH)

            # client command
            buf = await dreader.readexactly(4)  # ver, cmd, rsv, addr_type
            if buf[0] != 5 or buf[2] != 0:
                raise ValueError('%s malformed SOCKS5 command'
                                 % log_name)
            cmd = SOCKS5Command(buf[1:2])
            addr_type = SOCKS5AddressType(buf[3:4])
            if addr_type is SOCKS5AddressType.IPV4_ADDRESS:
                uhost = ipaddress.IPv4Address(await dreader.readexactly(4))
            elif addr_type is SOCKS5AddressType.IPV6_ADDRESS:
                uhost = ipaddress.IPv6Address(await dreader.readexactly(16))
            elif addr_type is SOCKS5AddressType.DOMAIN_NAME:
                buf = await dreader.readexactly(1)  # address len
                uhost = (await dreader.readexactly(buf[0])).decode('utf-8')
                # Sometimes clients will pass in an IP address literal (such as
                # "127.0.0.1" as a host name. For example, Firefox does this if
                # network.proxy.socks_remote_dns is set to True.
                # However, we don't bother converting them into IPv(4|6)Address
                # objects here, since we will hand the address to connecting
                # functions that take strings anyway.
            else:
                raise ValueError('%s unsupported address type %r'
                                 % (log_name, addr_type))
            uport = int.from_bytes(await dreader.readexactly(2), 'big')
            log_name = '{!s} <=> ({!r}, {!r})'.format(dname, uhost, uport)
            self._logger.debug('%s parsed target address', log_name)
            if cmd is not SOCKS5Command.CONNECT:
                await self._reply(
                    dwriter, SOCKS5Reply.COMMAND_NOT_SUPPORTED, INADDR_ANY, 0)
                raise ValueError(
                    'Client command %r not supported' % cmd)
            self._logger.info('%s received CONNECT command', log_name)

            try:
                ureader, uwriter = await connector(uhost, uport)
            except Exception as e:
                self._logger.debug(
                    '%s Exception while connecting: %r', log_name, e)
                reply = self._map_exception_to_socks5_reply(e)
                await self._reply(dwriter, reply, INADDR_ANY, 0)
                raise

            sockname = uwriter.get_extra_info('sockname')
            bind_host = ipaddress.ip_address(sockname[0])
            bind_port = sockname[1]
            await self._reply(
                dwriter, SOCKS5Reply.SUCCESS, bind_host, bind_port)
            return uhost, uport, ureader, uwriter
        except asyncio.IncompleteReadError as e:
            raise ValueError('Client did not complete negotiation') from e

    async def _reply(
            self,
            dwriter: asyncio.StreamWriter,
            reply: SOCKS5Reply,
            host: HostType,
            port: int
    ) -> None:
        if isinstance(host, ipaddress.IPv4Address):
            b_addr = SOCKS5AddressType.IPV4_ADDRESS + host.packed
        elif isinstance(host, ipaddress.IPv6Address):
            b_addr = SOCKS5AddressType.IPV6_ADDRESS + host.packed
        else:
            b_addr = host.encode('idna')
            b_addr = (SOCKS5AddressType.DOMAIN_NAME
                      + len(b_addr).to_bytes(1, 'big') + b_addr)
        dwriter.write(b'\x05' + reply + b'\x00'
                      + b_addr + port.to_bytes(2, 'big'))
        if reply is not SOCKS5Reply.SUCCESS:
            dwriter.write_eof()
        await dwriter.drain()
        self._logger.debug('%r sent reply %s',
                           dwriter.get_extra_info('peername'), reply)


class Relayer:
    """Relay data between two (StreamReader, StreamWriter) pairs."""
    _logger = logging.getLogger('relay')

    def __init__(
            self,
            *,
            bufsize=2**16,
    ) -> None:
        self._bufsize = bufsize

    async def _relay_data_side(
            self,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
            log_name: str,
    ) -> None:
        try:
            while True:
                buf = await reader.read(self._bufsize)
                if not buf:  # EOF
                    break
                writer.write(buf)
                await writer.drain()
            try:
                self._logger.debug('%s passing EOF', log_name)
                writer.write_eof()
                await writer.drain()
            except OSError as e:
                if e.errno == errno.ENOTCONN:
                    self._logger.debug(
                        '%s endpoint already closed when passing EOF', log_name)
                    return
                raise
        except Exception as e:
            self._logger.info('%s caught exception: %r', log_name, e)
            raise

    async def relay(
            self,
            dreader: asyncio.StreamReader,
            dwriter: asyncio.StreamWriter,
            ureader: asyncio.StreamReader,
            uwriter: asyncio.StreamWriter,
            uname: str,
    ) -> None:
        """Pass data from dreader to uwriter, and ureader to dwriter."""
        dname = repr(dwriter.get_extra_info('peername'))
        utask = asyncio.create_task(self._relay_data_side(
            dreader, uwriter, '{!s} --> {!s}'.format(dname, uname)))
        dtask = asyncio.create_task(self._relay_data_side(
            ureader, dwriter, '{!s} <-- {!s}'.format(dname, uname)))
        try:
            await asyncio.gather(utask, dtask)
        except:
            dtask.cancel()
            utask.cancel()
            raise


@contextlib.asynccontextmanager
async def closing_writer(writer: asyncio.StreamWriter):
    try:
        yield
    finally:
        writer.close()
        await writer.wait_closed()


def _set_tcp_nodelay(writer: asyncio.StreamWriter) -> None:
    """Disable Nagle's algorithm on the underlying socket, if applicable."""
    sock = writer.get_extra_info('socket')
    if sock is not None and sock.family in (socket.AF_INET, socket.AF_INET6):
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


async def handler(
        accept: AcceptFnType,
        connect: ConnectFnType,
        relay: RelayFnType,
        dreader: asyncio.StreamReader,
        dwriter: asyncio.StreamWriter
) -> None:
    """Main server handler."""
    logger = logging.getLogger('handler')
    dname = repr(dwriter.get_extra_info('peername'))
    log_name = '{!s} <=> ()'.format(dname)
    _set_tcp_nodelay(dwriter)
    try:
        async with contextlib.AsyncExitStack() as stack:
            logger.debug('%s received connection', log_name)
            await stack.enter_async_context(closing_writer(dwriter))
            uhost, uport, ureader, uwriter = await accept(
                dreader, dwriter, connect)
            _set_tcp_nodelay(uwriter)
            await stack.enter_async_context(closing_writer(uwriter))
            uname = '({!r}, {!r})'.format(uhost, uport)
            log_name = '{!s} <=> {!s}'.format(dname, uname)
            logger.info('%s relaying', log_name)
            await relay(dreader, dwriter, ureader, uwriter, uname)
            logger.info('%s done', log_name)
    except asyncio.CancelledError:
        logger.info('%s handler cancelled', log_name)
        raise
    except (OSError, ValueError, TimeoutError, ExceptionGroup) as e:
        logger.info('%s exception: %r', log_name, e)
    except Exception as e:
        logger.error('%s exception:', log_name, exc_info=e)


def sigterm_handler():
    logging.warning('Process received SIGTERM')
    sys.exit()


async def builtin_happy_eyeballs_connect(
        host: HostType,
        port: int,
        delay: float,
        interleave: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(
        str(host),
        port,
        happy_eyeballs_delay=delay,
        interleave=interleave,
    )


async def async_stagger_connect(
        host: HostType,
        port: int,
        delay: float,
        resolver,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await async_stagger.open_connection(
        str(host),
        port,
        delay=delay,
        resolver=resolver,
        raise_exc_group=True,
    )


def _make_listening_socket(host: str, port: int, backlog: int) -> socket.socket:
    """Create a listening socket with SO_REUSEPORT set, so that multiple
    worker processes can share the same address/port and have the kernel
    load-balance connections between them."""
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'SO_REUSEPORT'):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind((host, port))
    sock.listen(backlog)
    sock.setblocking(False)
    return sock


async def _serve_forever(sock: socket.socket, proxy_handler) -> None:
    server = await asyncio.start_server(proxy_handler, sock=sock)
    async with server:
        await server.serve_forever()


async def amain(config: ProxyConfig):
    loop = asyncio.get_event_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, sigterm_handler)
    acceptor = SOCKS5Acceptor()
    relayer = Relayer(bufsize=config.relay_buffer_size)
    if config.use_builtin_happy_eyeballs:
        connector = partial(
            builtin_happy_eyeballs_connect,
            delay=config.connection_attempt_delay,
            interleave=config.first_address_family_count,
        )
    else:
        if async_stagger is None:
            raise ImportError(
                'async_stagger module is required, but cannot be imported. '
                'To use without async_stagger, pass '
                '--happy-eyeballs-impl builtin.'
            )
        resolver = partial(
            async_stagger.resolvers.concurrent_resolver,
            resolution_delay=config.resolution_delay,
            first_addr_family_count=config.first_address_family_count,
            raise_exc_group=True,
        )
        connector = partial(
            async_stagger_connect,
            delay=config.connection_attempt_delay,
            resolver=resolver,
        )
    proxy_handler = partial(
        handler,
        acceptor.accept,
        connector,
        relayer.relay,
    )
    sockets = [
        _make_listening_socket(host, config.listen_port, config.listen_backlog)
        for host in config.listen_host
    ]
    await asyncio.gather(*(
        _serve_forever(sock, proxy_handler) for sock in sockets
    ))


def setup_logging(log_level: int) -> None:
    rootlogger = logging.getLogger()
    rootlogger.setLevel(log_level)
    stream_formatter = logging.Formatter('%(levelname)-8s %(name)s %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(stream_formatter)
    rootlogger.addHandler(stream_handler)
    logging.captureWarnings(True)
    warnings.filterwarnings('always')


def run_worker(config: ProxyConfig) -> None:
    setup_logging(config.log_level)
    logging.getLogger('config').debug('Starting with configuration: %r', config)
    run = uvloop.run if uvloop is not None else asyncio.run
    try:
        run(amain(config))
    except (KeyboardInterrupt, SystemExit) as e:
        logging.warning('Caught %r', e)


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    if config.worker_processes <= 1:
        run_worker(config)
        return

    workers = [
        multiprocessing.Process(target=run_worker, args=(config,))
        for _ in range(config.worker_processes)
    ]
    for worker in workers:
        worker.start()

    def _forward_signal(signum, _frame):
        for worker in workers:
            if worker.is_alive() and worker.pid is not None:
                os.kill(worker.pid, signum)

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)
    for worker in workers:
        worker.join()


if __name__ == '__main__':
    main()
