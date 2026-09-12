from entitybridge.normalization import normalize_postcode, normalize_text


def test_normalization_preserves_distinguishing_legal_words_and_nulls():
    assert normalize_text('  Acmé & BANK, Ｌimited  ') == 'ACMÉ BANK LIMITED'
    assert normalize_text('') is None
    assert normalize_text(None) is None
    assert normalize_postcode('sw1a  1aa') == 'SW1A1AA'


def test_matcher_projection_is_an_allowlist_and_never_derives_ids_from_registry():
    from entitybridge.normalization import MATCHER_COLUMNS, matcher_view
    rich = {'source': 'gleif', 'name': 'Example LTD', 'registration_number': '00123456',
            'lei': 'SECRET_LEI', 'url': '/company/00123456', 'true_entity_id': 'secret'}
    actual = matcher_view(rich, 'independent-record', 'independent-version')
    assert set(actual) == set(MATCHER_COLUMNS)
    assert '00123456' not in repr(actual)
    assert actual['record_id'] == 'independent-record'


def test_identifier_missing_redacts_identifiers_embedded_in_allowed_field_text():
    from entitybridge.normalization import matcher_view
    raw={'source':'gleif','name':'00134419 LIMITED','registration_number':'00134419',
         'lei':'549300AFVOSPDF34DN20','address':'REF 549300AFVOSPDF34DN20',
         'city':'London','postcode':'SW1A 1AA','country':'GB'}
    result=matcher_view(raw,'random-record','random-version')
    assert result['name'] is None and result['address'] is None
    assert result['city']=='LONDON' and result['postcode']=='SW1A1AA'
    assert not any(identifier in str(result) for identifier in ('00134419','549300AFVOSPDF34DN20'))
