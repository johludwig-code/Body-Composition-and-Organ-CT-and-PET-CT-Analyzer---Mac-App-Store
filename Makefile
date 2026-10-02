# Build chain (plan §15): runtime → models → app → sign → verify.
# Run on an Apple Silicon Mac with Xcode 16+, xcodegen and uv installed.

SHELL := /bin/bash
ROOT := $(abspath .)
PY := $(ROOT)/build/runtime/python/bin/python3
APP := $(ROOT)/build/Build/Products/Release/BCOAnalyzer.app
MODELS ?= --clinical
IDENTITY ?= -
CONFIG ?= Release

.PHONY: all test test-worker test-swift runtime models licenses app sign verify dmg clean

all:
	$(MAKE) runtime
	$(MAKE) models licenses app verify

test: test-worker test-swift

test-worker:
	cd Worker && uv run --no-project --with pytest --with jsonschema --with numpy --with ruff --python 3.12 \
	  bash -c 'ruff check . && ruff format --check . && pytest -q'
	uv run --no-project --with pytest --with ruff --python 3.12 \
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

models: | $(PY)
	$(PY) Scripts/fetch_models.py $(MODELS)

licenses: | $(PY)
	$(PY) Scripts/license_report.py --out build/licenses/THIRD_PARTY_NOTICES.md

app:
	xcodegen generate
	xcodebuild -project BCOAnalyzer.xcodeproj -scheme BCOAnalyzer -configuration $(CONFIG) \
	  -derivedDataPath build CODE_SIGN_IDENTITY="$(IDENTITY)" build

# Re-signs a built app with a Developer ID identity for the DMG channel.
# Store and TestFlight builds are archived and signed by Xcode instead.
sign:
	Scripts/sign_tree.sh "$(APP)/Contents/Resources/python" "$(IDENTITY)" App/Resources/Helper.entitlements
	codesign --force --sign "$(IDENTITY)" --timestamp --options runtime \
	  --entitlements App/Resources/BCOAnalyzer.entitlements "$(APP)"

verify:
	python3 Scripts/verify_bundle.py "$(APP)"

dmg: sign verify
	hdiutil create -volname "BCO Analyzer" -srcfolder "$(APP)" -ov -format UDZO build/BCOAnalyzer.dmg
	@echo "Notarise: xcrun notarytool submit build/BCOAnalyzer.dmg --keychain-profile <profile> --wait"
	@echo "Then:     xcrun stapler staple build/BCOAnalyzer.dmg && spctl -a -vv -t install build/BCOAnalyzer.dmg"

clean:
	rm -rf build BCOAnalyzer.xcodeproj
