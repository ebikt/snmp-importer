from .config import YamlValue
from .walker import ObjectName, SnmpValue, OctetString, TimeTicks
import pyasn1.type.univ as univ
from typing import Union

import re, ipaddress
from typing import cast, Iterator, Callable

def parse_oid(oidcfg:YamlValue) -> tuple[str, tuple[int,...], YamlValue]: # {{{
    oidstr = oidcfg.asStr()
    components = [x.strip() for x in oidstr.split('.')]
    if not components[0].isnumeric():
        parent = YamlValue(components[0],oidcfg.fileInfo + " first component").asStr(constraint='identifier')
        first = 1
    else:
        parent = ''
        first = 0
    numbers = []
    for i, c in list(enumerate(components))[first:]:
        if not c.isnumeric(): oidcfg.raiseExc(f"{i}-th component is not a number")
        numbers.append(int(c))
    return parent, tuple(numbers), oidcfg
# }}}

class InputDefinition: # {{{
    oid: ObjectName
    filters: tuple[str, ...]

    def __init__(self, cfg:YamlValue, aliases:dict[str, ObjectName]) -> None: # {{{
        s = cfg.asStr()
        pipeline = s.split('|')
        alias, numbers, _ = parse_oid(YamlValue(pipeline[0], cfg.fileInfo))
        if alias not in aliases:
            cfg.raiseExc(f"Unknown alias {alias!r}")
        self.oid = aliases[alias] + numbers
        filters = []
        for f in pipeline[1:]:
            assert f.strip() in ('mac', 'datetime', 'ip'), f"unknown filter {f}" # FIXME
            filters.append(f.strip())
        self.filters = tuple(filters)
    # }}}

# }}}

class Inputs: # {{{
    aliases: dict[str,ObjectName]
    inputs: dict[str,dict[str,InputDefinition]]

    def init_aliases(self, aliascfg:YamlValue) -> None: # {{{
        self.aliases = {'': ObjectName(cast(tuple[int,...],tuple()))}
        alias_definitions:dict[str, tuple[str, tuple[int,...], YamlValue]] = {}

        for alias, oidcfg in aliascfg.iterStrMap(allow_missing=True, keyconstraint='identifier'):
            if alias in alias_definitions:
                oidcfg.raiseExc(f"Multiple definitions of alias {alias}")
            alias_definitions[alias] = parse_oid(oidcfg)

        while len(alias_definitions):
            oldlen = len(alias_definitions)
            for alias in list(alias_definitions.keys()):
                parent, numbers, oidcfg = alias_definitions[alias]
                if parent in self.aliases:
                    self.aliases[alias] = self.aliases[parent] + numbers
                    del alias_definitions[alias]
                elif parent not in alias_definitions:
                    oidcfg.raiseExc(f"Unknown parent alias {parent!r}")
            if oldlen == len(alias_definitions):
                aliascfg.raiseExc(f"Cannot resolve aliases {', '.join(sorted(alias_definitions.keys()))} due to cyclic dependency.")
    # }}} 

    def __init__(self, cfg:YamlValue) -> None: # {{{
        with cfg.asStruct() as s:
            self.init_aliases(s['alias'])
            self.inputs = {}
            for gname, groupcfg in s['input'].iterStrMap(nonempty=True):
                grdef = {}
                for iname, inputcfg in groupcfg.iterStrMap(nonempty = True):
                    grdef[iname] = InputDefinition(inputcfg, self.aliases)
                self.inputs[gname] = grdef
    # }}}

    def has(self, group:str, column:str) -> bool: # {{{
        try:
            self.inputs[group][column]
            return True
        except KeyError:
            return False
    # }}}
    def get_oid(self, group:str, column:str) -> ObjectName:
        return self.inputs[group][column].oid
# }}}

ValueExpressionRet = Union[bool,int,float,bytes,str]

class InputTransformation:
    evaluate: Callable[['InputProxyL2'], dict[str,ValueExpressionRet]]

