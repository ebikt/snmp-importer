from .config  import YamlValue
from .walker  import Auth
from .metrics import Devices, Device

import re
import asyncio
import math
import time
import logging

from bisect import bisect
from typing import TypeVar, Generic, Callable

schedlogger = logging.getLogger('scheduler')
# {{{ Generic schedule

def parse_hms(timestr:str, excfactory:YamlValue) -> int:
    hms_str = timestr.split(':')
    if len(hms_str) > 3 or sum([ not x.strip().isnumeric() for x in hms_str ]) > 0:
        return -1
    hms = [ int(x.strip()) for x in hms_str ]
    val = hms[-1]
    if len(hms) > 1:
        if val > 59: excfactory.raiseExc(f"Too large secons value: {hms[-1]}")
        val += hms[-2] * 60
    if len(hms) > 2:
        if val > 3599: excfactory.raiseExc("Too large minutes value: {hms[-2]}")
        val += hms[-3]
    return val

class SchedPrimitive:
    def __init__(self, timecfg:YamlValue) -> None:
        times = timecfg.asStr().split('+')
        if len(times) != 2:
            period = -1
            offset = -1
        else:
            period = parse_hms(times[0], timecfg)
            offset = parse_hms(times[1], timecfg)

        if period <= 0 or offset < 0:
            timecfg.raiseExc("(HH:)MM:SS+(HH:)MM:SS format for period and offset of schedule must be used")
        self.period = period
        self.offset = offset

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>(offset={self.offset}, period={self.period})"

T=TypeVar('T')
class SchedTable(Generic[T]):
    def __init__(self, _:type[T]) -> None:
        schedlogger.log(19, f"{self} new")
        self.table: dict[int,list[T]] = {}
        self.period = 1
        self.stop_event = asyncio.Event()
        self.stop_event.clear()

    def add(self, when:SchedPrimitive, *what:T) -> None:
        schedlogger.info(f"{self} add {when}, {what}")
        if self.period % when.period:
            period = when.period * self.period // math.gcd(when.period, self.period)
            assert period % when.period == 0 and period % self.period == 0
            for key in list(self.table.keys()):
                for p in range(1, period // self.period):
                    self.table[key + p*self.period] = self.table[key]
            self.period = period
        for p in range(self.period // when.period):
            self.table.setdefault(p*self.period + when.offset,[]).extend(what)

    def stop(self) -> None:
        self.stop_time = time.time()
        self.stop_event.set()

    async def run(self, now:float, callback:Callable[[list[T]],None]) -> float:
        """
            Calls callback([job, job, job, ...]) for all jobs that should be triggered at that time.
        """
        schedlogger.log(21, f"{self} run, lastevent: {now}, period: {self.period}")
        schedule = sorted(self.table.items())
        period = self.period
        now_start, now_offset = divmod(now, period)
        now_start *= period
        pos    = bisect(schedule, (now_offset,))
        next_t = now
        for event_offset, event_data in schedule:
            schedlogger.log(21, f"{self}    table: offset:{event_offset:5d} tasks:{event_data}")
        while not self.stop_event.is_set():
            if pos >= len(schedule):
                pos = 0
                now_start += period
            event_offset, event_data = schedule[pos]
            next_t = now_start + event_offset
            now = time.time()
            schedlogger.log(21, f"{self} pos: {pos}, next_t: {next_t}, wait for {next_t - now}")
            if now < next_t:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), next_t - now)
                except TimeoutError:
                    pass
                else:
                    break
            schedlogger.log(21, f"{self} callback {event_data}")
            callback(event_data.copy())
            pos += 1
        if next_t < self.stop_time:
            schedlogger.log(21, f"{self} return A: {next_t}")
            return next_t
        elif now <= self.stop_time:
            schedlogger.log(21, f"{self} return B: {self.stop_time}")
            return self.stop_time
        else:
            schedlogger.log(21, f"{self} return C: {now + 0.0001}")
            return now + 0.0001

# }}}

# {{{ Scraping schedule

class ScrapeJob:
    def __init__(self, host_id:str, definition_id:str, auth_id:str, path:list[str], device:Device, auth:Auth) -> None:
        self.host_id = host_id
        self.job_id    = (host_id, definition_id, auth_id)
        self.device    = device
        self.auth      = auth
        self.path      = path

class ScrapeSchedule(SchedTable[ScrapeJob]):
    def __init__(self, cfg:YamlValue, devices: Devices, auths:dict[str, Auth]) -> None:
        self.job_ids:set[tuple[str,str,str]] = set()
        super().__init__(ScrapeJob)
        hostlabelre = re.compile(r'^\s*[A-Za-z][-_a-zA-Z0-9.]*\s*$')
        for itemcfg in cfg.iterList():
            with itemcfg.asStruct() as s:
                authname = s['auth'].asStr()
                if authname not in auths:
                    s['auth'].raiseExc(f"Unknown credential {authname}")
                else:
                    auth = auths[authname]
                req_indices = auth.path_indices
                static_paths = {}
                for hostname, hostpath in s['devices'].iterStrMap(allow_missing=True):
                    if not hostlabelre.match(hostname): hostpath.raiseExc(f"Invalid hostname label {hostname}")
                    path = hostpath.asStr().split('/')
                    path_indices = { i for i,_ in enumerate(path) }
                    if not (req_indices <= path_indices):
                        hostpath.raiseExc(f"Auth {authname} requires following path indices: {list(sorted(req_indices - path_indices))}")
                    static_paths[hostname] = path
                if len(static_paths) == 0:
                    continue

                for definition, timelinecfg in s['schedule'].iterStrMap(allow_missing=True):
                    if definition not in devices.devices:
                        timelinecfg.raiseExc(f"Unknown device definition {definition}")
                    jobs = [
                        ScrapeJob(hostname, definition, authname,
                                  path, devices.devices[definition], auth)
                        for hostname, path in static_paths.items() ]
                    self.add(SchedPrimitive(timelinecfg), *jobs)
                    self.job_ids |= set( job.job_id for job in jobs )
# }}}
