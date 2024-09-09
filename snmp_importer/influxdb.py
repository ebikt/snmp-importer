from .output import Renderer, ScrapedTables, ScrapedDevice
from .inputs import ValueExpressionRet
from .config import YamlValue
import re
import json
import time

def influx_id(s:str) -> str: # {{{
    assert re.match(r'^[a-zA-Z][-_a-zA-Z0-9]*$', s)
    return s.replace('-','_')
# }}}

def influx_lval(v:ValueExpressionRet) -> str: # {{{
    if isinstance(v, bool):
        return json.dumps(v)
    elif isinstance(v, int):
        return str(v)
    elif isinstance(v, float):
        return str(v)
    elif isinstance(v, bytes):
        s = ''.join(chr(c) if c < 127 else '?' for c in v)
    elif isinstance(v, str):
        s = v
    else:
        raise NotImplementedError(f"influx_lval {type(v)}")
    s = s.replace('\\','∖') #Do not produce "\\": influxdb may crash if multiple backslashes occur at wrong place
    if s == '': s = '-' # tag value must not be empty
    s = re.sub('[\x00-\x1f\x7f]','?', s)
    return re.sub(r'([ ,=])',r'\\\1', s.rstrip(' '))
# }}}

def influx_mval(v:ValueExpressionRet) -> str: # {{{
    if isinstance(v, bool):
        return json.dumps(v)
    elif isinstance(v, int):
        return str(v)+'i'
    elif isinstance(v, float):
        return str(v)
    elif isinstance(v, bytes):
        s = ''.join(chr(c) if c < 127 else '?' for c in v)
    elif isinstance(v, str):
        s = v
    else:
        raise NotImplementedError(f"influx_lval {type(v)}")
    s = s.replace('\\','∖') #Do not produce "\\": influxdb may crash if multiple backslashes occur at wrong place
    s = re.sub('[\x00-\x1f]','?', s)
    s = s.replace('"','\\"')
    return f'"{s}"'
# }}}

class InfluxDB(Renderer): # {{{

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "snmp-exporter/0.1",
    }

    def __init__(self, cfg:YamlValue):
        with cfg.asStruct() as s:
            dest_url = s['url'].asStr()
            colors   = s['colors'].asBool(default=False)
            gzip     = s['gzip'].asInt(default=[9,-1][colors], min=-1, max=9)
        super().__init__(dest_url, colors, self.get_gzip(gzip))

    def render(self, sts:ScrapedTables) -> bytes:
        render_start = time.time()
        now = str(round(sts.when * 1000000000))
        now = f"{self.color(4)}{now}{self.color(0)}"
        ret = []
        host_dim = f"{self.color(2)}host={self.color(0)}{influx_lval(sts.device.name)},{self.color(2)}definition={self.color(0)}{sts.device.device.name}"
        for tname, st in sorted(sts.tables.items()):
            for iid, row in sorted(st.rows.items()):
                influx_dims  = [host_dim]
                if not st.scalar:
                    influx_dims.append(f"{self.color(1)}iid={self.color(0)}{iid}")
                influx_dims += [f"{self.color(5)}{influx_id(key)}={self.color(0)}{influx_lval(value)}" for key, value in sorted(row.labels_dim.items())]
                influx_dims += [f"{self.color(5)}{influx_id(key)}={self.color(0)}{influx_lval(value)}" for key, value in sorted(row.influx_mtags.items())]
                influx_vals  = [f"{self.color(6)}{influx_id(key)}={self.color(0)}{influx_mval(value)}" for key, value in sorted(row.labels_val.items())]
                influx_vals += [f"{self.color(3)}{influx_id(key)}={self.color(0)}{influx_mval(value)}" for key, value in sorted(row.influx_val.items())]
                if influx_vals:
                    ret.append(f"{self.color(7)}{influx_id(st.name)}{self.color(0)},{','.join(influx_dims)} {','.join(influx_vals)} {now}\n")
        ret.append(f"{self.color(7)}snmp_importer_errors{self.color(0)},{host_dim} errors={sts.errors.count} {now}\n")
        timer_prefix = f"{self.color(7)}snmp_importer_stats{self.color(0)},{host_dim}"
        times = sts.times.copy()
        times['render'] = time.time() - render_start
        for timer, timeval in sorted(times.items()):
            ret.append(f"{timer_prefix},{self.color(1)}timer={self.color(0)}{influx_lval(timer)} {self.color(3)}seconds={self.color(0)}{timeval} {now}\n")
        return ''.join(ret).encode('utf-8')
# }}}
