"""Test-only code (``tests/testbed``) must never reach business code, the wheel or a production run.

Python has no compile-time switch (the node firmware's test filter is a build tag plus a symbol check
on the image), so the isolation here is layered and every layer has a test:

a) import graph: no ``.py`` under ``cairnmesh/``, ``firmware/`` or the repo root imports ``tests``
   or anything called ``testbed`` (AST scan: import statements, relative imports, ``importlib`` /
   ``__import__`` calls with a literal name, and the activation variable's name);
b) runtime: a fresh interpreter importing every ``cairnmesh`` module (and the firmware Python
   tools) with ``tests/`` on the path and the activation variable set has no testbed module loaded;
c) packaging: the wheel and the sdist built from this tree hold no ``tests/`` file;
d) ``assert_no_active_filters()`` ends full-network and production-path runs (and
   ``tests/conftest.py`` applies it after every test that has not opted in);
e) behaviour with nobody excluded, and "a dropped frame moves nothing", are in test_testbed_filter.py.

What none of this proves is in tests/testbed/README.md ("Not proven").
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BUSINESS_DIRS = ("cairnmesh", "firmware")
#: Names whose appearance as an import target means "test code": the package ``tests`` and anything testbed.
TEST_ROOTS = {"tests", "test"}
ENV_NAME = "CAIRN_TESTFILTER"


def load_non_production_modules() -> frozenset[str]:
    """Read the release filter from setup.py without executing ``setup()``."""

    tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "NON_PRODUCTION_MODULES" for target in targets):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            modules = ast.literal_eval(value.args[0])
            assert isinstance(modules, (set, frozenset))
            return frozenset(modules)
    raise AssertionError("setup.py has no literal NON_PRODUCTION_MODULES=frozenset({...})")


NON_PRODUCTION_MODULES = load_non_production_modules()


def business_files() -> list[Path]:
    files = [p for d in BUSINESS_DIRS for p in sorted((ROOT / d).rglob("*.py"))]
    files += sorted(ROOT.glob("*.py"))  # a setup.py / manage script at the root
    return [p for p in files if "__pycache__" not in p.parts and ".venv" not in p.parts]


def offences(source: str, relative_to_package: tuple[str, ...] = ()) -> list[str]:
    """Why ``source`` is not clean (empty list: clean)."""

    bad: list[str] = []

    def name_is_test(dotted: str) -> bool:
        parts = [x for x in dotted.split(".") if x]
        return bool(parts) and (parts[0] in TEST_ROOTS or any("testbed" in x for x in parts))

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [f"import {a.name}" for a in node.names if name_is_test(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if name_is_test(module):
                bad.append(f"from {'.' * node.level}{module} import ...")
            # `from . import testbed`, `from cairnmesh import testbed`, `from .. import tests`
            bad += [f"from {'.' * node.level}{module} import {a.name}" for a in node.names if name_is_test(a.name)]
        elif isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            if fn.endswith(("import_module", "__import__", "find_spec", "spec_from_file_location", "run_module", "run_path")):
                # a literal that names test code, or any non-literal first argument (cannot be shown clean)
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if name_is_test(first.value) or "testbed" in first.value:
                        bad.append(f"{fn}({first.value!r})")
                elif first is not None and not isinstance(first, ast.Constant):
                    bad.append(f"{fn}(<computed>) cannot be shown clean")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ENV_NAME in node.value or "tests.testbed" in node.value or "tests/testbed" in node.value:
                bad.append(f"string {node.value[:40]!r}")
    return bad


# ------------------------------------------------------------------ a) imports


def test_the_scan_covers_the_business_code():
    files = business_files()
    assert len(files) > 50
    names = {p.relative_to(ROOT).as_posix() for p in files}
    assert {"cairnmesh/mesh.py", "cairnmesh/gateway.py", "cairnmesh/bridge.py", "cairnmesh/secure.py"} <= names
    assert {"firmware/tools/rxfilter_ctl.py", "firmware/tools/bridge_bench.py"} <= names
    assert not any("testbed" in n for n in names)


def test_no_business_module_imports_test_code():
    dirty = {}
    for path in business_files():
        found = offences(path.read_text(encoding="utf-8"))
        if found:
            dirty[path.relative_to(ROOT).as_posix()] = found
    assert dirty == {}


@pytest.mark.parametrize(
    "source",
    [
        "import tests.testbed",
        "import tests",
        "from tests.testbed import rxfilter",
        "from tests import testbed",
        "from . import testbed",
        "from .testbed import rxfilter",
        "from cairnmesh import testbed",
        "from cairnmesh.testbed.rxfilter import FilteredPort",
        "import importlib\nimportlib.import_module('tests.testbed.rxfilter')",
        "import importlib\nimportlib.import_module(name)",
        "__import__('testbed')",
        "import os\nos.environ.get('CAIRN_TESTFILTER')",
        "path = 'tests/testbed/rxfilter.py'",
        "import test.support",
    ],
)
def test_the_scan_catches_every_way_in(source):
    assert offences(source), source


@pytest.mark.parametrize(
    "source",
    ["import os\nfrom cairnmesh.secure import seal", "from . import secure", "import importlib\nimportlib.import_module('cairnmesh.mesh')", "x = 'latest'"],
)
def test_the_scan_leaves_clean_code_alone(source):
    assert offences(source) == []


# ------------------------------------------------------------------ b) runtime


def run_clean(code: str, **env) -> subprocess.CompletedProcess:
    """A fresh interpreter with ``tests/`` importable and the activation variable set: the worst case."""

    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT / "tests")]), **{ENV_NAME: "1"}, **env)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120)


def test_importing_all_of_cairnmesh_loads_no_test_code():
    result = run_clean(
        """
        import importlib, pkgutil, sys
        import cairnmesh
        failed = []
        loaded = 0
        for info in pkgutil.walk_packages(cairnmesh.__path__, "cairnmesh."):
            try:
                importlib.import_module(info.name)
                loaded += 1
            except ImportError as exc:      # an optional dependency (pyserial, numpy) is missing
                failed.append((info.name, str(exc)))
        # a failed import must not hide a testbed import: what did get loaded is what counts
        print("LOADED", loaded)
        print("FAILED", [name for name, _ in failed])
        bad = sorted(m for m in sys.modules if "testbed" in m or m == "tests" or m.startswith("tests."))
        print("TESTMODULES", bad)
        sys.exit(1 if bad else 0)
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr
    loaded = int(result.stdout.split("LOADED")[1].split()[0])
    assert loaded >= 30, result.stdout
    assert "TESTMODULES []" in result.stdout


