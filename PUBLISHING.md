# YYLO Ledger PyPI Publishing Guide

This document provides complete instructions for building and publishing YYLO Ledger. The established PyPI distribution name remains `yylo-ledger` for backward compatibility.

## Quick Start

```bash
# 1. Bump version (if needed)
python scripts/bump_version.py patch

# 2. Test build locally
./scripts/publish.sh --dry-run

# 3. Test on TestPyPI first (recommended)
PYPI_TEST_TOKEN="your-test-pypi-token" ./scripts/publish.sh --test-pypi

# 4. Publish to PyPI
PIP_UPLOAD_TOKEN="your-pypi-token" ./scripts/publish.sh
```

## Prerequisites

### 1. System Requirements

**Minimal System Requirements**:
- Python 3.8+ with `venv` module available
- No manual dependency installation required

**Note**: The publish script automatically creates and manages its own virtual environment (`.venv`) with all required packages (`setuptools`, `wheel`, `twine`, `build`) for complete isolation from your system Python.

### 2. Get PyPI API Tokens

1. **PyPI Account**: Create account at https://pypi.org/account/register/
2. **TestPyPI Account** (optional): Create account at https://test.pypi.org/account/register/
3. **API Tokens**: Generate tokens at:
   - PyPI: https://pypi.org/manage/account/token/
   - TestPyPI: https://test.pypi.org/manage/account/token/

### 3. Set Environment Variables

```bash
# For PyPI (production)
export PIP_UPLOAD_TOKEN="pypi-xxxx..."

# For TestPyPI (testing)
export PYPI_TEST_TOKEN="pypi-xxxx..."
```

## Virtual Environment Management

The publish script uses an **isolated virtual environment** (`.venv`) for all operations:

**Key Features**:
- ✅ **Automatic Setup**: Script creates `.venv` if it doesn't exist
- ✅ **Idempotent**: Reuses existing virtual environment safely
- ✅ **Isolated Dependencies**: No conflicts with system Python packages
- ✅ **Modern Tools**: Uses `python3 -m build` instead of deprecated setup.py methods
- ✅ **Complete Automation**: No manual dependency management required

**What the script does automatically**:
1. Creates virtual environment at `PROJECT_ROOT/.venv`
2. Installs latest versions of: `setuptools`, `wheel`, `twine`, `build`
3. Uses virtual environment for all Python operations
4. Reuses existing environment if present (fast subsequent runs)

## Version Management

### Using the Version Bump Script

```bash
# Increment patch version (1.0.0 -> 1.0.1)
python scripts/bump_version.py patch

# Increment minor version (1.0.1 -> 1.1.0)
python scripts/bump_version.py minor

# Increment major version (1.1.0 -> 2.0.0)
python scripts/bump_version.py major

# Set specific version
python scripts/bump_version.py --set 0.2.0

# Create git tag with version bump
python scripts/bump_version.py patch --tag

# Preview changes without modifying files
python scripts/bump_version.py minor --dry-run
```

### Manual Version Update

Edit `setup.py` and change the version:

```python
setup(
    name="yylo-ledger",
    version="0.1.1",  # Update this line
    # ... rest of setup
)
```

## Publishing Process

### Step 1: Prepare Release

```bash
# Ensure you're on the main branch with latest changes
git checkout main
git pull origin main

# Update version (choose appropriate bump type)
python scripts/bump_version.py patch

# Commit version change
git add setup.py
git commit -m "Bump version to $(python scripts/bump_version.py --dry-run patch 2>/dev/null | grep 'Bumping' | cut -d' ' -f5)"
```

### Step 2: Test Build Locally

```bash
# Test build without uploading
./scripts/publish.sh --dry-run
```

This will:
- Set up isolated virtual environment (`.venv`) with required packages
- Clean previous build artifacts
- Build source distribution (.tar.gz) and wheel (.whl) using modern `python3 -m build`
- Validate distributions with twine
- Show what would be uploaded
- All operations run in isolated environment for safety

### Step 3: Test on TestPyPI (Recommended)

```bash
# Upload to TestPyPI first
PYPI_TEST_TOKEN="your-test-token" ./scripts/publish.sh --test-pypi
```

Test the uploaded package:

```bash
# Install from TestPyPI
pip install --index-url https://test.pypi.org/simple/ yylo-ledger

# Test basic functionality
yylo-ledger --help
yylo-ledger create "Test task"
yylo-ledger list
```

