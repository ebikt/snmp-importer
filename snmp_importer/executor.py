import sys, os

from .config   import loadFile, YamlValue, ConfigError
from .walker   import Auth, Walker, Target
from .inputs   import Inputs
from .output   import Renderer, ScrapedTables, ScrapedDevice
from .metrics  import Devices, Device
from .schedule import ScrapeJob, ScrapeSchedule

from typing import TypeVar, Callable, Iterator, cast, Self, Generic, Awaitable
import asyncio
import queue
import time
import traceback

# {{{ NonParallelTaskExecutor
class TaskDefinition:
    def update(self, older:Self) -> None:
        raise NotImplementedError()

    async def run(self) -> bool:
        raise NotImplementedError()

T=TypeVar('T', bound=TaskDefinition)
class NonParallelTask(Generic[T]):
    nexttask:T
    """ Holder of current and possible next execution of a task.
        Only single next execution is remembered (the last one),
        if there was more requests to execute (via method add()),
        before current run was finished, then all but the last one
        are intentionally ignored. """

    def __init__(self, description:str, newtask:T) -> None:
        self.description = description
        self.event       = asyncio.Event()
        self.terminate   = False
        self.running     = False
        self.counter_ok  = 0
        self.counter_err = 0
        self.enqueue(newtask)

    def enqueue(self, newtask:T) -> None:
        if hasattr(self, 'nexttask'):
            newtask.update(self.nexttask)
        self.terminate = False
        self.nexttask = newtask
        self.event.set()

    async def loop(self) -> None:
        try:
            self.running = True
            while not self.terminate:
                await self.event.wait()
                if self.terminate: break
                task = self.nexttask
                del self.nexttask
                self.terminate = True
                try:
                    ok = await task.run()
                except Exception as e:
                    ok = False
                    print(f"Error {type(e).__name__} when running {self.description}: {e!r}", file=sys.stderr)
                if ok:
                    self.counter_ok += 1
                else:
                    self.counter_err += 1
        except Exception as e:
            traceback.print_exc()
            raise
        finally:
            self.running = False

    def stop(self) -> None:
        self.terminate = True
        self.event.set()

class NonParallelTaskExecutor(Generic[T]):
    """ Container of multiple NonParallelTask objects, indexed by unique
        key of type tuple[str, ...] """
    def __init__(self) -> None:
        self.tasks:dict[tuple[str,...], NonParallelTask[T]] = {}
        self.loops:dict[tuple[str,...], asyncio.Task[None]] = {}
        self.stopping = False

    async def _task_loop(self, id:tuple[str,...]) -> None:
        while not self.tasks[id].terminate:
            await self.tasks[id].loop()
        self.tasks.pop(id)
        self.loops.pop(id)

    def add(self, id:tuple[str,...], newtask:T) -> None:
        assert not self.stopping
        if id not in self.tasks:
            self.tasks[id] = NonParallelTask(":".join(id), newtask)
        else:
            self.tasks[id].enqueue(newtask)
        if id not in self.loops:
            self.loops[id] = asyncio.create_task(self._task_loop(id))

    def remove(self, id:tuple[str, ...]) -> None:
        self.tasks[id].stop()

    def remove_not_listed(self, ids:set[tuple[str,...]]) -> None:
        for id in self.tasks.keys():
            if id not in ids:
                self.remove(id)

    async def stop(self) -> None:
        self.stopping = True
        for t in self.tasks.values():
            t.stop()
        while len(self.loops):
            asyncio.gather(*self.loops.values())
            #FIXME cancel on force termination
# }}}