def test_importing_the_firmware_python_tools_loads_no_test_code():
    result = run_clean(
        """
        import importlib.util, pathlib, sys
        tools = pathlib.Path("firmware/tools")
        sys.path.insert(0, str(tools.resolve()))
        done = []
        for path in sorted(tools.glob("*.py")):
            spec = importlib.util.spec_from_file_location("fw_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module     # a dataclass looks its module up here while it is built
            try:
                spec.loader.exec_module(module)
                done.append(path.stem)
            except ImportError:             # optional dependency missing
                sys.modules.pop(spec.name, None)
        bad = sorted(m for m in sys.modules if "testbed" in m or m == "tests" or m.startswith("tests."))
        print("DONE", len(done))
        print("TESTMODULES", bad)
        sys.exit(1 if bad else 0)
        """
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TESTMODULES []" in result.stdout
    assert int(result.stdout.split("DONE")[1].split()[0]) >= 5


def test_the_environment_variable_alone_activates_nothing():
    # Production path with the variable set (the worst case): a mesh network runs, no filter is made.
    result = run_clean(
        """
        import sys
        from cairnmesh.mesh_sim import MeshNetwork, line
        net = MeshNetwork(line(3), key=bytes(range(16)), seed=1)
        print("RAN", type(net).__name__)
        print("TESTMODULES", sorted(m for m in sys.modules if "testbed" in m))
        """
    )
    assert result.returncode == 0, result.stderr
    assert "RAN MeshNetwork" in result.stdout and "TESTMODULES []" in result.stdout


def test_importing_the_production_session_does_not_load_the_simulator():
    result = run_clean(
        """
        import sys
        import cairnmesh.session
        print("SIMLOADED", "cairnmesh.sim" in sys.modules)
        """
    )
    assert result.returncode == 0, result.stderr
    assert "SIMLOADED False" in result.stdout


# ----------------------------------------------------------------- c) packaging


def build_python() -> "str | None":
    """An interpreter that can run ``setuptools.build_meta`` offline (the test venv often cannot)."""

    probe = "import setuptools, sys; sys.exit(0 if int(setuptools.__version__.split('.')[0]) >= 77 else 1)"
    for exe in (sys.executable, shutil.which("python3"), "/usr/bin/python3"):
        if exe and subprocess.run([exe, "-c", probe], capture_output=True).returncode == 0:
            return exe
    return None


def require_builder() -> str:
    """The interpreter that builds; none: skip on a developer machine, FAIL under CI (``CI`` is set)."""

    builder = build_python()
    if builder is None:
        reason = "no interpreter with setuptools>=77 (a venv on Python 3.12+ has none): the wheel and sdist are NOT checked"
        if os.environ.get("CI"):
            pytest.fail(reason + "; CI must install setuptools>=77 (python -m pip install 'setuptools>=77')")
        pytest.skip(reason)
    return builder


def test_no_builder_skips_locally_and_fails_in_ci(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "build_python", lambda: None)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_builder()
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="CI must install"):
        require_builder()


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, Path]:
    """Wheel and sdist of a copy of this tree (copy: the build writes ``*.egg-info`` and ``build/``)."""

    builder = require_builder()
    work = tmp_path_factory.mktemp("pack")
    src, out = work / "src", work / "out"
    (out / "wheel").mkdir(parents=True)  # one folder each: a second build clears what else is in its folder
    (out / "sdist").mkdir()
    shutil.copytree(
        ROOT,
        src,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.egg-info", "build", "dist", ".pytest_cache", "firmware"),
    )
    (src / "firmware").mkdir()  # the build does not need the Go tree
    # Two interpreters: in one process the second build_meta call loses its artifact.
    for hook, folder in (("build_wheel", "wheel"), ("build_sdist", "sdist")):
        script = f"import setuptools.build_meta as b, sys; print(b.{hook}(sys.argv[1]))"
        done = subprocess.run([builder, "-c", script, str(out / folder)], cwd=src, capture_output=True, text=True, timeout=300)
        if done.returncode != 0:
            pytest.fail(f"{hook} failed:\n" + done.stdout[-2000:] + done.stderr[-2000:])
    wheels, sdists = sorted(out.glob("wheel/*.whl")), sorted(out.glob("sdist/*.tar.gz"))
    assert wheels and sdists, f"build wrote {sorted(p.name for p in out.iterdir())}\n{done.stdout[-700:]}"
    return wheels[0], sdists[0]


