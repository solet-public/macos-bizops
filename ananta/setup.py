from setuptools import find_packages, setup

setup(
    name="ananta",
    version="2.0.0",  # Must be > 1.3.9 (external PyPI 'ananta' package) to avoid conflicts
    description="A flexible framework for AI-driven applications",
    license="Apache-2.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    include_package_data=True,
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    python_requires="==3.13.*",
    install_requires=[
        "requests>=2.32.0",
        "python-dotenv>=1.0.0",
        "click>=8.0.0",
    ],
    entry_points={
        "console_scripts": [
            "ananta=ananta.cli:sync_main",
        ],
    },
)
