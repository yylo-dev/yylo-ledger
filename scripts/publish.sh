#!/bin/bash
#
# Automated PyPI Build and Publish Script for yylo-ledger
#
# This script automates the process of building and publishing the yylo-ledger
# package to PyPI with proper version management, cleanup, and isolated environment.
#
# Prerequisites:
# - Python 3.8+ with venv module available
# - PyPI account with API token
# - Environment variable PIP_UPLOAD_TOKEN set to your PyPI API token
#
# This script automatically creates and manages a virtual environment (.venv)
# with all required packages (setuptools, wheel, twine, build) for isolation.
#
# Usage:
#     ./scripts/publish.sh [--dry-run] [--test-pypi]
#
# Options:
#     --dry-run     Build and test locally without uploading
#     --test-pypi   Upload to TestPyPI instead of PyPI
#     --help        Show this help message
#
# Environment Variables:
#     PIP_UPLOAD_TOKEN    Your PyPI API token (required for upload)
#     PYPI_TEST_TOKEN     Your TestPyPI API token (for --test-pypi)
#

set -euo pipefail  # Exit on error, undefined vars, pipe failures

# Script configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VERSION_FILE="$PROJECT_ROOT/src/kanban/__init__.py"
VENV_DIR="$PROJECT_ROOT/.venv"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Parse command line arguments
DRY_RUN=false
TEST_PYPI=false

for arg in "$@"; do
    case $arg in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --test-pypi)
            TEST_PYPI=true
            shift
            ;;
        --help)
            echo "Automated PyPI Build and Publish Script for yylo-ledger"
            echo ""
            echo "Usage: $0 [--dry-run] [--test-pypi]"
            echo ""
            echo "Options:"
            echo "  --dry-run     Build and test locally without uploading"
            echo "  --test-pypi   Upload to TestPyPI instead of PyPI"
            echo "  --help        Show this help message"
            echo ""
            echo "Environment Variables:"
            echo "  PIP_UPLOAD_TOKEN    Your PyPI API token (required for upload)"
            echo "  PYPI_TEST_TOKEN     Your TestPyPI API token (for --test-pypi)"
            exit 0
            ;;
        *)
            log_error "Unknown option: $arg"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Function to setup virtual environment
setup_virtual_environment() {
    log_info "Setting up virtual environment..."

    cd "$PROJECT_ROOT"

    # Check if virtual environment already exists
    if [ -d "$VENV_DIR" ]; then
        log_info "Virtual environment already exists at $VENV_DIR"
    else
        log_info "Creating virtual environment at $VENV_DIR"
        python3 -m venv "$VENV_DIR"
        log_success "Virtual environment created"
    fi

    # Activate virtual environment
    source "$VENV_DIR/bin/activate"
    log_success "Virtual environment activated"

    # Upgrade pip in virtual environment
    log_info "Upgrading pip in virtual environment..."
    python3 -m pip install --upgrade pip

    # Install required packages
    log_info "Installing required packages in virtual environment..."
    local required_packages="setuptools wheel twine build"
    python3 -m pip install $required_packages

    log_success "Virtual environment setup complete"
}

# Function to check dependencies
check_dependencies() {
    log_info "Checking basic system dependencies..."

    if ! command -v python3 &> /dev/null; then
        log_error "python3 is not installed"
        exit 1
    fi

    # Check if python3 has venv module
    if ! python3 -m venv --help &> /dev/null; then
        log_error "python3-venv is not available"
        log_info "Install with: apt-get install python3-venv (Ubuntu/Debian) or equivalent"
        exit 1
    fi

    log_success "System dependencies are available"
}

