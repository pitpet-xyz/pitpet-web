all: format

format:
	find ./sic -name "*.py" -not -path './sic/migrations/*' | xargs black
