"""Small public site icons, cached by origin and returned as safe raster data."""
import base64
import io
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

from .web_page import checked_public_url, PublicRedirects, PublicHTTPHandler, PublicHTTPSHandler

_cache = OrderedDict()
_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)

def site_favicon(url):
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return {"icon": None}
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    with _lock:
        cached = _cache.get(origin)
        if cached and time.monotonic() < cached[0]:
            _cache.move_to_end(origin)
            return {"icon": cached[1]}
    icon = None
    if not _slots.acquire(timeout=1): return {"icon": None}
    try:
        address = checked_public_url(origin + "/favicon.ico")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirects(), PublicHTTPHandler(), PublicHTTPSHandler())
        request = urllib.request.Request(address, headers={"User-Agent": "Mozilla/5.0 (compatible; SaltySteakResearch/1.0)", "Accept": "image/*", "Accept-Encoding": "identity"})
        with opener.open(request, timeout=4) as response:
            payload = response.read(131073)
            if len(payload) > 131072: raise ValueError("Icon too large")
        from PIL import Image
        with Image.open(io.BytesIO(payload)) as image:
            if image.width > 1024 or image.height > 1024: raise ValueError("Icon dimensions too large")
            image.thumbnail((32, 32))
            output = io.BytesIO();image.convert("RGBA").save(output, format="PNG")
            icon = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception:
        icon = None
    finally:
        _slots.release()
    with _lock:
        _cache[origin] = (time.monotonic() + (3600 if icon else 60), icon)
        while len(_cache) > 128: _cache.popitem(last=False)
    return {"icon": icon}