# Function to get current version from setup.py
get_current_version() {
    # Ensure virtual environment is activated if it exists
    if [ -d "$VENV_DIR" ]; then
        source "$VENV_DIR/bin/activate"
    fi

    python3 -c "
import re
with open('$VERSION_FILE', 'r') as f:
    content = f.read()
    match = re.search(r'^__version__\s*=\s*[\"\\']([^\"\\' ]+)[\"\\']', content, re.MULTILINE)
    if match:
        print(match.group(1))
    else:
        print('0.0.0')
"
}

# Function to clean previous builds
clean_build_artifacts() {
    log_info "Cleaning previous build artifacts..."

    cd "$PROJECT_ROOT"

    # Remove build directories and files
    rm -rf dist/ build/ *.egg-info/

    # Remove compiled Python files
    find . -type f -name "*.pyc" -delete
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

    log_success "Build artifacts cleaned"
}

# Function to build distributions
build_distributions() {
    log_info "Building source distribution and wheel..."

    cd "$PROJECT_ROOT"

    # Ensure virtual environment is activated
    source "$VENV_DIR/bin/activate"

    # Build distributions using modern build tool
    python3 -m build

    # List built files
    log_info "Built distributions:"
    ls -la dist/

    log_success "Distributions built successfully"
}

# Function to validate distributions
validate_distributions() {
    log_info "Validating distributions..."

    cd "$PROJECT_ROOT"

    # Ensure virtual environment is activated
    source "$VENV_DIR/bin/activate"

    # Check distributions with twine
    python3 -m twine check dist/*

    log_success "Distributions are valid"
}

# Function to upload to PyPI
upload_to_pypi() {
    local repository="$1"
    local token_var="$2"

    if [ "$DRY_RUN" = true ]; then
        log_info "DRY RUN: Would upload to $repository"
        return 0
    fi

    log_info "Uploading to $repository..."

    # Ensure virtual environment is activated
    source "$VENV_DIR/bin/activate"

    # Check if token is set
    if [ -z "${!token_var:-}" ]; then
        log_error "Environment variable $token_var is not set"
        log_info "Get your token from https://pypi.org/manage/account/token/"
        exit 1
    fi

    # Upload with twine
    if [ "$repository" = "PyPI" ]; then
        python3 -m twine upload dist/* --username __token__ --password "${!token_var}"
    else
        python3 -m twine upload --repository testpypi dist/* --username __token__ --password "${!token_var}"
    fi

    log_success "Successfully uploaded to $repository"
}

# Function to show post-upload instructions
show_post_upload_instructions() {
    local current_version="$1"

    echo
    log_success "Package yylo-ledger v$current_version has been published!"
    echo
    echo "Installation instructions:"
    if [ "$TEST_PYPI" = true ]; then
        echo "  pip install --index-url https://test.pypi.org/simple/ yylo-ledger"
    else
        echo "  pip install yylo-ledger"
    fi
    echo
    echo "Usage:"
    echo "  yylo-ledger --help"
    echo "  yylo-ledger create \"My first task\" --tags important"
    echo "  yylo-ledger list --limit 5"
    echo
}

# Function to upgrade yylo-ledger in the monorepo's .venv_juno
upgrade_local_install() {
    local current_version="$1"

    # Find the monorepo .venv_juno (one level up from yylo_ledger/)
    local mono_root
    mono_root="$(dirname "$PROJECT_ROOT")"
    local mono_venv="$mono_root/.venv_juno"

    if [ ! -d "$mono_venv" ]; then
        log_warning "No .venv_juno found at $mono_venv — skipping local upgrade"
        return 0
    fi

    log_info "Upgrading yylo-ledger in $mono_venv to v$current_version..."

    # Activate the monorepo venv and upgrade from PyPI
    (
        source "$mono_venv/bin/activate"
        python3 -m pip install --upgrade "yylo-ledger==$current_version" --quiet 2>/dev/null \
            || python3 -m pip install --upgrade yylo-ledger --quiet 2>/dev/null
    )

    if [ $? -eq 0 ]; then
        log_success "Local .venv_juno upgraded to yylo-ledger v$current_version"
    else
        log_warning "Failed to upgrade local .venv_juno — run: pip install --upgrade yylo-ledger"
    fi
}

# Main execution
main() {
    log_info "Starting PyPI publish process for yylo-ledger"

    # Check system dependencies
    check_dependencies

    # Setup virtual environment
    setup_virtual_environment

    # Get current version
    local current_version
    current_version=$(get_current_version)
    log_info "Current version: $current_version"

    # Clean previous builds
    clean_build_artifacts

    # Build distributions
    build_distributions

    # Validate distributions
    validate_distributions

    # Upload to appropriate repository
    if [ "$DRY_RUN" = false ]; then
        if [ "$TEST_PYPI" = true ]; then
            upload_to_pypi "TestPyPI" "PYPI_TEST_TOKEN"
        else
            upload_to_pypi "PyPI" "PIP_UPLOAD_TOKEN"
        fi

        show_post_upload_instructions "$current_version"

        # Upgrade local .venv_juno install to match the just-published version
        upgrade_local_install "$current_version"
    else
        log_info "DRY RUN completed - no upload performed"
        log_info "To publish, run: $0 $([ "$TEST_PYPI" = true ] && echo "--test-pypi")"
    fi

    log_success "Publish process completed!"
}

# Run main function
main "$@"