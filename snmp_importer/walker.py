import asyncio
import socket
import logging
MYPY = False


if MYPY:
    from typing import overload, Self, Iterator, Literal
    # white lies
    class SnmpAuthType: pass
    class SnmpDispatcherType: pass
    class SnmpTransportType: pass

    import pyasn1.type.univ
    import pyasn1.type.base

    class EndOfMibView(pyasn1.type.univ.Null): pass
    class NoSuchInstance(pyasn1.type.univ.Null): pass
    class NoSuchObject(pyasn1.type.univ.Null): pass

    class SimpleAsn1Type(pyasn1.type.base.Asn1Type):
        def isValue(self) -> bool: ...
        readonly: bool
        typeId: int

    class ObjectName(pyasn1.type.univ.ObjectIdentifier, SimpleAsn1Type):
        _value: tuple[int, ...]
        def __add__(self, other:ObjectName|tuple[int, ...] ) -> ObjectName: ...
        def __radd__(self, other:ObjectName|tuple[int, ...] ) -> ObjectName: ...
        def asTuple(self) -> tuple[int, ...]: ...
        def isPrefixOf(self, other:ObjectName|tuple[int, ...]) -> bool: ...

        @overload
        def __getitem__(self, s:int) -> int: ...
        @overload
        def __getitem__(self, s:slice) -> ObjectName: ...
        def __getitem__(self, s:int|slice) -> int|ObjectName: ...


    class OctetString(pyasn1.type.univ.OctetString, SimpleAsn1Type):
        def __str__(self) -> str: ...
        def __bytes__(self) -> bytes: ...
        def asOctets(self) -> bytes: ...
        def asNumbers(self) -> tuple[int, ...]: ...
        def __len__(self) -> int: ...

        @overload
        def __getitem__(self, s:int) -> int: ...
        @overload
        def __getitem__(self, s:slice) -> OctetString: ...
        def __getitem__(self, s:int|slice) -> int|OctetString: ...

        def __iter__(self) -> Iterator[int]: ...
        def __contains__(self, other:int|bytes) -> bool: ...
        def __int__(self) -> int: ...
        def __float__(self) -> float: ...
        def __reversed__(self) -> OctetString: ...
        def prettyOut(self) -> str: ... #type: ignore[override]

    class IntegerBase(pyasn1.type.univ.Integer, SimpleAsn1Type):
        def __and__(self, other:Integer|int) -> Integer: ...
        def __rand__(self, other:Integer|int) -> Integer: ...
        def __or__(self, other:Integer|int) -> Integer: ...
        def __ror__(self, other:Integer|int) -> Integer: ...
        def __xor__(self, other:Integer|int) -> Integer: ...
        def __rxor__(self, other:Integer|int) -> Integer: ...
        def __lshift__(self, other:int) -> Integer: ...
        def __rhsift__(self, other:int) -> Integer: ...
        def __add__(self, other:Integer|int) -> Integer: ...
        def __radd__(self, other:Integer|int) -> Integer: ...
        def __sub__(self, other:Integer|int) -> Integer: ...
        def __rsub__(self, other:Integer|int) -> Integer: ...
        def __mul__(self, other:Integer|int) -> Integer: ...
        def __rmul__(self, other:Integer|int) -> Integer: ...
        def __mod__(self, other:Integer|int) -> Integer: ...
        def __rmod__(self, other:Integer|int) -> Integer: ...
        def __pow__(self, other:Integer|int) -> Integer: ... # type: ignore[override]
        def __rpow__(self, other:Integer|int) -> Integer: ...
        def __floordiv__(self, other:Integer|int) -> Integer: ...
        def __rfloordiv__(self, other:Integer|int) -> Integer: ...
        # def __truediv__(self, other:Integer|int) -> Real: ...
        # def __rtruediv__(self, other:Integer|int) -> Real: ...
        def __divmod__(self, other:Integer|int) -> Integer: ...
        def __rdivmod__(self, other:Integer|int) -> Integer: ...

        def __int__(self) -> int: ...
        def __float__(self) -> float: ...
        def __index__(self) -> int: ...
        def __abs__(self) -> Integer: ...
        def __pos__(self) -> Integer: ...
        def __neg__(self) -> Integer: ...
        def __invert__(self) -> Integer: ...
        #FIXME wrong typing stuff in pyasn1, but we do not need this
        #@overload
        #def __round__(self, n:Literal[0]) -> int: ...
        #@overload
        #def __round__(self, n:int) -> Integer: ...
        #def __round__(self, n:int) -> int|Integer: ...
        def __floor__(self) -> int: ...
        def __ceil__(self) -> int: ...
        def __trunc__(self) -> Integer: ...
        def prettyOut(self) -> str: ... #type: ignore[override]

    class Integer(IntegerBase): pass
    class TimeTicks(IntegerBase): pass
