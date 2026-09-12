# test_fares.py
import io
import zipfile

import pytest
from defusedxml.ElementTree import ParseError

from openbusdata_mcp.fares import extract_xml_bytes, parse_fare_prices

# Profile-shaped NeTEx: the structure the published BODS NeTEx Fares Profile
# v0.4 actually mandates (section 6.3.3 Table 11, section 7.2 Table 27,
# Appendix II), not the pre-fix fixture shape. Three price elements, one per
# route by which a price can be reached:
#   1. GeographicalIntervalPrice nested in a PriceGroup that a
#      DistanceMatrixElement references through priceGroups/PriceGroupRef
#      (the zone-to-zone case -- zones need reference resolution);
#   2. FareProductPrice nested in a PreassignedFareProduct (product priced,
#      no zones);
#   3. DistanceMatrixElementPrice nested directly inside its
#      DistanceMatrixElement (zones reachable by ancestor walk).
NETEX = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects>
  <CompositeFrame>
   <FaresFrame>
    <FareFrame version="1.0" id="FF-PRICE">
     <Name>Prices for adult single</Name>
     <FrameDefaults><DefaultCurrency>GBP</DefaultCurrency></FrameDefaults>
     <priceGroups>
      <PriceGroup version="1.0" id="price_band_1.20">
       <members>
        <GeographicalIntervalPrice version="1.0" id="price_band_1.20@adult">
         <Amount>1.20</Amount>
        </GeographicalIntervalPrice>
       </members>
      </PriceGroup>
     </priceGroups>
    </FareFrame>
    <FareFrame version="1.0" id="FF-PRODUCT">
     <fareProducts>
      <PreassignedFareProduct version="1.0" id="Trip@adult_single">
       <Name>Zone 1-2 single</Name>
       <prices>
        <FareProductPrice version="1.0" id="Trip@adult_single@price">
         <Amount>2.50</Amount>
        </FareProductPrice>
       </prices>
      </PreassignedFareProduct>
     </fareProducts>
     <fareStructureElements>
      <FareStructureElement version="1.0" id="Tariff@single">
       <Name>O/D pairs</Name>
       <distanceMatrixElements>
        <DistanceMatrixElement version="1.0" id="Z1+Z2">
         <priceGroups>
          <PriceGroupRef version="1.0" ref="price_band_1.20"/>
         </priceGroups>
         <StartTariffZoneRef version="1.0" ref="Z1"/>
         <EndTariffZoneRef version="1.0" ref="Z2"/>
        </DistanceMatrixElement>
        <DistanceMatrixElement version="1.0" id="Z5+Z6">
         <StartTariffZoneRef version="1.0" ref="Z5"/>
         <EndTariffZoneRef version="1.0" ref="Z6"/>
         <prices>
          <DistanceMatrixElementPrice version="1.0" id="Z5+Z6@price"
                                      currency="GBP">
           <Amount>3.00</Amount>
          </DistanceMatrixElementPrice>
         </prices>
        </DistanceMatrixElement>
       </distanceMatrixElements>
      </FareStructureElement>
     </fareStructureElements>
    </FareFrame>
   </FaresFrame>
  </CompositeFrame>
 </dataObjects>
</PublicationDelivery>'''

# Two O/D pairs sharing one price band: the same PriceGroup is referenced by
# both DistanceMatrixElements, exactly as the profile's PriceGroup mechanism
# is designed to be reused.
SHARED_BAND = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects>
  <CompositeFrame>
   <FaresFrame>
    <FareFrame version="1.0" id="FF-PRICE">
     <priceGroups>
      <PriceGroup version="1.0" id="price_band_1.20">
       <members>
        <GeographicalIntervalPrice version="1.0" id="price_band_1.20@adult">
         <Amount>1.20</Amount>
        </GeographicalIntervalPrice>
       </members>
      </PriceGroup>
     </priceGroups>
    </FareFrame>
    <FareFrame version="1.0" id="FF-PRODUCT">
     <fareStructureElements>
      <FareStructureElement version="1.0" id="Tariff@single">
       <distanceMatrixElements>
        <DistanceMatrixElement version="1.0" id="Z1+Z2">
         <priceGroups>
          <PriceGroupRef version="1.0" ref="price_band_1.20"/>
         </priceGroups>
         <StartTariffZoneRef version="1.0" ref="Z1"/>
         <EndTariffZoneRef version="1.0" ref="Z2"/>
        </DistanceMatrixElement>
        <DistanceMatrixElement version="1.0" id="Z5+Z6">
         <priceGroups>
          <PriceGroupRef version="1.0" ref="price_band_1.20"/>
         </priceGroups>
         <StartTariffZoneRef version="1.0" ref="Z5"/>
         <EndTariffZoneRef version="1.0" ref="Z6"/>
        </DistanceMatrixElement>
       </distanceMatrixElements>
      </FareStructureElement>
     </fareStructureElements>
    </FareFrame>
   </FaresFrame>
  </CompositeFrame>
 </dataObjects>
</PublicationDelivery>'''


