from typing import List, Any, Dict, Optional, Type

import json
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass, field, fields, asdict

import numpy
import pandas


logger = logging.getLogger(__name__)


class DocumentEncoder(json.JSONEncoder):
    """
    Encodes more exotic datatypes like pandas dataframes or file paths.
    """

    def default(self, obj):
        if isinstance(obj, numpy.ndarray):
            return obj.tolist()
        if isinstance(obj, pandas.DataFrame):
            return obj.to_json()
        if isinstance(obj, Path):
            return str(obj.absolute())
        try:
            return json.JSONEncoder.default(self, obj)
        except TypeError:
            return str(obj)


class DocumentDecoder(json.JSONDecoder):
    """
    Decodes more exotic datatypes like pandas dataframes or file paths.
    """

    def __init__(self, *_, object_hook=None, **__):
        super().__init__(object_hook=object_hook or self.document_decoder)

    def document_decoder(self, dictionary):
        if "array" in dictionary and dictionary.get("array"):
            dictionary["array"] = numpy.array(dictionary.get("array"))
        if "dataframe" in dictionary and dictionary.get("dataframe"):
            dictionary["dataframe"] = pandas.read_json(dictionary.get("dataframe", None))
        if "embedding" in dictionary and dictionary.get("embedding"):
            dictionary["embedding"] = numpy.array(dictionary.get("embedding"))

        return dictionary


@dataclass(frozen=True)
class Document:
    """
    Base data class containing some data to be queried.
    Can contain text snippets, tables, and file paths to images or audios.
    Documents can be sorted by score, saved to/from dictionary and JSON, and are immutable.

    Immutability is due to the fact that the document's ID depends on its content, so upon changing the content, also
    the ID should change. To avoid keeping IDs in sync with the content by using properties, and asking docstores to
    be aware of this corner case, we decide to make Documents immutable and remove the issue. If you need to modify a
    Document, consider using `to_dict()`, modifying the dict, and then create a new Document object using
    `Document.from_dict()`.

    :param id: Unique identifier for the document. Do not provide this value when initializing a document: it will be
        generated based on the document's attributes (see id_hash_keys).
    :param text: Text of the document, if the document contains text.
    :param array: Array of numbers associated with the document, if the document contains matrix data like image,
        audio, video, and such.
    :param dataframe: Pandas dataframe with the document's content, if the document contains tabular data.
    :param blob: Binary data associated with the document, if the document has any binary data associated with it.
    :param mime_type: MIME type of the document. Defaults to "text/plain".
    :param metadata: Additional custom metadata for the document.
    :param id_hash_keys: List of keys to use for the ID hash. Defaults to the four content fields of the document:
        text, array, dataframe and blob. This field can include other document fields (like mime_type) and metadata's
        top-level keys. Note that the order of the keys is important: the ID hash will be generated by concatenating
        the values of the keys in the order they appear in this list. Changing the order impacts the ID hash.
    :param score: Score of the document. Used for ranking, usually assigned by retrievers.
    :param embedding: Vector representation of the document.
    """

    id: str = field(default_factory=str)
    text: Optional[str] = field(default=None)
    array: Optional[numpy.ndarray] = field(default=None)
    dataframe: Optional[pandas.DataFrame] = field(default=None)
    blob: Optional[bytes] = field(default=None)
    mime_type: str = field(default="text/plain")
    metadata: Dict[str, Any] = field(default_factory=dict, hash=False)
    id_hash_keys: List[str] = field(default_factory=lambda: ["text", "array", "dataframe", "blob"], hash=False)
    score: Optional[float] = field(default=None, compare=False)
    embedding: Optional[numpy.ndarray] = field(default=None, repr=False)

    def __str__(self):
        text = self.text if len(self.text) < 100 else self.text[:100] + "..."
        array = self.array.shape if self.array is not None else "None"
        dataframe = self.dataframe.shape if self.dataframe is not None else "None"
        blob = f"{len(self.blob)} bytes" if self.blob is not None else "None"
        return f"{self.__class__.__name__}(mimetype: {self.mime_type}, text: '{text}', array: {array}, dataframe: {dataframe}, binary: {blob})"

    def __eq__(self, other):
        """
        Compares documents for equality. Uses the id to check whether the documents are supposed to be the same.
        """
        if type(self) == type(other):
            return self.id == other.id
        return False

    def __post_init__(self):
        """
        Generate the ID based on the init parameters.
        """
        # Validate metadata
        for key in self.metadata:
            if key in [field.name for field in fields(self)]:
                raise ValueError(f"Cannot name metadata fields as top-level document fields, like '{key}'.")

        # Note: we need to set the id this way because the dataclass is frozen. See the docstring.
        hashed_content = self._create_id()
        object.__setattr__(self, "id", hashed_content)

    def _create_id(self):
        """
        Creates a hash of the given content that acts as the document's ID.
        """
        document_data = self.flatten()
        contents = [self.__class__.__name__]
        missing_id_hash_keys = []
        if self.id_hash_keys:
            for key in self.id_hash_keys:
                if key not in document_data:
                    missing_id_hash_keys.append(key)
                else:
                    contents.append(str(document_data.get(key)))
        content_to_hash = ":".join(contents)
        doc_id = hashlib.sha256(str(content_to_hash).encode("utf-8")).hexdigest()
        if missing_id_hash_keys:
            logger.warning(
                "Document %s is missing the following id_hash_keys: %s. Using a hash of the remaining content as ID.",
                doc_id,
                missing_id_hash_keys,
            )
        return doc_id

    def to_dict(self):
        """
        Saves the Document into a dictionary.
        """
        return asdict(self)

    def to_json(self, json_encoder: Optional[Type[DocumentEncoder]] = None, **json_kwargs):
        """
        Saves the Document into a JSON string that can be later loaded back. Drops all binary data from the blob field.
        """
        dictionary = self.to_dict()
        del dictionary["blob"]
        return json.dumps(dictionary, cls=json_encoder or DocumentEncoder, **json_kwargs)

    @classmethod
    def from_dict(cls, dictionary):
        """
        Creates a new Document object from a dictionary of its fields.
        """
        return cls(**dictionary)

    @classmethod
    def from_json(cls, data, json_decoder: Optional[Type[DocumentDecoder]] = None, **json_kwargs):
        """
        Creates a new Document object from a JSON string.
        """
        dictionary = json.loads(data, cls=json_decoder or DocumentDecoder, **json_kwargs)
        return cls.from_dict(dictionary=dictionary)

    def flatten(self) -> Dict[str, Any]:
        """
        Returns a dictionary with all the fields of the document and the metadata on the same level.
        This allows filtering by all document fields, not only the metadata.
        """
        dictionary = self.to_dict()
        metadata = dictionary.pop("metadata", {})
        dictionary = {**dictionary, **metadata}
        return dictionary
