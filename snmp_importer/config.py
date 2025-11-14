import json, re, yaml

from typing import NoReturn, cast, Iterator, Literal, Type

class ConfigError(Exception): pass


class YamlFileInfoValue: # {{{
    fileInfo: str
    def raiseExc(self, msg:str) -> NoReturn: # {{{
        raise ConfigError(f"Configuration error at {self.fileInfo}: {msg}")
    # }}}
# }}}

class MissingValue(YamlFileInfoValue): # {{{
    def __init__(self, fileInfo:str) -> None: self.fileInfo = fileInfo
# }}}

StrConstraintType = Literal['none', 'identifier']
class StrConstraint: # {{{
    rx:re.Pattern[str]
    error:str
    patterns:dict[str, re.Pattern[str]] = {
        'none': re.compile(r'^.*$',re.MULTILINE),
        'identifier': re.compile('^[a-zA-Z][-_a-zA-Z0-9]*$'),
    }
    errors = {
        'none': '???',
        'identifier': 'must be identifier ([a-zA-Z][-_a-zA-Z0-9]*)',
    }

    def __init__(self, constraint:StrConstraintType) -> None:
        self.rx = self.patterns[constraint]
        self.error = self.errors[constraint]

    def __call__(self, subject:str) -> str:
        if self.rx.match(subject): return ''
        return self.error
# }}}

class YamlValue(YamlFileInfoValue): # {{{
    data: object

    def __init__(self, data:object, fileInfo:str) -> None: # {{{
        self.data = data
        self.fileInfo = fileInfo
    # }}}

    def asStr(self, strict:bool=False, default:'str|None' = None, constraint:StrConstraintType = 'none') -> str: # {{{
        data = self.data
        if data is None or self.isMissing():
            if default is None:
                self.raiseExc("Missing mandatory string value.")
            else:
                return default
        if isinstance(data, str):
            s = data
        elif not strict and isinstance(data, (bool, int, float)):
            s = json.dumps(data)
        else:
            self.raiseExc("Expected string type.")
        err =  StrConstraint(constraint)(s)
        if err:
            self.raiseExc(f"String value {err}")
        return s
    # }}}

    def asInt(self, strict:bool=False, default:'int|None' = None, min:'int|None' = None, max:'int|None' = None) -> int: # {{{
        data = self.data
        if data is None or self.isMissing():
            if default is None:
                self.raiseExc("Missing mandatory integer value.")
            else:
                return default
        if isinstance(data, int):
            i = data
        elif not strict and isinstance(data, (str, bool)):
            i = int(data)
        elif not strict and isinstance(data, float):
            i = int(round(data))
            assert abs(data-i) < 0.0001
        else:
            self.raiseExc("Expected integer type.")
        if min is not None and i < min:
            self.raiseExc(f"Value must ≥ {min}")
        if max is not None and i > max:
            self.raiseExc(f"Value must ≤ {max}")
        return i
    # }}}

    def asFloat(self, strict:bool=False, default:'float|None' = None, min:'float|None' = None, max:'float|None' = None) -> float: # {{{
        data = self.data
        if data is None or self.isMissing():
            if default is None:
                self.raiseExc("Missing mandatory integer value.")
            else:
                return default
        if isinstance(data, float):
            i = data
        elif isinstance(data, int):
            i = float(data)
        elif not strict and isinstance(data, (str, bool)):
            i = float(data)
        else:
            self.raiseExc("Expected float type.")
        if min is not None and i < min:
            self.raiseExc(f"Value must ≥ {min}")
        if max is not None and i > max:
            self.raiseExc(f"Value must ≤ {max}")
        return i
    # }}}

    def asBool(self, strict:bool=False, default:'bool|None' = None) -> bool: # {{{
        data = self.data
        if data is None or data == '' or self.isMissing():
            if default is None:
                self.raiseExc("Missing mandatory boolean value.")
            else:
                return default
        if data is True or data is False:
            i = data
        elif not strict and isinstance(data, (int, float)):
            if abs(data-0) < 0.0001:
                i = False
            elif abs(data-1) < 1.0001:
                i = True
            else:
                self.raiseExc(f"Boolean value expected.")
        elif not strict and isinstance(data, str):
            data = data.lower()
            if data in ('0', 'false', 'no', 'off'):
                i = False
            elif data in ('1', 'true', 'yes', 'on'):
                i = True
            else:
                self.raiseExc(f"Boolean value expected.")
        else:
            self.raiseExc("Expected boolean type.")
        return i
    # }}}

    def isMissing(self) -> bool: return isinstance(self.data, MissingValue)

    def asStruct(self) -> 'YamlStructWrapper': # {{{
        data = cast(dict[str, object], self.data)
        if not isinstance(data, dict):
            self.raiseExc(f"Expected structure (fixed mapping).")
        for key in data.keys():
            if not isinstance(key, str):
                self.raiseExc(f"Item name {key!r} is not string.")
        return YamlStructWrapper(data, self.fileInfo)
    # }}}

    def iterStrMap(self, allow_missing:bool = False, nonempty:bool = False, keyconstraint:StrConstraintType = "none") -> Iterator[tuple[str, 'YamlValue']]: # {{{
        if self.isMissing() or self.data is None:
            if allow_missing: return
        data = cast(dict[str, object], self.data)
        if not isinstance(data, dict):
            self.raiseExc(f"Expected mapping (dict with arbitrary keys).")
        if len(data) == 0 and nonempty:
            self.raiseExc(f"Mapping must contain at least one key:value pair.")
        constraint = StrConstraint(keyconstraint)
        for k, v in data.items():
            if not isinstance(k, str):
                self.raiseExc(f"Mapping key name {k!r} is not string.")
            err = constraint(k)
            if err != '':
                self.raiseExc(f"Mapping key name {k!r} {err}.")
            yield (k, YamlValue(v, f"{self.fileInfo}[{k!r}]"))
    # }}}

    def iterList(self, allow_missing:bool=False, nonempty:bool=False) -> Iterator['YamlValue']: # {{{
        if self.isMissing():
            if allow_missing: return
        data = cast(list[object], self.data)
        if not isinstance(data, list):
            self.raiseExc(f"Expected sequence (list) of items.")
        if nonempty and len(data) == 0:
            self.raiseExc(f"Expected nonempty sequence (list) of items.")
        for i,v in enumerate(data):
            yield YamlValue(v, f"{self.fileInfo}[{i}]")
    # }}}

    def peekDict(self, key:str) -> 'YamlValue': # {{{
        data = cast(dict[str,object], self.data)
        if not isinstance(data, dict) or key not in data:
            self.raiseExc(f"Expected mapping with key {key}")
        return YamlValue(data[key], f"{self.fileInfo}.{key}")
    # }}}
