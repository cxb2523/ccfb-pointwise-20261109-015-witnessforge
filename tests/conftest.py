from pathlib import Path

import pytest

from witnessforge import load_copybook, load_mapping

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "claims"


@pytest.fixture
def example_paths():
    return EXAMPLE / "claim.cpy", EXAMPLE / "mapping.yaml"


@pytest.fixture
def layout(example_paths):
    return load_copybook(example_paths[0])


@pytest.fixture
def mapping(example_paths, layout):
    return load_mapping(example_paths[1], layout)
