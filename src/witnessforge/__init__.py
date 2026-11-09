"""WitnessForge: mutation-guided minimal witness data for ETL mapping validation."""

__version__ = "0.1.0"

from .codec import decode_record, encode_record  # noqa: E402
from .copybook import Field, Layout, load_copybook, parse_copybook  # noqa: E402
from .mapping import Mapping, Rule, apply_mapping, load_mapping, mapping_from_dict  # noqa: E402
from .mutants import Mutant, generate_mutants  # noqa: E402
from .verify import verify  # noqa: E402
from .witness import ForgeResult, forge  # noqa: E402

__all__ = [
    "__version__",
    "Field",
    "Layout",
    "Mapping",
    "Rule",
    "Mutant",
    "ForgeResult",
    "parse_copybook",
    "load_copybook",
    "load_mapping",
    "mapping_from_dict",
    "apply_mapping",
    "encode_record",
    "decode_record",
    "generate_mutants",
    "forge",
    "verify",
]
