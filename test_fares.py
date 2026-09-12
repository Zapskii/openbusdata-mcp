# test_fares.py
import io
import zipfile

from openbusdata_mcp.fares import extract_xml_bytes, parse_fare_prices

NETEX = b'''<?xml version="1.0"?>
<PublicationDelivery xmlns="http://www.netex.org.uk/netex">
 <dataObjects>
  <CompositeFrame>
   <FaresFrame>
    <FareTable id="FT1">
     <Name>Single fares</Name>
     <preAssignedFareProducts>
      <PreassignedFareProduct id="P1">
       <Name>Zone 1-2 single</Name>
       <AccessRightsInProduct>
        <AccessRightInProduct>
         <PreassignedFare>
          <StartTariffZoneRef ref="Z1"/>
          <EndTariffZoneRef ref="Z2"/>
          <FarePrice currency="GBP"><Amount>250</Amount></FarePrice>
         </PreassignedFare>
         <PreassignedFare>
          <TariffZoneRef ref="Z3"/>
          <FarePrice><Amount>180</Amount></FarePrice>
         </PreassignedFare>
        </AccessRightInProduct>
       </AccessRightsInProduct>
      </PreassignedFareProduct>
     </preAssignedFareProducts>
    </FareTable>
   </FaresFrame>
  </CompositeFrame>
 </dataObjects>
</PublicationDelivery>'''


def test_parse_fare_prices_zones_and_product():
    prices = parse_fare_prices(NETEX)
    assert prices[0] == {"amount": "250", "currency": "GBP",
                         "start_zones": ["Z1"], "end_zones": ["Z2"],
                         "zones": [], "product": "Zone 1-2 single"}
    assert prices[1]["zones"] == ["Z3"]
    assert prices[1]["start_zones"] == [] and prices[1]["end_zones"] == []
    assert prices[1]["product"] == "Zone 1-2 single"
    assert prices[1]["currency"] == "GBP", "currency defaults to GBP"


def test_parse_fare_prices_skips_amountless_entries():
    bare = NETEX.replace(b"<Amount>180</Amount>", b"")
    prices = parse_fare_prices(bare)
    assert len(prices) == 1 and prices[0]["amount"] == "250"


def test_extract_xml_bytes_unzips():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fares.xml", NETEX)
    assert parse_fare_prices(buf.getvalue())[0]["amount"] == "250"


def test_extract_xml_bytes_passthrough():
    assert extract_xml_bytes(NETEX) == NETEX
