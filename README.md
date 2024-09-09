SNMP importer
=============

Python snmp scraper that can write into influxdb, prometheus, prometheus push
gateway and compatible databases such as victoriametrics.

#### Why another snmp scraper?

H3C 5120 switches can respod fast on SNMP, when using SNMP bulk next commands. Typical
SNMP exporters use SNMP walk, which is painfully slow. Or they use SNMP bulk get commands,
but cannot walk SNMP tables, so they need to be reconfigured, whenever there is added
or removed logical port on target SNMP device. This scraper implements SNMP bulk walk,
i.e. it walks individual SNMP table rows in paralel.

Also it uses it's own OID registry, thus it does not depend on correct MIB.

#### Dependencies

* [https://github.com/lextudio/pysnmp](PySNMP) v7.0
* [https://github.com/lextudio/pysnmpcrypto](PySNMP crypto) v0.1 - for SNMP v3 authentication

#### Instalation
Install python3 (possibly from your linux distribution)
`cd vendor; ./checkout-vendor.sh`

Configuration
-------------

### inputs.yml

Inputs has two configuration mappings:

* alias - these are justs shortcuts for SNMP OID prefix, may be recursive (i.e., one alias can refer to other alias)
* input - two-level mapping of names for oids with possible transformation of value

Example:
```
alias:
  mib2:     1.3.6.1.2.1
  system:   mib2.1
  if:       mib2.2.2.1
  physical: mib2.47.1.1.1.1

input:
  system:
    description: system.1
    name:        system.5
  ifaces:
    description: if.2
    mac:         if.6 | mac
  physical:
    description:  physical.2
    manufactured: physical.17 | datetime
```

### tables.yml

Configuration of individual scrapes:

```
scrape_name:
  includes:
    - another_scrape_name
    - another_scrape_name
  tables:
    table_name:
      metrics:
        -
          # use ifaces.high_speed OID from inputs.yml
          expr: ifaces.high_speed

          # export this as table_name_speed{...} to prometheus
          # and as table_name,... speed= to influx
          suffix: speed

          # Use TYPE gauge in prometheus text help
          type: gauge

          # Prometheus text help
          help: Interface speed (megabytes)

        -
          # Define multiple similar metrics at once
          loop:
            # define values of ${dir}
            dir: [in, out]
            # define values of ${type}
            type: [unicast, multicast, broadcast]
          # ${dir} and ${type} gets expanded: thus ifaces.in_unicast will be one of expressions of one metric
          expr: 'ifaces.${dir}_${type}'
          # export this as prometheus metric table_name_errors{dir=...,type=...,...}
          suffix: errors
          # export this as influx metric table_name,... ${dir}_${type}=
          influx: '${dir}_${type}'
          type: counter
          help: Errors on interface

        -
          # You can python exporessions in 'expr',
          # but then you need to specify that used values should
          # be scraped.
          expr: 'ifaces.admin_status * 10 + ifaces.oper_status'
          # Export this as table_name in prometheus, do not export this in influx
          suffix: ''
          type: gauge
          help: interface status

      labels:
        # export admin and oper status as labels of main metric (with empty suffix) in prometheus
        # and as (possibly text) values in influx
        # This can be again python expressions, but in that case you need
        # to specify scraped values in `scraped` field below.
        admin_status: ifaces.admin_status
        oper_status:  ifaces.oper_status

      dimensions:
        # Export these as labels of main metric (with empty suffix) in prometheus
        # export these as dimensions (measurement tags) in influx
        mac:  ifaces.mac
        name: ifaces.name

      # List of inputs, that are only in expressions,
      # as we do not imlement heuristics to extract input
      # names from expressions, this is the place, where
      # we let the importer know what to scrape.
      scrape: []

      # Python code to compute inputs from other inputs
      # result can be used in expressions as transform.property_name,
      # i.e., `transform.vendor` in this example
      transform: |
        from mac_vendor_lookup import MacLookup
        lookup = MacLookup()
        lookup.update_vendors() # <- runs at configuration load

        class Transform:
          def __init__(self, inputs):
            self.vendor = lookup(inputs.ifaces.mac)
```

You can extend table from included scrape definition (add metrics, dimensions, labels, …) in another
scrape definition. This is useful for devices that extend, e.g., interfaces table by some vendor
specific table with same indexes.

### auth.yml

Auth.yml specifies SNMP credentials (community or SNMPv3 credentials) and assigns names to those
credentials. Thus you can have "public" schedule where only names of credentials are used and
private `auth.yml` file.

Example:
```
# typical v1/v2c SNMP credentials
public:
  community: public

# Fortigate HA credentials (community selects HA node)
# specify scrape host as hostname/serial_number or hostname:port/serial_number
fortigate-serial:
  community: 'verysecret-{path[1]}'

v3-example:
  user: username
  auth: auth-password
  # USM_AUTH_XXX from pysnmp/entity/config.py
  auth_alg: HMAC96_SHA
  priv: priv-password
  # USM_PRIV_XXX from pysnmp/entity/config.py
  priv_alg: CFB128_AES
```

### schedule.yml

Schedule specifies what to scrape (toplevel `tables.yml` entry), where to scrape (hostname, port,
and mayble additional arguments for auth if needed), auth (credentials name), and when to scrape.

Example:
```
- auth: public
  devices:
    switch-1: 10.0.0.1
    switch-2: 10.0.0.2
  schedule:
    ifaces_basic: '00:30'
    lldp:         '05:00+01:10'

- auth: fortigate-serial
  devices:
    firewall-node-A: 192.168.0.1/FG0123456789ABCD
    firewall-node-B: 192.168.0.1/FG0123456789EFGH
  schedule:
    ifaces_basic: '00:30+00:15'
    lldp:         '05:00+3:10'
```
This example will run scrape `ifaces_basic` at `*:*:00` and `*:*:30` on `10.0.0.1` and `10.0.0.2` with
credentials that are named `public` in `auth.yml`. Results from `10.0.0.1` will have label
`instance=switch-1` in prometheus and `host=switch-1` in influxdb.
It will also run `lldp` scrape at `*:?1:10` and `*:?6:10` on same devices. And it will run two scrapes
`ifaces_basic` on `192.168.0.1` with different community (see `auth.yml` example) at `*:*:15` and `*:*:45`,
and `lldp` scrapes at `*:?3:10` and `*:?8:10`.

### outputs.yml

This specifies destination(s) where are results written.
Example:
```
influx-database-1:
  type: influx
  url:  "http://localhost:8086/write?db=snmp"
  #use compression level 1
  gzip: 1

victoria-server-protobuf:
  type: prombuf
  url: http://localhost:8428/api/v1/write

vuctoria-server-prometheus-exposition:
  type: promtext
  url: http://localhost:8428/api/v1/import/prometheus
  gzip: 1
```

Names of outputs are just for documentation purposes. Every scrape is written to all specified outputs.

Url accepts also keyword `stdout`, which is reserved for testing purposes. See `-I` and `-P` commandline
options.
