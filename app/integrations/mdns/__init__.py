"""HomeKit device discovery via mDNS / DNS-SD (D.3).

Passively browses `_hap._tcp` (Wi-Fi/Ethernet) and `_hap._udp` (Thread) HAP
advertisements on the LAN and projects discovered accessories into the registry
as ``homekit`` devices. Requires host networking (see TECHNICAL_DESIGN §9d).
"""

from .client import MdnsClient, parse_hap_service, project_discovered

__all__ = ["MdnsClient", "parse_hap_service", "project_discovered"]
