#!/usr/bin/make
#
# kwbackup — build and packaging helpers.
#
# The version is the subject of the current git commit (0.0.14, 0.0.15, ...).
# `make dist` bakes it into dist/kwbackup, `make build` also produces the .deb.

SHELL         := /bin/bash
PACKAGE       := kwbackup
VERSION       := $(shell git log -1 --format=%s)
DISTRIBUTION  ?= unstable
DEB_OPTS      ?=

help: ##@help Show this help
	@perl -e '$(HELP_ACTION)' $(MAKEFILE_LIST)

dist: ##@build Build the single-file artifact dist/kwbackup
	python3 build.py --version $(VERSION)

test: ##@test Build and run the built-in and end-to-end tests
	python3 build.py --test
	python3 tests/e2e.py

build: ##@build Build dist/kwbackup and the .deb package
	$(MAKE) dist
	$(MAKE) deb

deb: ##@build Build the .deb with debhelper (use DEB_OPTS=-d off Debian)
	dpkg-buildpackage -b -us -uc $(DEB_OPTS)

check-version: ##@test Compare the commit subject with debian/changelog
	@v="$$(git log -1 --format=%s)"; c="$$(dpkg-parsechangelog -S Version)"; \
	if [ "$$v" != "$$c" ]; then \
		echo "version mismatch: commit=$$v changelog=$$c"; exit 1; \
	else echo "version ok: $$v"; fi

changelog: ##@build Regenerate debian/changelog from the git history
	python3 packaging/gen-changelog.py --distribution $(DISTRIBUTION)

clean: ##@build Remove build artifacts
	rm -rf dist debian/files debian/.debhelper debian/$(PACKAGE) debian/*.substvars
	rm -f ../$(PACKAGE)_*.deb ../$(PACKAGE)_*.changes ../$(PACKAGE)_*.buildinfo
	dh_clean

.PHONY: help dist test build deb check-version changelog clean

# ------------------------------------------------
# Add the following 'help' target to your makefile, add help text after each target name starting with '\#\#'
# A category can be added with @category
GREEN  := $(shell tput -Txterm setaf 2)
YELLOW := $(shell tput -Txterm setaf 3)
WHITE  := $(shell tput -Txterm setaf 7)
RESET  := $(shell tput -Txterm sgr0)
HELP_ACTION = \
	%help; while(<>) { push @{$$help{$$2 // 'options'}}, [$$1, $$3] if /^([a-zA-Z\-_]+)\s*:.*\#\#(?:@([a-zA-Z\-]+))?\s(.*)$$/ }; \
	print "usage: make [target]\n\n"; for (sort keys %help) { print "${WHITE}$$_:${RESET}\n"; \
	for (@{$$help{$$_}}) { $$sep = " " x (32 - length $$_->[0]); print "  ${YELLOW}$$_->[0]${RESET}$$sep${GREEN}$$_->[1]${RESET}\n"; }; \
	print "\n"; }

# -eof-
