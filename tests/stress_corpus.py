"""A generated repository built to push every tool against its token cap.

- Hub.process has 500+ call sites spread over 60 packages, including a 1,000-char line.
- Big has 300 members with long signatures, and one 400-line method.
- 60 packages import each other in one long ring (a 60-module cycle) plus
  external imports.
- Paths are deep, Maven-style, so path handling and prefix factoring are exercised.
- commit_stress_history makes two commits, the second touching every file: 150 long
  signatures change, 540 method bodies change, and Hub.process is re-declared, so
  diff_impact has ~700 changed symbols and hotspots 60+ changed files.
Everything is written from scratch here; nothing is copied from real code.
"""

from pathlib import Path

from tests.gitrepo import ScriptedRepo

ROOT = "modules/a-rather-long-module-name/src/main/java/org/example/deeply/nested"
PACKAGES = 60
CALLERS_PER_PACKAGE = 9


def pkg(i: int) -> str:
    return f"org.example.deeply.nested.level{i:02d}"


def write_stress_repo(root: Path) -> Path:
    core = root / ROOT / "core"
    core.mkdir(parents=True)
    (core / "Hub.java").write_text(
        "package org.example.deeply.nested.core;\n\n"
        "public class Hub {\n"
        "    public static int process(int value) {\n"
        "        return value;\n"
        "    }\n"
        "}\n"
    )
    (core / "Big.java").write_text(_big_class())
    for i in range(PACKAGES):
        directory = root / ROOT / f"level{i:02d}"
        directory.mkdir(parents=True)
        (directory / f"Caller{i:02d}.java").write_text(_caller(i))
    return root


def _caller(i: int) -> str:
    nxt = (i + 1) % PACKAGES
    lines = [
        f"package {pkg(i)};",
        "",
        "import java.util.List;",
        "import java.util.Map;",
        f"import com.external.vendor{i}.Thing;",
        "import org.example.deeply.nested.core.Hub;",
        f"import {pkg(nxt)}.Caller{nxt:02d};",
        "",
        f"public class Caller{i:02d} {{",
    ]
    for m in range(CALLERS_PER_PACKAGE):
        lines += [
            f"    public int handleTheIncomingRequestNumber{m}(int value, String label) {{",
            f"        int a = Hub.process(value + {m});",
            "        return a;",
            "    }",
        ]
    if i == 0:
        long_expr = " + ".join(f"Hub.process({k})" for k in range(70))
        lines += ["    public int wide() {", f"        return {long_expr};", "    }"]
    lines.append(f"    Caller{nxt:02d} next;")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _big_class() -> str:
    params = ", ".join(f"Map<String, List<Integer>> argumentNumber{k}" for k in range(8))
    lines = ["package org.example.deeply.nested.core;", "", "import java.util.*;", ""]
    lines.append("/** A class with far too many members. */")
    lines.append("public class Big {")
    for k in range(150):
        lines.append(f"    private final Map<String, List<Integer>> fieldNumber{k} = null;")
    for k in range(150):
        lines.append(f"    public int methodNumber{k}({params}) {{ return {k}; }}")
    lines.append("    public int huge() {")
    lines.append("        int total = 0;")
    for k in range(400):
        lines.append(
            f"        total += Hub.process({k}) * {k} + methodNumber{k % 150}(null, null,"
            " null, null, null, null, null, null);"
        )
    lines.append("        return total;")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def commit_stress_history(repo: ScriptedRepo) -> None:
    repo.commit("stress: first", "2026-01-05T10:00:00+00:00")
    for path in sorted(repo.root.rglob("*.java")):
        text = path.read_text()
        text = text.replace("argumentNumber7)", "argumentNumber7, int extra)")  # Big
        text = text.replace("return a;", "return a + 1;")  # every Caller method
        text = text.replace("process(int value)", "process(int value, int... more)")  # Hub
        path.write_text(text)
    repo.commit("stress: touch everything", "2026-01-12T10:00:00+00:00")
