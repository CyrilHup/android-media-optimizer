"""Conservative Ultra HDR XMP scope; no silent loss of custom/motion metadata."""
import xml.etree.ElementTree as ET

RDF='http://www.w3.org/1999/02/22-rdf-syntax-ns#'
HDR='http://ns.adobe.com/hdr-gain-map/1.0/'
CONTAINER='http://ns.google.com/photos/1.0/container/'
ITEM='http://ns.google.com/photos/1.0/container/item/'


def hdr_xmp_issues(payload):
    if not payload:return ['missing_hdr_xmp']
    try:root=ET.fromstring(payload)
    except ET.ParseError:return ['malformed_hdr_xmp']
    tags={f'{{adobe:ns:meta/}}xmpmeta',*[f'{{{RDF}}}{name}' for name in ('RDF','Description','Seq','li')],
          f'{{{CONTAINER}}}Directory',f'{{{CONTAINER}}}Item'}
    attributes={f'{{adobe:ns:meta/}}xmptk',f'{{{RDF}}}about',f'{{{RDF}}}parseType',
                *[f'{{{ITEM}}}{name}' for name in ('Semantic','Mime','Length','Padding')],
                *[f'{{{HDR}}}{name}' for name in ('Version','GainMapMin','GainMapMax','Gamma','OffsetSDR','OffsetHDR','HDRCapacityMin','HDRCapacityMax','BaseRenditionIsHDR')]}
    issues=[]
    for element in root.iter():
        if element.tag not in tags:issues.append('custom_xmp_element')
        if any(key not in attributes for key in element.attrib):issues.append('custom_xmp_attribute')
        semantic=element.attrib.get(f'{{{ITEM}}}Semantic')
        if semantic and semantic not in ('Primary','GainMap'):issues.append('motion_or_other_auxiliary_item')
    return sorted(set(issues))
