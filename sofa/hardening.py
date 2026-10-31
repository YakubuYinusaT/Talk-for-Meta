"""Secrets that ship with the code are known to everyone who reads it. On an address people can reach they must never be the secret that guards anything."""

import logging
import secrets

log = logging.getLogger("sofa.hardening")

# setting -> the value it has when nobody set it
KNOWN_DEFAULTS = {"at_callback_secret": "dev-secret", "admin_token": "dev-admin-token", "provider_token": "dev-provider-token", "vendor_token": "dev-vendor-token"}


def public_address(settings) -> bool:
    return str(settings.public_base_url).startswith("https://")


def replace_known_defaults(settings) -> list[str]:
    """On an https address, any secret still at its published default is replaced by a random one for this run, so nothing is guarded by a known value. The pages it
    guards then stay shut until the real one is set in the settings. Returns the names that were replaced."""
    if not public_address(settings):
        return []
    replaced = []
    for name, default in KNOWN_DEFAULTS.items():
        if getattr(settings, name) == default:
            setattr(settings, name, secrets.token_urlsafe(24))
            replaced.append(name.upper())
    if replaced:
        log.error("these settings are still at the value published with the code, so they were replaced by a random one for this run: %s. Set them in .env.", ", ".join(replaced))
    return replaced
