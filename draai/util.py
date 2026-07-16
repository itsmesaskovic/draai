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
