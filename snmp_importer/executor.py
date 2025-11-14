import sys, os

from .config   import loadFile, YamlValue, ConfigError
from .walker   import Auth, Walker, Target
from .inputs   import Inputs
from .output   import Renderer, ScrapedTables, ScrapedDevice
from .metrics  import Devices, Device
from .schedule import ScrapeJob, ScrapeSchedule

from typing import TypeVar, Callable, Iterator, cast, Generic, Awaitable, TYPE_CHECKING
import asyncio
import queue
import time
import traceback
import yaml
if TYPE_CHECKING:
    from typing import Self

import logging
tasklogger = logging.getLogger('task')

# {{{ NonParallelTaskExecutor
class TaskDefinition:
    def update(self, older:'Self') -> None:
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
        tasklogger.debug(f"new {self} [{description}]")
        self.description = description
        self.event       = asyncio.Event()
        self.terminate   = False
        self.running     = False
        self.counter_ok  = 0
        self.counter_err = 0
        self.enqueue(newtask)

    def enqueue(self, newtask:T) -> None:
        if hasattr(self, 'nexttask'):
            tasklogger.warning(f"{self} [{self.description}] - replacing next task")
            newtask.update(self.nexttask)
        else:
            tasklogger.debug(f"{self} [{self.description}] - setting next task")
        self.terminate = False
        self.nexttask = newtask
        self.event.set()

    async def loop(self) -> None:
        tasklogger.debug(f"{self} loop start")
        try:
            self.running = True
            while not self.terminate:
                tasklogger.debug(f"{self} loop await event")
                await self.event.wait()
                tasklogger.debug(f"{self} loop event triggered")
                if self.terminate: break
                task = self.nexttask
                del self.nexttask
                self.terminate = True
                try:
                    tasklogger.debug(f"{self} run: {task}")
                    ok = await task.run()
                except Exception as e:
                    ok = False
                    tasklogger.error(f"{self} got exception {type(e).__name__}: {e!r}", exc_info=True)
                tasklogger.debug(f"{self} finished: {task}, got: {ok}")
                if ok:
                    self.counter_ok += 1
                else:
                    self.counter_err += 1
            tasklogger.debug(f"{self} loop finished")
        except Exception as e:
            tasklogger.critical(f"{self} loop exception {e}", exc_info=True)
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
        tasklogger.debug(f"{self} New nonparalleltask executor")
        self.tasks:dict[tuple[str,...], NonParallelTask[T]] = {}
        self.loops:dict[tuple[str,...], asyncio.Task[None]] = {}
        self.stopping = False

    async def _task_loop(self, id:tuple[str,...]) -> None:
        tasklogger.debug(f"{self} {id} loop start")
        while not self.tasks[id].terminate:
            tasklogger.debug(f"{self} {id} loop")
            await self.tasks[id].loop()
        tasklogger.debug(f"{self} {id} loop end")
        self.tasks.pop(id)
        self.loops.pop(id) #FIXME is this correct? We do not wait() for this.

    def add(self, id:tuple[str,...], newtask:T) -> None:
        tasklogger.debug(f"{self} add {id}")
        assert not self.stopping
        if id not in self.tasks:
            self.tasks[id] = NonParallelTask(":".join(id), newtask)
        else:
            self.tasks[id].enqueue(newtask)
        if id not in self.loops:
            tasklogger.debug(f"{self} {id} spawn loop")
            self.loops[id] = asyncio.create_task(self._task_loop(id))

    def remove(self, id:tuple[str, ...]) -> None:
        tasklogger.debug(f"{self} remove {id}")
        self.tasks[id].stop()

    def remove_not_listed(self, ids:set[tuple[str,...]]) -> None:
        tasklogger.debug(f"{self} remove excluding {ids}")
        for id in self.tasks.keys():
            if id not in ids:
                self.remove(id)

    async def stop(self) -> None:
        tasklogger.debug("f{self} stopping")
        self.stopping = True
        for t in self.tasks.values():
            t.stop()
        while len(self.loops):
            asyncio.gather(*self.loops.values())
            #FIXME cancel on force termination
        tasklogger.debug("f{self} stopped")
# }}}

