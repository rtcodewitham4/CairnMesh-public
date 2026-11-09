from __future__ import annotations

import json

from tools.quality_gate import check, mutation_audit_missing


def _project(tmp_path, source: str, classes: dict, files: dict | None = None) -> tuple:
    package = tmp_path / "cairnmesh"
    package.mkdir()
    (package / "sample.py").write_text(source, encoding="utf-8")
    baseline = tmp_path / "quality_baseline.json"
    baseline.write_text(
        json.dumps({"files": files or {}, "classes": classes}),
        encoding="utf-8",
    )
    return tmp_path, baseline


def _file_with_lines(line_count: int) -> str:
    return "pass\n" + "# padding\n" * (line_count - 1)


def _class(init_params: int | None = None, methods: int = 0) -> str:
    init = ""
    if init_params is not None:
        parameters = ["self", *(f"arg{i}" for i in range(init_params))]
        init = f"    def __init__({', '.join(parameters)}):\n        pass\n\n"
    body = "".join(f"    def method{i}(self):\n        pass\n\n" for i in range(methods))
    return f"class Sample:\n{init}{body}" if init or body else "class Sample:\n    pass\n"


def test_file_baseline_growth_fails(tmp_path):
    root, baseline = _project(
        tmp_path,
        _file_with_lines(1501),
        {},
        {"cairnmesh/sample.py": 1500},
    )

    failures = check(root, baseline)

    assert len(failures) == 1
    assert failures[0].kind == "file lines"
    assert "grew from baseline 1500 to 1501" in failures[0].format()


def test_file_reduction_passes(tmp_path):
    root, baseline = _project(
        tmp_path,
        _file_with_lines(1500),
        {},
        {"cairnmesh/sample.py": 1501},
    )

    assert check(root, baseline) == []


def test_new_file_violation_fails(tmp_path):
    root, baseline = _project(tmp_path, _file_with_lines(1501), {})

    failures = check(root, baseline)

    assert len(failures) == 1
    assert failures[0].baseline is None
    assert failures[0].kind == "file lines"
    assert "new limit is 1500" in failures[0].format()


def test_init_parameter_baseline_growth_fails(tmp_path):
    root, baseline = _project(
        tmp_path,
        _class(init_params=16),
        {"cairnmesh/sample.py::Sample": {"init_params": 15}},
    )

    failures = check(root, baseline)

    assert len(failures) == 1
    assert failures[0].kind == "__init__ parameters"
    assert "grew from baseline 15 to 16" in failures[0].format()


def test_init_parameter_reduction_passes(tmp_path):
    root, baseline = _project(
        tmp_path,
        _class(init_params=14),
        {"cairnmesh/sample.py::Sample": {"init_params": 15}},
    )

    assert check(root, baseline) == []


def test_new_init_parameter_violation_fails(tmp_path):
    root, baseline = _project(tmp_path, _class(init_params=17), {})

    failures = check(root, baseline)

    assert len(failures) == 1
    assert failures[0].kind == "__init__ parameters"


def test_class_method_count_is_not_checked(tmp_path):
    root, baseline = _project(tmp_path, _class(methods=41), {})

    assert check(root, baseline) == []


def test_mutation_audit_lists_missing_modules_without_failing_gate(tmp_path):
    root, baseline = _project(tmp_path, _class(), {})
    (root / "cairnmesh" / "extra.py").write_text("", encoding="utf-8")
    audit = root / "tests" / "audit"
    audit.mkdir(parents=True)
    (audit / "mutate.py").write_text('MODULES = ["sample"]\n', encoding="utf-8")

    assert mutation_audit_missing(root) == ["extra"]
    assert check(root, baseline) == []
