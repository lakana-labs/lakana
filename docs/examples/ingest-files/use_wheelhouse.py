"""Point a project's pyproject.toml at the wheel of the checkout, for lakana alone.

The page's reader takes lakana from the public package index, at the version `lakana init`
wrote. The documentation examples run the checkout, whose version no index has, so the pin
becomes the wheel's version and a flat index in the wheel's folder becomes the only source of
lakana; every other package still comes from the public index. The ingestion examples share it.

Usage: python3 use_wheelhouse.py PYPROJECT WHEELHOUSE
"""

import re
import sys
from pathlib import Path

pyproject, wheelhouse = Path(sys.argv[1]), sys.argv[2]
(wheel,) = Path(wheelhouse).glob("lakana-[0-9]*.whl")
version = wheel.name.split("-")[1]
text, count = re.subn(
    r'"lakana\[assets\]==[^"]+"', f'"lakana[assets]=={version}"', pyproject.read_text()
)
assert count == 1, text
pyproject.write_text(
    text
    + f"""
[[tool.uv.index]]
name = "lakana"
url = "{wheelhouse}"
format = "flat"
explicit = true

[tool.uv.sources]
lakana = {{ index = "lakana" }}
"""
)
