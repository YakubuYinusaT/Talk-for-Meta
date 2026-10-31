"""Starting the synthetic providers.

Each provider system (a state health network, a private clinic group, a pharmacy chain, a cancer centre) runs on its own with its own records and its own
secret, as a web app mounted at /synthetic/<slug>. Talk reaches each one through sofa/health/links.py; nothing in Talk is written for any one of them.
"""

import logging
from pathlib import Path


from . import data, provider
from .store import HospitalStore

log = logging.getLogger("sofa.health")
KINDS = {"state-phc": "clinics", "eko-care": "clinics", "everwell": "pharmacies", "onc-centre": "clinics"}   # what each provider system offers
DOMAIN_OF = {slug: "health_" + slug.replace("-", "_") for slug in data.NETWORKS}


def create_apps(settings) -> dict:
    """One web app per provider system, each with its own database file and its own secret, to be mounted at /synthetic/<slug>."""
    folder = Path(settings.hospital_db_dir or settings.storage_dir)
    apps = {}
    for slug, network in data.NETWORKS.items():
        path = ":memory:" if str(folder) == ":memory:" else str(folder / f"provider_{slug}.db")
        if path != ":memory:":
            folder.mkdir(parents=True, exist_ok=True)
        apps[slug] = provider.create_provider_app(network, settings.provider_secret(slug), HospitalStore(path, facilities=network.facilities), settings.hospital_desk_key)
    return apps