else:
    from pysnmp.proto.errind  import ErrorIndication, requestTimedOut
    from pysnmp.proto.rfc1902 import ObjectName, OctetString, Integer, TimeTicks
    from pysnmp.smi.rfc1902   import ObjectType,ObjectIdentity
    from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject
    import pysnmp.hlapi.transport
    import pycares
    import pysnmp.hlapi.v3arch.asyncio as snmp3

    SnmpAuthType = snmp3.CommunityData|snmp3.UsmUserData
    SnmpDispatcherType = snmp3.SnmpEngine
    SnmpTransportType = pysnmp.hlapi.transport.AbstractTransportTarget

#FIXME: add here more as needed
SnmpValue = Integer|TimeTicks|OctetString|ObjectName

from typing import Iterable, Callable, NoReturn, Generic, TypeVar
from types import TracebackType

from .config import YamlValue, ConfigError

snmplogger = logging.getLogger('snmp')

def parse_address(address:str) -> tuple[str, int|None]: # {{{
    if ':' not in address:
        parsed_host = address
        parsed_port:str|None = None
    else:
        parsed_host, parsed_port = address.rsplit(':',-1)
        if not parsed_port.isnumeric() or min( c in ':0123456789abcdefABCDEF' for c in parsed_host):
            parsed_host = address
            parsed_port = None
    if parsed_host[:1] == '[' and parsed_host[-1:] == ']':
        parsed_host = parsed_host[1:-1]
    return parsed_host, (None if parsed_port is None else int(parsed_port))
# }}}

