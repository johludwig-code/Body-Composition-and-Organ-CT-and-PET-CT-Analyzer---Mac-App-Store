# Build chain (plan §15): runtime → models → app → sign → verify.
# Run on an Apple Silicon Mac with Xcode 16+, xcodegen and uv installed.

SHELL := /bin/bash
# Relative on purpose: make splits target names at spaces, and with the
# repository on a volume such as "/Volumes/Ludwig T7" an absolute $(PY) as an
# order-only prerequisite never existed, so every target rebuilt the runtime
# and then failed to run it. Every recipe runs in this folder.
PY := build/runtime/python/bin/python3
APP := build/Build/Products/Release/BCOAnalyzer.app
MODELS ?= --clinical
IDENTITY ?= -
CONFIG ?= Release

.PHONY: all test test-worker test-swift runtime worker models licenses app sign verify dmg clean

all:
	$(MAKE) runtime
	$(MAKE) models licenses app verify

test: test-worker test-swift

test-worker:
	cd Worker && uv run --no-project --with pytest --with jsonschema --with numpy --with ruff==0.17.2 \
	  --with urllib3==2.8.0 --with matplotlib==3.11.2 --with SimpleITK==2.5.6 \
	  --with xlsxwriter==3.2.9 --with openpyxl --with pandas==3.0.6 \
	  --with pydicom==3.0.2 --with requests==2.34.2 --with imagecodecs==2026.8.16 \
	  --with dcm2niix==1.0.20260724 --python 3.12 \
	  bash -c 'ruff check . && ruff format --check . && pytest -q'
	uv run --no-project --with pytest --with ruff==0.17.2 --python 3.12 \
	  bash -c 'ruff check --config Worker/pyproject.toml Scripts && pytest -q Scripts/tests'

test-swift:
	cd Packages/BCOAKit && swift test
	cd Packages/BCOAStore && swift test

# `make runtime` always rebuilds. The other targets only need a runtime to
# exist: as plain prerequisites of a phony target they rebuilt it each time,
# so `make all` built the whole runtime three times.
runtime:
	Scripts/build_runtime.sh

$(PY):
	Scripts/build_runtime.sh

# Only the worker package, into a runtime that exists: what build_runtime.sh
# does with it, without rebuilding 1.2 GB. The old .pyc files go first,
# because unchecked-hash .pyc files are trusted whatever the source says.
worker: | $(PY)
	rm -rf build/runtime/python/lib/python3.12/site-packages/bcoa_worker
	cp -R Worker/bcoa_worker build/runtime/python/lib/python3.12/site-packages/bcoa_worker
	find build/runtime/python/lib/python3.12/site-packages/bcoa_worker -name __pycache__ -prune -exec rm -rf {} +
	"$(PY)" -I -m compileall -q --invalidation-mode unchecked-hash build/runtime/python/lib/python3.12/site-packages/bcoa_worker

models: | $(PY)
	"$(PY)" Scripts/fetch_models.py $(MODELS)

licenses: | $(PY)
	"$(PY)" Scripts/license_report.py --out build/licenses/THIRD_PARTY_NOTICES.md

app:
	xcodegen generate
	xcodebuild -project BCOAnalyzer.xcodeproj -scheme BCOAnalyzer -configuration $(CONFIG) \
	  -derivedDataPath build CODE_SIGN_IDENTITY="$(IDENTITY)" build

# Re-signs a built app with a Developer ID identity for the DMG channel.
# Store and TestFlight builds are archived and signed by Xcode instead.
# With the default IDENTITY "-" this signs ad hoc, without the timestamp
# server (only real identities can have a secure timestamp): a DMG for the
# owner's own Macs, not for notarisation.
sign:
	Scripts/sign_tree.sh "$(APP)/Contents/Resources/python" "$(IDENTITY)" App/Resources/Helper.entitlements
	codesign --force --sign "$(IDENTITY)" $(if $(filter -,$(IDENTITY)),--timestamp=none,--timestamp) \
	  --options runtime --entitlements App/Resources/BCOAnalyzer.entitlements "$(APP)"

verify:
	python3 Scripts/verify_bundle.py "$(APP)"

dmg: sign verify
	hdiutil create -volname "BCO Analyzer" -srcfolder "$(APP)" -ov -format UDZO build/BCOAnalyzer.dmg
	@echo "Notarise: xcrun notarytool submit build/BCOAnalyzer.dmg --keychain-profile <profile> --wait"
	@echo "Then:     xcrun stapler staple build/BCOAnalyzer.dmg && spctl -a -vv -t install build/BCOAnalyzer.dmg"

clean:
	rm -rf build BCOAnalyzer.xcodeproj
