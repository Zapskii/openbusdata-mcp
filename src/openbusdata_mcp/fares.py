"""BODS fares (NeTEx) price extraction.

BODS fares datasets are NeTEx XML documents, sometimes delivered as zip
containers (like timetables).

Where these element paths come from
-----------------------------------
They were derived from the published **BODS NeTEx Fares Profile v0.4** and the
CEN **NeTEx XSDs** — *not* from a captured live document. The live probe is
still pending: OPENBUS_API_KEY was unset in the environment this was written
in, so nothing below has been checked against a real BODS download.

The structure the profile mandates (profile section 6.3.3 Table 11, section
7.2 Table 27, and its Appendix II coded example):

  * a zone-to-zone price is a concrete price element, typically
    ``GeographicalIntervalPrice``, nested in a ``PriceGroup``'s ``members``;
  * that ``PriceGroup`` is reached **by reference**, not by containment, from a
    ``DistanceMatrixElement``: ``<priceGroups><PriceGroupRef ref="..."/></priceGroups>``;
  * ``StartTariffZoneRef`` / ``EndTariffZoneRef`` are direct children of that
    ``DistanceMatrixElement``.

So a zone price's zones are obtained by resolving an in-document reference:
price -> enclosing ``PriceGroup`` id -> the ``DistanceMatrixElement``(s) whose
``PriceGroupRef`` names that id -> their zone refs. One ``PriceGroup`` may be
shared by several ``DistanceMatrixElement``s — that is what a price band is
for — and then a single price legitimately applies to several O/D pairs. Each
pair is emitted as its own row, so zones are never crossed between pairs (a
row is never a cross-product of one pair's start zone with another's end zone).

A product price (``FareProductPrice``, nested in a ``PreassignedFareProduct``)
carries no zones, and the base NeTEx schema additionally allows a
``DistanceMatrixElementPrice`` to be nested directly inside its
``DistanceMatrixElement`` — that shape is read by the ancestor walk.

Prices declared in one place and *listed* by a ``PriceGroup`` through
``FarePriceRef`` are also resolved. A price that names no zones by any of these
routes is reported with empty zone lists rather than an invented attribution.

Scope note: this extracts the published price points (amount, currency,
zones, enclosing product name). Mapping tariff zones onto geographic stops is
a separate dataset concern and is deliberately not attempted here.
"""

import io
import zipfile
from collections import defaultdict

from defusedxml import ElementTree as SafeET

# Do NOT put "FarePrice" in this set. In the NeTEx XSD it is declared abstract:
#
#   <xsd:element name="FarePrice" abstract="true" substitutionGroup="FarePrice_"/>
#
# quoted verbatim from netex_part_3/part3_fares/netex_farePrice_version.xsd,
# checked at the v1.2 tag and again at v2.0.0 of the CEN NeTEx schema. The
# abstract="true" is identical at both versions — v2.0.0 changes only the
# substitutionGroup value (FarePrice_ at v1.2, FarePrice_Dummy at v2.0.0) — so
# the abstractness is a stable design property rather than a recent change.
# Abstract elements never appear in an instance document, so matching on
# "FarePrice" can only ever silently miss — the concrete substitutions below
# are what real documents actually carry. A later reader "simplifying" this
# back to the abstract head would re-break extraction.
PRICE_TAGS = {"FareProductPrice", "DistanceMatrixElementPrice",
              "CappingRulePrice", "GeographicalIntervalPrice"}
AMOUNT_TAGS = {"Amount", "PriceAmount", "amount"}
ZONE_TAGS = {"StartTariffZoneRef", "EndTariffZoneRef", "TariffZoneRef"}
PRODUCT_TAGS = {"PreassignedFareProduct", "FareProduct", "FareTable"}
PRICE_GROUP_TAG = "PriceGroup"
MATRIX_ELEMENT_TAG = "DistanceMatrixElement"


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


def _zone_refs(el):
    """Zone refs among the DIRECT children of el: (start, end, zones)."""
    start, end, zones = [], [], []
    for c in list(el):
        name = _local(c)
        if name not in ZONE_TAGS:
            continue
        ref = c.get("ref") or c.get("id") or (c.text or "").strip()
        if name == "StartTariffZoneRef":
            start.append(ref)
        elif name == "EndTariffZoneRef":
            end.append(ref)
        else:
            zones.append(ref)
    return start, end, zones


