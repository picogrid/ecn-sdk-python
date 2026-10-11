UV ?= uv
PYTHON ?= python3
SOURCE_DATE_EPOCH ?= 1735689600

.PHONY: check-deps check-dco check-license sync-deps generate-reference check-reference verify-types verify-release version-sync docs-install docs-check docs-smoke-local wheelhouse

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

check-dco:
	@test -n "$$DCO_BASE" || { echo "check-dco: DCO_BASE is required" >&2; exit 2; }
	@test -n "$$DCO_HEAD" || { echo "check-dco: DCO_HEAD is required" >&2; exit 2; }
	@set --; \
	if test -n "$$DCO_EXCLUDE_HEAD"; then set -- --exclude-head "$$DCO_EXCLUDE_HEAD"; fi; \
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m scripts.check_dco \
		--base "$$DCO_BASE" --head "$$DCO_HEAD" "$$@"

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

-include scripts/public-export.mk

wheelhouse:
	PYTHONDONTWRITEBYTECODE=1 $(UV) run --python 3.11 --no-project --with pip python -m scripts.build_wheelhouse