class InputTreeNode: # {{{
    max_items: 'int|None'
    childs: dict[int,'InputTreeNode']

    def __init__(self) -> None:
        self.max_items = 0
        self.childs = {}

    def add(self, oid:'ObjectName|tuple[int, ...]', max_items:'int|None') -> None:
        if len(oid) == 0:
            if self.max_items is None:
                return
            if max_items is None:
                self.max_items = None
            elif self.max_items < max_items:
                self.max_items = max_items
        else:
            if oid[0] not in self.childs:
                self.childs[oid[0]] = InputTreeNode()
            self.childs[oid[0]].add(oid[1:], max_items)

    def __iter__(self) -> 'Iterator[tuple[tuple[int,...], int|None]]':
        if self.max_items != 0:
            yield (), self.max_items
        if self.max_items is None:
            return
        for nibble, node in sorted(self.childs.items()):
            for suffix, max_items in node:
                yield (nibble,) + suffix, max_items

    def __getitem__(self, oid:ObjectName) -> tuple[ObjectName, ObjectName]:
        r1, r2 = self.get(oid.asTuple())
        return ObjectName(r1), ObjectName(r2)

    def get(self, oid:tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if oid == (): return (),()
        if self.max_items is None: return (), oid
        prefix, suffix = self.childs[oid[0]].get(oid[1:])
        return (oid[0],) + prefix, suffix
# }}}

class InputProxyL1:  # {{{
    def __init__(self, parent:'InputValueGroups', inputs: dict[str, InputDefinition], suffix:ObjectName) -> None:
        self.parent = parent
        self.inputs = inputs
        self.suffix = suffix

    def get(self, key:str, default:'ValueExpressionRet|None' = None) -> 'ValueExpressionRet|None':
        ret = self[key]
        return default if ret is None else ret

    def __getattr__(self, key:str) -> 'ValueExpressionRet|None':
        try:
            idef = self.inputs[key]
        except KeyError:
            pass
        else:
            return self.parent.evaluate(idef, self.suffix)
        raise AttributeError(key)

    def __getitem__(self, key:str) -> 'ValueExpressionRet|None':
        idef = self.inputs[key]
        return self.parent.evaluate(idef, self.suffix)
# }}}

class InputProxyL2: # {{{
    proxies: dict[str, InputProxyL1]
    def __init__(self, parent:'InputValueGroups', inputs: dict[str,dict[str, InputDefinition]], suffix:ObjectName, xform:'InputTransformation|None') -> None:
        self.parent = parent
        self.inputs = inputs
        self.suffix = suffix
        self.proxies = {}
        self.xform  = xform

    def __getattr__(self, group:str) -> InputProxyL1:
        try:
            return self[group]
        except KeyError:
            pass
        raise AttributeError(group)

    def __getitem__(self, group:str) -> InputProxyL1:
        if group not in self.proxies:
            if group in self.inputs:
                self.proxies[group] = InputProxyL1(self.parent, self.inputs[group], self.suffix)
            elif group == 'walker':
                self.proxies[group] = InputProxyL1(self.parent, {'iid': WalkerInfo('iid')}, self.suffix)
            elif group == 'transform' and self.xform is not None:
                self.proxies[group] = self.xform.evaluate(self) # type: ignore[assignment] # white lies
        return self.proxies[group]
# }}}

class WalkerInfo(InputDefinition): # {{{
    def __init__(self, what:str) -> None:
        self.oid = ObjectName((65536,))
        self.what = what
# }}}

class InputValueGroups: # {{{
    def __init__(self, data:'dict[ObjectName,dict[ObjectName,SnmpValue|None]]', inputs:Inputs, root:InputTreeNode, transformations:dict[str, InputTransformation]) -> None:
        self.inputs = inputs
        self.data   = data
        self.root   = root
        self.transformations = transformations

    def __getitem__(self, iid_tname:tuple[ObjectName,str]) -> InputProxyL2:
        iid, tname = iid_tname
        return InputProxyL2(self, self.inputs.inputs, iid, self.transformations.get(tname, None))

    def evaluate(self, idef:InputDefinition, iid:ObjectName) -> 'ValueExpressionRet|None':
        if isinstance(idef, WalkerInfo):
            assert idef.what == 'iid'
            return str(iid)
        prefix, suffix = self.root[idef.oid]
        suffix = suffix + iid
        try:
            snmp_value:SnmpValue|ValueExpressionRet|None = self.data[prefix][suffix]
        except KeyError:
            snmp_value = None
        for filtername in idef.filters:
            if filtername == 'mac' and snmp_value is not None:
                assert isinstance(snmp_value, OctetString) # type: ignore[misc] # wtf mypy?
                snmp_value = ':'.join( f"{x:02x}" for x in snmp_value )
            elif filtername == 'ip' and snmp_value is not None:
                assert isinstance(snmp_value, OctetString) # type: ignore[misc] # wtf mypy?
                snmp_value = str(ipaddress.ip_address(bytes(snmp_value)))
            elif filtername == 'datetime' and snmp_value is not None:
                raise NotImplementedError(repr(snmp_value))
        if snmp_value is None or isinstance(snmp_value, (int, str)): return snmp_value
        if isinstance(snmp_value, (OctetString,ObjectName)): # type: ignore[misc] # wtf mypy?
            return str(snmp_value)
        if isinstance(snmp_value, TimeTicks): # type: ignore[misc] # wtf mypy?
            return int(snmp_value)/100.0
        if isinstance(snmp_value, univ.Integer): # type: ignore[misc] # wtf mypy?
            return int(snmp_value)
        raise NotImplementedError(f"evaluate({repr(snmp_value)})")