def test_the_tree_really_has_a_testbed_to_leave_out():
    assert (ROOT / "tests" / "testbed" / "rxfilter.py").is_file()


def test_the_wheel_holds_no_test_code(built):
    names = zipfile.ZipFile(built[0]).namelist()
    assert any(n.startswith("cairnmesh/mesh.py") for n in names)  # it is the real wheel
    assert [n for n in names if "testbed" in n or n.startswith(("tests/", "test/")) or "rxfilter" in n.lower()] == []
    assert sorted({n.split("/")[0] for n in names if "/" in n and not n.endswith(".dist-info/") and ".dist-info" not in n}) == ["cairnmesh"]


def test_the_wheel_holds_only_production_modules(built):
    names = set(zipfile.ZipFile(built[0]).namelist())
    stray = sorted(f"cairnmesh/{module}.py" for module in NON_PRODUCTION_MODULES if f"cairnmesh/{module}.py" in names)
    assert stray == []
    assert "cairnmesh/controller.py" in names


def test_every_module_in_the_wheel_imports(built, tmp_path):
    wheel = tmp_path / "wheel"
    with zipfile.ZipFile(built[0]) as archive:
        archive.extractall(wheel)
    script = """
        import importlib
        import importlib.machinery
        import pathlib
        import pkgutil
        import sys

        wheel = pathlib.Path(sys.argv[1]).resolve()
        sys.path.insert(0, str(wheel))
        import cairnmesh
        assert pathlib.Path(cairnmesh.__file__).resolve().is_relative_to(wheel)
        # An editable install registers a meta-path finder from its .pth even under
        # ``python -I``. It would otherwise resolve cairnmesh.sim from the source tree
        # after cairnmesh itself came from the wheel. Keep only importlib's standard
        # finders so package children are resolved from cairnmesh.__path__ alone.
        standard_finders = (
            importlib.machinery.BuiltinImporter,
            importlib.machinery.FrozenImporter,
            importlib.machinery.PathFinder,
        )
        sys.meta_path[:] = [finder for finder in sys.meta_path if finder in standard_finders]
        assert cairnmesh.__path__ == [str(wheel / "cairnmesh")]

        imported = []
        failed = []
        for info in pkgutil.walk_packages(cairnmesh.__path__, "cairnmesh."):
            try:
                importlib.import_module(info.name)
                imported.append(info.name)
            except Exception as exc:
                failed.append((info.name, type(exc).__name__, str(exc)))

        expected = {f"cairnmesh.{path.stem}" for path in (wheel / "cairnmesh").glob("*.py") if path.name != "__init__.py"}
        expected.add("cairnmesh")
        if set(imported) | {"cairnmesh"} != expected:
            failed.append(("module coverage", "Mismatch", f"expected={sorted(expected)} imported={sorted(imported)}"))

        from cairnmesh.session import LinkSession
        for name, factory in (("simulated", LinkSession.simulated), ("mesh", lambda: LinkSession.mesh({}))):
            try:
                factory()
            except ImportError as exc:
                if "仿真仅源码树可用，wheel 不含" not in str(exc):
                    failed.append((f"LinkSession.{name}", "ImportError", str(exc)))
            else:
                failed.append((f"LinkSession.{name}", "NoImportError", "simulation module is absent"))

        print("IMPORTED", len(imported))
        print("FAILED", failed)
        sys.exit(1 if failed else 0)
    """
    result = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(script), str(wheel)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAILED []" in result.stdout


