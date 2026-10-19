import unittest
from hdr_metadata_policy import hdr_xmp_issues


class HDRMetadataPolicyTests(unittest.TestCase):
    def test_known_hdr_metadata_is_supported(self):
        payload=b'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"><rdf:RDF><rdf:Description hdrgm:Version="1.0"/></rdf:RDF></x:xmpmeta>'
        self.assertEqual(hdr_xmp_issues(payload),[])

    def test_custom_metadata_cannot_be_discarded(self):
        self.assertIn('custom_xmp_attribute',hdr_xmp_issues(b'<x:xmpmeta xmlns:x="adobe:ns:meta/" custom="preserve-me"/>'))

    def test_invalid_or_missing_metadata_stays_original(self):
        self.assertEqual(hdr_xmp_issues(b''),['missing_hdr_xmp'])
        self.assertEqual(hdr_xmp_issues(b'bad xml'),['malformed_hdr_xmp'])

    def test_motion_photo_item_is_not_treated_as_only_gainmap(self):
        payload=b'<C:Item xmlns:C="http://ns.google.com/photos/1.0/container/" xmlns:I="http://ns.google.com/photos/1.0/container/item/" I:Semantic="MotionPhoto"/>'
        self.assertIn('motion_or_other_auxiliary_item',hdr_xmp_issues(payload))
