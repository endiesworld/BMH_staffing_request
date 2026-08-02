"""Shared input helpers.

Lives in `accounts` because it is the base of the dependency DAG (ADR-007):
`servicing` may import from here, never the reverse. If a third consumer
appears that has nothing to do with accounts, that is the signal to extract a
dedicated shared module rather than keep piling utilities in here.
"""


def normalize_us_phone(raw):
    """Accept any way a person types a US number; return one canonical shape.

    "(555) 123-4567", "555.123.4567" and "1 555 123 4567" all become
    "+15551234567", so stored numbers are comparable and searchable.

    Raises ValueError if it is not ten digits (optionally with a leading 1).
    """
    digits = "".join(character for character in raw if character.isdigit())

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Enter a 10-digit US phone number, e.g. (555) 123-4567.")

    return f"+1{digits}"
