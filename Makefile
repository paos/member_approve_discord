.PHONY: install test run status sync bootstrap
install:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements-lock.txt
test:
	.venv/bin/python -m pytest -q
	.venv/bin/ruff check mensa tests
run:
	.venv/bin/python -m uvicorn mensa.app:create_app --factory --host 127.0.0.1 --port 8766 --no-access-log
status:
	.venv/bin/python -m mensa.cli status
sync:
	.venv/bin/python -m mensa.cli sync
bootstrap:
	.venv/bin/python -m mensa.cli bootstrap