class Target: # {{{
    engine:    SnmpDispatcherType
    auth:      SnmpAuthType
    host: str
    port: int
    ipv6: bool | None
    transport: SnmpTransportType
    tcp     = False
    timeout = 3.3
    retries = 3

    def __init__(self,
            auth:'Auth',
            host:str,
            port:int|None = None,
            path:list[str]|None = None,
            engine: SnmpTransportType|None = None,
            timeout: int = 2,
            retries: int = 3,
            ipv6: bool|None = None,
            tcp:  bool = False,
        ) -> None:
        self.host, parsed_port = parse_address(host)
        assert parsed_port is None or port is None, f"Port specified both as argument and as part of host string"
        self.port = parsed_port if parsed_port is not None else port if port is not None else 161
        self.ipv6 = ipv6
        self.tcp  = tcp
        self.timeout = timeout
        self.retries = retries
        self.auth = auth.get_auth( path if path is not None else [host] )
        if MYPY:
            assert False, "never happens"
        else:
            if engine is None:
                self.engine = snmp3.SnmpEngine()
            else:
                self.engine = engine
        snmplogger.log(5, f"{self}: {self.host}:{self.port} {path} self.engine (ipv6:{self.ipv6}, tcp:{self.tcp})")

    _FAMILIES:dict[bool|None,int] = {
        True:socket.AF_INET6,
        False:socket.AF_INET,
        None:0
    }
    async def resolve(self) -> SnmpTransportType:
        snmplogger.log(5, f"{self}: {self.host}:{self.port} getaddrinfo")
        results = await asyncio.get_running_loop().getaddrinfo(self.host, self.port,
            family=self._FAMILIES[self.ipv6],
            type=socket.SOCK_DGRAM,
            proto=socket.IPPROTO_UDP,
        )
        addr = min(results)[4][:2]
        assert len(addr) == 2
        if MYPY:
            assert False, "never happens"
        else:
            if ':' in addr[0]:
                if self.tcp:
                    cls = snmp3.Tcp6TransportTarget
                else:
                    cls = snmp3.Udp6TransportTarget
            else:
                if self.tcp:
                    cls = snmp3.TcpTransportTarget
                else:
                    cls = snmp3.UdpTransportTarget
            self.transport = cls(addr)
            self.common_args = (
                self.engine,
                self.auth,
                self.transport,
                snmp3.ContextData(),
            )
        snmplogger.log(5, f"{self}: {self.host}:{self.port} getaddrinfo -> {addr}")
        return self.transport

    if MYPY:
        async def getCmd(self, oids: Iterable[ObjectName|str])  -> list[tuple[ObjectName, SnmpValue]]: ...
        async def nextCmd(self, oids: Iterable[ObjectName|str]) -> list[list[tuple[ObjectName, SnmpValue]]]: ...
        async def bulkCmd(self, nonRepeaters:int, bulkWidth:int, oids:Iterable[ObjectName|str]) -> list[list[tuple[ObjectName, SnmpValue]]]: ...
        def close(self) -> None: ...
    else:
        async def prepare(self, oids):
            if not hasattr(self, 'transport'): await self.resolve()
            return [ObjectType(ObjectIdentity(oid)) for oid in oids ]

        async def wrapCmd(self, varBinds, command, *args, **kwargs):
            retries = self.retries
            common_args = self.common_args
            kwargs = {'lookupMib':False,'lexicographicMode':False} | kwargs
            while retries > 0:
                try:
                    async with asyncio.timeout(None if self.timeout <= 0 else self.timeout):
                        snmplogger.log(1,f"{self} await {command}")
                        errorIndication, errorStatus, errorIndex, result = await command(*common_args, *args, *varBinds, **kwargs)
                        snmplogger.log(1,f"{self} await {command} done")
                        if errorIndex or errorStatus or errorIndication:
                            try:
                                errorOid = varBinds[errorIndex - 1][0] if errorIndex else None
                            except Exception:
                                errorOid = None
                            if errorIndication:
                                if errorOid:
                                    errorIndication.__descr += f" at OID: {errorOid}"
                                raise errorIndication
                            if not errorStatus:
                                errorStatus = "unknown error"
                            if errorOid:
                                errorStatus += f" at OID {errorOid}"
                            raise ErrorIndication(repr(errorStatus))
                        return result
                except TimeoutError:
                    snmplogger.log(7,f"{self} {command} timeout, retries: {retries}")
                    retries -= 1
                else:
                    assert False, "never happens"
            raise requestTimedOut

        async def getCmd(self, oids: Iterable[ObjectName|str]) -> list[tuple[ObjectName, SnmpValue]]:
            varBinds = await self.prepare(oids)
            return await self.wrapCmd(
                varBinds,
                snmp3.getCmd,
            )

        async def nextCmd(self, oids: Iterable[ObjectName|str]) -> list[list[tuple[ObjectName, SnmpValue]]]:
            varBinds = await self.prepare(oids)
            return await self.wrapCmd(
                varBinds,
                snmp3.nextCmd,
            )

        async def bulkCmd(self, nonRepeaters:int, bulkWidth:int, oids:Iterable[ObjectName|str]) -> list[list[tuple[ObjectName, SnmpValue]]]:
            varBinds = await self.prepare(oids)
            return await self.wrapCmd(
                varBinds,
                snmp3.bulkCmd,
                nonRepeaters,
                bulkWidth,
            )
        def close(self) -> None:
            snmplogger.log(3,f"{self} close")
            self.engine.closeDispatcher() 
