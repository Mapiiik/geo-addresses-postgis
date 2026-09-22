<?xml version="1.0" encoding="UTF-8"?>
<!--
  A handful of units out of the DGU's INSPIRE Administrative Units download,
  cut down to what the importer reads and with the geometry shortened to a few
  coordinates: a settlement, the municipality above it and the county above
  that, plus a settlement whose municipality is not in the file.

  The shape is the source's own - the namespaces, the code list URL the level
  is named by, the "#" the upper unit is referenced with, and the spelling
  buried three elements deep under the name.
-->
<wfs:FeatureCollection
    xmlns:wfs="http://www.opengis.net/wfs/2.0"
    xmlns:au="http://inspire.ec.europa.eu/schemas/au/4.0"
    xmlns:base="http://inspire.ec.europa.eu/schemas/base/3.3"
    xmlns:gn="http://inspire.ec.europa.eu/schemas/gn/4.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    numberMatched="4" numberReturned="4">
  <wfs:member>
    <au:AdministrativeUnit gml:id="AdministrativeUnit.2147056058">
      <au:geometry>
        <gml:MultiSurface gml:id="geom.2147056058" srsDimension="2">
          <gml:surfaceMember><gml:Polygon gml:id="geom.2147056058.1"><gml:exterior><gml:LinearRing>
            <gml:posList>2516834.57 4999295.47 2516865.60 4999184.12 2516939.77 4998977.57</gml:posList>
          </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        </gml:MultiSurface>
      </au:geometry>
      <au:nationalCode>037591</au:nationalCode>
      <au:inspireId>
        <base:Identifier>
          <base:localId>NA.0005004004</base:localId>
          <base:namespace>HR.DGU.RPJ</base:namespace>
        </base:Identifier>
      </au:inspireId>
      <au:nationalLevel xlink:href="http://inspire.ec.europa.eu/codelist/AdministrativeHierarchyLevel/4thOrder" xlink:title="4thOrder"/>
      <au:name>
        <gn:GeographicalName>
          <gn:language>hrv</gn:language>
          <gn:spelling><gn:SpellingOfName><gn:text>Makarska</gn:text><gn:script>Latn</gn:script></gn:SpellingOfName></gn:spelling>
        </gn:GeographicalName>
      </au:name>
      <au:upperLevelUnit xlink:href="#AdministrativeUnit.2186725354" xlink:title="Makarska"/>
    </au:AdministrativeUnit>
  </wfs:member>
  <wfs:member>
    <au:AdministrativeUnit gml:id="AdministrativeUnit.2186725354">
      <au:nationalCode>02496</au:nationalCode>
      <au:inspireId>
        <base:Identifier>
          <base:localId>JLS.0052000239</base:localId>
          <base:namespace>HR.DGU.RPJ</base:namespace>
        </base:Identifier>
      </au:inspireId>
      <au:nationalLevel xlink:href="http://inspire.ec.europa.eu/codelist/AdministrativeHierarchyLevel/3rdOrder" xlink:title="3rdOrder"/>
      <au:name>
        <gn:GeographicalName>
          <gn:spelling><gn:SpellingOfName><gn:text>Makarska</gn:text></gn:SpellingOfName></gn:spelling>
        </gn:GeographicalName>
      </au:name>
      <au:upperLevelUnit xlink:href="#AdministrativeUnit.2185214101" xlink:title="Splitsko-dalmatinska županija"/>
    </au:AdministrativeUnit>
  </wfs:member>
  <wfs:member>
    <au:AdministrativeUnit gml:id="AdministrativeUnit.2185214101">
      <au:nationalCode>17</au:nationalCode>
      <au:inspireId>
        <base:Identifier>
          <base:localId>ZUP.0017</base:localId>
          <base:namespace>HR.DGU.RPJ</base:namespace>
        </base:Identifier>
      </au:inspireId>
      <au:nationalLevel xlink:href="http://inspire.ec.europa.eu/codelist/AdministrativeHierarchyLevel/2ndOrder" xlink:title="2ndOrder"/>
      <au:name>
        <gn:GeographicalName>
          <gn:spelling><gn:SpellingOfName><gn:text>Splitsko-dalmatinska županija</gn:text></gn:SpellingOfName></gn:spelling>
        </gn:GeographicalName>
      </au:name>
    </au:AdministrativeUnit>
  </wfs:member>
  <wfs:member>
    <au:AdministrativeUnit gml:id="AdministrativeUnit.5000573">
      <au:nationalCode>000019</au:nationalCode>
      <au:inspireId>
        <base:Identifier>
          <base:localId>NA.0005000573</base:localId>
          <base:namespace>HR.DGU.RPJ</base:namespace>
        </base:Identifier>
      </au:inspireId>
      <au:nationalLevel xlink:href="http://inspire.ec.europa.eu/codelist/AdministrativeHierarchyLevel/4thOrder" xlink:title="4thOrder"/>
      <au:name>
        <gn:GeographicalName>
          <gn:spelling><gn:SpellingOfName><gn:text>Ada</gn:text></gn:SpellingOfName></gn:spelling>
        </gn:GeographicalName>
      </au:name>
      <au:upperLevelUnit xlink:href="#AdministrativeUnit.2187881949" xlink:title="Čodolovci"/>
    </au:AdministrativeUnit>
  </wfs:member>
</wfs:FeatureCollection>
