#!/usr/bin/env python3
"""
Version Bump Utility for yylo-ledger

This script automates version management for the yylo-ledger package.
It increments major, minor, or patch versions in src/kanban/__init__.py and
can optionally create a git tag.

Usage:
    python scripts/bump_version.py [patch|minor|major] [--tag] [--dry-run]
    python scripts/bump_version.py --set VERSION [--tag] [--dry-run]

Arguments:
    patch       Increment patch version (default: 1.0.0 -> 1.0.1)
    minor       Increment minor version (1.0.1 -> 1.1.0)
    major       Increment major version (1.1.0 -> 2.0.0)
    --set       Set specific version (e.g., --set 0.1.0)

Options:
    --tag            Create a git tag with the new version
    --dry-run        Show what would be changed without modifying files
    --no-pypi-check  Skip published-version check and use local version only
    --help           Show this help message

Examples:
    python scripts/bump_version.py patch            # 1.0.0 -> 1.0.1
    python scripts/bump_version.py minor --tag      # 1.0.1 -> 1.1.0 + git tag
    python scripts/bump_version.py --set 0.1.0      # Set to 0.1.0
    python scripts/bump_version.py major --dry-run  # Preview major bump
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


class VersionBumper:
    """Utility class for version management."""

    def __init__(self, setup_file: Path):
        """Initialize with path to setup.py file."""
        self.setup_file = setup_file
        self.init_file = setup_file.parent / 'src' / 'kanban' / '__init__.py'
        if not self.init_file.exists():
            raise FileNotFoundError(f"Init file not found: {self.init_file}")

    def get_current_version(self) -> str:
        """Extract current local version from __init__.py."""
        content = self.init_file.read_text(encoding='utf-8')
        match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
        if not match:
            raise ValueError("Could not find __version__ in __init__.py")
        return match.group(1)

    def get_latest_published_version(self, package_name: str = "yylo-ledger", timeout_seconds: int = 5) -> Optional[str]:
        """Fetch latest published package version from PyPI.

        Returns None when network is unavailable, package does not exist, or
        the returned version cannot be parsed.
        """
        url = f"https://pypi.org/pypi/{package_name}/json"

        try:
            with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
                payload = json.load(response)
            published_version = payload.get("info", {}).get("version")
            if not published_version:
                return None

            # Validate format before returning
            self.parse_version(published_version)
            return published_version
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            return None

    def resolve_bump_baseline_version(
        self,
        package_name: str = "yylo-ledger",
        check_pypi: bool = True,
    ) -> Tuple[str, str, Optional[str]]:
        """Return (baseline_version, local_version, published_version).

        Why: bumping from a stale local checkout can generate a lower version than
        what's already published. Using the max(local, published) baseline avoids
        version rollback while keeping local __init__.py as the editable source.
        """
        local_version = self.get_current_version()
        published_version = self.get_latest_published_version(package_name) if check_pypi else None

        baseline_version = local_version
        if published_version and self.parse_version(published_version) > self.parse_version(local_version):
            baseline_version = published_version

        return baseline_version, local_version, published_version

    def parse_version(self, version: str) -> Tuple[int, int, int]:
        """Parse version string into (major, minor, patch) tuple."""
        try:
            parts = version.split('.')
            if len(parts) != 3:
                raise ValueError()
            return tuple(int(part) for part in parts)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid version format: {version}. Expected: major.minor.patch")

    def format_version(self, major: int, minor: int, patch: int) -> str:
        """Format version components into version string."""
        return f"{major}.{minor}.{patch}"

    def bump_version(self, bump_type: str, current_version: Optional[str] = None) -> str:
        """Bump version based on type (major, minor, patch)."""
        current = current_version or self.get_current_version()
        major, minor, patch = self.parse_version(current)

        if bump_type == "major":
            major += 1
            minor = 0
            patch = 0
        elif bump_type == "minor":
            minor += 1
            patch = 0
        elif bump_type == "patch":
            patch += 1
        else:
            raise ValueError(f"Invalid bump type: {bump_type}")

        return self.format_version(major, minor, patch)

    def set_version(self, new_version: str) -> str:
        """Set specific version after validation."""
        # Validate version format
        self.parse_version(new_version)
        return new_version

    def update_version_file(self, new_version: str, dry_run: bool = False) -> bool:
        """Update version in __init__.py (single source of truth; setup.py reads from it)."""
        content = self.init_file.read_text(encoding='utf-8')

        pattern = r'(^__version__\s*=\s*["\'])([^"\']+)(["\'])'
        match = re.search(pattern, content, re.MULTILINE)

        if not match:
            raise ValueError("Could not find __version__ pattern in __init__.py")

        old_version = match.group(2)
        new_content = re.sub(pattern, f'{match.group(1)}{new_version}{match.group(3)}', content, flags=re.MULTILINE)

        if dry_run:
            print(f"Would update {self.init_file}: {old_version} -> {new_version}")
            return True

        self.init_file.write_text(new_content, encoding='utf-8')
        print(f"Updated {self.init_file}: {old_version} -> {new_version}")
        return True

    def create_git_tag(self, version: str, dry_run: bool = False) -> bool:
        """Create git tag for the version."""
        tag_name = f"v{version}"

        if dry_run:
            print(f"Would create git tag: {tag_name}")
            return True

        try:
            # Check if we're in a git repository
            subprocess.run(['git', 'rev-parse', '--git-dir'],
                         check=True, capture_output=True)
        except subprocess.CalledProcessError:
            print("Warning: Not in a git repository, skipping tag creation")
            return False

        try:
            # Create annotated tag
            subprocess.run([
                'git', 'tag', '-a', tag_name,
                '-m', f"Release version {version}"
            ], check=True)
            print(f"Created git tag: {tag_name}")

            # Show tag creation advice
            print(f"To push tag to remote: git push origin {tag_name}")
            return True
        except subprocess.CalledProcessError as e:
            print(f"Error creating git tag: {e}")
            return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Version management utility for yylo-ledger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('\n\n')[1]  # Show examples from docstring
    )

    # Version bump type or set specific version
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('bump_type', nargs='?', choices=['patch', 'minor', 'major'],
                      help='Type of version bump (default: patch)')
    group.add_argument('--set', dest='set_version', metavar='VERSION',
                      help='Set specific version (e.g., 0.1.0)')

    # Options
    parser.add_argument('--tag', action='store_true',
                       help='Create git tag with new version')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show changes without modifying files')
    parser.add_argument('--no-pypi-check', action='store_true',
                       help='Skip PyPI latest-version check and use local version as bump baseline')

    # Parse arguments
    args = parser.parse_args()

    # Default to patch if no specific bump type provided
    if not args.set_version and not args.bump_type:
        args.bump_type = 'patch'

    # Find setup.py file
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    setup_file = project_root / 'setup.py'

    try:
        # Initialize version bumper
        bumper = VersionBumper(setup_file)

        # Calculate new version
        if args.set_version:
            current_version = bumper.get_current_version()
            print(f"Current local version: {current_version}")
            new_version = bumper.set_version(args.set_version)
            print(f"Setting version to: {new_version}")
        else:
            baseline_version, local_version, published_version = bumper.resolve_bump_baseline_version(
                check_pypi=not args.no_pypi_check
            )
            print(f"Current local version: {local_version}")
            if published_version:
                print(f"Latest published version: {published_version}")
            elif not args.no_pypi_check:
                print("Latest published version: unavailable (network/package lookup failed)")

            if baseline_version != local_version:
                print(
                    "Using published version as bump baseline to avoid version rollback: "
                    f"{baseline_version}"
                )

            new_version = bumper.bump_version(args.bump_type, current_version=baseline_version)
            print(f"Bumping {args.bump_type} version to: {new_version}")

        # Update __init__.py (setup.py reads version from it)
        if bumper.update_version_file(new_version, args.dry_run):
            # Create git tag if requested
            if args.tag:
                bumper.create_git_tag(new_version, args.dry_run)

        if not args.dry_run:
            print(f"\nVersion successfully updated to {new_version}")
            print("Next steps:")
            print("1. Review changes: git diff src/kanban/__init__.py")
            print("2. Commit changes: git add src/kanban/__init__.py && git commit -m 'Bump version to {}'".format(new_version))
            if args.tag:
                print("3. Push tag: git push origin v{}".format(new_version))
            print("4. Publish to PyPI: ./scripts/publish.sh")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()