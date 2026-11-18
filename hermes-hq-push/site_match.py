"""A 1Password login fills on its whole site, as 1Password itself does (the owner, 2026-11-16: the bots must be able
to use every password the key reads).

Hermes fills a vault login only on the exact origin saved on it, so a 1Password item saved for ``https://example.com``
was refused on ``https://www.example.com`` and on ``https://account.example.com``. This widens that one check, for
1Password items only (handles ``op:``): the page may be on any host of the saved site, the site being the saved host's
registrable domain (``account.example.com`` -> ``example.com``; ``shop.example.co.uk`` -> ``example.co.uk``). The scheme
and port must still match (an https login never fills on http), and hosts under a shared-hosting suffix
(``*.github.io``, ``*.vercel.app``, ...) or an IP address keep the exact match, because there each name belongs to
someone else. Everything else in Hermes's fill stays: the in-page re-check that the page didn't move, the redaction,
and what the result reports.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

MARK = "_hq_site_match"
PREFIX = "op:"

# Second-level labels that sit under a two-letter country code as a public suffix (``co.uk``, ``com.au``, ...).
_COUNTRY_SECOND = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nic", "mil", "ltd", "plc",
                   "nom", "sch", "info", "biz"}
# Suffixes where every subdomain is a different owner: a login saved for one never fills on another.
_SHARED = {"github.io", "gitlab.io", "vercel.app", "netlify.app", "pages.dev", "workers.dev", "herokuapp.com",
           "appspot.com", "web.app", "firebaseapp.com", "blogspot.com", "wordpress.com", "myshopify.com",
           "azurewebsites.net", "cloudfront.net", "amazonaws.com", "onrender.com", "fly.dev", "glitch.me",
           "repl.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com", "surge.sh", "bitbucket.io", "deno.dev",
           "railway.app", "up.railway.app", "web.core.windows.net", "s3.amazonaws.com", "translate.goog"}


def _parts(origin: str):
    try:
        u = urlsplit(origin)
        host = (u.hostname or "").lower().rstrip(".")
        port = u.port
    except ValueError:
        return None
    if not u.scheme or not host:
        return None
    return u.scheme.lower(), host, port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def site(host: str) -> str | None:
    """The registrable domain of ``host`` ("example.com", "example.co.uk"), or None where only the exact host may
    match (an IP, a single label, a shared-hosting name)."""
    host = host.lower().rstrip(".")
    if _is_ip(host) or "." not in host:
        return None
    labels = host.split(".")
    if any(host == s or host.endswith("." + s) for s in _SHARED):
        return None
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _COUNTRY_SECOND:
        return ".".join(labels[-3:])
    if len(labels) == 2 and len(labels[-1]) == 2 and labels[-2] in _COUNTRY_SECOND:
        return None   # "co.uk" itself is a suffix, not a site
    return ".".join(labels[-2:])


def covers(saved_origin: str, page_origin: str) -> bool:
    """Whether a login saved for ``saved_origin`` may fill on ``page_origin``: the same origin, or another host of
    the same site with the same scheme and port."""
    if saved_origin == page_origin:
        return True
    saved, page = _parts(saved_origin), _parts(page_origin)
    if not saved or not page or saved[0] != page[0] or saved[2] != page[2]:
        return False
    if saved[0] not in ("https", "http"):
        return False
    wide = site(saved[1])
    return wide is not None and site(page[1]) == wide


def widened(vault, original):
    """Hermes's ``_bound_page_origin(task_id, meta, probe_kind)``: unchanged, except that a 1Password item's
    "origin_mismatch" becomes the page's origin when the page is on the item's site."""
    def bound(task_id, meta, probe_kind):
        result = original(task_id, meta, probe_kind)
        if not isinstance(result, dict) or result.get("error_type") != "origin_mismatch":
            return result
        if not str(getattr(meta, "id", "")).startswith(PREFIX):
            return result
        page = vault._current_page_origin(task_id)
        if not page:
            return result
        from tools.browser_vault_context import saved_origins
        if any(covers(saved, page) for saved in saved_origins(meta)):
            return page
        return result

    setattr(bound, MARK, True)
    bound.__wrapped__ = original
    bound.__doc__ = original.__doc__
    return bound


def install() -> str:
    """Once per process: "installed", "already", or "absent" (no vault tool, or one shaped differently)."""
    try:
        import tools.browser_vault_tool as vault
    except Exception:
        return "absent"
    original = getattr(vault, "_bound_page_origin", None)
    if not callable(original) or not callable(getattr(vault, "_current_page_origin", None)):
        return "absent"
    if getattr(original, MARK, False):
        return "already"
    try:
        from tools.browser_vault_context import saved_origins  # noqa: F401
    except Exception:
        return "absent"
    vault._bound_page_origin = widened(vault, original)
    return "installed"
