.PHONY: install frontend-install frontend-build test lint typecheck demo dev verify

install:
	.venv/bin/pip install -e ".[dev]"
	$(MAKE) frontend-install

frontend-install:
	npm --prefix frontend ci

frontend-build:
	npm --prefix frontend run build

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check app tests

typecheck:
	.venv/bin/mypy app

demo:
	.venv/bin/python -m app.cli "OpenAI Agents SDK 和 LangChain 的职责是什么？"

dev: frontend-build
	.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

verify: lint typecheck test frontend-build
