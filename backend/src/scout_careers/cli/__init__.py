"""The ``scout-careers`` command line.

The operator's only interface until the API and the UI land, and the one that
keeps working after they do: a failing adapter must be visible without reading
logs (SOURCE_ADAPTERS.md §10.3), and ``scout-careers runs show`` is how.

Every read command takes ``--json`` so the same output can be piped somewhere.
The human rendering is the default because the human is the user.
"""

from __future__ import annotations
