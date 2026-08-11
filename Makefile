UV ?= uv
SOURCE_DATE_EPOCH ?= 1735689600
PUBLIC_EXPORT_DIR ?= build/public-export
PUBLIC_EXPORT_RECORD ?= build/public-export-record.json

.PHONY: check-deps check-license sync-deps generate-reference check-reference verify-types verify-release version-sync check-public-export public-export dry-run-cutover docs-install docs-check docs-smoke-local wheelhouse

docs-install:
	npm --prefix docs ci

docs-check:
	npm --prefix docs run docs:check

docs-smoke-local:
	npm --prefix docs run docs:smoke:local

sync-deps:
	$(UV) run --python 3.11 --no-project --with packaging==26.3 python scripts/sync_dep_locks.py

check-deps:
	$(UV) run --python 3.11 --no-project --with packaging==26.3 python scripts/sync_dep_locks.py --check

# The gate is standard-library only, so it must not depend on resolving the
# project environment: a license violation has to be reportable even then.
check-license:
	$(UV) run --python 3.11 --no-project python -m scripts.license_policy

generate-reference:
	$(UV) run --frozen python -m scripts.generate_api_reference --write

check-reference:
	$(UV) run --frozen python -m scripts.generate_api_reference --check

verify-types:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --frozen --isolated --python 3.11 python -m scripts.verify_types

verify-release: check-deps check-license
	PYTHONDONTWRITEBYTECODE=1 SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) $(UV) run --frozen --isolated --python 3.11 python -m scripts.verify_release

# The version-agreement gate on its own, which `verify-release` also runs. It is
# fast and needs no build, so it is the one to run while editing a version.
version-sync:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --frozen python -m scripts.version_sync

check-public-export:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --python 3.11 --no-project --with packaging==26.3 python -m scripts.public_export --verify

public-export:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --python 3.11 --no-project --with packaging==26.3 python -m scripts.public_export --out "$(PUBLIC_EXPORT_DIR)" --record "$(PUBLIC_EXPORT_RECORD)" --clean

dry-run-cutover:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --python 3.11 --no-project --with packaging==26.3 python -m scripts.public_export --dry-run-cutover

wheelhouse:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --python 3.11 --no-project --with pip python -m scripts.build_wheelhouse
