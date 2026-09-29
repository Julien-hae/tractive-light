all: setup_environment

clean:
	poetry run pre-commit uninstall
	rm -rf .venv

setup_environment: check
		pyenv install 3.13 --skip-existing \
		&& pyenv local 3.13 \
		&& poetry env use 3.13 \
		&& poetry install \
		&& poetry run pre-commit install

check: pyenv_exists poetry_exists is_git

pyenv_exists: ; @which pyenv > /dev/null

poetry_exists: ; @which poetry > /dev/null

is_git: ; @git rev-parse --git-dir > /dev/null
