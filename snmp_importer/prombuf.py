from .output import ScrapedTables, Renderer, ScrapedDevice, prom_id
from .inputs import ValueExpressionRet
from .protobuf import prometheus_pb2
from .config import YamlValue
import re, time
MYPY=False
if MYPY:
    class snappy:
        @staticmethod
        def compress(b:bytes) -> bytes: ...
else:
    import snappy

def prom_label(v:ValueExpressionRet) -> str: # {{{
    if isinstance(v, bool):
        s = str(int(v))
    elif isinstance(v, (int, float)):
        s = str(v)
    elif isinstance(v, bytes):
        try:
            s = v.decode('utf-8')
        except Exception:
            s = v.decode('iso8859-1')
    elif isinstance(v, str):
        s = v
    else:
        raise NotImplementedError(f"prom_label {type(v)}")
    return  s.rstrip(" ")
# }}}

class PrometheusV1ProtoBuf(Renderer): # {{{

    headers = {
        "Content-Type": "application/x-protobuf",
        "X-Prometheus-Remote-Write-Version": "0.1.0",
        "User-Agent": "snmp-exporter/0.1",
    }

    """
    DONE:
    MUST have label names sorted in lexicographical order

    OUT OF SCOPE: (let vmagent handle this for us)
    MUST send samples for any given series in timestamp order
    MUST retry write requests on HTTP 5xx responses and MUST use a backoff algorithm to prevent overwhelming the server
    MUST NOT retry write requests on HTTP 2xx and 4xx responses other than 429
    MAY retry on HTTP 429 responses, which could result in senders "falling behind"

    WONTUSE:
    MAY send multiple requests for different series in parallel
    Stale markers MUST be signalled by the special NaN value 0x7ff0000000000002. This value MUST NOT be used otherwise.

    WONTFIX:
    MUST send stale markers when a time series will no longer be appended to.
    """

    def __init__(self, cfg:YamlValue):
        with cfg.asStruct() as s:
            dest_url = s['url'].asStr()
            colors   = s['colors'].asBool(default=False)
            job      = s['job'].asStr(default='snmp2prom')

        self.job = job
        super().__init__(dest_url, colors, ('snappy', snappy.compress))

    def render(self, sts:ScrapedTables) -> bytes:
        render_start = time.time()
        now = round(sts.when*1000)
        wrq = prometheus_pb2.WriteRequest()
        global_labels = {'definition':sts.device.device.name, 'instance':sts.device.name, 'job':self.job}
        def add_value(value:float|int, labels:dict[str,str])->None:
            series = wrq.timeseries.add()
            for key, labelval in sorted((labels|global_labels).items()):
                label = series.labels.add()
                label.name = prom_id(key)
                label.value =  prom_label(labelval)
            sample = series.samples.add()
            sample.value = value
            sample.timestamp = now
        for tname, st in sorted(sts.tables.items()):
            for iid, row in sorted(st.rows.items()):
                for mname, value, labels in row.get_prom_metrics():
                    add_value(value, labels)
        for value, labels in sts.get_prom_scrape_metrics(render_start):
            add_value(value, labels)
        return wrq.SerializeToString()
# }}}

# TODO Implement prometheus V2 protobuf format.
# We do not use this yet, as it has no support in vmagent yet
