"""Application translations selected from the user's locale environment."""

import gettext
from pathlib import Path


_translation = gettext.translation(
    "gos-grub-manager", localedir=Path(__file__).resolve().parent / "locale",
    fallback=True,
)
_ = _translation.gettext
ngettext = _translation.ngettext