# {{{ non-parallel tasks
class SingleTargetScraper(TaskDefinition):
    def __init__(self, job: ScrapeJob,
                       inputs:Inputs, outputs:list[asyncio.Queue[ScrapedTables]]):
        self.job = job
        self.inputs = inputs
        self.outputs = outputs

    def update(self, older:'Self') -> None:
        #Just forget old configuration
        pass

    async def run(self) -> bool:
        tasklogger.debug(f"{self} run start")
        target = Target(self.job.auth, self.job.path[0], path = self.job.path, timeout = self.job.snmp_timeout, retries = self.job.snmp_retries)
        try:
            walker = Walker(
                target = target,
                bulkEnabled    = False,
                walkBulkWidth  = 1,
                getBulkLength  = 25,
                walkBulkLength = 25,
            )
            device = ScrapedDevice(self.job.host_id, walker, self.job.device, self.inputs)
            start = time.time()
            tasklogger.debug(f"{self} scrape")
            tables = await device.scrape()
            tasklogger.debug(f"{self} scraped")
            tables.set_when(start)
            for que in self.outputs:
                try:
                    que.put_nowait(tables)
                    tasklogger.debug(f"{self} scrape result queued")
                except Exception:
                    tasklogger.critical("Internal error: cannot append into queue {que}")
        finally:
            try:
                walker.close()
            except Exception:
                pass
        return True
# }}}

