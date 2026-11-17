"""Dataset and report source checks for external account delegation."""
from .errors import PlatformError

REFERENCE_FIELDS = frozenset({"dataset_id", "version", "content_hash"})


def local_only():
    return PlatformError("MCP_LOCAL_DATA_ONLY", 403)


def shareable_dataset(record):
    source = record.get("source")
    storage = record.get("row_storage")
    return (record.get("scope") in {"synthetic", "public"}
            and type(source) is dict and type(source.get("provider")) is str
            and source["provider"].casefold() != "wrds"
            and not (type(storage) is dict and "wrds_capture" in storage))


def require_dataset(record):
    if not shareable_dataset(record):
        raise local_only()


class ReportDataPolicy:
    """Resolve each distinct saved reference against the owner's immutable dataset."""
    def __init__(self, library, owner):
        self.library = library
        self.owner = owner
        self.checked = set()

    def require_reference(self, reference):
        try:
            if type(reference) is not dict or not REFERENCE_FIELDS <= reference.keys():
                raise ValueError
            ident, version, content_hash = (reference[key] for key in ("dataset_id", "version", "content_hash"))
            if type(ident) is not str or type(version) not in (int, str) or type(content_hash) is not str:
                raise ValueError
            key = ident, str(version), content_hash
            if key in self.checked:
                return
            if self.library is None:
                raise ValueError
            record = self.library.get(self.owner, ident)
            if str(record["version"]) != key[1] or record["content_hash"] != content_hash:
                raise ValueError
            require_dataset(record)
        except (KeyError, TypeError, ValueError):
            raise local_only() from None
        self.checked.add(key)

    def require_document(self, document):
        pending = [document]
        while pending:
            value = pending.pop()
            if type(value) is list:
                pending.extend(value)
            elif type(value) is dict:
                if (value.get("scope") == "licensed_local" or value.get("provider") == "wrds"
                        or "wrds_capture" in value):
                    raise local_only()
                if REFERENCE_FIELDS <= value.keys():
                    self.require_reference(value)
                for name in ("dataset_reference", "benchmark_reference"):
                    if name in value and value[name] is not None:
                        self.require_reference(value[name])
                pending.extend(value.values())
