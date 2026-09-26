"""A deliberately small module with one obvious improvement left to make."""

KNOWN = {"UTC", "America/Chicago", "Europe/London"}


def parse_timezone(name: str) -> str:
    """Return the timezone name as given. Unknown names pass through unchanged."""
    return name
