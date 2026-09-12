from entitybridge.identifier_rich import trusted_registry_edges


def test_registry_route_keeps_leading_zero_and_quarantines_duplicate_lei_records():
    base = {"registration_authority": "RA000585", "registration_number": "00001234", "category": "GENERAL",
            "status": "ACTIVE", "registration_status": "ISSUED"}
    a = base | {"source": "gleif", "record_id": "a", "record_version_id": "av1"}
    b = base | {"source": "companies_house", "record_id": "b", "record_version_id": "bv1"}
    assert trusted_registry_edges([a, b])["edges"][0]["evidence"]["registration_number"] == "00001234"
    duplicate = a | {"record_id": "c", "record_version_id": "cv1", "lei": "different"}
    assert not trusted_registry_edges([a, b, duplicate])["edges"]
    assert trusted_registry_edges([a, b, duplicate])["conflicts"]
