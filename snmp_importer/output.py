from .walker  import ObjectName, Walker
from .inputs  import InputProxyL2, ValueExpressionRet, InputValueGroups, InputTreeNode, Inputs
from .metrics import Table, Device, MetricMetaData, ValueExpression
from typing import Iterator, Callable, IO, cast
from types  import TracebackType
import aiohttp, zlib, time, sys, re, logging

def prom_id(s:str) -> str: #  {{{
    assert re.match(r'^[-_a-zA-Z][-_a-zA-Z0-9]*$', s)
    return s.replace('-','_')
# }}}

class ErrorCollector: # {{{
    def __init__(self, prefix:str) -> None:
        self.prefix = prefix
        self.count = 0

    def add(self, error:str) -> None:
        self.count += 1
        print(f"{self.prefix}: {error}", file=sys.stderr)

    def add_expr(self, e:BaseException, iid:ObjectName, ve:ValueExpression) -> None:
        self.add(f"{type(e).__name__}: {e!r} when evaluating {ve.expression!r} for {iid!r} on {ve.fileInfo}")
# }}}

class ScrapedTableRow: # {{{
    def __init__(self, iid:ObjectName, row_input:InputProxyL2, table:Table, errors:ErrorCollector) -> None:
        self.iid = iid
        self.tname = table.name
        self.labels_dim:dict[str,ValueExpressionRet] = {}
        self.labels_val:dict[str,ValueExpressionRet] = {}
        self.influx_val:dict[str,float|int] = {}
        self.influx_mtags:dict[str,ValueExpressionRet] = {}
        self.prom_metric:dict[str,list[tuple[float|int,dict[str,str]]]] = {}
        self.scalar = table.scalar

        for name, ve in table.labels.items():
            try:
                value = ve.getValue(row_input)
            except Exception as e:
                errors.add_expr(e, self.iid, ve)
                value = None
            if value is not None:
                self.labels_val[name] = value

        for name, ve in table.dimensions.items():
            try:
                value = ve.getValue(row_input)
            except Exception as e:
                errors.add_expr(e, self.iid, ve)
                value = None
            if value is not None:
                self.labels_dim[name] = value

        for mv in table.metrics:
            for m in mv.variants:
                if m.suffix not in self.prom_metric: self.prom_metric[m.suffix] = []
                try:
                    value = m.expr.getValue(row_input, True)
                except Exception as e:
                    errors.add_expr(e, self.iid, m.expr)
                    value = None
                if value is not None:
                    assert isinstance(value, (float,int))
                    mtags = {}
                    for tn, te in m.tags.items():
                        ts = te.getValue(row_input)
                        if ts is not None:
                            mtags[tn] = str(ts)
                            if m.influx:
                                self.influx_mtags[f"{m.influx}_{tn}"] = ts
                    self.prom_metric[m.suffix].append( (value, m.labels.copy() | mtags ) )
                    if m.influx:
                        self.influx_val[m.influx]=value

    def get_prom_metrics(self) -> Iterator[tuple[str, float|int, dict[str,str]]]:
        root_labels = {}
        iid = str(self.iid)
        for ldict in self.labels_val, self.labels_dim:
            for k, lv in ldict.items():
                root_labels[k] = str(lv)
        emit_root = len(root_labels) > 0
        for mname, labeled_values in self.prom_metric.items():
            for value, labels in labeled_values:
                labels = labels.copy()
                if mname == '':
                    labels.update(root_labels)
                    labels['__name__'] = self.tname
                    emit_root = False
                else:
                    labels['__name__'] = f'{self.tname}_{mname}'
                if not self.scalar:
                    labels['iid'] = iid
                yield mname, value, labels
        if emit_root:
            if not self.scalar:
                root_labels['iid'] = iid
            root_labels['__name__'] = self.tname
            yield '', 0, root_labels
# }}}

class ScrapedTable: # {{{
    def __init__(self, name:str, iids:list[ObjectName], ivg:InputValueGroups, table:Table, errors:ErrorCollector, scalar:bool):
        self.name = name
        self.rows:dict[ObjectName,ScrapedTableRow] = {}
        self.scalar = scalar

        for iid in iids:
            self.rows[iid] = ScrapedTableRow(iid, ivg[iid, name], table, errors)
# }}}

