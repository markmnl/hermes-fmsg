PLUGIN_DIR ?= $(HOME)/.hermes/plugins/fmsg

.PHONY: install uninstall test

install:
	mkdir -p $(PLUGIN_DIR)
	cp plugin/__init__.py plugin/plugin.yaml plugin/adapter.py plugin/fmsg_client.py $(PLUGIN_DIR)/
	@echo "Installed to $(PLUGIN_DIR). Set FMSG_API_URL and FMSG_API_KEY in ~/.hermes/.env, then restart the gateway."

uninstall:
	rm -rf $(PLUGIN_DIR)

test:
	python -m pytest tests/ -v