# {{{ non-parallel tasks
class SingleTargetScraper(TaskDefinition):
    def __init__(self, job: ScrapeJob,
                       inputs:Inputs, outputs:list[asyncio.Queue[ScrapedTables]]):
        self.job = job
        self.inputs = inputs
        self.outputs = outputs

    def update(self, older:Self) -> None:
        #Just forget old configuration
        pass

    async def run(self) -> bool:
        walker = Walker(
            Target(self.job.auth, self.job.path[0], path = self.job.path),
            bulkEnabled    = False,
            walkBulkWidth  = 1,
            getBulkLength  = 25,
            walkBulkLength = 25,
        )
        device = ScrapedDevice(self.job.host_id, walker, self.job.device, self.inputs)
        start = time.time()
        tables = await device.scrape()
        tables.set_when(start)
        for que in self.outputs:
            try:
                que.put_nowait(tables)
            except Exception:
                sys.stderr.write(f"Internal error: cannot append into queue {que}\n")
        return True
# }}}

OUTPUT_TYPES:dict[str, type[Renderer]|tuple[str,str]] = {
    "influx":(".influxdb","InfluxDB"),
    "promtext":(".promtext","PrometheusText"),
    "prombuf":(".prombuf", "PrometheusV1ProtoBuf"),
}

def getRenderer(cfg:YamlValue) -> Renderer: # {{{ # On demand import according to OUTPUT_TYPES
    t = cfg.peekDict('type')
    ts = t.asStr()
    try:
        tt = OUTPUT_TYPES[ts]
    except KeyError:
        t.raiseExc(f"Output type must be one of {', '.join(OUTPUT_TYPES.keys())}")
    if isinstance(tt, tuple):
        v:dict[str,object] = {'__name__':__name__}
        exec(f"from {tt[0]} import {tt[1]}", v)
        tt = cast(type[Renderer],v[tt[1]])
        OUTPUT_TYPES[ts] = tt # cache imported value
    del cfg.data['type'] # type: ignore
    ret = tt(cfg) # type: ignore[arg-type]
    cfg.data['type'] = ts # type: ignore
    return ret
# }}}

# {{{ Configuration parsing
class Context:
    def __init__(self, cfg:dict[str,YamlValue]) -> None:
        self.cfg = cfg.copy()

    def load_auths(self, cfg:YamlValue) -> None:
        ret = {}
        for name, cfgv in cfg.iterStrMap(keyconstraint='identifier'):
            ret[name] = Auth(cfgv)
        self.auth = ret

    def load_inputs(self, cfg:YamlValue) -> None:
        self.inputs = Inputs(cfg)

    def load_devices(self, cfg:YamlValue) -> None:
        self.devices = Devices(cfg, self.inputs)

    def load_schedule(self, cfg:YamlValue) -> None:
        self.schedule = ScrapeSchedule(cfg, self.devices, self.auth)

    def load_outputs(self, cfg:YamlValue) -> None:
        ret = {}
        for name, cfgv in cfg.iterStrMap(keyconstraint='identifier'):
            ret[name] = getRenderer(cfgv)
        self.outputs = ret

    def load(self, name:str, configuration:str|YamlValue|None) -> tuple[bool, str]:
        phase = "parse " + {
            "auths": "credentials",
            "inputs": "inputs",
            "devices": "table definitions",
            "schedule": "schedule",
            "outputs": "output definitions",
        }[name]
        cfg = self.cfg[name]
        handler = cast(Callable[[YamlValue],None], getattr(self, f"load_{name}"))
        if isinstance(configuration, str):
            try:
                cfg = loadFile(configuration)
            except ConfigError as e:
                return False, f"{phase} - {type(e).__name__}: {e}"
            except Exception as e:
                return False, f"{phase} ({configuration}) - {type(e).__name__}: {e!r}"
        elif isinstance(configuration, YamlValue):
            cfg = configuration
        else:
            assert configuration is None, f"{configuration!r}"
        try:
            handler(cfg)
            return True, f"{phase} - OK"
        except ConfigError as e:
            return False, f"{phase} - {type(e).__name__}: {e}"
# }}}

class StopMessage: pass