# }}}

T=TypeVar('T')
class IndexProbe(Generic[T]): # {{{
    def __init__(self, ret:T) -> None:
        self.ret = ret
        self.indices:set[int|str] = set()

    def __getitem__(self, index:str|int) -> T:
        if not isinstance(index, (str, int)):
            raise ConfigError(f"Path index is of wrong type {type(index)!r}")
        self.indices.add(index)
        return self.ret
# }}}

class Auth: # {{{
    kwargs:dict[str,str|None]
    args:list[str]
    path_indices:set[str|int]

    def __init__(self, cfg: YamlValue) -> None:
        with cfg.asStruct() as s:
            if not s['user'].isMissing():
                self.args = [s['user'].asStr()]
                self.kwargs = {}
                if not (s['auth'].isMissing() and s['auth_alg'].isMissing()):
                    self.kwargs['authKey'] = s['auth'].asStr()
                    if not MYPY and not s['auth_alg'].isMissing():
                        self.kwargs['authProtocol'] = getattr(snmp3,f"USM_AUTH_{s['auth_alg'].asStr()}")
                if not (s['priv'].isMissing() and s['priv_alg'].isMissing()):
                    self.kwargs['privKey'] = s['priv'].asStr()
                    if not MYPY and not s['priv_alg'].isMissing():
                        self.kwargs['privProtocol'] = getattr(snmp3,f"USM_PRIV_{s['priv_alg'].asStr()}")
                if not MYPY:
                    self.auth_class = snmp3.UsmUserData
            else:
                if s['community'].isMissing():
                    self.args = ['public']
                else:
                    self.args = [s['community'].asStr()]
                if not s['index'].isMissing():
                    self.args.insert(0, s['index'].asStr())
                self.kwargs = {}
                if not MYPY:
                    self.auth_class = snmp3.CommunityData
        probe = IndexProbe('(probe)')
        self.get_auth(probe)
        self.path_indices = probe.indices

    def get_auth(self, path:list[str]|IndexProbe[str]) -> SnmpAuthType:
        args   = [ x.format(path=path) if isinstance(x, str) else x for x in self.args ]
        kwargs = { k:(x.format(path=path) if isinstance(x, str) else x) for k,x in self.kwargs.items() }
        if MYPY:
            assert False
        else:
            if not isinstance(path, IndexProbe):
                return self.auth_class(*args, **kwargs)
# }}}

class Break(Exception): pass

