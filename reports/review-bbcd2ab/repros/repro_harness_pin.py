"""Does a pinned `--harness-file` survive a scanned repo's own `.orion/runtime.json`? Token-free: only
`runtime.targets.select` runs; nothing is started.

    python reports/review-bbcd2ab/repros/repro_harness_pin.py [ORION_CHECKOUT]

Case 1: `--harness-file mine.py` with the driver at its default (auto), repo descriptor {"kind": "http"}.
Case 2: `--runtime-driver harness --harness-file mine.py`, repo descriptor {"kind": "harness", "file": ...}.
A pinned file that wins gives a HarnessDriver running mine.py with no image from the repo, in both cases.
"""
import json
import sys
import tempfile
from pathlib import Path

checkout = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(checkout))
from orion.runtime import targets   # noqa: E402

work = Path(tempfile.mkdtemp(prefix="repro-harness-"))
mine = work / "mine.py"
mine.write_text("print(1)\n", encoding="utf-8")


def repo_with(descriptor: dict) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=work))
    (repo / ".orion").mkdir()
    (repo / ".orion" / "runtime.json").write_text(json.dumps(descriptor), encoding="utf-8")
    return repo


def show(label: str, sel) -> None:
    if sel is None:
        print(label, "-> None (stage skipped)")
        return
    drv = sel[0]
    picked = {k: v for k, v in vars(drv).items() if k in ("_boot_cmd", "boot_cmd", "_harness_file", "_image")}
    print(label, "->", type(drv).__name__, picked)


print("orion code:", Path(sys.modules["orion"].__file__).parent)
http_repo = repo_with({"kind": "http", "boot": ["echo", "repo-command-on-host"]})
show("case 1 (auto + pinned file, repo kind http)",
     targets.select(str(http_repo), driver="auto", harness_file=str(mine), sandbox="docker"))
harness_repo = repo_with({"kind": "harness", "language": "py", "file": "repo_script.py", "image": "some/image:tag"})
show("case 2 (harness + pinned file, repo kind harness)",
     targets.select(str(harness_repo), driver="harness", harness_file=str(mine), sandbox="docker"))