def test_parse_fare_prices_profile_shape():
    prices = parse_fare_prices(NETEX)
    assert len(prices) == 3

    # 1. Zone band price: amount from the nested Amount, zones resolved by
    #    following the DistanceMatrixElement's PriceGroupRef.
    assert prices[0] == {"amount": "1.20", "currency": "GBP",
                         "start_zones": ["Z1"], "end_zones": ["Z2"],
                         "zones": [], "product": None}

    # 2. Product price: enclosing product name, no zones.
    assert prices[1] == {"amount": "2.50", "currency": "GBP",
                         "start_zones": [], "end_zones": [], "zones": [],
                         "product": "Zone 1-2 single"}

    # 3. Price nested in its own DistanceMatrixElement: zones found by walking
    #    ancestors; currency from the element's own attribute.
    assert prices[2] == {"amount": "3.00", "currency": "GBP",
                         "start_zones": ["Z5"], "end_zones": ["Z6"],
                         "zones": [], "product": None}


def test_parse_fare_prices_reads_zone_refs_on_the_price_element_itself():
    """The walk must start AT the price element, not at its parent.

    A zone ref that is a direct child of the price element is invisible to a
    walk that begins one level up.
    """
    doc = NETEX.replace(
        b'<Amount>3.00</Amount>',
        b'<Amount>3.00</Amount>\n'
        b'           <StartTariffZoneRef version="1.0" ref="Z7"/>\n'
        b'           <EndTariffZoneRef version="1.0" ref="Z8"/>')
    prices = parse_fare_prices(doc)
    assert prices[2]["start_zones"] == ["Z7"]
    assert prices[2]["end_zones"] == ["Z8"]


def test_parse_fare_prices_shared_price_band_keeps_od_pairs_separate():
    """A band shared by two O/D pairs yields one row per pair, never a
    cross-product of the two pairs' zones."""
    band = [p for p in parse_fare_prices(SHARED_BAND)
            if p["amount"] == "1.20"]
    assert len(band) == 2
    assert [(p["start_zones"], p["end_zones"]) for p in band] == \
        [(["Z1"], ["Z2"]), (["Z5"], ["Z6"])]


def test_parse_fare_prices_skips_amountless_entries():
    bare = NETEX.replace(b"<Amount>1.20</Amount>", b"")
    prices = parse_fare_prices(bare)
    assert [p["amount"] for p in prices] == ["2.50", "3.00"]


def test_parse_fare_prices_ignores_abstract_fareprice_element():
    """FarePrice is abstract in the NeTEx XSD, so a conformant document never
    contains one; a stray one must not be picked up as a price."""
    doc = NETEX.replace(b"<GeographicalIntervalPrice",
                        b"<FarePrice").replace(
        b"</GeographicalIntervalPrice>", b"</FarePrice>")
    assert [p["amount"] for p in parse_fare_prices(doc)] == ["2.50", "3.00"]


def test_extract_xml_bytes_unzips():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fares.xml", NETEX)
    assert parse_fare_prices(buf.getvalue())[0]["amount"] == "1.20"


def test_extract_xml_bytes_passthrough():
    assert extract_xml_bytes(NETEX) == NETEX


def test_extract_xml_bytes_rejects_malformed_zip():
    with pytest.raises(zipfile.BadZipFile):
        extract_xml_bytes(b"PK\x03\x04not-really-a-zip")


def test_extract_xml_bytes_rejects_zip_without_xml_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fares.csv", b"not xml")
    with pytest.raises(ValueError, match=r"no \.xml member"):
        extract_xml_bytes(buf.getvalue())


def test_parse_fare_prices_rejects_malformed_xml():
    with pytest.raises(ParseError):
        parse_fare_prices(b"<PublicationDelivery><unclosed>")
