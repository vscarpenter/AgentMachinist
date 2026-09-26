from timezones import parse_timezone


def test_known_name_passes_through():
    assert parse_timezone("UTC") == "UTC"