def _ancestor_zone_refs(el, parents):
    """Zone refs carried by el itself or by its nearest ancestor carrying any.

    The walk starts AT el, not at its parent: a zone ref that is a direct child
    of the price element is otherwise invisible.
    """
    cur = el
    while cur is not None:
        refs = _zone_refs(cur)
        if any(refs):
            return refs
        cur = parents.get(cur)
    return [], [], []


def _product_name(el, parents):
    """Name of the nearest enclosing product/table element."""
    cur = parents.get(el)
    while cur is not None:
        if _local(cur) in PRODUCT_TAGS:
            nm = next((c.text for c in list(cur)
                       if _local(c) in ("Name", "name") and c.text), None)
            return nm.strip() if nm else _local(cur)
        cur = parents.get(cur)
    return None


def _pairs_by_price_group(root):
    """PriceGroup id -> [(start, end, zones), ...], one entry per
    DistanceMatrixElement referencing that group."""
    out = defaultdict(list)
    for el in root.iter():
        if _local(el) != MATRIX_ELEMENT_TAG:
            continue
        refs = _zone_refs(el)
        if not any(refs):
            continue
        for d in el.iter():
            if _local(d) == "PriceGroupRef":
                ref = d.get("ref") or (d.text or "").strip()
                if ref:
                    out[ref].append(refs)
    return out


def _group_of_listed_price(root):
    """Price id -> PriceGroup id, for prices a PriceGroup lists by FarePriceRef
    instead of holding them in its members."""
    out = {}
    for el in root.iter():
        if _local(el) != PRICE_GROUP_TAG:
            continue
        gid = el.get("id")
        if not gid:
            continue
        for d in el.iter():
            if _local(d) == "FarePriceRef":
                ref = d.get("ref") or (d.text or "").strip()
                if ref:
                    out[ref] = gid
    return out


def _enclosing_group_id(el, parents, listed_group):
    """The id of the PriceGroup that governs this price, if any."""
    cur = el
    while cur is not None:
        if _local(cur) == PRICE_GROUP_TAG:
            return cur.get("id")
        cur = parents.get(cur)
    return listed_group.get(el.get("id"))


def _row(amount, currency, start, end, zones, product) -> dict:
    return {"amount": amount, "currency": currency,
            "start_zones": start, "end_zones": end,
            "zones": zones, "product": product}


def parse_fare_prices(content: bytes) -> list[dict]:
    """Extract published price points from a NeTEx fares document.

    For each price element: amount from an Amount/PriceAmount child (or the
    element's own amount attribute — entries without any amount are skipped);
    currency from the currency attribute, defaulting to GBP (the profile sets
    FareFrame/FrameDefaults/DefaultCurrency to GBP).

    Zones come from the element itself or its nearest ancestor carrying any
    zone ref, split into start_zones / end_zones where the NeTEx tags
    distinguish them (generic TariffZoneRef lands in "zones"). When no ancestor
    carries zone refs, the enclosing PriceGroup is followed to any
    DistanceMatrixElement that references it, and one row is emitted per
    referencing O/D pair. The enclosing product/table Name becomes "product".
    """
    root = SafeET.fromstring(extract_xml_bytes(content))
    parents = {c: p for p in root.iter() for c in list(p)}
    pairs_by_group = _pairs_by_price_group(root)
    listed_group = _group_of_listed_price(root)

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
        currency = el.get("currency") or "GBP"
        product = _product_name(el, parents)
        start, end, zones = _ancestor_zone_refs(el, parents)
        if any((start, end, zones)):
            out.append(_row(amount, currency, start, end, zones, product))
            continue
        group_id = _enclosing_group_id(el, parents, listed_group)
        pairs = pairs_by_group.get(group_id) if group_id else None
        if pairs:
            for pair_start, pair_end, pair_zones in pairs:
                out.append(_row(amount, currency, list(pair_start),
                                list(pair_end), list(pair_zones), product))
        else:
            out.append(_row(amount, currency, [], [], [], product))
    return out
