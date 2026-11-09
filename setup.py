"""Setuptools hook that keeps development-only modules out of releases."""

from __future__ import annotations

from setuptools import setup
from setuptools.command.build_py import build_py

# Runtime modules are included as before. These modules stay available from a
# source checkout (and to tests/tools there), but are not part of the wheel or sdist.
NON_PRODUCTION_MODULES = frozenset(
    {
        "channel",
        "conformance",
        "demo",
        "discovery_bench",
        "gate",
        "liveness_demo",
        "mesh_demo",
        "mesh_sim",
        "node_conformance",
        "radio_bench",
        "radio_model",
        "relay_conformance",
        "robustness_bench",
        "session_bench",
        "session_demo",
        "sim",
        "simpool",
        "stress_bench",
        "vectors",
        "wake_bench",
    }
)


class ProductionBuildPy(build_py):
    """Build ``cairnmesh`` without its simulation and development entry points."""

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        if package != "cairnmesh":
            return modules
        return [module for module in modules if module[1] not in NON_PRODUCTION_MODULES]


setup(cmdclass={"build_py": ProductionBuildPy})
