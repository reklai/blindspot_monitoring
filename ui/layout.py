"""Choose dashboard grid dimensions for the visible tiles."""

from __future__ import annotations


def get_smart_grid(num_cameras: int) -> tuple[int, int]:
    """Return ``(rows, columns)`` for the requested number of dashboard tiles.

    Small counts use fixed, readable arrangements. Larger counts add rows
    while limiting the dashboard to four columns.
    """
    if num_cameras <= 1:
        return 1, 1
    elif num_cameras == 2:
        return 1, 2
    elif num_cameras == 3:
        return 1, 3
    elif num_cameras == 4:
        return 2, 2
    elif num_cameras <= 6:
        return 2, 3
    elif num_cameras <= 9:
        return 3, 3
    else:
        cols = min(4, int(num_cameras**0.5 * 1.5))
        rows = (num_cameras + cols - 1) // cols
        return rows, cols
