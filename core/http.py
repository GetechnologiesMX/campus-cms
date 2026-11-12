import ipaddress


def client_ip(request):
    """
    The browser's IP, for rate limits. Campus' server calls this API and
    replaces X-Forwarded-For with the client IP; a call that comes in through
    Caddy gets the caller's IP there. The last entry is the one the nearest
    proxy wrote, so that's the one we take.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[-1].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
