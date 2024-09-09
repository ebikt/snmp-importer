from .output  import Renderer, ScrapedTables, ScrapedDevice, prom_id
from .inputs  import ValueExpressionRet
from .config  import YamlValue
import re, time

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
    return  '"' + s.rstrip(" ").replace('\\', '\\\\').replace('\n','\\n').replace('"', '\\"') + '"'
# }}}

class PrometheusText(Renderer): # {{{

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": "snmp-exporter/0.1",
    }

    def __init__(self, cfg:YamlValue):
        with cfg.asStruct() as s:
            dest_url = s['url'].asStr()
            colors   = s['colors'].asBool(default=False)
            gzip     = s['gzip'].asInt(default=[9,-1][colors], min=-1, max=9)
            job      = s['job'].asStr(default='snmp2prom')

        self.job = job
        super().__init__(dest_url, colors, self.get_gzip(gzip))

    def render(self, sts:ScrapedTables) -> bytes:
        render_start = time.time()
        now = str(round(sts.when*1000))
        now = f"{self.color(4)}{now}{self.color(0)}"
        ret:dict[tuple[str,str],list[str]] = {}
        mnames:dict[tuple[str,str],str] = {}
        global_labels = f"{self.color(6)}job={self.color(0)}{prom_label(self.job)},{self.color(2)}instance={self.color(0)}{prom_label(sts.device.name)},{self.color(2)}definition={self.color(0)}{prom_label(sts.device.device.name)}"
        for tname, st in sorted(sts.tables.items()):
            for iid, row in sorted(st.rows.items()):
                for mname, value, labels in row.get_prom_metrics():
                    mkey = (st.name, mname)
                    if mkey not in ret: ret[mkey] = []
                    assert isinstance(value, (int, float))
                    out_labels = [ f"{self.color(1)}{prom_id(key)}={self.color(0)}{prom_label(evalue)}" for key, evalue in sorted(labels.items()) if key != '__name__' ]
                    mname = prom_id(labels['__name__'])
                    mnames[mkey] = mname
                    ret[mkey].append(f"{self.color(3)}{mname}{self.color(0)}{{{global_labels},{','.join(out_labels)}}} {value}")
        rets = []
        for mkey, mrows in sorted(ret.items()):
            meta = sts.device.device.metadata[mkey]
            mname = mnames[mkey]
            if meta is not None:
                if len(meta.description):
                    help = meta.description
                    if help.endswith('\\'): help += '.'
                    rets.append(f"# {self.color(2)}HELP {self.color(3)}{mname}{self.color(0)} {help}\n")
                if len(meta.type):
                    rets.append(f"# {self.color(2)}TYPE {self.color(3)}{mname}{self.color(4)} {prom_id(meta.type)}{self.color(0)}\n")
            for mrow in mrows:
                rets.append(f"{mrow} {now}\n")

        last_scrape_metric = ''
        for mvalue, labels in sts.get_prom_scrape_metrics(render_start):
            mname = prom_id(labels['__name__'])
            if last_scrape_metric != mname:
                last_scrape_metric = mname
                smt, smh = sts.scrape_meta[mname]
                rets.append(f"# {self.color(2)}HELP {self.color(3)}{mname}{self.color(0)} {smh}\n")
                rets.append(f"# {self.color(2)}TYPE {self.color(3)}{mname}{self.color(4)} {smt}{self.color(0)}\n")
            out_labels = [ f",{self.color(1)}{prom_id(key)}={self.color(0)}{prom_label(evalue)}" for key, evalue in sorted(labels.items()) if key != '__name__' ]
            rets.append(f"{self.color(3)}{mname}{self.color(0)}{{{global_labels + ''.join(out_labels)}}} {mvalue} {now}\n")
        return ''.join(rets).encode('utf-8')
# }}}