class Walker: # {{{
    def __init__(self,
            target:Target,
            bulkEnabled:bool = True,
            getBulkLength:int = 50,
            walkBulkLength:int = 30,
            walkBulkWidth:int = 10
        ): # {{{
        snmplogger.log(6, f"{self}: ({target.host}:{target.port}) new")
        self.target = target
        self.bulkEnabled = bulkEnabled
        self.getBulkLength = getBulkLength
        self.walkBulkLength = walkBulkLength
        self.walkBulkWidth = walkBulkWidth
    # }}}

    def toOid(self, oid:str|ObjectName) -> ObjectName: # {{{
        if MYPY: assert False
        else: return ObjectName(oid)
    # }}}

    async def get_next(self, oids:Iterable[ObjectName|str], bulkLength:int|None = None, strict:bool = False) -> dict[ObjectName, SnmpValue|None]: # {{{
        """ Gets scalar values (ending with .0), or just first subvalue if strict == False """
        if bulkLength is None:
            bulkLength = self.getBulkLength
        oidsIn = [self.toOid(oid) for oid in oids]
        scalars:dict[ObjectName, SnmpValue|None] = { oid:None for oid in oidsIn }
        if MYPY:
            oidMatches: Callable[[ObjectName, ObjectName], bool]
        else:
            if strict:
                oidMatches = lambda outer, inner: outer + (0,) == inner
            else:
                oidMatches = lambda outer, inner: outer.isPrefixOf(inner)
        while len(oidsIn):
            length = min(len(oidsIn), bulkLength)
            if self.bulkEnabled:
                tbl = await self.target.bulkCmd(length, 1, oidsIn[:length])
                assert len(tbl) == 1
                row = tbl[0]
            else:
                tbl = await self.target.nextCmd(oidsIn[:length])
                assert len(tbl) == 1
                row = tbl[0]
            assert len(row) == length
            for i, kv in enumerate(row):
                reqOid = oidsIn[i]
                if oidMatches(reqOid, kv[0]):
                    scalars[reqOid] = kv[1]
                else:
                    scalars[reqOid] = None
            oidsIn = oidsIn[length:]
        return scalars
    # }}}

    async def walk(self, oids:Iterable[ObjectName|str|tuple[ObjectName|str,int|None]], bulkWidth:int|None = None, bulkLength:int|None = None) -> dict[ObjectName,dict[ObjectName,SnmpValue|None]]: # {{{
        """ Walk oids, up to bulkLength at once, upto bulkWidth deep.
            individual oid may be string representation or ObjectName, or tuple (oid, max_items),
            where max_items is maximum scraped values for that oid
        """
        snmplogger.log(4, f"{self}: ({self.target.host}:{self.target.port}) walk start")
        if bulkWidth is None:
            bulkWidth = self.walkBulkWidth
        if bulkLength is None:
            bulkLength = self.walkBulkLength
        oidLim = {}
        oidsIn = []
        for oidin in oids:
            if isinstance(oidin, (ObjectName, str)): # type: ignore[misc] # wtf mypy?
                oidu = oidin
                limit = None
            else:
                oidu, limit = oidin
            vecOid = self.toOid(oidu)
            oidLim[vecOid] = limit
            oidsIn.append( (vecOid, vecOid) )
        oidSet = {vecOid:curOid for vecOid, curOid in oidsIn}
        vectors:dict[ObjectName,dict[ObjectName,SnmpValue|None]] = {oid:{} for oid in oidSet}
        offset = 0
        while len(oidSet):
            oidsIn = sorted(oidSet.items())
            if len(oidsIn) > bulkLength:
                oidsIn = oidsIn[offset:offset+bulkLength]
                offset = (offset + bulkLength) % len(oidsIn)
            if self.bulkEnabled:
                tbl = await self.target.bulkCmd(0, bulkWidth, (oid[1] for oid in oidsIn) )
            else:
                tbl = await self.target.nextCmd( ( oid[1] for oid in oidsIn ) )
            for row in tbl:
                for i, kv in enumerate(row):
                    curOid = kv[0]
                    vecOid = oidsIn[i][0]
                    l = len(vecOid)
                    try:
                        if not vecOid.isPrefixOf(curOid): raise Break()
                        if isinstance(kv[1], EndOfMibView): raise Break() # type: ignore[misc] # wtf mypy?
                        if isinstance(kv[1], (NoSuchObject, NoSuchInstance)): raise NotImplementedError("FIXME: do we need to break loop?") # type: ignore[misc] # wtf mypy?
                        lim = oidLim[vecOid]
                        if lim is not None:
                            if lim <= 0: raise Break()
                            oidLim[vecOid] = lim - 1
                        oidSet[vecOid] = curOid
                        vectors[vecOid][curOid[l:]] = kv[1]
                    except Break:
                        oidSet.pop(vecOid, None)
                        if offset >= i and offset > 0:
                            offset = offset - 1
        snmplogger.log(4,f"{self}: ({self.target.host}:{self.target.port}) walk end")
        return vectors
    # }}}
# }}}