class ScrapedTables: # {{{
    def __init__(self, tables:dict[str,ScrapedTable], errors:ErrorCollector, parent:'ScrapedDevice', timers:dict[str,float]) -> None:
        self.tables = tables
        self.errors = errors
        self.device = parent
        self.times  = timers

    def set_when(self, now:float) -> None:
        self.when = now

    scrape_meta = {
        "snmp_importer_errors": ("gauge", "Number of errors encountered when scraping device"),
        "snmp_importer_stats":  ("gauge", "Timers for scraping device"),
    }

    def get_prom_scrape_metrics(self, render_start:float) -> Iterator[tuple[float|int, dict[str,str]]]:
        yield self.errors.count,{"__name__":"snmp_importer_errors"}
        assert 'render' not in self.times
        for timer, timeval in self.times.items():
            yield timeval, {"__name__":"snmp_importer_stats","timer":timer}
        yield time.time() - render_start, {"__name__":"snmp_importer_stats","timer":"render"}
# }}}

class ScrapedDevice: # {{{
    def __init__(self, name:str, walker:Walker, definition:Device, inputs:Inputs) -> None:
        self.name = name
        self.walker = walker
        self.device = definition
        self.root = InputTreeNode()
        self.inputs = inputs
        for tname,table in self.device.tables.items():
            for k1, k2 in table.scrape.keys():
                self.root.add(self.inputs.get_oid(k1, k2), table.max_rows)
        self.scrape_limit = tuple( (ObjectName(oid), limit) for oid, limit in self.root) 

    async def scrape(self) -> ScrapedTables:
        errors = ErrorCollector(f"f{self.device.name}({self.walker.target.host})")
        scrape_start = time.time()
        try:
            retv = await self.walker.walk(self.scrape_limit)
        except Exception as e:
            errors.add(f"Scraping device failed: {type(e).__name__}: {e!r}")
            return ScrapedTables({},errors, self, {"scrape":time.time() - scrape_start, "rows": 0})
        scrape_end = time.time()
        scrape_time = scrape_end - scrape_start
        ivg = InputValueGroups(retv, self.inputs, self.root, self.device.transformations)
        tables_out:dict[str,ScrapedTable] = {}


        for tname, table in self.device.tables.items():
            iid_count:dict[ObjectName,int] = {}
            for k1, k2 in table.scrape.keys():
                prefix, suffix = self.root[self.inputs.get_oid(k1, k2)]
                for vecid in retv[prefix]:
                    if suffix.isPrefixOf(vecid):
                        iid = vecid[len(suffix):]
                        if iid not in iid_count: iid_count[iid] = 1
                        else: iid_count[iid] += 1
            iid_order = sorted( (cnt,iid) for iid, cnt in iid_count.items() )
            iids = [ x[1] for x in iid_order[:table.max_rows] ]
            tables_out[tname] = ScrapedTable(tname, iids, ivg, table, errors, table.scalar)
        rows_end = time.time()
        rows_time = rows_end - scrape_end
        return ScrapedTables(tables_out, errors, self,{"scrape":scrape_time,"rows":rows_time})
# }}}

class StdioResponse: # {{{
    status = 200
    def __init__(self, headers:dict[str,str]):
        self.headers = headers

    async def read(self) -> bytes:
        return b""

    async def text(self) -> str:
        return ""
# }}}

class StdioSession: # {{{
    def __init__(self, fd:IO[bytes], url:str, count:str) -> None:
        self.fd    = fd
        self.url   = url
        self.count = count
        self.resp_headers = {"X-Std-IO": url}

    async def post(self, url:str, data:bytes) -> StdioResponse:
        assert self.url == url, "Url change not supported!"
        if self.count:
            data = f"{self.count}: {len(data)}\n".encode('ascii')
        # This is in fact synchronous write.
        # FIXME may be asynchronous for pipes (and unix sockets)
        self.fd.write(data)
        try:
            self.fd.flush()
        except Exception:
            pass
        return StdioResponse(self.resp_headers)
# }}}