OUTPUT_TYPES:'dict[str, type[Renderer]|tuple[str,str]]' = {
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

configlogger = logging.getLogger('config')
# {{{ Configuration parsing
class Context:
    def __init__(self, cfg:dict[str,YamlValue]) -> None:
        self.cfg = cfg.copy()

    def load_auths(self, cfg:YamlValue) -> None:
        configlogger.debug(f"{self} load_auths: {yaml.dump(cfg.data)}")
        ret = {}
        for name, cfgv in cfg.iterStrMap(keyconstraint='identifier'):
            ret[name] = Auth(cfgv)
        self.auth = ret

    def load_inputs(self, cfg:YamlValue) -> None:
        configlogger.debug(f"{self} load_inputs: {yaml.dump(cfg.data)}")
        self.inputs = Inputs(cfg)

    def load_devices(self, cfg:YamlValue) -> None:
        configlogger.debug(f"{self} load_devices: {yaml.dump(cfg.data)}")
        self.devices = Devices(cfg, self.inputs)

    def load_schedule(self, cfg:YamlValue) -> None:
        configlogger.debug(f"{self} load_schedule: {yaml.dump(cfg.data)}")
        self.schedule = ScrapeSchedule(cfg, self.devices, self.auth)

    def load_outputs(self, cfg:YamlValue) -> None:
        configlogger.debug(f"{self} load_outputs: {yaml.dump(cfg.data)}")
        ret = {}
        for name, cfgv in cfg.iterStrMap(keyconstraint='identifier'):
            ret[name] = getRenderer(cfgv)
        self.outputs = ret

    def load(self, name:str, configuration:'str|YamlValue|None') -> tuple[bool, str]:
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

outputlogger = logging.getLogger('output')
class OutputRunner:
    queue:'asyncio.Queue[ScrapedTables|StopMessage]'

    def __init__(self, name: str, output:Renderer, parent:'Executor') -> None:
        outputlogger.debug(f"{self} new ({name}, {output}, {parent})")
        self.name   = name
        self.output = output
        self.parent = parent
        self.queue  = asyncio.Queue()
        self.counter_ok  = 0
        self.counter_err = 0

    async def run(self) -> None:
        async with self.output as sender: # FIXME recreate session on error?
            while True:
                outputlogger.debug(f"{self}: wait for output")
                sts = await self.queue.get()
                if isinstance(sts, StopMessage):
                    outputlogger.debug(f"{self}: StopMessage")
                    self.parent.output_finished(self)
                    return
                try:
                    outputlogger.debug(f"{self}: got {sts}, sending...")
                    await sender.render_and_send(sts)
                    outputlogger.debug(f"{self}: sent")
                    self.counter_ok += 1
                except Exception as e:
                    outputlogger.error(f"{self} Cannot send output {sts}: {e}", exc_info=True)
                    self.counter_err += 1

    def spawn(self) -> None:
        outputlogger.info(f"{self} spawn")
        self.task = asyncio.create_task(self.run()) # FIXME not awaited?

executorlogger = logging.getLogger("executor")
class Executor:
    def __init__(self) -> None:
        executorlogger.debug(f"{self}: new")
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
        auth_path:'str|YamlValue|None' = None,
        input_path:'str|YamlValue|None' = None,
        table_path:'str|YamlValue|None' = None,
        schedule_path:'str|YamlValue|None' = None,
        output_path:'str|YamlValue|None' = None,
    ) -> tuple[bool,str]:
        executorlogger.debug(f"{self}: reload")
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
            if not res:
                executorlogger.debug(f"{self}: {name}:{path} error: {err}")
                return res, err

        if hasattr(self, 'next_context'):
            executorlogger.debug(f"{self}: triggered reload event")
            self.reload_event.set()
        self.next_context = ctx
        executorlogger.debug(f"{self}: reload - configuration parsed successfully")
        return (True, "Configuration was successfully parsed.")

    def commit_reload(self) -> None:
        executorlogger.debug(f"{self}: commit reload")
        self.inputs   = self.next_context.inputs
        self.schedule = self.next_context.schedule
        for k, q in self.outputs.items():
            executorlogger.debug(f"{self}: stopping output {k} ({q})")
            q.put_nowait(StopMessage())

        self.outputs = {}
        for k, o in self.next_context.outputs.items():
            executorlogger.debug(f"{self}: spawning output runner {k} ({o})")
            ro = OutputRunner(k, o, self)
            self.outputs[k] = ro.queue
            self.output_runners.add(ro)
            ro.spawn()
        executorlogger.debug(f"{self}: commit reload done")

    def output_finished(self, ro:OutputRunner) -> None:
        executorlogger.debug(f"{self}: output finished for {ro} -> discarding runner")
        self.output_runners.discard(ro)

    sched_task:asyncio.Task[float]

    async def run(self) -> None:
        executorlogger.debug(f"{self}: run")
        while not self.scrape_executor.stopping:
            executorlogger.debug(f"{self}: wait for reload")
            await self.reload_event.wait()
            if self.scrape_executor.stopping: break
            executorlogger.debug(f"{self}: clear reload event")
            self.reload_event.clear()
            executorlogger.debug(f"{self}: stop schedule")
            self.schedule.stop()
            if hasattr(self, 'sched_task'):
                executorlogger.debug(f"{self}: await schedule")
                now = await self.sched_task
                executorlogger.debug(f"{self}: schedule done, last event: {now}")
            else:
                now = time.time()
                executorlogger.debug(f"{self}: last event set to current time: {now}")
            self.commit_reload()
            executorlogger.debug(f"{self}: spawning scheduler task")
            self.sched_task = asyncio.create_task(self.schedule.run(now, self.spawn_scrape))
        executorlogger.debug(f"{self}: run end: stopping schedule")
        self.schedule.stop()
        if hasattr(self, 'sched_task'):
            executorlogger.debug(f"{self}: run end: awaiting scheduler task")
            await self.sched_task
            executorlogger.debug(f"{self}: run end: scheduler task done, deleting")
            del self.sched_task
        executorlogger.debug(f"{self}: run end: awaitng scrape executor")
        await self.scrape_executor.stop() #FIXME
        executorlogger.debug(f"{self}: run end: scrape executor done")

    def get_scraper(self, job:ScrapeJob) -> SingleTargetScraper:
        assert len(self.outputs) > 0
        return SingleTargetScraper(job, self.inputs, list(cast(dict[str,asyncio.Queue[ScrapedTables]],self.outputs).values())) # covariance versus contravariance

    def spawn_scrape(self, jobs:list[ScrapeJob]) -> None:
        for job in jobs:
            if len(self.outputs) > 0:
                sts = self.get_scraper(job)
                executorlogger.debug(f"{self}: spawn scrape ({sts.job.job_id}, {sts})")
                self.scrape_executor.add(sts.job.job_id, sts)
            else:
                executorlogger.debug(f"{self}: {job}: Nothing to scrape, no outputs defined")
                pass #FIXME log no-outputs
