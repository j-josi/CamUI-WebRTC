LOG ?= ERROR

_LOG_UPPER = $(shell echo '$(LOG)' | tr '[:lower:]' '[:upper:]')
_VALID_LOG_LEVELS = DEBUG INFO WARNING ERROR CRITICAL
_LOG_LEVEL = $(if $(filter $(_LOG_UPPER),$(_VALID_LOG_LEVELS)),$(_LOG_UPPER),ERROR)
_DEV_LOG_LEVEL = $(if $(filter $(_LOG_UPPER),$(_VALID_LOG_LEVELS)),$(_LOG_UPPER),DEBUG)

# Suppress libcamera C++ logs unless LOG is DEBUG or INFO
ifeq ($(filter DEBUG INFO,$(_LOG_LEVEL)),)
_LIBCAMERA_LOG = LIBCAMERA_LOG_LEVELS=*:ERROR
else
_LIBCAMERA_LOG =
endif

ifeq ($(filter DEBUG INFO,$(_DEV_LOG_LEVEL)),)
_DEV_LIBCAMERA_LOG = LIBCAMERA_LOG_LEVELS=*:ERROR
else
_DEV_LIBCAMERA_LOG =
endif

.PHONY: help run infra dev release

help:
	@echo "Usage: make <target> [VARIABLE=value]"
	@echo ""
	@echo "Targets:"
	@echo "  run                   Start infra containers + run camui directly on host (fast iteration)"
	@echo "  infra                 Start only mediamtx and coturn containers"
	@echo "  dev                   Build, start, and follow logs of the Docker dev stack (Ctrl+C stops all containers)"
	@echo "  test                  Run all test suites"
	@echo "  release VERSION=x.y.z Tag and push a new release to remote git repository (e.g. make release VERSION=0.2.0)"
	@echo ""
	@echo "Variables:"
	@echo "  LOG=LEVEL             Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL (case-insensitive)"
	@echo "                        default for 'run': ERROR — default for 'dev': DEBUG"

run: infra
	trap 'docker compose -f docker/docker-compose.yml stop mediamtx coturn' EXIT; \
	trap '' INT; \
	$(_LIBCAMERA_LOG) LOG_LEVEL=$(_LOG_LEVEL) venv/bin/gunicorn \
		--worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
		--workers 1 \
		--bind 0.0.0.0:80 \
		--reload \
		--log-level $(_LOG_LEVEL) \
		app:app

infra:
	docker compose -f docker/docker-compose.yml up -d mediamtx coturn

dev:
	$(_DEV_LIBCAMERA_LOG) LOG_LEVEL=$(_DEV_LOG_LEVEL) docker compose -f docker/docker-compose.dev.yml up -d --build
	trap 'docker compose -f docker/docker-compose.dev.yml stop' EXIT; \
	trap '' INT; \
	docker compose -f docker/docker-compose.dev.yml logs -f camui

test:
	python tests/test_filename_gen.py
	python tests/test_system_settings.py
	python tests/test_validation.py
	python tests/test_bench_cpu.py
	python tests/test_bench_common.py
	python tests/test_bench_encoders.py
	python tests/test_camera_manager_audio.py

release:
ifndef VERSION
	$(error VERSION is not set. Usage: make release VERSION=0.2.0)
endif
	git tag v$(VERSION)
	git push origin v$(VERSION)
