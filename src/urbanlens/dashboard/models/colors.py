"""Material Design 500-shade color palette as a Django TextChoices enum.

Keep values in sync with the ``$color-*-500`` variables in
``dashboard/frontend/sass/_tokens.scss``.
"""

from django.db.models import TextChoices


class MaterialColor(TextChoices):
    """Material Design 500-shade hex colors."""

    RED = "#F44336", "Red"
    PINK = "#E91E63", "Pink"
    PURPLE = "#9C27B0", "Purple"
    INDIGO = "#3F51B5", "Indigo"
    BLUE = "#2196F3", "Blue"
    TEAL = "#009688", "Teal"
    GREEN = "#4CAF50", "Green"
    YELLOW = "#FFEB3B", "Yellow"
    AMBER = "#FFC107", "Amber"
    DEEP_ORANGE = "#FF5722", "Deep Orange"
    BROWN = "#795548", "Brown"
    BLACK = "#000000", "Black"
    WHITE = "#FFFFFF", "White"
    GREY = "#999999", "Grey"
