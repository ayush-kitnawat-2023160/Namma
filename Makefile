IMAGE ?= namma-iitb:latest
.PHONY: build smoke check shell help
help:
	@echo "make build | smoke | check | shell"
build:
	NAMMA_IMAGE=$(IMAGE) ./build.sh
smoke:
	NAMMA_IMAGE=$(IMAGE) ./run.sh smoke
check:
	NAMMA_IMAGE=$(IMAGE) ./run.sh check
shell:
	NAMMA_IMAGE=$(IMAGE) ./run.sh shell

