#!/usr/bin/env python3
"""
Setup script for YYLO Ledger.

This package provides Git-native per-task Markdown storage, append-only history,
a disposable query cache, and an LLM-optimized interface.
"""

from setuptools import setup, find_packages
import os
import re


def read_version():
    """Read version from yylo_ledger/__init__.py (single source of truth)."""
    init_path = os.path.join(os.path.dirname(__file__), 'src', 'yylo_ledger', '__init__.py')
    with open(init_path, 'r', encoding='utf-8') as f:
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', f.read(), re.MULTILINE)
        if match:
            return match.group(1)
    raise RuntimeError("Unable to find version in kanban/__init__.py")


# Read README for long description
def read_readme():
    """Read README.md for long description."""
    readme_path = os.path.join(os.path.dirname(__file__), 'README.md')
    if os.path.exists(readme_path):
        with open(readme_path, 'r', encoding='utf-8') as f:
            return f.read()
    return "YYLO Ledger: Git-native task management with safe per-task Markdown storage."

# Runtime dependency metadata must be self-contained in the build script.  The
# 2.0.5 sdist omitted requirements.txt, causing sdist-derived wheels to silently
# lose this requirement.  Keep one package-metadata source of truth here.
RUNTIME_REQUIREMENTS = ["ruamel.yaml>=0.18.6,<0.19"]

setup(
    name="yylo-ledger",
    version=read_version(),
    author="JUNO AI INC.",
    author_email="support@yylo.dev",
    description="YYLO Ledger: Git-native task management with safe per-task Markdown storage",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/yylo-dev/yylo-ledger",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Bug Tracking",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Utilities",
    ],
    python_requires=">=3.8",
    install_requires=RUNTIME_REQUIREMENTS,
    entry_points={
        "console_scripts": [
            "yylo-ledger=yylo_ledger.cli:main",
            # Bounded 0.1 RC migration shims; each emits a deprecation action.
            "juno-ledger=yylo_ledger.cli:legacy_main",
            "ledger-juno=yylo_ledger.cli:legacy_main",
            "jl=yylo_ledger.cli:legacy_main",
            "juno-kanban=yylo_ledger.cli:legacy_main",
            "juno-feedback=yylo_ledger.cli:legacy_main",
            "kanban-juno=yylo_ledger.cli:legacy_main",
        ],
    },
    include_package_data=True,
    package_data={
        "yylo_ledger": ["*.json", "*.md"],
    },
    project_urls={
        "Bug Reports": "https://github.com/yylo-dev/yylo-ledger/issues",
        "Source": "https://github.com/yylo-dev/yylo-ledger",
        "Documentation": "https://yylo.dev",
    },
    keywords="ledger kanban task-manager cli markdown git sqlite productivity",
    zip_safe=False,
)
