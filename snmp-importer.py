#!/usr/bin/python3

import asyncio, argparse, os, sys, json
from typing import cast, Annotated, Literal
import logging.config

from snmp_importer.config      import YamlValue
from snmp_importer.executor    import Executor
MYPY=False
try:
    from aio_exporter.aio_sdnotify import SystemdNotifier
except ImportError:
    if not MYPY:
        class SystemdNotifier:
            async def notify(self, **statekw:bytes|str|int) -> bool:
                return False

BASE_DIR = os.path.dirname(__file__)

class ArgMeta: # {{{
    __metadata__: tuple[str, dict[str, object]]

class Deleted: pass
# }}}

class LogFormat(logging.Formatter): # {{{
    def format(self, record:logging.LogRecord) -> str:
        record.cl = self.level_color(record.levelno)
        return super().format(record)

    if not MYPY:
        def __init__(self, *args, **kwargs) -> None:
            self._colors = kwargs.pop('color')
            super().__init__(*args, **kwargs)

    _LEVEL_NAMES = {
        10: 'DBG',
        20: 'INF',
        30: 'WRN',
        40: 'ERR',
        50: 'CRI',
    }

    _LEVEL_COLORS = [
        "30;1",
        "34",
        "37",
        "33",
        "31",
        "31;1",
    ]
    _colors = True

    def level_color(self, level:int) -> str:
        if level in self._LEVEL_NAMES:
            txt = self._LEVEL_NAMES[level]
        elif 10 < level < 60:
            offset = level % 10
            txt = f"{self._LEVEL_NAMES[level-offset][:1]}_{offset}"
        else:
            txt = f"{level:3d}"
        if not self._colors:
            return txt
        level = min(max(0,level // 10), len(self._LEVEL_COLORS)-1)
        return f"\x1b[{self._LEVEL_COLORS[level]}m{txt}\x1b[0m"
# }}}

class Args:
    cfg_dir:     Annotated[str,      '-D', dict(default=os.path.join(BASE_DIR, 'cfg'),
                help='Base directory for configuration paths starting with @/')]
    inputs:      Annotated[str,      '-i', dict(default='@/inputs.yml',
                help='SNMP OID descriptiors configuration (we do not use MIB files)')]
    tables:      Annotated[str,      '-t', dict(default='@/tables.yml',
                help='Named table sets to scrape. Single entry in schedule scrapes signle table set.')]
    auths:       Annotated[str,      '-a', dict(default='@/auth.yml',
                help='Configuration of credentials. Single entry in schedule uses single credential.')]
    schedule:    Annotated[str,      '-s', dict(default='[]',
                help='Schedule configuration.')]
    outputs:     Annotated[str,      '-o', dict(default='@/outputs.yml',
                help='Output configuration.')]
    test:        Annotated[str|None, '-T', dict(default=None, metavar='TABLE/ADDRESS/AUTH[,retries=CNT][,timeout=SECONDS]',
                help='run single scrape of table set TABLE on address ADDRESS using auth AUTH')]
    test_influx: Annotated[bool,     '-I', dict(default=False, action='store_true',
                help='Use colored stdout influx writer as output.')]
    test_prom:   Annotated[bool,     '-P', dict(default=False, action='store_true',
                help='Use colored stdout promfile writer as output.')]
    test_run:    Annotated[int,      '-R', dict(default=0, metavar='SECONDS',type=int,
                help='Exit after SECONDS (for testing of scheduler)')]

    loglevel:    Annotated[str,      '-l', dict(default=None, metavar="LEVEL",
                help='Logging level (number or python logging constant (CRITICAL, ERROR, WARNING, INFO, DEBUG)')]
     # {{{ 
    def __init__(self) -> None:
        self.parser = argparse.ArgumentParser()
        deleted = Deleted()
        for k, v in cast(dict[str,ArgMeta], self.__annotations__).items():
            short = v.__metadata__[0]
            m     = v.__metadata__[1].copy()
            assert isinstance(short, str) and short[0] == '-'
            long  = '--' + k.replace('_','-')
            self.parser.add_argument(long, short, **m) # type: ignore

    def parse(self) -> tuple[dict[str, YamlValue|str], Literal["run","test","cfg"], int]:
        args = self.parser.parse_args()
        loglevel_str = cast(str|None, args.loglevel)
        if loglevel_str is None:
            if cast(object, args.test) is None:
                loglevel = 20 # INFO
            else:
                loglevel = 0 # ALL
        elif loglevel_str.isdigit():
            loglevel = int(loglevel_str)
        else:
            try:
                loglevel = cast(int, getattr(logging, loglevel_str.upper()))
                assert isinstance(loglevel, int)
            except Exception:
                print("--loglevel expects number or python logging constant", file=sys.stderr)
                sys.exit(1)
        lg = logging.getLogger("test")
        if not MYPY:
            lf = LogFormat(
                fmt   = "%(created).5f %(cl)s %(name)s(%(filename)s:%(lineno)s): %(message)s",
                color = sys.stderr.isatty(),
            )
            lh = logging.StreamHandler(stream=sys.stderr) #FIXME this blocks!
            lh.setFormatter(lf)
            lr = logging.getLogger()
            lr.setLevel(loglevel)
            assert len(lr.handlers) == 0
            lr.addHandler(lh)

        logging.log(2,"2")
        logging.log(1,"1")
        logging.debug("dbg")
        lg.log(2,"2")
        lg.log(1,"1")
        lg.debug("dbg")
        ret:dict[str, YamlValue|str] = {}
        test_outputs = {}
        if cast(bool, args.test_prom):
            test_outputs['promtext'] = {'type':   'promtext', 'url': 'stdout', 'colors': True}
        if cast(bool, args.test_influx):
            test_outputs['influx']   = {'type':     'influx', 'url': 'stdout', 'colors': True}
        if len(test_outputs):
            ret['output_path'] = YamlValue(test_outputs, '--test-x')
        if cast(object, args.test) is None:
            mode:Literal['run', 'test', 'cfg'] = 'run'
        else:
            mode = 'test'
            try:
                taa = cast(str, args.test).split('/')
                assert len(taa) >= 3
                tbl = taa[0]
                addr = '/'.join(taa[1:-1])
                auth_params = taa[-1].split(',')
            except Exception:
                print("--test expects TABLE/ADDRESS/AUTH[,timeout=FLOAT][,retries=INT]", file=sys.stderr)
                sys.exit(1)
            schedcfg:dict[str, object] = {
                'devices':  {"CMDLINE":addr},
                'auth':     auth_params[0],
                'schedule': {tbl:"00:01+00:00"},
            }
            try:
                for param in auth_params[1:]:
                    paramkey, paramvalue = param.split('=')
                    assert paramkey in ('timeout', 'retries',)
                    schedcfg[f"snmp_{paramkey}"] = paramvalue
            except Exception:
                print("--test invalid snmp option, supported are only timeout=FLOAT or retries=INT")
            ret['schedule_path'] = YamlValue( [schedcfg], '--test')
        for attr in {'inputs', 'tables', 'auths', 'schedule', 'outputs'}:
            out_attr = attr[:-1] if attr[-1] == 's' else attr
            out_attr += '_path'
            if out_attr in ret: continue
            path = cast(str, getattr(args, attr))
            if path[:1] in ('{','['):
                ret[out_attr] = YamlValue(cast(object, json.loads(path)), f'--{{ attr }}')
            elif path[:2] == '@/':
                ret[out_attr] = os.path.join(cast(str, args.cfg_dir), path[2:].lstrip('/'))
            else:
                ret[out_attr] = path
        if mode == 'run' and cast(str,args.schedule) == '[]':
            mode = 'cfg'
        return ret, mode, cast(int, args.test_run)
    # }}}

class Main:
    def __init__(self) -> None:
        args, mode, limit = Args().parse()
        self.logger = logging.getLogger('Main')
        self.executor = Executor()
        ok, msg = self.executor.reload(**args)
        if not ok:
            print(f"Bad configuration: {msg}", file=sys.stderr)
            sys.exit(1)
        asyncio.run(getattr(self, mode)(limit)) # type: ignore

    async def run(self, limit:float) -> None:
        if not limit:
            sdn = SystemdNotifier()
            await sdn.notify(status="Starting...")
        executor = self.executor
        run = asyncio.create_task(executor.run())
        if limit > 0:
            self.logger.info(f"running for {limit} seconds")
            await asyncio.sleep(limit)
            self.logger.info(f"finished")
            return #FIXME
        else:
            await sdn.notify(status="Running", ready=1)
            self.logger.info('notified systemd: READY')
        await run

    async def test(self, limit:float) -> None:
        executor = self.executor
        executor.commit_reload()
        tbl = executor.schedule.table
        assert len(tbl) == 1
        assert len(tbl[0]) == 1
        assert set(tbl.keys()) == {0}
        scraper = executor.get_scraper(tbl[0][0])
        await scraper.run()
        executor.reload(schedule_path=YamlValue([],"stop"),output_path=YamlValue({},"stop"))
        executor.commit_reload()
        await asyncio.wait([ro.task for ro in executor.output_runners])

    async def cfg(self, limit:float) -> None:
        pass

Main()
