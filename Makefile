.PHONY: install data test lint run-api run-ui eval

install:
	pip install -e ".[dev]"

data:
	python -m data.generate --out data/payments.duckdb --seed 42

test:
	pytest -q

lint:
	ruff check src tests data eval

run-api:
	uvicorn analyst.api.main:app --reload --port 8000

run-ui:
	streamlit run ui/app.py

eval:
	python -m eval.run_eval