def test_non_production_modules_remain_available_in_the_source_tree():
    assert (ROOT / "cairnmesh" / "sim.py").is_file()
    assert (ROOT / "cairnmesh" / "session_bench.py").is_file()


def test_the_sdist_holds_no_test_code(built):
    with tarfile.open(built[1]) as tar:
        names = tar.getnames()
    assert any(n.endswith("cairnmesh/mesh.py") for n in names)
    stray = [n for n in names if "testbed" in n or "/tests/" in n or "/test/" in n or "rxfilter" in n.lower()]
    assert stray == []
    non_production = {f"cairnmesh/{module}.py" for module in NON_PRODUCTION_MODULES}
    assert [n for n in names if any(n == path or n.endswith(f"/{path}") for path in non_production)] == []


def test_the_packaging_config_only_names_the_package():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+; the two builds above cover 3.10
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["setuptools"]["packages"] == ["cairnmesh"]
    assert (ROOT / "setup.py").is_file()
    assert "package-dir" not in config["tool"]["setuptools"] and "py-modules" not in config["tool"]["setuptools"]


# ------------------------------------------------------- d) nothing left active


def test_assert_no_active_filters_sees_a_live_filter(monkeypatch):
    from tests.testbed import rxfilter as F

    monkeypatch.setenv(F.ENV, "1")

    class Port:
        def set_receiver(self, callback):
            self.receiver = callback

        def send(self, data):
            pass

    key = bytes(range(16))
    F.assert_no_active_filters()
    f = F.FilteredPort(Port(), key=key, node=5, topo="5-1")
    try:
        with pytest.raises(AssertionError, match="node=5"):
            F.assert_no_active_filters()
        f.close()
        F.assert_no_active_filters()
        # a FilteredPort handed in as a port in use is caught even after close (nested ones: the registry)
        with pytest.raises(AssertionError, match="FilteredPort"):
            F.assert_no_active_filters(f)
        F.assert_no_active_filters(Port())
    finally:
        F.close_all()


pytest_plugins = ["pytester"]

PROBE = """
import pytest
from tests.testbed import rxfilter as F

class Port:
    def set_receiver(self, callback): pass

def leak(): 
    return F.FilteredPort(Port(), key=bytes(16), node=5, topo="5-1")

def test_clean():
    pass

def test_leaks_without_opting_in(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")
    leak()

@pytest.fixture
def testbed_env(monkeypatch):
    monkeypatch.setenv(F.ENV, "1")

def test_opted_in_may_hold_a_filter(testbed_env):
    leak()

def test_clean_again_after_the_leak():
    F.assert_no_active_filters()
"""


def test_a_leaked_filter_fails_the_test_that_left_it(pytester, monkeypatch):
    """tests/conftest.py checks after every test: the real file, run on a probe suite in a subprocess."""

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]))
    pytester.makeconftest((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_probe=PROBE)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-o", "addopts=", "-rA")
    outcomes = {line.split()[1].split("::")[-1]: line.split()[0] for line in result.outlines if line.startswith(("PASSED", "FAILED", "ERROR"))}
    assert outcomes["test_clean"] == "PASSED"
    assert outcomes["test_leaks_without_opting_in"] == "ERROR" or outcomes["test_leaks_without_opting_in"] == "FAILED"
    assert outcomes["test_opted_in_may_hold_a_filter"] == "PASSED"
    assert outcomes["test_clean_again_after_the_leak"] == "PASSED"  # the leak was closed, not inherited
    result.stdout.fnmatch_lines(["*test receive filter active where none is allowed*"])
