"""Small generic helpers shared across modules."""


def hms_to_sec(t):
    try:
        h, m, s = t.split(":")
        return int(h) * 3600 + int(m) * 60 + int(float(s))
    except Exception:
        return 0


def sec_to_hms(n):
    n = max(0, int(n))
    return "%d:%02d:%02d" % (n // 3600, (n % 3600) // 60, n % 60)


def parse_ipv4_addrs(text):
    """Pull LAN IPv4 addresses out of `ifconfig` / `ip -4 addr` output.
    Skips loopback, link-local and point-to-point interfaces (VPN tunnels
    such as ipsec/utun/ppp): discovery must stay on the local network.
    Order preserved, no duplicates."""
    import re
    out = []
    # Split into per-interface blocks: a block starts at a non-indented line.
    for block in re.split(r"\n(?=\S)", text):
        head = block.split("\n", 1)[0]
        if "POINTOPOINT" in head or "LOOPBACK" in head:
            continue
        for ip in re.findall(r"\binet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", block):
            if ip.startswith("127.") or ip.startswith("169.254.") or ip in out:
                continue
            out.append(ip)
    return out


def local_ipv4_addrs():
    """Every LAN IPv4 address on this machine (stdlib only). Multicast discovery
    must be sent on each LAN one: with a VPN up, the default multicast route
    points into the tunnel and never reaches the speakers on the Wi-Fi."""
    import subprocess
    for cmd in (["/sbin/ifconfig"], ["ifconfig"], ["ip", "-4", "addr"]):
        try:
            text = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=2).stdout
        except Exception:
            continue
        addrs = parse_ipv4_addrs(text)
        if addrs:
            return addrs
    return []
