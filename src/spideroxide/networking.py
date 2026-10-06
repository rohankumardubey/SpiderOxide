from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from httpcore import AsyncNetworkBackend, AsyncNetworkStream
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION

from .settings import Settings

logger = logging.getLogger(__name__)

_TLS_VERSIONS = {
    "TLSv1.0": ssl.TLSVersion.TLSv1,
    "TLSv1.1": ssl.TLSVersion.TLSv1_1,
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}
_RESOLVERS = {
    "scrapy.resolver.CachingThreadedResolver": socket.AF_INET,
    "spideroxide.networking.CachingThreadedResolver": socket.AF_INET,
    "scrapy.resolver.CachingHostnameResolver": socket.AF_UNSPEC,
    "spideroxide.networking.CachingHostnameResolver": socket.AF_UNSPEC,
}
_SCRAPY_CONTEXT_FACTORIES = {
    "SENTINEL": False,
    "scrapy.core.downloader.contextfactory.ScrapyClientContextFactory": False,
    "spideroxide.networking.ScrapyClientContextFactory": False,
    "scrapy.core.downloader.contextfactory.BrowserLikeContextFactory": True,
    "spideroxide.networking.BrowserLikeContextFactory": True,
}


class ScrapyClientContextFactory:
    """Scrapy-compatible permissive TLS context marker."""


class BrowserLikeContextFactory:
    """Scrapy-compatible verifying TLS context marker."""


def verifies_certificates(settings: Settings) -> bool:
    factory = str(
        settings.get(
            "DOWNLOADER_CLIENTCONTEXTFACTORY",
            "SENTINEL",
        )
    )
    try:
        factory_verifies = _SCRAPY_CONTEXT_FACTORIES[factory]
    except KeyError as error:
        raise ValueError(
            "DOWNLOADER_CLIENTCONTEXTFACTORY must select "
            "ScrapyClientContextFactory or BrowserLikeContextFactory"
        ) from error
    return factory_verifies or settings.getbool("DOWNLOAD_VERIFY_CERTIFICATES", False)


def _tls_version(settings: Settings, name: str) -> ssl.TLSVersion | None:
    value = settings.get(name)
    if value is None:
        return None
    try:
        return _TLS_VERSIONS[str(value)]
    except KeyError as error:
        raise ValueError(f"Unknown {name} value: {value}") from error


def make_ssl_context(settings: Settings) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if verifies_certificates(settings):
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_default_certs()
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    minimum = _tls_version(settings, "DOWNLOAD_TLS_MIN_VERSION")
    maximum = _tls_version(settings, "DOWNLOAD_TLS_MAX_VERSION")
    if minimum is not None:
        context.minimum_version = minimum
    if maximum is not None:
        context.maximum_version = maximum
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("DOWNLOAD_TLS_MIN_VERSION cannot exceed DOWNLOAD_TLS_MAX_VERSION")

    ciphers = settings.get("DOWNLOADER_CLIENT_TLS_CIPHERS", "DEFAULT")
    if ciphers:
        try:
            context.set_ciphers(str(ciphers))
        except ssl.SSLError as error:
            raise ValueError(f"invalid DOWNLOADER_CLIENT_TLS_CIPHERS value: {ciphers}") from error
    certificate = settings.get("DOWNLOADER_CLIENT_CERTIFICATE")
    private_key = settings.get("DOWNLOADER_CLIENT_KEY")
    key_password = settings.get("DOWNLOADER_CLIENT_KEY_PASSWORD")
    if private_key is not None and certificate is None:
        raise ValueError("DOWNLOADER_CLIENT_KEY requires DOWNLOADER_CLIENT_CERTIFICATE")
    if certificate is not None:
        context.load_cert_chain(
            str(certificate),
            None if private_key is None else str(private_key),
            None if key_password is None else str(key_password),
        )
    return context


