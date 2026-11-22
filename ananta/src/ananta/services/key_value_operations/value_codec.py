"""The one definition of how a key-value store keeps a value in its text column.

Stored format: every value is kept as its JSON text (``json.dumps``), so an
object, a list, a string, a number, a boolean and ``None`` each come back as
the same kind of value that was written. A value that ``json.dumps`` cannot
encode is refused by ``encode_value`` (the error propagates); nothing is
stored for it.

Legacy-text rule: rows that did not come through ``encode_value`` exist (raw
status strings, ids and markers written before this codec, and test fixtures
that write the table directly). ``decode_value`` returns text that is not
valid JSON as that string, so those rows keep reading. Legacy text that happens
to be valid JSON (for example ``'5'`` or ``'true'``) reads back as its JSON
meaning. Old rows are not converted in bulk: during a blue-green overlap the
previous release still reads them.
"""

from __future__ import annotations

import json


def encode_value(value: object) -> str:
    """Return the text to store for ``value``; ``TypeError`` or ``ValueError`` propagates."""
    return json.dumps(value)


def decode_value(stored: str | None) -> object:
    """Return the value that ``stored`` holds; text that is not JSON is returned as that string."""
    if stored is None:
        return None
    try:
        return json.loads(stored)
    except ValueError:
        return stored
