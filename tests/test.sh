#!/bin/sh

if ! cd "$(dirname "$0")"; then
  echo "Failed to change directory" >&2
  exit 1
fi

DEST=localhost:6161
# FIXME: this is configured in auth.yml
AUTH=test

case "$#:$1" in
  0:)
    echo << __USAGE__
Usage: test.sh TEST
where TEST is:
  selftest      runs snmpwalk on test snmp server - this is for debugging problems with test framework
  influx        outputs colored influx from 'basic' table
  promtext      outputs colored promtext from 'vdx' table (that runs some python code transformation from config)
  forever       launches test server
  victoria [agent] [proto]
                writes to local victoriametrics
                using either prometheus exposition format or prometheus protobuf (proto)
                either directly or through vmagent (agent)
__USAGE__
  ;;
  1:forever)
    ./server.py forever
    ;;
  1:selftest)
    ./server.py snmpwalk -v2c -c "$AUTH" "$DEST" .1
    ;;
  1:influx)
    ./server.py ../snmp-importer.py -D . -I -T basic/"$DEST"/"$AUTH"
    ;;
  1:promtext)
    ./server.py ../snmp-importer.py -D . -P -T vdx/"$DEST"/"$AUTH"
    ;;
  1:influx-local)
    set -ex
    influx -execute 'drop database test; create database test;'
    ./server.py ../snmp-importer.py -D . -T vdx/"$DEST"/"$AUTH" -o '{"influx":{"type":"influx", "url":"http://localhost:8086/write?db=test","gzip":1}}'
    set +x
    influx -database test -execute 'show measurements'
    influx -database test -execute 'select * from snmp_importer_stats'
    influx -database test -execute 'select * from snmp_importer_errors'
    influx -database test -execute 'select * from snmp_sysinfo'
    influx -database test -execute 'select * from snmp_interfaces'
    ;;
  *:victoria)
    shift
    SERVER=http://localhost:8428/api/v1
    FORMAT=text
    for ARG in "$@"; do
      case "$ARG" in
	agent) SERVER=http://localhost:8429/api/v1 ;;
	*://*) SERVER="$ARG" ;;
	text|proto)  FORMAT="$ARG" ;;
	*) echo "Unsupported victoria argument $ARG"; exit 1 ;;
      esac
    done
    case "$FORMAT" in
      text)  O='{"victoria":{"type":"promtext", "url":"'"$SERVER"'/import/prometheus","gzip":1}}' ;;
      proto) O='{"victoria":{"type":"prombuf",  "url":"'"$SERVER"'/write"}}' ;;
      *) echo "Assertion error"; exit 1 ;;
    esac
    set -x
    ./server.py ../snmp-importer.py -D . -T vdx/"$DEST"/"$AUTH" -o "$O"
    ;;
  1:schedule)
    set -x
    ./server.py ../snmp-importer.py -D . -I -s '[{"auth":"test","devices":{"CMDLINE":"'"$DEST"'"},"schedule":{"basic":"00:05+00:01"}}]' -R 21
    ;;
  *)
    echo "Not supported: $@"
    exit 1;
    ;;
esac