def client_identity_pem(settings: Settings) -> bytes | None:
    certificate = settings.get("DOWNLOADER_CLIENT_CERTIFICATE")
    if certificate is None:
        return None
    if settings.get("DOWNLOADER_CLIENT_KEY_PASSWORD") is not None:
        raise ValueError("Rust downloader does not support encrypted DOWNLOADER_CLIENT_KEY files")
    private_key = settings.get("DOWNLOADER_CLIENT_KEY")
    certificate_data = Path(str(certificate)).read_bytes()
    if private_key is None:
        return certificate_data
    return certificate_data + b"\n" + Path(str(private_key)).read_bytes()


def bind_host(settings: Settings) -> str | None:
    value = settings.get("DOWNLOAD_BIND_ADDRESS")
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (tuple, list)) and len(value) == 2 and isinstance(value[0], str):
        if int(value[1]) != 0:
            logger.warning(
                "DOWNLOAD_BIND_ADDRESS port %s is ignored; only the local address is supported",
                value[1],
            )
        return value[0] or None
    raise ValueError("DOWNLOAD_BIND_ADDRESS must be a host string or (host, port) pair")


def resolver_family(settings: Settings) -> int:
    value = str(
        settings.get(
            "DNS_RESOLVER",
            settings.get(
                "TWISTED_DNS_RESOLVER",
                "scrapy.resolver.CachingThreadedResolver",
            ),
        )
    )
    try:
        return _RESOLVERS[value]
    except KeyError as error:
        raise ValueError(
            "DNS_RESOLVER must select CachingThreadedResolver or CachingHostnameResolver"
        ) from error


class CachingResolver:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.getbool("DNSCACHE_ENABLED", True)
        self.limit = settings.getint("DNSCACHE_SIZE", 10000)
        self.timeout = settings.getfloat("DNS_TIMEOUT", 60.0)
        self.family = resolver_family(settings)
        if self.limit < 0:
            raise ValueError("DNSCACHE_SIZE cannot be negative")
        if self.timeout <= 0:
            raise ValueError("DNS_TIMEOUT must be positive")
        self._cache: OrderedDict[str, tuple[str, ...]] = OrderedDict()

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return (host,)

        if self.enabled and host in self._cache:
            addresses = self._cache.pop(host)
            self._cache[host] = addresses
            return addresses

        loop = asyncio.get_running_loop()
        records = await asyncio.wait_for(
            loop.getaddrinfo(
                host,
                port,
                family=self.family,
                type=socket.SOCK_STREAM,
            ),
            timeout=self.timeout,
        )
        addresses = tuple(dict.fromkeys(record[4][0] for record in records))
        if not addresses:
            raise socket.gaierror(f"no addresses found for {host}")
        if self.enabled and self.limit:
            self._cache[host] = addresses
            while len(self._cache) > self.limit:
                self._cache.popitem(last=False)
        return addresses

    @property
    def cache_size(self) -> int:
        return len(self._cache)


class ResolvingNetworkBackend(AsyncNetworkBackend):
    def __init__(self, resolver: CachingResolver) -> None:
        self.resolver = resolver
        self.backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        addresses = await self.resolver.resolve(host, port)
        last_error: OSError | None = None
        for address in addresses:
            try:
                return await self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except OSError as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise socket.gaierror(f"no addresses found for {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        return await self.backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class CachingThreadedResolver(CachingResolver):
    pass


class CachingHostnameResolver(CachingResolver):
    pass


def install_httpx_resolver(transport: Any, resolver: CachingResolver) -> None:
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise ValueError("HTTP transport does not support configurable DNS resolution")
    pool._network_backend = ResolvingNetworkBackend(resolver)


def tls_debug_info(response: Any) -> tuple[str | None, bytes | None, str | None]:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None, None, None
    server_address = stream.get_extra_info("server_addr")
    ip_address = None if server_address is None else str(server_address[0])
    ssl_object = stream.get_extra_info("ssl_object")
    if not isinstance(ssl_object, ssl.SSLObject):
        return ip_address, None, None
    certificate = ssl_object.getpeercert(binary_form=True)
    cipher = ssl_object.cipher()
    detail = f"protocol {ssl_object.version()}, cipher {None if cipher is None else cipher[0]}"
    return ip_address, certificate, detail
