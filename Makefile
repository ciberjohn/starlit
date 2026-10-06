.PHONY: venv sample test run docker

venv:
	python3 -m venv venv && ./venv/bin/pip install -r requirements.txt pytest

sample:
	./venv/bin/python engine/crochet_chart.py && mv rows.json sample/rows.json
	./venv/bin/python sample/make_sample_pdf.py

test:
	./venv/bin/python -m pytest -q
	./venv/bin/python tests/paginate_pdf_test.py

run:
	./venv/bin/python app/app.py

docker:
	docker build -t starlit .
	docker run --rm -p 6090:6090 -v starlit-data:/data starlit
