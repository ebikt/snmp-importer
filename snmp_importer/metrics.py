from .config import YamlValue, ConfigError
from .walker import SnmpValue, Integer, OctetString
from .inputs import Inputs, ValueExpressionRet, InputProxyL2, InputTransformation
import re, math
from typing import cast, Iterator, Mapping, Self, Callable
from types import CodeType
import itertools

class InputValueGroup: # FIXME
    def __getitem__(self, key:str) -> ValueExpressionRet|None:
        raise NotImplementedError()

    def get(self, key:str, default:str) -> ValueExpressionRet|None:
        raise NotImplementedError()

class ValueExpression: # {{{
    metric: tuple[str,str]|None
    fileInfo: str
    expression: str
    code: CodeType

    def __init__(self, expr:YamlValue) -> None:
        self.fileInfo = expr.fileInfo
        self.expression = expr.asStr()
        if re.match(r'^\s*[a-zA-Z0-9][-_a-zA-Z0-9]*\s*\.\s*[a-zA-Z0-9][-_a-zA-Z0-9]*\s*$', self.expression):
            key = self.expression.split('.')
            self.metric = (key[0].strip(), key[1].strip())
        else:
            self.metric = None
            self.code = compile(self.expression, self.fileInfo, 'eval')

    def getValue(self, locals:InputProxyL2, numeric:bool=False) -> ValueExpressionRet|None:
        try:
            if self.metric is None:
                ret = cast(ValueExpressionRet|None, eval(self.code, cast(dict[str,None],{'re':re, 'math':math}), cast(Mapping[str,object],locals)))
            elif self.metric[0] == 'transform':
                ret = getattr(locals[self.metric[0]],self.metric[1])
            else:
                ret = locals[self.metric[0]][self.metric[1]]
        except Exception as e:
            row = str(locals['walker'].get('iid',''))
            if row:
                row = f" for {row}"
            raise ValueError(f"Failed to evaluate {self.expression} from {self.fileInfo}{row}: {type(e).__name__}: {e}") 
        if ret is None: return None
        if isinstance(ret, (int, float)):
            return ret
        if isinstance(ret, (bytes, str)) and not numeric:
            return ret
        if isinstance(ret, Integer): # type: ignore[misc] # wtf mypy?
            return int(ret)
        if isinstance(ret, OctetString) and not numeric: # type: ignore[misc] # wtf mypy?
            return bytes(ret)
        raise ValueError(f"Failed to evaluate {self.expression}: expression returned {ret!r}")
# }}}

class TransformationCode(InputTransformation): # {{{
    fileInfo: str
    def __init__(self, expr: YamlValue) -> None:
        self.fileInfo = expr.fileInfo
        ret_globals:dict[str,object] = {}
        expr_str = expr.asStr()
        exec(compile(expr_str,expr.fileInfo,'exec'), ret_globals)
        ret = ret_globals.get('Transform', None)
        assert isinstance(ret, type), f"Code at {self.fileInfo} must define class Transform, that takes single argument, that is mapping of input groups for given table row." # type: ignore[misc]
        self.evaluate = cast(Callable[[InputProxyL2], dict[str,ValueExpressionRet]], ret)
# }}}

class Metric: # {{{
    def __init__(self, labels:dict[str,str], expr:YamlValue, influx:str, suffix:str, type:str, help:str, tags:dict[str,YamlValue]) -> None:
        self.labels = labels.copy()
        self.expr   = ValueExpression(expr)
        self.influx = influx
        self.suffix = suffix
        self.type   = type
        self.help   = help
        self.tags   = { tname:ValueExpression(expr) for tname, expr in tags.items() }
# }}}

def iterate_alternatives(choices:dict[str, tuple[str, ...]], order:list[str]) -> Iterator[dict[str, str]]: # {{{
    if len(order) == 0:
        yield {}
        return
    key = order[-1]
    for recursion in iterate_alternatives(choices, order[:-1]):
        for value in choices[key]:
            ret = recursion.copy()
            ret[key] = value
            yield ret
# }}}

