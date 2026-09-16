"""Headless native-path contract probe; never installs/starts the host runtime."""
import ast
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from browser_fabric.service import create_browser_fabric

# Run the real composition expressions, not a test copy of '/data'. Importing
# the full host builder would pull in unrelated live service dependencies.
tree = ast.parse((root / "backend/host_runtime_builder.py").read_text(encoding="utf-8-sig"))
install = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "install_host_runtime")
assignments = [node for node in install.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id in {"data_root", "browser"} for target in node.targets)]
assert len(assignments) == 2, "HostRuntime composition changed; update the integration probe"
async def browser_credential(*args, **kwargs):
    raise AssertionError("The download-path probe must never request cloud credentials")

scope = {"os": os, "host": SimpleNamespace(data_dir=sys.argv[1]),
         "astb": SimpleNamespace(session_artifacts=None), "create_browser_fabric": create_browser_fabric,
         "browser_credential": browser_credential}
exec(compile(ast.Module(body=assignments, type_ignores=[]), "host_runtime_builder.py", "exec"), scope)
browser = scope["browser"]
staged = os.path.realpath(sys.argv[2])
assert os.path.isfile(staged), "Native completed file must exist"
assert os.path.dirname(staged) == browser.download_staging_root, (staged, browser.download_staging_root)
print(json.dumps({"staging_root": browser.download_staging_root}))
