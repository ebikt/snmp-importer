#!/usr/bin/python3

import asyncio, argparse, os, sys, json
from typing import cast, Annotated, Literal

from snmp_importer.config      import YamlValue
from snmp_importer.executor    import Executor
try:
    from aio_exporter.aio_sdnotify import SystemdNotifier
except ImportError:
    MYPY=False
    if not MYPY:
        class SystemdNotifier:
            async def notify(self, **statekw:bytes|str|int) -> bool:
                return False

BASE_DIR = os.path.dirname(__file__)

class ArgMeta: # {{{
    __metadata__: tuple[str, dict[str, object]]

class Deleted: pass
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
    test:        Annotated[str|None, '-T', dict(default=None, metavar='TABLE/ADDRESS/AUTH',
                help='run single scrape of table set TABLE on address ADDRESS using auth AUTH')]
    test_influx: Annotated[bool,     '-I', dict(default=False, action='store_true',
                help='Use colored stdout influx writer as output.')]
    test_prom:   Annotated[bool,     '-P', dict(default=False, action='store_true',
                help='Use colored stdout promfile writer as output.')]
    test_run:    Annotated[int,      '-R', dict(default=0, metavar='SECONDS',type=int,
                help='Exit after SECONDS (for testing of scheduler)')]
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
        ret:dict[str, YamlValue|str] = {}
        args = self.parser.parse_args()
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
                auth = taa[-1]
            except Exception:
                print("--test expects TABLE/ADDRESS/AUTH", file=sys.stderr)
                sys.exit(1)
            ret['schedule_path'] = YamlValue( [{
                'devices':  {"CMDLINE":addr},
                'auth':     auth,
                'schedule': {tbl:"00:01+00:00"},
            }], '--test')
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
            await asyncio.sleep(limit)
            return #FIXME
        else:
            await sdn.notify(status="Running", ready=1)
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