class OutputRunner:
    queue:asyncio.Queue[ScrapedTables|StopMessage]

    def __init__(self, name: str, output:Renderer, parent:'Executor') -> None:
        self.name   = name
        self.output = output
        self.parent = parent
        self.queue  = asyncio.Queue()
        self.counter_ok  = 0
        self.counter_err = 0

    async def run(self) -> None:
        async with self.output as sender: # FIXME recreate session on error?
            while True:
                sts = await self.queue.get()
                if isinstance(sts, StopMessage):
                    self.parent.output_finished(self)
                    return
                try:
                    await sender.render_and_send(sts)
                    self.counter_ok += 1
                except Exception as e:
                    traceback.print_exc()
                    self.counter_err += 1

    def spawn(self) -> None:
        self.task = asyncio.create_task(self.run())

class Executor:
    def __init__(self) -> None:
        self.cfg = {
            'inputs':   YamlValue({"alias":{"zero":"0"},"input":{"zero":{"zero":"zero"}}}, "embedded"),
            'devices':  YamlValue({"zero":{}}, "embedded"),
            'auths':    YamlValue({"public":{}}, "embedded"),
            'schedule': YamlValue([], "embedded"),
            'outputs':  YamlValue({}, "embedded"),
        }
        self.reload_event = asyncio.Event()
        res, err = self.reload(None, None, None, None, None)
        self.schedule = self.next_context.schedule
        assert res, err
        self.last:float|None = None

        self.scrape_executor = NonParallelTaskExecutor[SingleTargetScraper]()
        self.outputs:dict[str,asyncio.Queue[ScrapedTables|StopMessage]] = {}
        self.output_runners:set[OutputRunner] = set()

    def reload(self,
        auth_path:str|YamlValue|None = None,
        input_path:str|YamlValue|None = None,
        table_path:str|YamlValue|None = None,
        schedule_path:str|YamlValue|None = None,
        output_path:str|YamlValue|None = None,
    ) -> tuple[bool,str]:
        # FIXME: disable reload when stopping
        ctx = Context(self.cfg)
        for name, path in [
            ('inputs', input_path),
            ('devices', table_path),
            ('auths', auth_path),
            ('schedule', schedule_path),
            ('outputs', output_path),
        ]:
            res, err = ctx.load(name, path)
            if not res: return res, err

        if hasattr(self, 'next_context'):
            self.reload_event.set()
        self.next_context = ctx
        return (True, "Configuration was successfully parsed.")

    def commit_reload(self) -> None:
        self.inputs   = self.next_context.inputs
        self.schedule = self.next_context.schedule
        for k, q in self.outputs.items():
            q.put_nowait(StopMessage())

        self.outputs = {}
        for k, o in self.next_context.outputs.items():
            ro = OutputRunner(k, o, self)
            self.outputs[k] = ro.queue
            self.output_runners.add(ro)
            ro.spawn()

    def output_finished(self, ro:OutputRunner) -> None:
        self.output_runners.discard(ro)

    sched_task:asyncio.Task[float]

    async def run(self) -> None:
        while not self.scrape_executor.stopping:
            await self.reload_event.wait()
            if self.scrape_executor.stopping: break
            self.reload_event.clear()
            self.schedule.stop()
            if hasattr(self, 'sched_task'):
                now = await self.sched_task
            else:
                now = time.time()
            self.commit_reload()
            self.sched_task = asyncio.create_task(self.schedule.run(now, self.spawn_scrape))
        self.schedule.stop()
        if hasattr(self, 'sched_task'):
            await self.sched_task
            del self.sched_task
        await self.scrape_executor.stop() #FIXME

    def get_scraper(self, job:ScrapeJob) -> SingleTargetScraper:
        assert len(self.outputs) > 0
        return SingleTargetScraper(job, self.inputs, list(cast(dict[str,asyncio.Queue[ScrapedTables]],self.outputs).values())) # covariance versus contravariance

    def spawn_scrape(self, jobs:list[ScrapeJob]) -> None:
        for job in jobs:
            if len(self.outputs) > 0:
                sts = self.get_scraper(job)
                self.scrape_executor.add(sts.job.job_id, sts)
            else:
                pass #FIXME log no-outputs
