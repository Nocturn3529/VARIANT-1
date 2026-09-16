"""Root and nested ignore semantics used by glob/grep walks."""

from pathlib import Path
import json
import subprocess
import pytest

from file_ignore import IgnoreMatcher
from run_context import Variant1RunContext, bind_run_context

CASES = json.loads((Path(__file__).parent / "fixtures/ignore_conformance.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[str(index) for index in range(len(CASES))])
def test_ignore_grammar_conformance(tmp_path, case):
    (tmp_path / ".gitignore").write_text("\n".join(case["patterns"]), encoding="utf-8")
    matcher = IgnoreMatcher(str(tmp_path), project_root=str(tmp_path))
    assert matcher.ignored(case["path"], is_dir=case.get("directory", False)) is case["ignored"]


def test_shared_ignore_cases_match_installed_frontend_library():
    root = Path(__file__).resolve().parents[2]
    module = root / "node_modules/ignore"
    if not module.exists():
        pytest.skip("frontend dependencies are not installed in this backend-only environment")
    script = "const ignore=require(process.argv[1]); const cases=JSON.parse(require('fs').readFileSync(0,'utf8')); process.stdout.write(JSON.stringify(cases.map(c=>ignore().add(c.patterns).ignores(c.path+(c.directory?'/':'')))));"
    result = subprocess.run(["node", "-e", script, str(module)], input=json.dumps(CASES),
                            text=True, capture_output=True, check=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert json.loads(result.stdout) == [case["ignored"] for case in CASES]


def _context(root: Path):
    return bind_run_context(Variant1RunContext.create(
        source="chat",
        metadata={"working_directory": str(root), "project_roots": [str(root)]},
    ))


def test_subdirectory_search_keeps_project_root_ignore_rules(tmp_path: Path):
    project = tmp_path / "project"
    search = project / "src"
    search.mkdir(parents=True)
    (project / ".gitignore").write_text("*.min.js\ngenerated/\n", encoding="utf-8")

    with _context(project):
        matcher = IgnoreMatcher(str(search))

    assert matcher.ignored("bundle.min.js") is True
    assert matcher.ignored("normal.js") is False


def test_nested_ignore_file_applies_when_walk_enters_directory(tmp_path: Path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (nested / ".gitignore").write_text("secret.txt\n", encoding="utf-8")

    with _context(project):
        matcher = IgnoreMatcher(str(project))

    assert matcher.ignored("nested", is_dir=True) is False
    assert matcher.ignored("nested/secret.txt") is True
    assert matcher.ignored("nested/public.txt") is False


def test_root_directory_pattern_applies_when_search_starts_inside_it(tmp_path: Path):
    project = tmp_path / "project"
    search = project / "generated"
    search.mkdir(parents=True)
    (project / ".gitignore").write_text("generated/\n", encoding="utf-8")

    with _context(project):
        matcher = IgnoreMatcher(str(search))

    assert matcher.ignored("bundle.js") is True
