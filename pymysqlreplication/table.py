# -*- coding: utf-8 -*-
from typing import List
from typing import Dict
from typing import Optional
from pymysqlreplication.column import Column


class Table(object):
    def __init__(self, column_schemas: List[Dict[str, str]], table_id: int, schema: str, table: str,
                 columns: List[Column], primary_key: Optional[List[str]] = None):
        if primary_key is None:
            primary_key = [c.data["name"] for c in columns if c.data["is_primary"]]
            if len(primary_key) == 0:
                primary_key = ''
            elif len(primary_key) == 1:
                primary_key, = primary_key
            else:
                primary_key = tuple(primary_key)

        self.__dict__.update({
            "column_schemas": column_schemas,
            "table_id": table_id,
            "schema": schema,
            "table": table,
            "columns": columns,
            "primary_key": primary_key
        })

    @property
    def data(self):
        return dict((k, v) for (k, v) in self.__dict__.items() if not k.startswith('_'))

    def __eq__(self, other: 'Table') -> bool:
        return self.data == other.data

    def __ne__(self, other: 'Table') -> bool:
        return not self.__eq__(other)

    def serializable_data(self) -> 'Table':
        return self.data
