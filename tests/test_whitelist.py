from companion.whitelist import is_allowed, parse_whitelist


def test_parse_mixed_separators():
    assert parse_whitelist('100000001, 123，456 789;  ') == {'100000001', '123', '456', '789'}


def test_empty_means_no_group():
    assert parse_whitelist('') == frozenset()
    assert parse_whitelist(None) == frozenset()
    assert not is_allowed(100000001, parse_whitelist(''))


def test_non_digits_ignored_and_int_ids_match():
    wl = parse_whitelist('abc,100000001,12a')
    assert wl == {'100000001'}
    assert is_allowed(100000001, wl)
    assert not is_allowed(100000002, wl)
