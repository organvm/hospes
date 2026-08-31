.PHONY: demo test validate done dashboard stress

demo:
	PYTHONPATH=. python3 -m hospes demo

test:
	python3 -m pytest tests -q

validate:
	PYTHONPATH=. python3 -m hospes validate

done:
	./done.sh

dashboard:
	@echo "Opening dashboard — serve with:"
	@echo "  cd dashboard && python3 -m http.server 8080"
	@echo "Then open http://localhost:8080"
	@cd dashboard && python3 -m http.server 8080

stress:
	pip install -e '.[test,api]'
	python3 -m pytest tests -v
	PYTHONPATH=. python3 -m hospes demo
	PYTHONPATH=. python3 -m hospes demo
	PYTHONPATH=. python3 -m hospes validate
	./done.sh
