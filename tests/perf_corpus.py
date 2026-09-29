"""Deterministic synthetic Java + Python corpus for indexing benchmarks.

The code is shaped like ordinary service code (imports, fields, Javadoc and
docstrings, methods with branches, loops and cross-class calls) so that parse
and extraction work per line is realistic. It is generated, so it says nothing
about resolution quality -- only about speed.
"""

from pathlib import Path

JAVA_METHOD = """
    /**
     * Handles step {m} for {cls}.
     * @param input the value to process
     */
    public int step{m}(int input, String label) {{
        int total = input;
        for (int i = 0; i < {m} + 3; i++) {{
            if (i % 2 == 0) {{
                total += helper.compute{other}(i, label);
            }} else {{
                total -= Math.max(i, other.step{prev}(total, label.trim()));
            }}
        }}
        items.add(new Entry(label, total));
        log("step{m} " + label + " -> " + total);
        return validate(total) ? total : fallback(total, label);
    }}
"""

PY_METHOD = '''
    def step_{m}(self, value, label=None):
        """Handle step {m} for {cls}."""
        total = value
        for i in range({m} + 3):
            if i % 2 == 0:
                total += self.helper.compute_{other}(i, label)
            else:
                total -= max(i, self.other.step_{prev}(total, str(label).strip()))
        self.items.append(Entry(label, total))
        logger.info("step_{m} %s -> %s", label, total)
        return total if self.validate(total) else self.fallback(total, label)
'''


def _java_file(pkg: int, n: int, methods: int) -> str:
    cls = f"Service{pkg}x{n}"
    body = "".join(
        JAVA_METHOD.format(m=m, cls=cls, other=(m * 7) % methods, prev=max(m - 1, 0))
        for m in range(methods)
    )
    return f"""package com.synthetic.pkg{pkg};

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import com.synthetic.pkg{(pkg + 1) % 10}.Service{(pkg + 1) % 10}x{n};
import com.synthetic.common.*;

/**
 * Synthetic service {cls}.
 */
public class {cls} extends BaseService implements Handler {{
    private final List<Entry> items = new ArrayList<>();
    private final Helper helper;
    private final Service{(pkg + 1) % 10}x{n} other;
    private int count, errors = 0;

    public {cls}(Helper helper, Service{(pkg + 1) % 10}x{n} other) {{
        super("{cls}");
        this.helper = helper;
        this.other = other;
    }}
{body}
    private boolean validate(int value) {{
        return value >= 0 && value < Integer.MAX_VALUE;
    }}

    private int fallback(int value, String label) {{
        errors++;
        return Math.abs(value) + label.length();
    }}

    static class Entry {{
        final String label;
        final int total;

        Entry(String label, int total) {{
            this.label = label;
            this.total = total;
        }}
    }}
}}
"""


def _python_file(pkg: int, n: int, methods: int) -> str:
    cls = f"Service{pkg}x{n}"
    body = "".join(
        PY_METHOD.format(m=m, cls=cls, other=(m * 7) % methods, prev=max(m - 1, 0))
        for m in range(methods)
    )
    return f'''"""Synthetic module {cls}."""

import logging
from dataclasses import dataclass

from synthetic.common import BaseService, Helper
from synthetic.pkg{(pkg + 1) % 10}.mod{n} import Service{(pkg + 1) % 10}x{n}

logger = logging.getLogger(__name__)


@dataclass
class Entry:
    label: str
    total: int


class {cls}(BaseService):
    """Synthetic service {cls}."""

    def __init__(self, helper: Helper, other: "Service{(pkg + 1) % 10}x{n}"):
        super().__init__("{cls}")
        self.helper = helper
        self.other = other
        self.items = []
{body}
    def validate(self, value):
        return 0 <= value < 2**31

    def fallback(self, value, label):
        return abs(value) + len(str(label))
'''


def generate(root: Path, target_lines: int = 50_000, methods: int = 10) -> int:
    """Write a corpus of about target_lines lines under root. Returns the actual count."""
    lines = 0
    n = 0
    while lines < target_lines:
        pkg = n % 10
        java = _java_file(pkg, n, methods)
        python = _python_file(pkg, n, methods)
        java_path = root / "java" / f"com/synthetic/pkg{pkg}/Service{pkg}x{n}.java"
        py_path = root / "python" / f"synthetic/pkg{pkg}/mod{n}.py"
        for path, text in ((java_path, java), (py_path, python)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            lines += text.count("\n")
        n += 1
    for pkg_dir in (root / "python" / "synthetic").rglob("*"):
        if pkg_dir.is_dir():
            (pkg_dir / "__init__.py").write_text("")
    (root / "python" / "synthetic" / "__init__.py").write_text("")
    return lines
