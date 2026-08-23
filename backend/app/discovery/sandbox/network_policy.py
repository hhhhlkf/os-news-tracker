"""Domain and address policy shared by the host and the controlled egress proxy."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


BLOCKED_METADATA_ADDRESSES = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}
FORBIDDEN_PACKAGE_REPOSITORIES = (
    "pypi.org",
    "pythonhosted.org",
    "anaconda.org",
    "repo.anaconda.com",
    "repo.continuum.io",
    "conda-forge.org",
    "download.pytorch.org",
    "pypi.tuna.tsinghua.edu.cn",
    "mirrors.aliyun.com",
    "mirrors.cloud.tencent.com",
    "mirrors.ustc.edu.cn",
    "registry.npmjs.org",
    "rubygems.org",
    "packagist.org",
)
NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")
IPV4_COMPATIBLE = ipaddress.ip_network("::/96")


class NetworkPolicyError(ValueError):
    """A target escaped the connector's approved public-domain policy."""


def normalize_hostname(value: str) -> str:
    hostname = value.strip().rstrip(".").lower()
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise NetworkPolicyError("invalid target hostname") from exc


def domain_is_allowed(hostname: str, allowed_domains: tuple[str, ...]) -> bool:
    """Require an exact Manifest domain; parent-domain wildcarding is forbidden."""
    normalized = normalize_hostname(hostname)
    return not is_package_repository(normalized) and normalized in allowed_domains


def is_package_repository(hostname: str) -> bool:
    """Package installation endpoints stay forbidden even when a Manifest declares them."""
    normalized = normalize_hostname(hostname)
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in FORBIDDEN_PACKAGE_REPOSITORIES
    )


def validate_public_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve a host and reject every non-global or metadata address."""
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise NetworkPolicyError(f"target DNS resolution failed: {hostname}") from exc
    addresses = tuple(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise NetworkPolicyError(f"target DNS resolution returned no addresses: {hostname}")
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address.split("%", 1)[0])
        if _address_is_forbidden(address):
            raise NetworkPolicyError(f"target resolved to a prohibited address: {address}")
    return addresses


def _address_is_forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        address in BLOCKED_METADATA_ADDRESSES
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return False
    embedded: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in NAT64_WELL_KNOWN or address in NAT64_LOCAL_USE or address in IPV4_COMPATIBLE:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return any(_address_is_forbidden(candidate) for candidate in embedded)


def validate_proxy_target(
    target: str,
    *,
    allowed_domains: tuple[str, ...],
    default_port: int | None = None,
) -> tuple[str, int, tuple[str, ...]]:
    """Validate an absolute HTTP URL or CONNECT host:port and pin safe addresses."""
    if "://" in target:
        parsed = urlsplit(target)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise NetworkPolicyError("proxy target must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise NetworkPolicyError("proxy target credentials are forbidden")
        hostname = normalize_hostname(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            if default_port is None:
                raise NetworkPolicyError("CONNECT target must include a port")
            host, port_text = target, str(default_port)
        hostname = normalize_hostname(host.strip("[]"))
        try:
            port = int(port_text)
        except ValueError as exc:
            raise NetworkPolicyError("proxy target port is invalid") from exc
    if port not in {80, 443}:
        raise NetworkPolicyError("only destination ports 80 and 443 are allowed")
    if is_package_repository(hostname):
        raise NetworkPolicyError(f"package repository access is forbidden at runtime: {hostname}")
    if not domain_is_allowed(hostname, allowed_domains):
        raise NetworkPolicyError(f"target domain is not approved by the Manifest: {hostname}")
    return hostname, port, validate_public_addresses(hostname, port)
