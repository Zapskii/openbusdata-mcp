"""BODS fares (NeTEx) price extraction.

BODS fares datasets are NeTEx XML documents, sometimes delivered as zip
containers (like timetables). Parsing is by local element name — BODS NeTEx
namespace URIs vary — with these constants at the top so a live-structure
change is a one-place fix (record the real names here when the probe runs):

  ...FareTable/Name, PreassignedFareProduct/Name,
  PreassignedFare > StartTariffZoneRef / EndTariffZoneRef / TariffZoneRef
  (ref attributes) > FarePrice(currency) > Amount.

Live probe pending: OPENBUS_API_KEY was unset in the dispatch environment, so
the live fares endpoint was not queried and the element names above are the
constants this module parses against, not names confirmed against live data.

Scope note: this extracts the published price points (amount, currency,
zones, enclosing product name). Mapping tariff zones onto geographic stops
is a separate dataset concern and is deliberately not attempted here.
"""

import io
import zipfile

from defusedxml import ElementTree as SafeET

PRICE_TAGS = {"FarePrice", "farePrice"}
AMOUNT_TAGS = {"Amount", "PriceAmount", "amount"}
ZONE_TAGS = {"StartTariffZoneRef", "EndTariffZoneRef", "TariffZoneRef"}
PRODUCT_TAGS = {"PreassignedFareProduct", "FareProduct", "FareTable"}


def extract_xml_bytes(content: bytes) -> bytes:
    """Unzip PK-magic payloads (first .xml member), else pass through."""
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for name in z.namelist():
                if name.lower().endswith(".xml"):
                    return z.read(name)
        raise ValueError("zip archive contains no .xml member")
    return content


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def parse_fare_prices(content: bytes) -> list:
    """Extract published price points from a NeTEx fares document.

    For each price element: amount from an Amount/PriceAmount child (or the
    element's own amount attribute — entries without any amount are skipped);
    currency from the currency attribute, defaulting to GBP. Zone refs are
    found by walking up ancestors — the first ancestor level that carries
    any zone ref wins — and are split into start_zones / end_zones where the
    NeTEx tags distinguish them (generic TariffZoneRef lands in "zones").
    The enclosing product/table Name becomes "product".
    """
    root = SafeET.fromstring(extract_xml_bytes(content))
    parents = {c: p for p in root.iter() for c in list(p)}
    out = []
    for el in root.iter():
        if _local(el) not in PRICE_TAGS:
            continue
        amount = el.get("amount")
        if not amount:
            for child in el.iter():
                if _local(child) in AMOUNT_TAGS and child.text and child.text.strip():
                    amount = child.text.strip()
                    break
        if not amount:
            continue
        start_zones, end_zones, zones = [], [], []
        cur = parents.get(el)
        while cur is not None and not (start_zones or end_zones or zones):
            for c in list(cur):
                if _local(c) in ZONE_TAGS:
                    ref = c.get("ref") or c.get("id") or (c.text or "").strip()
                    if _local(c) == "StartTariffZoneRef":
                        start_zones.append(ref)
                    elif _local(c) == "EndTariffZoneRef":
                        end_zones.append(ref)
                    else:
                        zones.append(ref)
            cur = parents.get(cur)
        product = None
        cur = parents.get(el)
        while cur is not None:
            if _local(cur) in PRODUCT_TAGS:
                nm = next((c.text for c in list(cur)
                           if _local(c) in ("Name", "name") and c.text), None)
                product = nm.strip() if nm else _local(cur)
                break
            cur = parents.get(cur)
        out.append({"amount": amount,
                    "currency": el.get("currency") or "GBP",
                    "start_zones": start_zones, "end_zones": end_zones,
                    "zones": zones, "product": product})
    return out
