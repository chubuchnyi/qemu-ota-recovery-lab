SHELL := /bin/bash

PROJECT_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILDER_IMAGE ?= qemu-ota-recovery-builder:2026.05
VERSION ?= v1
BROKEN ?= 0
BUILD_OUTPUT := $(PROJECT_DIR)/output/buildroot
RELEASE_DIR := $(PROJECT_DIR)/artifacts/images/$(VERSION)
UID := $(shell id -u)
GID := $(shell id -g)

DOCKER_RUN = docker run --rm \
	--user $(UID):$(GID) \
	-e HOME=/tmp \
	-e BR2_DL_DIR=/work/dl \
	-e OTA_LAB_VERSION=$(VERSION) \
	-e OTA_LAB_BROKEN=$(BROKEN) \
	-v $(PROJECT_DIR):/work \
	-w /work \
	$(BUILDER_IMAGE)

.PHONY: help bootstrap builder keys configure build publish bundle run \
	fresh-run build-broken test-e2e check clean-output

help:
	@echo "QEMU OTA/recovery lab"
	@echo
	@echo "  make bootstrap                 initialize dependencies and dev keys"
	@echo "  make build VERSION=v1          build a bootable A/B + recovery disk"
	@echo "  make run VERSION=v1            boot a persistent qcow2 overlay"
	@echo "  make fresh-run VERSION=v1      discard only the VM overlay, then boot"
	@echo "  make bundle VERSION=v2         incrementally build v2 and sign an OTA bundle"
	@echo "  make build-broken VERSION=vbad build a bundle whose health check fails"
	@echo "  make test-e2e                  build and test the complete failure matrix"
	@echo "  make serve                     serve artifacts on host port 8000"
	@echo "  make check                     run static checks"

bootstrap: builder keys
	git submodule update --init --recursive

builder:
	docker build -t $(BUILDER_IMAGE) .

keys:
	./scripts/gen-dev-keys.sh

configure: bootstrap
	$(DOCKER_RUN) make -C /work/buildroot \
		O=/work/output/buildroot \
		BR2_EXTERNAL=/work \
		qemu_ota_lab_defconfig

build: configure
	$(DOCKER_RUN) make -C /work/buildroot \
		O=/work/output/buildroot \
		BR2_EXTERNAL=/work
	$(MAKE) publish VERSION=$(VERSION)

publish:
	mkdir -p "$(RELEASE_DIR)"
	cp --sparse=always "$(BUILD_OUTPUT)/images/disk.img" \
		"$(RELEASE_DIR)/disk.img"

bundle: build
	$(DOCKER_RUN) ./scripts/create-bundle.sh \
		/work/output/buildroot \
		"$(VERSION)" \
		/work/artifacts/update-$(VERSION).raucb

build-broken:
	$(MAKE) bundle VERSION=$(VERSION) BROKEN=1

test-e2e:
	$(MAKE) build VERSION=v1
	$(MAKE) bundle VERSION=v2
	$(MAKE) build-broken VERSION=vbad
	./scripts/e2e_test.py

run:
	./scripts/run-qemu.sh $(RELEASE_DIR)/disk.img

fresh-run:
	./scripts/run-qemu.sh --fresh $(RELEASE_DIR)/disk.img

serve:
	python3 -m http.server 8000 --directory artifacts

check:
	shellcheck scripts/*.sh board/qemu-x86_64/*.sh \
		board/qemu-x86_64/rootfs-overlay/etc/init.d/S*
	bash -n scripts/*.sh board/qemu-x86_64/*.sh
	python3 -c "compile(open('scripts/e2e_test.py', encoding='utf-8').read(), 'scripts/e2e_test.py', 'exec')"

clean-output:
	@echo "Removing shared generated Buildroot output"
	rm -rf "$(BUILD_OUTPUT)"
