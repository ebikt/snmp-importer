#!/usr/bin/python3
import sys, asyncio
MYPY = False

# TODO: this will be test runner. For now it is basic snmp agent that responds to 1.3.6.1.2.1.1

if MYPY:
    class Dispatcher: pass
    async def start_snmp(port:int = 6161) -> Dispatcher: ...
    async def stop_snmp(d:asyncio.Task[Dispatcher]) -> None: ...
else:
    async def start_snmp(port:int = 6161): # {{{
        from pysnmp.entity import engine, config
        from pysnmp.entity.rfc3413 import cmdrsp, context
        from pysnmp.carrier.asyncio.dgram import udp
        from pysnmp.proto.api import v2c

        # Create SNMP engine
        snmpEngine = engine.SnmpEngine()

        # Transport setup

        # UDP over IPv4
        config.addTransport(
            snmpEngine, udp.DOMAIN_NAME, udp.UdpTransport().openServerMode(("127.0.0.1", port))
        )

        # SNMPv2c setup

        # SecurityName <-> CommunityName mapping.
        config.addV1System(snmpEngine, "my-area", "test")

        # Allow read MIB access for this user / securityModels at VACM
        config.addVacmUser(snmpEngine, 2, "my-area", "noAuthNoPriv", (1,))

        # Create an SNMP context
        snmpContext = context.SnmpContext(snmpEngine)

        # --- create custom Managed Object Instance ---

        mibInstrum = snmpContext.getMibInstrum()
        mibBuilder = mibInstrum.getMibBuilder()

        MibScalar, MibScalarInstance = mibBuilder.importSymbols(
            "SNMPv2-SMI", "MibScalar", "MibScalarInstance"
        )

        class Row(MibScalarInstance):
            _prefix = tuple()
            def __init__(self, value, *args, **kwargs):
                self._value = value
                super().__init__(*args, **kwargs)

            def getValue(self, name, idx, snmpEngine=None, acFun=None, cbCtx=None, oName=None):
                return self.getSyntax().clone(self._value)

            @classmethod
            def r(cls, name, type, values):
                if isinstance(name, int):
                    name = (name,)
                if isinstance(values, list):
                    values = {(i+1,): v for i, v in enumerate(values)}
                elif not isinstance(values, dict):
                    values = {(0,):values}
                name = cls._prefix + name
                return (
                    MibScalar(name, type),
                ) + tuple(
                    cls(value, name, idx, type) for idx, value in values.items()
                )

        mib2 = (1,3,6,1,2,1)
        enterprises = (1,3,6,1,4,1)
        class InfoS(Row): _prefix = mib2 + (1,)
        class If(Row):    _prefix = mib2 + (2,2,1)
        class IfX(Row):   _prefix = mib2 + (31,1,1,1)
        class Bcsi(Row):  _prefix = enterprises + (1588,3,1,8,1,2,1)

        I = v2c.Integer32
        H = v2c.Integer
        S = v2c.OctetString

        mibBuilder.exportSymbols(
            "__MY_MIB",
            *InfoS.r(3, v2c.TimeTicks(),   123456),
            *InfoS.r(5, S(), "TEST"),
            *If.r(2, S(), ["desc1", "desc2"]),
            *If.r(3, I(), [161, 161]), #type
            *If.r(6, S(), ["\x11\x22\x33\x44\x55\x66", "\x61\x52\x43\x34\x25\x16"]), #mac
            *If.r(7, I(), [1,1]), #admin status
            *If.r(8, I(), [1,1]), #oper status
            *If.r(13, I(), [13001, 13002]),
            *If.r(14, I(), [14001, 14002]),
            *If.r(19, I(), [19001, 19002]),
            *If.r(20, I(), [20001, 20002]),
            *IfX.r(1, S(), ["port1", "port2"]),
            *IfX.r(6, H(), [106001,106002]),
            *IfX.r(7, H(), [107001,107002]),
            *IfX.r(8, H(), [108001,108002]),
            *IfX.r(9, H(), [109001,109002]),
            *IfX.r(10, H(), [110001,110002]),
            *IfX.r(11, H(), [111001,111002]),
            *IfX.r(12, H(), [112001,112002]),
            *IfX.r(13, H(), [113001,113002]),

            *IfX.r(15, I(), [1000,1000]),
            *IfX.r(18,S(), ["alias1", "alias2"]),

            *Bcsi.r(1, S(), [       "35 C Normal", "Not Supported" ]),
            *Bcsi.r(2, I(), [                  5 ,              1  ]),
            *Bcsi.r(3, S(), [ "-2.128 dBm Normal", "Not Supported" ]),
            *Bcsi.r(4, I(), [                613 ,              0  ]),
            *Bcsi.r(5, I(), [                  5 ,              1  ]),
            *Bcsi.r(6, S(), [ "-2.317 dBm Normal", "Not Supported" ]),
            *Bcsi.r(7, I(), [                587 ,              0  ]),
            *Bcsi.r(8, S(), ["5.502 mAmps Normal", "Not Supported" ]),
        )

        # --- end of Managed Object Instance initialization ----

        # Register SNMP Applications at the SNMP engine for particular SNMP context
        #cmdrsp.SetCommandResponder(snmpEngine, snmpContext)
        cmdrsp.GetCommandResponder(snmpEngine, snmpContext)
        cmdrsp.NextCommandResponder(snmpEngine, snmpContext)
        cmdrsp.BulkCommandResponder(snmpEngine, snmpContext)

        # Register an imaginary never-ending job to keep I/O dispatcher running forever
        snmpEngine.transportDispatcher.jobStarted(1)

        return snmpEngine.transportDispatcher
    # }}}

    async def stop_snmp(dispatcherTask):
        dispatcher = await dispatcherTask
        dispatcher.closeDispatcher()

async def run(cmdline:list[str]) -> None: # {{{
    await start_snmp()
    p = await asyncio.create_subprocess_exec(*cmdline)
    try:
        await p.communicate()
    except asyncio.CancelledError: #FIXME: keyboard interrupt
        try:
            p.kill()
        except Exception:
            pass
# }}}


if sys.argv[1:] != ['forever']:
    asyncio.run(run(sys.argv[1:]))
else:
    loop = asyncio.new_event_loop()
    t = loop.create_task(start_snmp())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(stop_snmp(t))