class StdIoSessionManager: # {{{
    def __init__(self, url:str):
        self.url = url
        self.file:IO[bytes]|None = None

    async def __aenter__(self) -> StdioSession:
        count=''
        url = self.url
        if url.startswith('size:'): #Just for testing
            count, url = url[5:].split(':',1)
        if url == 'stdout':
            return StdioSession(cast(IO[bytes],sys.stdout.buffer), self.url, count) # What the fucking cast? Python3 typing is cursed by Any.
        elif url == 'stderr':
            return StdioSession(cast(IO[bytes],sys.stderr.buffer), self.url, count)
        elif url.startswith('file://'):
            self.file = open(self.url[7:],'ab')
            # FIXME: use asyncio if file is pipe or socket
            return StdioSession(self.file, self.url, count)
        else:
            assert False, f"Invalid url {self.url}"

    async def __aexit__(self, et:type[BaseException], e:Exception, tb:TracebackType) -> None:
        if self.file is not None:
            self.file.close()
        return
#}}}

class SenderError(Exception): pass

senderlogger = logging.getLogger('send')

class Sender: # {{{
    def __init__(self, parent:'Renderer', session:aiohttp.ClientSession|StdioSession) -> None:
        self.session = session
        self.url     = parent.dest_url
        self.render  = parent.render
        self.encoder = parent.encoder

    async def send(self, data:bytes) -> None:
        encoded = self.encoder(data)
        senderlogger.log(7, f"{self}({self.url}) post ({len(data)}/{len(encoded)})")
        rv = await self.session.post(self.url, data=encoded)
        if rv.status in (200,204):
            senderlogger.info(f"{self}({self.url}) post result: {rv.status}")
            for k, v in rv.headers.items():
                senderlogger.log(7, f"> {k}: {v}")
            body = await rv.read() # Body should be small, we should consume it, to allow pipelining.
            senderlogger.log(7, f"body: {body!r}")
            return

        senderlogger.error(f"{self}({self.url}) post result: {rv.status}")
        msg = f"Invalid response ({rv.status})\n"
        try:
            for k, v in rv.headers.items():
                senderlogger.warning(f"> {k}: {v}")
                msg += f"> {k}: {v}\n"
            text = await rv.text()
            senderlogger.warning(f"body: {text}")
            msg += "> " + text
        except Exception as e:
            msg += f"Failed to parse invalid response headers: {e}\n"
        raise SenderError(msg)

    async def render_and_send(self, st:ScrapedTables) -> None:
        senderlogger.debug(f"{self}({self.url}) render_and_send: render")
        data = self.render(st)
        senderlogger.debug(f"{self}({self.url}) render_and_send: send")
        await self.send(data)
        senderlogger.debug(f"{self}({self.url}) render_and_send: send done")
# }}}

class Renderer: # {{{
    headers:dict[str,str] = {}

    @staticmethod
    def _identity(data:bytes) -> bytes: return data

    @staticmethod
    def get_gzip(gzip:int = -1) -> tuple[str, Callable[[bytes],bytes]]|None:
        if gzip > 0:
            def compressor(data:bytes) -> bytes:
                return zlib.compress(data, level=gzip, wbits=31)  # 25<=wbits<=31, for gzip compatibility, see documentation
            return ('gzip', compressor)
        else:
            return None

    def __init__(self, dest_url:str = '', colors:bool=False, encoding:tuple[str, Callable[[bytes],bytes]]|None = None):
        self.dest_url = dest_url
        self.headers = self.headers.copy()
        if colors:
            self.color = self._color_do
        else:
            self.color = self._color_dont
        if encoding is not None:
            self.headers['Content-Encoding'] = encoding[0]
            self.encoder = encoding[1]
        else:
            self.encoder = self._identity

    def _color_do(self, color:int) -> str:
        return f"\x1b[3{color};1m" if color else "\x1b[0m"

    def _color_dont(self, color:int) -> str:
        return ''

    def render(self, st:ScrapedTables) -> bytes: raise NotImplementedError("abstract method")

    session_manager: aiohttp.ClientSession|StdIoSessionManager
    async def __aenter__(self) -> Sender:
        if self.dest_url.startswith('http'):
            self.session_manager = aiohttp.ClientSession(headers=self.headers)
        else:
            self.session_manager = StdIoSessionManager(self.dest_url)
        return Sender(self, await self.session_manager.__aenter__())

    async def __aexit__(self, et:type[BaseException], e:Exception, tb:TracebackType) -> None:
        return await self.session_manager.__aexit__(et, e, tb)

    async def render_and_send_single(self, st:ScrapedTables) -> None:
        async with self as sender:
            await sender.render_and_send(st)
# }}}