class MetricVariants: # {{{
    variants:list[Metric]

    _varsub = re.compile(r'\$\{\s*([A-Za-z0-9][-_A-Za-z0-9]*)\s*\}')
    def _label_expand(self, labels:dict[str,str], scfg:YamlValue, default:str|None = None, nonempty:bool=False) -> str:
        if scfg.isMissing():
            if default is None or nonempty:
                scfg.raiseExc(f"Value must be nonempty string")
            else:
                return default
        s = scfg.asStr()
        if nonempty and s.strip() == '':
            scfg.raiseExc(f"Value must be nonempty string")
        def sub(m:re.Match[str]) -> str:
            return labels[cast(str,m.group(1))] # FIXME wrong `re` typing (Any)
        return self._varsub.sub(sub, s)

    def __init__(self, metricfg:YamlValue) -> None:
        with metricfg.asStruct() as s:
            label_variants:dict[str,tuple[str, ...]] = {}
            for name, itemcfg in s['loop'].iterStrMap(allow_missing=True, keyconstraint='identifier'):
                label_variants[name] = tuple( x.asStr(constraint='identifier') for x in itemcfg.iterList())
            self.variants = []
            for labels in iterate_alternatives(label_variants, sorted(label_variants.keys())):
                expr = YamlValue(self._label_expand(labels, s['expr'], nonempty=True), s['expr'].fileInfo)
                suffix = self._label_expand(labels, s['suffix'], default='')
                influx = self._label_expand(labels, s['influx'], default=suffix)
                mtype  = self._label_expand(labels, s['type'],   default='unknown')
                mhelp  = self._label_expand(labels, s['help'],   default='')
                tags = {}
                for tname, tcfg in s['tags'].iterStrMap(allow_missing=True, keyconstraint='identifier'):
                    tags[tname] = YamlValue(self._label_expand(labels, tcfg), tcfg.fileInfo)
                self.variants.append(Metric(labels, expr, influx, suffix, mtype, mhelp, tags))
# }}}

class Table: # {{{
    metrics:list[MetricVariants]
    labels:dict[str, ValueExpression]
    dimensions:dict[str, ValueExpression]
    scrape:dict[tuple[str,str], str]
    max_rows:int|None
    scalar:bool
    transformation:TransformationCode|None = None

    def __init__(self, name:str, cfg:YamlValue, inputs:Inputs) -> None: # {{{
        self.name = name
        self.scrape     = {}
        def addscrape(expr:ValueExpression) -> None:
            sc2 = expr.metric
            if sc2 is not None and sc2 not in self.scrape and sc2[0] != 'transform':
                self.scrape[sc2] = expr.fileInfo
        with cfg.asStruct() as s:
            self.metrics    = [ MetricVariants(itemcfg)  for itemcfg in s['metrics'].iterList(allow_missing = True) ]
            self.labels     = { name:ValueExpression(itemcfg) for name, itemcfg in s['labels'].iterStrMap(allow_missing=True, keyconstraint="identifier") }
            self.dimensions = { name:ValueExpression(itemcfg) for name, itemcfg in s['dimensions'].iterStrMap(allow_missing=True, keyconstraint="identifier") }
            if not s['transform'].isMissing():
                self.transformation = TransformationCode(s['transform'])
            for itemcfg in s['scrape'].iterList(allow_missing=True):
                scs = itemcfg.asStr()
                sca =scs.split('.')
                if len(sca) != 2:
                    itemcfg.raiseExc("Expected input name (two identifiers separated by dot)")
                first = YamlValue(sca[0].strip(),itemcfg.fileInfo + ' first part').asStr(constraint='identifier')
                second = YamlValue(sca[1].strip(),itemcfg.fileInfo + ' second part').asStr(constraint='identifier')
                self.scrape[first, second] = itemcfg.fileInfo
            for mv in self.metrics:
                for m in mv.variants:
                    addscrape(m.expr)
                    for te in m.tags.values():
                        addscrape(te)
            for ladis in self.labels, self.dimensions:
                for ve in ladis.values():
                    addscrape(ve)
            for sc2, fi in self.scrape.items():
                if sc2[0] != 'transform' and not inputs.has(*sc2):
                    YamlValue('.'.join(sc2), fi).raiseExc(f"Unknown input {sc2[0]}.{sc2[1]}.")
            if s['max_rows'].isMissing():
                self.max_rows = None
            else:
                self.max_rows = s['max_rows'].asInt(min=0)
            if s['scalar'].asBool(default=False):
                if self.max_rows not in (None, 1):
                    s['max_rows'].raiseExc(f"scalar implies max_rows=1, got {self.max_rows}")
                self.scalar = True
                self.max_rows = 1
            else:
                self.scalar = False
    # }}}

    def merge(self, other:'Table') -> Self: # {{{
        self.metrics = other.metrics + self.metrics
        for lname, dimension in other.dimensions.items():
            assert lname not in self.dimensions, "Duplicate dimension {lname}, defined in  {dimension.fileInfo} and {self.dimensions[dimension].fileInfo}"
            self.dimensions[lname] = dimension
        for lname, label in other.labels.items():
            assert lname not in self.labels, "Duplicate label {lname}, defined in  {label.fileInfo} and {self.labels[label].fileInfo}"
            self.labels[lname] = label
        for scrape, scrapeInfo in other.scrape.items():
            self.scrape.setdefault(scrape, scrapeInfo)
        if self.max_rows is None: self.max_rows = other.max_rows
        if self.transformation is None: self.transformation = other.transformation
        if other.scalar:
            self.scalar = True
            assert self.max_rows == 1, "Merging table with scalar==True, but max_rows == {self.max_rows} (must be 1)"
        return self
    # }}}
