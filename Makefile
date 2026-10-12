PY ?= python
JAVA ?= tools/jdk-17.0.20.1+1-jre/bin/java
LLM_URL ?= http://127.0.0.1:11434/v1
LLM_MODEL ?= llama3.2
PG_URL ?= postgresql+psycopg://clinagent:clinagent@127.0.0.1:5433/clinagent

.PHONY: install lint format test run data synthea store cases eval eval-det eval-llm report up down pg-load hapi-load

install:
	$(PY) -m pip install -e ".[dev,postgres,otlp]"

lint:
	ruff check . && ruff format --check .

format:
	ruff check --fix . && ruff format .

test:
	$(PY) -m pytest

run:
	uvicorn clinagent.api:app --port 8020 --reload

# --- data: Synthea (needs Java 17+) -> compact store
synthea:
	$(JAVA) -Xmx8g -Dfile.encoding=UTF-8 -jar tools/synthea-with-dependencies.jar -s 42 -cs 42 -p 5000 -r 20260101 \
	  --exporter.baseDirectory=./data/synthea/ --exporter.years_of_history=5 --exporter.fhir.bulk_data=true \
	  --exporter.fhir.transaction_bundle=false \
	  --exporter.fhir.included_resources=Patient,MedicationRequest,Medication,AllergyIntolerance,Condition,Observation \
	  --exporter.hospital.fhir.export=false --exporter.practitioner.fhir.export=false \
	  --generate.only_alive_patients=true Delaware

store:
	$(PY) scripts/build_store.py --src data/synthea/fhir --db sqlite:///data/clinical.db

data: synthea store

# --- evaluation
cases:
	$(PY) -m clinagent.eval.cases --split dev --n 150 --out eval/cases_dev.jsonl
	$(PY) -m clinagent.eval.cases --split test --n 600 --out eval/cases_test.jsonl

eval-det:
	$(PY) -m clinagent.eval.run --cases eval/cases_test.jsonl --name test_det

eval-llm:
	$(PY) -m clinagent.eval.run --cases eval/cases_test.jsonl --name test_llm --llm-base-url $(LLM_URL) --llm-model $(LLM_MODEL)

report:
	$(PY) -m clinagent.eval.report

eval: cases eval-det eval-llm report

# --- containers
up:
	docker compose up -d --build

down:
	docker compose down

pg-load:
	docker compose --profile postgres up -d postgres
	$(PY) scripts/migrate_store.py --dst $(PG_URL)

hapi-load:
	docker compose --profile fhir up -d hapi
	$(PY) scripts/load_hapi.py --fhir http://127.0.0.1:8080/fhir --n 200
