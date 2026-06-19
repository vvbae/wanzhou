"""数据源 adapter 包。每个源一个文件，统一输出内部 dict。"""

from cnbib.sources.base import SOURCE_FIELDS, Source, empty_record
from cnbib.sources.google_books import GoogleBooksSource
from cnbib.sources.openlibrary import OpenLibrarySource

__all__ = [
    "SOURCE_FIELDS",
    "Source",
    "empty_record",
    "GoogleBooksSource",
    "OpenLibrarySource",
]