# }}}

class MetricMetaData: # {{{
    def __init__(self, type:str, description:str):
        self.type = type
        self.description = description

    def __eq__(self, other:object) -> bool:
        if type(self) != type(other): return False
        assert isinstance(other, MetricMetaData)
        if self.type != other.type: return False
        if self.description != other.description: return False
        return True
# }}}

EMPTYMETA=MetricMetaData('UNSPECIFIED','Common labels.')

class Device: # {{{
    tables: dict[str, Table]
    merge_tables: dict[str, Table]
    includes: set[str]
    metadata: dict[tuple[str,str], MetricMetaData]
    transformations: dict[str, InputTransformation]

    def __init__(self, name:str, cfg:YamlValue, inputs:Inputs) -> None: # {{{
        self.name = name
        self.metadata = {}
        with cfg.asStruct() as s:
            self.tables = { name:Table(name, itemcfg, inputs) for name, itemcfg in s['tables'].iterStrMap(allow_missing=True, keyconstraint="identifier") }
            self.merge_tables = { name:Table(name, itemcfg, inputs) for name, itemcfg in s['merge_tables'].iterStrMap(allow_missing=True, keyconstraint="identifier") }
            self.includes = set()
            for itemcfg in s['include'].iterList(allow_missing=True):
                self.includes.add(itemcfg.asStr())
            for tname, table in itertools.chain(self.tables.items(),self.merge_tables.items()):
                for mv in table.metrics:
                    for m in mv.variants:
                        key = (tname, m.suffix)
                        val = MetricMetaData(m.type, m.help)
                        if key in self.metadata:
                            assert val == self.metadata[key], f"Metadata differs for {key}: {val} vs {self.metadata[key]}"
                        else:
                            self.metadata[key] = val
                if (tname,'') not in self.metadata:
                    self.metadata[tname,''] = EMPTYMETA
    # }}}
# }}}

class Devices: # {{{
    devices: dict[str, Device]
    def __init__(self, toplevelcfg:YamlValue, inputs: Inputs) -> None:
        self.devices = { name: Device(name, itemcfg, inputs) for name, itemcfg in toplevelcfg.iterStrMap(nonempty=True, keyconstraint="identifier") }
        self.expand_includes_and_merges()

    def expand_includes_and_merges(self) -> None:
        todo = set(self.devices.keys())
        while len(todo):
            oldlen = len(todo)
            for name in list(todo):
                device = self.devices[name]
                for dep in list(device.includes):
                    if dep not in self.devices:
                        raise ConfigError(f"Device {name} depends on nonexistent device {dep}.")
                    if len(self.devices[dep].includes):
                        break
                    # dep has no further dependencies, lets copy its tables
                    for tname, table in self.devices[dep].tables.items():
                        if tname in device.tables:
                            if table != device.tables[tname]:
                                raise ConfigError(f"Device {name} redefines table {tname} defined in {dep}.")
                        else:
                            device.tables[tname] = table
                    for tname, table in device.merge_tables.items():
                        device.tables[tname] = table.merge(device.tables[tname])
                    device.merge_tables = {} # all tables were merged, we do not need them anymore
                    for mname, meta in self.devices[dep].metadata.items():
                        if mname in device.metadata and device.metadata[mname] != EMPTYMETA:
                            if device.metadata[mname] != meta:
                                raise ConfigError(f"Device {name} redefines metadata {mname} defined in {dep}.")
                        else:
                            device.metadata[mname] = meta
                    # we copied tables and metadata, so remove name from list of dependencies
                    device.includes.remove(dep)
                else:
                    assert len(device.includes) == 0
                    device.transformations = { tname:table.transformation for tname, table in device.tables.items() if table.transformation is not None }
                    todo.remove(name)
            if len(todo) == oldlen:
                raise ConfigError(f"Devices {sorted(todo)} cannot be expanded due to cyclic include.")

    def __getitem__(self, name:str) -> Device:
        return self.devices[name]
# }}}
