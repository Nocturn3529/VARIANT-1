"""Operating-system credential storage primitives."""

import secrets


def matches_ascii_token(supplied: str, expected: str) -> bool:
    """Reject malformed encodings before comparing issued local credentials."""
    return bool(
        supplied and expected and supplied.isascii() and expected.isascii()
        and secrets.compare_digest(supplied, expected)
    )
