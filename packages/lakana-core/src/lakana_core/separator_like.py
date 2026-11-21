"""Letters, marks and digits that look like a separator (#609); generated, do not edit.

Written by `scripts/separator_like_characters.py` from Unicode's `confusables.txt` of
2026-08-06, with Python 3.14's `unicodedata` (Unicode 16.0.0).
Each key is a character the table-name allowlist would accept, and its value the
skeleton `confusables.txt` gives it, which holds `.`, `/`, a backslash or `:`.
"""

from typing import Final

CONFUSABLES_DATE: Final[str] = "2026-08-06"
"""The date of the `confusables.txt` the table was generated from."""

SEPARATOR_LIKE_CHARACTERS: Final[dict[str, str]] = {
    "\u02d0": ":",  # MODIFIER LETTER TRIANGULAR COLON
    "\u0660": ".",  # ARABIC-INDIC DIGIT ZERO
    "\u06f0": ".",  # EXTENDED ARABIC-INDIC DIGIT ZERO
    "\u0903": ":",  # DEVANAGARI SIGN VISARGA
    "\u0a83": ":",  # GUJARATI SIGN VISARGA
    "\u2cc6": "/",  # COPTIC CAPITAL LETTER OLD COPTIC ESH
    "\u2cc7": "/",  # COPTIC SMALL LETTER OLD COPTIC ESH
    "\u3033": "/",  # VERTICAL KANA REPEAT MARK UPPER HALF
    "\u30ce": "/",  # KATAKANA LETTER NO
    "\u4e36": "\\",  # CJK UNIFIED IDEOGRAPH-4E36
    "\u4e3f": "/",  # CJK UNIFIED IDEOGRAPH-4E3F
    "\ua4f8": ".",  # LISU LETTER TONE MYA TI
    "\ua4fa": "..",  # LISU LETTER TONE MYA CYA
    "\ua4fb": ".,",  # LISU LETTER TONE MYA BO
    "\ua4fd": ":",  # LISU LETTER TONE MYA JEU
    "\uabec": ".",  # MEETEI MAYEK LUM IYEK
    "\uff89": "/",  # HALFWIDTH KATAKANA LETTER NO
    "\U00011002": ":",  # BRAHMI SIGN VISARGA
    "\U00011082": ":",  # KAITHI SIGN VISARGA
    "\U00011182": ":",  # SHARADA SIGN VISARGA
    "\U000115be": ":",  # SIDDHAM SIGN VISARGA
    "\U000116ac": ":",  # TAKRI SIGN VISARGA
    "\U00011838": ":",  # DOGRA SIGN VISARGA
    "\U0001d16d": ".",  # MUSICAL SYMBOL COMBINING AUGMENTATION DOT
}
"""Character to skeleton; `lakana_core.identifiers.character_outside_name_allowlist`
refuses the keys in a new namespace, table or view name."""
