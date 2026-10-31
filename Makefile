.PHONY: install dev dev-api dev-web lint typecheck test synth check deploy

install:
	uv sync
	npm --prefix web ci --no-audit --no-fund
	npm --prefix infra ci --no-audit --no-fund

# Runs the API and the UI together with the fake LLM unless .env says otherwise.
dev: install
	$(MAKE) -j2 dev-api dev-web

dev-api:
	uv run uvicorn ticketgate.api:create_app --factory --host 127.0.0.1 --port 8000

dev-web:
	npm --prefix web run dev

lint:
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix web run lint
	npm --prefix infra run lint

typecheck:
	uv run mypy
	npm --prefix web run typecheck
	npm --prefix infra run typecheck

test:
	uv run pytest
	npm --prefix web test
	npm --prefix infra test

synth:
	npm --prefix infra run synth

check: lint typecheck test synth

# Creates real AWS resources that cost money. See "Deploy" in the README first.
# Example: make deploy CONTEXT="-c githubRepo=owner/repo -c allowedCidr=203.0.113.0/24"
deploy:
	cd infra && npx cdk deploy $(CONTEXT)
