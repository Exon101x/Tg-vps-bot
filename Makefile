.PHONY: setup run test lint docker up

setup:
	python deploy.py

run:
	python bot.py

test:
	pytest -q

lint:
	ruff check .

docker:
	docker build -t vpsbot .

up:
	docker compose up -d --build