# }}}

class YamlStruct(YamlFileInfoValue): # {{{
    data:     dict[str, object]
    seen:     set[str]

    def __init__(self, data:dict[str, object], fileInfo:str) -> None: # {{{
        self.data     = data
        self.fileInfo = fileInfo
        self.seen     = set()
    # }}}

    def __getitem__(self, key:str) -> YamlValue:
        if key not in self.data:
            data:object = MissingValue(f"{self.fileInfo}.{key}")
        else:
            data = self.data[key]
            self.seen.add(key)
        return YamlValue(data, f"{self.fileInfo}.{key}")
    # }}}

    def checkUnknown(self) -> None: # {{{
        unseen = set(self.data.keys()) - self.seen
        if len(unseen):
            self.raiseExc(f"Unknown structure item(s): {', '.join(sorted(unseen))}")
    # }}}
# }}}

class YamlStructWrapper: # {{{
    def __init__(self, data:dict[str, object], fileInfo:str) -> None: # {{{
        self.data = data
        self.fileInfo = fileInfo
    # }}}

    def __enter__(self) -> YamlStruct: # {{{
        assert not hasattr(self, 'struct')
        self.struct = YamlStruct(self.data, self.fileInfo)
        return self.struct
    # }}}

    def __exit__(self, t:'Type[BaseException]|None', v:'BaseException|None', tr:object) -> None: # {{{
        if v is None:
            self.struct.checkUnknown()
    # }}}
# }}}


def loadFile(fname:str) -> YamlValue: # {{{
    with open(fname) as f:
        data = cast(object, yaml.safe_load(f))
    return YamlValue(data, f"{fname}:")
# }}}