### Step 4: Publish to PyPI

```bash
# Upload to PyPI (production)
PIP_UPLOAD_TOKEN="your-pypi-token" ./scripts/publish.sh
```

### Step 5: Post-Publishing

```bash
# Create and push git tag
git tag v$(python -c "import re; print(re.search(r'version=\"([^\"]+)\"', open('setup.py').read()).group(1))")
git push origin --tags

# Test installation from PyPI
pip install --upgrade yylo-ledger
```

## Script Options

### publish.sh Options

```bash
./scripts/publish.sh [OPTIONS]

Options:
  --dry-run     Build and validate locally without uploading
  --test-pypi   Upload to TestPyPI instead of PyPI
  --help        Show help message
```

### bump_version.py Options

```bash
python scripts/bump_version.py [COMMAND] [OPTIONS]

Commands:
  patch         Increment patch version (default)
  minor         Increment minor version
  major         Increment major version
  --set VERSION Set specific version

Options:
  --tag         Create git tag with new version
  --dry-run     Show changes without modifying files
  --help        Show help message
```

## Troubleshooting

### Common Issues

1. **Virtual Environment Issues**
   ```bash
   # Remove and recreate virtual environment if corrupted
   rm -rf .venv
   ./scripts/publish.sh --dry-run  # Will recreate automatically

   # Check if python3-venv is available (Ubuntu/Debian)
   sudo apt-get install python3-venv
   ```

2. **Authentication Errors**
   ```bash
   # Verify token is set correctly
   echo $PIP_UPLOAD_TOKEN

   # Use double-quotes for token with special characters
   export PIP_UPLOAD_TOKEN="pypi-token-here"
   ```

3. **Version Already Exists**
   ```bash
   # Bump version and try again
   python scripts/bump_version.py patch
   ```

4. **Build Failures**
   ```bash
   # Clean and rebuild (virtual environment automatically recreates packages)
   rm -rf dist/ build/ *.egg-info/
   ./scripts/publish.sh --dry-run
   ```

### Distribution Validation

```bash
# Manually validate distributions (using virtual environment)
source .venv/bin/activate
twine check dist/*

# View package contents
tar -tzf dist/yylo_ledger-0.1.0.tar.gz
unzip -l dist/yylo_ledger-0.1.0-py3-none-any.whl
```

### Testing Installation

```bash
# Test from different sources
pip install yylo-ledger                              # From PyPI
pip install --index-url https://test.pypi.org/simple/ yylo-ledger  # From TestPyPI
pip install dist/yylo_ledger-0.1.0-py3-none-any.whl # Local wheel
```

## Package Information

- **Product Name**: YYLO Ledger
- **Package Name**: `yylo-ledger` (compatibility identity)
- **Preferred Entry Points**: `yylo-ledger`, `ledger-juno`, `jl`
- **Legacy Entry Points**: `yylo-ledger`, `juno-feedback`, `kanban-juno`
- **Python Versions**: 3.8+
- **Dependencies**: `ruamel.yaml>=0.18.6,<0.19`
- **License**: MIT
- **Repository**: https://github.com/yylo-dev/yylo-ledger

## Release Checklist

- [ ] Version bumped in setup.py
- [ ] Changes committed to git
- [ ] Dry-run build successful
- [ ] Tested on TestPyPI (optional but recommended)
- [ ] Published to PyPI
- [ ] Git tag created and pushed
- [ ] Installation from PyPI verified

## Automation Examples

### Complete Release Script

```bash
#!/bin/bash
# complete-release.sh - Full release automation

set -e

# Get new version
NEW_VERSION=$(python scripts/bump_version.py patch --dry-run | grep "Bumping" | cut -d' ' -f5)

echo "Releasing version $NEW_VERSION"

# Bump version
python scripts/bump_version.py patch

# Commit version change
git add setup.py
git commit -m "Bump version to $NEW_VERSION"

# Test build
./scripts/publish.sh --dry-run

# Test on TestPyPI
read -p "Test on TestPyPI first? (y/n): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    ./scripts/publish.sh --test-pypi
    read -p "TestPyPI test successful? Continue to PyPI? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Publish to PyPI
./scripts/publish.sh

# Create and push tag
git tag v$NEW_VERSION
git push origin v$NEW_VERSION
git push origin main

echo "Successfully released version $NEW_VERSION"
```

Make executable:
```bash
chmod +x complete-release.sh
```
