from setuptools import setup, find_packages

setup(
    name="foreman",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.9",
    install_requires=[],          # stdlib only; PyYAML optional
    entry_points={"console_scripts": ["foreman=foreman.cli:main"]},
)
